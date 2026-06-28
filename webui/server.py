"""
IHM backend pour autofix-agent (squelette minimal).

Lance scripts/check_env.py et scripts/batch_run.py en subprocess et streame
leurs logs en direct vers le navigateur via WebSocket.

Lancement :
    pip install fastapi uvicorn
    python -m uvicorn webui.server:app --host 127.0.0.1 --port 8420
    (depuis la racine du bundle)

Puis ouvrir http://127.0.0.1:8420 dans le navigateur.

Note : ce serveur n'expose AUCUN secret et ne fait qu'orchestrer les deux
scripts existants. Il reproduit le chargement de config.env que faisait
_load_config.bat, pour que batch_run.py voie FIX_TARGET_REPO et les autres
reglages.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# --- Localisation du bundle -------------------------------------------------
# server.py vit dans <bundle>/webui/server.py -> le bundle est le parent.
BUNDLE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BUNDLE_DIR / "scripts"
WEBUI_DIR = BUNDLE_DIR / "webui"

app = FastAPI(title="autofix-agent UI")


# --- Chargement de config.env (equivalent de _load_config.bat) --------------
def find_config_file() -> Path | None:
    """Cherche config.env aux deux emplacements connus du bundle.

    Priorite identique a _load_config.bat :
      1. <bundle>/../templates_external_config/config.env  (externe partage)
      2. <bundle>/templates_external_config/config.env     (interne, defaut)
    """
    candidates = [
        BUNDLE_DIR.parent / "templates_external_config" / "config.env",
        BUNDLE_DIR / "templates_external_config" / "config.env",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _find_env_file(name: str) -> Path | None:
    """Variante generique de find_config_file pour secrets.env, etc."""
    for parent in (BUNDLE_DIR.parent, BUNDLE_DIR):
        candidate = parent / "templates_external_config" / name
        if candidate.is_file():
            return candidate
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def load_config_env() -> dict[str, str]:
    """Parse config.env + secrets.env en dict (config en premier, secrets ecrasent
    si meme cle, c'est volontaire). Ne touche PAS a os.environ ici ;
    on fusionnera au moment de lancer le subprocess (priorite au shell).
    """
    cfg: dict[str, str] = {}
    for env_name in ("config.env", "secrets.env"):
        path = _find_env_file(env_name)
        if path:
            cfg.update(_parse_env_file(path))
    return cfg


def build_subprocess_env() -> dict[str, str]:
    """Env du subprocess = os.environ + config.env (le shell gagne, comme
    setIfUndefined dans _load_config.bat). Force aussi l'UTF-8 cote enfant.
    """
    env = dict(os.environ)
    for key, value in load_config_env().items():
        env.setdefault(key, value)  # ne PAS ecraser le shell
    # Coherence avec main.py (_force_utf8_stdout) : on veut de l'UTF-8 propre
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # IMPORTANT : sans ca, batch_run.py bufferise tous ses print() dans le pipe
    # et l'IHM reste figee a "En cours..." pendant des minutes/heures.
    # Equivalent du flag -u, force le flush ligne par ligne.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def resolve_repo_path() -> str:
    """Le repo cible vient de FIX_TARGET_REPO (config.env ou shell)."""
    env = build_subprocess_env()
    return env.get("FIX_TARGET_REPO", "").strip()


# --- Streaming d'un subprocess vers le WebSocket ----------------------------
async def stream_command(ws: WebSocket, cmd: list[str], label: str) -> None:
    """Lance `cmd` et envoie chaque ligne de sortie au client au fil de l'eau.
    stdout et stderr sont fusionnes pour garder l'ordre chronologique des logs.
    """
    await ws.send_json({"type": "start", "label": label, "cmd": cmd})

    env = build_subprocess_env()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BUNDLE_DIR),
            stdin=asyncio.subprocess.DEVNULL,  # evite tout hang sur input()
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # fusion stderr->stdout
            env=env,
        )
    except FileNotFoundError as exc:
        await ws.send_json({"type": "error", "line": f"Commande introuvable: {exc}"})
        await ws.send_json({"type": "end", "label": label, "returncode": -1})
        return

    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        await ws.send_json({"type": "log", "line": line})

    returncode = await proc.wait()
    await ws.send_json({"type": "end", "label": label, "returncode": returncode})


# --- Endpoints --------------------------------------------------------------
@app.get("/api/status")
async def status() -> dict:
    """Petit endpoint de sante : montre ce que le backend a resolu."""
    cfg_file = find_config_file()
    return {
        "bundle_dir": str(BUNDLE_DIR),
        "config_file": str(cfg_file) if cfg_file else None,
        "repo_path": resolve_repo_path() or None,
        "python": sys.executable,
    }


def _ping_ollama(base_url: str, timeout: float = 2.0) -> dict:
    """GET <base_url>/api/tags pour verifier qu'Ollama tourne. Aucune dependance externe."""
    url = base_url.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        models = [m.get("name", "") for m in (data.get("models") or [])]
        return {"ok": True, "url": base_url, "models": models[:8]}
    except Exception as exc:
        return {"ok": False, "url": base_url, "error": str(exc)[:120]}


def _git_version() -> dict:
    """Verifie que git est dans le PATH."""
    git = shutil.which("git")
    if not git:
        return {"ok": False, "error": "git introuvable dans le PATH"}
    try:
        out = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=3)
        return {"ok": out.returncode == 0, "version": out.stdout.strip()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}


