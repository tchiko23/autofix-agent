"""app.service.exploration_agent — Agent autonome de fouille v9.14.

Architecture ReAct (Reasoning + Acting) :
  Tour N : LLM raisonne -> propose un appel d'outil -> sandbox execute -> resultat injecte -> tour N+1
  Tour final : LLM produit un PLAN JSON applicable (edits + score)

Le LLM dispose de 11 outils en lecture seule (voir agent_toolset.py).
La boucle s'arrete quand :
  - LLM declare avoir trouve la solution (action=final_answer)
  - Limite de tours atteinte (sandbox)
  - Budget tokens depasse (sandbox)
  - LLM declare ne pas pouvoir trouver (unable_to_explore=true)
  - Exception non recuperable
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.service.agent_sandbox import AgentSandbox, SandboxConfig, SandboxLimitExceeded, SandboxViolation
from app.service.agent_toolset import AgentToolset, ToolResult

logger = logging.getLogger("autofix-agent.exploration")


@dataclass
class ExplorationResult:
    """Resultat final de l'agent."""
    success: bool
    plan: dict[str, Any] = field(default_factory=dict)
    exploration_trace: list[dict] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    rejection_reason: str = ""
    validation_score: int = 0


# =========================================================================
# Prompt systeme de l'agent
# =========================================================================

AGENT_SYSTEM_PROMPT = """Tu es un agent autonome de correction de bugs Java/Spring.

Ta mission : explorer un repo Java/Spring pour identifier la zone defaillante
d'un bug, puis proposer un correctif JSON applicable.

## Outils disponibles (lecture seule)

A chaque tour, tu reponds AVEC EXACTEMENT UNE ACTION au format JSON :

```json
{
  "thought": "<ton raisonnement de 1-3 phrases>",
  "action": "<nom_outil>",
  "args": {"...": "..."}
}
```

### Outils de lecture

- `read_file(path)` : lit un fichier complet (max 100Ko)
- `read_file_range(path, start_line, end_line)` : lit lignes [start, end]
- `list_dir(path)` : liste un repertoire
- `find_files(pattern, base_path)` : trouve fichiers par glob (ex: *Service*.java)

### Outils de recherche

- `grep(pattern, path_glob)` : cherche un regex dans fichiers
- `grep_method(method_name)` : cherche definitions/appels d'une methode Java
- `find_callers(method_name)` : trouve les appelants d'une methode
- `find_implementations(interface_name)` : trouve les implementations d'une interface

### Outils Git (historique)

- `git_log(path, max_entries=10)` : historique des commits sur un fichier
- `git_blame(path, line)` : auteur d'une ligne (anonymise)
- `git_show(commit_sha)` : diff d'un commit

### Action finale

Quand tu as identifie le bug ET sa correction :

```json
{
  "thought": "<resume de ton analyse>",
  "action": "final_answer",
  "args": {
    "exploration_summary": "<ce que tu as decouvert en 2-3 phrases>",
    "root_cause": "<la cause precise du bug>",
    "fix_summary": "<description du correctif>",
    "edits": [
      {
        "file": "<chemin/relatif/au/repo/X.java>",
        "search": "",
        "replace": "<methode Java complete corrigee>",
        "replace_strategy": "whole_method",
        "match_hint_method": "<nom_methode>",
        "justification": "<pourquoi cette modification>"
      }
    ],
    "files_to_change": ["<chemin/X.java>"],
    "endpoint_concerned": "<si tu as identifie l'endpoint REST en cascade, ex: POST /api/x/y>",
    "methods_traced": ["<liste des methodes traversees en cascade>"],
    "validation_score": <int 0-100>
  }
}
```

### Si tu ne peux vraiment pas trouver

```json
{
  "thought": "<raison de l'abandon>",
  "action": "unable_to_explore",
  "args": {"reason": "<explication>"}
}
```

## Strategie d'exploration recommandee

1. Lis d'abord le ticket et les candidats fournis (rovo_investigation_candidates)
2. Si un endpoint REST est mentionne dans les RG : trace la cascade
   controller -> service -> repository -> DB depuis cet endpoint
3. Sinon, commence par grep_method sur les methodes candidates
4. Examine la signature et le code des methodes suspectes
5. Identifie precisement OU le bug se produit (ligne + raison)
6. Propose un patch minimal et chirurgical

## Regles strictes

- UNE SEULE ACTION par reponse (pas de plusieurs outils en parallele)
- Reponse JSON pure, pas de markdown autour
- Si tu trouves la zone : final_answer avec edits applicables
- Si tu epuises les pistes : unable_to_explore avec raison claire
"""


