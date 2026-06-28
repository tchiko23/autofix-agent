from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from app.service.correction_contract_service import basenames_to_paths
from app.service.correction_contract_service import build_correction_contract
from app.service.correction_contract_service import contract_to_prompt_block
from app.service.correction_contract_service import dedupe_keep_order
from app.service.correction_contract_service import extract_hints
from app.service.llm_service import OllamaPlannerClient
from app.utils.repo_utils import RepoOps

ALLOWED_FIX_STYLES = {
    "minimal_local_null_guard",
    "safe_filter_or_skip_invalid_item",
    "api_contract_fix",
    "input_validation_guard",
    "minimal_local_change",
}


def load_prompt(base_dir: Path, name: str) -> str:
    return (base_dir / name).read_text(encoding="utf-8")


def summarize_execution_spec(base_dir: Path) -> str:
    return (base_dir / "execution_spec.md").read_text(encoding="utf-8")


def _extract_paths_from_grep_output(raw: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in (raw or "").splitlines():
        match = re.match(r"([^:]+):\d+:", line)
        if not match:
            continue
        path = match.group(1).replace("\\", "/")
        counts[path] = counts.get(path, 0) + 1
    return counts


def _support_path_score(path: str, contract: dict[str, Any], priority_paths: list[str], grep_path_counts: dict[str, int]) -> int:
    basename = PurePosixPath(path).name.lower()
    score = 0
    if basename in {name.lower() for name in contract.get("stacktrace_files", [])}:
        score += 400
    if basename in {name.lower() for name in contract.get("evidence_files", [])}:
        score += 180
    if basename in {name.lower() for name in contract.get("may_touch", [])}:
        score += 160
    parent = str(PurePosixPath(path).parent)
    priority_parents = {str(PurePosixPath(item).parent) for item in priority_paths}
    if parent in priority_parents:
        score += 140
    score += min(grep_path_counts.get(path, 0), 10) * 35
    return score


def _score_file(path: str, contract: dict[str, Any], grep_path_counts: dict[str, int]) -> int:
    lower = path.lower()
    basename = lower.split("/")[-1]
    score = 0
    if lower.endswith(".java"):
        score += 30
    if "/src/main/java/" in lower:
        score += 100
    if "/src/test/java/" in lower:
        score -= 120
    if basename in {name.lower() for name in contract.get("must_touch", [])}:
        score += 2500
    if basename in {name.lower() for name in contract.get("stacktrace_files", [])}:
        score += 900
    if basename in {name.lower() for name in contract.get("may_touch", [])}:
        score += 300
    if basename in {name.lower() for name in contract.get("must_not_touch", [])}:
        score -= 1200
    if basename == f"{str(contract.get('must_touch_method') or '').lower()}.java":
        score += 250
    for cls in contract.get("class_names", []):
        if basename == f"{cls.lower()}.java":
            score += 150
    score += min(grep_path_counts.get(path, 0), 10) * 25
    return score


def _narrow_selection(selected: list[str], contract: dict[str, Any], max_files: int) -> list[str]:
    must_not = {name.lower() for name in contract.get("must_not_touch", [])}
    must_touch = {name.lower() for name in contract.get("must_touch", [])}
    out: list[str] = []
    for path in selected:
        basename = PurePosixPath(path).name.lower()
        if basename in must_not and basename not in must_touch:
            continue
        out.append(path)
    return dedupe_keep_order(out)[:max_files]


def select_candidate_files(
    repo: RepoOps,
    analysis_text: str,
    max_files: int,
    max_chars: int,
    *,
    max_support_files: int = 2,
    contract: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, str], dict[str, Any], dict[str, Any]]:
    contract = contract or build_correction_contract(analysis_text)
    hints = extract_hints(analysis_text)
    tracked = repo.tracked_files()

    must_touch_paths = basenames_to_paths(tracked, contract.get("must_touch", []))
    may_touch_paths = basenames_to_paths(tracked, contract.get("may_touch", []))
    must_not = {name.lower() for name in contract.get("must_not_touch", [])}

    grep_hits: dict[str, str] = {}
    grep_path_counts: dict[str, int] = {}
    for term in contract.get("localization_search_terms", [])[:6] + contract.get("anchor_terms", [])[:6]:
        try:
            raw = repo.grep(term)
        except Exception:
            raw = ""
        if raw:
            grep_hits[term] = raw
            for path, count in _extract_paths_from_grep_output(raw).items():
                grep_path_counts[path] = grep_path_counts.get(path, 0) + count

    selected: list[str] = []
    if must_touch_paths:
        ranked_support = sorted(
            [path for path in may_touch_paths if PurePosixPath(path).name.lower() not in must_not],
            key=lambda item: _support_path_score(item, contract, must_touch_paths, grep_path_counts),
            reverse=True,
        )
        support_cap = 1 if contract.get("discipline") == "strict" else max_support_files
        selected = dedupe_keep_order(must_touch_paths + ranked_support[: max(1, support_cap)])
    else:
        rescored = [(path, _score_file(path, contract, grep_path_counts)) for path in tracked]
        rescored.sort(key=lambda x: x[1], reverse=True)
        selected = [path for path, score in rescored if score > 0][: max(max_files, 1)]

    if not selected:
        fallback = basenames_to_paths(tracked, hints.get("priority_java_files", []))
        selected = fallback[: max(max_files, 1)] if fallback else tracked[: max(max_files, 1)]

    selected = _narrow_selection(selected, contract, max_files=max_files)
    anchors = dedupe_keep_order(contract.get("localization_search_terms", []) + contract.get("anchor_terms", []))
    file_map = repo.read_files(selected, limit=max_chars, anchors=anchors)
    return selected, file_map, grep_hits, contract


