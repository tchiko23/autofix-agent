from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re


def _sanitize_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "unknown"


def build_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass(slots=True)
class RunContext:
    run_id: str
    ticket_key: str
    root_dir: Path
    artifact_dir: Path
    log_path: Path

    @classmethod
    def create(cls, root_dir: Path, ticket_key: str | None = None) -> "RunContext":
        run_id = build_run_id()
        ticket = _sanitize_segment(ticket_key or "NO_TICKET")
        artifact_dir = root_dir / ticket / run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path = artifact_dir / "run.log"
        return cls(run_id=run_id, ticket_key=ticket, root_dir=root_dir, artifact_dir=artifact_dir, log_path=log_path)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(message)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def write_text(self, relative_name: str | Path, content: str) -> Path:
        path = self.artifact_dir / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, relative_name: str | Path, payload: object) -> Path:
        path = self.artifact_dir / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def append_history(self, payload: object) -> Path:
        history_path = self.root_dir / "history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return history_path
