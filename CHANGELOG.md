# Changelog

The full historical changelog (v9.0 through v9.15, in French) lives in CHANGELOG.fr.md. The English changelog below covers the v9.16 series in detail, where most of the public release work happened, plus brief one line summaries of older milestones.

## v1.4 OSS, Project rename to autofix-agent

Three changes in this release:

- Project rename. fix-anomalie-agent becomes autofix-agent. Clear English name. Recognizable on GitHub. Avoids the French and the typo of the prior name.
- Documentation rewrite. All public docs follow a strict writing style: short sentences, active voice, no em dashes, no filler words, no bold or italic. The reader gets the same information faster.
- Backward compatibility. The Jira commenter detects both prior tag prefixes (fix-anomalie-agent and local-fix-agent) so old tickets do not get re-commented after the rename. Old artifacts stay readable.

## v9.16.11, Jira comment mirrors the GitLab MR description

The GitLab MR description previously contained 7+ rich sections (problem, proposed solution, business rules, tests, similar incidents, risks, meta). The Jira comment posted in parallel had only 4 (summary, MR link, business rules, tests). Consequence: the Business Analyst had to open GitLab to get the full analysis. Broke the validation flow.

CommentPayload now accepts two optional fields, contract and plan. When present, the fields enable the comment enrichment. Three new sections got added to the Jira comment at the same positions as in the GitLab MR description:

- DETAILED SOLUTION: modified method, approach, fix summary, business justification, validation score.
- SIMILAR INCIDENTS: up to 5 past tickets (from Rovo similar_past_incidents).
- RISKS AND UNCERTAINTIES: up to 8 entries with category and severity (from Rovo risks_and_uncertainties).

The SUMMARY section gains Exception and Localization lines (from contract.exception_class, crash_file, crash_method, crash_line).

Design choices: no Java code in the Jira comment (the BA clicks the MR link to see code), safety caps of 5 similar incidents and 8 risks, silent omission when a section has no data.

## v9.16.10, Rebrand to fix-anomalie-agent plus enriched Jira comment

Three grouped changes touching the Jira return path quality:

1. Rename the brand local-fix-agent to fix-anomalie-agent on user visible outputs (Jira comment, generated Java test file headers, .gitignore pushed to the target repo).
2. Bold the 5 section titles of the Jira comment for readability.
3. Strengthen the Rovo prompt (v1.4.4) so business rules and their Confluence URLs become the PRIMARY output. What lets the BA decide which non regression tests to run.

app/service/jira_commenter.py defines TAG_PREFIX = "[fix-anomalie-agent" and LEGACY_TAG_PREFIXES = ("[local-fix-agent",) for backward compatibility. has_run_comment detects both. No double posting on tickets already commented by the previous version.

The 5 headers get produced with **...** notation. _text_to_adf parses them into ADF nodes with marks: [{"type": "strong"}] for clean bold rendering in Jira Cloud. When Rovo provides validates_rg: [...] (v1.4.4 field), the TESTS TO ADD section shows [validates: ...] after the scenario.

NOT renamed (intentional): internal logger names and the internal contract["source"] field. Both kept for tooling and data continuity.

The Rovo prompt v1.4.4 (in docs/prompt_rovo_v1.4.4.md) adds four new rules: active Confluence URL search via MCP, validates_rg link test to business rule, mandatory reporting of every cited business rule reference, and a stronger framing of rules_of_business as the primary deliverable. The bundle stays forward compatible with v1.4.3 analyses.

## v9.16.9, Re wiring the free mode LLM safety net as stage 3f

When the entire cascade fails (deterministic patterns 3a, Rovo applicator 3b, constrained LLM 3c, method only fallback 3d, AND exploration agent 3e), the ticket ended in a propositional MR without a fix. The free mode LLM safety net from v9.13.2 (prompt = bug definition plus classes and methods plus source, carte blanche to the LLM) got REPLACED by the ExplorationAgent in v9.14. The code still existed (llm_libre_fallback_agent.py) but never got called.

agent_service.py gains a new stage 3f inserted after the exploration block. The stage runs only when the plan stays empty after 3a through 3e. The stage builds a file_map (max 6 files, 8000 chars each), calls try_llm_libre, and when validation_score >= AGENT_LLM_LIBRE_MIN_VALIDATION_SCORE (default 40), adopts the plan. The llm_libre_mode flag triggers the [LLM-LIBRE] title prefix, draft mode, and the needs-review label.

Configuration: AGENT_LLM_LIBRE_ENABLED=true (default) and AGENT_LLM_LIBRE_MIN_VALIDATION_SCORE=40. New artifact: runs/RUN-NNNN/run-id/llm_libre_result.json.

## v9.16.8, Batch robustness, aggressive cleanup between tickets

A run on a real ticket in v9.16.7 ended with delivery_status=MR_BLOCKED and diagnostic_error: Unable to checkout local branch 'main' because the previous ticket had left the working tree unclean.

scripts/batch_run.py now does aggressive cleanup before EACH ticket: git reset --hard HEAD, then git clean -fd (untracked files plus directories), then verification git status --porcelain returns empty. When still dirty, the ticket gets skipped with a clear error rather than running on a polluted tree.

