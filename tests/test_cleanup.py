from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest

from plugins.ledger.contract import MESSAGE_CATALOG, Control, Input, Output
from plugins.turn_projection.plugin import TurnProjection
from plugins.ledger.log import MessageLog

from plugins.turn_projection.contract import TURN_PROJECTION
from github_watch_test_package.ledger import EventLedger
from github_watch_test_package.plugin import Runtime


def dispatch(ledger, key, input_id):
    """Create a dispatched event with the same durable states as the poller."""
    event = ledger.create_event(event_key=key, repo="owner/repo", kind="issue", number=1,
                                trigger_kind="owner_mention", trigger_id=key)
    assert event is not None
    for before, after in (("discovered", "claimed"), ("claimed", "context_ready"),
                          ("context_ready", "message_submitting"), ("message_submitting", "dispatched")):
        ledger.transition(key, expected=(before,), status=after, thread_id="s", input_message_id=input_id)
    return event


@pytest.fixture
def state(tmp_path):
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
    log = MessageLog(tmp_path / "sessions.db")
    inputs = log.writer("s", author="user", source="programmatic", body_types=(Input,), content={})
    outputs = log.writer("s", author="assistant", source="programmatic", body_types=(Output,), content={})
    removed = []
    # No models plugin: optional call stats borrow as None and usage is skipped.
    runtime = Runtime(SimpleNamespace(borrow=lambda _key: nullcontext(None)), SimpleNamespace(poll_seconds=15))
    runtime._bound = (ledger, SimpleNamespace(cleanup=lambda key: removed.append(key) or True), None, None)
    try:
        yield runtime, ledger, log, inputs, outputs, removed
    finally:
        log.close()


@pytest.mark.asyncio
async def test_cleanup_uses_changed_fixed_prefixes_and_checks_late_events(state):
    runtime, ledger, log, inputs, outputs, removed = state
    calls = []

    class Projection(TurnProjection):
        def project(self, messages, source):
            calls.append(tuple(message.seq for message in messages))
            return super().project(messages, source)

    inputs.append("a", Input(()))
    first = dispatch(ledger, "first", "a")
    projection = Projection()
    await runtime.cleanup_completed(log.catalog(), projection, {"s": 0})
    await runtime.cleanup_completed(log.catalog(), projection, {"s": 0, "unrelated": 10})
    assert calls == [(0,)] and removed == []

    inputs.append("b", Input(()))
    second = dispatch(ledger, "second", "b")
    outputs.append("done", Output((), "complete"))
    # The newer terminal is not visible until its exact prefix is observed.
    await runtime.cleanup_completed(log.catalog(), projection, {"s": 1})
    assert calls[-1] == (0, 1) and len(calls) == 2 and removed == []
    await runtime.cleanup_completed(log.catalog(), projection, {"s": 2})
    assert len(calls) == 3
    assert removed == [first.operation_id, second.operation_id]
    late = dispatch(ledger, "late", "b")
    await runtime.cleanup_completed(log.catalog(), projection, {"s": 2})
    assert removed[-1] == late.operation_id and len(calls) == 4
    assert ledger.dispatched_events() == [] and runtime._cleanup_heads == {}


@pytest.mark.asyncio
async def test_cleanup_keeps_scope_until_cancelled_worker_finishes(state):
    runtime, ledger, log, inputs, outputs, _ = state
    inputs.append("a", Input(()))
    outputs.append("done", Output((), "complete"))
    event = dispatch(ledger, "first", "a")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    scope = {"active": False}
    main_thread = threading.get_ident()

    def cleanup(_key):
        assert threading.get_ident() != main_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "event loop did not release the worker"
        assert scope["active"]
        return True

    runtime._bound = (ledger, SimpleNamespace(cleanup=cleanup), None, None)

    async def run():
        scope["active"] = True
        try:
            await runtime.cleanup_completed(log.catalog(), TurnProjection())
        finally:
            scope["active"] = False

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        for _ in range(2):
            task.cancel()
            heartbeat = asyncio.Event()
            loop.call_soon(heartbeat.set)
            await heartbeat.wait()
            assert not task.done() and scope["active"]
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not scope["active"]
    assert ledger.get_event(event.event_key).status == "completed"


@pytest.mark.asyncio
async def test_cleanup_failure_remains_dispatched_and_can_retry(state):
    runtime, ledger, log, inputs, outputs, removed = state
    inputs.append("a", Input(()))
    outputs.append("done", Output((), "complete"))
    event = dispatch(ledger, "first", "a")
    bound = runtime._bound

    def fail(_key):
        raise OSError("checkout is unavailable")

    runtime._bound = (ledger, SimpleNamespace(cleanup=fail), None, None)
    with pytest.raises(OSError, match="checkout is unavailable"):
        await runtime.cleanup_completed(log.catalog(), TurnProjection())
    assert ledger.get_event(event.event_key).status == "dispatched"
    runtime._bound = bound
    await runtime.cleanup_completed(log.catalog(), TurnProjection())
    assert removed == [event.operation_id]


