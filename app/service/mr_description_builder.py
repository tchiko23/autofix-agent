"""app.service.mr_description_builder — Construction des descriptions de MR v9.11.

Le bundle produit deux types de MR :
  - MR_CREATED : MR de patch automatique (le bundle a applique un correctif Java)
  - DIAGNOSTIC_MR_CREATED : MR de proposition (le bundle propose, sans appliquer)

Pour chaque type, on construit une description Markdown structuree en 5 sections :
  1. Description du probleme
  2. Proposition de solution
  3. Test unitaire automatise
  4. Tableau des regles de gestion a respecter en TNR
  5. Meta-informations agentiques

Plus une section optionnelle :
  6. Incidents similaires (si Rovo en a remonte)

Toutes les donnees structurees viennent du JSON Rovo via le contrat enrichi.
Aucune extraction depuis le Markdown : "JSON source, Markdown derive".
"""
from __future__ import annotations

from typing import Any


def build_mr_description(
    *,
    contract: dict[str, Any],
    localization: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    proposal: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    ticket_key: str = "",
    is_patch_mr: bool = True,
    bundle_version: str = "v9.11",
    model_name: str = "",
    run_id: str = "",
) -> str:
    """Construit la description Markdown structuree d'une MR.

    Args:
        contract: contrat de correction enrichi par Rovo
        localization: target_file, target_method, snapshot
        plan: plan de patch genere par le LLM
        proposal: proposition issue du SolutionProposalAgent
        result: champs de result (delivery_status, validation_score, etc.)
        ticket_key: RUN-XXXXX
        is_patch_mr: True pour MR de patch, False pour MR de proposition
        bundle_version, model_name, run_id: meta-informations
    """
    contract = contract or {}
    plan = plan or {}
    proposal = proposal or {}
    result = result or {}
    localization = localization or {}

    sections: list[str] = []

    # Header de la MR
    if is_patch_mr:
        sections.append(f"# 🤖 Patch automatique — {ticket_key or '(ticket non identifie)'}\n")
    else:
        sections.append(f"# 📋 Proposition de correction — {ticket_key or '(ticket non identifie)'}\n")

    # v9.14 : section avertissement EN TETE si le plan vient de l'agent autonome d'exploration
    if plan.get("agent_explored"):
        score = plan.get("validation_score", 0)
        endpoint = plan.get("endpoint_concerned", "") or ""
        methods = plan.get("methods_traced") or []
        sections.append(
            "## ⚠️ AVERTISSEMENT — Correctif issu d'une exploration agent autonome\n\n"
            "> Ce correctif a ete genere apres echec des etages habituels du pipeline\n"
            "> (patterns deterministes + LLM avec contraintes + fallback method-only).\n"
            "> Un agent autonome a explore le repo en lecture seule pour identifier la zone defaillante.\n>\n"
            f"> **Score de validation : `{score}/100`. Review humaine OBLIGATOIRE avant merge.**\n>\n"
            + (f"> **Endpoint traversé** : `{endpoint}`\n>\n" if endpoint else "")
            + (f"> **Cascade de méthodes** : `{' -> '.join(methods[:5])}`\n>\n" if methods else "")
            + "> Points a verifier en priorite :\n"
            "> - La zone modifiée est bien la cause racine du bug (pas un symptôme adjacent)\n"
            "> - La signature des méthodes modifiées est préservée\n"
            "> - Les annotations Spring/Lombok sont préservées\n"
            "> - Le `exploration_trace.json` (artefact du run) trace le raisonnement de l'agent\n"
        )

    # v9.13.2 (legacy) : section avertissement EN TETE si le plan vient du filet LLM libre
    elif plan.get("llm_libre_mode"):
        score = plan.get("validation_score", 0)
        sections.append(
            "## ⚠️ AVERTISSEMENT — Patch en mode LLM libre\n\n"
            "> Ce correctif a ete genere apres echec des etages habituels du pipeline\n"
            "> (patterns deterministes + LLM avec contraintes + fallback method-only).\n"
            "> Le LLM a eu carte blanche sur la methode cible.\n>\n"
            f"> **Score de validation : `{score}/100`. Review humaine OBLIGATOIRE avant merge.**\n>\n"
            "> Points a verifier en priorite :\n"
            "> - Le correctif touche bien la zone reellement defaillante\n"
            "> - La signature des methodes modifiees est preservee\n"
            "> - Les annotations Spring/Lombok sont preservees\n"
            "> - Aucune logique metier n'a ete altered de maniere imprevue\n"
        )

    # Section 1 : Description du probleme
    sections.append(_build_problem_section(contract, ticket_key))

    # Section 2 : Proposition de solution
    sections.append(_build_solution_section(contract, plan, proposal, is_patch_mr))

    # Section 3 : Test unitaire automatise
    sections.append(_build_tests_section(contract, proposal))

    # Section 4 : Tableau des regles de gestion
    sections.append(_build_rules_of_business_section(contract))

    # Section 5 (optionnelle) : Incidents similaires
    similar_section = _build_similar_incidents_section(contract)
    if similar_section:
        sections.append(similar_section)

    # Section 6 : Risques et incertitudes (si Rovo en a fournis)
    risks_section = _build_risks_section(contract)
    if risks_section:
        sections.append(risks_section)

    # Section finale : Meta-informations agentiques
    sections.append(_build_meta_section(
        contract=contract,
        plan=plan,
        result=result,
        bundle_version=bundle_version,
        model_name=model_name,
        run_id=run_id,
        is_patch_mr=is_patch_mr,
    ))

    return "\n".join(sections)


