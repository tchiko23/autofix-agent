from __future__ import annotations

from pathlib import Path
from typing import Any

from app.service.llm_service import OllamaPlannerClient
from app.service.planner_service import build_plan


class PatchPlannerAgent:
    def __init__(self, llm: OllamaPlannerClient, prompts_dir: Path, rag_retriever=None, log=None):
        self.llm = llm
        self.prompts_dir = prompts_dir
        self.rag_retriever = rag_retriever
        self.log = log or (lambda *_a, **_kw: None)
        # v9.12: breakdown du dernier prompt pour [CONTEXT_BREAKDOWN] + prompt_breakdown.json
        self.last_breakdown: dict[str, Any] = {}

    def plan(
        self,
        analysis_text: str,
        selected_files: list[str],
        file_map: dict[str, str],
        contract: dict[str, Any],
        localization: dict[str, Any],
        *,
        require_file_overlap: bool = True,
    ) -> dict[str, Any]:
        # v9.15.2 : detection du mode "Rovo enrichi".
        # Bug observe TICKET-NNNNN : depuis v9.13, Rovo ne fournit plus de
        # rovo_code_suggestion (par design). L'ancienne condition exigeait
        # ce champ, donc le mode elague ne s'activait JAMAIS et le bundle
        # envoyait 12 Ko de JSON brut au LLM (43 min de generation).
        # Desormais : le mode elague s'active des qu'il y a du contenu Rovo
        # exploitable (diagnostic, candidats, ou source rovo).
        rovo_present = bool(
            contract
            and (
                str(contract.get("source", "")).startswith("rovo")
                or contract.get("rovo_code_suggestion")
                or contract.get("investigation_candidates")
                or contract.get("immediate_cause")
                or contract.get("root_cause_hypothesis")
            )
        )

        if rovo_present:
            enriched_text = self._build_pruned_prompt_for_rovo(
                analysis_text=analysis_text,
                contract=contract,
                localization=localization,
                file_map=file_map,
            )
        else:
            enriched_text = self._build_legacy_prompt(
                analysis_text=analysis_text,
                contract=contract,
                localization=localization,
            )

        breakdown = self.last_breakdown
        self.log(f"[CONTEXT_BREAKDOWN] rovo_present={rovo_present} total_chars={len(enriched_text)}")
        for source, size in breakdown.items():
            self.log(f"[CONTEXT_BREAKDOWN]   {source}: {size} chars")
        self.log(f"[PROMPT_LLM] size={len(enriched_text)} chars")
        if len(enriched_text) > 1200:
            self.log(f"[PROMPT_LLM] first 500: {enriched_text[:500]!r}")
            self.log(f"[PROMPT_LLM] last 500: {enriched_text[-500:]!r}")
        else:
            self.log(f"[PROMPT_LLM] full: {enriched_text!r}")

        # v9.15.2 : court-circuit rapide. Si Rovo n'a fourni NI methode cible
        # NI suggestion de code (cas TICKET-NNNNN : bug metier/data sans cible
        # precise), le LLM standard va mouliner 40+ minutes pour halluciner.
        # On abandonne tout de suite et on laisse la main a l'agent
        # d'exploration qui est concu pour ces cas-la.
        has_target_method = bool(
            (contract.get("must_touch_method") or contract.get("crash_method") or "").strip()
        )
        has_code_suggestion = bool((contract.get("rovo_code_suggestion") or "").strip())
        if rovo_present and not has_target_method and not has_code_suggestion:
            self.log(
                "[PATCH_PLANNER] no target method and no code suggestion from Rovo "
                "-> skipping standard LLM (fast), delegating to exploration agent"
            )
            return {
                "edits": [],
                "files_to_change": [],
                "unable_to_fix": True,
                "unable_to_fix_reason": "no_target_for_standard_llm_delegating_to_agent",
                "validation_score": 0,
            }

        plan_result = build_plan(
            self.llm,
            self.prompts_dir,
            enriched_text,
            selected_files,
            file_map,
            contract,
            require_file_overlap=require_file_overlap,
            localization_override=localization,
        )

        edits_count = len(plan_result.get("edits") or [])
        unable = bool(plan_result.get("unable_to_fix"))
        self.log(
            f"[LLM_RESPONSE] edits={edits_count} unable_to_fix={unable} "
            f"validation_score={plan_result.get('validation_score')} "
            f"confidence={plan_result.get('confidence_score')}"
        )
        return plan_result

    def _build_pruned_prompt_for_rovo(
        self, *, analysis_text: str, contract: dict[str, Any],
        localization: dict[str, Any], file_map: dict[str, str],
    ) -> str:
        """v9.12 : prompt ELAGUE quand Rovo a fourni la suggestion + contrat."""
        breakdown: dict[str, int] = {}

        rovo_suggestion = (contract.get("rovo_code_suggestion") or "").strip()
        rovo_location = (contract.get("rovo_code_location_hint") or "").strip()
        rovo_rationale = (contract.get("rovo_rationale_business") or "").strip()
        target_method = contract.get("must_touch_method") or contract.get("crash_method") or ""
        if "." in target_method:
            target_method = target_method.rsplit(".", 1)[-1]
        target_file = ""
        must_touch = contract.get("must_touch") or []
        if must_touch and isinstance(must_touch, list):
            target_file = must_touch[0]

        current_method_source = ""
        snapshot = localization.get("snapshot") or {} if localization else {}
        if snapshot.get("method_source"):
            current_method_source = snapshot["method_source"]
        else:
            for fname, fcontent in (file_map or {}).items():
                if target_file and (target_file in fname or fname.endswith(target_file)):
                    if target_method and target_method in fcontent:
                        current_method_source = self._extract_method_source_quick(fcontent, target_method)
                        break

        lines = [
            "# CORRECTIF A APPLIQUER",
            "",
            "Tu es un applicateur de patch Java. Un analyste senior a deja analyse",
            "ce ticket et propose un correctif. Ta SEULE mission est de produire",
            "un edit JSON `whole_method` qui applique ce correctif a la methode cible.",
            "",
            "## Methode cible",
            f"Fichier : `{target_file}`",
            f"Methode : `{target_method}`",
            "",
            "## Code source ACTUEL de la methode cible",
            "```java",
            current_method_source or "(non disponible)",
            "```",
            "",
            "## Suggestion de correctif (par Rovo)",
            "```java",
            rovo_suggestion,
            "```",
            "",
        ]
        breakdown["rovo_suggestion"] = len(rovo_suggestion)
        breakdown["current_method_source"] = len(current_method_source)

        if rovo_location:
            lines.extend(["## Emplacement", rovo_location, ""])
            breakdown["rovo_location"] = len(rovo_location)
        if rovo_rationale:
            lines.extend(["## Justification metier", rovo_rationale, ""])
            breakdown["rovo_rationale"] = len(rovo_rationale)

        lines.extend([
            "## Consignes",
            "- Adapte la suggestion au style du code actuel (noms de champs, imports)",
            "- Si la suggestion contient un placeholder type `<raw_message_field>`,",
            "  remplace par le vrai nom de champ visible dans le code source actuel",
            "- Conserve la signature exacte de la methode",
            "- Conserve toutes les annotations (@Override, @Transactional, etc.)",
            "- Conserve l'indentation du fichier (le moteur v9.9 s'en charge)",
            "- Ne supprime aucune logique non liee au bug",
            "",
            "Produis UN SEUL edit `whole_method` ciblant la methode citee ci-dessus.",
        ])

        full_text = "\n".join(lines)
        breakdown["consignes_et_template"] = len(full_text) - sum(breakdown.values())
        breakdown["TOTAL"] = len(full_text)
        self.last_breakdown = breakdown
        return full_text

    def _build_legacy_prompt(
        self, *, analysis_text: str, contract: dict[str, Any], localization: dict[str, Any],
    ) -> str:
        """Comportement v9.11 quand Rovo n'a pas fourni de JSON enrichi."""
        enriched_text = analysis_text
        breakdown: dict[str, int] = {"analysis_text": len(analysis_text)}

        if self.rag_retriever is not None and self.rag_retriever.is_available():
            try:
                from app.rag.retriever import format_episodes_for_prompt
                target_file = localization.get("target_file", "") if localization else ""
                target_method = localization.get("target_method", "") if localization else ""
                repo_filter = (contract.get("repo_hint") if contract else None) or None
                episodes = self.rag_retriever.find_similar(
                    contract=contract, target_file=target_file,
                    target_method=target_method, repo_filter=repo_filter, top_k=3,
                )
                if episodes:
                    rag_section = format_episodes_for_prompt(episodes, max_chars=6000)
                    breakdown["rag_section"] = len(rag_section)
                    enriched_text = (
                        rag_section + "\n\n# ============================================\n"
                        + "# ANALYSE DU TICKET COURANT (a corriger)\n"
                        + "# ============================================\n\n" + analysis_text
                    )
            except Exception:
                enriched_text = analysis_text

        rovo_suggestion = (contract.get("rovo_code_suggestion") or "").strip() if contract else ""
        if rovo_suggestion:
            rovo_header = (
                "# CORRECTIF PRE-ANALYSE (par Rovo)\n\n"
                f"Applique le correctif suivant a la methode cible :\n```java\n{rovo_suggestion}\n```\n\n"
            )
            breakdown["rovo_suggestion"] = len(rovo_suggestion)
            enriched_text = rovo_header + enriched_text

        breakdown["TOTAL"] = len(enriched_text)
        self.last_breakdown = breakdown
        return enriched_text

    @staticmethod
    def _extract_method_source_quick(file_content: str, method_name: str) -> str:
        import re
        try:
            sig_pattern = re.compile(
                rf"(?ms)^\s*(?:@[\w.]+(?:\([^)]*\))?\s*\n?\s*)*"
                rf"(?:public|protected|private|static|final|synchronized|\s)*"
                rf"[\w.<>\[\],\s]+\s+{re.escape(method_name)}\s*\([^)]*\)\s*"
                rf"(?:throws\s+[\w.,\s]+)?\s*\{{"
            )
            m = sig_pattern.search(file_content)
            if not m:
                return ""
            brace_start = file_content.find("{", m.start())
            depth = 0
            for i in range(brace_start, len(file_content)):
                ch = file_content[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        line_start = file_content.rfind("\n", 0, m.start()) + 1
                        return file_content[line_start:i + 1]
            return ""
        except Exception:
            return ""