@pytest.mark.asyncio
async def test_poll_checks_dispatch_that_arrived_after_message_wake(state):
    runtime, ledger, log, inputs, outputs, removed = state
    inputs.append("a", Input(()))
    outputs.append("done", Output((), "complete"))
    await runtime.cleanup_completed(log.catalog(), TurnProjection(), {"s": 1})
    stopped = asyncio.Event()

    class Context:
        @asynccontextmanager
        async def runtime_scope(self):
            yield

        def borrow(self, _key):
            return nullcontext(None)

        def require(self, key):
            if key == MESSAGE_CATALOG:
                return log.catalog()
            if key == TURN_PROJECTION:
                return TurnProjection()
            return None  # PROGRAMMATIC is unused by this controlled poll.

    class Watch:
        async def poll(self, _repos, _messages):
            dispatch(ledger, "late", "a")

    async def wait(_deadline):
        stopped.set()
        await asyncio.Future()

    runtime._ctx = Context()
    runtime._config = SimpleNamespace(poll_seconds=15, repositories=["owner/repo"])
    runtime._bound = (*runtime._bound[:3], Watch())
    runtime._wait = wait
    task = asyncio.create_task(runtime._poll_loop())
    try:
        await asyncio.wait_for(stopped.wait(), 3)
        assert len(removed) == 1 and ledger.dispatched_events() == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_dispatched_retries_failure_then_abandons_and_settles(tmp_path):
    from agent.plugin_composition.tasks import RESTART_GATE, TASKS, Tasks
    from plugins.content.contract import CONTENT
    from plugins.ledger.contract import MESSAGE_WRITERS, ContentPart, ContentReferences, SessionAttributes
    from plugins.sources.contract import SOURCE_SESSION_V4 as SOURCE_SESSION
    from plugins.sources.session import SourceSession
    from plugins.models.contract import MODEL_CALL_STATS, ModelCallStats, ModelUsage
    from github_watch_test_package.usage import UsageLedger

    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    ledger.insert_item("owner/repo", "pr", 1, "t1", 0)
    event = ledger.create_event(
        event_key="owner/repo:pr:1:opened",
        repo="owner/repo",
        kind="pr",
        number=1,
        trigger_kind="opened",
        trigger_id="1",
    )
    assert event is not None
    for before, after in (
        ("discovered", "claimed"),
        ("claimed", "context_ready"),
        ("context_ready", "message_submitting"),
        ("message_submitting", "dispatched"),
    ):
        ledger.transition(
            event.event_key,
            expected=(before,),
            status=after,
            thread_id="s-fail",
            input_message_id="in-1",
        )

    log = MessageLog(tmp_path / "sessions.db")
    log.ensure_session("s-fail", SessionAttributes(visibility="internal", learning="excluded"))
    writers = SimpleNamespace(
        bind=lambda _ctx, *, author, source, body_types, content: lambda sid: log.writer(
            sid, author=author, source=source, body_types=body_types, content=content
        )
    )
    tasks = Tasks()
    removed = []

    class FakeCtx:
        def require(self, key):
            if key == SOURCE_SESSION:
                return SourceSession
            if key == MESSAGE_CATALOG:
                return log.catalog()
            if key == MESSAGE_WRITERS:
                return writers
            if key == TASKS:
                return SimpleNamespace(open=lambda _c: tasks)
            if key == RESTART_GATE:
                return None
            if key == CONTENT:
                return SimpleNamespace(check_text=lambda _part: None)
            raise KeyError(key)

        def borrow(self, key):
            if key == MODEL_CALL_STATS:
                return nullcontext(
                    lambda call_id: ModelCallStats(
                        call_record_id=call_id,
                        model="gpt-test",
                        state="success",
                        first_token_ms=10,
                        duration_ms=100,
                        usage=ModelUsage(input_tokens=100, output_tokens=25, coverage="exact"),
                    )
                )
            return nullcontext(None)

    usage = UsageLedger(tmp_path / "usage.jsonl")
    runtime = Runtime(
        FakeCtx(),
        SimpleNamespace(
            poll_seconds=15,
            failure_retry_limit=1,
            failure_retry_delay_seconds=60,
        ),
    )
    runtime._bound = (ledger, SimpleNamespace(cleanup=lambda key: removed.append(key) or True), None, None)
    runtime._usage = usage

    executions = 0

    async def fail_turn(_task, _reader, _source):
        nonlocal executions
        executions += 1
        # Record one assistant partial call before failing, so usage accounting is verified.
        assistant = log.writer(
            "s-fail",
            author="assistant",
            source="github-watch",
            body_types=(Output,),
            content={"model.facts": lambda _p: ContentReferences()},
        )
        assistant.append("call-1", Output((ContentPart("model.facts", {"call_record_id": "c-1"}),), "continue"))
        raise RuntimeError("upstream reset by peer")

    runtime._execute_turn = fail_turn
    # 1. Seed initial Input and execute -> Control("failure") written, still dispatched.
    gw_inputs = log.writer("s-fail", author="github-watch", source="github-watch", body_types=(Input,), content={})
    gw_inputs.append("in-1", Input(()))
    await runtime.run_dispatched()
    await runtime.cleanup_completed(log.catalog(), TurnProjection())
    assert executions == 1
    assert ledger.get_event(event.event_key).status == "dispatched"
    assert removed == []

    # 2. During cooldown (60s), run_dispatched must wait and neither re-execute nor write controls.
    await runtime.run_dispatched()
    assert executions == 1
    assert ledger.get_event(event.event_key).status == "dispatched"
    controls = [m.body for m in log.catalog().reader("s-fail").snapshot() if isinstance(m.body, Control)]
    assert [c.action for c in controls] == ["failure"]

    # 3. Once cooldown elapses, retry runs once (failure_retries becomes 1) and fails again.
    runtime._config.failure_retry_delay_seconds = 0
    await runtime.run_dispatched()
    assert executions == 2
    assert ledger.get_event(event.event_key).status == "dispatched"
    controls = [m.body for m in log.catalog().reader("s-fail").snapshot() if isinstance(m.body, Control)]
    assert [c.action for c in controls] == ["failure", "resume", "failure"]

    # 4. Retry budget exhausted: next pass writes Control("abandon") without re-executing and cleanup settles to failed.
    await runtime.run_dispatched()
    assert executions == 2
    await runtime.cleanup_completed(log.catalog(), TurnProjection())
    settled = ledger.get_event(event.event_key)
    assert settled.status == "failed"
    assert removed == [event.operation_id]
    controls = [m.body for m in log.catalog().reader("s-fail").snapshot() if isinstance(m.body, Control)]
    assert [c.action for c in controls] == ["failure", "resume", "failure", "abandon"]
    records = usage.records()
    assert len(records) == 1 and records[0].turn_status == "abandoned" and records[0].calls == 1
    log.close()


