"""app.config.external_config — Chargeur de configuration externe v9.15.

v9.15 : centralisation de la configuration. Au lieu de 5 fichiers disperses
(.env, .env.example, application.properties, .secrets.env, .secrets.env.example),
la configuration tient desormais dans 2 fichiers, dans un dossier dedie :

  templates_external_config/
    |-- config.env      <- TOUTE la config non-sensible (chemins, modele, toggles)
    `-- secrets.env     <- UNIQUEMENT les secrets (tokens)

Priorite de chargement (du plus prioritaire au moins) :
  1. Variables d'environnement deja definies dans le shell
  2. templates_external_config/secrets.env
  3. templates_external_config/config.env
  4. Fallback historique : bundle/secrets/.secrets.env, bundle/.env,
     bundle/application.properties (compatibilite descendante v9.0-v9.14)
"""
from __future__ import annotations

import os
from pathlib import Path

# Variables que le bundle considere comme essentielles a afficher au demarrage.
TRACKED_VARS = [
    "OLLAMA_MODEL",
    "OLLAMA_BASE_URL",
    "FORGE_TOKEN",
    "FORGE_PROJECT_ID",
    "FORGE_BASE_URL",
    "FORGE_API_URL",
    "FIX_TARGET_REPO",
    "JIRA_BASE_URL",
    "JIRA_USER",
    "JIRA_TOKEN",
    "CONFLUENCE_BASE_URL",
    "CONFLUENCE_SPACE_KEYS",
]


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple d'un fichier .env: KEY=VALUE par ligne, # commentaires."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key:
                out[key] = value
    except OSError:
        pass
    return out


def _find_config_dir(bundle_root: Path) -> Path | None:
    """Cherche le dossier templates_external_config/.

    v9.15 : cherche a 2 emplacements, dans l'ordre :
      1. DANS le bundle : bundle_root/templates_external_config/
         (emplacement par defaut, livre avec le bundle)
      2. A COTE du bundle : bundle_root.parent/templates_external_config/
         (config partagee entre plusieurs versions du bundle)

    L'emplacement 2 (externe) est prioritaire s'il existe, car il permet de
    partager la meme config entre versions du bundle sans recopier.
    """
    external = bundle_root.parent / "templates_external_config"
    if external.is_dir():
        return external
    internal = bundle_root / "templates_external_config"
    if internal.is_dir():
        return internal
    return None


def load_external_config(bundle_root: Path, log=print) -> dict[str, str]:
    """Charge la configuration v9.15 avec cascade et retourne les sources."""
    sources: dict[str, str] = {}

    # 1. Variables deja dans le shell : priorite absolue
    for v in TRACKED_VARS:
        if os.environ.get(v):
            sources[v] = "shell"

    # 2. + 3. Dossier templates_external_config/
    config_dir = _find_config_dir(bundle_root)
    if config_dir:
        log(f"[CONFIG] config directory: {config_dir}")

        # 3. config.env (non-sensible)
        config_env_path = config_dir / "config.env"
        if config_env_path.is_file():
            for k, v in _parse_env_file(config_env_path).items():
                if k not in os.environ:
                    os.environ[k] = v
                    if k in TRACKED_VARS:
                        sources[k] = "config.env"
            log(f"[CONFIG] loaded: {config_env_path}")
        else:
            log(f"[CONFIG] WARNING: config.env not found in {config_dir}")

        # 2. secrets.env (sensible) - charge apres config.env mais ne l'ecrase pas
        secrets_env_path = config_dir / "secrets.env"
        if secrets_env_path.is_file():
            for k, v in _parse_env_file(secrets_env_path).items():
                if k not in os.environ:
                    os.environ[k] = v
                    if k in TRACKED_VARS:
                        sources[k] = "secrets.env"
            log(f"[CONFIG] loaded: {secrets_env_path}")
        else:
            log(f"[CONFIG] WARNING: secrets.env not found in {config_dir}")
    else:
        log("[CONFIG] no templates_external_config/ folder found "
            f"(searched in {bundle_root} and {bundle_root.parent})")

    # 4. Fallbacks historiques (compatibilite descendante v9.0-v9.14)
    _load_legacy_fallbacks(bundle_root, sources, log)

    # 5. Marquer les variables encore manquantes
    for v in TRACKED_VARS:
        if v not in sources and not os.environ.get(v):
            sources[v] = "missing"

    return sources


def _load_legacy_fallbacks(bundle_root: Path, sources: dict[str, str], log) -> None:
    """Charge les anciens fichiers de config pour compatibilite descendante."""
    legacy_files = [
        (bundle_root / "secrets" / ".secrets.env", "legacy.secrets_env"),
        (bundle_root / ".env", "legacy.env"),
    ]
    for path, source_label in legacy_files:
        if path.is_file():
            for k, v in _parse_env_file(path).items():
                if k not in os.environ:
                    os.environ[k] = v
                    if k in TRACKED_VARS:
                        sources[k] = source_label
            log(f"[CONFIG] loaded legacy fallback: {path}")


def log_config_summary(sources: dict[str, str], log=print) -> None:
    """Affiche un resume clair de la cascade pour les vars critiques."""
    log("[CONFIG] === resume de la cascade de configuration ===")
    by_source: dict[str, list[str]] = {}
    for var, src in sources.items():
        by_source.setdefault(src, []).append(var)
    display_order = [
        ("shell", "shell env"),
        ("config.env", "templates_external_config/config.env"),
        ("secrets.env", "templates_external_config/secrets.env"),
        ("legacy.secrets_env", "legacy bundle/secrets/.secrets.env"),
        ("legacy.env", "legacy bundle/.env"),
        ("missing", "MISSING"),
    ]
    for src_key, label in display_order:
        if src_key in by_source:
            vars_list = sorted(by_source[src_key])
            log(f"[CONFIG]   from {label}: {', '.join(vars_list)}")
    log("[CONFIG] ================================================")


def bootstrap_external_config(bundle_root: Path, verbose: bool = True) -> dict[str, str]:
    """Point d'entree principal: charge la cascade et log le resume."""
    def _log(msg):
        if verbose:
            print(msg)
    sources = load_external_config(bundle_root, log=_log)
    if verbose:
        log_config_summary(sources, log=_log)
    return sources
