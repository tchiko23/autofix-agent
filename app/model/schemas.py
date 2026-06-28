from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class RunOptions:
    repo: Path
    analysis_file: Path
    base_branch: str | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    dry_run: bool = False
    apply_patch: bool = False
    commit: bool = False
    push: bool = False
    create_mr: bool = False


@dataclass(slots=True)
class PlanEdit:
    file: str
    search: str
    replace: str


@dataclass(slots=True)
class Plan:
    problem_summary: str = ""
    root_cause: str = ""
    fix_summary: str = ""
    files_to_change: list[str] = field(default_factory=list)
    edits: list[PlanEdit] = field(default_factory=list)
    tests_suggested: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    commit_message: str = "fix: apply automated corrective change"
    change_request_title: str = "Automated corrective change"
    short_description: str = ""
    already_fixed: bool = False
    unable_to_fix: bool = False
    unable_to_fix_reason: str = ""
