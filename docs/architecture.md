# Architecture

This document walks through how the bundle works and why. Read here when you plan to extend the pipeline, write new stages, or change behavior beyond configuration.

## Mental model

The bundle runs a deterministic pipeline. From ticket analysis to Merge Request, the pipeline goes through five macro steps:

1. Read and normalize the ticket analysis
2. Localize the bug in the codebase
3. Cascade through six generation strategies until one produces a patch
4. Validate the patch (score, confidence, thresholds)
5. Deliver (commit, push, MR, Jira comment)

The cascade sits at the heart of the system. Each stage tries something different. Each stage either produces a plan (a set of edits) or falls through to the next stage.

## The cascade

```
3a  Pattern fixer        regex based fixes for known exceptions (NPE guards, etc.)
3b  Rovo applicator      uses the upstream LLM structured suggestion when precise enough
3c  LLM PatchPlanner     single Ollama call with constrained prompt
3d  Method-only fallback single LLM call, only the target method shown
3e  ExplorationAgent     AGENTIC: ReAct loop, ~13 tools, up to 40 turns
3f  Free-mode LLM        single call with carte blanche, used when 3a-3e fail
```

Why cheap to expensive? Each stage costs more in tokens and latency than the previous. Stage 3a runs in milliseconds. Stage 3e on a local CPU model takes 5 to 30 minutes. The cascade order avoids burning 30 minutes on a bug a regex would fix in 10 milliseconds.

Only stage 3e qualifies as genuinely agentic. The LLM decides what to do next, picks a tool, observes the result, iterates. The other stages ask the LLM once and accept or reject the answer.

## Data flow

Every run produces structured artifacts in runs/RUN-NNNN/run-id/:

```
correction_contract.json     what the bundle thinks the bug is, post-normalization
selected_files.json          candidate files for the cascade to focus on
diagnosis.json               high-level diagnosis (cause, exception, fix style)
localization.json            target_file plus target_method
plan_raw.txt                 raw LLM output before parsing
plan.json                    parsed plan with edits, validated
exploration_trace.json       turn-by-turn ReAct trace, when stage 3e ran
llm_libre_result.json        output of stage 3f, when stage 3f ran
proposal.json + proposal.md  MR description content
validator_report.json        score, confidence, validator reasons
result.json                  final result dict (delivery_status, MR url)
summary.json                 high-level outcome summary
run.log                      chronological log
```

The artifacts stay first class. The WebUI Pre-MR analysis tab reads them. The run history (runs/history.jsonl) indexes them.

This design gives three benefits:

1. Debuggable. When a run produces a weird MR, you see exactly what each stage decided.
2. Pausable and resumable. A stage reads the previous stage artifact and continues. Today the pipeline runs linear. The structure supports replay.
3. UI friendly. The WebUI does not parse logs to know what happened. The WebUI reads the JSON.

## The orchestrator

app/service/agent_service.py, FixAgent.run drives the whole pipeline. Today the method runs around 800 lines. Splitting into stage classes sits on the roadmap.

Conceptually the orchestrator does:

```
def run(self, options):
    contract  = self.normalize(analysis_text)              # step 1
    files     = self.select_files(contract)
    diagnosis = self.diagnose(contract, files)             # step 2
    target    = self.localize(diagnosis, files)
    sources   = self.resolve_sources(target)
    
    plan = None
    for stage in (PatternFixer, RovoApplicator, LLMPatchPlanner,
                  MethodOnlyFallback, ExplorationAgent, FreeModeLLM):
        if plan and plan.has_edits: break
        plan = stage(contract, target, sources, plan).try_fix()
    
    score, confidence = self.validate(plan)                # step 4
    return self.deliver(plan, score, confidence)           # step 5
```

The actual code stays imperative and does not use stage classes yet. The refactor sits on the roadmap.

## The agentic stage: ExplorationAgent

When stages 3a through 3d fail, ExplorationAgent kicks in. The stage runs a ReAct loop:

