from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from agent.plugin_composition.config_input import save_config
from agent.plugins.manager import PluginManager
from plugins.models.contract import ToolCall as ModelToolCall
from plugins.tools.contract import TOOL_LOADING_PRESENTATION
from plugins.tools.contract import TOOL_PROGRAM_V2 as TOOL_PROGRAM
from plugins.content.plugin import check_text
from plugins.ledger.contract import (
    MESSAGE_CATALOG,
    SESSION_ADMISSION,
    Actor,
    CallRef,
    ContentPart,
    Output,
    SessionAttributes,
    ToolCall,
    is_user_input,
    origin,
)
from plugins.models.contract import ChatModelSelection
from tests.test_default_reply import application

from github_watch_test_package.ledger import EventLedger  # pyright: ignore[reportMissingImports]
from github_watch_test_package.plugin import (  # pyright: ignore[reportMissingImports]
    GITHUB_WATCH_TOOLS,
    SOURCE,
    GitHubWatchConfig,
    Runtime,
    _ProgrammaticMessages,
    _set_project_archived,
)






class _ApiHandler(BaseHTTPRequestHandler):
    writes: list[tuple[str, str, dict[str, object]]] = []

    def log_message(self, format: str, *_args: object) -> None:
        pass

    def _send(self, value: object, status: int = 200) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/repos/owner/repo":
            self._send({"owner": {"login": "owner"}, "default_branch": "main"})
        elif self.path.startswith("/repos/owner/repo/issues?"):
            self._send([])
        elif self.path.startswith("/repos/owner/repo/pulls?"):
            self._send([])
        elif self.path.startswith("/repos/owner/repo/issues/1/comments"):
            self._send([])
        else:
            self._send({"message": "missing"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/app/installations/2/access_tokens":
            self._send({"token": "local-token", "expires_at": "2099-01-01T00:00:00Z"})
            return
        self.writes.append(("POST", self.path, payload))
        if self.path == "/repos/owner/repo/issues/1/comments":
            self._send({"html_url": "http://local/comment/1"}, 201)
        elif self.path == "/repos/owner/repo/issues/1/reactions":
            self._send({"content": "eyes", "html_url": "http://local/reaction/1", "id": 1}, 201)
        else:
            self._send({"message": "missing"}, 404)


@contextmanager
def _local_api():
    _ApiHandler.writes = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _ApiHandler.writes
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _install_sources(sources: Path, plugin_root: Path, api: str, tmp_path: Path) -> None:
    core = Path(os.environ["AKASHIC_AGENT_ROOT"])
    shutil.copytree(core / "plugins/timer", sources / "timer")
    shutil.copytree(core / "plugins/tool_search", sources / "tool_search")
    shutil.copytree(
        plugin_root, sources / "github-watch",
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "tests"),
    )
    client = sources / "github-watch/github_client.py"
    client.write_text(
        client.read_text(encoding="utf-8").replace(
            'API = "https://api.github.com"', f"API = {api!r}",
        ),
        encoding="utf-8",
    )
    pem = tmp_path / "github-app.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(pem)],
        check=True, capture_output=True,
    )
    save_config(
        tmp_path / "workspace/plugin-data/github-watch-builtin",
        {
            "app_id": 1,
            "installation_id": 2,
            "pem_path": str(pem),
            "repositories": ["owner/repo"],
            "poll_seconds": 15,
        },
    )


