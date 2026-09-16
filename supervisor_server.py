"""Expose the Agno supervisor as a tiny HTTP service for the NaVILA runner.

The NaVILA/Habitat runtime uses Python 3.10, while Agno runs in Python 3.13.
This server keeps those environments decoupled.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from navila_agno.contracts import ExecutionEvent, EventType
from navila_agno.supervisor import AgnoSupervisor

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


class SupervisorHTTPService:
    def __init__(self) -> None:
        self.supervisor = AgnoSupervisor(
            model_provider=os.getenv("MODEL_PROVIDER", "openai_like"),
            model_id=os.getenv("MODEL_ID", "deepseek-v4-pro"),
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            db_file=os.getenv("DB_FILE", str(PROJECT_DIR / "data/supervisor_memory.db")),
        )
        self._plan_cache: dict[str, Any] = {}

    def plan(self, mission: str) -> dict[str, Any]:
        key = hashlib.sha256(mission.strip().encode("utf-8")).hexdigest()
        if key not in self._plan_cache:
            route = self.supervisor.plan(mission)
            self._plan_cache[key] = {
                "mission": mission,
                "subgoals": [asdict(sg) for sg in route.subgoals],
            }
        return self._plan_cache[key]

    def replan(self, mission: str, event: ExecutionEvent) -> dict[str, Any]:
        replacement = self.supervisor.replan(mission, event)
        return {"subgoals": [asdict(sg) for sg in replacement] if replacement else []}


SERVICE = SupervisorHTTPService()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "model": SERVICE.supervisor.model_id})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid json: {exc}"})
            return

        if self.path == "/plan":
            mission = payload.get("mission", "")
            if not mission:
                self._send_json(400, {"error": "mission is required"})
                return
            self._send_json(200, SERVICE.plan(mission))

        elif self.path == "/replan":
            mission = payload.get("mission", "")
            raw_event = payload.get("event", {})
            try:
                event = ExecutionEvent(
                    type=EventType(raw_event.get("type", "subgoal_failed")),
                    subgoal_id=raw_event.get("subgoal_id", ""),
                    message=raw_event.get("message", ""),
                    step_count=int(raw_event.get("step_count", 0)),
                )
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": f"invalid event: {exc}"})
                return
            self._send_json(200, SERVICE.replan(mission, event))

        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        print(f"[supervisor-server] {self.address_string()} - {format % args}")


def main() -> None:
    host = os.getenv("AGENT_SUPERVISOR_HOST", "127.0.0.1")
    port = int(os.getenv("AGENT_SUPERVISOR_PORT", "8012"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[supervisor-server] Agno supervisor listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
