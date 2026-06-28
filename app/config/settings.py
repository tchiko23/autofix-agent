from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.properties import read_properties

ROOT_DIR = Path(__file__).resolve().parents[2]
APP_PROPS = read_properties(ROOT_DIR / "application.properties")

# v9.9: chargement de la configuration externe en CASCADE avant tout autre
# acces a os.environ. Voir app/config/external_config.py pour le detail.
# Priorite (du plus prioritaire au moins) :
#   1. shell env
#   2. ../.config/config.env (externe partage, non-secret)
#   3. ../.config/.secrets.env (externe partage, secrets)
#   4. bundle/secrets/.secrets.env (interne au bundle, fallback historique)
#   5. bundle/.env (legacy fichier .env du bundle v9.0-v9.8)
#
# Cela permet:
# - Plusieurs versions du bundle de partager les memes secrets via ../.config/
# - Le modele Ollama d'etre choisi via OLLAMA_MODEL dans config.env (centralise)
# - Un fallback transparent sur les anciens fichiers internes
from app.config.external_config import bootstrap_external_config
_CONFIG_SOURCES = bootstrap_external_config(ROOT_DIR, verbose=True)


def _prop(name: str, default: str = "") -> str:
    return APP_PROPS.get(name, default)


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    return default


def _first(*, env_names: tuple[str, ...], prop_name: str | None = None, default: str = "") -> str:
    value = _first_env(*env_names, default="")
    if value:
        return value
    if prop_name:
        value = _prop(prop_name, "")
        if value:
            return value
    return default


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _normalize_gitlab_api_url(raw: str) -> str:
    """v9.16.4 : normalise l'URL de l'API GitLab.

    Tolere les erreurs de saisie frequentes dans config.env :
      - 'https://gitlab.x.com/'        -> 'https://gitlab.x.com/api/v4'
      - 'https://gitlab.x.com'         -> 'https://gitlab.x.com/api/v4'
      - 'https://gitlab.x.com/api/v4/' -> 'https://gitlab.x.com/api/v4'
      - 'https://gitlab.x.com/api/v4'  -> inchange

    Evite les URL cassees du type 'https://gitlab.x.com//projects/...'
    ou les appels sur la racine sans /api/v4 (cause du 404 observe).
    """
    if not raw:
        return raw
    url = raw.strip().rstrip("/")
    if not url:
        return raw
    # Si l'URL ne contient pas deja le segment /api/v<N>, on l'ajoute
    import re as _re
    if not _re.search(r"/api/v\d+$", url):
        url = url + "/api/v4"
    return url


def _to_csv_list(value: str | None) -> list[str]:
    """Parse a comma-separated string into a list of trimmed non-empty items.

    Used for config values like agent.application_package_prefixes
    where the user may declare 'com.example.,com.example.subpkg.' or leave empty.
    """
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _to_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _to_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(slots=True)
class OllamaSettings:
    base_url: str = field(default_factory=lambda: _first(env_names=("OLLAMA_BASE_URL",), prop_name="ollama.base_url", default="http://localhost:11434"))
    # v9.9 BLOC 3: aucun defaut hardcode. Le modele DOIT venir de la config :
    # 1. OLLAMA_MODEL dans le shell, OU
    # 2. OLLAMA_MODEL dans ../.config/config.env (cas recommande), OU
    # 3. ollama.model dans application.properties (fallback historique)
    # Si absolument rien n'est defini, on echoue tot avec un message clair
    # plutot que d'utiliser un modele potentiellement inexistant.
    model: str = field(default_factory=lambda: _first(env_names=("OLLAMA_MODEL",), prop_name="ollama.model", default=""))
    temperature: float = field(default_factory=lambda: _to_float(_first(env_names=("OLLAMA_TEMPERATURE",), prop_name="ollama.temperature", default="0"), 0.0))
    num_predict: int = field(default_factory=lambda: _to_int(_first(env_names=("OLLAMA_NUM_PREDICT",), prop_name="ollama.num_predict", default="4096"), 4096))
    disable_thinking: bool = field(default_factory=lambda: _to_bool(_first(env_names=("OLLAMA_DISABLE_THINKING",), prop_name="ollama.disable_thinking", default="true"), True))

    def __post_init__(self) -> None:
        # v9.9: garde-fou - si aucun modele n'est configure, on ne demarre pas
        # avec une chaine vide qui produirait des erreurs Ollama confuses.
        if not self.model or not self.model.strip():
            raise RuntimeError(
                "OLLAMA_MODEL non configure. Definis-le dans :\n"
                "  - ../.config/config.env (recommande, format: OLLAMA_MODEL=qwen3-coder:30b)\n"
                "  - ou dans application.properties (ollama.model=...)\n"
                "  - ou comme variable d'environnement shell"
            )


