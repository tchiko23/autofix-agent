"""app.rag.indexer — Indexation et query des episodes dans ChromaDB.

ChromaDB est utilise en mode embedded (sans serveur). Le store physique
est un dossier sur disque. Une seule base unifiee `episodes.chroma` avec
3 collections semantiques (symptom / patch / context), et une base
separee `rules_of_business.chroma` pour les RG.

Chaque episode est tagge avec des metadonnees filtrables au query time
(repo, ticket_key, freshness, status). Cela permet, par exemple,
to retrieve only episodes from the target repo over les 12 derniers mois.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.rag.models import Episode, RuleOfBusiness


# Noms des collections ChromaDB pour les 3 axes semantiques
COLLECTION_SYMPTOM = "episodes_by_symptom"
COLLECTION_PATCH = "episodes_by_patch"
COLLECTION_CONTEXT = "episodes_by_context"
COLLECTION_RG = "rules_of_business"


class EpisodeIndex:
    """Gestionnaire de l'index ChromaDB des episodes.

    Utilisation:
        idx = EpisodeIndex("/path/to/rag_index/episodes.chroma")
        idx.upsert_episodes(episodes_with_embeddings)
        results = idx.query_similar("NullPointerException on user lookup", repo_filter="back", top_k=5)
    """

    def __init__(self, store_path: Path, log=print):
        self.store_path = Path(store_path)
        self.log = log
        self._client = None
        self._collections: dict[str, Any] = {}

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:
            raise RuntimeError(
                "chromadb non installe. Installer avec: pip install chromadb"
            ) from exc

        self.store_path.mkdir(parents=True, exist_ok=True)
        # anonymized_telemetry=False est CRITIQUE pour RGPD/secteur regule
        self._client = chromadb.PersistentClient(
            path=str(self.store_path),
            settings=Settings(anonymized_telemetry=False, allow_reset=False),
        )
        return self._client

    def _get_collection(self, name: str, dimension: int):
        if name in self._collections:
            return self._collections[name]
        client = self._ensure_client()
        # get_or_create pour idempotence
        col = client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine", "dimension": dimension},
        )
        self._collections[name] = col
        return col

    def upsert_episodes(
        self,
        episodes: list[Episode],
        embeddings_symptom: list[list[float]],
        embeddings_patch: list[list[float]],
        embeddings_context: list[list[float]],
    ) -> int:
        """Insere ou met a jour les episodes dans les 3 collections.

        ChromaDB gere l'idempotence via les IDs : upsert d'un meme episode_id
        ecrase les donnees precedentes.
        """
        if not episodes:
            return 0
        assert len(embeddings_symptom) == len(episodes), "embeddings count mismatch"
        assert len(embeddings_patch) == len(episodes), "embeddings count mismatch"
        assert len(embeddings_context) == len(episodes), "embeddings count mismatch"

        dim = len(embeddings_symptom[0])
        col_symptom = self._get_collection(COLLECTION_SYMPTOM, dim)
        col_patch = self._get_collection(COLLECTION_PATCH, dim)
        col_context = self._get_collection(COLLECTION_CONTEXT, dim)

        ids = [ep.episode_id for ep in episodes]
        # Metadonnees filtrables (ChromaDB n'accepte que les types primitifs)
        metadatas = [self._build_metadata(ep) for ep in episodes]
        # Documents (texte source) - utile pour debug et display
        symptom_docs = [ep.symptom_text[:8000] for ep in episodes]
        patch_docs = [ep.patch_text[:8000] for ep in episodes]
        context_docs = [ep.context_text[:4000] for ep in episodes]

        col_symptom.upsert(
            ids=ids, embeddings=embeddings_symptom,
            metadatas=metadatas, documents=symptom_docs,
        )
        col_patch.upsert(
            ids=ids, embeddings=embeddings_patch,
            metadatas=metadatas, documents=patch_docs,
        )
        col_context.upsert(
            ids=ids, embeddings=embeddings_context,
            metadatas=metadatas, documents=context_docs,
        )
        return len(episodes)

    def query_similar(
        self,
        query_embedding_symptom: list[float],
        query_embedding_context: list[float] | None = None,
        repo_filter: str | None = None,
        top_k: int = 5,
        min_freshness: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Recupere les episodes les plus similaires.

        Strategie: requete principale sur le symptome, optionnellement
        croisee avec le contexte fichier.

        Filtres ChromaDB sur metadonnees (repo, freshness_weight).
        """
        dim = len(query_embedding_symptom)
        col_symptom = self._get_collection(COLLECTION_SYMPTOM, dim)

        # Construire le filtre ChromaDB (format "where")
        where: dict[str, Any] = {}
        if repo_filter and repo_filter != "unknown":
            where["repo"] = {"$eq": repo_filter}
        if min_freshness > 0:
            where["freshness_weight"] = {"$gte": min_freshness}

        # Si plusieurs filtres, ChromaDB attend un $and
        if len(where) > 1:
            where = {"$and": [{k: v} for k, v in where.items()]}

        result = col_symptom.query(
            query_embeddings=[query_embedding_symptom],
            n_results=top_k,
            where=where if where else None,
        )
        return self._format_query_result(result)

    @staticmethod
    def _build_metadata(ep: Episode) -> dict[str, Any]:
        """Construit le dict de metadonnees ChromaDB (types primitifs uniquement)."""
        return {
            "episode_id": ep.episode_id,
            "ticket_key": ep.ticket_key,
            "repo": ep.repo,
            "commit_sha": ep.commit.sha,
            "commit_short_sha": ep.commit.short_sha,
            "commit_date": ep.commit.date,
            "timestamp": ep.timestamp,
            "freshness_weight": float(ep.freshness_weight),
            "is_deprecated": bool(ep.is_deprecated),
            "files_count": len(ep.commit.files_changed),
            "primary_file": ep.commit.files_changed[0] if ep.commit.files_changed else "",
            "rg_count": len(ep.rules_of_business),
            "rg_refs": ",".join(rg.reference for rg in ep.rules_of_business)[:500],
            "priority": ep.ticket.priority if ep.ticket else "",
            "summary": (ep.ticket.summary[:300] if ep.ticket else ""),
        }

    @staticmethod
    def _format_query_result(result: dict[str, Any]) -> list[dict[str, Any]]:
        """Convertit le format ChromaDB en liste de dicts plus exploitables."""
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        out = []
        for i, doc_id in enumerate(ids):
            out.append({
                "episode_id": doc_id,
                "document": documents[i] if i < len(documents) else "",
                "metadata": metadatas[i] if i < len(metadatas) else {},
                "distance": float(distances[i]) if i < len(distances) else 1.0,
                "similarity": 1.0 - float(distances[i]) if i < len(distances) else 0.0,
            })
        return out

    def count(self) -> int:
        """Renvoie le nombre d'episodes indexes."""
        try:
            col = self._get_collection(COLLECTION_SYMPTOM, 384)
            return col.count()
        except Exception:
            return 0


