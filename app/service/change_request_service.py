from __future__ import annotations

from pathlib import Path
from typing import Any


def load_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _format_automated_tests(skeleton: dict[str, Any] | None) -> str:
    """Rend la section 'Tests automatisés' à partir du squelette produit
    par TestSkeletonAgent.
    """
    if not skeleton or not isinstance(skeleton, dict):
        return "_Aucun squelette de test généré pour cette MR._"

    cases = skeleton.get("test_cases") or []
    if not cases:
        return "_Aucun squelette de test généré pour cette MR._"

    lines: list[str] = []
    class_name = skeleton.get("test_class_name") or "TestClass"
    package = skeleton.get("test_class_package") or "NON_ETABLI"
    note = skeleton.get("note") or ""

    lines.append(f"Classe de test cible : `{class_name}`  ")
    lines.append(f"Package : `{package}`")
    if note:
        lines.append("")
        lines.append(f"_Note : {note}_")
    lines.append("")

    for i, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            continue
        label = case.get("label") or f"Cas {i}"
        lines.append(f"### Cas {i} — {label}")
        lines.append("")
        lines.append(f"**Given** {case.get('given') or 'NON_ETABLI'}  ")
        lines.append(f"**When** {case.get('when') or 'NON_ETABLI'}  ")
        lines.append(f"**Then** {case.get('then') or 'NON_ETABLI'}")
        lines.append("")
        lines.append("```java")
        code = case.get("code") or "// NON_ETABLI"
        lines.append(code)
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip()


def render_change_request(
    template_path: Path,
    *,
    title: str,
    short_description: str,
    problem_summary: str,
    root_cause: str,
    fix_summary: str,
    files_changed: list[str],
    tests_suggested: list[str],
    risk_notes: list[str],
    description_append: str,
    automated_tests: dict[str, Any] | None = None,
) -> str:
    template = load_template(template_path)
    replacements = {
        "{{title}}": title,
        "{{short_description}}": short_description or fix_summary,
        "{{problem_summary}}": problem_summary,
        "{{root_cause}}": root_cause,
        "{{fix_summary}}": fix_summary,
        "{{files_changed}}": "\n".join(f"- {path}" for path in files_changed) or "- None",
        "{{tests_suggested}}": "\n".join(f"- {item}" for item in tests_suggested) or "- Not specified",
        "{{automated_tests}}": _format_automated_tests(automated_tests),
        "{{risk_notes}}": "\n".join(f"- {item}" for item in risk_notes) or "- None",
        "{{description_append}}": description_append or "",
    }
    body = template
    for source, target in replacements.items():
        body = body.replace(source, target)
    return body