@dataclass(slots=True)
class ForgeSettings:
    provider: str = field(default_factory=lambda: _first(env_names=("FORGE_PROVIDER",), prop_name="forge.provider", default="gitlab").lower())
    api_url: str = field(default_factory=lambda: _normalize_gitlab_api_url(_first(env_names=("FORGE_API_URL", "GITLAB_API_URL"), prop_name="forge.api_url", default="https://gitlab.your-company.com/api/v4")))
    token: str = field(default_factory=lambda: _first(env_names=("FORGE_TOKEN", "GITLAB_TOKEN"), default=""))
    project_id: str = field(default_factory=lambda: _first(env_names=("FORGE_PROJECT_ID", "GITLAB_PROJECT_ID"), default=""))
    owner: str = field(default_factory=lambda: _first(env_names=("FORGE_OWNER",), default=""))
    repo: str = field(default_factory=lambda: _first(env_names=("FORGE_REPO",), default=""))
    assignee_username: str = field(default_factory=lambda: _first(env_names=("FORGE_ASSIGNEE_USERNAME",), prop_name="forge.assignee_username", default=""))
    reviewers: list[str] = field(default_factory=lambda: _to_list(_first(env_names=("FORGE_REVIEWERS",), prop_name="forge.reviewers", default="")))
    labels: list[str] = field(default_factory=lambda: _to_list(_first(env_names=("FORGE_LABELS",), prop_name="forge.labels", default="BUG,RUN")))
    short_description: str = field(default_factory=lambda: _first(env_names=("FORGE_SHORT_DESCRIPTION",), prop_name="forge.short_description", default=""))
    description_append: str = field(default_factory=lambda: _first(env_names=("FORGE_DESCRIPTION_APPEND",), prop_name="forge.description_append", default="Merge request générée automatiquement à partir du fichier d'analyse."))
    draft: bool = field(default_factory=lambda: _to_bool(_first(env_names=("FORGE_DRAFT",), prop_name="forge.draft", default="true"), True))
    always_ship_diagnostic_mr: bool = field(default_factory=lambda: _to_bool(_first(env_names=("FORGE_ALWAYS_SHIP_DIAGNOSTIC_MR",), prop_name="forge.always_ship_diagnostic_mr", default="true"), True))


@dataclass(slots=True)
class BranchSettings:
    base_branch: str = field(default_factory=lambda: _first(env_names=("DEFAULT_BASE_BRANCH",), prop_name="default.base_branch", default="main"))
    target_branch: str = field(default_factory=lambda: _first(env_names=("DEFAULT_TARGET_BRANCH",), prop_name="default.target_branch", default="main"))
    source_branch_prefix: str = field(default_factory=lambda: _first(env_names=("DEFAULT_SOURCE_BRANCH_PREFIX",), prop_name="default.source_branch_prefix", default="hotfix/"))
    source_branch_mode: str = field(default_factory=lambda: _first(env_names=("DEFAULT_SOURCE_BRANCH_MODE",), prop_name="default.source_branch_mode", default="idempotent").lower())


