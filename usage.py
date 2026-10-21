"""Per-PR inference usage: turn cost recorded when an event settles.

Each Issue/PR has one stable programmatic session, so a finished turn maps to
exactly one event. Usage is read from the model call records the turn observed,
never estimated: a call whose provider usage is not exact is left out and counted
so the totals can say how much they are missing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugins.ledger.contract import ContentPart, Message
from plugins.models.contract import ModelCallStats
from plugins.turn_projection.contract import Turn

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """One settled turn's inference cost, already summed over its calls."""

    event_key: str
    repo: str
    kind: str
    number: int
    trigger_kind: str
    session_id: str
    turn_status: str
    recorded_at: str
    models: tuple[str, ...]
    calls: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    # Calls whose provider usage was not exact, so they contribute no tokens.
    excluded_calls: int

    @property
    def hit_rate(self) -> float | None:
        """Share of input tokens that came from cache; None when nothing to divide."""

        total = self.input_tokens
        return None if total <= 0 else self.cached_input_tokens / total

    def body(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "repo": self.repo,
            "kind": self.kind,
            "number": self.number,
            "trigger_kind": self.trigger_kind,
            "session_id": self.session_id,
            "turn_status": self.turn_status,
            "recorded_at": self.recorded_at,
            "models": list(self.models),
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "hit_rate": self.hit_rate,
            "excluded_calls": self.excluded_calls,
        }

    @classmethod
    def from_body(cls, payload: Mapping[str, Any]) -> "TurnUsage":
        return cls(
            event_key=str(payload["event_key"]),
            repo=str(payload["repo"]),
            kind=str(payload["kind"]),
            number=int(payload["number"]),
            trigger_kind=str(payload["trigger_kind"]),
            session_id=str(payload["session_id"]),
            turn_status=str(payload["turn_status"]),
            recorded_at=str(payload["recorded_at"]),
            models=tuple(str(name) for name in payload.get("models", ())),
            calls=int(payload["calls"]),
            input_tokens=int(payload["input_tokens"]),
            cached_input_tokens=int(payload["cached_input_tokens"]),
            cache_write_input_tokens=int(payload["cache_write_input_tokens"]),
            output_tokens=int(payload["output_tokens"]),
            reasoning_output_tokens=int(payload["reasoning_output_tokens"]),
            excluded_calls=int(payload["excluded_calls"]),
        )


def call_ids(messages: Iterable[Message]) -> tuple[str, ...]:
    """Collect the model call records a turn observed, in order, without duplicates."""

    seen: dict[str, None] = {}
    for message in messages:
        body = message.body
        parts = getattr(body, "parts", ())
        for part in parts:
            if not isinstance(part, ContentPart) or part.kind != "model.facts":
                continue
            raw: object = part.value
            if not isinstance(raw, Mapping):
                continue
            value = raw.get("call_record_id")
            if isinstance(value, str) and value:
                seen.setdefault(value, None)
    return tuple(seen)


def measure_turn(
    *,
    turn: Turn,
    messages: Iterable[Message],
    stat: Callable[[str], ModelCallStats],
    event_key: str,
    repo: str,
    kind: str,
    number: int,
    trigger_kind: str,
    session_id: str,
    recorded_at: str,
) -> TurnUsage:
    """Sum exact provider usage for one turn; count the calls that had none."""

    # Only this turn's messages: the stable session snapshot also holds every
    # earlier turn, whose calls were already settled with their own events.
    members = set(turn.message_ids)
    messages = [message for message in messages if message.message_id in members]
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    models: dict[str, None] = {}
    counted = excluded = 0
    for identity in call_ids(messages):
        record = stat(identity)
        usage = record.usage
        if usage is None or usage.coverage != "exact":
            excluded += 1
            continue
        counted += 1
        models.setdefault(record.model, None)
        totals["input_tokens"] += usage.input_tokens or 0
        totals["cached_input_tokens"] += usage.cached_input_tokens or 0
        totals["cache_write_input_tokens"] += usage.cache_write_input_tokens or 0
        totals["output_tokens"] += usage.output_tokens or 0
        totals["reasoning_output_tokens"] += usage.reasoning_output_tokens or 0
    return TurnUsage(
        event_key=event_key,
        repo=repo,
        kind=kind,
        number=number,
        trigger_kind=trigger_kind,
        session_id=session_id,
        turn_status=turn.status,
        recorded_at=recorded_at,
        models=tuple(models),
        calls=counted,
        excluded_calls=excluded,
        **totals,
    )


