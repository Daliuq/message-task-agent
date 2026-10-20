from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol

from .store import Store


class Agent(Protocol):
    name: str
    version: str

    def analyze(self, event: dict[str, Any]) -> dict[str, Any]: ...


class JsonCommandAgent:
    """Run a configured local agent over JSON stdin/stdout (never through a shell)."""

    def __init__(self, command: list[str], timeout_seconds: int = 120):
        if not command:
            raise ValueError("Agent 命令不能为空")
        self.command = list(command)
        self.timeout_seconds = timeout_seconds
        self.name = self.command[0]
        self.version = "json-stdio-v1"

    def analyze(self, event: dict[str, Any]) -> dict[str, Any]:
        request = json.dumps(event, ensure_ascii=False)
        completed = subprocess.run(
            self.command,
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Agent 退出码 {completed.returncode}: {completed.stderr[-1500:]}"
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Agent stdout 不是有效 JSON: {exc}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("tasks", []), list):
            raise ValueError("Agent JSON 顶层必须是对象，tasks 必须为数组")
        return result


class NoopAgent:
    """Explicit test/development adapter; it records consumption without extracting tasks."""

    name = "noop"
    version = "0.1"

    def analyze(self, event: dict[str, Any]) -> dict[str, Any]:
        return {"tasks": [], "note": "noop agent; no extraction performed"}


class AgentWorker:
    def __init__(self, store: Store, agent: Agent, consumer_name: str = "extractor"):
        self.store = store
        self.agent = agent
        self.consumer_name = consumer_name

    def process_once(self) -> dict[str, Any] | None:
        event = self.store.claim_next_event(self.consumer_name)
        if event is None:
            return None
        try:
            result = self.agent.analyze(event)
            if not isinstance(result, dict):
                raise ValueError("Agent 输出必须是 JSON 对象")
            applied = self.store.complete_event(
                event, result, agent_name=self.agent.name, agent_version=self.agent.version
            )
            return {"event_id": event["event_id"], "status": "succeeded", **applied}
        except Exception as exc:
            self.store.fail_event(event, f"{type(exc).__name__}: {exc}",
                                  agent_name=self.agent.name, agent_version=self.agent.version)
            return {"event_id": event["event_id"], "status": "failed", "error": str(exc)}