def build_agent_initial_prompt(
    *,
    ticket_key: str,
    immediate_cause: str,
    root_cause_hypothesis: str,
    investigation_candidates: list[dict],
    endpoint_hint: str = "",
    crash_method: str = "",
    must_touch: list[str] = None,
) -> str:
    """Construit le prompt initial avec le contexte du ticket."""
    must_touch = must_touch or []
    lines = [
        AGENT_SYSTEM_PROMPT,
        "",
        "---",
        "",
        f"# Ticket a investiguer : {ticket_key}",
        "",
        "## Description du probleme",
    ]
    if immediate_cause:
        lines.append(f"**Cause immediate** : {immediate_cause[:500]}")
    if root_cause_hypothesis:
        lines.append(f"**Hypothese racine** : {root_cause_hypothesis[:500]}")
    lines.append("")

    if endpoint_hint:
        lines.append(f"## Endpoint REST concerne (point d'entree des RG)")
        lines.append(f"`{endpoint_hint}`")
        lines.append("**Conseil** : trace la cascade depuis cet endpoint vers le code metier.")
        lines.append("")

    if crash_method:
        lines.append(f"## Methode signalee dans la stack trace")
        lines.append(f"`{crash_method}`")
        lines.append("")

    if investigation_candidates:
        lines.append("## Candidats a investiguer (par probabilite decroissante)")
        for c in investigation_candidates[:10]:
            rank = c.get("rank", "?")
            loc = c.get("location", "?")
            prob = c.get("probability", "?")
            reasoning = c.get("reasoning", "")
            lines.append(f"- rank {rank} ({prob}) : `{loc}` — {reasoning[:200]}")
        lines.append("")

    if must_touch:
        lines.append("## Fichiers cites comme modifiables")
        for f in must_touch[:8]:
            lines.append(f"- `{f}`")
        lines.append("")

    lines.extend([
        "## Action attendue",
        "",
        "Commence par explorer. Reponds avec UN SEUL JSON contenant `thought` + `action` + `args`.",
        "",
    ])
    return "\n".join(lines)


# =========================================================================
# Parsing de la reponse LLM
# =========================================================================