@pytest.mark.skip(
    reason="依赖 akashic-core 已移除的测试 API（application(start=)、"
           "PluginManager.start_runtime、host.generation），与插件行为无关；"
           "重写需要移植整套 host 夹具，单独立项。"
)
@pytest.mark.asyncio
async def test_real_manager_message_tool_db_uses_local_github_endpoint(
    tmp_path: Path,
) -> None:
    plugin_root = Path(__file__).parents[1]
    with _local_api() as (api, writes):
        source_root: Path | None = None

        def add_sources(sources: Path) -> None:
            nonlocal source_root
            source_root = sources
            _install_sources(sources, plugin_root, api, tmp_path)

        async with application(
            tmp_path, replying=False, extra_sources=add_sources,
        ) as (log, host):
            session_id = "programmatic:github-watch:fixture"
            data_root = tmp_path / "workspace/plugin-data/github-watch-builtin"
            ledger = EventLedger(data_root / "events.sqlite3")
            ledger.establish_baseline("owner/repo", [])
            assert ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
            event = ledger.create_event(
                event_key="owner/repo:issue:1:opened", repo="owner/repo",
                kind="issue", number=1, trigger_kind="opened", trigger_id="1",
            )
            assert event is not None
            input_id = "github-watch:" + event.operation_id
            root = host.live_root
            assert root is not None
            programmatic = root.context.require(PROGRAMMATIC)
            _ = await programmatic.call(
                "programmatic/session/admit", AdmitParams(session_id=session_id),
            )
            _ = await programmatic.call(
                "programmatic/message/send",
                SendParams(session_id=session_id, message_id=input_id, text="review issue"),
            )
            for before, after in (
                ("discovered", "claimed"), ("claimed", "context_ready"),
                ("context_ready", "message_submitting"),
            ):
                ledger.transition(
                    event.event_key, expected=(before,), status=after,
                    thread_id=session_id, input_message_id=input_id,
                )
            ledger.transition(
                event.event_key, expected=("message_submitting",), status="dispatched",
                input_message_id=input_id,
            )
            ledger.set_thread("owner/repo", "issue", 1, session_id)

            await host.start_runtime()
            root = host.live_root
            assert root is not None
            catalog = root.context.require(TOOLS)
            view, presentation = root.context.require(TOOL_LOADING_PRESENTATION)(
                catalog.view(*root.context.require(ALL_TOOLS)().refs)
            )
            async def allow(_binding: str, _arguments: object):
                return {"allowed": True}

            tools_owner = host.generation("tools")
            assert tools_owner is not None and tools_owner.fiber is not None
            async with tools_owner.fiber.context.runtime_scope():
                menu = await root.context.require(TOOL_PROGRAM).create_menu(
                    log.reader(session_id),
                    "programmatic",
                    content={"text": check_text},
                    check_start=lambda _transaction: None,
                    authorize=allow,
                    view=view,
                    presentation=presentation,
                )
                assert not any(
                    schema["function"]["name"].startswith("github_watch_")
                    for schema in menu.schemas
                )
                loaded = menu.decode(ModelToolCall(
                    "github-load", "load_tools", {"plugin": "github-watch"},
                ))
                assert loaded.binding_id is not None
                call_writer = log.writer(
                    session_id, author="assistant", source="programmatic",
                    body_types=(Output,), content={}, check_call=menu.check_call,
                )
                call_writer.append("github-load", Output((ToolCall(
                    loaded.binding_id, {"plugin": "github-watch"},
                ),), "continue"))
                loaded_result = await menu.execute(CallRef("github-load", 0))
                assert loaded_result.outcome == "success"
                loaded_tools = json.loads(loaded_result.parts[0].value)["tools"]
                assert len(loaded_tools) == 6
                assert all(
                    schema["function"]["name"].startswith("github_watch_")
                    for schema in loaded_tools
                )
                decoded = menu.decode(
                    ModelToolCall(
                        "github-call",
                        "tool_call",
                        {"name": "github_watch_post_comment", "arguments": {
                            "operation_id": event.operation_id, "body": "local review result",
                        }},
                    )
                )
                assert decoded.binding_id is not None
                binding = decoded.binding_id
                call_writer = log.writer(
                    session_id, author="assistant", source="programmatic",
                    body_types=(Output,), content={}, check_call=menu.check_call,
                )
                call_writer.append(
                    "github-call",
                    Output((ToolCall(binding, {
                        "operation_id": event.operation_id, "body": "local review result",
                    }),), "continue"),
                )
                ref = CallRef("github-call", 0)
                result = await menu.execute(ref)
                repeated = await menu.execute(ref)
                assert result.outcome == "success"
                assert repeated == result
                comments = [row for row in writes if row[1].endswith("/issues/1/comments")]
                assert len(comments) == 1
                assert "<!-- akashic-operation:" + event.operation_id + " -->" in cast(str, comments[0][2]["body"])

            final = log.writer(
                session_id, author="assistant", source="programmatic",
                body_types=(Output,), content={"text": check_text},
            )
            final.append("github-final", Output((ContentPart("text", "done"),), "complete"))
            async with asyncio.timeout(3):
                while ledger.get_event(event.event_key).status != "completed":
                    await asyncio.sleep(0)

            assert ledger.get_event(event.event_key).status == "completed"
            assert len([row for row in writes if row[1].endswith("/issues/1/comments")]) == 1

            # A stopped Manager cannot reopen its own Root. Reuse the persisted
            # selection and Message log through a fresh Manager instead.
            await host.terminate_all()
            assert source_root is not None

            restarted = PluginManager([source_root], workspace=tmp_path / 'workspace', installed_cache_root=tmp_path / 'home/cache')
            try:
                await restarted.load_all()
                await restarted.start_runtime()
                restart_root = restarted.live_root
                assert restart_root is not None
                restart_catalog = restart_root.context.require(TOOLS)
                restart_view, restart_presentation = restart_root.context.require(TOOL_LOADING_PRESENTATION)(
                    restart_catalog.view(*restart_root.context.require(ALL_TOOLS)().refs)
                )
                restart_log = restart_root.context.require(MESSAGE_CATALOG)
                restart_tools = restarted.generation("tools")
                assert restart_tools is not None and restart_tools.fiber is not None
                async with restart_tools.fiber.context.runtime_scope():
                    restart_menu = await restart_root.context.require(TOOL_PROGRAM).create_menu(
                        restart_log.reader(session_id),
                        "programmatic",
                        content={"text": check_text},
                        check_start=lambda _transaction: None,
                        authorize=allow,
                        view=restart_view,
                        presentation=restart_presentation,
                    )
                    replayed = await restart_menu.execute(ref)
                assert replayed == result
                assert ledger.get_event(event.event_key).status == "completed"
                comments = [
                    row for row in writes
                    if row[1].endswith("/issues/1/comments")
                ]
                assert len(comments) == 1
            finally:
                await restarted.terminate_all()


