# Rovo prompt guide

This bundle does not call Atlassian or any LLM to read your Jira tickets. The bundle reads a structured analysis file from disk, analysis/issue_analysis_TICKET-NNNN.txt. You produce the file with an upstream LLM agent. Typically Atlassian Rovo (with MCP access to Jira and Confluence) or any other capable model (Claude, GPT-4).

The prompt driving the upstream LLM lives in docs/prompt_rovo_v1.4.4.md. This guide explains how to use the prompt.

## Input format the bundle expects

Each analysis file has two parts, in this order:

```
<free form description of the bug, in plain text>
<the structured JSON block produced by the prompt v1.4.4>
```

The bundle extracts the JSON block automatically. The extractor tolerates whitespace and a Markdown code fence around the JSON. When the JSON stays malformed, the bundle still runs with degraded context. Quality drops fast.

See examples/issue_analysis_TICKET-1234.txt for a complete sample.

## The prompt at a glance

docs/prompt_rovo_v1.4.4.md holds the system and user instructions you give to your upstream LLM. The instructions tell the model to produce a single JSON object with this schema:

```json
{
  "schema_version": "1.4.4",
  "ticket_key": "RUN-1234",
  "diagnosis": { "immediate_cause": "...", "root_cause_hypothesis": "...", "confidence_level": "high" },
  "crash": { "exception_class": null, "crash_file": null, "crash_method": null },
  "fix_recommendation": { "fix_style": "business_logic_fix", "approach_summary": "...", "rationale_business": "..." },
  "must_touch": ["src/main/java/.../Foo.java"],
  "must_touch_method": "doSomething",
  "repo_hint": "back",
  "investigation_candidates": [ { "rank": 1, "location": "...", "file_hint": "...", "reasoning": "..." } ],
  "rules_of_business": [ { "reference": "DOMAIN.RG001", "summary_extract": "...", "confluence_url": "https://...", "validation_required": "non_regression" } ],
  "acceptance_criteria": [ { "criterion": "...", "expected_outcome": "..." } ],
  "tests_to_add": [ { "scenario": "...", "test_class_hint": "...", "validates_rg": ["DOMAIN.RG001"] } ],
  "similar_past_incidents": [],
  "risks_and_uncertainties": []
}
```

The full prompt has 10 enforcement rules (no markdown wrapping, no placeholder objects, real URLs only). Read docs/prompt_rovo_v1.4.4.md for the details.

## Setting up Rovo

To use Atlassian Rovo:

1. Open the Rovo agent in your Atlassian instance
2. Create a new agent, e.g. "Bug Analyst for autofix-agent"
3. Paste the content of docs/prompt_rovo_v1.4.4.md as the agent instructions
4. Enable Jira and Confluence MCP tool access so Rovo reads tickets and looks up business rule pages
5. To analyze a ticket, ask the agent: "Analyze RUN-1234 following your instructions"
6. Copy the agent output. Save to analysis/issue_analysis_TICKET-1234.txt

You scale this with a custom integration batching many tickets at once.

## Using Claude, GPT-4, or another model

The prompt stays model agnostic. Steps:

1. Open your favorite chat interface (Claude.ai, ChatGPT)
2. Paste docs/prompt_rovo_v1.4.4.md as the system prompt or as the first message
3. Paste the ticket content (description, comments, stack trace) as the user message
4. Copy the JSON output

Without MCP access to Confluence, the model does not actively look up business rule URLs (Rule 8 in the prompt). The model returns null for confluence_url more often. A degradation. Not a blocker. The bundle still works. The BA looks up the RG URLs manually.

## What good looks like

A high quality analysis has:

- must_touch and must_touch_method filled with real paths and method names from the codebase
- repo_hint correctly identifying whether the bug lives in the backend, frontend, or batch (helps the bundle target the right code)
- rules_of_business with full references AND Confluence URLs (the BA decides what TNRs to run from the Jira comment alone)
- tests_to_add with validates_rg pointing back to entries in rules_of_business so the BA sees the matrix of test to business rule
- similar_past_incidents with real ticket_key values when applicable (the bundle pattern fixer stage 3a applies known patterns)

A low quality analysis has:

- must_touch empty and file_hint null everywhere. The bundle has to explore blindly via stage 3e.
- repo_hint missing. The bundle targets the wrong repo.
- rules_of_business empty. No TNR identified. The BA redoes the analysis manually.

Invest time in your upstream prompt and provider. Bundle quality depends heavily on what comes in.

## Schema versioning

The prompt version maps to a schema_version in the JSON output:

- v1.4.4: adds validates_rg, active Confluence search rules
- v1.4.3: stricter format, no phantom RG rows
- earlier 1.4.x: legacy, works but lacks fields

The bundle stays forward compatible. A v1.4.3 analysis runs fine. The new validates_rg field stays absent from the Jira comment.

## Adapting the prompt to your stack

The prompt mentions Java and Spring conventions because the bundle was originally developed against a Java codebase. The bundle and the prompt schema (schema_version 1.4.4) stay non Java specific. The JSON keys (must_touch, must_touch_method, fix_style) map to any language concepts.

To adapt the bundle for another stack (Python, TypeScript, Go, Kotlin):

- Change must_touch_method semantics to your language convention (function name, exported symbol, module level callable)
- Adjust the fix_style enum to your typical bug categories. Python adds dict_key_access_guard, type_check_added.
- Update the prompt examples. Today all Java.

A reasonable starting point: copy prompt_rovo_v1.4.4.md to prompt_rovo_v1.4.4-<your-language>.md. Keep the JSON schema as is. The schema stays language agnostic. Rewrite only the explanatory text and examples. See docs/extending.md for the full extension story.
