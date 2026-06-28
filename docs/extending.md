# Extending autofix-agent

This document covers how to extend the bundle along three axes:

1. Adding a language adapter so stages 3a, 3b, and test generation work for languages other than Java
2. Adding a new cascade stage as a new fix generation strategy
3. Plugging in a custom validator to replace the LLM self assessment

For other extensions, open an issue first. The discussion saves you time.

## 1. Adding a language adapter

### What language support means here

Six of the seven moving parts in the pipeline stay language agnostic today:

- The orchestrator (agent_service.py)
- Stages 3c, 3d, 3e, 3f (all LLM driven)
- The validator (score and confidence thresholds)
- Branch, commit, push, MR plumbing
- The Jira return comment
- The WebUI

Three pieces stay language specific today:

- Pattern fixer (stage 3a), app/service/pattern_based_fix_generator.py. Regex based fixes for common exceptions, e.g. NullPointerException to null guard.
- Test generator, app/service/test_generator.py. Produces JUnit 5 test stubs alongside the patch.
- Rovo prompt, docs/prompt_rovo_v1.4.4.md. Mentions Java conventions in examples.

### The honest current state

Today these three pieces sit outside a clean abstraction. Java assumptions live inline. Adding Python support means forking the relevant module, replacing Java patterns with Python equivalents, and wiring a conditional in the orchestrator. Works for one alternative language. Does not scale to four or five languages cleanly.

A future refactor to a formal LanguageAdapter interface sits on the roadmap. See Proposed architecture below. We held off until at least one second concrete language adapter exists, to avoid premature abstraction.

### Path of least resistance, today

To run the bundle on a non Java codebase right now:

1. Disable the test generator. Set AGENT_GENERATE_JUNIT_TESTS=false in your config.env. Skips the JUnit step entirely. Other languages get a similar generator contributed later. For now you avoid irrelevant Java test stubs.

2. Stage 3a (PatternFixer) goes no op for non Java exceptions. The patterns in pattern_based_fix_generator.py match Java exception class names only. Fine. The cascade falls through to 3b, then 3c, then 3d, then 3e (ExplorationAgent), then 3f (free mode LLM). You lose the speed of stage 3a. You gain LLM driven fixes.

3. Stage 3b (RovoApplicator) depends on what your upstream LLM produces. Adapt the Rovo prompt (see docs/rovo-prompt-guide.md) to ask for Python conventions. Stage 3b then applies Python suggestions.

4. The Rovo prompt has Java examples in docs/prompt_rovo_v1.4.4.md. Copy to docs/prompt_rovo_v1.4.4-python.md. Change examples to Python or your language. Use this version with your upstream agent. The bundle reads whatever JSON v1.4.4 your agent produces.

The above gets the bundle running on a Python or TypeScript codebase. Quality stays good on stages 3c through 3f, no help from 3a, partial from 3b.

### Adding deterministic patterns for your language

To make stage 3a fire on your language too, copy pattern_based_fix_generator.py to a new module, e.g. pattern_based_fix_generator_python.py. Replace Java exception names and fix templates with Python ones (AttributeError to null guard, KeyError to dict.get fallback, TypeError on None to type check). Register in agent_service.py where stage 3a runs.

Today the orchestrator picks the pattern generator like this, paraphrased:

```
if contract.get("repo_hint") == "python":
    fixer = PatternBasedFixGeneratorPython(contract, ...)
else:
    fixer = PatternBasedFixGenerator(contract, ...)
```

Duplication heavy. Clear. Works without an abstraction layer. When the codebase has three or more language fixers, the refactor below kicks in.

### Adding a test generator for your language

Same pattern as the pattern fixer. Copy test_generator.py. Rewrite the JUnit prompt (app/prompts/test_skeleton_prompt.md) into pytest, Jest, RSpec, or GoTest. Register based on repo_hint or file extension detection in agent_service.py.

### Proposed architecture, future

When at least one non Java adapter ships, the plan refactors to a clean interface:

```
# app/adapters/language_adapter.py
class LanguageAdapter(Protocol):
    name: str  # "java", "python", "typescript", ...
    
    def matches_repo(self, repo_path: Path) -> bool:
        """Detect whether this adapter applies (file extensions, build files)"""
    
    def pattern_fixer(self) -> PatternFixer | None:
        """Return a stage-3a fixer, or None to skip stage 3a."""
    
    def test_generator(self) -> TestGenerator | None:
        """Return a test stub generator, or None to skip."""
    
    def language_specific_prompt_hints(self) -> str:
        """Snippets injected into LLM prompts."""

# app/adapters/java_adapter.py
class JavaAdapter:
    name = "java"
    def matches_repo(self, repo_path):
        return any(repo_path.rglob("pom.xml")) or any(repo_path.rglob("build.gradle*"))
    def pattern_fixer(self):
        return PatternBasedFixGeneratorJava(...)
    def test_generator(self):
        return JUnitTestGenerator(...)
    def language_specific_prompt_hints(self):
        return "Follow Spring conventions, use @Override, ..."

# In agent_service.py
adapters = [JavaAdapter(), PythonAdapter(), TypeScriptAdapter()]
active_adapter = next((a for a in adapters if a.matches_repo(repo_path)), None)
```