@pytest.mark.asyncio
async def test_run_dispatched_retry_succeeds_and_completes(tmp_path):
    from agent.plugin_composition.tasks import RESTART_GATE, TASKS, Tasks
    from plugins.content.contract import CONTENT
    from plugins.ledger.contract import MESSAGE_WRITERS, ContentPart, ContentReferences, SessionAttributes
    from plugins.sources.contract import SOURCE_SESSION_V4 as SOURCE_SESSION
    from plugins.sources.session import SourceSession

    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    ledger.insert_item("owner/repo", "pr", 2, "t2", 0)
    event = ledger.create_event(
        event_key="owner/repo:pr:2:opened",
        repo="owner/repo",
        kind="pr",
        number=2,
        trigger_kind="opened",
        trigger_id="2",
    )
    assert event is not None
    for before, after in (
        ("discovered", "claimed"),
        ("claimed", "context_ready"),
        ("context_ready", "message_submitting"),
        ("message_submitting", "dispatched"),
    ):
        ledger.transition(
            event.event_key,
            expected=(before,),
            status=after,
            thread_id="s-ok",
            input_message_id="in-2",
        )

    log = MessageLog(tmp_path / "sessions.db")
    log.ensure_session("s-ok", SessionAttributes(visibility="internal", learning="excluded"))
    writers = SimpleNamespace(
        bind=lambda _ctx, *, author, source, body_types, content: lambda sid: log.writer(
            sid, author=author, source=source, body_types=body_types, content=content
        )
    )
    tasks = Tasks()
    removed = []

    class FakeCtx:
        def require(self, key):
            if key == SOURCE_SESSION:
                return SourceSession
            if key == MESSAGE_CATALOG:
                return log.catalog()
            if key == MESSAGE_WRITERS:
                return writers
            if key == TASKS:
                return SimpleNamespace(open=lambda _c: tasks)
            if key == RESTART_GATE:
                return None
            if key == CONTENT:
                return SimpleNamespace(check_text=lambda _part: None)
            raise KeyError(key)

        def borrow(self, _key):
            return nullcontext(None)

    runtime = Runtime(
        FakeCtx(),
        SimpleNamespace(
            poll_seconds=15,
            failure_retry_limit=1,
            failure_retry_delay_seconds=0,
        ),
    )
    runtime._bound = (ledger, SimpleNamespace(cleanup=lambda key: removed.append(key) or True), None, None)

    attempts = 0

    async def flaky_turn(_task, _reader, _source):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient network drop")
        assistant = log.writer(
            "s-ok",
            author="assistant",
            source="github-watch",
            body_types=(Output,),
            content={"text": lambda _p: ContentReferences()},
        )
        return assistant.append("out-ok", Output((ContentPart("text", "ok"),), "complete"))

    runtime._execute_turn = flaky_turn
    gw_inputs = log.writer("s-ok", author="github-watch", source="github-watch", body_types=(Input,), content={})
    gw_inputs.append("in-2", Input(()))

    await runtime.run_dispatched()
    assert ledger.get_event(event.event_key).status == "dispatched"

    await runtime.run_dispatched()
    await runtime.cleanup_completed(log.catalog(), TurnProjection())
    assert ledger.get_event(event.event_key).status == "completed"
    assert removed == [event.operation_id]
    assert attempts == 2
    log.close()


