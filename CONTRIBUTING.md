# Contributing

Thanks for considering a contribution. This document covers the practical points.

## Before you open a PR

- Open an issue first for non trivial changes. Even a one paragraph "I plan to do X, the approach would be Y" works. The discussion saves you from rework.
- One change per PR. Easier to review. Easier to roll back.

## Setup

```
git clone <your-fork>
cd autofix-agent
python -m pip install -r requirements.txt
```

For the WebUI:

```
python -m pip install fastapi "uvicorn[standard]"
python -m uvicorn webui.server:app --host 127.0.0.1 --port 8420
```

## Run on your own data

The bundle expects:

1. A target code repo on disk, cloned locally. Java and Spring stay best supported today. Other languages work via the LLM driven cascade stages.
2. Ticket analysis files in analysis/issue_analysis_TICKET-NNNN.txt
3. A configured templates_external_config/config.env and secrets.env

See the examples folder for sample analysis files to smoke test the pipeline.

## Code style

- Python 3.10 and above. Use match, pipe type unions, dict[str, X] over Dict[str, X].
- Black formatted, not enforced today but appreciated.
- Type hints on public functions. Internal helpers skip them when obvious.
- Docstrings in French OR English. Stay consistent within a module.

## Where to make changes

For a new pipeline stage: app/service/agent_service.py, FixAgent.run.

For the agentic loop (tools, turn budget): app/service/exploration_agent.py and app/service/agent_toolset.py.

For the MR description format: app/service/mr_description_builder.py.

For the Jira comment format: app/service/jira_commenter.py, function build_comment_body.

For the Rovo prompt: docs/prompt_rovo_v1.4.4.md. The prompt runs in your upstream tool.

For defaults and thresholds: app/config/settings.py.

For the WebUI: webui/index.html, a single file React app via Babel standalone.

For the backend serving the WebUI: webui/server.py, FastAPI.

## Testing

The bundle has no automated test suite today. Tests sit on the roadmap as a high priority. Until tests land, the workflow stays manual:

1. Run the bundle on a known ticket in your local context
2. Check artifacts land in runs/RUN-NNNN/run-id/ and look sane
3. For changes affecting MR descriptions or Jira comments, post on a non critical test ticket first

If you contribute tests, the recommended structure stays tests/ at the project root, with pytest and light fixtures mocking RepoOps, LLM, and GitLabService.

## High value contributions

Areas where help moves the project forward:

- Add pytest infrastructure. Even a tests/test_smoke.py running the cascade end to end with mocked dependencies wins big.
- Split agent_service.py, FixAgent.run. Today around 800 lines. Each cascade stage deserves a class of its own.
- Multi repo support. Today the bundle targets one repo per run. Routing based on repo_hint would handle back, front, and batch tickets in one run.
- Better validator. The LLM self rates patches. An external validator (second LLM with a critique prompt, or AST diff analyzer) would give a stronger signal.
- Interactive chat tab in the WebUI. Placeholder exists. Making the tab functional requires a /ws/chat endpoint and a session store for per ticket conversations.

## Coding principles

A few opinionated choices worth knowing:

- Stages communicate via JSON artifacts. Each pipeline stage writes output to runs/RUN-NNNN/run-id/artifact.json. The next stage reads from there. The pipeline stays pausable, debuggable, resumable.
- Cascade order runs cheap then expensive. Deterministic stages first. Single LLM calls next. Autonomous agent last. Do not add an expensive stage early.
- Fail safe by default. Each optional step (Jira comment, RAG) wraps in try/except so a partial failure never breaks the run. When you add a try/except, log the actual error. Silent swallows stay forbidden.
- No new dependencies without discussion. The bundle aims for a small footprint (Python stdlib plus requests plus python-dotenv plus optional chromadb and sentence-transformers for RAG).

## Questions

Open an issue. For quick questions, the discussion benefits future contributors who hit the same point.
