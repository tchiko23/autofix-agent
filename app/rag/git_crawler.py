"""app.rag.git_crawler — Extraction des commits de hotfix depuis 3 repos Git.

Crawl `git log` sur les 2 dernieres annees pour matcher les patterns de
hotfix branch (RUN-XXXXX dans le message ou nom de branche).

Pour chaque commit retenu:
  - Extraction du ticket key
  - Liste des fichiers modifies
  - Diff complet (tronque a 8000 chars)
  - Tagging du repo (back / front / batch)
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterator

from app.rag.models import CommitInfo


TICKET_KEY_PATTERN = re.compile(r"\b(RUN-\d+)\b", re.IGNORECASE)
# Match any business rule reference of the form DOMAIN.RG<N>. Per the v1.4.4
# Rovo prompt rule 7, the canonical regex matches any uppercase domain
# (letters and underscores) followed by .RGNNN. Adapt this pattern to your
# own RG naming convention if it differs.
RG_REFERENCE_PATTERN = re.compile(r"\b([A-Z][A-Z_]+\.RG\d+)\b")

# Limite de taille du diff pour eviter de stocker des commits massifs
# (refactos, merges importants). 8000 chars = environ 200 lignes de diff.
DIFF_MAX_CHARS = 8000

# Filtres anti-bruit
SKIP_MESSAGE_PATTERNS = [
    re.compile(r"^Merge\b", re.IGNORECASE),
    re.compile(r"^(WIP|wip)\b"),
    re.compile(r"^Revert\b"),
]

# Extensions de fichiers consideres comme "code source applicatif"
APPLICATION_FILE_EXTENSIONS = {
    ".java", ".kt", ".scala",  # JVM
    ".ts", ".tsx", ".js", ".jsx", ".vue",  # Front
    ".sql", ".xml", ".properties", ".yml", ".yaml",  # Config / data
}


def _run_git(args: list[str], cwd: Path, timeout: int = 60) -> str:
    """Lance une commande git et retourne stdout. Vide en cas d'echec."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def extract_ticket_keys(text: str) -> list[str]:
    """Extrait toutes les cles de tickets RUN d'un texte."""
    if not text:
        return []
    matches = TICKET_KEY_PATTERN.findall(text)
    # Uppercase + dedupe + tri
    return sorted(set(m.upper() for m in matches))


def extract_rg_references(text: str) -> list[str]:
    """Extract all business rule references of form <DOMAIN>.RG<N> from text."""
    if not text:
        return []
    return sorted(set(RG_REFERENCE_PATTERN.findall(text)))


def _should_skip_message(message: str) -> bool:
    """Vrai si le commit est du bruit (merge, WIP, revert)."""
    first_line = (message or "").splitlines()[0] if message else ""
    return any(p.search(first_line) for p in SKIP_MESSAGE_PATTERNS)


def _filter_application_files(files: list[str]) -> list[str]:
    """Garde uniquement les fichiers de code applicatif."""
    return [
        f for f in files
        if any(f.lower().endswith(ext) for ext in APPLICATION_FILE_EXTENSIONS)
    ]


