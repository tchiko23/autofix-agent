"""app.service.llm_libre_fallback_agent — Filet de securite LLM libre v9.13.2.

Phase 2 du plan d'action : quand la cascade habituelle (pattern -> applicateur
deterministe -> LLM standard) echoue, au lieu de tomber en MR de proposition
vide, on lance un dernier appel LLM en mode "libre" :

  - Sans contraintes de cibles (pas de must_touch_method strict)
  - Sans guardrails post-generation strict
  - Avec un prompt court contenant juste : le probleme + le code source
  - Avec instruction au LLM de produire un correctif minimal

Si le LLM produit un patch avec un validation_score >= seuil (40 par defaut),
on l'applique et on cree une MR en mode DRAFT avec :
  - Prefixe titre : [LLM-LIBRE]
  - Label GitLab : needs-review
  - Section avertissement en tete de description

Sinon, on tombe en MR de proposition vide (comportement v9.13.1).

Philosophie : mieux vaut un correctif imparfait a reviewer qu'une MR vide.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LlmLibreResult:
    """Resultat d'une tentative LLM libre."""
    success: bool
    plan: dict[str, Any] | None = None
    validation_score: int = 0
    rejection_reason: str = ""
    prompt_used: str = ""


def build_libre_prompt(
    *,
    analysis_text: str,
    contract: dict[str, Any],
    file_map: dict[str, str],
    max_chars: int = 6000,
) -> str:
    """Construit le prompt minimal pour le LLM libre.

    Strategie : on donne le maximum de contexte source possible (jusqu'a max_chars)
    sans forcer une methode cible. Le LLM choisit ou modifier.
    """
    lines = [
        "# DERNIER RECOURS : CORRECTIF JAVA LIBRE",
        "",
        "Les etages precedents du pipeline (patterns deterministes, LLM avec contraintes)",
        "ont echoue a produire un correctif applicable pour ce ticket.",
        "Tu es la derniere chance. Ton objectif est de produire UN PATCH JAVA APPLIQUABLE,",
        "meme imparfait. Une review humaine validera ensuite.",
        "",
        "## Tu as carte blanche",
        "- Aucune methode cible imposee : choisis librement OU modifier",
        "- Aucun fichier impose : pivote vers la vraie source du bug si necessaire",
        "- Privilegie un fix minimal, chirurgical, syntaxiquement valide",
        "- Si tu ne peux vraiment rien proposer, retourne un plan vide explicitement",
        "",
        "## Resume du probleme",
    ]

    # Resume minimal (200-500 chars)
    immediate = (contract.get("immediate_cause") or "").strip()
    root = (contract.get("root_cause_hypothesis") or "").strip()
    exception_class = (contract.get("exception_class") or "").strip()
    exception_msg = (contract.get("exception_message") or "").strip()
    crash_file = (contract.get("crash_file") or "").strip()
    crash_method = (contract.get("crash_method") or contract.get("must_touch_method") or "").strip()

    if exception_class:
        lines.append(f"Exception : `{exception_class}`")
    if exception_msg:
        lines.append(f"Message : {exception_msg[:200]}")
    if crash_file:
        loc = crash_file
        if crash_method:
            loc += f" → `{crash_method}`"
        lines.append(f"Localisation : `{loc}`")
    if immediate:
        lines.append(f"")
        lines.append(f"Cause immediate : {immediate[:400]}")
    if root:
        lines.append(f"")
        lines.append(f"Hypothese racine : {root[:400]}")
    lines.append("")

    # Code source de la methode cible si disponible (resolver v9.13)
    method_source = (contract.get("target_method_source") or "").strip()
    if method_source:
        lines.append("## Code source actuel de la methode cible")
        lines.append("```java")
        lines.append(method_source[:2000])
        lines.append("```")
        lines.append("")

    # Sinon, on injecte les fichiers du file_map (priorise must_touch)
    must_touch = contract.get("must_touch_paths") or contract.get("must_touch") or []
    if not method_source and file_map:
        lines.append("## Fichiers candidats du repo")
        chars_left = max_chars - len("\n".join(lines))
        included = 0
        # Priorite : must_touch files first
        ordered_files = []
        for f in must_touch:
            if isinstance(f, str):
                for fname in file_map:
                    if fname.endswith(f) or f in fname:
                        if fname not in ordered_files:
                            ordered_files.append(fname)
        for fname in file_map:
            if fname not in ordered_files:
                ordered_files.append(fname)

        for fname in ordered_files:
            content = file_map.get(fname, "")
            if not content:
                continue
            chunk = f"\n### `{fname}`\n```java\n{content[:1500]}\n```\n"
            if len(chunk) > chars_left:
                break
            lines.append(chunk)
            chars_left -= len(chunk)
            included += 1
            if included >= 3:
                break
        lines.append("")

    # Instruction finale
    lines.extend([
        "## Instruction",
        "Produis UN edit JSON `whole_method` qui applique un correctif minimal Java.",
        "Format de sortie obligatoire :",
        "```json",
        "{",
        '  "fix_summary": "<1-2 phrases decrivant le correctif>",',
        '  "root_cause": "<1 phrase sur la cause>",',
        '  "edits": [',
        '    {',
        '      "file": "<chemin/relatif/au/repo/X.java>",',
        '      "search": "",',
        '      "replace": "<methode Java complete>",',
        '      "replace_strategy": "whole_method",',
        '      "match_hint_method": "<nom_methode>",',
        '      "justification": "<pourquoi cette modification>"',
        '    }',
        '  ],',
        '  "files_to_change": ["<chemin/X.java>"],',
        '  "risk_notes": ["Patch en mode LLM libre, review humaine obligatoire"],',
        '  "validation_score": <0-100>,',
        '  "unable_to_fix": false',
        "}",
        "```",
        "",
        "Si tu ne peux vraiment rien proposer, retourne ce JSON :",
        '`{"unable_to_fix": true, "edits": [], "fix_summary": "<raison>"}`',
    ])

    return "\n".join(lines)