def _selected_file_aliases(selected_files: list[str]) -> str:
    lines: list[str] = []
    for path in selected_files:
        basename = PurePosixPath(path).name
        lines.append(f"- {path} (basename: {basename})")
    return "\n".join(lines)


def _priority_paths(selected_files: list[str], contract: dict[str, Any]) -> list[str]:
    wanted = {name.lower() for name in contract.get("must_touch", [])}
    return [path for path in selected_files if PurePosixPath(path).name.lower() in wanted]


def _support_paths(selected_files: list[str], priority_paths: list[str], limit: int = 1) -> list[str]:
    support = [path for path in selected_files if path not in priority_paths]
    return support[:limit]


def _focus_text(content: str, anchors: list[str], limit: int = 8000) -> str:
    text = (content or "").replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    windows: list[tuple[int, int]] = []
    for anchor in anchors[:12]:
        if not anchor:
            continue
        try:
            matches = list(re.finditer(re.escape(anchor), text, flags=re.IGNORECASE))
        except re.error:
            matches = []
        for match in matches[:2]:
            start = max(0, match.start() - 1200)
            end = min(len(text), match.end() + 2200)
            windows.append((start, end))
        if len(windows) >= 8:
            break
    if not windows:
        return text[:limit]
    windows.sort()
    merged: list[list[int]] = []
    for start, end in windows:
        if not merged or start > merged[-1][1] + 250:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    parts: list[str] = []
    used = 0
    for start, end in merged:
        chunk = text[start:end].strip()
        if not chunk:
            continue
        wrapped = f"\n// FOCUSED SNIPPET\n{chunk}\n"
        if used + len(wrapped) > limit:
            remaining = limit - used
            if remaining < 300:
                break
            wrapped = wrapped[:remaining]
        parts.append(wrapped)
        used += len(wrapped)
        if used >= limit:
            break
    return "".join(parts)[:limit] or text[:limit]


def _deterministic_localization(contract: dict[str, Any], selected_files: list[str], file_map: dict[str, str]) -> dict[str, Any]:
    priority_paths = _priority_paths(selected_files, contract)
    target_file = priority_paths[0] if priority_paths else (selected_files[0] if selected_files else "")
    target_method = str(contract.get("must_touch_method") or contract.get("crash_method") or "").strip()
    support_paths = _support_paths(selected_files, priority_paths, limit=1 if contract.get("discipline") == "strict" else 2)
    reasoning = [
        str(contract.get("priority_reasons", {}).get(PurePosixPath(target_file).name, "")).strip(),
        str(contract.get("immediate_cause") or "").strip(),
    ]
    anchors = dedupe_keep_order(
        [target_method, str(contract.get("crash_method") or ""), str(contract.get("crash_file") or "")]
        + contract.get("localization_search_terms", [])
    )
    target_context = _focus_text(file_map.get(target_file, ""), anchors, limit=min(7000, max(2500, len(file_map.get(target_file, "")))))
    localized_map: dict[str, str] = {}
    if target_file:
        localized_map[target_file] = target_context
    for support_path in support_paths:
        localized_map[support_path] = _focus_text(file_map.get(support_path, ""), anchors, limit=3500)
    return {
        "target_file": target_file,
        "target_method": target_method,
        "support_paths": support_paths,
        "anchors": anchors,
        "reasoning": [item for item in reasoning if item],
        "file_map": localized_map,
    }