def _build_problem_section(contract: dict[str, Any], ticket_key: str) -> str:
    """Section 1 : Description du probleme."""
    lines = ["## 🐛 Description du problème\n"]

    if ticket_key:
        lines.append(f"**Référence ticket** : `{ticket_key}`")

    crash_file = contract.get("crash_file") or ""
    crash_method = contract.get("crash_method") or contract.get("must_touch_method") or ""
    crash_line = contract.get("crash_line") or ""
    exception_class = contract.get("exception_class") or ""

    if exception_class or crash_file:
        lines.append("")
        lines.append("**Symptôme technique** :")
        if exception_class:
            lines.append(f"- Exception : `{exception_class}`")
        if crash_file:
            location = crash_file
            if crash_method:
                location += f" → `{crash_method}()`"
            if crash_line:
                location += f" (ligne {crash_line})"
            lines.append(f"- Localisation : `{location}`")

    immediate_cause = contract.get("immediate_cause") or ""
    if immediate_cause:
        lines.append("")
        lines.append(f"**Cause immédiate** : {immediate_cause}")

    root_cause = contract.get("root_cause_hypothesis") or ""
    if root_cause:
        lines.append("")
        lines.append(f"**Hypothèse racine** : {root_cause}")

    return "\n".join(lines) + "\n"


def _build_solution_section(
    contract: dict[str, Any],
    plan: dict[str, Any],
    proposal: dict[str, Any],
    is_patch_mr: bool,
) -> str:
    """Section 2 : Proposition de solution."""
    lines = ["## 🔧 Proposition de solution\n"]

    fix_style = contract.get("fix_style") or "non_classifie"
    lines.append(f"**Approche** : `{fix_style}`")

    must_touch_method = contract.get("must_touch_method") or ""
    must_touch = contract.get("must_touch") or []
    if must_touch_method and must_touch:
        primary_file = must_touch[0] if isinstance(must_touch, list) else must_touch
        lines.append(f"**Méthode modifiée** : `{primary_file}::{must_touch_method}`")

    # Resume
    problem_summary = contract.get("problem_summary") or plan.get("fix_summary") or ""
    if problem_summary:
        lines.append("")
        lines.append("**Résumé du correctif** :")
        lines.append(f"> {problem_summary}")

    # Code suggere par Rovo (uniquement en MR de proposition pour aider revieweur)
    if not is_patch_mr:
        rovo_code = contract.get("rovo_code_suggestion") or ""
        if rovo_code:
            lines.append("")
            lines.append("**Suggestion de code initiale** :")
            lines.append("```java")
            lines.append(rovo_code)
            lines.append("```")

    # Justification metier
    rovo_rationale = contract.get("rovo_rationale_business") or ""
    if rovo_rationale:
        lines.append("")
        lines.append(f"**Justification métier** : {rovo_rationale}")

    return "\n".join(lines) + "\n"