@pytest.mark.asyncio
async def test_reconcile_failed_does_not_bypass_intervening_pause(tmp_path):
    from agent.plugin_composition.tasks import RESTART_GATE, TASKS, Tasks
    from plugins.content.contract import CONTENT
    from plugins.ledger.contract import MESSAGE_WRITERS, SessionAttributes
    from plugins.sources.contract import SOURCE_SESSION_V4 as SOURCE_SESSION
    from plugins.sources.session import SourceSession

    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    ledger.insert_item("owner/repo", "pr", 3, "t3", 0)
    event = ledger.create_event(
        event_key="owner/repo:pr:3:opened",
        repo="owner/repo",
        kind="pr",
        number=3,
        trigger_kind="opened",
        trigger_id="3",
    )
    assert event is not None
    for before, after in (
        ("discovered", "claimed"),
        ("claimed", "context_ready"),
        ("context_ready", "message_submitting"),
        ("message_submitting", "dispatched"),
    ):
        ledger.transition(
            event.event_key,
            expected=(before,),
            status=after,
            thread_id="s-pause",
            input_message_id="in-3",
        )

    log = MessageLog(tmp_path / "sessions.db")
    log.ensure_session("s-pause", SessionAttributes(visibility="internal", learning="excluded"))
    gw_inputs = log.writer("s-pause", author="github-watch", source="github-watch", body_types=(Input,), content={})
    gw_inputs.append("in-3", Input(()))
    gw_controls = log.writer("s-pause", author="github-watch", source="github-watch", body_types=(Control,), content={})
    gw_controls.append("fail-3", Control("failure", 0, "network drop"))

    orig_claim = ledger.claim_failure_retry

    def claim_and_intervene(*args, **kwargs):
        decision = orig_claim(*args, **kwargs)
        # Concurrent pause lands after _pending_failure observed head=1, before CAS resume.
        gw_controls.append("pause-3", Control("pause", 1, "operator paused"))
        return decision

    ledger.claim_failure_retry = claim_and_intervene  # type: ignore[method-assign]

    writers = SimpleNamespace(
        bind=lambda _ctx, *, author, source, body_types, content: lambda sid: log.writer(
            sid, author=author, source=source, body_types=body_types, content=content
        )
    )
    tasks = Tasks()

    class FakeCtx:
        def require(self, key):
            if key == SOURCE_SESSION:
                return SourceSession
            if key == MESSAGE_CATALOG:
                return log.catalog()
            if key == MESSAGE_WRITERS:
                return writers
            if key == TASKS:
                return SimpleNamespace(open=lambda _c: tasks)
            if key == RESTART_GATE:
                return None
            if key == CONTENT:
                return SimpleNamespace(check_text=lambda _part: None)
            raise KeyError(key)

        def borrow(self, _key):
            return nullcontext(None)

    runtime = Runtime(
        FakeCtx(),
        SimpleNamespace(poll_seconds=15, failure_retry_limit=1, failure_retry_delay_seconds=0),
    )
    runtime._bound = (ledger, SimpleNamespace(cleanup=lambda _key: True), None, None)

    await runtime.run_dispatched()
    controls = [m.body for m in log.catalog().reader("s-pause").snapshot() if isinstance(m.body, Control)]
    assert [c.action for c in controls] == ["failure", "pause"]
    assert not await SourceSession.needs_reply(log.catalog().reader("s-pause"), "github-watch")
    log.close()


@pytest.mark.asyncio
async def test_legacy_programmatic_failure_and_github_watch_settle_independently_in_same_session(tmp_path):
    from plugins.ledger.contract import ContentPart, ContentReferences, SessionAttributes
    from plugins.sources.contract import SOURCE_SESSION_V4 as SOURCE_SESSION
    from plugins.sources.session import SourceSession

    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    ledger.insert_item("owner/repo", "pr", 4, "t4", 0)

    old_event = ledger.create_event(
        event_key="owner/repo:pr:4:opened",
        repo="owner/repo",
        kind="pr",
        number=4,
        trigger_kind="opened",
        trigger_id="4",
    )
    new_event = ledger.create_event(
        event_key="owner/repo:pr:4:comment:9",
        repo="owner/repo",
        kind="pr",
        number=4,
        trigger_kind="comment",
        trigger_id="9",
    )
    assert old_event is not None and new_event is not None
    for ev, mid in ((old_event, "in-prog"), (new_event, "in-gw")):
        for before, after in (
            ("discovered", "claimed"),
            ("claimed", "context_ready"),
            ("context_ready", "message_submitting"),
            ("message_submitting", "dispatched"),
        ):
            ledger.transition(
                ev.event_key,
                expected=(before,),
                status=after,
                thread_id="s-mixed",
                input_message_id=mid,
            )

    log = MessageLog(tmp_path / "sessions.db")
    log.ensure_session("s-mixed", SessionAttributes(visibility="internal", learning="excluded"))
    prog_in = log.writer("s-mixed", author="programmatic", source="programmatic", body_types=(Input,), content={})
    prog_ctrl = log.writer("s-mixed", author="programmatic", source="programmatic", body_types=(Control,), content={})
    prog_in.append("in-prog", Input(()))
    prog_ctrl.append("fail-prog", Control("failure", 0, "legacy failure"))

    gw_in = log.writer("s-mixed", author="github-watch", source="github-watch", body_types=(Input,), content={})
    gw_out = log.writer(
        "s-mixed",
        author="assistant",
        source="github-watch",
        body_types=(Output,),
        content={"text": lambda _p: ContentReferences()},
    )
    gw_in.append("in-gw", Input(()))
    gw_out.append("out-gw", Output((ContentPart("text", "done"),), "complete"))

    removed = []

    class FakeCtx:
        def require(self, key):
            if key == SOURCE_SESSION:
                return SourceSession
            if key == MESSAGE_CATALOG:
                return log.catalog()
            raise KeyError(key)

        def borrow(self, _key):
            return nullcontext(None)

    runtime = Runtime(
        FakeCtx(),
        SimpleNamespace(poll_seconds=15, failure_retry_limit=1, failure_retry_delay_seconds=0),
    )
    runtime._bound = (ledger, SimpleNamespace(cleanup=lambda key: removed.append(key) or True), None, None)

    await runtime.run_dispatched()
    await runtime.cleanup_completed(log.catalog(), TurnProjection())

    assert ledger.get_event(old_event.event_key).status == "manual_reconcile"
    assert ledger.get_event(new_event.event_key).status == "completed"
    # Legacy programmatic source log is never mutated by github-watch, and its checkout is kept for TTL.
    prog_controls = [
        m.body for m in log.catalog().reader("s-mixed").snapshot()
        if m.source == "programmatic" and isinstance(m.body, Control)
    ]
    assert [c.action for c in prog_controls] == ["failure"]
    assert removed == [new_event.operation_id]
    log.close()


