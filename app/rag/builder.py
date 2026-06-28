"""app.rag.builder — Orchestrateur qui construit l'index RAG complet.

Pipeline:
  1. Crawl Git multi-repos -> liste de CommitInfo
  2. Enrichissement Jira (description, labels, commentaires, RG referencees)
  3. Enrichissement Confluence (contenu des RG)
  4. Assemblage Episode (commit + ticket + RG + metadonnees)
  5. Embedding texte (3 axes: symptom, patch, context)
  6. Indexation ChromaDB
  7. Sauvegarde manifest pour refresh incremental
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.rag.confluence_client import ConfluenceClient
from app.rag.embedder import embed_texts
from app.rag.git_crawler import (
    crawl_repo_commits,
    extract_rg_references,
    extract_ticket_keys,
    infer_repo_tag_from_files,
    infer_repo_tag_from_path,
)
from app.rag.indexer import (
    EpisodeIndex,
    RuleOfBusinessIndex,
    load_manifest,
    save_manifest,
)
from app.rag.jira_client import JiraClient, infer_repo_from_jira_ticket
from app.rag.models import CommitInfo, Episode, JiraTicketInfo, RuleOfBusiness, compute_freshness_weight


def _build_symptom_text(ep: Episode) -> str:
    """Construit le texte pour l'embedding 'symptom' (matchera les nouveaux tickets)."""
    parts = []
    if ep.ticket:
        parts.append(f"Ticket: {ep.ticket.summary}")
        if ep.ticket.description:
            parts.append(f"Description: {ep.ticket.description[:2000]}")
        if ep.ticket.comments:
            # On garde les 3 premiers commentaires (souvent les + informatifs)
            parts.append("Commentaires:\n" + "\n---\n".join(ep.ticket.comments[:3])[:2000])
    else:
        parts.append(f"Commit: {ep.commit.message[:1500]}")
    return "\n".join(parts)


def _build_patch_text(ep: Episode) -> str:
    """Texte pour embedding 'patch' (matchera des patterns de correction similaires)."""
    parts = [f"Commit message: {ep.commit.message[:500]}"]
    if ep.commit.diff:
        parts.append(f"Diff:\n{ep.commit.diff[:6000]}")
    return "\n".join(parts)


def _build_context_text(ep: Episode) -> str:
    """Texte pour embedding 'context' (matchera la zone du code)."""
    parts = [f"Repo: {ep.repo}"]
    if ep.commit.files_changed:
        parts.append("Files: " + ", ".join(ep.commit.files_changed[:20]))
    if ep.ticket and ep.ticket.components:
        parts.append("Components: " + ", ".join(ep.ticket.components))
    return "\n".join(parts)


def _normalize_branch_for_repo_tag(commit: CommitInfo, repo_default_tag: str) -> str:
    """Determine le tag repo final pour un commit avec priorite:
    1. Repo Git (myapp-back -> back)
    2. Inference depuis les fichiers modifies (cas monorepo)
    """
    if repo_default_tag and repo_default_tag != "unknown":
        return repo_default_tag
    return infer_repo_tag_from_files(commit.files_changed)


def build_episodes_from_commits(
    commits: list[CommitInfo],
    repo_default_tag: str,
    jira: JiraClient | None,
    confluence: ConfluenceClient | None,
    confluence_spaces: list[str] | None,
    log=print,
) -> tuple[list[Episode], dict[str, RuleOfBusiness]]:
    """Assemble les commits + tickets Jira + RG Confluence en Episodes.

    Retourne aussi le dict des RG indexees (deduplique par reference).
    """
    episodes: list[Episode] = []
    rg_collected: dict[str, RuleOfBusiness] = {}
    jira_cache: dict[str, JiraTicketInfo | None] = {}

    for commit in commits:
        ticket_keys = extract_ticket_keys(commit.message)
        if not ticket_keys:
            continue
        # Un commit peut referencer plusieurs tickets. On prend le premier
        # comme principal (souvent le ticket de hotfix), on ignore les autres.
        ticket_key = ticket_keys[0]

        # Enrichissement Jira (avec cache pour eviter doublons API)
        ticket: JiraTicketInfo | None = None
        if jira is not None:
            if ticket_key not in jira_cache:
                jira_cache[ticket_key] = jira.fetch_ticket(ticket_key)
            ticket = jira_cache[ticket_key]

        # Determination du tag repo final
        repo_tag = _normalize_branch_for_repo_tag(commit, repo_default_tag)
        # Si Jira a un label/composant explicite, il prime sur l'inference
        if ticket is not None:
            jira_repo = infer_repo_from_jira_ticket(ticket, fallback=repo_tag)
            if jira_repo != "unknown":
                repo_tag = jira_repo

        # Collecte des RG mentionnees (commit + ticket)
        commit_rgs = extract_rg_references(commit.message)
        ticket_rgs = ticket.referenced_rg if ticket else []
        all_rg_refs = sorted(set(commit_rgs + ticket_rgs))

        # Recuperation Confluence (avec cache)
        episode_rgs: list[RuleOfBusiness] = []
        if confluence is not None:
            for ref in all_rg_refs:
                if ref in rg_collected:
                    episode_rgs.append(rg_collected[ref])
                    continue
                rg = confluence.find_rule_of_business(ref, space_keys=confluence_spaces)
                if rg:
                    rg_collected[ref] = rg
                    episode_rgs.append(rg)

        ep = Episode(
            episode_id=f"{ticket_key}_{commit.short_sha}",
            ticket_key=ticket_key,
            repo=repo_tag,
            commit=commit,
            ticket=ticket,
            rules_of_business=episode_rgs,
            timestamp=commit.date,
            freshness_weight=compute_freshness_weight(commit.date),
        )
        ep.symptom_text = _build_symptom_text(ep)
        ep.patch_text = _build_patch_text(ep)
        ep.context_text = _build_context_text(ep)
        episodes.append(ep)

    log(f"[BUILDER] assembled {len(episodes)} episodes, {len(rg_collected)} unique RGs")
    return episodes, rg_collected


