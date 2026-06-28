# autofix-agent

An autonomous bug fixing pipeline. You give the bundle a ticket analysis. The bundle locates the bug, builds a patch, and opens a draft Merge Request on GitLab.

![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![status](https://img.shields.io/badge/status-beta-orange)

## What you get

You supply a ticket analysis. The file holds two parts. First a free form bug description. Second a JSON block produced upstream by an LLM agent like Atlassian Rovo, Claude, or GPT-4.

The bundle reads the file. The bundle runs a six stage cascade. The bundle pushes a branch to GitLab. The bundle opens a draft MR. The bundle posts a structured comment back on the Jira ticket.

When the cascade builds a confident fix, the MR contains the patch. When the cascade cannot reach the confidence threshold, the MR opens as a proposition with the full diagnosis for a human to finish.

The bundle stays language agnostic at the core. Stages 3c through 3f run pure LLM dialogue and adapt to any language. Stages 3a and 3b plus the test generator ship Java and Spring optimizations today. See the Supported languages section below.

## Cascade overview

Six stages run in order. Each stage tries a different strategy. The first stage to produce a valid plan wins.

```
Ticket analysis (JSON)
        |
        v
   Orchestrator
        |
        v
   3a  Pattern fixer (deterministic)
   3b  Rovo applicator (deterministic)
   3c  LLM PatchPlanner (single call)
   3d  Method-only fallback (single call)
   3e  ExplorationAgent  <-- ReAct loop, ~13 tools, 40 turns max
   3f  Free-mode LLM (single call)
        |
        v
   GitLab MR  +  Jira comment back link
```

Cheap stages run before expensive stages. Deterministic patterns try first. Single LLM calls try next. The autonomous agent runs only as a last resort. The free mode safety net stays for the truly hard tickets.

## What the bundle is not

Two clarifications matter.

The bundle is not multi-agent. The bundle runs a sequential pipeline. Only stage 3e is genuinely agentic, a ReAct loop where the LLM picks tools across many turns. The other stages run once and return.

The bundle is not MCP. Rovo speaks MCP to Jira and Confluence upstream. The bundle reads the file Rovo produced. The bundle never opens an MCP session of its own. Native MCP support sits on the roadmap in docs/extending.md.

These distinctions matter when you describe the system to other engineers. Do not oversell.

## Quick start

Prerequisites:

- Python 3.10 or above on the host
- Git in PATH
- An LLM provider, either a local Ollama or API access to Anthropic or OpenAI
- A GitLab project with an API token of scope api
- Optional Jira credentials for the back comment feature
- A local clone of your target repo

Install:

```
git clone <this-repo-url> autofix-agent
cd autofix-agent
python -m pip install -r requirements.txt
```

Configure templates_external_config/config.env:

```
FIX_TARGET_REPO=/path/to/your/repo
FORGE_API_URL=https://gitlab.your-company.com/api/v4
FORGE_PROJECT_ID=12345
FORGE_BASE_URL=https://gitlab.your-company.com
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3-coder:30b
```

Configure templates_external_config/secrets.env, never commit:

```
FORGE_TOKEN=glpat-xxxxxxxxxxxx
JIRA_BASE_URL=https://your-company.atlassian.net
JIRA_USER=your.email@your-company.com
JIRA_TOKEN=ATATT3xFfGF0xxxx
```

Drop ticket analyses in analysis/issue_analysis_TICKET-NNNN.txt. See docs/rovo-prompt-guide.md for the prompt to use with your upstream LLM. See examples/ for a working sample.

Run from CLI:

```
python -m scripts.batch_run --repo /path/to/your/repo --base-branch main
```

Run with the web UI:

```
cd webui
start_webui.bat
```

Open http://127.0.0.1:8420. The UI shows live logs, the cascade stages, and a per ticket pre-MR analysis view.

## Configuration

All configuration lives in environment variables. See docs/configuration.md for the full reference. The important variables:

- FIX_TARGET_REPO: absolute path to the local clone of your repo, required
- FORGE_API_URL: GitLab API base URL, required
- FORGE_PROJECT_ID: numeric project ID, required
- FORGE_TOKEN: API token with scope api, required, in secrets.env
- OLLAMA_BASE_URL: LLM endpoint, default http://localhost:11434
- OLLAMA_MODEL: model name, default qwen3-coder:30b
- AGENT_MIN_PATCH_SCORE: below this, the patch gets rejected, default 65
- AGENT_MIN_AUTOCOMMIT_SCORE: below this, the MR opens as proposition only, default 85
- AGENT_LLM_LIBRE_ENABLED: enable stage 3f, default true

## Architecture in brief

A deeper walkthrough lives in docs/architecture.md. The essentials:

- app/service/agent_service.py: orchestrator, reads analysis, drives the cascade, writes artifacts
- app/service/exploration_agent.py: stage 3e, ReAct loop with 13 tools
- app/service/llm_libre_fallback_agent.py: stage 3f, free-mode LLM safety net
- app/service/jira_commenter.py: posts the analysis back to Jira after the MR opens
- app/service/mr_description_builder.py: builds the rich MR description
- webui/: FastAPI backend and React frontend, CDN based, no build step

Every run writes a folder under runs/RUN-NNNN/run-id/:

```
correction_contract.json     <- the bundle understanding of the bug
plan.json                    <- the patch the bundle applied
exploration_trace.json       <- what stage 3e explored
llm_libre_result.json        <- output of stage 3f when run
validator_report.json        <- score, confidence, gating verdict
run.log                      <- chronological log
```

These artifacts feed the web UI Pre-MR analysis tab.

## Roadmap

Work in the pipeline, not yet shipped:

- Interactive chat tab. Talk to the agent about a ticket, give hints, regenerate the MR. Skeleton present as a placeholder in the WebUI.
- Final action tab. Approve, iterate, or reject as a human gate before the MR opens.
- Multi-repo routing. Today the bundle handles one repo per run. Future versions dispatch tickets based on repo_hint to back, front, or batch repos.
- Real validator. Today the LLM self-rates. An external validator (linter, second LLM, AST diff) would give a more trustworthy signal.
- Native MCP client for Jira and Confluence. Today the bundle reads a JSON file from disk produced upstream by Rovo via MCP. A native MCP client would fetch tickets and Confluence pages directly. Design in docs/extending.md.
- Multi-agent refactor of the cascade. Today the cascade runs six sequential stages with one ReAct loop inside stage 3e. A multi-agent design would split the work across specialized agents (Localizer, Patcher, Reviewer, Test-writer, Risk) sharing a message bus. Design in docs/extending.md.

To work on any of these, read CONTRIBUTING.md.

## Supported languages

Six stages run in the cascade. Each stage has a different language sensitivity:

- Stage 3a, Pattern fixer: regex based fixes for common exception patterns. Java patterns ship today. Adding other languages stays a contained task.
- Stage 3b, Rovo applicator: applies the upstream LLM structured suggestion. Partial language sensitivity, depends on what your upstream agent produces.
- Stage 3c, LLM PatchPlanner: single LLM call with a constrained prompt. Language agnostic. The LLM adapts to whatever language appears.
- Stage 3d, Method-only fallback: single LLM call with narrow context. Language agnostic.
- Stage 3e, ExplorationAgent: ReAct loop with 13 generic tools (read_file, grep, git_log, find_callers). Language agnostic.
- Stage 3f, Free-mode LLM: single LLM call with carte blanche. Language agnostic.
- Test generator, optional: produces JUnit test stubs. Java only today. Disable for other languages with AGENT_GENERATE_JUNIT_TESTS=false.

Today the bundle ships well tuned for Java and Spring. Other languages (Python, TypeScript, Go, Kotlin, Ruby) run via stages 3c through 3f without modification. You lose the speed of stage 3a and the auto generated tests, you keep the LLM driven fixes.

Adding a language adapter stays a contained piece of work. See docs/extending.md for the design.

## Limitations and honest caveats

A few things to know before adopting:

- Quality depends heavily on the upstream analysis. When the analysis JSON does not tell the agent where to look (must_touch, file_hint), stage 3e has to explore blindly. Slow on CPU. Uneven in quality. Invest in a good upstream prompt.
- The LLM self rates patches. Scores above 85 deserve scrutiny on non trivial fixes. The confidence threshold guards the worst cases by downgrading to a proposition MR. You still review every MR.
- CPU is slow. A typical run with a 30B local model takes 30 to 45 minutes per ticket on CPU. On a recent GPU, the same ticket runs in 2 to 5 minutes. Plan accordingly.
- Best results today on Java and Spring. The bundle was built against Java and Spring monorepos. Stages 3a, 3b, and the test generator reflect this. Other languages work via the LLM stages with fewer optimizations until per language adapters get contributed. See docs/extending.md.

## License

MIT. See LICENSE.

## Acknowledgements

The architecture (deterministic cascade plus agentic last resort) draws on patterns in Aider, SWE-Bench work, and the ReAct paper. The free mode LLM safety net (stage 3f) adapts the idea of letting the LLM try something even at low confidence, marked for human review.
