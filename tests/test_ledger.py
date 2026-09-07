from __future__ import annotations

import sqlite3

from github_watch_test_package.ledger import EventLedger


def test_event_identity_and_recovery_are_idempotent(tmp_path):
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
    first = ledger.create_event(
        event_key="owner/repo:issue:1:opened",
        repo="owner/repo",
        kind="issue",
        number=1,
        trigger_kind="opened",
        trigger_id="1",
    )
    assert first is not None
    assert (
        ledger.create_event(
            event_key="owner/repo:issue:1:opened",
            repo="owner/repo",
            kind="issue",
            number=1,
            trigger_kind="opened",
            trigger_id="1",
        )
        is None
    )

    ledger.transition(first.event_key, expected=("discovered",), status="claimed")
    assert ledger.recover_interrupted() == {
        "safe_requeued": 1,
        "manual_reconcile": 0,
    }
    assert ledger.get_event(first.event_key).status == "discovered"


def test_started_external_effect_is_never_automatically_retried(tmp_path):
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "pr", 2, "t1", 0)
    event = ledger.create_event(
        event_key="owner/repo:pr:2:opened",
        repo="owner/repo",
        kind="pr",
        number=2,
        trigger_kind="opened",
        trigger_id="2",
    )
    assert event is not None
    ledger.transition(event.event_key, expected=("discovered",), status="claimed")
    ledger.transition(event.event_key, expected=("claimed",), status="context_ready")
    ledger.transition(
        event.event_key, expected=("context_ready",), status="turn_submitting"
    )

    assert ledger.recover_interrupted() == {
        "safe_requeued": 0,
        "manual_reconcile": 1,
    }
    assert ledger.pending_events() == []
    assert ledger.get_event(event.event_key).status == "manual_reconcile"


def test_event_can_be_resolved_by_operation_identity(tmp_path):
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
    event = ledger.create_event(
        event_key="owner/repo:issue:1:opened",
        repo="owner/repo",
        kind="issue",
        number=1,
        trigger_kind="opened",
        trigger_id="1",
    )
    assert event is not None
    ledger.transition(event.event_key, expected=("discovered",), status="claimed")
    ledger.transition(event.event_key, expected=("claimed",), status="context_ready")
    ledger.transition(
        event.event_key,
        expected=("context_ready",),
        status="turn_submitting",
        thread_id="programmatic:one",
    )
    ledger.transition(event.event_key, expected=("turn_submitting",), status="dispatched")

    assert (
        ledger.get_event_by_operation(event.operation_id).event_key == event.event_key
    )


def test_legacy_dispatched_event_keeps_evidence_without_replay(tmp_path):
    path = tmp_path / "events.sqlite3"
    ledger = EventLedger(path)
    for number, message_id in ((1, None), (2, "input:new")):
        assert ledger.insert_item("owner/repo", "issue", number, "t1", 0)
        event = ledger.create_event(
            event_key=f"owner/repo:issue:{number}:opened",
            repo="owner/repo",
            kind="issue",
            number=number,
            trigger_kind="opened",
            trigger_id=str(number),
        )
        ledger.transition(
            event.event_key,
            expected=("discovered",),
            status="dispatched",
            thread_id=f"thread:{number}",
            input_message_id=message_id,
        )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE events SET turn_id = 'turn:old' WHERE event_key = ?",
            ("owner/repo:issue:1:opened",),
        )
    assert ledger.recover_interrupted() == {
        "safe_requeued": 0,
        "manual_reconcile": 1,
    }
    old = ledger.get_event("owner/repo:issue:1:opened")
    assert old.status == "manual_reconcile"
    assert old.thread_id == "thread:1"
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT turn_id FROM events WHERE event_key = ?",
            (old.event_key,),
        ).fetchone() == ("turn:old",)
    assert ledger.get_event("owner/repo:issue:2:opened").status == "dispatched"
    assert ledger.recover_interrupted() == {
        "safe_requeued": 0,
        "manual_reconcile": 0,
    }
