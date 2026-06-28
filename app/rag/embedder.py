"""app.rag.embedder — Calcul d'embeddings semantiques pour les episodes.

Utilise sentence-transformers avec multilingual-e5-small par defaut.
Telechargement automatique du modele au premier usage (HuggingFace) avec
fallback offline si modele deja en cache.

Pour un usage 100% airgap, on peut configurer rag.embedding_model_path
avec un chemin local vers le modele pre-telecharge.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

# Variables module pour cacher l'instance (les imports lourds ne se font
# qu'au premier appel, pas au demarrage du bundle).
_MODEL = None
_MODEL_NAME = None


def _load_model(model_name_or_path: str):
    """Charge le modele sentence-transformers (lazy + cached).

    Si model_name_or_path est un chemin local existant, on charge depuis le
    disque. Sinon on demande a sentence-transformers de telecharger
    (necessite acces HuggingFace).
    """
    global _MODEL, _MODEL_NAME
    if _MODEL is not None and _MODEL_NAME == model_name_or_path:
        return _MODEL

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers n'est pas installe. "
            "Installer avec: pip install sentence-transformers"
        ) from exc

    # Variables d'environnement pour HuggingFace offline si necessaire
    # (HF_HUB_OFFLINE=1 pour forcer le cache, sans tentative reseau)
    target = model_name_or_path
    if Path(target).is_dir():
        # Chemin local
        _MODEL = SentenceTransformer(target, device="cpu")
    else:
        # Nom de modele HuggingFace (telechargement si pas en cache)
        _MODEL = SentenceTransformer(target, device="cpu")

    _MODEL_NAME = model_name_or_path
    return _MODEL


def embed_texts(
    texts: list[str],
    model_name_or_path: str = "intfloat/multilingual-e5-small",
    batch_size: int = 16,
    prefix: str = "passage: ",
) -> list[list[float]]:
    """Calcule les embeddings d'une liste de textes.

    Pour multilingual-e5, on prefixe les textes avec 'passage: ' (indexation)
    ou 'query: ' (recherche). Ce prefixe ameliore la qualite de la similarite.

    Retourne une liste de vecteurs (chaque vecteur = liste de floats).
    """
    if not texts:
        return []
    model = _load_model(model_name_or_path)

    # Appliquer le prefixe e5 si necessaire (skip si chemin local custom)
    if "e5" in model_name_or_path.lower():
        prefixed = [prefix + (t or "") for t in texts]
    else:
        prefixed = list(texts)

    # Encode produit des np.ndarray, on convertit en liste de listes
    embeddings = model.encode(
        prefixed,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,  # Important pour cosine similarity
    )
    return [vec.tolist() for vec in embeddings]


def embed_query(
    text: str,
    model_name_or_path: str = "intfloat/multilingual-e5-small",
) -> list[float]:
    """Calcule l'embedding d'un texte de requete (avec prefixe 'query:')."""
    if not text:
        return []
    vecs = embed_texts([text], model_name_or_path=model_name_or_path, prefix="query: ")
    return vecs[0] if vecs else []


def get_embedding_dimension(model_name_or_path: str = "intfloat/multilingual-e5-small") -> int:
    """Renvoie la dimension du modele (384 pour e5-small, 768 pour e5-base, etc.)."""
    model = _load_model(model_name_or_path)
    return model.get_sentence_embedding_dimension()