def _build_localization_block(localization: dict[str, Any]) -> str:
    snapshot = localization.get('snapshot', {}) if isinstance(localization.get('snapshot'), dict) else {}
    lines = [
        f"- target_file: {localization.get('target_file') or 'NON_ETABLI'}",
        f"- target_method: {localization.get('target_method') or 'NON_ETABLI'}",
        f"- expected_target_method: {localization.get('expected_target_method') or 'NON_ETABLI'}",
        f"- support_paths: {', '.join(localization.get('support_paths', [])) or '(aucun)'}",
        f"- anchors: {', '.join(localization.get('anchors', [])) or '(aucun)'}",
        f"- replace_strategy_preference: {localization.get('replace_strategy_preference') or 'whole_method'}",
        f"- method_fingerprint: {snapshot.get('fingerprint') or 'NON_ETABLI'}",
        f"- method_span: {snapshot.get('start_line') or 'NON_ETABLI'}-{snapshot.get('end_line') or 'NON_ETABLI'}",
    ]
    for item in localization.get("reasoning", []) or []:
        lines.append(f"- localization_reason: {item}")
    return "\n".join(lines)


def _build_prompt(
    prompts_dir: Path,
    analysis_text: str,
    selected_files: list[str],
    file_map: dict[str, str],
    contract: dict[str, Any],
    priority_paths: list[str],
    localization: dict[str, Any],
    *,
    retry_note: str = "",
) -> str:
    template = load_prompt(prompts_dir, "planning_prompt.md")
    file_context = [f"### FILE: {path}\n```\n{content}\n```" for path, content in file_map.items()]
    support_paths = [path for path in selected_files if path not in priority_paths]
    return (
        template.replace("{{ANALYSIS_TEXT}}", analysis_text)
        .replace("{{CORRECTION_CONTRACT}}", contract_to_prompt_block(contract))
        .replace("{{LOCALIZATION_BLOCK}}", _build_localization_block(localization))
        .replace("{{EXECUTION_SPEC_SUMMARY}}", summarize_execution_spec(prompts_dir))
        .replace("{{SELECTED_FILES}}", "\n".join(selected_files))
        .replace("{{SELECTED_FILE_ALIASES}}", _selected_file_aliases(selected_files))
        .replace("{{PRIORITY_FILES}}", "\n".join(priority_paths) or "(aucun fichier prioritaire explicite)")
        .replace("{{SUPPORT_FILES}}", "\n".join(support_paths) or "(aucun)")
        .replace("{{RETRY_NOTE}}", retry_note)
        .replace("{{FILE_CONTEXT}}", "\n\n".join(file_context))
    )


def _normalize_path(candidate: str, selected_files: list[str]) -> str:
    item = candidate.replace("\\", "/").strip()
    if item in selected_files:
        return item

    suffix_matches = [path for path in selected_files if path.endswith(item)]
    if len(suffix_matches) == 1:
        return suffix_matches[0]

    basename = PurePosixPath(item).name.lower()
    if basename:
        basename_matches = [path for path in selected_files if PurePosixPath(path).name.lower() == basename]
        if len(basename_matches) == 1:
            return basename_matches[0]

    if "." in item:
        simple = item.split(".")[-1]
        if not simple.endswith(".java"):
            simple = simple + ".java"
        basename_matches = [path for path in selected_files if PurePosixPath(path).name.lower() == simple.lower()]
        if len(basename_matches) == 1:
            return basename_matches[0]

    return item