def index_episodes(
    episodes: list[Episode],
    embedding_model: str,
    index_path: Path,
    log=print,
) -> int:
    """Calcule les 3 embeddings et insere dans ChromaDB."""
    if not episodes:
        return 0
    log(f"[BUILDER] embedding {len(episodes)} episodes...")

    symptoms = [ep.symptom_text for ep in episodes]
    patches = [ep.patch_text for ep in episodes]
    contexts = [ep.context_text for ep in episodes]

    emb_sym = embed_texts(symptoms, model_name_or_path=embedding_model)
    emb_pat = embed_texts(patches, model_name_or_path=embedding_model)
    emb_ctx = embed_texts(contexts, model_name_or_path=embedding_model)

    idx = EpisodeIndex(index_path, log=log)
    inserted = idx.upsert_episodes(episodes, emb_sym, emb_pat, emb_ctx)
    log(f"[BUILDER] indexed {inserted} episodes into {index_path}")
    return inserted


def index_rules_of_business(
    rules: list[RuleOfBusiness],
    embedding_model: str,
    rg_index_path: Path,
    log=print,
) -> int:
    if not rules:
        return 0
    log(f"[BUILDER] embedding {len(rules)} rules of business...")
    texts = [f"{r.title}\n{r.content}" for r in rules]
    embs = embed_texts(texts, model_name_or_path=embedding_model)
    idx = RuleOfBusinessIndex(rg_index_path, log=log)
    return idx.upsert_rules(rules, embs)


def build_full_index(
    git_repos: list[dict[str, str]],
    rag_root: Path,
    embedding_model: str,
    jira_client: JiraClient | None,
    confluence_client: ConfluenceClient | None,
    confluence_spaces: list[str] | None,
    since: str = "2 years ago",
    log=print,
) -> dict[str, Any]:
    """Construit l'index RAG complet a partir de zero.

    git_repos: liste de dicts {"path": "...", "tag": "back|front|batch"}
    rag_root: dossier ou stocker episodes.chroma et rules_of_business.chroma
    """
    rag_root = Path(rag_root)
    rag_root.mkdir(parents=True, exist_ok=True)
    episode_index_path = rag_root / "episodes.chroma"
    rg_index_path = rag_root / "rules_of_business.chroma"
    raw_dir = rag_root / "raw"
    raw_dir.mkdir(exist_ok=True)

    all_episodes: list[Episode] = []
    all_rules: dict[str, RuleOfBusiness] = {}
    stats_per_repo: dict[str, int] = {}

    for repo_cfg in git_repos:
        repo_path = Path(repo_cfg["path"])
        repo_tag = repo_cfg.get("tag") or infer_repo_tag_from_path(repo_path.name)
        log(f"[BUILDER] === repo {repo_tag} : {repo_path} ===")

        commits = list(crawl_repo_commits(repo_path, since=since, log=log))
        # Persistance brute pour debug / reprise
        (raw_dir / f"commits_{repo_tag}.jsonl").write_text(
            "\n".join(json.dumps({
                "sha": c.sha, "short_sha": c.short_sha, "date": c.date,
                "message_first_line": c.message.splitlines()[0] if c.message else "",
                "files_count": len(c.files_changed),
            }) for c in commits),
            encoding="utf-8",
        )

        episodes, rgs = build_episodes_from_commits(
            commits=commits,
            repo_default_tag=repo_tag,
            jira=jira_client,
            confluence=confluence_client,
            confluence_spaces=confluence_spaces,
            log=log,
        )
        all_episodes.extend(episodes)
        all_rules.update(rgs)
        stats_per_repo[repo_tag] = len(episodes)

    log(f"[BUILDER] total episodes: {len(all_episodes)}, total RGs: {len(all_rules)}")

    # Indexation
    inserted = index_episodes(all_episodes, embedding_model, episode_index_path, log=log)
    rg_inserted = 0
    if all_rules:
        rg_inserted = index_rules_of_business(
            list(all_rules.values()), embedding_model, rg_index_path, log=log
        )

    # Manifest pour refresh incremental
    manifest = {
        "version": "9.8",
        "embedding_model": embedding_model,
        "since": since,
        "repos": [{"path": r["path"], "tag": r.get("tag", "unknown")} for r in git_repos],
        "episodes_count": inserted,
        "rules_count": rg_inserted,
        "stats_per_repo": stats_per_repo,
        "last_commit_per_repo": _collect_last_commits(git_repos),
    }
    save_manifest(rag_root / "manifest.json", manifest)
    log(f"[BUILDER] manifest saved to {rag_root / 'manifest.json'}")
    return manifest


def _collect_last_commits(git_repos: list[dict[str, str]]) -> dict[str, str]:
    """Retient le HEAD de chaque repo pour le refresh incremental."""
    import subprocess
    out: dict[str, str] = {}
    for r in git_repos:
        tag = r.get("tag", "unknown")
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=r["path"], capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                out[tag] = result.stdout.strip()
        except Exception:
            continue
    return out
