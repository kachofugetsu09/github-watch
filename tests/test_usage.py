from __future__ import annotations

from typing import Any

from github_watch_test_package.usage import (
    TurnUsage,
    UsageLedger,
    call_ids,
    measure_turn,
    summarize,
)
from plugins.ledger.contract import ContentPart, Output
from plugins.models.contract import ModelCallStats, ModelUsage
from plugins.turn_projection.contract import Turn


class FakeMessage:
    """Minimal stand-in exposing only the parts the projection reads."""

    def __init__(self, message_id: str, call_record_ids: tuple[str, ...]) -> None:
        self.message_id = message_id
        self.body = Output(
            parts=tuple(
                ContentPart("model.facts", {"call_record_id": identity})
                for identity in call_record_ids
            )
            + (ContentPart("text", "ignored"),),
            finish="complete",
        )


def stats_for(mapping: dict[str, ModelCallStats]):
    def lookup(identity: str) -> ModelCallStats:
        return mapping[identity]

    return lookup


def call_stats(identity: str, *, model: str, coverage: str, **usage: Any) -> ModelCallStats:
    return ModelCallStats(
        call_record_id=identity,
        model=model,
        state="success",
        first_token_ms=1.0,
        duration_ms=2.0,
        usage=ModelUsage(coverage=coverage, **usage),  # type: ignore[arg-type]
    )


def turn(*message_ids: str):
    """Build a closed turn that references the given messages."""

    return Turn(
        source="programmatic",
        after_seq=0,
        through_seq=len(message_ids),
        ending_message_id=message_ids[-1] if message_ids else None,
        status="complete",
        message_ids=message_ids,
        observations=(),
    )


def test_call_ids_dedupes_and_ignores_other_parts() -> None:
    messages = [
        FakeMessage("m1", ("call-a", "call-b")),
        FakeMessage("m2", ("call-b", "call-c")),
        FakeMessage("m3", ()),
    ]
    assert call_ids(messages) == ("call-a", "call-b", "call-c")


def test_measure_turn_sums_exact_usage_and_counts_excluded() -> None:
    # m0 belongs to an earlier turn of the same stable session and must not be re-counted.
    messages = [
        FakeMessage("m0", ("call-old",)),
        FakeMessage("m1", ("call-a", "call-b")),
        FakeMessage("m2", ("call-c",)),
    ]
    lookup = stats_for({
        "call-a": call_stats(
            "call-a", model="deepseek-v4-flash", coverage="exact",
            input_tokens=1000, cached_input_tokens=900, cache_write_input_tokens=50,
            output_tokens=40, reasoning_output_tokens=10,
        ),
        "call-b": call_stats(
            "call-b", model="deepseek-v4-flash", coverage="partial",
            input_tokens=500, cached_input_tokens=400, output_tokens=20,
        ),
        "call-c": call_stats(
            "call-c", model="glm-5.3", coverage="exact",
            input_tokens=200, cached_input_tokens=100, output_tokens=5,
        ),
    })
    usage = measure_turn(
        turn=turn("m1", "m2"),
        messages=messages,
        stat=lookup,
        event_key="owner/repo:pr:7:opened",
        repo="owner/repo",
        kind="pr",
        number=7,
        trigger_kind="opened",
        session_id="programmatic:github-watch:abc",
        recorded_at="2026-10-21T10:00:00+00:00",
    )
    # Only exact calls contribute tokens; the partial one is counted as excluded.
    assert usage.calls == 2 and usage.excluded_calls == 1
    assert usage.input_tokens == 1200
    assert usage.cached_input_tokens == 1000
    assert usage.cache_write_input_tokens == 50
    assert usage.output_tokens == 45
    assert usage.reasoning_output_tokens == 10
    assert usage.models == ("deepseek-v4-flash", "glm-5.3")
    assert usage.hit_rate == 1000 / 1200


def test_ledger_appends_once_per_event_and_survives_torn_line(tmp_path) -> None:
    path = tmp_path / "usage.jsonl"
    ledger = UsageLedger(path)
    first = _usage("owner/repo:pr:1:opened", number=1, input_tokens=100, cached=90)
    ledger.append(first)
    ledger.append(_usage("owner/repo:pr:1:opened", number=1, input_tokens=999, cached=999))
    assert [record.input_tokens for record in ledger.records()] == [100]

    # A partially written last line must not hide the records before it.
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version": 1, "event_key": "trunc')
    ledger.append(_usage("owner/repo:pr:2:opened", number=2, input_tokens=50, cached=25))
    assert {record.number for record in ledger.records()} == {1, 2}


def test_summarize_groups_per_object_and_averages_per_turn() -> None:
    # Two events of PR 1 (opened, then a comment) aggregate into one object.
    records = [
        _usage("owner/repo:pr:1:opened", number=1, input_tokens=1000, cached=900, output=10),
        _usage("owner/repo:pr:1:comment:9", number=1, input_tokens=500, cached=400, output=5),
        _usage("owner/repo:pr:2:opened", number=2, input_tokens=2000, cached=1000, output=20),
    ]
    report = summarize(records)
    totals = report["totals"]
    assert totals["objects"] == 2 and totals["turns"] == 3
    assert totals["input_tokens"] == 3500
    assert totals["cached_input_tokens"] == 2300
    assert abs(totals["hit_rate"] - 2300 / 3500) < 1e-9
    assert abs(totals["average_per_turn"]["input_tokens"] - 3500 / 3) < 1e-9

    # Sorted by input tokens so the most expensive object comes first.
    objects = report["objects"]
    assert [item["number"] for item in objects] == [2, 1]
    assert objects[0]["hit_rate"] == 0.5
    assert objects[1]["turns"] == 2


def _usage(
    event_key: str, *, number: int, input_tokens: int, cached: int, output: int = 0,
) -> TurnUsage:
    return TurnUsage(
        event_key=event_key,
        repo="owner/repo",
        kind="pr",
        number=number,
        trigger_kind="opened",
        session_id="programmatic:github-watch:abc",
        turn_status="complete",
        recorded_at="2026-10-21T10:00:00+00:00",
        models=("deepseek-v4-flash",),
        calls=1,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_input_tokens=0,
        output_tokens=output,
        reasoning_output_tokens=0,
        excluded_calls=0,
    )
