"""batch_run.py — Exécution séquentielle de plusieurs tickets.

Détecte automatiquement tous les fichiers d'analyse dans `analysis/` au format
`issue_analysis_*.txt` ou `*.txt` et lance un full-run sur chacun. Entre
chaque ticket, ramène le repo cible à `origin/{base_branch}` pour repartir
d'un état propre.

Usage:
    python -m scripts.batch_run --repo <chemin_repo>
    python scripts/batch_run.py --repo <chemin_repo>

Conventions de nommage des fichiers d'entrée:
    - issue_analysis_TICKET-1234.txt   (préfixe explicite)
    - TICKET-NNNNN.txt                   (ticket seul)
    - issue_analysis.txt              (mono-ticket legacy, ignoré en mode batch
                                       sauf si seul fichier présent)

Le ticket affecté à un fichier est extrait :
    1. du nom de fichier (regex sur préfixe RUN-, BUG-, JIRA-, etc.)
    2. à défaut, de la section "0. IDENTIFICATION DU TICKET COURANT" du contenu

Sortie:
    - Run individuels dans runs/{TICKET}/{run_id}/ (comme en mode mono-ticket)
    - Récapitulatif batch dans runs/batch_summary_{TIMESTAMP}.md
    - Code de sortie 0 si tous les tickets ont été traités (même s'ils ont
      donné MR_BLOCKED ou DIAGNOSTIC_MR_CREATED), 1 si un plantage Python
      a empêché l'exécution.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


# Pattern pour extraire un ticket de type RUN-12345, BUG-1234, JIRA-1234, etc.
_TICKET_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _extract_ticket_from_filename(path: Path) -> str | None:
    """Tente d'extraire la clé de ticket depuis le nom du fichier."""
    name = path.stem
    # issue_analysis_TICKET-1234 → TICKET-NNNNN
    if name.lower().startswith("issue_analysis_"):
        candidate = name[len("issue_analysis_"):]
        m = _TICKET_KEY_PATTERN.search(candidate)
        if m:
            return m.group(1)
    # TICKET-NNNNN brut
    m = _TICKET_KEY_PATTERN.search(name)
    if m:
        return m.group(1)
    return None