1. The agent sees the contract, the target file source, the conversation so far
2. The agent picks one of around 13 tools:
   - read_file(path)
   - grep(query, path)
   - find_callers(method)
   - find_implementers(interface)
   - git_log(path, n)
   - git_blame(path, line)
   - list_files(directory)
   - and a few others
3. The tool result appends to the conversation
4. Loop. Stop when the agent emits a final patch or hits the turn budget (default 40)

The tools stay read only. The agent explores the codebase as much as needed. The agent modifies files only at the end through the final edits block in the response.

Cost: each turn runs one LLM call. On a 30B local model, one ExplorationAgent run typically costs 5 to 30 LLM calls. On GPU the wait stays bearable. On CPU the wait drags.

## The free mode safety net: stage 3f

Added in v9.16.9. When the whole cascade including the agentic stage gives up, stage 3f tries one last LLM call with carte blanche:

- Input: the contract plus the source of up to 6 candidate files (8KB each)
- Instruction: do your best, no constraint on which fields to change
- Output threshold: validation_score >= AGENT_LLM_LIBRE_MIN_VALIDATION_SCORE (default 40) gets the plan adopted

When adopted, the resulting MR opens as draft with a [LLM-LIBRE] title prefix and a needs-review label. The MR description includes a clear notice. A human must validate. Better an imperfect MR to review than nothing.

## Validation

After the cascade produces a plan, the validator computes:

- validation_score: the LLM self assessment, or a heuristic fallback
- model_confidence: the LLM certainty about the diagnosis, separate from the score

These compare to two thresholds:

- AGENT_MIN_PATCH_SCORE, default 65. Below this, the plan gets rejected. The MR opens as proposition with diagnosis only.
- AGENT_MIN_AUTOCOMMIT_SCORE, default 85. Below this, the patch lands on the branch. The MR stays draft for review.

Two thresholds avoid an all or nothing failure mode. A 75/100 patch lands but stays in draft. A 50/100 patch never lands, only the diagnosis goes out.

## Delivery

When the plan passes the gates:

1. Create a new branch from the base branch
2. Apply the edits through search/replace in the target files
3. Optionally generate JUnit test stubs via app/service/test_generator.py. Java specific. Disable with AGENT_GENERATE_JUNIT_TESTS=false for other languages.
4. Commit with a structured message
5. Push to the forge
6. Create the MR via the forge API. GitLab stays the primary. GitHub support stays partial.
7. Build the rich MR description (problem, solution, RG, tests, similar incidents, risks)
8. Post a structured comment back to Jira when credentials get configured

Steps 5 to 8 stay independent. When Jira goes down, the GitLab MR still happens. When GitLab goes down, the local branch stays so a human pushes manually.

## The WebUI

webui/ holds a thin FastAPI backend plus a single page React app. CDN based. No build step required.

The backend exposes:

- GET /api/status: basic info about the running bundle
- GET /api/health: live checks (Ollama reachable, repo exists, git available)
- GET /api/history: reads runs/history.jsonl for the dashboard
- GET /api/run_detail?ticket=&run_id=: reads run artifacts for the Pre-MR analysis tab
- WS /ws/run: streams logs of check_env or batch_run subprocess invocations

The UI parses logs in real time to detect the current pipeline stage and shows a visual indicator. The mapping from log markers ([LOCALIZER], [PLAN], [LLM_LIBRE]) to stage IDs lives in webui/index.html under the STAGES constant.

## Why the bundle is not multi-agent and not MCP

A frequent question deserves a clear answer.

Multi-agent? No. The cascade stages stay pipeline functions. The stages do not dialogue. The stages pass artifacts. Only stage 3e runs an agentic loop.

MCP? No. MCP is the protocol Rovo uses to read Jira and Confluence upstream. The bundle reads what Rovo produced. The bundle does not speak MCP. The bundle tools (in agent_toolset.py) form a local custom registry, not MCP servers.

These distinctions matter when you describe the system to other engineers. Do not oversell.