def try_llm_libre(
    *,
    llm,
    analysis_text: str,
    contract: dict[str, Any],
    file_map: dict[str, str],
    min_score: int = 40,
    logger=print,
) -> LlmLibreResult:
    """Lance l'appel LLM libre et evalue le resultat.

    Args:
        llm: instance de OllamaPlannerClient
        analysis_text: rapport d'analyse complet (pas utilise directement, fourni
                       au cas ou on voudrait l'inclure dans le prompt)
        contract: contrat de correction (enrichi par resolver/Rovo)
        file_map: dict {chemin: contenu_java} des fichiers candidats
        min_score: seuil minimum de validation_score pour accepter le patch (40 defaut)

    Returns:
        LlmLibreResult avec plan + validation_score si succes, ou rejection_reason
    """
    prompt = build_libre_prompt(
        analysis_text=analysis_text, contract=contract, file_map=file_map,
    )
    logger(f"[LLM_LIBRE] prompt size={len(prompt)} chars")
    logger(f"[LLM_LIBRE] calling LLM in free mode (no must_touch constraint)...")

    try:
        raw = llm.generate_text(prompt)
    except Exception as exc:
        return LlmLibreResult(
            success=False,
            rejection_reason=f"llm_call_failed: {exc}",
            prompt_used=prompt,
        )

    # Parser le JSON de sortie
    import json
    import re

    parsed = None
    # Tenter parsing direct
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Tenter d'extraire un bloc ```json...```
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
        # Tenter d'extraire un objet JSON brut
        if parsed is None:
            m2 = re.search(r"(\{(?:[^{}]|\{[^{}]*\})*\})", raw, re.DOTALL)
            if m2:
                try:
                    parsed = json.loads(m2.group(1))
                except (json.JSONDecodeError, ValueError):
                    pass

    if not isinstance(parsed, dict):
        return LlmLibreResult(
            success=False,
            rejection_reason="llm_output_not_parseable_as_json",
            prompt_used=prompt,
        )

    # Si unable_to_fix=true explicite, on respecte
    if parsed.get("unable_to_fix") is True:
        return LlmLibreResult(
            success=False,
            rejection_reason="llm_libre_explicitly_unable",
            plan=parsed,
            prompt_used=prompt,
        )

    edits = parsed.get("edits") or []
    if not edits:
        return LlmLibreResult(
            success=False,
            rejection_reason="llm_libre_produced_no_edits",
            plan=parsed,
            prompt_used=prompt,
        )

    # Recuperer ou calculer validation_score
    score = parsed.get("validation_score")
    if not isinstance(score, (int, float)):
        # Heuristique simple si LLM n'a pas fourni de score
        score = _heuristic_score(parsed, contract, file_map)
    score = int(score)

    if score < min_score:
        return LlmLibreResult(
            success=False,
            validation_score=score,
            rejection_reason=f"score_too_low:{score}<{min_score}",
            plan=parsed,
            prompt_used=prompt,
        )

    # Marquer le plan comme issu du LLM libre
    parsed["llm_libre_mode"] = True
    parsed["validation_score"] = score
    risk_notes = list(parsed.get("risk_notes", []))
    if "Patch en mode LLM libre" not in " ".join(str(r) for r in risk_notes):
        risk_notes.append("Patch genere en mode LLM libre v9.13.2 : review humaine OBLIGATOIRE")
    parsed["risk_notes"] = risk_notes

    return LlmLibreResult(
        success=True,
        plan=parsed,
        validation_score=score,
        prompt_used=prompt,
    )


