"""TestSkeletonAgent — produit des squelettes JUnit 5 + Given/When/Then.

Appelé uniquement quand le pipeline a abouti à un patch applicable. Sa sortie
est injectée dans la description de MR pour donner au développeur :
  1. Une spécification Given/When/Then lisible
  2. Du code JUnit prêt à coller dans la classe de test correspondante

Le double chemin (JSON strict puis fallback texte) plus la normalisation finale
garantissent que la sortie a toujours la même structure, peu importe la qualité
de la réponse LLM.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.service.llm_service import OllamaPlannerClient


def _derive_package_from_path(target_file: str) -> str:
    """Extrait le package Java depuis un chemin source.

    Exemple: src/main/java/com/example/foo/Bar.java
             → com.example.subpkg.foo
    """
    if not target_file:
        return "NON_ETABLI"
    parts = target_file.replace("\\", "/").split("/")
    try:
        idx = parts.index("java")
        # Tout ce qui est après /java/ et avant le fichier .java final
        package_parts = parts[idx + 1:-1]
        if package_parts:
            return ".".join(package_parts)
    except ValueError:
        pass
    return "NON_ETABLI"


def _derive_class_name_from_path(target_file: str) -> str:
    if not target_file:
        return "Unknown"
    name = target_file.replace("\\", "/").split("/")[-1]
    if name.endswith(".java"):
        name = name[:-5]
    return name or "Unknown"


def _empty_skeleton(target_file: str, target_method: str, reason: str) -> dict[str, Any]:
    """Squelette par défaut quand le LLM ne produit rien d'exploitable."""
    class_name = _derive_class_name_from_path(target_file)
    return {
        "test_class_name": f"{class_name}Test",
        "test_class_package": _derive_package_from_path(target_file),
        "test_cases": [
            {
                "label": "À compléter manuellement",
                "given": "NON_ETABLI",
                "when": f"Appel à {target_method or 'la méthode corrigée'}",
                "then": "NON_ETABLI",
                "code": "// NON_ETABLI — squelette de test à compléter par le développeur"
            }
        ],
        "note": reason or "Squelette par défaut généré"
    }


def _normalize(payload: object, target_file: str, target_method: str) -> dict[str, Any]:
    """Force une structure stable quel que soit ce que le LLM a produit."""
    if not isinstance(payload, dict):
        return _empty_skeleton(target_file, target_method, "Réponse LLM non parsable en JSON")

    class_name = _derive_class_name_from_path(target_file)
    package = _derive_package_from_path(target_file)

    out = {
        "test_class_name": str(payload.get("test_class_name") or f"{class_name}Test"),
        "test_class_package": str(payload.get("test_class_package") or package),
        "test_cases": [],
        "note": str(payload.get("note") or ""),
    }

    # On ne fait PAS confiance au package retourné par le LLM s'il diffère
    # radicalement du package dérivé du chemin (anti-hallucination).
    if package != "NON_ETABLI" and out["test_class_package"] != package:
        out["note"] = (out["note"] + " | package corrigé depuis target_file").strip(" |")
        out["test_class_package"] = package

    raw_cases = payload.get("test_cases") or []
    if not isinstance(raw_cases, list) or not raw_cases:
        return _empty_skeleton(target_file, target_method, "Aucun cas de test produit par le LLM")

    for raw in raw_cases[:3]:  # Plafond à 3 tests, le LLM divague au-delà
        if not isinstance(raw, dict):
            continue
        case = {
            "label": str(raw.get("label") or "Cas de test"),
            "given": str(raw.get("given") or "NON_ETABLI"),
            "when": str(raw.get("when") or "NON_ETABLI"),
            "then": str(raw.get("then") or "NON_ETABLI"),
            "code": str(raw.get("code") or "// NON_ETABLI"),
        }
        out["test_cases"].append(case)

    if not out["test_cases"]:
        return _empty_skeleton(target_file, target_method, "Aucun cas de test exploitable")

    return out


class TestSkeletonAgent:
    def __init__(self, llm: OllamaPlannerClient, prompts_dir: Path):
        self.llm = llm
        self.prompts_dir = prompts_dir

    def generate(
        self,
        *,
        target_file: str,
        target_method: str,
        method_source: str,
        fix_summary: str,
        tests_suggested: list[str],
        logger=print,
    ) -> dict[str, Any]:
        """Produit un dict structuré { test_class_name, test_class_package,
        test_cases: [{label, given, when, then, code}], note }.

        Ne lève jamais — toujours retourne une structure normalisée.
        """
        try:
            template_path = self.prompts_dir / "test_skeleton_prompt.md"
            template = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger(f"[TEST_SKELETON] prompt unreadable: {exc}")
            return _empty_skeleton(target_file, target_method, f"prompt manquant: {exc}")

        class_name = _derive_class_name_from_path(target_file)
        prompt = (
            template
            .replace("{{target_file}}", target_file or "NON_ETABLI")
            .replace("{{target_method}}", target_method or "NON_ETABLI")
            .replace("{{target_method_class}}", class_name)
            .replace("{{method_source}}", method_source[:6000] if method_source else "NON_ETABLI")
            .replace("{{fix_summary}}", fix_summary or "NON_ETABLI")
            .replace("{{tests_suggested}}", "; ".join(tests_suggested) if tests_suggested else "NON_ETABLI")
        )

        # Chemin 1 — JSON strict (format=json côté Ollama)
        try:
            payload = self.llm.generate_json(prompt=prompt)
            normalized = _normalize(payload, target_file, target_method)
            if normalized["test_cases"] and normalized["test_cases"][0]["code"] != "// NON_ETABLI":
                logger(f"[TEST_SKELETON] {len(normalized['test_cases'])} test(s) generated via generate_json")
                return normalized
            logger("[TEST_SKELETON] generate_json produced empty cases, trying generate_text")
        except Exception as exc:
            logger(f"[TEST_SKELETON] generate_json failed: {exc}")

        # Chemin 2 — fallback texte
        try:
            raw = self.llm.generate_text(
                prompt=prompt + "\n\nRappel: réponds UNIQUEMENT en JSON valide.",
                system="Tu produis du JSON strict avec des squelettes de tests JUnit 5.",
            )
            # On tente d'extraire un bloc JSON même si entouré de texte
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    normalized = _normalize(parsed, target_file, target_method)
                    if normalized["test_cases"] and normalized["test_cases"][0]["code"] != "// NON_ETABLI":
                        logger(f"[TEST_SKELETON] {len(normalized['test_cases'])} test(s) generated via generate_text fallback")
                        return normalized
                except json.JSONDecodeError as exc:
                    logger(f"[TEST_SKELETON] JSON in text response is invalid: {exc}")
        except Exception as exc:
            logger(f"[TEST_SKELETON] generate_text failed: {exc}")

        # Chemin 3 — squelette vide normalisé (jamais de plantage en aval)
        return _empty_skeleton(target_file, target_method, "Aucune génération LLM exploitable")