def crawl_repo_commits(
    repo_path: Path,
    since: str = "2 years ago",
    max_commits: int = 5000,
    log=print,
) -> Iterator[CommitInfo]:
    """Crawle les commits d'un repo Git et yield les CommitInfo retenus.

    Filtres appliques:
    - Message contient RUN-XXXXX
    - Pas un Merge / WIP / Revert
    - Au moins 1 fichier de code applicatif modifie
    - Diff < limit (sinon tronque + flag)
    """
    repo_path = Path(repo_path)
    if not repo_path.is_dir():
        log(f"[GIT] WARN: repo not found: {repo_path}")
        return

    log(f"[GIT] crawling {repo_path}")

    # Recupere les SHAs des commits matchant RUN- depuis 'since'
    # %H = sha, %ai = date ISO, %an = author, %ae = email, %s = subject
    fmt = "%H%x09%ai%x09%an%x09%ae%x09%s"
    sha_log = _run_git(
        ["log", "--all", f"--since={since}", "--grep=RUN-", "-i",
         f"--pretty=format:{fmt}", f"--max-count={max_commits}"],
        cwd=repo_path,
        timeout=120,
    )
    if not sha_log.strip():
        log(f"[GIT] no commits matched RUN- in {repo_path}")
        return

    lines = sha_log.strip().splitlines()
    log(f"[GIT] found {len(lines)} candidate commits")

    yielded = 0
    skipped_bruit = 0
    skipped_no_files = 0

    for line in lines:
        parts = line.split("\t", 4)
        if len(parts) < 5:
            continue
        sha, date_iso, author, email, subject = parts

        # Recupere le message complet (subject + body) pour matcher RG/tickets
        full_msg = _run_git(["log", "-1", "--format=%B", sha], cwd=repo_path, timeout=10)
        if _should_skip_message(full_msg):
            skipped_bruit += 1
            continue

        # Liste des fichiers modifies
        files_raw = _run_git(
            ["show", "--name-only", "--pretty=format:", sha],
            cwd=repo_path,
            timeout=20,
        )
        files = [f for f in files_raw.strip().splitlines() if f.strip()]
        app_files = _filter_application_files(files)
        if not app_files:
            skipped_no_files += 1
            continue

        # Recupere le diff complet (tronque si trop long)
        diff_raw = _run_git(
            ["show", "--pretty=format:", "--no-color", sha],
            cwd=repo_path,
            timeout=30,
        )
        diff_truncated = False
        if len(diff_raw) > DIFF_MAX_CHARS:
            diff_raw = diff_raw[:DIFF_MAX_CHARS] + "\n... [diff truncated]"
            diff_truncated = True

        # Branche d'origine si detectable (pour les commits de hotfix
        # mergees, on retrouve souvent "hotfix/RUN-XXXXX" dans le subject)
        branch_hint = ""
        m = re.search(r"(hotfix|bugfix|fix)/(RUN-\d+)", full_msg, re.IGNORECASE)
        if m:
            branch_hint = m.group(0)

        yield CommitInfo(
            sha=sha,
            short_sha=sha[:8],
            author=author,
            email=email,
            date=date_iso,
            message=full_msg.strip(),
            branch=branch_hint,
            files_changed=files,
            diff=diff_raw,
            diff_truncated=diff_truncated,
        )
        yielded += 1

    log(f"[GIT] yielded {yielded} commits "
        f"(skipped {skipped_bruit} bruit, {skipped_no_files} no-app-files)")


def infer_repo_tag_from_path(repo_root_name: str) -> str:
    """Devine le tag (back/front/batch) depuis le nom du repo Git.

    Convention attendue: le nom contient SOIT "front", "batch", soit (en dernier
    recours) "back". On teste front et batch en priorite, puis back en fallback.
    """
    n = repo_root_name.lower()
    if "front" in n:
        return "front"
    if "batch" in n:
        return "batch"
    if "back" in n:
        return "back"
    return "unknown"


def infer_repo_tag_from_files(files: list[str]) -> str:
    """Devine le tag depuis les chemins de fichiers modifies.

    Utile quand le repo est un monorepo ou quand le repo_name ne match pas
    la convention attendue. Retourne le tag dominant.
    """
    if not files:
        return "unknown"
    counts: dict[str, int] = {"back": 0, "front": 0, "batch": 0}
    for f in files:
        fl = f.lower()
        if "front" in fl or "/ui/" in fl or fl.endswith((".ts", ".tsx", ".vue", ".jsx")):
            counts["front"] += 1
        elif "batch" in fl or "/jobs/" in fl or "scheduler" in fl:
            counts["batch"] += 1
        else:
            # Par defaut, du Java/Spring tombe en "back"
            counts["back"] += 1
    return max(counts, key=counts.get)
