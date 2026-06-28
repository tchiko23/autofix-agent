# Configuration

All configuration runs through environment variables. The bundle reads them from two files at startup:

- templates_external_config/config.env: non secret defaults
- templates_external_config/secrets.env: API tokens, never commit

When both files contain the same key, secrets.env wins.

You also override any value through the shell environment (highest priority). Useful for one off runs.

## Required

FIX_TARGET_REPO: absolute path to the local clone of the code repo the bundle modifies.

FORGE_API_URL: GitLab API base URL, e.g. https://gitlab.your-company.com/api/v4.

FORGE_PROJECT_ID: numeric ID of the GitLab project. Find in Project Settings, General.

FORGE_TOKEN: GitLab personal access token with scope api. Put in secrets.env.

OLLAMA_BASE_URL: Ollama endpoint, default http://localhost:11434.

OLLAMA_MODEL: model name as registered in Ollama. E.g. qwen3-coder:30b, codellama:34b.

## Optional: Jira back comments

When all three values get set, the bundle posts a structured comment to the originating Jira ticket after the MR opens.

JIRA_BASE_URL: e.g. https://your-company.atlassian.net.

JIRA_USER: email of the Atlassian account.

JIRA_TOKEN: API token from https://id.atlassian.com/manage-profile/security/api-tokens. Put in secrets.env.

JIRA_COMMENT_ENABLED: true (default) or false to skip even when credentials get set.

JIRA_NOTIFY_ON_COMMENT: true (default). Controls whether the comment triggers Jira notifications.

When any of JIRA_BASE_URL, JIRA_USER, JIRA_TOKEN goes missing, the back comment skips silently. A log line [JIRA] commenter disabled appears.

## Quality gates

AGENT_MIN_PATCH_SCORE, default 65. Below this, the patch gets rejected. The MR opens with the diagnosis only.

AGENT_MIN_AUTOCOMMIT_SCORE, default 85. Below this, the patch lands on the branch. The MR stays draft, labeled needs-review.

AGENT_LLM_LIBRE_ENABLED, default true. Enable stage 3f, the free mode LLM. Set to false to skip stage 3f. The bundle then falls back to a proposition only MR when the cascade fails.

AGENT_LLM_LIBRE_MIN_VALIDATION_SCORE, default 40. Minimum score for stage 3f to adopt the plan.

AGENT_EXPLORATION_ENABLED, default true. Enable stage 3e, the autonomous exploration. Set to false for testing. Useful to force stage 3f to run.

## Cascade tuning

AGENT_MAX_FILES, default 12. Maximum number of candidate files passed to the cascade.

AGENT_PRIORITY_REPO_PATHS, default back,front,batch. Comma separated subfolders the file selector prioritizes. Empty means no priority.

AGENT_PACKAGE_PREFIXES, default empty. Comma separated Java package prefixes, e.g. com.example.,com.example.subpkg. Filters relevant files.

AGENT_EXPLORATION_MAX_TURNS, default 40. Turn budget for stage 3e.

## Branching

AGENT_SOURCE_BRANCH_PREFIX, default hotfix/. Prefix for branches the bundle creates.

AGENT_SOURCE_BRANCH_MODE, default stable. Modes: stable (one branch per ticket, reused), unique (one branch per run, never reused), idempotent (hash based).

AGENT_TARGET_BRANCH, default main. Base branch for the MR.

## RAG, optional and off by default

When enabled, the bundle uses ChromaDB and sentence-transformers to retrieve relevant code snippets from the repo as context for the LLM. Heavy. Extra disk space. Longer cold start. Off by default.

RAG_ENABLED, default false. Master switch. Requires chromadb and sentence-transformers from requirements.txt.

RAG_INDEX_DIR, default .chroma. Where the index lives.

RAG_TOP_K, default 5. Number of snippets to retrieve per query.

## LLM provider, advanced

By default the bundle calls Ollama. Swap to other providers when needed:

LLM_PROVIDER, default ollama. Values: ollama, anthropic, openai.

ANTHROPIC_API_KEY: required when LLM_PROVIDER=anthropic. Put in secrets.env.

ANTHROPIC_MODEL, default claude-3-5-sonnet-latest.

OPENAI_API_KEY: required when LLM_PROVIDER=openai. Put in secrets.env.

OPENAI_MODEL, default gpt-4o.

## Subprocess timeouts

GIT_TIMEOUT_SECONDS, not implemented yet. On the roadmap. Would prevent indefinite hangs on git fetch.

## Demo mode

AGENT_DEMO_MODE, default false. When true, disables some timeouts and exits faster on partial successes. Useful for demos.

## Reading the resolved config

The bundle emits a [CONFIG] block at startup showing which variables came from where:

```
[CONFIG] === resume de la cascade de configuration ===
[CONFIG]   from shell env: FIX_TARGET_REPO, FORGE_TOKEN, ...
[CONFIG]   from config.env: AGENT_MAX_FILES, AGENT_PRIORITY_REPO_PATHS, ...
[CONFIG]   from secrets.env: JIRA_TOKEN
[CONFIG]   from MISSING: ANTHROPIC_API_KEY, OPENAI_API_KEY
[CONFIG] ================================================
```

Your first stop when something does not behave as expected.

## Example minimal config

```
# config.env
FIX_TARGET_REPO=/home/dev/projects/my-java-app
FORGE_API_URL=https://gitlab.example.com/api/v4
FORGE_PROJECT_ID=42
FORGE_BASE_URL=https://gitlab.example.com
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3-coder:30b
AGENT_PACKAGE_PREFIXES=com.example.,com.example.subpkg.
```

```
# secrets.env (NEVER COMMIT)
FORGE_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
JIRA_BASE_URL=https://example.atlassian.net
JIRA_USER=dev@example.com
JIRA_TOKEN=ATATT3xFfGF0xxxxxxxxxxxxxxx
```