def _extract_ticket_from_content(path: Path) -> str | None:
    """Fallback : extrait la clé de ticket depuis le contenu du fichier."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Cherche "Ticket Jira courant: <KEY>" puis n'importe quel pattern ticket
    for line in text.splitlines()[:80]:  # 80 premières lignes suffisent
        if "ticket jira courant" in line.lower() or "ticket:" in line.lower():
            m = _TICKET_KEY_PATTERN.search(line)
            if m:
                return m.group(1)
    # Dernier recours : premier ticket trouvé dans tout le texte
    m = _TICKET_KEY_PATTERN.search(text)
    if m:
        return m.group(1)
    return None


def discover_analysis_files(analysis_dir: Path) -> list[tuple[Path, str]]:
    """Liste les fichiers d'analyse à traiter, avec la clé de ticket extraite.

    Retourne une liste triée alphabétiquement de tuples (path, ticket_key).
    """
    if not analysis_dir.is_dir():
        return []
    candidates: list[tuple[Path, str]] = []
    for path in sorted(analysis_dir.glob("*.txt")):
        # Skip le template
        if path.name.lower() in {"issue_analysis.template.txt", "readme.txt"}:
            continue
        ticket = _extract_ticket_from_filename(path) or _extract_ticket_from_content(path)
        if ticket is None:
            # Si on ne peut pas dériver de ticket, on skippe avec warning
            print(f"[BATCH] WARN: ticket key not found in {path.name}, skipping")
            continue
        candidates.append((path, ticket))
    return candidates


def reset_repo_to_base(repo_path: Path, base_branch: str, log) -> bool:
    """Ramène le repo cible à origin/{base_branch} pour repartir propre.

    v9.16.8 : ajoute un nettoyage AGRESSIF (reset --hard HEAD + clean -fd)
    AVANT le checkout. Sans ce nettoyage, si un ticket precedent a laisse
    des fichiers modifies (cas observe sur TICKET-NNNNN v9.16.7), `git checkout`
    echoue avec "Your local changes would be overwritten". Le run global
    s'arrete alors qu'il pourrait continuer avec le ticket suivant.
    """
    try:
        # v9.16.8 : nettoyer l'etat AVANT toute operation. On ne perd rien
        # d'utile car on s'apprete a partir d'origin/{base_branch}.
        # Ces operations sont best-effort : on log l'erreur mais on continue,
        # car certains repos peuvent etre sur une branche detachee, etc.
        for cleanup_cmd in (
            ["git", "reset", "--hard", "HEAD"],
            ["git", "clean", "-fd"],
        ):
            try:
                subprocess.run(
                    cleanup_cmd, cwd=repo_path, check=True,
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
            except subprocess.CalledProcessError as cleanup_exc:
                log(f"[BATCH] WARN: cleanup '{' '.join(cleanup_cmd)}' "
                    f"failed (continue): {(cleanup_exc.stderr or '').strip()[:200]}")
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=repo_path, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        subprocess.run(
            ["git", "checkout", base_branch],
            cwd=repo_path, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{base_branch}"],
            cwd=repo_path, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        log(f"[BATCH] repo reset to origin/{base_branch}")
        return True
    except subprocess.CalledProcessError as exc:
        log(f"[BATCH] ERROR: repo reset failed: {exc.stderr or exc}")
        return False


def run_one_ticket(
    bundle_dir: Path,
    repo_path: Path,
    analysis_file: Path,
    ticket_key: str,
    log,
) -> dict[str, Any]:
    """Lance un full-run pour un ticket. Retourne un résumé pour le batch."""
    started_at = time.time()
    log(f"[BATCH] === {ticket_key} === ({analysis_file.name})")
    cmd = [
        sys.executable, "-m", "app.main",
        "--repo", str(repo_path),
        "--analysis-file", str(analysis_file),
        "--apply-patch", "--commit", "--push", "--create-mr",
    ]
    # v9.16.1 : timeout par ticket configurable via AGENT_BATCH_TICKET_TIMEOUT.
    # En mode demo (runs longs sur CPU), mettre 0 pour DESACTIVER le timeout.
    # Bug v9.16 : un timeout de 2h etait code en dur ici et tuait les tickets
    # qui depassaient 2h, alors que le mode demo veut justement les laisser finir.
    _timeout_raw = os.environ.get("AGENT_BATCH_TICKET_TIMEOUT", "0").strip()
    try:
        _timeout_val = int(_timeout_raw)
    except ValueError:
        _timeout_val = 0
    subprocess_timeout = None if _timeout_val <= 0 else _timeout_val
    if subprocess_timeout is None:
        log(f"[BATCH] {ticket_key}: no timeout (demo mode, run until completion)")
    else:
        log(f"[BATCH] {ticket_key}: timeout = {subprocess_timeout}s")
    try:
        proc = subprocess.run(
            cmd, cwd=bundle_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=subprocess_timeout
        )
        elapsed = time.time() - started_at
        outcome = _read_outcome_from_history(bundle_dir, ticket_key, started_at)
        outcome["elapsed_seconds"] = round(elapsed, 1)
        outcome["python_returncode"] = proc.returncode
        outcome["analysis_file"] = analysis_file.name
        outcome["ticket_key"] = ticket_key
        if proc.returncode != 0:
            log(f"[BATCH] {ticket_key} FAILED (returncode={proc.returncode})")
            outcome.setdefault("delivery_status", "PYTHON_ERROR")
            # On capture les dernières lignes stderr pour le summary
            tail = "\n".join((proc.stderr or "").splitlines()[-15:])
            outcome["error_tail"] = tail
        else:
            log(f"[BATCH] {ticket_key} done (status={outcome.get('delivery_status')})")
        return outcome
    except subprocess.TimeoutExpired:
        log(f"[BATCH] {ticket_key} TIMEOUT (> {subprocess_timeout}s)")
        return {
            "ticket_key": ticket_key,
            "analysis_file": analysis_file.name,
            "delivery_status": "TIMEOUT",
            "elapsed_seconds": round(time.time() - started_at, 1),
            "python_returncode": -1,
        }
    except Exception as exc:
        log(f"[BATCH] {ticket_key} EXCEPTION: {exc}")
        return {
            "ticket_key": ticket_key,
            "analysis_file": analysis_file.name,
            "delivery_status": "BATCH_EXCEPTION",
            "elapsed_seconds": round(time.time() - started_at, 1),
            "python_returncode": -1,
            "error_tail": str(exc),
        }


def _read_outcome_from_history(bundle_dir: Path, ticket_key: str, started_after: float) -> dict[str, Any]:
    """Lit history.jsonl et récupère la dernière entrée pour ce ticket
    démarrée après started_after (timestamp Unix).
    """
    history = bundle_dir / "runs" / "history.jsonl"
    if not history.exists():
        return {"delivery_status": "NO_HISTORY"}
    last_match = None
    try:
        for line in history.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("ticket_key") != ticket_key:
                continue
            # On ne peut pas filtrer précisément par started_after sans timestamp
            # dans history. On prend la dernière entrée pour ce ticket.
            last_match = entry
    except OSError:
        return {"delivery_status": "HISTORY_UNREADABLE"}
    if last_match is None:
        return {"delivery_status": "NOT_IN_HISTORY"}
    return {
        "delivery_status": last_match.get("delivery_status"),
        "mr_blocked_reason": last_match.get("mr_blocked_reason"),
        "pr_url": last_match.get("pr_url"),
        "validation_score": last_match.get("validation_score"),
        "model_confidence": last_match.get("model_confidence"),
        "source_branch": last_match.get("source_branch"),
        "run_id": last_match.get("run_id"),
    }


def write_batch_summary(bundle_dir: Path, started_iso: str, results: list[dict[str, Any]]) -> Path:
    """Écrit un récapitulatif Markdown dans runs/batch_summary_{TS}.md."""
    out_path = bundle_dir / "runs" / f"batch_summary_{started_iso}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Batch run summary — {started_iso}",
        "",
        f"Tickets traités : **{len(results)}**",
        "",
    ]

    # Comptage par statut
    status_counts: dict[str, int] = {}
    for r in results:
        s = str(r.get("delivery_status") or "UNKNOWN")
        status_counts[s] = status_counts.get(s, 0) + 1
    if status_counts:
        lines.append("## Répartition par statut")
        lines.append("")
        for status, count in sorted(status_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- **{status}** : {count}")
        lines.append("")

    # Tableau détaillé
    lines.append("## Détail par ticket")
    lines.append("")
    lines.append("| Ticket | Statut | MR | Confiance | Durée | Motif si bloqué |")
    lines.append("|--------|--------|----|-----------:|------:|------------------|")
    for r in results:
        ticket = str(r.get("ticket_key") or "?")
        status = str(r.get("delivery_status") or "?")
        mr = r.get("pr_url") or "-"
        if mr and mr.startswith("http"):
            mr = f"[lien]({mr})"
        confidence = r.get("model_confidence") or "-"
        elapsed = r.get("elapsed_seconds")
        elapsed_str = f"{int(elapsed // 60)}min{int(elapsed % 60):02d}s" if elapsed else "-"
        block = (r.get("mr_blocked_reason") or "").replace("|", "/")[:80]
        lines.append(f"| {ticket} | {status} | {mr} | {confidence} | {elapsed_str} | {block} |")
    lines.append("")

    # Section échecs si pertinent
    failures = [r for r in results if r.get("python_returncode", 0) != 0]
    if failures:
        lines.append("## Plantages Python ou timeouts")
        lines.append("")
        for r in failures:
            lines.append(f"### {r.get('ticket_key')}")
            lines.append("```")
            lines.append(str(r.get("error_tail") or "(no stderr captured)"))
            lines.append("```")
            lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch execution of tickets through the autofix-agent pipeline."
    )
    parser.add_argument("--repo", required=True, help="Chemin du dépôt Git cible.")
    parser.add_argument(
        "--analysis-dir",
        default=None,
        help="Répertoire des fichiers d'analyse (défaut: <bundle>/analysis).",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        help="Branche de base à laquelle ramener le repo entre chaque ticket (défaut: main).",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Ne pas ramener le repo à origin/<base_branch> entre chaque ticket. "
             "Si activé, c'est à toi de garantir un repo propre.",
    )
    parser.add_argument(
        "--stop-on-python-error",
        action="store_true",
        help="Arrêter le batch dès qu'un run plante avec un returncode != 0. "
             "Par défaut, le batch continue même en cas de MR_BLOCKED ou plantage.",
    )
    args = parser.parse_args()

    bundle_dir = Path(__file__).resolve().parent.parent
    repo_path = Path(args.repo).resolve()
    analysis_dir = Path(args.analysis_dir).resolve() if args.analysis_dir else bundle_dir / "analysis"

    if not repo_path.is_dir():
        print(f"[BATCH] ERROR: repo not found: {repo_path}", file=sys.stderr)
        return 2

    files = discover_analysis_files(analysis_dir)
    if not files:
        print(f"[BATCH] ERROR: no analysis files found in {analysis_dir}", file=sys.stderr)
        return 2

    log = print
    started_iso = datetime.now().strftime("%Y%m%d-%H%M%S")
    log(f"[BATCH] start at {started_iso}")
    log(f"[BATCH] bundle={bundle_dir}")
    log(f"[BATCH] repo={repo_path}")
    log(f"[BATCH] {len(files)} ticket(s) to process:")
    for path, ticket in files:
        log(f"  - {ticket} ← {path.name}")

    results: list[dict[str, Any]] = []
    for i, (path, ticket) in enumerate(files, 1):
        log("")
        log(f"[BATCH] [{i}/{len(files)}] processing {ticket}")

        # Reset repo entre chaque ticket
        if not args.no_reset:
            ok = reset_repo_to_base(repo_path, args.base_branch, log)
            if not ok:
                log(f"[BATCH] {ticket} skipped: repo reset failed")
                results.append({
                    "ticket_key": ticket,
                    "analysis_file": path.name,
                    "delivery_status": "REPO_RESET_FAILED",
                    "elapsed_seconds": 0,
                    "python_returncode": -1,
                })
                continue

        outcome = run_one_ticket(bundle_dir, repo_path, path, ticket, log)
        results.append(outcome)

        if args.stop_on_python_error and outcome.get("python_returncode", 0) != 0:
            log(f"[BATCH] stop_on_python_error: aborting after {ticket}")
            break

    summary_path = write_batch_summary(bundle_dir, started_iso, results)
    log("")
    log(f"[BATCH] summary written to {summary_path}")
    log(f"[BATCH] processed {len(results)} ticket(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
