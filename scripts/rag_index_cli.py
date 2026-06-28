"""scripts.rag_index_cli — CLI pour construire ou rafraichir l'index RAG.

Lance depuis scripts/build_rag_index.bat ou scripts/refresh_rag_index.bat.

Usage:
    python -m scripts.rag_index_cli --mode build
    python -m scripts.rag_index_cli --mode refresh

Le mode 'build' fait une indexation complete (peut prendre 1-3h).
Le mode 'refresh' ne traite que les nouveaux commits depuis le manifest.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

# Reconfigurer stdout en UTF-8 (idem main.py v9.7)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or refresh the RAG index.")
    parser.add_argument(
        "--mode",
        choices=("build", "refresh"),
        default="build",
        help="'build' = full reindex, 'refresh' = incremental update (default: build)",
    )
    args = parser.parse_args()

    from app.config.settings import settings, ROOT_DIR

    if not settings.rag.git_repos.strip():
        print("[ERROR] rag.git_repos est vide dans application.properties.")
        print("        Configure les chemins de tes repos Git avant de relancer.")
        print("        Format : path1|tag;path2|tag;...")
        print("        Exemple :")
        print("          rag.git_repos=C:/.../myapp-back|back;C:/.../myapp-front|front")
        return 2

    git_repos = settings.rag.parse_repos()
    print(f"[CLI] {len(git_repos)} repos a indexer :")
    for r in git_repos:
        path = Path(r["path"])
        ok = "OK" if path.is_dir() else "ABSENT"
        print(f"  - {r.get('tag', '?'):<8} {r['path']}  [{ok}]")

    # Verifier acces Jira/Confluence (optionnels mais recommandes)
    jira_client = None
    confluence_client = None
    confluence_spaces = None

    jira_url = os.getenv("JIRA_BASE_URL", "").strip()
    jira_token = os.getenv("JIRA_TOKEN", "").strip()
    jira_user = os.getenv("JIRA_USER", "").strip()
    jira_auth = os.getenv("JIRA_AUTH_TYPE", "basic").strip()

    if jira_url and jira_token:
        try:
            from app.rag.jira_client import JiraClient
            jira_client = JiraClient(
                base_url=jira_url,
                user=jira_user,
                token=jira_token,
                auth_type=jira_auth,
            )
            print(f"[CLI] Jira client OK ({jira_url}, auth={jira_auth})")
        except Exception as exc:
            print(f"[CLI] WARN: Jira disabled (config error): {exc}")
    else:
        print("[CLI] WARN: JIRA_BASE_URL ou JIRA_TOKEN manquant. Indexation sans enrichissement Jira.")
        print("       Les episodes ne contiendront que la donnee Git brute.")

    conf_url = os.getenv("CONFLUENCE_BASE_URL", "").strip()
    conf_user = os.getenv("CONFLUENCE_USER", "").strip() or jira_user
    conf_token = os.getenv("CONFLUENCE_TOKEN", "").strip() or jira_token
    conf_spaces_raw = os.getenv("CONFLUENCE_SPACE_KEYS", "").strip()
    confluence_spaces = [s.strip() for s in conf_spaces_raw.split(",") if s.strip()]

    if conf_url and conf_token:
        try:
            from app.rag.confluence_client import ConfluenceClient
            confluence_client = ConfluenceClient(
                base_url=conf_url,
                user=conf_user,
                token=conf_token,
                auth_type=jira_auth,
            )
            print(f"[CLI] Confluence client OK ({conf_url}, spaces={confluence_spaces})")
        except Exception as exc:
            print(f"[CLI] WARN: Confluence disabled (config error): {exc}")
    else:
        print("[CLI] INFO: Confluence non configure. Les RG ne seront pas indexees.")

    # Charger le modele d'embedding (verification precoce)
    print(f"[CLI] embedding model : {settings.rag.embedding_model}")
    print(f"[CLI] (premier chargement = telechargement HuggingFace si pas en cache, ~120 Mo)")

    rag_root = Path(settings.rag.index_path)
    if not rag_root.is_absolute():
        rag_root = ROOT_DIR / rag_root

    if args.mode == "build":
        print(f"[CLI] MODE BUILD - indexation complete dans {rag_root}")
        from app.rag.builder import build_full_index
        manifest = build_full_index(
            git_repos=git_repos,
            rag_root=rag_root,
            embedding_model=settings.rag.embedding_model,
            jira_client=jira_client,
            confluence_client=confluence_client,
            confluence_spaces=confluence_spaces,
            since=settings.rag.since,
            log=print,
        )
        print(f"[CLI] DONE. {manifest.get('episodes_count', 0)} episodes indexes.")
        print(f"[CLI] Manifest : {rag_root / 'manifest.json'}")
        print()
        print("[CLI] Prochaines etapes :")
        print("  1. Editer application.properties et passer rag.enabled=true")
        print("  2. Relancer un ticket pour verifier que le RAG est consulte")
        print("     (chercher 'retrieved N similar episodes' dans le run.log)")
        return 0

    # Mode refresh
    print(f"[CLI] MODE REFRESH - mise a jour incrementale depuis {rag_root}")
    from app.rag.indexer import load_manifest
    manifest = load_manifest(rag_root / "manifest.json")
    if not manifest:
        print("[ERROR] Aucun manifest trouve. Lance d'abord 'build_rag_index.bat'.")
        return 2

    # Pour cette version, refresh = rebuild a partir du since du manifest.
    # ChromaDB upsert gere l'idempotence, donc les episodes existants ne
    # sont pas duppliques, juste mis a jour si modifies.
    print("[CLI] refresh = re-crawl + upsert dans l'index existant")
    print("[CLI] (ChromaDB upsert gere la deduplication)")
    from app.rag.builder import build_full_index
    new_manifest = build_full_index(
        git_repos=git_repos,
        rag_root=rag_root,
        embedding_model=settings.rag.embedding_model,
        jira_client=jira_client,
        confluence_client=confluence_client,
        confluence_spaces=confluence_spaces,
        since=settings.rag.since,
        log=print,
    )
    print(f"[CLI] DONE. {new_manifest.get('episodes_count', 0)} episodes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
