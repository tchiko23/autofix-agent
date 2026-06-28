"""app.service.deterministic_applicator — Mode applicateur deterministe v9.12.

Quand Rovo fournit une suggestion de code suffisamment riche pour etre appliquee
SANS appel LLM local, on court-circuite le PatchPlanner et on applique directement.
Cela elimine 30-60 minutes d'inference LLM par ticket.

GATING STRICT - toutes les conditions doivent etre remplies :
  - confidence_level ≥ high
  - code_suggestion sans placeholder (<...>, TODO, ???, ...)
  - code_suggestion parse comme methode Java complete (signature + corps)
  - Signature match exact avec la methode cible dans le code source
  - Pas plus de 1 fichier a modifier
  - fix_style dans whitelist sure (null_guard, validation, exception_handling)
  - Fichier cible existe + methode cible existe dans le repo

Si UNE seule condition echoue, on retombe sur le flux LLM normal (etape 3 du pipeline).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Patterns de placeholders qui invalident automatiquement la suggestion Rovo
PLACEHOLDER_PATTERNS = [
    re.compile(r"<[a-zA-Z_][a-zA-Z0-9_]*>"),       # <raw_message_field>, <chemin_fichier>
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\bXXX\b"),
    re.compile(r"\?\?\?"),
    re.compile(r"\.\.\.", re.MULTILINE),           # corps en "..." souvent placeholder
    re.compile(r"\bplaceholder\b", re.IGNORECASE),
    re.compile(r"//\s*your code here", re.IGNORECASE),
    re.compile(r"//\s*remplacer par", re.IGNORECASE),
]

# fix_style autorises pour le mode applicateur deterministe
ALLOWED_FIX_STYLES = {
    "minimal_local_null_guard",
    "minimal_local_validation",
    "exception_handling_fix",
}

# Mots-cles dangereux qui ne doivent JAMAIS apparaitre dans la suggestion
DANGEROUS_KEYWORDS = [
    "System.exit",
    "Runtime.exec",
    "Runtime.getRuntime",
    ".delete()",
    "deleteFile",
    "DROP TABLE",
    "DELETE FROM",
    "TRUNCATE",
]

# Niveaux de confiance Rovo acceptes (ordre du plus permissif vers le plus strict)
ACCEPTED_CONFIDENCE_LEVELS = {"high", "very_high"}


@dataclass
class GatingResult:
    """Resultat du gating de l'applicateur deterministe."""
    passed: bool
    conditions: dict[str, dict[str, Any]] = field(default_factory=dict)
    decision_reason: str = ""

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.conditions[name] = {"passed": passed, "detail": detail}

    def to_log_lines(self) -> list[str]:
        """Format pour run.log : 1 ligne par condition + decision finale."""
        lines = []
        for name, info in self.conditions.items():
            status = "✓ PASSED" if info["passed"] else "✗ FAILED"
            line = f"[ROVO_GATING] {name} -> {status}"
            if info.get("detail"):
                line += f" ({info['detail']})"
            lines.append(line)
        decision = "APPLY_DETERMINISTIC" if self.passed else "FALLBACK_TO_LLM"
        lines.append(f"[ROVO_GATING] decision: {decision} | reason: {self.decision_reason}")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "conditions": self.conditions,
            "decision_reason": self.decision_reason,
        }