def _count_analysis_files() -> int:
    """Compte les fichiers .txt dans <bundle>/analysis/."""
    analysis_dir = BUNDLE_DIR / "analysis"
    if not analysis_dir.is_dir():
        return 0
    return sum(1 for p in analysis_dir.glob("*.txt") if p.is_file())


@app.get("/api/health")
async def health() -> dict:
    """Verifications LIVE du systeme (Ollama, Git, repo, analyses).
    Pratique pour le dashboard : on voit immediatement ce qui ne va pas
    sans devoir lancer check_env.py.
    """
    env = build_subprocess_env()
    repo = env.get("FIX_TARGET_REPO", "").strip()
    ollama_url = env.get("OLLAMA_BASE_URL", "http://localhost:11434").strip()
    ollama_model = env.get("OLLAMA_MODEL", "").strip()
    jira_configured = all(env.get(k) for k in ("JIRA_BASE_URL", "JIRA_TOKEN", "JIRA_USER"))
    forge_configured = bool(env.get("FORGE_TOKEN"))
    rag_enabled = (env.get("RAG_ENABLED", "false").strip().lower() == "true")

    # Checks paralleles (executes a la suite mais bornes en temps)
    loop = asyncio.get_event_loop()
    ollama = await loop.run_in_executor(None, _ping_ollama, ollama_url)
    git = await loop.run_in_executor(None, _git_version)

    return {
        "repo": {
            "ok": bool(repo) and Path(repo).is_dir(),
            "path": repo or None,
            "exists": Path(repo).is_dir() if repo else False,
        },
        "config": {
            "ok": find_config_file() is not None,
            "path": str(find_config_file()) if find_config_file() else None,
        },
        "ollama": {**ollama, "model": ollama_model or None},
        "git": git,
        "analysis": {
            "ok": _count_analysis_files() > 0,
            "count": _count_analysis_files(),
            "dir": str(BUNDLE_DIR / "analysis"),
        },
        "jira": {"configured": jira_configured},
        "forge": {"configured": forge_configured},
        "rag": {"enabled": rag_enabled},
    }


# --- Historique des runs (source de verite : runs/history.jsonl) ------------
RUNS_DIR = BUNDLE_DIR / "runs"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


@app.get("/api/history")
async def history(limit: int = 100) -> dict:
    """Lit runs/history.jsonl et retourne les derniers runs, du plus recent au
    plus ancien. Deduplique par (ticket_key, run_id) en gardant la DERNIERE
    entree (le bundle ecrit STARTED puis le statut final pour un meme run).
    C'est la source de verite pour le score, la confiance et l'URL de la MR —
    ces infos ne passent jamais par stdout du batch (subprocess capture).
    """
    path = RUNS_DIR / "history.jsonl"
    if not path.is_file():
        return {"entries": []}
    dedup: dict[tuple, dict] = {}
    order: list[tuple] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (entry.get("ticket_key"), entry.get("run_id"))
            if key not in dedup:
                order.append(key)
            dedup[key] = entry  # la derniere occurrence ecrase STARTED
    except OSError:
        return {"entries": []}
    entries = [dedup[k] for k in order][-max(1, min(limit, 500)):]
    entries.reverse()  # plus recent en premier
    # On ne renvoie que les champs utiles a l'IHM
    slim = [
        {
            "ticket_key": e.get("ticket_key"),
            "run_id": e.get("run_id"),
            "delivery_status": e.get("delivery_status"),
            "pr_url": e.get("pr_url"),
            "validation_score": e.get("validation_score"),
            "model_confidence": e.get("model_confidence"),
            "source_branch": e.get("source_branch"),
            "mr_blocked_reason": e.get("mr_blocked_reason"),
        }
        for e in entries
    ]
    return {"entries": slim}


def _read_artifact_json(run_dir: Path, name: str):
    p = run_dir / name
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