The roadmap lands this when someone contributes the second adapter. PRs welcome.

## 2. Adding a new cascade stage

The cascade today lives hard coded inside FixAgent.run as a sequence of if/elif blocks, one per stage. A future refactor extracts these into stage classes. See CONTRIBUTING.md, "High value contributions".

For now, to add a new stage:

1. Decide where the stage fits in the cost order. Most new stages land between 3d (method-only fallback) and 3e (ExplorationAgent). Stage 3e stays the expensive one.
2. Add your stage as a new block in agent_service.py, FixAgent.run, gated by a feature flag in settings.py so the stage gets disabled.
3. Write the artifact your stage produces to runs/RUN-NNNN/run-id/, e.g. my_stage_result.json. The WebUI Pre-MR analysis tab picks the artifact up.
4. Update docs/architecture.md to mention your stage in the cascade diagram.

When your stage uses the LLM, reuse the existing self.llm instance. The instance handles the provider abstraction (Ollama, Anthropic, OpenAI).

## 3. Plugging in a custom validator

The current validator (app/service/validator.py, called from agent_service.py after the cascade) uses the LLM own validation_score and model_confidence from the produced plan. A known weakness. The LLM self rates and tends to be optimistic.

An external validator gives a much more trustworthy signal. Two designs:

Lightweight validator, a second LLM call. Take the diff produced by the cascade, the original contract, and the test cases. Ask a separate LLM call: is this fix likely correct given the contract? Treat the answer as external_validation_score. Combine with the self rated score via min() or weighted average.

Static analysis validator. Parse the diff. Run language specific linters or AST analysis on the modified files. For Java: javac -Werror on the diff. For Python: ruff check and mypy. Any new error introduced by the patch drops the validation score below the autocommit threshold.

To plug in a custom validator, modify app/service/validator.py, Validator.validate or write a new validator and select via a VALIDATOR_TYPE env var. The validator returns a ValidatorReport dict the orchestrator uses for the gating decision.

PRs implementing either of these would land high value.

## 4. Roadmap items

Two larger evolutions sit on the roadmap. Both change the bundle at a structural level. Neither ships today. The docs and README reflect this honestly so no reader assumes the bundle does work outside its current scope.

### Native MCP client for Jira and Confluence

The bundle today reads ticket analyses from a file on disk. A human or a script puts the file there. Rovo or another upstream agent does the MCP work to fetch the ticket and Confluence pages.

Today the flow:

1. You open Rovo in Atlassian
2. Rovo speaks MCP to Jira and Confluence
3. Rovo produces a JSON analysis
4. You save the JSON to analysis/issue_analysis_TICKET-NNNN.txt
5. The bundle reads the file

A native MCP client skips steps 1 to 4. The bundle opens an MCP session to Atlassian Remote MCP Server directly. The bundle calls jira.get_issue, confluence.search, confluence.get_page on demand.

Benefits:

- One step workflow, no manual file shuffling
- Always fresh analysis, no copy paste lag
- Programmatic batch runs over hundreds of tickets

Cost:

- Two to four days of development
- New dependency on an MCP client library
- New configuration (MCP endpoint URL, OAuth tokens, scopes)
- Loses the explicit decoupling between Rovo and the bundle, which today helps debugging

The right time to build this evolution: when your team runs more than fifty tickets per week. Below this volume, file based input wins on simplicity.

### Multi-agent refactor of the cascade

The cascade today runs as six sequential stages inside one orchestrator. Stage 3e contains the only ReAct loop. The other five stages run once and return.

A multi-agent design splits the work across agents with distinct roles. Each agent owns a single responsibility:

- Localizer agent: where in the codebase does the bug live
- Patcher agent: produce a candidate fix
- Reviewer agent: score the patch, request changes
- Test-writer agent: produce JUnit or pytest stubs
- Risk agent: flag side effects

The agents exchange messages over a shared bus. The orchestrator becomes a scheduler. Each agent runs in isolation with its own LLM context window.

Benefits:

- Each agent specializes, prompts get shorter and clearer
- Parallelism: Patcher and Test-writer run at the same time
- Easier to swap one agent for a better implementation
- Clearer reasoning trace for debugging

Cost:

- One to two weeks of refactor work
- Message bus and shared state design
- More LLM calls, higher cost on cloud providers
- Harder to reproduce a single run, since agent timing varies

The right time to build this evolution: when stage 3e on its own needs to call other sub agents to make decisions. The current single loop design works well for the volume you handle today.

## Where to ask

- Open an issue on the GitHub repo for design discussion before writing code
- Tag the issue with language-support, cascade, or validator so the issue stays discoverable

Honest expectation: this is a young open source project. Response time on issues stays best effort. To move fast, fork and send a PR. Often quicker than waiting for design alignment.
