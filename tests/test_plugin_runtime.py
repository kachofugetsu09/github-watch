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
from plugins.programmatic.control import AdmitParams, PROGRAMMATIC, SendParams
from plugins.tools.plugin import TOOLS, ALL_TOOLS
from plugins.ledger.contract import MESSAGE_CATALOG, CallRef, ContentPart, Output, ToolCall
from tests.test_default_reply import application

from github_watch_test_package.ledger import EventLedger  # pyright: ignore[reportMissingImports]






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
    shutil.copytree(core / "plugins/programmatic", sources / "programmatic")
    shutil.copytree(core / "plugins/gateway", sources / "gateway")
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
            tmp_path, replying=False, start=False, extra_sources=add_sources,
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
                assert len(loaded_tools) == 5
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