def _build_tests_section(
    contract: dict[str, Any],
    proposal: dict[str, Any],
) -> str:
    """Section 3 : Test unitaire automatise."""
    lines = ["## ✅ Tests à ajouter ou valider\n"]

    # Tests Rovo enrichis (avec classe + methode + scenario)
    rovo_tests = contract.get("rovo_tests_to_add_full") or []
    if rovo_tests:
        for i, t in enumerate(rovo_tests[:5], 1):
            if not isinstance(t, dict):
                continue
            tc = t.get("test_class") or ""
            tm = t.get("test_method_suggestion") or t.get("test_method") or ""
            sc = t.get("scenario") or ""
            lines.append(f"**Test {i}** :")
            if tc:
                lines.append(f"- Classe : `{tc}`")
            if tm:
                lines.append(f"- Méthode : `{tm}`")
            if sc:
                lines.append(f"- Scénario : {sc}")
            lines.append("")
    else:
        # Fallback sur oracle_tests
        oracle_tests = contract.get("oracle_tests") or []
        if oracle_tests:
            lines.append("**Tests à couvrir** :")
            for t in oracle_tests[:5]:
                lines.append(f"- {t}")
            lines.append("")
        else:
            lines.append("_Aucun test spécifique identifié. Ajouter au minimum un test de non-régression sur la méthode modifiée._")
            lines.append("")

    return "\n".join(lines)


def _build_rules_of_business_section(contract: dict[str, Any]) -> str:
    """Section 4 : Tableau des regles de gestion a respecter en TNR.

    Format tableau Markdown. Chaque RG est fournie en JSON par Rovo
    avec impact_status. Fallback "À valider en TNR" si impact_status absent.
    """
    lines = ["## 📋 Règles de gestion à valider en TNR\n"]

    rules = contract.get("rovo_rules_of_business") or []
    if not rules:
        lines.append("_Aucune règle de gestion identifiée par l'analyse._")
        lines.append("")
        lines.append("_⚠️ Recommandation : valider manuellement le périmètre fonctionnel impacté avant merge._")
        lines.append("")
        return "\n".join(lines)

    lines.append("| Référence | Statut | Résumé | Confluence |")
    lines.append("|-----------|--------|--------|------------|")

    for rg in rules:
        if not isinstance(rg, dict):
            continue
        ref = rg.get("reference", "?")
        status = _normalize_impact_status(rg.get("impact_status"))
        summary = (rg.get("summary_extract") or "")[:120]
        url = rg.get("confluence_url") or ""
        url_md = f"[lien]({url})" if url else "_(URL non fournie)_"

        # Echapper les `|` dans le summary
        summary_escaped = summary.replace("|", "\\|")
        ref_escaped = str(ref).replace("|", "\\|")
        lines.append(f"| `{ref_escaped}` | {status} | {summary_escaped} | {url_md} |")

    lines.append("")
    return "\n".join(lines)


def _normalize_impact_status(raw_status: Any) -> str:
    """Normalise l'impact_status produit par Rovo en libelle Markdown.

    Valeurs enumerees attendues (cf. prompt Rovo v9.11) :
      - 'impactee' / 'a_valider_tnr' -> '🔴 À valider en TNR'
      - 'non_regression' -> '🟡 Non-régression'
      - 'mentionnee' / 'pour_information' -> '⚪ Pour information'

    Fallback : '🔴 À valider en TNR' (par securite).
    """
    if not raw_status:
        return "🔴 À valider en TNR"
    s = str(raw_status).strip().lower().replace("-", "_").replace(" ", "_")
    if s in {"impactee", "a_valider_tnr", "a_valider", "impacted", "to_validate"}:
        return "🔴 À valider en TNR"
    if s in {"non_regression", "non_reg", "regression_check"}:
        return "🟡 Non-régression"
    if s in {"mentionnee", "pour_information", "information_only", "info"}:
        return "⚪ Pour information"
    # Valeur inconnue : on affiche tel quel + emoji defaut
    return f"🔴 À valider en TNR _(statut Rovo: {raw_status})_"