@pytest.mark.asyncio
async def test_legacy_programmatic_multi_input_failure_and_interleaved_resume_complete(tmp_path):
    from plugins.ledger.contract import ContentPart, ContentReferences, SessionAttributes
    from plugins.sources.contract import SOURCE_SESSION_V4 as SOURCE_SESSION
    from plugins.sources.session import SourceSession

    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    ledger.insert_item("owner/repo", "pr", 8, "t8", 0)
    ev_a = ledger.create_event(
        event_key="owner/repo:pr:8:opened",
        repo="owner/repo",
        kind="pr",
        number=8,
        trigger_kind="opened",
        trigger_id="8",
    )
    ev_b = ledger.create_event(
        event_key="owner/repo:pr:8:comment:1",
        repo="owner/repo",
        kind="pr",
        number=8,
        trigger_kind="comment",
        trigger_id="1",
    )
    assert ev_a is not None and ev_b is not None
    for ev, mid in ((ev_a, "in-a"), (ev_b, "in-b")):
        for before, after in (
            ("discovered", "claimed"),
            ("claimed", "context_ready"),
            ("context_ready", "message_submitting"),
            ("message_submitting", "dispatched"),
        ):
            ledger.transition(
                ev.event_key,
                expected=(before,),
                status=after,
                thread_id="s-multi-prog",
                input_message_id=mid,
            )

    log = MessageLog(tmp_path / "sessions.db")
    log.ensure_session("s-multi-prog", SessionAttributes(visibility="internal", learning="excluded"))
    prog_in = log.writer("s-multi-prog", author="programmatic", source="programmatic", body_types=(Input,), content={})
    prog_ctrl = log.writer("s-multi-prog", author="programmatic", source="programmatic", body_types=(Control,), content={})
    prog_out = log.writer(
        "s-multi-prog",
        author="assistant",
        source="programmatic",
        body_types=(Output,),
        content={"text": lambda _p: ContentReferences()},
    )
    # Input A -> Input B -> Control("failure")
    prog_in.append("in-a", Input(()))
    prog_in.append("in-b", Input(()))
    prog_ctrl.append("fail-ab", Control("failure", 1, "both failed"))

    removed = []

    class FakeCtx:
        def require(self, key):
            if key == SOURCE_SESSION:
                return SourceSession
            if key == MESSAGE_CATALOG:
                return log.catalog()
            raise KeyError(key)

        def borrow(self, _key):
            return nullcontext(None)

    runtime = Runtime(FakeCtx(), SimpleNamespace(poll_seconds=15, failure_retry_limit=1, failure_retry_delay_seconds=0))
    runtime._bound = (ledger, SimpleNamespace(cleanup=lambda key: removed.append(key) or True), None, None)

    await runtime.run_dispatched()
    await runtime.cleanup_completed(log.catalog(), TurnProjection())
    assert ledger.get_event(ev_a.event_key).status == "manual_reconcile"
    assert ledger.get_event(ev_b.event_key).status == "manual_reconcile"
    assert removed == []

    # Now test interleaving: another event is at failure, then resumed and completed before cleanup runs.
    ev_c = ledger.create_event(
        event_key="owner/repo:pr:8:comment:2",
        repo="owner/repo",
        kind="pr",
        number=8,
        trigger_kind="comment",
        trigger_id="2",
    )
    assert ev_c is not None
    for before, after in (
        ("discovered", "claimed"),
        ("claimed", "context_ready"),
        ("context_ready", "message_submitting"),
        ("message_submitting", "dispatched"),
    ):
        ledger.transition(
            ev_c.event_key,
            expected=(before,),
            status=after,
            thread_id="s-multi-prog",
            input_message_id="in-c",
        )
    prog_in.append("in-c", Input(()))
    prog_ctrl.append("fail-c", Control("failure", 3, "transient"))
    prog_ctrl.append("resume-c", Control("resume", 4))
    prog_out.append("done-c", Output((ContentPart("text", "recovered"),), "complete"))

    await runtime.run_dispatched()
    await runtime.cleanup_completed(log.catalog(), TurnProjection())
    assert ledger.get_event(ev_c.event_key).status == "completed"
    assert removed == [ev_c.operation_id]

    # Stale head notification (head=1) arriving after resume+complete (head=3) must NOT mark manual_reconcile.
    ev_d = ledger.create_event(
        event_key="owner/repo:pr:8:comment:3",
        repo="owner/repo",
        kind="pr",
        number=8,
        trigger_kind="comment",
        trigger_id="3",
    )
    assert ev_d is not None
    for before, after in (
        ("discovered", "claimed"),
        ("claimed", "context_ready"),
        ("context_ready", "message_submitting"),
        ("message_submitting", "dispatched"),
    ):
        ledger.transition(
            ev_d.event_key,
            expected=(before,),
            status=after,
            thread_id="s-stale-head",
            input_message_id="in-d",
        )
    log.ensure_session("s-stale-head", SessionAttributes(visibility="internal", learning="excluded"))
    d_in = log.writer("s-stale-head", author="programmatic", source="programmatic", body_types=(Input,), content={})
    d_ctrl = log.writer("s-stale-head", author="programmatic", source="programmatic", body_types=(Control,), content={})
    d_out = log.writer(
        "s-stale-head",
        author="assistant",
        source="programmatic",
        body_types=(Output,),
        content={"text": lambda _p: ContentReferences()},
    )
    d_in.append("in-d", Input(()))
    d_ctrl.append("fail-d", Control("failure", 0, "transient"))
    d_ctrl.append("resume-d", Control("resume", 1))
    d_out.append("done-d", Output((ContentPart("text", "recovered"),), "complete"))

    await runtime.cleanup_completed(log.catalog(), TurnProjection(), {"s-stale-head": 1})
    assert ledger.get_event(ev_d.event_key).status == "dispatched"
    assert removed == [ev_c.operation_id]
    await runtime.cleanup_completed(log.catalog(), TurnProjection(), {"s-stale-head": 3})
    assert ledger.get_event(ev_d.event_key).status == "completed"
    assert removed == [ev_c.operation_id, ev_d.operation_id]
    log.close()


