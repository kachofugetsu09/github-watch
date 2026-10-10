"""用临时真实 Core MessageLog 和 SQLite 检查账本取消窗口与重开身份。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any

from agent.plugins.manager import PluginManager
from infra.channels.artifacts import ChannelAttachmentArtifactStore
from session.artifact_store import ArtifactStore
from session.message import Input
from tests.test_default_reply import application


def load_source(source: Path) -> Any:
    """只加载明确选中的实际源码，缺少 Core 依赖直接失败。"""
    package = ModuleType("github_ledger_io_source")
    package.__path__ = [str(source)]
    sys.modules[package.__name__] = package
    return importlib.import_module(package.__name__ + ".plugin")


def leaf_errors(error: BaseException) -> list[BaseException]:
    """保留取消和物理 SQLite 失败的独立证据。"""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaf_errors(child)]
    return [error]


def raw_messages(path: Path) -> list[Any]:
    """读取实际权威行，并先核对数据库完整性。"""
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return connection.execute("SELECT * FROM messages ORDER BY rowid").fetchall()


def digest(rows: list[Any]) -> str:
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


async def scenario(plugin: Any, kind: str, expect_blocking: bool,
                   expect_cancel_only: bool = False) -> dict[str, Any]:
    """在真正的 programmatic 消费者前后等待实际 SQL，再以新 Manager 重开。"""
    with TemporaryDirectory(prefix="github-ledger-io-") as directory:
        root = Path(directory)
        core = Path(os.environ["AKASHIC_AGENT_ROOT"])
        source_root: Path | None = None

        def add_sources(sources: Path) -> None:
            nonlocal source_root
            source_root = sources
            shutil.copytree(core / "plugins/programmatic", sources / "programmatic",
                            ignore=shutil.ignore_patterns("__pycache__"))

        # 1. 真实 Manager 提供 programmatic API；没有 reply loop、模型或 GitHub 请求。
        async with application(root, replying=False, extra_sources=add_sources) as (log, host):
            path = root / "events.sqlite3"
            ledger = plugin.EventLedger(path)
            ledger.establish_baseline("owner/repo", [])
            ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
            event = ledger.create_event(event_key="owner/repo:issue:1:opened",
                                        repo="owner/repo", kind="issue", number=1,
                                        trigger_kind="opened", trigger_id="1")
            assert event is not None
            if kind != "recovery_error_cancel":
                for before, after in (("discovered", "claimed"), ("claimed", "context_ready")):
                    ledger.transition(event.event_key, expected=(before,), status=after)
            watch = plugin.GitHubWatch(
                client=plugin.GitHubClient(app_id=1, installation_id=2, pem_path=root / "unused.pem"),
                ledger=ledger, checkouts=None, data_dir=root,
                mention="@bot", bot_login="bot",
            )
            coordinator = sys.modules[plugin.__package__ + ".github_watch"]
            session_id = coordinator._session_id(event)
            input_id = coordinator._input_message_id(event)
            ledger.set_thread("owner/repo", "issue", 1, session_id)
            live = host.live_root
            assert live is not None
            port = plugin._ProgrammaticMessages(live.context.require(plugin.PROGRAMMATIC))
            manifest, checkout = root / "manifest.json", root / "checkout"
            manifest.write_text("{}", encoding="utf-8")
            if kind == "recovery_error_cancel":
                # 此处只隔离回执恢复；远端 evidence/Git 不在本场景验收范围。
                (root / "item.json").write_text("{}", encoding="utf-8")
                watch._context.build = lambda _event: manifest
                watch._checkouts = SimpleNamespace(prepare=lambda _event, _item: SimpleNamespace(path=checkout))
            prompt = watch._build_prompt(event, manifest, checkout)
            receipts: dict[str, Any] = {"kind": kind, "connections": []}
            main_thread = threading.get_ident()
            caller: asyncio.Task[Any] | None = None
            termination: asyncio.Task[None] | None = None
            connect = sqlite3.connect
            entered, released, checkpoint = threading.Event(), threading.Event(), threading.Event()
            ready = threading.Event()
            loop = asyncio.get_running_loop()
            observer: asyncio.Task[None] | None = None

            class TrackedConnection(sqlite3.Connection):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.record = {"opened_thread": threading.get_ident(), "closed": False}
                    receipts["connections"].append(self.record)
                    self.status: str | None = None

                def execute(self, sql, parameters: Any = (), /):
                    if "UPDATE events SET" in sql and parameters:
                        self.status = parameters[0]
                        if self.status == "message_submitting" and kind in {"intent_lock", "write_error_cancel"}:
                            receipts["entered_at"] = time.monotonic()
                            entered.set()
                            if kind == "write_error_cancel":
                                if not released.wait(10):
                                    raise TimeoutError("actual ledger write failure barrier was not released")
                                super().execute("PRAGMA query_only=ON")
                    result = super().execute(sql, parameters)
                    if kind == "recovery_error_cancel" and "FROM events WHERE event_key" in sql:
                        receipts["entered_at"] = time.monotonic()
                        entered.set()
                        if not released.wait(10):
                            raise TimeoutError("actual recovery SELECT barrier was not released")
                    return result

                def __exit__(self, exc_type, exc_value, traceback):
                    result = super().__exit__(exc_type, exc_value, traceback)
                    # SQLite context 的原生提交不调用 Python commit override。
                    if exc_type is None and ((kind in {"intent_cancel", "terminate_intent"} and self.status == "message_submitting")
                            or (kind == "dispatched_cancel" and self.status == "dispatched")):
                        receipts["entered_at"] = time.monotonic()
                        entered.set()
                        if not released.wait(10):
                            raise TimeoutError("actual commit barrier was not released")
                    return result

                def close(self):
                    super().close()
                    self.record.update(closed=True, closed_thread=threading.get_ident(), closed_at=time.monotonic())

            def tracked_connect(database, *args, **kwargs):
                if str(database) == str(path):
                    kwargs["factory"] = TrackedConnection
                return connect(database, *args, **kwargs)

            async def observe() -> None:
                nonlocal termination
                try:
                    assert caller is not None
                    receipts["checkpoint_at"] = time.monotonic()
                    receipts["caller_pending"] = not caller.done()
                    if kind != "intent_lock":
                        if kind == "terminate_intent":
                            termination = asyncio.create_task(host.terminate_all())
                            async with asyncio.timeout(3):
                                while not caller.cancelling():
                                    await asyncio.sleep(0)
                            assert not termination.done()
                            receipts["manager_draining"] = True
                        else:
                            caller.cancel()
                        await asyncio.sleep(0)
                        caller.cancel()
                        await asyncio.sleep(0)
                        assert not caller.done()
                        receipts["cancel_draining"] = True
                    checkpoint.set()
                except BaseException as error:
                    receipts["observer_failure"] = repr(error)
                    checkpoint.set()

            def schedule_observer() -> None:
                nonlocal observer
                observer = asyncio.create_task(observe())

            def controller() -> None:
                try:
                    with closing(connect(path)) as writer:
                        if kind == "intent_lock":
                            writer.execute("BEGIN IMMEDIATE")
                        ready.set()
                        if not entered.wait(10):
                            raise TimeoutError("actual ledger operation did not enter")
                        if released.is_set():
                            return
                        loop.call_soon_threadsafe(schedule_observer)
                        receipts["checkpoint_before_release"] = checkpoint.wait(1)
                        receipts["released_at"] = time.monotonic()
                        writer.rollback()
                        released.set()
                except BaseException as error:
                    receipts["controller_failure"] = repr(error)
                    ready.set()
                    released.set()

            thread: threading.Thread | None = None

            class ResponseLost:
                async def admit(self, target: str) -> None:
                    await port.admit(target)

                async def submit(self, target: str, message_id: str, content: str) -> str:
                    accepted = await port.submit(target, message_id, content)
                    assert accepted == message_id
                    raise OSError("controlled response lost after actual Message append")

            # 2. 被测连接执行原 SQL/commit/close；只有等待屏障由独立线程控制。
            try:
                if kind != "response_lost":
                    sqlite3.connect = tracked_connect
                    thread = threading.Thread(target=controller, daemon=True)
                    thread.start()
                    assert await asyncio.to_thread(ready.wait, 10)
                messages = ResponseLost() if kind in {"response_lost", "recovery_error_cancel"} else port
                operation = (watch._process_event(event, messages) if kind == "recovery_error_cancel"
                             else watch._dispatch_message(event, manifest, checkout, messages))
                caller = await live.context.spawn(
                    operation,
                    name="check-github-ledger-" + kind,
                )
                try:
                    await asyncio.wait_for(asyncio.shield(caller), 15)
                except BaseException as error:
                    errors = leaf_errors(error)
                    if kind in {"intent_cancel", "dispatched_cancel", "terminate_intent"}:
                        assert len(errors) == 1 and isinstance(errors[0], asyncio.CancelledError)
                    elif kind == "write_error_cancel":
                        assert any(isinstance(x, asyncio.CancelledError) for x in errors)
                        assert any(isinstance(x, sqlite3.OperationalError) and "readonly" in str(x) for x in errors)
                    elif kind == "response_lost":
                        assert len(errors) == 1 and isinstance(errors[0], OSError)
                    elif kind == "recovery_error_cancel":
                        if expect_cancel_only:
                            assert len(errors) == 1 and isinstance(errors[0], asyncio.CancelledError)
                        else:
                            assert any(isinstance(x, OSError) for x in errors)
                            assert any(isinstance(x, asyncio.CancelledError) for x in errors)
                    else:
                        raise
                    receipts["errors"] = [type(x).__name__ for x in errors]
                    receipts["caller_classified_cancelled"] = caller.cancelled()
                if observer is not None:
                    await observer
                if termination is not None:
                    await termination
                    receipts["manager_terminated_at"] = time.monotonic()
                    assert all(record["closed_at"] <= receipts["manager_terminated_at"]
                               for record in receipts["connections"])
                if thread is not None:
                    thread.join(2)
                    assert not thread.is_alive()
                    assert "controller_failure" not in receipts and "observer_failure" not in receipts
                    assert receipts["checkpoint_before_release"] is not expect_blocking
                    receipts["checkpoint_lag_seconds"] = receipts["checkpoint_at"] - receipts["entered_at"]
                    if not expect_blocking:
                        assert receipts["caller_pending"]
                        assert all(record["closed"] and record["opened_thread"] == record["closed_thread"]
                                   and record["opened_thread"] != main_thread for record in receipts["connections"])
            finally:
                released.set()
                entered.set()
                sqlite3.connect = connect
                if thread is not None:
                    thread.join(2)
                if caller is not None and not caller.done():
                    await asyncio.gather(caller, return_exceptions=True)
                if termination is not None:
                    await termination

            state = ledger.get_event(event.event_key)
            first = tuple(message for message in log.reader(session_id).snapshot()
                          if isinstance(message.body, Input))
            expected_first = 0 if kind in {"intent_cancel", "terminate_intent", "write_error_cancel"} else 1
            assert len(first) == expected_first
            assert all(message.message_id == input_id for message in first)
            receipts.update(before_restart_status=state.status, before_restart_inputs=len(first))
            accepted_rows = raw_messages(root / "sessions.db")
            if kind in {"intent_cancel", "terminate_intent", "response_lost", "recovery_error_cancel"}:
                assert state.status == "message_submitting"
            elif kind == "write_error_cancel":
                assert state.status == "context_ready"
            else:
                assert state.status == "dispatched"

            # 3. 新 Manager 读取同一日志；重试只用原身份和正文，已 dispatched 不重发。
            await host.terminate_all()
            assert source_root is not None

            metadata = ArtifactStore(root / "sessions.db")
            artifacts = ChannelAttachmentArtifactStore(workspace=root / "workspace", metadata_store=metadata)
            reopened = PluginManager([source_root], workspace=root / "workspace",
                                     installed_cache_root=root / "home/cache", message_log=log,
                                     channel_attachment_store=artifacts)
            try:
                await reopened.load_all()
                await reopened.start_runtime()
                fresh = reopened.live_root
                assert fresh is not None
                next_port = plugin._ProgrammaticMessages(fresh.context.require(plugin.PROGRAMMATIC))
                restored = plugin.EventLedger(path)
                recovery = restored.recover_interrupted()
                current = restored.get_event(event.event_key)
                if current.status == "discovered":
                    assert recovery["safe_requeued"] == 1
                    for before, after in (("discovered", "claimed"), ("claimed", "context_ready")):
                        restored.transition(event.event_key, expected=(before,), status=after)
                    watch._ledger = restored
                    await watch._dispatch_message(event, manifest, checkout, next_port)
                else:
                    assert current.status == "dispatched" and recovery["safe_requeued"] == 0
                final = tuple(message for message in log.reader(session_id).snapshot()
                              if isinstance(message.body, Input))
                assert len(final) == 1 and final[0].message_id == input_id
                assert final[0].body.parts[0].value == prompt
                final_rows = raw_messages(root / "sessions.db")
                assert final_rows[:len(accepted_rows)] == accepted_rows
                with closing(connect(path)) as connection:
                    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                    assert connection.execute("SELECT count(*) FROM events").fetchone() == (1,)
                assert restored.get_event(event.event_key).status == "dispatched"
                receipts.update(after_restart_inputs=1, same_identity=True, prior_message_rows_preserved=True,
                                messages_sha256=digest(final_rows), ledger_events=1)
            finally:
                await reopened.terminate_all()
                metadata.close()

            return receipts


async def main() -> None:
    """每个场景只写临时目录，任何错误或身份漂移都返回非零。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expect-blocking", action="store_true")
    parser.add_argument("--recovery-only", action="store_true")
    parser.add_argument("--expect-cancel-only", action="store_true")
    args = parser.parse_args()
    plugin = load_source(args.source.resolve())
    kinds = ("recovery_error_cancel",) if args.recovery_only else ("intent_lock",) if args.expect_blocking else (
        "intent_lock", "intent_cancel", "dispatched_cancel", "terminate_intent", "write_error_cancel", "response_lost",
        "recovery_error_cancel",
    )
    receipts = [await scenario(plugin, kind, args.expect_blocking, args.expect_cancel_only) for kind in kinds]
    print(json.dumps({"scenarios": receipts, "scope": "temporary real Manager/MessageLog/SQLite; recovery evidence/checkout fixture only; no model/external GitHub/Git"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