def _build_similar_incidents_section(contract: dict[str, Any]) -> str:
    """Section 5 (optionnelle) : Incidents similaires.

    Affichee uniquement si Rovo a remonte au moins 1 incident.
    Format compact : 1 ligne par incident.
    """
    incidents = contract.get("rovo_similar_past_incidents") or []
    if not incidents:
        return ""

    lines = ["## 🔍 Incidents similaires précédemment résolus\n"]
    for inc in incidents[:5]:  # max 5
        if not isinstance(inc, dict):
            continue
        key = inc.get("ticket_key", "?")
        reason = inc.get("similarity_reason", "")
        pattern = inc.get("fix_pattern_applied", "")
        parts = [f"- **{key}**"]
        if reason:
            parts.append(f"_{reason}_")
        if pattern:
            parts.append(f"→ pattern appliqué : `{pattern}`")
        lines.append(" ".join(parts))

    lines.append("")
    return "\n".join(lines)


def _build_risks_section(contract: dict[str, Any]) -> str:
    """Section optionnelle : Risques et incertitudes identifies par Rovo."""
    risks = contract.get("rovo_risks_and_uncertainties") or []
    if not risks:
        return ""

    lines = ["## ⚠️ Risques et incertitudes à valider\n"]
    for r in risks[:5]:
        if not isinstance(r, dict):
            continue
        rtype = r.get("type", "risk")
        severity = r.get("severity", "")
        desc = r.get("description", "")
        if not desc:
            continue
        # Emoji selon type
        emoji = "🔴" if rtype == "risk" and severity == "high" else \
                "🟠" if rtype == "risk" and severity == "medium" else \
                "🟡" if rtype == "risk" else "🔵"
        prefix = f"{emoji} **{rtype}**"
        if severity:
            prefix += f" ({severity})"
        lines.append(f"- {prefix} : {desc}")
    lines.append("")
    return "\n".join(lines)


def _build_meta_section(
    *,
    contract: dict[str, Any],
    plan: dict[str, Any],
    result: dict[str, Any],
    bundle_version: str,
    model_name: str,
    run_id: str,
    is_patch_mr: bool,
) -> str:
    """Section finale : Meta-informations agentiques."""
    lines = ["---\n", "## 🤖 Méta-informations agentiques\n"]

    # v9.13.2 : libelle de source generique cote utilisateur
    # On masque l'origine technique (rovo+local-fix-agent) pour ne pas
    # exposer les agents internes dans la MR. La MR affiche un seul nom
    # generique qui represente le pipeline complet.
    source = "autofix-agent"
    confidence = contract.get("rovo_confidence_level") or ""
    if not confidence:
        score = plan.get("validation_score") or result.get("validation_score") or 0
        if isinstance(score, (int, float)):
            if score >= 80:
                confidence = "high"
            elif score >= 60:
                confidence = "medium"
            else:
                confidence = "low"

    rovo_repaired = contract.get("rovo_repaired_by") or ""
    rovo_partial = contract.get("rovo_partial_extraction") or False

    lines.append(f"- **Source** : `{source}`")
    lines.append(f"- **Bundle** : `{bundle_version}`")
    if model_name:
        lines.append(f"- **Modèle local** : `{model_name}`")
    if confidence:
        lines.append(f"- **Confiance** : `{confidence}`")
    if run_id:
        lines.append(f"- **Run ID** : `{run_id}`")
    # v9.13.2 : notes techniques internes masquees pour ne pas exposer Rovo
    # cote utilisateur. Ces infos restent dans les artefacts de run (rovo_extraction.json,
    # rovo_gating.json) pour debugging cote equipe technique.

    if not is_patch_mr:
        lines.append("")
        lines.append("> ℹ️ Cette MR est une **proposition de correction**, sans patch appliqué automatiquement. Un développeur doit valider l'analyse et appliquer le patch manuellement avant merge.")

    return "\n".join(lines) + "\n"