@pytest.mark.asyncio
async def test_migration_clears_stale_cleanup_head_and_settles_without_new_messages(tmp_path):
    import sqlite3
    from plugins.ledger.contract import ContentPart, ContentReferences, SessionAttributes

    db_path = tmp_path / "events.sqlite3"
    log = MessageLog(tmp_path / "sessions.db")
    log.ensure_session("s-mig", SessionAttributes(visibility="internal", learning="excluded"))
    log.writer("s-mig", author="programmatic", source="programmatic", body_types=(Input,), content={}).append("in-old", Input(()))
    log.writer(
        "s-mig",
        author="assistant",
        source="programmatic",
        body_types=(Output,),
        content={"text": lambda _p: ContentReferences()},
    ).append("out-old", Output((ContentPart("text", "done"),), "complete"))
    log.writer("s-mig", author="github-watch", source="github-watch", body_types=(Input,), content={}).append("in-new", Input(()))

    # Build a pre-migration events.sqlite3 (without failure_retries/last_failure_seq) with stale cleanup_head.
    revision = log.catalog().prefix_revision()
    assert revision is not None
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE items(
                repo TEXT NOT NULL, kind TEXT NOT NULL, number INTEGER NOT NULL,
                thread_id TEXT, last_updated_at TEXT NOT NULL, last_comment_id INTEGER NOT NULL DEFAULT 0,
                draft INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(repo, kind, number)
            );
            CREATE TABLE events(
                event_key TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                repo TEXT NOT NULL, kind TEXT NOT NULL, number INTEGER NOT NULL,
                trigger_kind TEXT NOT NULL, trigger_id TEXT NOT NULL, status TEXT NOT NULL,
                thread_id TEXT, turn_id TEXT, input_message_id TEXT, model_pin TEXT,
                response TEXT, artifact_id TEXT, error TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, cleanup_head INTEGER
            );
            """
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('cleanup_prefix_revision', ?)", (str(revision),))
        conn.execute(
            "INSERT INTO items VALUES('owner/repo', 'pr', 6, 's-mig', 't1', 0, 0, 't1', 't1')"
        )
        conn.execute(
            """
            INSERT INTO events(event_key, operation_id, repo, kind, number, trigger_kind, trigger_id,
                               status, thread_id, input_message_id, created_at, updated_at, cleanup_head)
            VALUES('owner/repo:pr:6:opened', '00000000000000000000000000000006', 'owner/repo', 'pr', 6,
                   'opened', '6', 'dispatched', 's-mig', 'in-old', 't1', 't1', 2)
            """
        )

    # Inject failure inside _migrate after DDL to prove DDL + cleanup_head clear commit atomically.
    orig_migrate = EventLedger._migrate

    def flaky_migrate(self, connection):
        orig_migrate(self, connection)
        raise RuntimeError("injected crash during migration")

    EventLedger._migrate = flaky_migrate  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            EventLedger(db_path)
    finally:
        EventLedger._migrate = orig_migrate  # type: ignore[method-assign]

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert "failure_retries" not in cols
        assert conn.execute("SELECT cleanup_head FROM events").fetchone()[0] == 2

    # Reopening EventLedger triggers clean atomic migration and invalidates old cleanup_head.
    ledger = EventLedger(db_path)
    removed = []
    runtime = Runtime(SimpleNamespace(borrow=lambda _key: nullcontext(None)), SimpleNamespace(poll_seconds=15))
    runtime._bound = (ledger, SimpleNamespace(cleanup=lambda key: removed.append(key) or True), None, None)

    # Without adding any new messages, the previously shadowed legacy event now settles cleanly.
    await runtime.cleanup_completed(log.catalog(), TurnProjection())
    assert ledger.get_event("owner/repo:pr:6:opened").status == "completed"
    assert removed == ["00000000000000000000000000000006"]
    log.close()


@pytest.mark.asyncio
async def test_reconcile_failed_respects_restart_gate_and_holds_permit(tmp_path):
    import sys
    from agent.plugin_composition.tasks import RESTART_GATE, TASKS, Tasks
    from agent.restart import RestartGate
    from plugins.content.contract import CONTENT
    from plugins.ledger.contract import MESSAGE_WRITERS, SessionAttributes
    from plugins.sources.contract import SOURCE_SESSION_V4 as SOURCE_SESSION
    from plugins.sources.session import SourceSession

    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    ledger.insert_item("owner/repo", "pr", 7, "t7", 0)
    event = ledger.create_event(
        event_key="owner/repo:pr:7:opened",
        repo="owner/repo",
        kind="pr",
        number=7,
        trigger_kind="opened",
        trigger_id="7",
    )
    assert event is not None
    for before, after in (
        ("discovered", "claimed"),
        ("claimed", "context_ready"),
        ("context_ready", "message_submitting"),
        ("message_submitting", "dispatched"),
    ):
        ledger.transition(
            event.event_key,
            expected=(before,),
            status=after,
            thread_id="s-gate",
            input_message_id="in-7",
        )

    log = MessageLog(tmp_path / "sessions.db")
    log.ensure_session("s-gate", SessionAttributes(visibility="internal", learning="excluded"))
    log.writer("s-gate", author="github-watch", source="github-watch", body_types=(Input,), content={}).append("in-7", Input(()))
    log.writer("s-gate", author="github-watch", source="github-watch", body_types=(Control,), content={}).append("fail-7", Control("failure", 0, "err"))

    gate = RestartGate(boot_id="boot-1", supervised=False)
    gate.prepare_stop("stop-req-1")

    writers = SimpleNamespace(
        bind=lambda _ctx, *, author, source, body_types, content: lambda sid: log.writer(
            sid, author=author, source=source, body_types=body_types, content=content
        )
    )
    tasks = Tasks()

    class Ctx:
        def require(self, key):
            if key == SOURCE_SESSION:
                return SourceSession
            if key == MESSAGE_CATALOG:
                return log.catalog()
            if key == MESSAGE_WRITERS:
                return writers
            if key == TASKS:
                return SimpleNamespace(open=lambda _c: tasks)
            if key == RESTART_GATE:
                return gate
            if key == CONTENT:
                return SimpleNamespace(check_text=lambda _part: None)
            raise KeyError(key)

        def borrow(self, key):
            if key == RESTART_GATE:
                return nullcontext(gate)
            return nullcontext(None)

    rt = Runtime(Ctx(), SimpleNamespace(poll_seconds=15, failure_retry_limit=1, failure_retry_delay_seconds=0))
    rt._bound = (ledger, SimpleNamespace(cleanup=lambda _key: True), None, None)

    # 1. Closed gate blocks resume before admission; log remains on Control("failure").
    await rt.run_dispatched()
    controls = [m.body for m in log.catalog().reader("s-gate").snapshot() if isinstance(m.body, Control)]
    assert [c.action for c in controls] == ["failure"]

    # 2. Reopen gate: verify permit is held during session.control() and released afterwards.
    gate.abort("stop-req-1")
    seen_permits = []
    orig_open_source = sys.modules[Runtime.__module__].open_source

    class ObservingSession:
        def __init__(self, inner):
            self._inner = inner

        async def control(self, *args, **kwargs):
            seen_permits.append(gate.permit_count)
            return await self._inner.control(*args, **kwargs)

        async def start(self, program):
            return await self._inner.start(program)

    async def ok_turn(_task, _reader, _source):
        from plugins.ledger.contract import ContentPart, ContentReferences
        out = log.writer(
            "s-gate",
            author="assistant",
            source="github-watch",
            body_types=(Output,),
            content={"text": lambda _p: ContentReferences()},
        )
        return out.append("ok-7", Output((ContentPart("text", "ok"),), "complete"))

    rt._execute_turn = ok_turn
    sys.modules[Runtime.__module__].open_source = lambda ctx, sid: ObservingSession(orig_open_source(ctx, sid))
    try:
        await rt.run_dispatched()
    finally:
        sys.modules[Runtime.__module__].open_source = orig_open_source

    assert seen_permits == [1]
    assert gate.permit_count == 0
    controls_after = [m.body for m in log.catalog().reader("s-gate").snapshot() if isinstance(m.body, Control)]
    assert [c.action for c in controls_after] == ["failure", "resume"]
    log.close()


@pytest.mark.asyncio
async def test_failure_recovery_across_restarted_runtimes_and_ledgers(tmp_path):
    import sys
    from agent.plugin_composition.tasks import RESTART_GATE, TASKS, Tasks
    from plugins.content.contract import CONTENT
    from plugins.ledger.contract import MESSAGE_WRITERS, ContentPart, ContentReferences, SessionAttributes
    from plugins.sources.contract import SOURCE_SESSION_V4 as SOURCE_SESSION
    from plugins.sources.session import SourceSession

    ledger_path = tmp_path / "events.sqlite3"
    db_path = tmp_path / "sessions.db"

    ledger1 = EventLedger(ledger_path)
    ledger1.establish_baseline("owner/repo", [])
    ledger1.insert_item("owner/repo", "pr", 5, "t5", 0)
    event = ledger1.create_event(
        event_key="owner/repo:pr:5:opened",
        repo="owner/repo",
        kind="pr",
        number=5,
        trigger_kind="opened",
        trigger_id="5",
    )
    assert event is not None
    for before, after in (
        ("discovered", "claimed"),
        ("claimed", "context_ready"),
        ("context_ready", "message_submitting"),
        ("message_submitting", "dispatched"),
    ):
        ledger1.transition(
            event.event_key,
            expected=(before,),
            status=after,
            thread_id="s-crash",
            input_message_id="in-5",
        )

    log1 = MessageLog(db_path)
    log1.ensure_session("s-crash", SessionAttributes(visibility="internal", learning="excluded"))
    log1.writer("s-crash", author="github-watch", source="github-watch", body_types=(Input,), content={}).append("in-5", Input(()))
    log1.writer("s-crash", author="github-watch", source="github-watch", body_types=(Control,), content={}).append("fail-5", Control("failure", 0, "transient err"))

    # Breakpoint A: claim committed to events.sqlite3, crashed before resume written to sessions.db.
    assert ledger1.claim_failure_retry(event.event_key, control_seq=1, max_retries=1, cooldown_ready=True) == "retry"
    log1.close()

    def build_runtime(turn_fn):
        ledger = EventLedger(ledger_path)
        log = MessageLog(db_path)
        removed = []
        writers = SimpleNamespace(
            bind=lambda _ctx, *, author, source, body_types, content: lambda sid: log.writer(
                sid, author=author, source=source, body_types=body_types, content=content
            )
        )
        tasks = Tasks()

        class Ctx:
            def require(self, key):
                if key == SOURCE_SESSION:
                    return SourceSession
                if key == MESSAGE_CATALOG:
                    return log.catalog()
                if key == MESSAGE_WRITERS:
                    return writers
                if key == TASKS:
                    return SimpleNamespace(open=lambda _c: tasks)
                if key == RESTART_GATE:
                    return None
                if key == CONTENT:
                    return SimpleNamespace(check_text=lambda _part: None)
                raise KeyError(key)

            def borrow(self, _key):
                return nullcontext(None)

        rt = Runtime(Ctx(), SimpleNamespace(poll_seconds=15, failure_retry_limit=1, failure_retry_delay_seconds=0))
        rt._bound = (ledger, SimpleNamespace(cleanup=lambda key: removed.append(key) or True), None, None)
        rt._execute_turn = turn_fn
        return rt, ledger, log, removed

    # Breakpoint B: restarted runtime finishes writing Control("resume"), then crashes BEFORE turn runs.
    async def crash_before_start(_task, _reader, _source):
        raise AssertionError("not expected in breakpoint B")

    rt2, ledger2, log2, _ = build_runtime(crash_before_start)
    # Simulate open_source succeeding to commit resume, then start() crashing before executing turn.
    orig_open_source = sys.modules[Runtime.__module__].open_source

    class FailOnStart:
        def __init__(self, inner):
            self._inner = inner

        async def control(self, *args, **kwargs):
            return await self._inner.control(*args, **kwargs)

        async def start(self, _program):
            raise RuntimeError("simulated crash after resume before turn start")

    sys.modules[Runtime.__module__].open_source = lambda ctx, sid: FailOnStart(orig_open_source(ctx, sid))
    try:
        await rt2.run_dispatched()
    finally:
        sys.modules[Runtime.__module__].open_source = orig_open_source

    controls2 = [m.body for m in log2.catalog().reader("s-crash").snapshot() if isinstance(m.body, Control)]
    assert [c.action for c in controls2] == ["failure", "resume"]
    import sqlite3
    with sqlite3.connect(ledger_path) as conn:
        assert conn.execute("SELECT failure_retries FROM events WHERE event_key = ?", (event.event_key,)).fetchone()[0] == 1
    log2.close()

    # Breakpoint C: restarted runtime runs the resumed turn (which fails again), then writes abandon & crashes before cleanup.
    async def fail_again(_task, _reader, _source):
        raise RuntimeError("permanent err")

    rt3, ledger3, log3, removed3 = build_runtime(fail_again)
    await rt3.run_dispatched()
    assert removed3 == []
    assert ledger3.get_event(event.event_key).status == "dispatched"
    await rt3.run_dispatched()
    controls3 = [m.body for m in log3.catalog().reader("s-crash").snapshot() if isinstance(m.body, Control)]
    assert [c.action for c in controls3] == ["failure", "resume", "failure", "abandon"]
    log3.close()

    # Final restart: cleanup_completed runs on persisted disk state and settles event + checkout.
    rt4, ledger4, log4, removed4 = build_runtime(fail_again)
    await rt4.cleanup_completed(log4.catalog(), TurnProjection())
    assert ledger4.get_event(event.event_key).status == "failed"
    assert removed4 == [event.operation_id]
    log4.close()
