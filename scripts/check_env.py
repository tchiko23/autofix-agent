"""check_env.py — Validation de l'environnement avant un run.

Implémentation Python robuste qui remplace l'ancien check_env.bat.
Le wrapper scripts/check_env.bat appelle ce script.

Vérifications effectuées :
  1. Python (version)
  2. Git accessible (commande dans le PATH)
  3. Repo cible existe et est un dépôt Git valide
  4. Fichiers d'analyse présents (mode mono-ticket OU mode batch)
  5. Secrets GitLab (fichier secrets/.secrets.env contient FORGE_TOKEN)
  6. Ollama joignable sur localhost:11434 (warning si absent, non bloquant)
  7. Imports Python du bundle (correction_contract_service, ticket_utils)

Sortie :
  - 0 si tout va bien
  - 1 si au moins une vérification critique a échoué (warnings ne comptent pas)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def use_color() -> bool:
    """Activer la couleur seulement si le terminal le supporte.
    Sous Windows, cmd.exe historique ne gère pas ANSI sans configuration ;
    on désactive sauf si TERM ou WT_SESSION suggère un terminal moderne.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        # Windows Terminal ou ConEmu posent ces variables
        return bool(os.environ.get("WT_SESSION") or os.environ.get("ANSICON"))
    return sys.stdout.isatty()


_USE_COLOR = use_color()


def _ok(msg: str) -> str:
    return f"{GREEN}[OK]{RESET} {msg}" if _USE_COLOR else f"[OK] {msg}"


def _err(msg: str) -> str:
    return f"{RED}[ERROR]{RESET} {msg}" if _USE_COLOR else f"[ERROR] {msg}"


def _warn(msg: str) -> str:
    return f"{YELLOW}[WARN]{RESET} {msg}" if _USE_COLOR else f"[WARN] {msg}"


def check_python() -> tuple[bool, str]:
    return True, f"Python {sys.version.split()[0]}"


def check_git() -> tuple[bool, str]:
    git = shutil.which("git")
    if not git:
        return False, "git introuvable dans le PATH"
    try:
        out = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0:
            return False, "git --version a echoue"
        return True, out.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"appel git impossible: {exc}"


def check_repo(repo_path: Path) -> tuple[bool, str]:
    if not repo_path.is_dir():
        return False, (
            f"repo cible introuvable : {repo_path}\n"
            f"          Conseil : cloner le repo a cote du bundle, "
            f"ou definir FIX_TARGET_REPO"
        )
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10
        )
        if out.returncode != 0 or out.stdout.strip() != "true":
            return False, f"{repo_path} n'est pas un depot Git"
        return True, f"{repo_path}"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"git rev-parse impossible: {exc}"


def check_analysis_files(analysis_dir: Path) -> tuple[bool, str]:
    if not analysis_dir.is_dir():
        return False, f"repertoire d'analyse introuvable : {analysis_dir}"

    mono = analysis_dir / "issue_analysis.txt"
    mono_ok = mono.is_file()

    batch_files = sorted(
        f for f in analysis_dir.glob("issue_analysis_*.txt")
        if f.name.lower() != "issue_analysis.template.txt"
    )

    if not mono_ok and not batch_files:
        return False, (
            f"aucun fichier d'analyse dans {analysis_dir}\n"
            f"          Conseil : deposer issue_analysis.txt (mono-ticket)\n"
            f"                    ou issue_analysis_TICKET-XXXXX.txt (mode batch)"
        )

    msgs = []
    if mono_ok:
        msgs.append("mode mono-ticket : issue_analysis.txt")
    if batch_files:
        listing = "\n".join(f"            - {f.name}" for f in batch_files)
        msgs.append(f"mode batch : {len(batch_files)} fichier(s)\n{listing}")
    return True, "\n          ".join(msgs)


def check_secrets(bundle_dir: Path) -> tuple[bool, str]:
    """v9.9: verifie que FORGE_TOKEN est defini quelque part dans la cascade :
    - shell env
    - ../.config/.secrets.env (externe partage)
    - bundle/secrets/.secrets.env (interne fallback)
    """
    import os
    import sys

    # On charge l'environnement comme le ferait le bundle au demarrage
    try:
        sys.path.insert(0, str(bundle_dir))
        from app.config.external_config import bootstrap_external_config
        # verbose=False pour ne pas dupliquer le log avec celui du run reel
        sources = bootstrap_external_config(bundle_dir, verbose=False)
    finally:
        if str(bundle_dir) in sys.path:
            sys.path.remove(str(bundle_dir))

    forge_token = os.environ.get("FORGE_TOKEN", "").strip()
    if not forge_token:
        return False, (
            "FORGE_TOKEN non defini dans la cascade\n"
            "          Definir FORGE_TOKEN dans (par ordre de priorite):\n"
            "          1. Variable d'env shell : set FORGE_TOKEN=xxx\n"
            "          2. ../.config/.secrets.env (externe partage, recommande)\n"
            "          3. bundle/secrets/.secrets.env (interne)\n"
            f"          Template: voir templates_external_config/secrets.env"
        )
    source = sources.get("FORGE_TOKEN", "unknown")
    return True, f"FORGE_TOKEN OK (source: {source})"


