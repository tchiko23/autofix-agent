from __future__ import annotations

from typing import Any


# v9.5: icônes et libellés humains par catégorie de solution alternative.
_CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "configuration_fix": ("⚙️", "Correction par configuration"),
    "code_fix": ("💻", "Correction par code applicatif"),
    "mapping_fix": ("🔌", "Correction par mapping ORM / type"),
    "data_fix": ("🗄️", "Correction par script SQL / données"),
    "process_fix": ("📋", "Correction par processus / planification"),
}

_APPLICABILITY_ICONS: dict[str, str] = {
    "applicable": "✅",
    "non applicable": "❌",
    "à valider": "🟡",
}


def _render_alternative(item: dict[str, Any]) -> list[str]:
    """Rend une entrée d'alternative_solutions en bloc Markdown."""
    cat = str(item.get("category") or "other")
    icon, default_label = _CATEGORY_LABELS.get(cat, ("🔧", cat.replace("_", " ").title()))
    title = str(item.get("title") or default_label).strip() or default_label
    applicability = str(item.get("applicability") or "à valider").lower()
    app_icon = _APPLICABILITY_ICONS.get(applicability, "🟡")
    effort = str(item.get("effort") or "").strip()
    scope = str(item.get("scope") or "").strip()

    # En-tête : icône catégorie + titre + statut applicabilité
    header_meta_parts = [f"{app_icon} {applicability}"]
    if effort:
        header_meta_parts.append(f"effort {effort}")
    if scope:
        header_meta_parts.append(f"périmètre {scope}")
    meta_line = " · ".join(header_meta_parts)

    lines = [
        f"### {icon} {default_label} — {title}",
        f"_{meta_line}_",
        "",
    ]

    description = str(item.get("description") or "").strip()
    if description and description != "NON_ETABLI":
        lines.append(description)
        lines.append("")
    elif description == "NON_ETABLI":
        lines.append("_NON_ETABLI — aucune analyse fournie pour cette catégorie._")
        lines.append("")

    # Code snippet (SQL / properties / Java) si présent
    code = str(item.get("code_snippet") or "").strip()
    if code:
        # On ne devine pas le langage côté rendu : le LLM peut écrire SQL,
        # properties, java, yaml. Bloc de code générique.
        lines.append("```")
        lines.append(code)
        lines.append("```")
        lines.append("")

    advantages = item.get("advantages") or []
    drawbacks = item.get("drawbacks") or []
    if advantages:
        lines.append("**Avantages**")
        lines.extend([f"- {a}" for a in advantages])
        lines.append("")
    if drawbacks:
        lines.append("**Inconvénients**")
        lines.extend([f"- {d}" for d in drawbacks])
        lines.append("")

    reason = str(item.get("applicability_reason") or "").strip()
    if reason and applicability != "applicable":
        lines.append(f"_Note : {reason}_")
        lines.append("")

    return lines


def _render_alternatives_section(alternatives: list[dict[str, Any]] | None) -> list[str]:
    """Rend la section 'Solutions envisageables' à partir de la liste normalisée."""
    if not alternatives:
        return []
    lines = [
        "## Solutions envisageables",
        "",
        "Le bundle n'a pas pu produire un correctif automatique fiable. Voici "
        "les pistes de solution identifiées, examinées par catégorie. Le réviseur "
        "arbitre selon le contexte technique et fonctionnel.",
        "",
    ]
    for alt in alternatives:
        if isinstance(alt, dict):
            lines.extend(_render_alternative(alt))
    return lines


def render_proposal_markdown(proposal: dict[str, Any], *, ticket_key: str = "") -> str:
    def _section(title: str, value: Any) -> list[str]:
        lines = [f"## {title}"]
        if isinstance(value, list):
            if value:
                lines.extend([f"- {item}" for item in value])
            else:
                lines.append("- NON_ETABLI")
        else:
            lines.append(str(value or "NON_ETABLI"))
        lines.append("")
        return lines

    lines: list[str] = [f"# Proposition technico-fonctionnelle {ticket_key}".strip(), ""]
    lines += _section("Résumé", proposal.get("problem_summary"))
    lines += _section("Ce qui s'est passé", proposal.get("what_happened"))
    lines += _section("Cause racine", proposal.get("root_cause"))
    lines += _section("Fichier cible", proposal.get("primary_target_file"))
    lines += _section("Méthode cible", proposal.get("primary_target_method"))
    lines += _section("Stratégie de correction", proposal.get("fix_strategy"))
    lines += _section("Solution proposée", proposal.get("proposed_solution"))
    lines += _section("Pourquoi cette solution", proposal.get("why_this_solution"))
    lines += _section("Pourquoi pas plus large", proposal.get("why_not_broader_change"))
    # v9.5: nouvelle section structurée des solutions alternatives examinées
    # par catégorie. Elle apparaît AVANT les contraintes/risques/tests pour
    # donner au réviseur la vue d'ensemble dès le début.
    lines += _render_alternatives_section(proposal.get("alternative_solutions"))
    lines += _section("Contraintes métier à préserver", proposal.get("business_contract_to_preserve"))
    lines += _section("Contraintes techniques", proposal.get("technical_constraints"))
    lines += _section("Risques", proposal.get("risks"))
    lines += _section("Tests à ajouter", proposal.get("tests_to_add"))
    lines += _section("Critères d'acceptation", proposal.get("acceptance_criteria"))
    lines += _section("Observabilité", proposal.get("observability"))
    lines += _section("Questions ouvertes", proposal.get("open_questions"))
    return "\n".join(lines).strip() + "\n"