class UsageLedger:
    """Append-only JSONL of settled turns; one line per event settlement."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, usage: TurnUsage) -> None:
        """Append one record; a re-settled event is not written twice."""

        for existing in self.records():
            if existing.event_key == usage.event_key:
                return
        line = json.dumps(
            {"schema_version": SCHEMA_VERSION, **usage.body()},
            ensure_ascii=False,
            sort_keys=True,
        )
        # A crash can leave a torn last line without its newline. Start a fresh
        # line so one bad record cannot swallow the next one.
        needs_break = self.path.is_file() and self.path.stat().st_size > 0
        with self.path.open("a", encoding="utf-8") as handle:
            if needs_break and not self._ends_with_newline():
                handle.write("\n")
            handle.write(line + "\n")

    def _ends_with_newline(self) -> bool:
        with self.path.open("rb") as handle:
            handle.seek(-1, 2)
            return handle.read(1) == b"\n"

    def records(self) -> list[TurnUsage]:
        """Read every well-formed record; a torn last line is skipped, not fatal."""

        if not self.path.is_file():
            return []
        result: list[TurnUsage] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
                continue
            try:
                result.append(TurnUsage.from_body(payload))
            except (KeyError, TypeError, ValueError):
                continue
        return result


def summarize(records: Iterable[TurnUsage]) -> dict[str, Any]:
    """Aggregate per Issue/PR totals plus the overall and per-turn averages."""

    # One entry per Issue/PR: each event_key (opened, ready, every comment) is one turn of it.
    per_object: dict[tuple[str, str, int], dict[str, Any]] = {}
    for usage in records:
        entry = per_object.setdefault(
            (usage.repo, usage.kind, usage.number),
            {
                "repo": usage.repo,
                "kind": usage.kind,
                "number": usage.number,
                "trigger_kinds": [],
                "models": [],
                "turns": 0,
                "calls": 0,
                "excluded_calls": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "first_recorded_at": usage.recorded_at,
                "last_recorded_at": usage.recorded_at,
            },
        )
        for name in usage.models:
            if name not in entry["models"]:
                entry["models"].append(name)
        if usage.trigger_kind not in entry["trigger_kinds"]:
            entry["trigger_kinds"].append(usage.trigger_kind)
        entry["turns"] += 1
        entry["calls"] += usage.calls
        entry["excluded_calls"] += usage.excluded_calls
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            entry[field] += getattr(usage, field)
        entry["first_recorded_at"] = min(entry["first_recorded_at"], usage.recorded_at)
        entry["last_recorded_at"] = max(entry["last_recorded_at"], usage.recorded_at)

    objects = []
    for entry in per_object.values():
        total_input = entry["input_tokens"]
        entry["hit_rate"] = None if total_input <= 0 else entry["cached_input_tokens"] / total_input
        objects.append(entry)
    objects.sort(key=lambda item: (-item["input_tokens"], item["repo"], item["number"]))

    totals: dict[str, Any] = {
        "objects": len(objects),
        "turns": sum(item["turns"] for item in objects),
        "calls": sum(item["calls"] for item in objects),
        "excluded_calls": sum(item["excluded_calls"] for item in objects),
        "input_tokens": sum(item["input_tokens"] for item in objects),
        "cached_input_tokens": sum(item["cached_input_tokens"] for item in objects),
        "cache_write_input_tokens": sum(item["cache_write_input_tokens"] for item in objects),
        "output_tokens": sum(item["output_tokens"] for item in objects),
        "reasoning_output_tokens": sum(item["reasoning_output_tokens"] for item in objects),
    }
    total_input = totals["input_tokens"]
    totals["hit_rate"] = None if total_input <= 0 else totals["cached_input_tokens"] / total_input
    turns = totals["turns"]
    totals["average_per_turn"] = {
        field: (totals[field] / turns if turns else None)
        for field in ("input_tokens", "cached_input_tokens", "output_tokens")
    }
    return {"totals": totals, "objects": objects}