@app.get("/api/run_detail")
async def run_detail(ticket: str, run_id: str) -> dict:
    """Detail d'un run pour l'onglet Pre-MR analysis : diagnostic, conclusion
    de l'agent, patch, trace d'exploration. Lit les artefacts JSON ecrits par
    RunContext dans runs/<TICKET>/<run_id>/.
    """
    # Anti path-traversal : segments stricts
    if not (_SAFE_SEGMENT.match(ticket or "") and _SAFE_SEGMENT.match(run_id or "")):
        return {"error": "invalid ticket or run_id"}
    run_dir = RUNS_DIR / ticket / run_id
    if not run_dir.is_dir():
        return {"error": f"run not found: {ticket}/{run_id}"}

    contract = _read_artifact_json(run_dir, "correction_contract.json") or {}
    plan = _read_artifact_json(run_dir, "plan.json") or {}
    agent_result = _read_artifact_json(run_dir, "agent_result.json") or {}
    trace = _read_artifact_json(run_dir, "exploration_trace.json") or {}
    localization = _read_artifact_json(run_dir, "localization.json") or {}

    # Trace d'exploration : nb de tours + outils utilises (best-effort sur la structure)
    turns = trace.get("turns") if isinstance(trace.get("turns"), list) else []
    tools_used = []
    for t in turns:
        if isinstance(t, dict):
            tool = t.get("tool") or t.get("tool_name") or (t.get("action") or {}).get("tool")
            if tool:
                tools_used.append(str(tool))

    # Fichiers touches par le plan
    edits = plan.get("edits") if isinstance(plan.get("edits"), list) else []
    edited_files = []
    for e in edits:
        if isinstance(e, dict):
            f = e.get("file") or e.get("path") or e.get("target_file")
            if f and f not in edited_files:
                edited_files.append(str(f))

    # Dernieres lignes du run.log
    log_tail = []
    log_path = run_dir / "run.log"
    if log_path.is_file():
        try:
            log_tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        except OSError:
            pass

    return {
        "ticket": ticket,
        "run_id": run_id,
        "diagnosis": {
            "immediate_cause": contract.get("immediate_cause"),
            "root_cause_hypothesis": contract.get("root_cause_hypothesis"),
            "exception_class": contract.get("exception_class"),
            "fix_style": contract.get("fix_style"),
        },
        "localization": {
            "target_file": localization.get("target_file")
                or ((contract.get("must_touch") or [None])[0]),
            "target_method": localization.get("target_method") or contract.get("must_touch_method"),
        },
        "conclusion": {
            "fix_summary": plan.get("fix_summary") or agent_result.get("fix_summary"),
            "root_cause": plan.get("root_cause") or agent_result.get("root_cause"),
            "validation_score": plan.get("validation_score") or agent_result.get("validation_score"),
            "agent_explored": bool(plan.get("agent_explored")),
            "llm_libre_mode": bool(plan.get("llm_libre_mode")),
            "rejection_reason": agent_result.get("rejection_reason"),
            "edited_files": edited_files,
            "edit_count": len(edits),
        },
        "exploration": {
            "attempted": bool(trace),
            "turn_count": len(turns),
            "tools_used": tools_used[:30],
        },
        "log_tail": log_tail,
        "artifacts_present": sorted(p.name for p in run_dir.iterdir() if p.is_file())[:30],
    }


@app.websocket("/ws/run")
async def ws_run(ws: WebSocket) -> None:
    """Canal unique : le client envoie {"action": "check_env"|"batch_run", ...},
    le serveur lance le script correspondant et streame les logs.
    """
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            try:
                payload = json.loads(msg)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "line": "message JSON invalide"})
                continue

            action = payload.get("action")
            repo = resolve_repo_path()

            if not repo:
                await ws.send_json({
                    "type": "error",
                    "line": "FIX_TARGET_REPO introuvable dans config.env. "
                            "Verifie templates_external_config/config.env.",
                })
                await ws.send_json({"type": "end", "label": action or "?", "returncode": -1})
                continue

            if action == "check_env":
                cmd = [
                    sys.executable,
                    str(SCRIPTS_DIR / "check_env.py"),
                    "--bundle-dir", str(BUNDLE_DIR),
                    "--repo-dir", repo,
                ]
                await stream_command(ws, cmd, label="check_env")

            elif action == "batch_run":
                cmd = [sys.executable, "-m", "scripts.batch_run", "--repo", repo]
                base_branch = (payload.get("base_branch") or "").strip()
                if base_branch:
                    cmd += ["--base-branch", base_branch]
                if payload.get("no_reset"):
                    cmd += ["--no-reset"]
                if payload.get("stop_on_python_error"):
                    cmd += ["--stop-on-python-error"]
                await stream_command(ws, cmd, label="batch_run")

            else:
                await ws.send_json({"type": "error", "line": f"action inconnue: {action}"})
    except WebSocketDisconnect:
        return


# --- Sert l'IHM (un seul fichier HTML autonome) -----------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    html_path = WEBUI_DIR / "index.html"
    if html_path.is_file():
        return html_path.read_text(encoding="utf-8")
    return "<h1>index.html introuvable dans webui/</h1>"