@dataclass(slots=True)
class AgentSettings:
    max_files: int = field(default_factory=lambda: _to_int(_first(env_names=("AGENT_MAX_FILES",), prop_name="agent.max_files", default="6"), 6))
    max_support_files: int = field(default_factory=lambda: _to_int(_first(env_names=("AGENT_MAX_SUPPORT_FILES",), prop_name="agent.max_support_files", default="3"), 3))
    max_file_chars: int = field(default_factory=lambda: _to_int(_first(env_names=("AGENT_MAX_FILE_CHARS",), prop_name="agent.max_file_chars", default="10000"), 10000))
    workdir_name: str = field(default_factory=lambda: _first(env_names=("AGENT_WORKDIR",), prop_name="agent.workdir", default=".work"))
    artifacts_root_name: str = field(default_factory=lambda: _first(env_names=("AGENT_ARTIFACTS_ROOT",), prop_name="agent.artifacts_root", default="runs"))
    artifacts_root_override: str = field(default_factory=lambda: _first(env_names=("FIX_ARTIFACTS_ROOT",), prop_name="agent.artifacts_root_override", default=""))
    enable_timing_logs: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_ENABLE_TIMING_LOGS",), prop_name="agent.enable_timing_logs", default="true"), True))
    require_clean_repo: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_REQUIRE_CLEAN_REPO",), prop_name="agent.require_clean_repo", default="true"), True))
    auto_restore_work_artifacts: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_AUTO_RESTORE_WORK_ARTIFACTS",), prop_name="agent.auto_restore_work_artifacts", default="true"), True))
    require_plan_file_overlap: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_REQUIRE_PLAN_FILE_OVERLAP",), prop_name="agent.require_plan_file_overlap", default="true"), True))
    min_patch_score: int = field(default_factory=lambda: _to_int(_first(env_names=("AGENT_MIN_PATCH_SCORE",), prop_name="agent.min_patch_score", default="65"), 65))
    min_autocommit_score: int = field(default_factory=lambda: _to_int(_first(env_names=("AGENT_MIN_AUTOCOMMIT_SCORE",), prop_name="agent.min_autocommit_score", default="85"), 85))
    enable_history: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_ENABLE_HISTORY",), prop_name="agent.enable_history", default="true"), True))
    enable_multi_agent: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_ENABLE_MULTI_AGENT",), prop_name="agent.enable_multi_agent", default="true"), True))
    enable_llm_normalizer: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_ENABLE_LLM_NORMALIZER",), prop_name="agent.enable_llm_normalizer", default="false"), False))
    enable_llm_localizer: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_ENABLE_LLM_LOCALIZER",), prop_name="agent.enable_llm_localizer", default="false"), False))
    enable_llm_diagnosis: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_ENABLE_LLM_DIAGNOSIS",), prop_name="agent.enable_llm_diagnosis", default="true"), True))
    # v9.3: whitelist applicative explicite (override de l'auto-détection pom.xml).
    # Format CSV: "com.example.,com.example.subpkg." (un préfixe par item, point final inclus).
    # Si vide → auto-détection depuis les pom.xml du repo cible. Si auto-détection
    # vide aussi → fallback sur la blacklist FRAMEWORK_PACKAGE_PREFIXES seule (v9.2).
    application_package_prefixes: list[str] = field(default_factory=lambda: _to_csv_list(_first(env_names=("AGENT_APPLICATION_PACKAGE_PREFIXES",), prop_name="agent.application_package_prefixes", default="")))
    # v9.12: mode applicateur deterministe. Si true, le bundle saute le LLM
    # PatchPlanner quand Rovo a fourni une suggestion exploitable (gating strict).
    # Si false, comportement v9.11 strict (toujours passer par le LLM).
    deterministic_applicator_enabled: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_DETERMINISTIC_APPLICATOR_ENABLED",), prop_name="agent.deterministic_applicator_enabled", default="true"), True))
    # v9.13.2 : filet de securite LLM libre (Phase 2)
    # Quand toute la cascade echoue, on lance un dernier appel LLM en mode libre
    # (sans contraintes de cibles) pour produire un correctif applicable meme imparfait.
    llm_libre_enabled: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_LLM_LIBRE_ENABLED",), prop_name="agent.llm_libre_enabled", default="true"), True))
    llm_libre_min_validation_score: int = field(default_factory=lambda: int(_first(env_names=("AGENT_LLM_LIBRE_MIN_VALIDATION_SCORE",), prop_name="agent.llm_libre_min_validation_score", default="40") or "40"))

    # v9.14 : agent autonome d'exploration (Option alpha : remplace LLM libre)
    # Quand toute la cascade echoue, on lance un agent qui fouille le repo en
    # lecture seule avec 11 outils (read_file, grep, find_files, git_log, etc.)
    # et propose un patch. Mode batch nuit/weekend (8-30 min par ticket).
    exploration_agent_enabled: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_EXPLORATION_ENABLED",), prop_name="agent.exploration_agent_enabled", default="true"), True))
    exploration_max_turns: int = field(default_factory=lambda: int(_first(env_names=("AGENT_EXPLORATION_MAX_TURNS",), prop_name="agent.exploration_max_turns", default="20") or "20"))
    exploration_min_validation_score: int = field(default_factory=lambda: int(_first(env_names=("AGENT_EXPLORATION_MIN_VALIDATION_SCORE",), prop_name="agent.exploration_min_validation_score", default="40") or "40"))
    exploration_max_tokens: int = field(default_factory=lambda: int(_first(env_names=("AGENT_EXPLORATION_MAX_TOKENS",), prop_name="agent.exploration_max_tokens", default="50000") or "50000"))
    # v9.13.1 : genericite
    ticket_prefixes: list[str] = field(default_factory=lambda: _to_csv_list(_first(env_names=("AGENT_TICKET_PREFIXES",), prop_name="agent.ticket_prefixes", default="RUN")) or ["RUN"])
    priority_repo_paths: list[str] = field(default_factory=lambda: _to_csv_list(_first(env_names=("AGENT_PRIORITY_REPO_PATHS",), prop_name="agent.priority_repo_paths", default="back,front,batch")) or [])
    generate_junit_tests: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_GENERATE_JUNIT_TESTS",), prop_name="agent.generate_junit_tests", default="true"), True))
    anonymize_personal_data: bool = field(default_factory=lambda: _to_bool(_first(env_names=("AGENT_ANONYMIZE_PERSONAL_DATA",), prop_name="agent.anonymize_personal_data", default="true"), True))

    def artifacts_root(self) -> Path:
        if self.artifacts_root_override:
            path = Path(self.artifacts_root_override).expanduser().resolve()
        else:
            path = (ROOT_DIR / self.artifacts_root_name).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(slots=True)