def parse_agent_response(raw: str) -> dict | None:
    """Extrait le JSON de la reponse LLM (tolerant aux ```json...``` et au texte autour)."""
    if not raw:
        return None
    # Tenter parsing direct
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    # Bloc ```json...```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    # Objet JSON brut
    m2 = re.search(r"(\{(?:[^{}]|\{[^{}]*\})*\})", raw, re.DOTALL)
    if m2:
        try:
            return json.loads(m2.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# =========================================================================
# Dispatch des outils
# =========================================================================

def dispatch_tool(tool_name: str, args: dict, toolset: AgentToolset) -> ToolResult:
    """Appelle l'outil correspondant via le toolset."""
    if not isinstance(args, dict):
        return ToolResult(False, "", {}, error="args_not_a_dict")
    dispatch_map = {
        "read_file": lambda: toolset.read_file(args.get("path", "")),
        "read_file_range": lambda: toolset.read_file_range(
            args.get("path", ""), int(args.get("start_line", 1) or 1), int(args.get("end_line", 100) or 100),
        ),
        "list_dir": lambda: toolset.list_dir(args.get("path", ".")),
        "find_files": lambda: toolset.find_files(args.get("pattern", "*.java"), args.get("base_path", ".")),
        "grep": lambda: toolset.grep(args.get("pattern", ""), args.get("path_glob", "*.java")),
        "grep_method": lambda: toolset.grep_method(args.get("method_name", "")),
        "find_callers": lambda: toolset.find_callers(args.get("method_name", "")),
        "find_implementations": lambda: toolset.find_implementations(args.get("interface_name", "")),
        "git_log": lambda: toolset.git_log(args.get("path", ""), int(args.get("max_entries", 10) or 10)),
        "git_blame": lambda: toolset.git_blame(args.get("path", ""), int(args.get("line", 1) or 1)),
        "git_show": lambda: toolset.git_show(args.get("commit_sha", "")),
    }
    handler = dispatch_map.get(tool_name)
    if not handler:
        return ToolResult(False, "", {}, error=f"unknown_tool: {tool_name}")
    try:
        return handler()
    except SandboxLimitExceeded:
        raise  # propagate to stop the loop
    except Exception as exc:
        return ToolResult(False, "", {}, error=f"tool_exception: {exc}")


# =========================================================================
# Boucle d'exploration principale
# =========================================================================

def run_exploration_agent(
    *,
    llm,
    repo_root: Path,
    ticket_key: str,
    immediate_cause: str = "",
    root_cause_hypothesis: str = "",
    investigation_candidates: list[dict] = None,
    endpoint_hint: str = "",
    crash_method: str = "",
    must_touch: list[str] = None,
    max_turns: int = 20,
    min_validation_score: int = 40,
    logger_fn=print,
    trace_save_fn=None,
) -> ExplorationResult:
    """Lance la boucle d'exploration agent.

    Args:
        llm: instance de OllamaPlannerClient avec methode .generate_text(prompt)
        repo_root: racine du repo local (back/your-repo)
        ticket_key: RUN-XXXXX
        immediate_cause, root_cause_hypothesis: extraits du contrat Rovo
        investigation_candidates: liste de candidats hierarchises (directive C v9.14)
        endpoint_hint: endpoint REST cite dans les RG Confluence (directive C v9.14)
        crash_method: methode dans la stack trace
        must_touch: fichiers cites par Rovo
        max_turns: limite de tours d'exploration (20 par defaut, 40 en mode demo)
        min_validation_score: seuil min pour accepter le patch final (40 par defaut)
        logger_fn: fonction de logging
        trace_save_fn: v9.16 callback optionnel appele apres CHAQUE tour avec
                       le dict de trace courant. Permet la sauvegarde
                       incrementale pour ne rien perdre si un run de
                       plusieurs heures plante en cours de route.

    Returns:
        ExplorationResult avec plan + exploration_trace si succes
    """
    investigation_candidates = investigation_candidates or []
    must_touch = must_touch or []

    # Initialiser sandbox + toolset
    sandbox_config = SandboxConfig(
        repo_root=repo_root,
        max_exploration_turns=max_turns,
    )
    sandbox = AgentSandbox(sandbox_config)
    toolset = AgentToolset(sandbox)

    # Prompt initial
    initial_prompt = build_agent_initial_prompt(
        ticket_key=ticket_key, immediate_cause=immediate_cause,
        root_cause_hypothesis=root_cause_hypothesis,
        investigation_candidates=investigation_candidates,
        endpoint_hint=endpoint_hint, crash_method=crash_method,
        must_touch=must_touch,
    )

    logger_fn(f"[AGENT] starting exploration for {ticket_key} (max_turns={max_turns})")
    logger_fn(f"[AGENT] initial prompt size: {len(initial_prompt)} chars")
    logger_fn(f"[AGENT] candidates: {len(investigation_candidates)}, endpoint: {endpoint_hint or '(none)'}")

    conversation = [{"role": "user", "content": initial_prompt}]
    final_plan: dict | None = None
    last_partial_plan: dict | None = None  # Pour directive D : utiliser le dernier plan partiel si limit atteinte
    rejection_reason = ""
    consecutive_parse_failures = 0  # v9.16.2 : garde-fou anti-gaspillage

    for turn in range(1, max_turns + 1):
        try:
            sandbox.consume_turn()
        except SandboxLimitExceeded as exc:
            logger_fn(f"[AGENT] sandbox limit: {exc}")
            rejection_reason = str(exc)
            break

        logger_fn(f"[AGENT] turn {turn}/{max_turns} ...")

        # Construire le prompt complet (concatenation des tours)
        full_prompt = _build_turn_prompt(conversation)

        # Appel LLM
        try:
            raw = llm.generate_text(full_prompt)
        except Exception as exc:
            logger_fn(f"[AGENT] llm_call_failed at turn {turn}: {exc}")
            rejection_reason = f"llm_failure_turn_{turn}: {exc}"
            break

        # Parser la reponse
        parsed = parse_agent_response(raw)
        if not parsed:
            consecutive_parse_failures += 1
            logger_fn(
                f"[AGENT] turn {turn}: response not parseable "
                f"(consecutive={consecutive_parse_failures}/3)"
            )
            # v9.16.2 : garde-fou anti-gaspillage. Si le modele rate le format
            # JSON 3 fois DE SUITE, inutile de continuer 15 tours de plus a
            # tourner dans le vide (observe TICKET-NNNNN : 11 tours gaspilles).
            # On arrete et on laisse la cascade produire une MR de proposition.
            if consecutive_parse_failures >= 3:
                logger_fn(
                    "[AGENT] aborting: 3 consecutive unparseable responses. "
                    "Le modele n'arrive pas a tenir le format JSON requis."
                )
                rejection_reason = "model_cannot_hold_json_format_3x"
                break
            conversation.append({"role": "assistant", "content": raw[:500]})
            conversation.append({"role": "user", "content": (
                "Ta reponse n'etait pas un JSON valide. Reponds STRICTEMENT au format "
                "`{\"thought\": \"...\", \"action\": \"...\", \"args\": {...}}` sans markdown."
            )})
            continue

        # Reponse parseable : on remet le compteur a zero
        consecutive_parse_failures = 0

        action = parsed.get("action", "")
        args = parsed.get("args", {}) if isinstance(parsed.get("args"), dict) else {}
        thought = parsed.get("thought", "")[:300]

        logger_fn(f"[AGENT] turn {turn}: action={action}, thought={thought[:100]}")

        # Action finale
        if action == "final_answer":
            final_plan = args
            logger_fn(f"[AGENT] turn {turn}: agent declared final_answer")
            break

        # Abandon
        if action == "unable_to_explore":
            reason = args.get("reason", "no_reason_given")
            logger_fn(f"[AGENT] turn {turn}: agent gave up: {reason}")
            rejection_reason = f"agent_unable_to_explore: {reason}"
            break

        # Garde partielle : si l'agent a deja construit un plan en chemin
        if "edits" in args and args.get("edits"):
            last_partial_plan = args

        # Executer l'outil
        try:
            tool_result = dispatch_tool(action, args, toolset)
        except SandboxLimitExceeded as exc:
            logger_fn(f"[AGENT] turn {turn}: sandbox limit reached: {exc}")
            rejection_reason = str(exc)
            break

        # Construire la reponse de tour
        if tool_result.success:
            obs = f"## Resultat de `{action}` (succes)\n```\n{tool_result.output[:3000]}\n```"
        else:
            obs = f"## Resultat de `{action}` (echec)\nErreur : {tool_result.error}"

        conversation.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
        conversation.append({"role": "user", "content": obs})

        # v9.16 : sauvegarde incrementale apres CHAQUE tour.
        # Sur un run de plusieurs heures, si Ollama ou la VM crashe, on garde
        # la trace de tout ce qui a ete fait jusque-la (precieux pour analyse).
        if trace_save_fn is not None:
            try:
                partial_summary = sandbox.summary()
                trace_save_fn({
                    "status": "in_progress",
                    "turns_completed": turn,
                    "summary": partial_summary,
                    "operations": partial_summary.get("operations_log", []),
                })
            except Exception as save_exc:
                logger_fn(f"[AGENT] trace save warning (non-fatal): {save_exc}")

    # Si pas de final_answer mais qu'on a un plan partiel, on l'utilise (directive D)
    if not final_plan and last_partial_plan:
        logger_fn("[AGENT] using last partial plan (limit reached without final_answer)")
        final_plan = last_partial_plan
        rejection_reason = rejection_reason or "max_turns_reached_using_partial_plan"

    summary = sandbox.summary()

    if not final_plan or not final_plan.get("edits"):
        return ExplorationResult(
            success=False,
            exploration_trace=summary["operations_log"],
            summary=summary,
            rejection_reason=rejection_reason or "no_plan_produced",
        )

    # Validation du score
    score = final_plan.get("validation_score", 0)
    if not isinstance(score, (int, float)):
        score = 50  # fallback
    score = int(score)

    if score < min_validation_score:
        return ExplorationResult(
            success=False,
            plan=final_plan,
            exploration_trace=summary["operations_log"],
            summary=summary,
            rejection_reason=f"score_too_low:{score}<{min_validation_score}",
            validation_score=score,
        )

    # Marquer le plan comme issu de l'agent (utilise par mr builder)
    final_plan["agent_explored"] = True
    final_plan["validation_score"] = score
    risk_notes = list(final_plan.get("risk_notes", []))
    if not any("agent" in str(r).lower() for r in risk_notes):
        risk_notes.append("Patch issu d'une exploration agent autonome v9.14 : review humaine OBLIGATOIRE")
    final_plan["risk_notes"] = risk_notes

    return ExplorationResult(
        success=True,
        plan=final_plan,
        exploration_trace=summary["operations_log"],
        summary=summary,
        validation_score=score,
    )


def _build_turn_prompt(conversation: list[dict]) -> str:
    """Construit le prompt LLM par concatenation des tours.

    Format simple : on alterne USER / ASSISTANT en texte plat. Ollama API
    accepte cela en mode /api/generate.
    """
    parts = []
    for msg in conversation:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        prefix = "USER:" if role == "user" else "ASSISTANT:"
        parts.append(f"{prefix}\n{content}\n")
    parts.append("ASSISTANT:\n")  # Marqueur de relance
    return "\n".join(parts)
