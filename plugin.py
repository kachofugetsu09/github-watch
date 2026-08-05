"""Akashic API v2 entrypoint for the GitHub polling bot."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import cast

from agent.plugins import IntervalTrigger, Plugin, PluginJobContext, PluginJobSpec
from pydantic import BaseModel, Field, field_validator

from .github_client import GitHubClient
from .github_watch import GitHubWatch
from .ledger import EventLedger

logger = logging.getLogger("plugin.github-watch")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubWatchConfig(BaseModel):
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    pem_path: str = Field(min_length=1)
    repositories: list[str] = Field(min_length=1)
    mention: str = "@akashic-review-bot"
    bot_login: str = "akashic-review-bot[bot]"
    poll_seconds: int = Field(default=60, ge=15)
    turn_timeout_seconds: int = Field(default=900, ge=30)
    control_endpoint: str | None = None

    @field_validator("repositories")
    @classmethod
    def validate_repositories(cls, value: list[str]) -> list[str]:
        invalid = [repo for repo in value if _REPOSITORY.fullmatch(repo) is None]
        if invalid:
            raise ValueError(f"invalid owner/repository values: {invalid}")
        if len(set(value)) != len(value):
            raise ValueError("repositories contains duplicates")
        return value

    @field_validator("mention")
    @classmethod
    def validate_mention(cls, value: str) -> str:
        if not value.startswith("@") or any(character.isspace() for character in value):
            raise ValueError("mention must be one @handle")
        return value


class GitHubWatchPlugin(Plugin):
    api_version = 2
    name = "github-watch"
    version = "1.0.0"
    desc = "Poll GitHub and wake one stable Akashic thread per issue or PR"
    ConfigModel = GitHubWatchConfig

    def activate(self) -> None:
        """Initialize the durable ledger after the generation becomes active."""

        config = cast(GitHubWatchConfig, self.context.config)
        data_dir = self.context.data_dir
        workspace = self.context.workspace
        if data_dir is None or workspace is None:
            raise RuntimeError("github-watch requires plugin data_dir and workspace")
        pem_path = Path(config.pem_path).expanduser()
        if not pem_path.is_file():
            raise FileNotFoundError(f"GitHub App PEM does not exist: {pem_path}")
        data_dir.mkdir(parents=True, exist_ok=True)
        ledger = EventLedger(data_dir / "events.sqlite3")
        ledger.integrity_check()
        recovered = ledger.recover_interrupted()
        if any(recovered.values()):
            logger.warning("github-watch recovered interrupted states: %s", recovered)
        endpoint = config.control_endpoint or str(workspace / "akashic.sock")
        client = GitHubClient(
            app_id=config.app_id,
            installation_id=config.installation_id,
            pem_path=pem_path,
        )
        self._watch = GitHubWatch(
            client=client,
            ledger=ledger,
            data_dir=data_dir,
            control_endpoint=endpoint,
            mention=config.mention,
            bot_login=config.bot_login,
            turn_timeout_seconds=config.turn_timeout_seconds,
        )

    def jobs(self) -> list[PluginJobSpec]:
        config = cast(GitHubWatchConfig, self.context.config)
        return [
            PluginJobSpec(
                id="poll",
                triggers=[IntervalTrigger(seconds=config.poll_seconds)],
                handler=self.poll,
                coalesce=True,
            )
        ]

    async def poll(self, ctx: PluginJobContext) -> None:
        config = cast(GitHubWatchConfig, ctx.plugin_context.config)
        await self._watch.poll(config.repositories)
