# Bundle configuration reference

The entire bundle configuration lives in TWO files in this folder:

- config.env: non sensitive configuration
- secrets.env: secret tokens only

Format: KEY=value, one per line. No quotes needed. Empty lines and lines starting with # get ignored.

## Where to put this folder

The bundle looks for templates_external_config/ in two locations, in order:

1. NEXT TO the bundle (recommended): <parent>/templates_external_config/. Config gets shared across bundle versions.
2. INSIDE the bundle: autofix-agent/templates_external_config/. Default location shipped with the bundle.

When both exist, the one next to the bundle takes priority.

## Load priority

A variable defined in the shell (export KEY=value or set KEY=value) always overrides the file. Otherwise: config.env then secrets.env. Older files (.env, application.properties) no longer get used since v9.15.


=== FILE: secrets.env ===

FORGE_TOKEN: GitLab Personal Access Token (with scopes api and write_repository). REQUIRED to create MRs. Example: glpat-xxxxxxxxxxxxxxxxxxxx.

JIRA_TOKEN: Jira API token. OPTIONAL, only when the bundle queries Jira. Example: ATATT3xFfGF0xxxxxxxx.

CONFLUENCE_TOKEN: Confluence API token. OPTIONAL, only when the bundle reads Confluence. Example: ATATT3xFfGF0xxxxxxxx.

Never commit secrets.env to Git.


=== FILE: config.env ===

--- Ollama, local LLM engine ---

OLLAMA_BASE_URL: Ollama server URL. Leave as is for a local instance. Value: http://localhost:11434.

OLLAMA_MODEL: Name of the Ollama model used by the agent. Recommended: qwen3-coder:30b.

OLLAMA_TEMPERATURE: Sampling temperature. 0 means deterministic. Recommended for code. Value: 0.

OLLAMA_NUM_PREDICT: Max tokens generated per call. Value: 4096.

--- Target repo, CRITICAL ---

FIX_TARGET_REPO: Absolute path to the local Git repo the bundle modifies. The folder must contain .git directly. When wrong, the bundle does not find the code to fix. Example: C:\path\to\your\repo.

--- GitLab, forge where MRs get created ---

FORGE_PROVIDER: Forge type. Only gitlab works today. Value: gitlab.

FORGE_API_URL: GitLab API URL. For self hosted GitLab, adapt the domain. Example: https://gitlab.your-company.com/api/v4.

FORGE_PROJECT_ID: GitLab project ID. Either group/project or the numeric ID. Example: your-org/your-project.

FORGE_BASE_URL: Forge base URL (no /api/v4). Optional, derived from FORGE_API_URL when empty. Example: https://gitlab.your-company.com.

FORGE_ASSIGNEE_USERNAME: GitLab username assigned to created MRs. Empty means no assignee.

FORGE_REVIEWERS: Comma separated reviewers for MRs. Empty means no reviewers.

FORGE_LABELS: Comma separated labels applied to MRs. Value: BUG,FIX.

FORGE_DRAFT: true means MRs get created as drafts. Value: true.

FORGE_DESCRIPTION_APPEND: Text appended to the bottom of every MR description.

--- Git branches ---

DEFAULT_BASE_BRANCH: Branch from which fix branches get created. Value: main.

DEFAULT_TARGET_BRANCH: Target branch for the MRs. Value: main.

DEFAULT_SOURCE_BRANCH_PREFIX: Prefix for fix branches. Produces hotfix/RUN-NNNN. Value: hotfix/.

--- Jira and Confluence, optional ---

JIRA_BASE_URL: Jira instance URL. Optional. Example: https://your-company.atlassian.net.

JIRA_USER: Jira account email. Optional.

CONFLUENCE_BASE_URL: Confluence instance URL. Optional.

CONFLUENCE_SPACE_KEYS: Comma separated Confluence space keys to consult. Optional.

--- Agent general parameters ---

AGENT_MAX_FILES: Max number of files loaded for context. Value: 12.

AGENT_MAX_FILE_CHARS: Max characters read per file. Value: 12000.

AGENT_WORKDIR: Temporary work folder. Value: .work.

AGENT_ENABLE_TIMING_LOGS: true means log durations of each step. Value: true.

AGENT_TICKET_PREFIXES: Comma separated recognized ticket prefixes. Value: RUN.

AGENT_PRIORITY_REPO_PATHS: Comma separated folders to prioritize within the repo. Value: back,front,batch.

AGENT_GENERATE_JUNIT_TESTS: true means generate JUnit tests alongside fixes. Value: true. Set to false for non Java projects.

AGENT_ANONYMIZE_PERSONAL_DATA: true means anonymize personal data in logs and artifacts. Value: true.

AGENT_DETERMINISTIC_APPLICATOR_ENABLED: true means enable the deterministic applicator with gating. Value: true.

--- Agent stage 3f, free mode LLM safety net ---

AGENT_LLM_LIBRE_ENABLED: true means enable the free mode LLM safety net. Value: true.

AGENT_LLM_LIBRE_MIN_VALIDATION_SCORE: Minimum score to accept a patch from stage 3f. Value: 40.

--- Agent stage 3e, autonomous exploration ---

AGENT_EXPLORATION_ENABLED: true means enable the autonomous exploration agent. Value: true.

AGENT_EXPLORATION_MAX_TURNS: Max number of exploration turns. Value: 20.

AGENT_EXPLORATION_MIN_VALIDATION_SCORE: Minimum score to accept an exploration patch. Value: 40.

AGENT_EXPLORATION_MAX_TOKENS: Total token budget for one exploration. Value: 50000.


=== QUICK START ===

1. Open secrets.env. Fill FORGE_TOKEN with your GitLab Personal Access Token.
2. Open config.env. Set FIX_TARGET_REPO (path to your local Git repo) and FORGE_API_URL (your forge URL).
3. All other values stay at sensible defaults. Leave as is.
4. Run scripts\check_env.bat (Windows) or python -m scripts.check_env. Everything should turn green.
