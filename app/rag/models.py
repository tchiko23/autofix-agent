"""app.rag.models — Modele de donnees des episodes de resolution de bug.

Un Episode represente UNE resolution complete d'un bug historique:
  - Le probleme (ticket Jira + symptome)
  - Le diagnostic (fichiers, methodes, RG impactees)
  - La solution (commit Git + diff)
  - Les metadonnees pour filtrage (repo, frafraicheur, statut)

Cette structure est l'unite atomique du RAG. Chaque episode est embedde
sur 3 axes semantiques (symptome, patch, contexte fichier) et indexe
dans ChromaDB.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


@dataclass
class CommitInfo:
    """Informations Git d'un commit de hotfix."""
    sha: str
    short_sha: str
    author: str
    email: str
    date: str  # ISO 8601
    message: str
    branch: str = ""
    files_changed: list[str] = field(default_factory=list)
    diff: str = ""  # Tronque a 8000 chars pour eviter explosion taille
    diff_truncated: bool = False


@dataclass
class JiraTicketInfo:
    """Informations Jira d'un ticket RUN."""
    key: str  # TICKET-NNNNN
    summary: str
    description: str
    priority: str = ""
    status: str = ""
    resolution: str = ""
    components: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    # RG referencees dans description + commentaires (regex <DOMAIN>.RG<N>)
    referenced_rg: list[str] = field(default_factory=list)


@dataclass
class RuleOfBusiness:
    """Une regle de gestion <DOMAIN>.RG<N> indexee depuis Confluence."""
    reference: str  # PAYMENTS.RG004
    title: str
    content: str  # Texte propre (HTML stripped)
    url: str = ""
    space_key: str = ""
    last_modified: str = ""


@dataclass
class Episode:
    """Episode de resolution de bug = un commit + son ticket + ses RG.

    C'est l'unite atomique du RAG. Chaque Episode est embedde et indexe
    pour permettre la recuperation par similarite semantique.
    """
    episode_id: str  # RUN-NNNNN_HHHHHHHH (ticket_key + short_sha)
    ticket_key: str
    repo: str  # "back" | "front" | "batch"
    commit: CommitInfo
    ticket: JiraTicketInfo | None = None
    rules_of_business: list[RuleOfBusiness] = field(default_factory=list)
    # Metadonnees calculees
    timestamp: str = ""  # ISO de la date du commit, pour tri/filtrage
    freshness_weight: float = 1.0  # 1.0 = recent, 0.5 = ancien, 0.0 = exclu
    is_deprecated: bool = False
    # Texte derive pour embedding (calcule par builder)
    symptom_text: str = ""  # Pour embedding_symptom
    patch_text: str = ""    # Pour embedding_patch
    context_text: str = ""  # Pour embedding_context

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        commit_data = data.pop("commit")
        ticket_data = data.pop("ticket", None)
        rg_data = data.pop("rules_of_business", [])
        ep = cls(**data)
        ep.commit = CommitInfo(**commit_data)
        if ticket_data:
            ep.ticket = JiraTicketInfo(**ticket_data)
        ep.rules_of_business = [RuleOfBusiness(**rg) for rg in rg_data]
        return ep


def compute_freshness_weight(commit_date_iso: str, max_age_days: int = 730) -> float:
    """Calcule un poids de fraicheur entre 0.0 et 1.0.

    Plus le commit est recent, plus son poids est eleve. Permet de
    moins ponderer les patches anciens (conventions peut-etre obsoletes)
    sans les exclure totalement.

    max_age_days par defaut = 730 (2 ans), conforme a la config v9.8.
    """
    try:
        commit_dt = datetime.fromisoformat(commit_date_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.5  # Valeur neutre si date illisible

    age_days = (datetime.now().astimezone() - commit_dt).days
    if age_days < 0:
        return 1.0  # Commit dans le futur (horloge desynchronisee)
    if age_days >= max_age_days:
        return 0.1  # Tres ancien mais on garde une trace
    # Decroissance lineaire de 1.0 (jour 0) a 0.3 (jour max_age_days)
    return max(0.3, 1.0 - (age_days / max_age_days) * 0.7)