class RagSettings:
    """v9.8: configuration du module RAG (Retrieval-Augmented Generation).

    Le RAG est OPT-IN par defaut (enabled=false). On l'active manuellement
    apres avoir construit l'index via scripts/build_rag_index.bat.
    """
    enabled: bool = field(
        default_factory=lambda: _prop("rag.enabled", "false").strip().lower() == "true"
    )
    index_path: str = field(
        default_factory=lambda: _prop("rag.index_path", "rag_index")
    )
    embedding_model: str = field(
        default_factory=lambda: _prop("rag.embedding_model", "intfloat/multilingual-e5-small")
    )
    # Liste de chemins de repos Git separes par |
    # Format: "path1|tag1;path2|tag2;..."
    # ex: "C:/repos/myapp-back|back;C:/repos/myapp-front|front"
    git_repos: str = field(
        default_factory=lambda: _prop("rag.git_repos", "")
    )
    since: str = field(
        default_factory=lambda: _prop("rag.since", "2 years ago")
    )
    top_k: int = field(
        default_factory=lambda: int(_prop("rag.top_k", "3") or "3")
    )
    min_freshness: float = field(
        default_factory=lambda: float(_prop("rag.min_freshness", "0.3") or "0.3")
    )

    def parse_repos(self) -> list[dict[str, str]]:
        """Parse rag.git_repos en liste de dicts utilisables par le builder."""
        if not self.git_repos.strip():
            return []
        repos = []
        for chunk in self.git_repos.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "|" in chunk:
                path, tag = chunk.split("|", 1)
                repos.append({"path": path.strip(), "tag": tag.strip()})
            else:
                repos.append({"path": chunk, "tag": ""})
        return repos


@dataclass(slots=True)
class Settings:
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    forge: ForgeSettings = field(default_factory=ForgeSettings)
    branches: BranchSettings = field(default_factory=BranchSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    rag: RagSettings = field(default_factory=RagSettings)


settings = Settings()
