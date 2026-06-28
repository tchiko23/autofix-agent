"""app.rag.retriever — Client de retrieval au runtime.

Appele par le PatchPlanner avant de generer un patch. Construit une query
a partir du contrat de correction, calcule l'embedding, et recupere les
top-K episodes les plus similaires.

Si le RAG est desactive (rag.enabled=false) ou si l'index est introuvable,
retourne une liste vide silencieusement (le bundle fonctionne comme avant).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.rag.embedder import embed_query
from app.rag.indexer import EpisodeIndex


class RAGRetriever:
    """Retriever utilise au runtime par le PatchPlanner.

    Usage:
        retriever = RAGRetriever(rag_root_path, model_name)
        episodes = retriever.find_similar(
            contract=contract,
            target_file=target_file,
            top_k=3,
        )
        # episodes = liste de dicts avec metadata + similarity
    """

    def __init__(
        self,
        rag_root: Path | str,
        embedding_model: str = "intfloat/multilingual-e5-small",
        enabled: bool = True,
        log=print,
    ):
        self.rag_root = Path(rag_root)
        self.embedding_model = embedding_model
        self.enabled = enabled
        self.log = log
        self._episode_index: EpisodeIndex | None = None

    def is_available(self) -> bool:
        """Renvoie True si le RAG est utilisable (active + index present)."""
        if not self.enabled:
            return False
        episodes_path = self.rag_root / "episodes.chroma"
        return episodes_path.is_dir() and any(episodes_path.iterdir())

    def _get_index(self) -> EpisodeIndex | None:
        if self._episode_index is not None:
            return self._episode_index
        if not self.is_available():
            return None
        try:
            self._episode_index = EpisodeIndex(
                self.rag_root / "episodes.chroma",
                log=self.log,
            )
            return self._episode_index
        except Exception as exc:
            self.log(f"[RAG] cannot open index: {exc}")
            return None

    def find_similar(
        self,
        contract: dict[str, Any],
        target_file: str = "",
        target_method: str = "",
        repo_filter: str | None = None,
        top_k: int = 3,
        min_freshness: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Recupere les top-K episodes similaires au probleme courant.

        Strategie:
        - Construit un texte de query a partir du contrat (symptome metier)
        - Calcule l'embedding query
        - Recherche dans la collection 'episodes_by_symptom'
        - Filtre par repo si demande
        - Filtre par fraicheur minimum
        """
        idx = self._get_index()
        if idx is None:
            return []

        # Construction de la query textuelle
        query_text = self._build_query_text(contract, target_file, target_method)
        if not query_text.strip():
            return []

        try:
            emb = embed_query(query_text, model_name_or_path=self.embedding_model)
        except Exception as exc:
            self.log(f"[RAG] embedding failed: {exc}")
            return []

        if not emb:
            return []

        try:
            results = idx.query_similar(
                query_embedding_symptom=emb,
                repo_filter=repo_filter,
                top_k=top_k,
                min_freshness=min_freshness,
            )
        except Exception as exc:
            self.log(f"[RAG] query failed: {exc}")
            return []

        self.log(f"[RAG] retrieved {len(results)} similar episodes for query")
        return results

    @staticmethod
    def _build_query_text(
        contract: dict[str, Any],
        target_file: str,
        target_method: str,
    ) -> str:
        """Compose un texte de requete a partir du contrat de correction.

        On reprend les memes signaux que le builder utilise pour le texte
        'symptom' des episodes indexes, afin que la similarite cosine
        marche dans le bon sens.
        """
        parts = []
        if contract.get("problem_summary"):
            parts.append(f"Ticket: {contract['problem_summary']}")
        if contract.get("immediate_cause"):
            parts.append(f"Description: {contract['immediate_cause']}")
        if target_file:
            parts.append(f"File: {target_file}")
        if target_method:
            parts.append(f"Method: {target_method}")
        if contract.get("fix_style"):
            parts.append(f"Fix style: {contract['fix_style']}")
        # On garde les premiers acceptance criteria pour aider la similarite
        ac = contract.get("acceptance_criteria") or []
        if ac:
            parts.append("Criteres:\n" + "\n".join(str(x) for x in ac[:5])[:500])
        return "\n".join(parts)


def format_episodes_for_prompt(episodes: list[dict[str, Any]], max_chars: int = 6000) -> str:
    """Formate une liste d'episodes pour injection dans le prompt PatchPlanner.

    Chaque episode est presente comme un exemple historique avec:
    - Ticket key + summary
    - Repo + fichier principal
    - Extrait du document indexe (qui contient symptome + description)

    Tronque a max_chars total pour eviter de saturer le contexte du LLM.
    """
    if not episodes:
        return ""
    lines = ["# Episodes historiques similaires (corrections passees dans ce projet) :", ""]
    total_chars = 0
    for i, ep in enumerate(episodes, 1):
        meta = ep.get("metadata") or {}
        ticket = meta.get("ticket_key", "?")
        summary = meta.get("summary", "")
        repo = meta.get("repo", "?")
        primary_file = meta.get("primary_file", "")
        sim = ep.get("similarity", 0.0)
        rg_refs = meta.get("rg_refs", "")
        commit_sha = meta.get("commit_short_sha", "")
        commit_date = (meta.get("commit_date") or "")[:10]

        block = []
        block.append(f"## Exemple {i} - {ticket} (similarite {sim:.2f})")
        block.append(f"- Repo: {repo}")
        block.append(f"- Fichier principal: {primary_file}")
        if summary:
            block.append(f"- Resume: {summary}")
        if rg_refs:
            block.append(f"- RG referencees: {rg_refs}")
        if commit_sha:
            block.append(f"- Correctif applique (commit {commit_sha}, {commit_date}):")

        # Extrait du document (symptome + commencement)
        doc = ep.get("document") or ""
        if doc:
            extract = doc[:1500]
            block.append(f"```\n{extract}\n```")
        block.append("")

        block_text = "\n".join(block)
        if total_chars + len(block_text) > max_chars:
            lines.append(f"_(... {len(episodes) - i + 1} autres episodes tronques pour respecter la limite de contexte)_")
            break
        lines.append(block_text)
        total_chars += len(block_text)

    return "\n".join(lines)