def _heuristic_score(plan: dict[str, Any], contract: dict[str, Any], file_map: dict[str, str]) -> int:
    """Estimation grossiere du validation_score si le LLM n'en a pas fourni."""
    score = 0
    edits = plan.get("edits") or []
    if edits:
        score += 30
        # Edit avec replace non vide
        for e in edits:
            if isinstance(e, dict) and (e.get("replace") or "").strip():
                score += 15
                break
        # File ciblee existe dans file_map
        for e in edits:
            if isinstance(e, dict):
                f = e.get("file") or ""
                if any(fname.endswith(f) or f.endswith(fname) for fname in file_map):
                    score += 20
                    break
    if plan.get("fix_summary"):
        score += 15
    if plan.get("root_cause"):
        score += 10
    if plan.get("files_to_change"):
        score += 10
    return min(100, score)


def build_libre_mr_metadata(plan: dict[str, Any], ticket_key: str) -> dict[str, Any]:
    """Construit les metadonnees specifiques aux MR en mode LLM libre.

    Returns:
        dict avec :
          - title_prefix : "[LLM-LIBRE]"
          - is_draft : True
          - labels_add : ["needs-review"]
          - warning_section : Markdown a inserer en tete de description
    """
    return {
        "title_prefix": "[LLM-LIBRE]",
        "is_draft": True,
        "labels_add": ["needs-review"],
        "warning_section": (
            "## ⚠️ AVERTISSEMENT — Patch en mode LLM libre\n\n"
            "> Ce correctif a ete genere apres echec des etages habituels du pipeline\n"
            "> (patterns deterministes + LLM avec contraintes). Le LLM a eu carte\n"
            "> blanche sur la methode cible.\n>\n"
            "> **Une review humaine approfondie est OBLIGATOIRE avant merge.**\n>\n"
            "> Points a verifier en priorite :\n"
            "> - Le correctif touche bien la zone reellement defaillante\n"
            "> - La signature des methodes modifiees est preservee\n"
            "> - Les annotations Spring/Lombok sont preservees\n"
            "> - Aucune logique metier n'a ete altered de maniere imprevue\n"
        ),
    }