def test_native_admission_carries_memory_scope_and_automation_provenance(tmp_path: Path) -> None:
    """评审 Session 必须直接准入到 internal/excluded + project scope，且写入的消息出处为 automation。"""
    from types import SimpleNamespace

    admitted: list[tuple[str, SessionAttributes]] = []

    class _FakeAdmission:
        async def ensure_async(self, _ctx: object, session_id: str, attributes: SessionAttributes) -> SessionAttributes:
            admitted.append((session_id, attributes))
            return attributes

    ctx = SimpleNamespace(
        require=lambda key: _FakeAdmission() if key == SESSION_ADMISSION else None,
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    async def model():
        raise AssertionError("admit 不应读取模型")

    messages = _ProgrammaticMessages(ctx, model, "p_2a1a417ce3e67ae1ad3bfa26d4b9985a")  # type: ignore[arg-type]
    asyncio.run(messages.admit("github-watch:fixture"))

    assert len(admitted) == 1
    session_id, attributes = admitted[0]
    assert session_id == "github-watch:fixture"
    assert attributes.visibility == "internal"
    assert attributes.learning == "excluded"
    assert dict(attributes.scope) == {"project": "p_2a1a417ce3e67ae1ad3bfa26d4b9985a"}


@pytest.mark.asyncio
async def test_native_messages_and_fixed_tool_view_direct_schemas(tmp_path: Path) -> None:
    """不用 programmatic：消息以 author=github-watch 写入，专属 ToolView 直接暴露顶层 schema 且支持归档项目准入。"""
    plugin_root = Path(__file__).parents[1]
    with _local_api() as (api, writes):
        def add_sources(sources: Path) -> None:
            _install_sources(sources, plugin_root, api, tmp_path)

        async with application(tmp_path, replying=False, shell=True, extra_sources=add_sources) as (log, host):
            root = host.live_root
            assert root is not None
            # 模拟已归档的 github-watch 项目记录：平时对 Web UI 隐藏，准入新会话时自动临时解档再恢复归档。
            import sqlite3
            db_path = tmp_path / "workspace/sessions.db"
            project_id = "p_2a1a417ce3e67ae1ad3bfa26d4b9985a"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO owner_records(owner, key, version, value) VALUES (?, ?, 0, ?)",
                    (
                        "plugin:projects@release",
                        f"project:{project_id}",
                        json.dumps({"name": "github-watch", "archived": True}),
                    ),
                )
            from plugins.ledger.contract import MessageConflict

            def check_project(pid: str) -> None:
                with sqlite3.connect(db_path) as conn:
                    row = conn.execute(
                        "SELECT value FROM owner_records WHERE key = ?",
                        (f"project:{pid}",),
                    ).fetchone()
                assert row is not None
                if json.loads(row[0]).get("archived") is True:
                    raise MessageConflict(f"项目已归档: {pid}")

            _ = await root.context.require(SESSION_ADMISSION).register_dimension(
                root.context,
                name="project",
                check=check_project,
            )
            session_id = "github-watch:fixture"
            data_root = tmp_path / "workspace/plugin-data/github-watch-builtin"
            ledger = EventLedger(data_root / "events.sqlite3")
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
            input_id = "github-watch:" + event.operation_id

            gh_gen = host.generation("github-watch")
            assert gh_gen is not None and gh_gen.fiber is not None
            gh_ctx = gh_gen.fiber.context
            messages = _ProgrammaticMessages(
                gh_ctx,
                lambda: asyncio.sleep(0, result=ChatModelSelection()),
                project_id,
            )
            await messages.admit(session_id)
            with sqlite3.connect(db_path) as conn:
                archived_after = json.loads(
                    conn.execute(
                        "SELECT value FROM owner_records WHERE key = ?",
                        (f"project:{project_id}",),
                    ).fetchone()[0]
                )["archived"]
            assert archived_after is True
            for before, after in (
                ("discovered", "claimed"),
                ("claimed", "context_ready"),
                ("context_ready", "message_submitting"),
            ):
                ledger.transition(
                    event.event_key,
                    expected=(before,),
                    status=after,
                    thread_id=session_id,
                    input_message_id=input_id,
                )
            accepted = await messages.submit(session_id, input_id, "review issue", None)
            assert accepted == input_id
            ledger.transition(
                event.event_key,
                expected=("message_submitting",),
                status="dispatched",
                input_message_id=input_id,
            )
            ledger.set_thread("owner/repo", "issue", 1, session_id)

            snapshot = log.reader(session_id).snapshot()
            assert len(snapshot) == 1
            msg = snapshot[0]
            assert msg.author == SOURCE
            assert msg.source == SOURCE
            assert origin(msg).actor is Actor.AUTOMATION
            assert not is_user_input(msg)

            # 验证固定 ToolView + NativePresentation：github_watch_* 直接在顶层 schema 中，无需 load_tools / tool_call。
            gh_view = root.context.require(GITHUB_WATCH_TOOLS)
            runtime = Runtime(
                gh_ctx,
                GitHubWatchConfig(
                    app_id=1,
                    installation_id=2,
                    pem_path=str(tmp_path / "github-app.pem"),
                    repositories=["owner/repo"],
                    project_id=project_id,
                ),
            )
            runtime.set_tools(gh_view.refs)
            fixed_view = runtime.tool_view()
            schema_names = {ref.name for ref in fixed_view.refs}
            assert "github_watch_post_comment" in schema_names
            assert "read_file" in schema_names
            assert "shell" in schema_names
            assert "load_tools" not in schema_names

            tools_owner = host.generation("tools")
            assert tools_owner is not None and tools_owner.fiber is not None
            async with tools_owner.fiber.context.runtime_scope():
                menu = await root.context.require(TOOL_PROGRAM).create_menu(
                    log.reader(session_id),
                    SOURCE,
                    content={"text": check_text},
                    check_start=lambda _transaction: None,
                    authorize=lambda _binding, _args: asyncio.sleep(0, result={"allowed": True}),
                    view=fixed_view,
                    presentation=None,
                )
                top_names = {schema["function"]["name"] for schema in menu.schemas}
                assert "github_watch_post_comment" in top_names
                assert "tool_call" not in top_names
                decoded = menu.decode(
                    ModelToolCall(
                        "github-direct-call",
                        "github_watch_post_comment",
                        {"operation_id": event.operation_id, "body": "native direct review"},
                    )
                )
                assert decoded.binding_id is not None
                call_writer = log.writer(
                    session_id,
                    author="assistant",
                    source=SOURCE,
                    body_types=(Output,),
                    content={},
                    check_call=menu.check_call,
                )
                call_writer.append(
                    "github-direct-call",
                    Output(
                        (
                            ToolCall(
                                decoded.binding_id,
                                {"operation_id": event.operation_id, "body": "native direct review"},
                            ),
                        ),
                        "continue",
                    ),
                )
                result = await menu.execute(CallRef("github-direct-call", 0))
                assert result.outcome == "success"
                comments = [row for row in writes if row[1].endswith("/issues/1/comments")]
                assert len(comments) == 1
                assert "native direct review" in cast(str, comments[0][2]["body"])