class RuleOfBusinessIndex:
    """Index des Regles de Gestion (<DOMAIN>.RG<N>) depuis Confluence."""

    def __init__(self, store_path: Path, log=print):
        self.store_path = Path(store_path)
        self.log = log
        self._client = None
        self._collection = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        import chromadb
        from chromadb.config import Settings
        self.store_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(self.store_path),
            settings=Settings(anonymized_telemetry=False, allow_reset=False),
        )
        return self._client

    def _get_col(self, dim: int):
        if self._collection is not None:
            return self._collection
        client = self._ensure_client()
        self._collection = client.get_or_create_collection(
            name=COLLECTION_RG,
            metadata={"hnsw:space": "cosine", "dimension": dim},
        )
        return self._collection

    def upsert_rules(
        self,
        rules: list[RuleOfBusiness],
        embeddings: list[list[float]],
    ) -> int:
        if not rules:
            return 0
        col = self._get_col(len(embeddings[0]))
        ids = [r.reference for r in rules]
        docs = [f"{r.title}\n{r.content}"[:6000] for r in rules]
        metadatas = [{
            "reference": r.reference,
            "title": r.title,
            "url": r.url,
            "space_key": r.space_key,
            "last_modified": r.last_modified,
        } for r in rules]
        col.upsert(ids=ids, embeddings=embeddings, documents=docs, metadatas=metadatas)
        return len(rules)


def save_manifest(manifest_path: Path, data: dict[str, Any]) -> None:
    """Persiste l'etat de l'indexation pour le refresh incremental."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat()
    manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """Charge l'etat de l'indexation precedente. Renvoie dict vide si absent."""
    if not manifest_path.is_file():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
