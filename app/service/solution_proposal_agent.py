from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROPOSAL_SYSTEM = (
    "/no_think "
    "Tu es un expert senior Java / Spring / analyse d'incidents. "
    "Tu produis soit un JSON strict valide, soit du texte technique court sans markdown parasite, sans balises <think>."
)


def _load_prompt_template(prompts_dir: Path) -> str:
    prompt_path = prompts_dir / "proposal_fallback_prompt.md"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return (
        "Tu dois produire UNIQUEMENT une proposition technico-fonctionnelle exploitable pour une MR de revue humaine.\n"
        "Ne produis pas de patch git. Ne produis pas de diff. N'invente pas de fichier hors périmètre.\n"
        "Réponds en JSON strict avec les clés: problem_summary, what_happened, root_cause, confidence, primary_target_file, primary_target_method, "
        "secondary_targets, fix_strategy, proposed_solution, why_this_solution, why_not_broader_change, business_contract_to_preserve, technical_constraints, risks, tests_to_add, acceptance_criteria, observability, open_questions."
    )


@dataclass(slots=True)
class SolutionProposalAgent:
    llm: Any
    prompts_dir: Path

    def propose(
        self,
        *,
        analysis_text: str,
        contract: dict[str, Any],
        localization: dict[str, Any],
        plan: dict[str, Any],
        result: dict[str, Any],
        selected_files: list[str],
    ) -> dict[str, Any]:
        template = _load_prompt_template(self.prompts_dir)
        prompt = self._build_prompt(template, analysis_text, contract, localization, plan, result, selected_files)
        first_attempt: dict[str, Any] | None = None
        try:
            proposal = self.llm.generate_json(prompt=prompt, system=PROPOSAL_SYSTEM)
            if isinstance(proposal, dict):
                first_attempt = proposal
                # v9.7: si la premiere tentative a renvoye du JSON parseable mais
                # SANS alternative_solutions exploitables (tableau vide ou cles
                # inattendues), on tente un second appel cible uniquement sur les
                # 5 categories. C'est moins gourmand en tokens et le LLM se
                # concentre sur cette seule production.
                alts = proposal.get("alternative_solutions")
                if not isinstance(alts, list) or len(alts) == 0:
                    enriched = self._retry_alternatives_only(
                        analysis_text=analysis_text,
                        contract=contract,
                        plan=plan,
                    )
                    if enriched:
                        proposal["alternative_solutions"] = enriched
                return self._normalize(proposal, contract, localization, plan, result)
        except Exception:
            pass

        # v9.7: si le 1er appel JSON a totalement plante, on retente AU MINIMUM
        # le bloc alternative_solutions seul. Mieux vaut une proposition partielle
        # avec 5 alternatives bien structurees qu'une coquille vide.
        retry_alternatives = self._retry_alternatives_only(
            analysis_text=analysis_text,
            contract=contract,
            plan=plan,
        )

        raw = self.llm.generate_text(prompt=prompt, system=PROPOSAL_SYSTEM)
        return self._normalize(
            {
                "problem_summary": str(plan.get("problem_summary") or contract.get("problem_summary") or "Incident non livré automatiquement."),
                "what_happened": "NON_ETABLI",
                "root_cause": str(plan.get("unable_to_fix_reason") or contract.get("immediate_cause") or "NON_ETABLI"),
                "confidence": int(plan.get("confidence_score") or 60),
                "primary_target_file": str(localization.get("target_file") or (contract.get("must_touch") or [""])[0]),
                "primary_target_method": str(localization.get("target_method") or contract.get("must_touch_method") or contract.get("crash_method") or ""),
                "secondary_targets": [str(x) for x in contract.get("may_touch", [])[:3]],
                "fix_strategy": str(contract.get("fix_style") or "proposal_only"),
                "proposed_solution": "Voir what_happened pour la proposition détaillée produite par le LLM.",
                "why_this_solution": "Le pipeline automatique n'a pas pu livrer un patch fiable; proposition fournie pour revue humaine.",
                "why_not_broader_change": "Conserver un périmètre local et réversible tant que le correctif n'a pas été validé par un expert.",
                "business_contract_to_preserve": [],
                "technical_constraints": [],
                "risks": [],
                "tests_to_add": [str(x) for x in contract.get("oracle_tests", [])[:8]],
                "acceptance_criteria": [str(x) for x in contract.get("acceptance_criteria", [])[:8]],
                "observability": [],
                "open_questions": [],
                "alternative_solutions": retry_alternatives or [],
            },
            contract,
            localization,
            plan,
            result,
        )

    def _retry_alternatives_only(
        self,
        *,
        analysis_text: str,
        contract: dict[str, Any],
        plan: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        """v9.7: appel LLM cible UNIQUEMENT sur les 5 categories d'alternatives.

        Quand la 1re generation rate ou produit une liste vide, on retente avec
        un prompt minimal qui ne demande QUE le tableau alternative_solutions,
        en limitant le contexte au contrat (pas tout le texte d'analyse).
        Beaucoup plus court → plus stable cote LLM → meilleure chance de produire
        au moins ce contenu critique pour la MR de proposition.
        """
        immediate_cause = str(contract.get("immediate_cause") or "")[:400]
        problem_summary = str(contract.get("problem_summary") or "")[:300]
        crash_file = str(contract.get("crash_file") or "")
        crash_method = str(contract.get("crash_method") or "")
        fix_style = str(contract.get("fix_style") or "")
        # Court extrait du symptome pour ancrer le LLM
        analysis_excerpt = (analysis_text or "")[:1200]

        retry_prompt = f"""Tu produis UNIQUEMENT le tableau JSON `alternative_solutions` pour un incident logiciel Java/Spring.

CONTEXTE DU TICKET:
- problem_summary: {problem_summary}
- immediate_cause: {immediate_cause}
- crash_file: {crash_file}
- crash_method: {crash_method}
- fix_style: {fix_style}

EXTRAIT D'ANALYSE:
{analysis_excerpt}

Tu DOIS produire un tableau JSON contenant EXACTEMENT 5 entrees, une par categorie, dans cet ordre:
1. configuration_fix
2. code_fix
3. mapping_fix
4. data_fix
5. process_fix

Chaque entree a la structure:
{{
  "category": "<nom_categorie_exact>",
  "title": "<titre court 5-10 mots>",
  "description": "<2-4 phrases qui expliquent comment ca marche, ancrees sur le contrat ci-dessus>",
  "code_snippet": "<extrait code/SQL/properties si pertinent, vide sinon>",
  "advantages": ["<av1>", "<av2>"],
  "drawbacks": ["<inc1>", "<inc2>"],
  "effort": "faible|moyen|eleve",
  "scope": "applicatif|base de donnees|infrastructure|process",
  "applicability": "applicable|non applicable|a valider",
  "applicability_reason": "<phrase qui justifie le statut>"
}}

Si une categorie n'est pas pertinente pour ce ticket precis, mets `applicability: "non applicable"` avec une `applicability_reason` claire.
Pour les bugs d'infrastructure (JDBC, timezone, DST, configuration Spring, serialisation), la categorie recommandee est generalement `configuration_fix` ou `mapping_fix`, JAMAIS `code_fix` (un null guard ne corrige pas un bug d'infrastructure, il le masque).

Reponds UNIQUEMENT par un objet JSON de la forme {{"alternative_solutions": [<5 entrees>]}}.
Aucun preambule, aucun markdown autour.
"""
        try:
            response = self.llm.generate_json(
                prompt=retry_prompt,
                system="/no_think Tu produis du JSON strict valide. Aucun texte autour. Aucun markdown.",
            )
        except Exception:
            return None
        if not isinstance(response, dict):
            return None
        alts = response.get("alternative_solutions")
        if isinstance(alts, list) and alts:
            return alts
        return None

    def _build_prompt(
        self,
        template: str,
        analysis_text: str,
        contract: dict[str, Any],
        localization: dict[str, Any],
        plan: dict[str, Any],
        result: dict[str, Any],
        selected_files: list[str],
    ) -> str:
        payload = {
            "analysis": analysis_text,
            "contract": contract,
            "localization": {
                "target_file": localization.get("target_file"),
                "target_method": localization.get("target_method"),
                "must_touch_method": contract.get("must_touch_method"),
            },
            "plan": {
                "problem_summary": plan.get("problem_summary"),
                "root_cause": plan.get("root_cause"),
                "fix_summary": plan.get("fix_summary"),
                "unable_to_fix": plan.get("unable_to_fix"),
                "unable_to_fix_reason": plan.get("unable_to_fix_reason"),
                "risk_notes": plan.get("risk_notes", []),
            },
            "result": {
                "ticket_key": result.get("ticket_key"),
                "mr_blocked_reason": result.get("mr_blocked_reason"),
                "patch_result": result.get("patch_result"),
                "delivery_status": result.get("delivery_status"),
            },
            "selected_files": selected_files[:8],
        }
        return (
            f"{template}\n\n"
            "CONTEXTE STRUCTURE\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "Produis maintenant UNIQUEMENT le JSON ou, si impossible, une proposition technico-fonctionnelle concise et exploitable."
        )

    def _normalize(
        self,
        proposal: dict[str, Any],
        contract: dict[str, Any],
        localization: dict[str, Any],
        plan: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        def _ls(v: Any) -> list[str]:
            if isinstance(v, list):
                return [str(x) for x in v if str(x).strip()]
            if v:
                return [str(v)]
            return []

        # v9.5: ne PAS recopier le mr_blocked_reason dans what_happened/root_cause/risks
        # (bug observé sur TICKET-NNNNN : les sections "Ce qui s'est passé", "Cause racine"
        # et "Risques" recopiaient le message du guardrail au lieu de la vraie cause).
        # Si le LLM ne produit rien d'exploitable pour ces champs, on retombe sur
        # NON_ETABLI plutôt que sur le motif technique de blocage du bundle.
        what_happened_raw = str(proposal.get("what_happened") or "").strip()
        root_cause_raw = str(proposal.get("root_cause") or "").strip()
        # Heuristique: si le texte produit commence par "Guardrail:" ou contient
        # "mr_blocked_reason", c'est probablement le motif de blocage qui a fuité.
        def _looks_like_block_reason(text: str) -> bool:
            t = text.lower()
            return (
                t.startswith("guardrail:")
                or "mr_blocked_reason" in t
                or "block_reason" in t
                or t.startswith("unresolvable target")
                or t.startswith("justification target")
                or "fix_style inattendu" in t
                or t.startswith("autocommit_threshold")
            )

        # v9.7: helper pour filtrer une liste de strings en eliminant les
        # fuites de motifs techniques de blocage. Utilise pour `risks` qui
        # est alimente par plan.risk_notes (lui-meme contamine par les
        # guardrails) et pour les autres listes susceptibles de fuir.
        def _filter_block_reasons(items: list[str]) -> list[str]:
            return [s for s in items if not _looks_like_block_reason(s)]

        if not what_happened_raw or _looks_like_block_reason(what_happened_raw):
            what_happened_raw = "NON_ETABLI — le LLM n'a pas produit d'analyse exploitable de ce qui s'est passé. Voir la stack trace et le symptôme dans l'analyse d'origine."
        if not root_cause_raw or _looks_like_block_reason(root_cause_raw):
            root_cause_raw = str(contract.get("immediate_cause") or "NON_ETABLI").strip() or "NON_ETABLI"

        # v9.5: normalisation des alternative_solutions avec catégories forcées.
        alternatives = self._normalize_alternative_solutions(
            proposal.get("alternative_solutions"),
            contract=contract,
            plan=plan,
            result=result,
        )

        return {
            "problem_summary": str(proposal.get("problem_summary") or plan.get("problem_summary") or contract.get("problem_summary") or "Incident non livré automatiquement."),
            "what_happened": what_happened_raw,
            "root_cause": root_cause_raw,
            "ticket_type": str(proposal.get("ticket_type") or ("METIER" if contract.get("acceptance_criteria") else "TECHNIQUE")),
            "confidence": int(proposal.get("confidence") or plan.get("confidence_score") or 60),
            "primary_target_file": str(proposal.get("primary_target_file") or localization.get("target_file") or (contract.get("must_touch") or [""])[0]),
            "primary_target_method": str(proposal.get("primary_target_method") or localization.get("target_method") or contract.get("must_touch_method") or contract.get("crash_method") or ""),
            "secondary_targets": _ls(proposal.get("secondary_targets") or contract.get("may_touch", [])[:3]),
            "fix_strategy": str(proposal.get("fix_strategy") or contract.get("fix_style") or "proposal_only"),
            "proposed_solution": str(proposal.get("proposed_solution") or plan.get("fix_summary") or "Proposition à valider par un expert technique."),
            "why_this_solution": str(proposal.get("why_this_solution") or "La proposition reprend le périmètre le plus local et le plus sûr identifié par l'analyse."),
            "why_not_broader_change": str(proposal.get("why_not_broader_change") or "Éviter d'élargir le scope tant qu'un correctif local n'a pas été validé."),
            "business_contract_to_preserve": _ls(proposal.get("business_contract_to_preserve") or []),
            "technical_constraints": _filter_block_reasons(_ls(proposal.get("technical_constraints") or [])),
            # v9.7: filtre des fuites guardrails dans la liste risks.
            # Avant v9.7, les motifs "Guardrail:", "Unresolvable target file",
            # "fix_style inattendu" et "justification target" remontaient depuis
            # plan.risk_notes vers la section Risques de la MR de proposition,
            # ce qui donnait au revieweur du jargon technique du bundle au lieu
            # de risques metier reels. Ces motifs sont desormais filtres.
            "risks": _filter_block_reasons(_ls(proposal.get("risks") or plan.get("risk_notes") or [])),
            "tests_to_add": _ls(proposal.get("tests_to_add") or contract.get("oracle_tests") or []),
            "acceptance_criteria": _ls(proposal.get("acceptance_criteria") or contract.get("acceptance_criteria") or []),
            "observability": _ls(proposal.get("observability") or []),
            "open_questions": _ls(proposal.get("open_questions") or []),
            "alternative_solutions": alternatives,
        }

    # v9.5: catégories standard forcées par le prompt. Si le LLM oublie une catégorie,
    # on la complète avec une entrée "non applicable" pour signaler à l'humain qu'aucune
    # piste n'a été examinée dans cette dimension.
    _STANDARD_CATEGORIES = ("configuration_fix", "code_fix", "mapping_fix", "data_fix", "process_fix")

    def _normalize_alternative_solutions(
        self,
        raw: Any,
        *,
        contract: dict[str, Any],
        plan: dict[str, Any],
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Force la liste à contenir exactement les 5 catégories standard.

        Pour chaque catégorie attendue :
        - si le LLM a produit une entrée pour cette catégorie, on la normalise
        - si manquante, on injecte une entrée "non applicable / NON_ETABLI"
          pour que l'humain voie clairement que cette piste n'a pas été examinée.

        Plafond: une seule entrée par catégorie. Les doublons éventuels du LLM
        sont consolidés (la première entrée prime).
        """
        provided: dict[str, dict[str, Any]] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                cat = str(item.get("category") or "").strip().lower()
                if cat not in self._STANDARD_CATEGORIES:
                    continue
                if cat in provided:
                    continue  # premier match prime
                provided[cat] = item

        out: list[dict[str, Any]] = []
        for cat in self._STANDARD_CATEGORIES:
            if cat in provided:
                out.append(self._normalize_one_alternative(provided[cat], cat))
            else:
                out.append(self._missing_alternative(cat))
        return out

    @staticmethod
    def _normalize_one_alternative(item: dict[str, Any], category: str) -> dict[str, Any]:
        def _str(key: str, default: str = "") -> str:
            v = item.get(key)
            return str(v).strip() if v not in (None, "") else default

        def _strlist(key: str) -> list[str]:
            v = item.get(key)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()][:6]
            return []

        applicability = _str("applicability", "à valider").lower()
        if applicability not in {"applicable", "non applicable", "à valider"}:
            applicability = "à valider"

        effort = _str("effort", "moyen").lower()
        if effort not in {"faible", "moyen", "élevé"}:
            effort = "moyen"

        scope = _str("scope", "applicatif").lower()
        if scope not in {"applicatif", "base de données", "infrastructure", "process"}:
            scope = "applicatif"

        # Tronquer le code_snippet pour éviter qu'un LLM bavard noie la MR.
        code_snippet = _str("code_snippet", "")
        if code_snippet.count("\n") > 20:
            code_snippet = "\n".join(code_snippet.splitlines()[:20]) + "\n... (extrait tronqué)"

        return {
            "category": category,
            "title": _str("title", "Solution sans titre"),
            "description": _str("description", "NON_ETABLI"),
            "code_snippet": code_snippet,
            "advantages": _strlist("advantages"),
            "drawbacks": _strlist("drawbacks"),
            "effort": effort,
            "scope": scope,
            "applicability": applicability,
            "applicability_reason": _str("applicability_reason", ""),
        }

    @staticmethod
    def _missing_alternative(category: str) -> dict[str, Any]:
        return {
            "category": category,
            "title": "Catégorie non examinée par le LLM",
            "description": "NON_ETABLI — le LLM n'a pas produit d'entrée pour cette catégorie. Le réviseur peut souhaiter examiner cette piste manuellement.",
            "code_snippet": "",
            "advantages": [],
            "drawbacks": [],
            "effort": "à valider",
            "scope": "à valider",
            "applicability": "à valider",
            "applicability_reason": "Aucune analyse fournie par le LLM sur cette dimension.",
        }