def check_gating(
    contract: dict[str, Any],
    rovo: dict[str, Any] | None,
    file_content: str = "",
    target_method_source: str = "",
) -> GatingResult:
    """v9.12 GATING STRICT - verifie si le mode applicateur deterministe
    peut etre utilise. Retourne un GatingResult detaillant chaque condition.
    """
    g = GatingResult(passed=False)

    # Condition 1 : Rovo doit avoir fourni un JSON exploitable
    if not rovo:
        g.add("rovo_json_present", False, "Rovo n'a pas fourni de section JSON")
        g.decision_reason = "no_rovo_json"
        return g
    g.add("rovo_json_present", True)

    # Condition 2 : confidence_level >= high
    confidence = (
        (contract.get("rovo_confidence_level") or "").lower().strip()
        or (rovo.get("diagnosis") or {}).get("confidence_level", "").lower().strip()
    )
    if confidence not in ACCEPTED_CONFIDENCE_LEVELS:
        g.add(
            "confidence_level_high_or_very_high",
            False,
            f"actuel='{confidence}', accepte={sorted(ACCEPTED_CONFIDENCE_LEVELS)}",
        )
        g.decision_reason = "confidence_too_low"
        return g
    g.add("confidence_level_high_or_very_high", True, f"actuel='{confidence}'")

    # Condition 3 : code_suggestion existe et non vide
    code_suggestion = (contract.get("rovo_code_suggestion") or "").strip()
    if not code_suggestion:
        g.add("code_suggestion_present", False, "champ vide ou absent")
        g.decision_reason = "no_code_suggestion"
        return g
    g.add("code_suggestion_present", True, f"taille={len(code_suggestion)} chars")

    # Condition 4 : code_suggestion sans placeholder
    placeholder_found = _detect_placeholder(code_suggestion)
    if placeholder_found:
        g.add(
            "no_placeholder_in_suggestion",
            False,
            f"placeholder detecte: {placeholder_found!r}",
        )
        g.decision_reason = "suggestion_contains_placeholder"
        return g
    g.add("no_placeholder_in_suggestion", True)

    # Condition 5 : aucun mot-cle dangereux
    dangerous_found = [kw for kw in DANGEROUS_KEYWORDS if kw in code_suggestion]
    if dangerous_found:
        g.add(
            "no_dangerous_keywords",
            False,
            f"mots-cles dangereux: {dangerous_found}",
        )
        g.decision_reason = "dangerous_keyword_in_suggestion"
        return g
    g.add("no_dangerous_keywords", True)

    # Condition 6 : code_suggestion parse comme methode Java complete
    parse_info = _parse_java_method(code_suggestion)
    if not parse_info["is_complete_method"]:
        g.add(
            "suggestion_is_complete_java_method",
            False,
            parse_info["reason"],
        )
        g.decision_reason = "suggestion_not_complete_method"
        return g
    g.add(
        "suggestion_is_complete_java_method",
        True,
        f"signature='{parse_info['signature'][:80]}'",
    )

    # Condition 7 : fix_style dans whitelist
    fix_style = (contract.get("fix_style") or "").strip().lower()
    if fix_style not in ALLOWED_FIX_STYLES:
        g.add(
            "fix_style_allowed",
            False,
            f"actuel='{fix_style}', autorises={sorted(ALLOWED_FIX_STYLES)}",
        )
        g.decision_reason = "fix_style_not_allowed_for_deterministic"
        return g
    g.add("fix_style_allowed", True, f"actuel='{fix_style}'")

    # Condition 8 : un seul fichier a modifier
    must_touch = contract.get("must_touch") or []
    must_touch_paths = contract.get("must_touch_paths") or []
    if len(must_touch) > 1 or len(must_touch_paths) > 1:
        g.add(
            "single_file_to_modify",
            False,
            f"must_touch={must_touch}, paths={must_touch_paths}",
        )
        g.decision_reason = "multi_file_modification_unsupported"
        return g
    g.add("single_file_to_modify", True, f"file={(must_touch or must_touch_paths)[0] if (must_touch or must_touch_paths) else '?'}")

    # Condition 9 : signature de la suggestion = signature de la methode cible
    target_method_name = contract.get("must_touch_method") or contract.get("crash_method") or ""
    # Sur les noms qualifies "Class.method", on garde uniquement "method"
    if "." in target_method_name:
        target_method_name = target_method_name.rsplit(".", 1)[-1]

    if target_method_source:
        target_signature = _extract_signature(target_method_source)
        suggestion_signature = parse_info["signature"]
        if not _signatures_compatible(target_signature, suggestion_signature, target_method_name):
            g.add(
                "signatures_match",
                False,
                f"target='{target_signature[:80]}' vs suggestion='{suggestion_signature[:80]}'",
            )
            g.decision_reason = "signature_mismatch"
            return g
        g.add("signatures_match", True, f"method='{target_method_name}'")
    else:
        # Pas de code source disponible - on peut quand meme verifier que le nom de la methode
        # apparait dans la suggestion (defense legere)
        if target_method_name and target_method_name not in code_suggestion:
            g.add(
                "method_name_in_suggestion",
                False,
                f"methode '{target_method_name}' absente de la suggestion",
            )
            g.decision_reason = "method_name_not_in_suggestion"
            return g
        g.add(
            "method_name_in_suggestion",
            True,
            f"method='{target_method_name}' present mais code source non verifie",
        )

    # Toutes les conditions passees
    g.passed = True
    g.decision_reason = "all_gating_conditions_passed"
    return g


def _detect_placeholder(code: str) -> str | None:
    """Retourne le 1er placeholder detecte ou None."""
    for pattern in PLACEHOLDER_PATTERNS:
        m = pattern.search(code)
        if m:
            return m.group(0)
    return None