def _targeted_basenames(plan: dict[str, Any]) -> list[str]:
    targeted: list[str] = []
    for item in plan.get("files_to_change", []):
        targeted.append(PurePosixPath(str(item)).name.lower())
    for edit in plan.get("edits", []):
        targeted.append(PurePosixPath(str(edit.get("file", ""))).name.lower())
    return [value for value in targeted if value]


def _basename_set(values: list[str]) -> set[str]:
    return {PurePosixPath(value).name.lower() for value in values if value}


def _tokenize_for_guard(value: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_./:-]+", value or "") if len(token) >= 4}


def _flatten_text_for_guard(plan: dict[str, Any]) -> str:
    chunks = [
        str(plan.get("problem_summary", "")),
        str(plan.get("root_cause", "")),
        str(plan.get("fix_summary", "")),
        str(plan.get("short_description", "")),
        str(plan.get("fix_style", "")),
    ]
    chunks.extend(str(item) for item in plan.get("files_to_change", []))
    chunks.extend(str(item) for item in plan.get("tests_suggested", []))
    chunks.extend(str(item) for item in plan.get("oracle_checks", []))
    chunks.extend(str(item) for item in plan.get("risk_notes", []))
    for edit in plan.get("edits", []):
        if isinstance(edit, dict):
            chunks.append(str(edit.get("file", "")))
            chunks.append(str(edit.get("search", "")))
            chunks.append(str(edit.get("replace", "")))
    for item in plan.get("edit_justifications", []):
        if isinstance(item, dict):
            chunks.append(str(item.get("file", "")))
            chunks.append(str(item.get("why_this_file", "")))
            chunks.append(str(item.get("symptom_resolved", "")))
            chunks.append(str(item.get("preserves_contract", "")))
            chunks.extend(str(x) for x in item.get("ticket_evidence", []) if isinstance(x, str))
    return "\n".join(chunks)