## v9.16.7, Jira return loop, automatic comment

After creating a GitLab MR, automatically post a structured comment on the originating Jira ticket. Closes the Jira to GitLab to Jira loop. Business Analysts who live in Jira see the automatic analysis without going to GitLab.

5 design decisions: structured 5 section comment (no EXECUTION section), business rules as a Reference Validation URL table with absent when URL stays missing, comment ONLY when a real MR opens, idempotence by run_id, notifications enabled by default.

Configuration in secrets.env plus config.env: JIRA_BASE_URL, JIRA_USER, JIRA_TOKEN (required), JIRA_COMMENT_ENABLED=true (default), JIRA_NOTIFY_ON_COMMENT=true (default).

When any required Jira variable goes missing, the back comment skips silently. Non blocking: any Jira error gets logged but does not fail the GitLab run.

## v9.16.6, Developer Hint plus external configurable LLM provider

Developer Hint: a developer provides a hint ("look at class X" or "the bug lives in module Y") via --hint "..." CLI arg or a HINT_FILE env var. The hint gets injected into the constrained LLM prompt (stage 3c) to guide the diagnosis. Validation guardrails ensure the hint does not fully override Rovo analysis (length cap, plain text only).

External LLM provider: the bundle now uses a non Ollama LLM via the LLM_PROVIDER setting. Values: ollama (default), anthropic (requires ANTHROPIC_API_KEY plus ANTHROPIC_MODEL), openai (requires OPENAI_API_KEY plus OPENAI_MODEL). Useful for benchmarking cascade quality between providers, or for users without local GPU access.

## v9.16.5, Tolerant repair of broken Rovo JSON

Rovo sometimes produces JSON not strictly valid: missing parent keys before arrays, trailing commas, unescaped quotes in extracts. The extractor v9.16.5 introduces a tolerant repair pass fixing the most common patterns BEFORE running json.loads(). Reduced the Rovo JSON malformed to ticket ignored rate by around 80 percent.

## v9.16.4, Precise GitLab diagnostic plus API URL auto correction

A configuration error caused mass MR failures with cryptic HTTPError 404. v9.16.4 adds preflight diagnostics: detects when FORGE_API_URL ends in / (now stripped), detects when FORGE_API_URL does not end in /api/v4 (auto appends), diagnoses 401, 403, 404 with specific guidance (token scope, project ID format).

## v9.16.3, Windows encoding crash fix plus GitLab diagnostic

Bug observed on Windows VMs with non UTF8 console encoding. The bundle crashed when logging non ASCII characters from Rovo. Fixed by forcing PYTHONIOENCODING=utf-8 for subprocess invocations and adding a --no-emoji mode for safer terminal output.

## v9.16.2, Always create an MR plus anti wasted effort guardrail

Even when the cascade fails completely, the bundle now creates a proposition MR with the Rovo analysis attached. The reasoning: a ticket without an MR stays invisible to the team. A proposition MR surfaces in the GitLab MR list and triggers human review.

Anti waste guardrail: when the cascade fails AND Rovo had nothing useful (no must_touch, no must_touch_method), skip the proposition MR. The MR would only be noise.

## v9.16.1, Fixed 2 hour batch timeout

A subprocess timeout was hardcoded to 7200 seconds. Long batches on slow hardware got killed mid run. Made configurable via BATCH_TICKET_TIMEOUT_SECONDS (default 3600) and applied per ticket instead of per batch.

## v9.16, Demo mode for the IT investment proposal

A AGENT_DEMO_MODE=true flag adds extra friendly log lines for live demos, skips some non essential steps taking 30+ seconds on CPU, and forces a single ticket execution flow. Not meant for production. Purely for live presentations.

## Earlier versions, brief

v9.15.x: fixed 3 blocking bugs surfaced by real runs (Rovo extractor edge cases, must_touch resolution, MR title generation).

v9.15: centralized configuration. Replaced scattered .env and application.properties files with a single templates_external_config/ folder with config.env plus secrets.env. Reload priority: shell env above secrets.env above config.env.

v9.14: introduction of the autonomous ExplorationAgent (stage 3e). ReAct loop with around 13 tools (read_file, grep, git_log, git_blame, find_callers) and a 40 turn budget. Replaced the v9.13.2 free mode LLM safety net. Later re wired as stage 3f in v9.16.9.

v9.13.x: cascade restructuring into 5 named stages (3a through 3e) with clear gating logic between them. Introduction of the first free mode LLM safety net. Later re wired in v9.16.9.

v9.12.x: Rovo prompt v1.3.x. Structured JSON output with investigation_candidates, acceptance_criteria, tests_to_add, similar_past_incidents, risks_and_uncertainties.

v9.11: introduction of rules_of_business in the Rovo output. The deliverable driving non regression test selection downstream.

v9.10 and earlier: foundational work. The cascade architecture, the constrained LLM PatchPlanner, the Rovo to bundle pipeline, the GitLab MR creation.

For full historical detail of these earlier versions (in French), see CHANGELOG.fr.md.