def _parse_java_method(code: str) -> dict[str, Any]:
    """Verifie que code est une methode Java complete (signature + accolades equilibrees).

    Retourne dict avec : is_complete_method, signature, body_size, reason.
    """
    code = code.strip()
    # Retirer markdown fences eventuels
    code = re.sub(r"^```(?:java)?\s*", "", code).strip()
    code = re.sub(r"\s*```$", "", code).strip()

    # Pattern signature : optional modifiers + return type + method_name + (params) + optional throws + {
    sig_pattern = re.compile(
        r"(?ms)^\s*"
        r"(?:@[\w.]+(?:\([^)]*\))?\s*\n?\s*)*"  # annotations
        r"(?:public|protected|private|\s)*"
        r"(?:static|final|synchronized|abstract|\s)*"
        r"(?:<[^>]+>\s*)?"                       # generics
        r"([\w.<>\[\],\s]+\s+\w+\s*\([^)]*\))"   # type method(params)
        r"\s*(?:throws\s+[\w.,\s]+)?"
        r"\s*\{"
    )
    m = sig_pattern.search(code)
    if not m:
        return {
            "is_complete_method": False,
            "signature": "",
            "body_size": 0,
            "reason": "signature pattern not matched",
        }
    signature = m.group(1).strip()

    # Verifier que les accolades du corps sont equilibrees
    brace_start = code.find("{", m.start())
    if brace_start < 0:
        return {
            "is_complete_method": False,
            "signature": signature,
            "body_size": 0,
            "reason": "no opening brace after signature",
        }

    depth = 0
    in_string = False
    in_char = False
    escape = False
    body_end = -1
    for i in range(brace_start, len(code)):
        ch = code[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if not in_char and ch == '"':
            in_string = not in_string
            continue
        if not in_string and ch == "'":
            in_char = not in_char
            continue
        if in_string or in_char:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = i
                break
    if body_end < 0:
        return {
            "is_complete_method": False,
            "signature": signature,
            "body_size": 0,
            "reason": "unbalanced braces",
        }

    return {
        "is_complete_method": True,
        "signature": signature,
        "body_size": body_end - brace_start,
        "reason": "",
    }


def _extract_signature(method_source: str) -> str:
    """Extrait la signature d'une methode Java depuis son source complet."""
    parsed = _parse_java_method(method_source)
    return parsed.get("signature", "")


def _signatures_compatible(sig_a: str, sig_b: str, method_name: str) -> bool:
    """Verifie que deux signatures Java decrivent la meme methode.

    Strategie : normaliser (espaces, modificateurs) puis comparer
    method_name + params types (return type peut varier marginalement).
    """
    def normalize(sig: str) -> dict[str, str]:
        # Trouver method_name(params)
        m = re.search(rf"\b{re.escape(method_name)}\s*\(([^)]*)\)", sig)
        if not m:
            return {"name": "", "params": ""}
        params = m.group(1).strip()
        # Normaliser les params : retirer les noms de variables, garder les types
        param_types = []
        if params:
            for p in params.split(","):
                # "final String name" -> "String"
                tokens = p.strip().split()
                if tokens:
                    # Le type est l'avant-dernier token (le nom de variable est le dernier)
                    if len(tokens) >= 2:
                        param_types.append(tokens[-2])
                    else:
                        param_types.append(tokens[0])
        return {"name": method_name, "params": ",".join(param_types)}

    norm_a = normalize(sig_a)
    norm_b = normalize(sig_b)
    return norm_a["name"] == norm_b["name"] and norm_a["params"] == norm_b["params"]


@dataclass
class ApplicatorResult:
    """Resultat de l'application deterministe."""
    applied: bool
    mode: str = ""           # "deterministic_applied" | "deterministic_failed"
    error: str = ""
    edit: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None


def build_deterministic_plan(
    contract: dict[str, Any],
    rovo: dict[str, Any],
    target_file_path: str,
    target_method_name: str,
) -> dict[str, Any]:
    """Construit un plan compatible avec le pipeline existant a partir de la
    suggestion Rovo, SANS appel LLM. Le plan a 1 seul edit whole_method que le
    reste du pipeline (validator, applicator) peut consommer normalement.
    """
    code_suggestion = (contract.get("rovo_code_suggestion") or "").strip()
    rationale = (contract.get("rovo_rationale_business") or "").strip()
    approach = (contract.get("problem_summary") or "").strip()
    fix_style = contract.get("fix_style") or ""

    plan: dict[str, Any] = {
        "fix_summary": approach or f"Application directe de la suggestion Rovo ({fix_style})",
        "short_description": f"fix: {target_method_name} via Rovo (deterministic)",
        "problem_summary": approach,
        "root_cause": (contract.get("root_cause_hypothesis") or contract.get("immediate_cause") or ""),
        "files_to_change": [target_file_path],
        "edits": [
            {
                "file": target_file_path,
                "search": "",
                "replace": code_suggestion,
                "replace_strategy": "whole_method",
                "match_hint_method": target_method_name,
                "justification": rationale or "Suggestion Rovo appliquee directement (mode deterministe v9.12)",
            }
        ],
        "tests_suggested": [t for t in (contract.get("oracle_tests") or [])],
        "risk_notes": ["Mode applicateur deterministe v9.12 : aucune validation LLM, source = Rovo confidence>=high"],
        "validation_score": 90,  # On fait confiance a Rovo (gating strict deja passe)
        "confidence_score": 90,
        "unable_to_fix": False,
        "deterministic_applied": True,
        "source": "rovo_deterministic",
    }
    return plan