def check_ollama() -> tuple[bool, str]:
    """Renvoie (True, msg) si Ollama répond, (False, msg) sinon.
    Le caller traite ce check comme un warning, pas comme un échec critique."""
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                return True, "Ollama repond sur http://localhost:11434"
            return False, f"Ollama HTTP {resp.status}"
    except urllib.error.URLError as exc:
        return False, f"Ollama injoignable : {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return False, f"Ollama injoignable : {exc}"
    except Exception as exc:
        return False, f"erreur Ollama : {exc}"


def check_python_imports(bundle_dir: Path) -> tuple[bool, str]:
    """Vérifie que les modules clés du bundle s'importent."""
    sys.path.insert(0, str(bundle_dir))
    try:
        from app.utils.ticket_utils import extract_current_ticket_key  # noqa: F401
        from app.service.correction_contract_service import build_correction_contract  # noqa: F401
        return True, "imports app.* OK"
    except ImportError as exc:
        return False, f"import en echec : {exc}"
    finally:
        if str(bundle_dir) in sys.path:
            sys.path.remove(str(bundle_dir))


def main() -> int:
    parser = argparse.ArgumentParser(description="Vérification de l'environnement.")
    parser.add_argument("--bundle-dir", required=True, help="Chemin du bundle.")
    parser.add_argument("--repo-dir", required=True, help="Chemin du dépôt cible.")
    args = parser.parse_args()

    bundle_dir = Path(args.bundle_dir).resolve()
    repo_dir = Path(args.repo_dir).resolve()
    analysis_dir = bundle_dir / "analysis"
    runs_dir = bundle_dir / "runs"
    secrets_file = bundle_dir / "secrets" / ".secrets.env"

    runs_dir.mkdir(exist_ok=True)

    err_count = 0

    # 1. Python
    print("[CHECK] Python")
    ok, msg = check_python()
    print(f"  {_ok(msg) if ok else _err(msg)}")
    if not ok:
        err_count += 1

    # 2. Git
    print("[CHECK] Git")
    ok, msg = check_git()
    print(f"  {_ok(msg) if ok else _err(msg)}")
    if not ok:
        err_count += 1

    # 3. Repo cible
    print(f"[CHECK] Repo cible")
    ok, msg = check_repo(repo_dir)
    print(f"  {_ok(msg) if ok else _err(msg)}")
    if not ok:
        err_count += 1

    # 4. Fichiers d'analyse
    print("[CHECK] Fichiers d'analyse")
    ok, msg = check_analysis_files(analysis_dir)
    print(f"  {_ok(msg) if ok else _err(msg)}")
    if not ok:
        err_count += 1

    # 5. Secrets
    print("[CHECK] Secrets GitLab")
    ok, msg = check_secrets(bundle_dir)
    print(f"  {_ok(msg) if ok else _err(msg)}")
    if not ok:
        err_count += 1

    # 6. Ollama (warning, non bloquant)
    print("[CHECK] Ollama")
    ok, msg = check_ollama()
    if ok:
        print(f"  {_ok(msg)}")
    else:
        print(f"  {_warn(msg)}")
        print("          Conseil : verifier que 'ollama serve' tourne (non bloquant)")

    # 7. Imports Python
    print("[CHECK] Modules Python du bundle")
    ok, msg = check_python_imports(bundle_dir)
    print(f"  {_ok(msg) if ok else _err(msg)}")
    if not ok:
        err_count += 1

    # 8. v9.8: RAG (informatif, jamais bloquant)
    print("[CHECK] RAG (Retrieval-Augmented Generation)")
    try:
        sys.path.insert(0, str(bundle_dir))
        from app.config.settings import settings as _s
        rag_enabled = bool(_s.rag.enabled)
        rag_path = bundle_dir / _s.rag.index_path
        rag_path = rag_path.resolve()
        index_exists = rag_path.is_dir() and any(rag_path.iterdir())
        if rag_enabled and index_exists:
            print(f"  {_ok(f'RAG actif, index = {rag_path}')}")
        elif rag_enabled and not index_exists:
            print(f"  {_warn(f'rag.enabled=true mais index introuvable a {rag_path}')}")
            print("          Conseil : lancer scripts\\build_rag_index.bat pour creer l'index.")
        else:
            print(f"  {_warn('RAG desactive (rag.enabled=false). Le bundle se comporte comme v9.7.')}")
            if index_exists:
                print(f"          Index existant detecte a {rag_path}. Pour activer : rag.enabled=true.")
        # Verifier que chromadb et sentence-transformers sont installes
        try:
            import chromadb  # noqa
            import sentence_transformers  # noqa
            print(f"  {_ok('dependances RAG installees (chromadb + sentence-transformers)')}")
        except ImportError as ie:
            print(f"  {_warn(f'dependances RAG manquantes ({ie}). pip install -r requirements.txt')}")
    except Exception as exc:
        print(f"  {_warn(f'check RAG impossible : {exc}')}")
    finally:
        if str(bundle_dir) in sys.path:
            sys.path.remove(str(bundle_dir))

    # Résumé chemins
    print()
    print("=== Resume des chemins ===")
    print(f"BUNDLE_DIR : {bundle_dir}")
    print(f"REPO_DIR   : {repo_dir}")
    print(f"ANALYSIS   : {analysis_dir}")
    print(f"RUNS_DIR   : {runs_dir}")
    print()

    if err_count > 0:
        print(_err(f"{err_count} verification(s) critique(s) en echec"))
        return 1
    print(_ok("environnement valide"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