def _justify_edits(plan: dict[str, Any], selected_files: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    raw = plan.get("edit_justifications") or []
    if not isinstance(raw, list):
        return [], ["edit_justifications absent or invalid"]

    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            notes.append("invalid justification entry dropped")
            continue
        file_path = _normalize_path(str(item.get("file", "")), selected_files)
        why_this_file = str(item.get("why_this_file", "")).strip()
        symptom_resolved = str(item.get("symptom_resolved", "")).strip()
        preserves_contract = str(item.get("preserves_contract", "")).strip()
        confidence = item.get("confidence", 0)
        evidence = [str(x).strip() for x in item.get("ticket_evidence", []) if str(x).strip()]
        if file_path not in selected_files:
            notes.append(f"justification target unresolved: {item.get('file', '')}")
            continue
        if not why_this_file or not symptom_resolved or not evidence:
            notes.append(f"justification incomplete for {file_path}")
            continue
        try:
            confidence_int = int(confidence)
        except Exception:
            confidence_int = 0
        normalized.append(
            {
                "file": file_path,
                "why_this_file": why_this_file,
                "ticket_evidence": evidence,
                "symptom_resolved": symptom_resolved,
                "preserves_contract": preserves_contract,
                "confidence": max(0, min(100, confidence_int)),
            }
        )
    return normalized, notes


def _file_overlap_guard(plan: dict[str, Any], contract: dict[str, Any], require_file_overlap: bool) -> list[str]:
    if not require_file_overlap:
        return []
    allowed_basenames = _basename_set(contract.get("support_files", []))
    if not allowed_basenames:
        return []
    targeted = _targeted_basenames(plan)
    if targeted and any(item in allowed_basenames for item in targeted):
        return []
    note = (
        "Guardrail: aucun fichier ciblé par le plan ne recoupe les fichiers explicitement mentionnés dans l'analyse. "
        f"Fichiers attendus≈{sorted(allowed_basenames)} ; fichiers ciblés={sorted(set(targeted))}"
    )
    plan["unable_to_fix"] = True
    plan["unable_to_fix_reason"] = note
    plan["edits"] = []
    plan["files_to_change"] = []
    return [note]


def _must_touch_guard(plan: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    required = _basename_set(contract.get("must_touch", []))
    if not required:
        return []
    targeted = set(_targeted_basenames(plan))
    if targeted & required:
        return []
    note = (
        "Guardrail: le plan doit cibler au moins un fichier de `must_touch`. "
        f"must_touch={sorted(required)} ; ciblés={sorted(targeted)}"
    )
    plan["unable_to_fix"] = True
    plan["unable_to_fix_reason"] = note
    plan["edits"] = []
    plan["files_to_change"] = []
    return [note]


def _must_not_touch_guard(plan: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    forbidden = _basename_set(contract.get("must_not_touch", []))
    targeted = set(_targeted_basenames(plan))
    offenders = sorted(targeted & forbidden)
    if not offenders:
        return []
    note = f"Guardrail: le plan cible un fichier must_not_touch sans preuve suffisante: {offenders}"
    plan["unable_to_fix"] = True
    plan["unable_to_fix_reason"] = note
    plan["edits"] = []
    plan["files_to_change"] = []
    return [note]


def _immediate_cause_guard(plan: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    cause_tokens = _tokenize_for_guard(" ".join(contract.get("immediate_cause_terms", [])))
    if not cause_tokens:
        return []
    plan_tokens = _tokenize_for_guard(_flatten_text_for_guard(plan))
    overlap = sorted(plan_tokens & cause_tokens)
    if overlap:
        return []
    note = (
        "Guardrail: le plan ne traite pas explicitement la cause immédiate décrite dans l'analyse. "
        f"Tokens cause≈{sorted(cause_tokens)[:20]}"
    )
    plan["unable_to_fix"] = True
    plan["unable_to_fix_reason"] = note
    plan["edits"] = []
    plan["files_to_change"] = []
    return [note]


def _method_lock_guard(plan: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    """v9.13.1 OPTION BETA : guardrail assoupli.

    Comportement v9.13 (strict) : bloque le plan si `must_touch_method` n'apparait
    pas explicitement dans le plan (edits, fix_summary, etc.).

    Comportement v9.13.1 (assoupli) : accepte le pivot du LLM vers une autre
    methode A CONDITION que le fichier modifie soit dans le perimetre identifie
    par Rovo (must_touch_paths, may_touch_paths) ou le scan automatique
    (files_to_change qui appartient au repo).

    Justification : on observe que Rovo, sans acces au code source, propose
    parfois une methode cible erronee. Le LLM, en regardant le code reel,
    peut pivoter vers la vraie methode coupable. On accepte ce pivot tant
    qu'il reste dans le perimetre du ticket.

    Si pivot non accepte : on degrade le blocage en warning (risk_note) au
    lieu de unable_to_fix complet. Le plan est conserve, l'humain pourra
    juger en revue de MR.
    """
    target_method = str(contract.get("must_touch_method") or "").strip().lower()
    if not target_method:
        return []
    plan_text = _flatten_text_for_guard(plan).lower()
    if target_method in plan_text:
        return []

    # v9.13.1 : verifier si le pivot LLM reste dans le perimetre du ticket
    plan_files = set()
    for edit in plan.get("edits", []) or []:
        if isinstance(edit, dict):
            f = str(edit.get("file") or "").lower()
            if f:
                plan_files.add(f)
    for f in plan.get("files_to_change", []) or []:
        plan_files.add(str(f).lower())

    rovo_files = set()
    for f in (contract.get("must_touch_paths") or []):
        rovo_files.add(str(f).lower())
    for f in (contract.get("may_touch_paths") or []):
        rovo_files.add(str(f).lower())
    for f in (contract.get("must_touch") or []):
        rovo_files.add(str(f).lower())
    for f in (contract.get("may_touch") or []):
        rovo_files.add(str(f).lower())

    # Verifier le recoupement par basename ou par contains
    in_scope = False
    for pf in plan_files:
        pf_base = pf.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        for rf in rovo_files:
            rf_base = rf.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if pf_base == rf_base or pf.endswith(rf) or rf.endswith(pf):
                in_scope = True
                break
        if in_scope:
            break

    if in_scope:
        # Pivot accepte : on log en warning mais on n'echoue pas
        warning = (
            f"v9.13.1: pivot LLM accepte. methode attendue par Rovo={target_method}, "
            f"plan touche {sorted(plan_files)[:3]} qui est dans le perimetre Rovo."
        )
        plan["risk_notes"] = list(plan.get("risk_notes", [])) + [warning]
        plan["method_pivoted_from_rovo"] = True
        plan["method_pivoted_warning"] = warning
        return []

    # Pivot hors perimetre : on bloque (comportement v9.13)
    note = f"Guardrail: le plan ne recoupe pas explicitement la methode prioritaire `{target_method}` et le pivot LLM est hors perimetre Rovo."
    plan["unable_to_fix"] = True
    plan["unable_to_fix_reason"] = note
    plan["edits"] = []
    plan["files_to_change"] = []
    return [note]


def _safe_fix_style_guard(plan: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    expected = str(contract.get("fix_style", "")).strip()
    actual = str(plan.get("fix_style", "")).strip()
    if actual not in ALLOWED_FIX_STYLES:
        actual = "minimal_local_change"
        plan["fix_style"] = actual
    if not expected or expected == actual:
        return []
    note = f"Guardrail: fix_style inattendu. attendu={expected}, proposé={actual}"
    plan["risk_notes"] = list(plan.get("risk_notes", [])) + [note]
    return [note]


def _oracle_guard(plan: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    expected = contract.get("acceptance_criteria", []) or contract.get("oracle_tests", [])
    if not expected:
        return []
    plan_text = _flatten_text_for_guard(plan).lower()
    overlaps = 0
    for criterion in expected:
        keywords = [token.lower() for token in re.findall(r"[A-Za-zÀ-ÿ0-9_/.-]+", criterion) if len(token) >= 4]
        if keywords and any(keyword in plan_text for keyword in keywords[:6]):
            overlaps += 1
    if overlaps >= 1:
        return []
    note = "Guardrail: les tests/critères d'acceptation du plan ne recoupent pas l'oracle de validation extrait de l'analyse."
    plan["unable_to_fix"] = True
    plan["unable_to_fix_reason"] = note
    plan["edits"] = []
    plan["files_to_change"] = []
    return [note]


def _compute_validation_score(plan: dict[str, Any], contract: dict[str, Any], justification_notes: list[str]) -> int:
    score = 0
    targeted = set(_targeted_basenames(plan))
    must_touch = _basename_set(contract.get("must_touch", []))
    must_not_touch = _basename_set(contract.get("must_not_touch", []))
    if targeted & must_touch:
        score += 30
    if not (targeted & must_not_touch):
        score += 15
    if str(plan.get("fix_style", "")) == str(contract.get("fix_style", "")):
        score += 10
    cause_tokens = _tokenize_for_guard(" ".join(contract.get("immediate_cause_terms", [])))
    plan_tokens = _tokenize_for_guard(_flatten_text_for_guard(plan))
    if cause_tokens and (cause_tokens & plan_tokens):
        score += 15
    if plan.get("tests_suggested"):
        score += 10
    if plan.get("oracle_checks"):
        score += 5
    if plan.get("edits"):
        score += 10
    if str(contract.get("must_touch_method") or "").strip().lower() in _flatten_text_for_guard(plan).lower():
        score += 10
    edit_count = len(plan.get("edits", [])) if isinstance(plan.get("edits"), list) else 0
    discipline = contract.get("discipline")
    if discipline == "strict" and 1 <= edit_count <= 2:
        score += 10
    elif discipline == "local" and 1 <= edit_count <= 3:
        score += 10
    elif discipline == "extended" and 1 <= edit_count <= 4:
        score += 10
    justifications = plan.get("edit_justifications", [])
    if justifications and not justification_notes and len(justifications) >= edit_count:
        score += 10
    return max(0, min(100, score))


def normalize_and_validate_plan(
    plan: dict[str, Any],
    selected_files: list[str],
    file_map: dict[str, str],
    *,
    contract: dict[str, Any],
    require_file_overlap: bool,
) -> dict[str, Any]:
    files = plan.get("files_to_change") or []
    normalized_files: list[str] = []
    for item in files:
        if isinstance(item, str):
            normalized = _normalize_path(item, selected_files)
            if normalized in selected_files:
                normalized_files.append(normalized)
    plan["files_to_change"] = list(dict.fromkeys(normalized_files))

    raw_edits = plan.get("edits") or []
    valid_edits: list[dict[str, Any]] = []
    already_fixed_detected = False
    validation_notes: list[str] = []

    for edit in raw_edits:
        if not isinstance(edit, dict):
            continue
        file_path = _normalize_path(str(edit.get("file", "")), selected_files)
        search = str(edit.get("search", ""))
        replace = str(edit.get("replace", ""))
        if file_path not in selected_files:
            validation_notes.append(f"Unresolvable target file dropped: {edit.get('file', '')}")
            continue
        if not search.strip() and str(edit.get('replace_strategy', '')).strip().lower() != 'whole_method':
            validation_notes.append(f"Empty search snippet dropped for {file_path}")
            continue
        if search.replace("\r\n", "\n") == replace.replace("\r\n", "\n"):
            validation_notes.append(f"No-op edit dropped for {file_path}")
            continue

        content = file_map.get(file_path, "")
        norm_content = content.replace("\r\n", "\n")
        norm_search = search.replace("\r\n", "\n")
        norm_replace = replace.replace("\r\n", "\n")
        replace_strategy = str(edit.get("replace_strategy", "")).strip().lower()
        match_hint_method = str(edit.get("match_hint_method", "") or edit.get("target_method", "")).strip()
        if norm_search in norm_content:
            accepted = {"file": file_path, "search": search, "replace": replace}
            if replace_strategy:
                accepted["replace_strategy"] = replace_strategy
            if match_hint_method:
                accepted["match_hint_method"] = match_hint_method
            valid_edits.append(accepted)
            if file_path not in plan["files_to_change"]:
                plan["files_to_change"].append(file_path)
            continue
        if replace_strategy == "whole_method" and match_hint_method and match_hint_method.lower() in norm_content.lower():
            accepted = {"file": file_path, "search": search, "replace": replace, "replace_strategy": replace_strategy, "match_hint_method": match_hint_method}
            fingerprint = str(edit.get('original_method_fingerprint', '')).strip()
            if fingerprint:
                accepted['original_method_fingerprint'] = fingerprint
            valid_edits.append(accepted)
            if file_path not in plan["files_to_change"]:
                plan["files_to_change"].append(file_path)
            validation_notes.append(f"Whole-method edit accepted for {file_path}::{match_hint_method}")
            continue
        if norm_replace and norm_replace in norm_content:
            already_fixed_detected = True
            validation_notes.append(f"Replacement already present in {file_path}")
            continue
        validation_notes.append(f"Search snippet not found in {file_path}")

    plan["edits"] = valid_edits
    plan.setdefault("tests_suggested", [])
    plan.setdefault("risk_notes", [])
    plan.setdefault("commit_message", "fix: apply automated code correction")
    plan.setdefault("change_request_title", "Automated corrective change")
    plan.setdefault("short_description", "")
    plan.setdefault("fix_style", contract.get("fix_style", "minimal_local_change"))
    plan.setdefault("confidence_score", 0)
    plan.setdefault("oracle_checks", list(contract.get("acceptance_criteria", [])[:3] or contract.get("oracle_tests", [])[:3]))
    plan.setdefault("already_fixed", False)
    plan.setdefault("unable_to_fix", False)
    plan.setdefault("unable_to_fix_reason", "")

    justifications, justification_notes = _justify_edits(plan, selected_files)
    plan["edit_justifications"] = justifications
    validation_notes.extend(justification_notes)

    if validation_notes:
        plan["risk_notes"] = list(plan.get("risk_notes", [])) + validation_notes

    if not valid_edits and not plan.get("unable_to_fix"):
        if already_fixed_detected:
            plan["already_fixed"] = True
            plan["unable_to_fix"] = False
            if not plan.get("fix_summary"):
                plan["fix_summary"] = "Le correctif semble déjà présent."
            plan["short_description"] = plan.get("short_description") or "Aucune modification effective nécessaire."
        else:
            plan["unable_to_fix"] = True
            plan["unable_to_fix_reason"] = plan.get("unable_to_fix_reason") or "Aucune édition applicable n'a pu être validée localement."

    notes: list[str] = []
    notes.extend(_file_overlap_guard(plan, contract, require_file_overlap=require_file_overlap))
    notes.extend(_must_touch_guard(plan, contract))
    notes.extend(_must_not_touch_guard(plan, contract))
    notes.extend(_immediate_cause_guard(plan, contract))
    notes.extend(_method_lock_guard(plan, contract))
    notes.extend(_safe_fix_style_guard(plan, contract))
    notes.extend(_oracle_guard(plan, contract))
    if notes:
        plan["risk_notes"] = list(plan.get("risk_notes", [])) + notes

    validation_score = _compute_validation_score(plan, contract, justification_notes=justification_notes)
    plan["validation_score"] = validation_score

    try:
        model_confidence = int(plan.get("confidence_score", 0))
    except Exception:
        model_confidence = 0
    plan["confidence_score"] = max(0, min(100, model_confidence))
    return plan


def build_plan(
    llm: OllamaPlannerClient,
    prompts_dir: Path,
    analysis_text: str,
    selected_files: list[str],
    file_map: dict[str, str],
    contract: dict[str, Any],
    *,
    require_file_overlap: bool = True,
    localization_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    system = "Réponds uniquement en JSON strict valide."
    priority_paths = _priority_paths(selected_files, contract)
    localization = localization_override or _deterministic_localization(contract, selected_files, file_map)

    if localization.get("target_file") and localization.get("target_method"):
        planning_files = [localization.get("target_file")]
    else:
        planning_files = dedupe_keep_order([localization.get("target_file", "")] + localization.get("support_paths", []))
        planning_files = [path for path in planning_files if path in selected_files]
        if not planning_files:
            planning_files = selected_files
    planning_map = {path: localization.get("file_map", {}).get(path, file_map.get(path, "")) for path in planning_files if path in file_map or path in localization.get("file_map", {})}
    if not planning_map:
        planning_map = {path: file_map.get(path, "") for path in planning_files if path in file_map}

    prompt = _build_prompt(
        prompts_dir,
        analysis_text,
        planning_files,
        planning_map,
        contract,
        [path for path in priority_paths if path in planning_files] or planning_files[:1],
        localization,
    )
    plan = llm.generate_json(prompt=prompt, system=system)
    snapshot = localization.get("snapshot", {}) if isinstance(localization.get("snapshot"), dict) else {}
    for edit in plan.get("edits", []) if isinstance(plan.get("edits"), list) else []:
        if not isinstance(edit, dict):
            continue
        if str(edit.get("replace_strategy", "")).strip().lower() == "whole_method":
            if not str(edit.get("match_hint_method", "") or edit.get("target_method", "")).strip() and localization.get("target_method"):
                edit["match_hint_method"] = localization.get("target_method")
            if not str(edit.get("original_method_fingerprint", "")).strip() and snapshot.get("fingerprint"):
                edit["original_method_fingerprint"] = f"sha256:{snapshot.get('fingerprint')}"
    normalized = normalize_and_validate_plan(
        plan,
        planning_files,
        planning_map,
        contract=contract,
        require_file_overlap=require_file_overlap,
    )
    normalized["localization"] = {
        "target_file": localization.get("target_file"),
        "target_method": localization.get("target_method"),
        "support_paths": localization.get("support_paths", []),
        "anchors": localization.get("anchors", []),
    }

    if normalized.get("unable_to_fix") and localization.get("target_file"):
        retry_note = (
            "Tentative précédente invalide ou hors périmètre. "
            "Tu dois corriger d'abord la cause immédiate dans le fichier et la méthode verrouillés par la localisation. "
            "N'utilise aucun autre fichier. Si un patch fiable n'est pas possible dans ce seul fichier, retourne unable_to_fix=true."
        )
        retry_files = [localization["target_file"]]
        retry_map = {path: planning_map.get(path, file_map.get(path, "")) for path in retry_files if path in planning_map or path in file_map}
        retry_prompt = _build_prompt(
            prompts_dir,
            analysis_text,
            retry_files,
            retry_map,
            contract,
            retry_files,
            localization,
            retry_note=retry_note,
        )
        retry_plan = llm.generate_json(prompt=retry_prompt, system=system)
        retry_normalized = normalize_and_validate_plan(
            retry_plan,
            retry_files,
            retry_map,
            contract=contract,
            require_file_overlap=require_file_overlap,
        )
        retry_normalized["localization"] = normalized["localization"]
        return retry_normalized

    return normalized
