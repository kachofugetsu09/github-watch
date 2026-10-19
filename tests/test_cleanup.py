from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from plugins.ledger.contract import MESSAGE_CATALOG
from plugins.turn_projection.plugin import TurnProjection
from plugins.ledger.log import MessageLog
from plugins.ledger.contract import Input, Output

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
    runtime = Runtime(SimpleNamespace(), SimpleNamespace(poll_seconds=15))
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
