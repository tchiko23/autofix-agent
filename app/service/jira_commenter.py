"""app.service.jira_commenter — Boucle de retour vers Jira v9.16.7.

Apres la creation d'une MR GitLab (correctif ou proposition), poste un
commentaire structure sur le ticket Jira d'origine pour fermer la boucle
Jira -> GitLab -> Jira. Permet aux Business Analysts qui vivent dans Jira
de voir l'analyse automatique sans aller dans GitLab.

Configuration via templates_external_config/config.env + secrets.env :
  JIRA_BASE_URL=https://your-company.atlassian.net
  JIRA_USER=<email Atlassian>
  JIRA_TOKEN=<API token>          (dans secrets.env)
  JIRA_COMMENT_ENABLED=true       (defaut : true si credentials presents)
  JIRA_NOTIFY_ON_COMMENT=true     (defaut : true)

Decisions de design (Q1-Q5 validees) :
  Q1 : commentaire structure 5 sections (sans section EXECUTION)
  Q2 : RG sous forme de tableau avec URL ou 'absent'
  Q3 : commenter UNIQUEMENT si MR creee (correctif ou proposition)
  Q4 : idempotence par run_id (skip si commentaire deja poste)
  Q5 : notifications activees par defaut
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger("autofix-agent.jira_commenter")

# Tag unique en debut de commentaire pour permettre la detection d'idempotence
TAG_PREFIX = "[autofix-agent"
# v1.4 OSS : detect both prior names so old comments stay recognized.
LEGACY_TAG_PREFIXES = ("[fix-anomalie-agent", "[local-fix-agent")


@dataclass
class JiraConfig:
    """Configuration Jira lue depuis l'environnement."""

    base_url: str = ""
    user: str = ""
    token: str = ""
    enabled: bool = True
    notify: bool = True

    @classmethod
    def from_env(cls) -> "JiraConfig":
        return cls(
            base_url=(os.environ.get("JIRA_BASE_URL") or "").strip().rstrip("/"),
            user=(os.environ.get("JIRA_USER") or "").strip(),
            token=(os.environ.get("JIRA_TOKEN") or "").strip(),
            enabled=_to_bool(os.environ.get("JIRA_COMMENT_ENABLED"), True),
            notify=_to_bool(os.environ.get("JIRA_NOTIFY_ON_COMMENT"), True),
        )

    def is_usable(self) -> tuple[bool, str]:
        """Retourne (utilisable, raison_si_non).

        Le bundle ne tente pas de commenter si manque credentials, mais
        ne plante pas non plus. Log explicite.
        """
        if not self.enabled:
            return False, "JIRA_COMMENT_ENABLED=false"
        if not self.base_url:
            return False, "JIRA_BASE_URL vide"
        if not self.user:
            return False, "JIRA_USER vide"
        if not self.token:
            return False, "JIRA_TOKEN vide"
        return True, ""


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class CommentPayload:
    """Donnees structurees pour composer le commentaire."""

    ticket_key: str
    run_id: str
    mr_url: str
    mr_branch: str
    mr_kind: str  # "correctif" | "proposition"
    rovo_json: dict[str, Any] = field(default_factory=dict)
    hint_used: bool = False
    # v9.16.11 : contract et plan permettent au commentaire de mirroir la
    # description MR GitLab (sections SOLUTION, RISQUES, INCIDENTS SIMILAIRES,
    # details du crash). Optionnels pour la retro-compatibilite : si absents,
    # le commentaire degrade gracieusement vers la version v9.16.10.
    contract: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)


class JiraCommenterError(Exception):
    """Erreur du commenter Jira."""


class JiraCommenter:
    """Client minimal pour poster un commentaire sur Jira Cloud."""

    def __init__(self, config: JiraConfig):
        self.config = config

    def has_run_comment(self, ticket_key: str, run_id: str) -> bool:
        """Q4 : verifie si un commentaire pour ce run_id existe deja.

        Lit les commentaires du ticket et cherche le tag [local-fix-agent
        run_id=<run_id>]. Si trouve, on ne reposte pas.
        """
        try:
            url = f"{self.config.base_url}/rest/api/3/issue/{ticket_key}/comment"
            response = requests.get(
                url,
                auth=(self.config.user, self.config.token),
                headers={"Accept": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            logger.warning(f"could not list comments for {ticket_key}: {exc}")
            # Par prudence, on dit non-existant pour ne pas bloquer le post
            return False

        marker = f"run_id={run_id}"
        # v9.16.10 : reconnait nouveau ET ancien prefixe (retro-compat).
        accepted_prefixes = (TAG_PREFIX, *LEGACY_TAG_PREFIXES)
        for comment in data.get("comments", []):
            # Le contenu peut etre en ADF (Atlassian Document Format) ou texte plat
            body = comment.get("body", "")
            if isinstance(body, dict):
                # ADF : on cherche dans le texte concatene
                body_text = _flatten_adf(body)
            else:
                body_text = str(body)
            if any(p in body_text for p in accepted_prefixes) and marker in body_text:
                return True
        return False

    def post_run_comment(self, payload: CommentPayload) -> str | None:
        """Poste le commentaire. Retourne l'URL du commentaire ou None.

        Idempotence : si un commentaire existe deja pour ce run_id, ne reposte
        pas. Retourne None silencieusement.
        """
        if self.has_run_comment(payload.ticket_key, payload.run_id):
            logger.info(
                f"[JIRA] comment for run_id={payload.run_id} already exists "
                f"on {payload.ticket_key}, skipping (idempotence Q4)"
            )
            return None

        body_text = build_comment_body(payload)
        adf_body = _text_to_adf(body_text)
        url = f"{self.config.base_url}/rest/api/3/issue/{payload.ticket_key}/comment"
        if not self.config.notify:
            url = url + "?notifyUsers=false"

        try:
            response = requests.post(
                url,
                auth=(self.config.user, self.config.token),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"body": adf_body},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise JiraCommenterError(f"network error posting to Jira: {exc}") from exc

        if response.status_code not in (200, 201):
            body_excerpt = (response.text or "")[:300]
            raise JiraCommenterError(
                f"jira returned HTTP {response.status_code} for {payload.ticket_key}: "
                f"{body_excerpt}"
            )

        data = response.json() if response.text else {}
        comment_id = data.get("id", "")
        web_url = f"{self.config.base_url}/browse/{payload.ticket_key}"
        if comment_id:
            web_url = f"{web_url}?focusedCommentId={comment_id}"
        return web_url


def _flatten_adf(adf_node: Any) -> str:
    """Extrait recursivement tout le texte d'un noeud ADF."""
    if isinstance(adf_node, dict):
        if adf_node.get("type") == "text":
            return adf_node.get("text", "")
        content = adf_node.get("content", [])
        return "".join(_flatten_adf(c) for c in content)
    if isinstance(adf_node, list):
        return "".join(_flatten_adf(c) for c in adf_node)
    return ""


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _line_to_inline_nodes(line: str) -> list[dict[str, Any]]:
    """Convertit une ligne en noeuds ADF inline, en parsant `**...**` comme gras.

    Exemple : `pre **bold** post` -> [text(pre ), text(bold, strong), text( post)].
    Si pas de marqueur, retourne un seul noeud text simple. Garde le rendu
    Markdown-like compatible avec le reste de l'IHM et lisible en texte brut
    (les `**` apparaissent dans le source mais Jira les masque grace aux marks).
    """
    nodes: list[dict[str, Any]] = []
    pos = 0
    for m in _BOLD_RE.finditer(line):
        if m.start() > pos:
            nodes.append({"type": "text", "text": line[pos:m.start()]})
        nodes.append({
            "type": "text",
            "text": m.group(1),
            "marks": [{"type": "strong"}],
        })
        pos = m.end()
    if pos < len(line):
        nodes.append({"type": "text", "text": line[pos:]})
    if not nodes:
        nodes.append({"type": "text", "text": line})
    return nodes


def _text_to_adf(text: str) -> dict[str, Any]:
    """Convertit du texte plain en ADF minimal pour Jira Cloud.

    Chaque ligne devient un paragraphe ; les lignes vides creent des
    paragraphes vides (espacement). v9.16.10 : parse `**...**` comme gras
    (marks ADF strong) pour les titres de section ; le reste reste plain text.
    """
    lines = text.split("\n")
    content = []
    for line in lines:
        if line.strip():
            content.append({
                "type": "paragraph",
                "content": _line_to_inline_nodes(line),
            })
        else:
            content.append({"type": "paragraph"})
    return {
        "type": "doc",
        "version": 1,
        "content": content,
    }


def build_comment_body(payload: CommentPayload) -> str:
    """Construit le corps du commentaire selon le format Q1 valide.

    Sections (v9.16.11 : mirroir de la description MR GitLab pour que le BA
    ait toute l'analyse sans aller sur GitLab) :
      - Tag d'identification (idempotence)
      - RESUME (cause + symptome technique)
      - SOLUTION DETAILLEE (approche, methode, resume du correctif, justification)
      - MERGE REQUEST (lien GitLab)
      - REGLES DE GESTION IMPACTEES (tableau RG | URL ou 'absent') - Q2
      - CRITERES D'ACCEPTATION
      - TESTS A AJOUTER
      - INCIDENTS SIMILAIRES (si Rovo a identifie)
      - RISQUES ET INCERTITUDES (si Rovo a remonte)
    Les sections SOLUTION DETAILLEE / INCIDENTS / RISQUES sont silencieusement
    omises si les donnees correspondantes ne sont pas fournies.
    """
    lines: list[str] = []
    rovo = payload.rovo_json or {}
    contract = payload.contract or {}
    plan = payload.plan or {}

    # 1. Tag d'identification - permet la detection d'idempotence Q4
    lines.append(f"{TAG_PREFIX} run_id={payload.run_id}]")
    lines.append("Analyse automatique generee par autofix-agent.")
    lines.append("")

    # 2. RESUME (enrichi v9.16.11 avec le symptome technique du contract)
    diagnosis = rovo.get("diagnosis") or {}
    fix_reco = rovo.get("fix_recommendation") or {}
    lines.append("**=== RESUME ===**")
    # Symptome technique : exception + fichier/methode/ligne (depuis contract)
    exception_class = (contract.get("exception_class") or "").strip()
    crash_file = (contract.get("crash_file") or "").strip()
    crash_method = (contract.get("crash_method") or contract.get("must_touch_method") or "").strip()
    crash_line = str(contract.get("crash_line") or "").strip()
    if exception_class:
        lines.append(f"- Exception : {exception_class}")
    if crash_file:
        loc = crash_file
        if crash_method:
            loc += f" -> {crash_method}()"
        if crash_line:
            loc += f" (ligne {crash_line})"
        lines.append(f"- Localisation : {loc}")
    immediate = (diagnosis.get("immediate_cause") or "").strip()
    if immediate:
        lines.append(f"- Cause immediate : {immediate}")
    root_cause = (diagnosis.get("root_cause_hypothesis") or "").strip()
    if root_cause:
        lines.append(f"- Cause racine probable : {root_cause}")
    confidence = (diagnosis.get("confidence_level") or "").strip()
    if confidence:
        lines.append(f"- Confiance : {confidence}")
    fix_style = (fix_reco.get("fix_style") or "").strip()
    if fix_style:
        lines.append(f"- Type de fix : {fix_style}")
    mode_label = "guidage expert" if payload.hint_used else "exploration automatique"
    lines.append(f"- Mode : {mode_label}")
    lines.append("")

    # 3. SOLUTION DETAILLEE (NOUVEAU v9.16.11 - mirror MR section "Proposition")
    solution_lines = _build_solution_block(contract, plan, fix_reco)
    if solution_lines:
        lines.append("**=== SOLUTION DETAILLEE ===**")
        lines.extend(solution_lines)
        lines.append("")

    # 4. MERGE REQUEST
    lines.append("**=== MERGE REQUEST ===**")
    lines.append(f"- Type : {payload.mr_kind}")
    if payload.mr_branch:
        lines.append(f"- Branche : {payload.mr_branch}")
    if payload.mr_url:
        lines.append(f"- URL : {payload.mr_url}")
    lines.append("- Statut : Draft (a reviewer)")
    lines.append("")

    # 5. REGLES DE GESTION IMPACTEES - format tableau (Q2)
    rules = rovo.get("rules_of_business") or []
    if rules:
        lines.append("**=== REGLES DE GESTION IMPACTEES (TNR a executer) ===**")
        rg_w = max(len("Reference"), max((len(r.get("reference") or "") for r in rules), default=0))
        val_w = max(len("Validation"), max(
            (len(r.get("validation_required") or "") for r in rules), default=0))
        lines.append(f"| {'Reference'.ljust(rg_w)} | {'Validation'.ljust(val_w)} | URL Confluence |")
        lines.append(f"|{'-' * (rg_w + 2)}|{'-' * (val_w + 2)}|----------------|")
        for r in rules:
            ref = (r.get("reference") or "").strip()
            val = (r.get("validation_required") or "").strip()
            url = (r.get("confluence_url") or "").strip()
            if url and url.lower().startswith(("http://", "https://")):
                url_cell = url
            else:
                url_cell = "absent"
            lines.append(f"| {ref.ljust(rg_w)} | {val.ljust(val_w)} | {url_cell} |")
        lines.append("")

    # 6. CRITERES D'ACCEPTATION
    criteria = rovo.get("acceptance_criteria") or []
    if criteria:
        lines.append("**=== CRITERES D'ACCEPTATION ===**")
        for c in criteria:
            crit = (c.get("criterion") or "").strip()
            outcome = (c.get("expected_outcome") or "").strip()
            if crit and outcome:
                lines.append(f"- {crit} -> {outcome}")
            elif crit:
                lines.append(f"- {crit}")
        lines.append("")

    # 7. TESTS A AJOUTER
    tests = rovo.get("tests_to_add") or []
    if tests:
        lines.append("**=== TESTS A AJOUTER ===**")
        for t in tests:
            cls = (t.get("test_class_hint") or "").strip()
            mth = (t.get("test_method_hint") or "").strip()
            typ = (t.get("test_type") or "").strip()
            scen = (t.get("scenario") or "").strip()
            validates_rg = t.get("validates_rg") or []
            validates_rg = [str(r).strip() for r in validates_rg if str(r).strip()]
            label_parts = []
            if cls and mth:
                label_parts.append(f"{cls}.{mth}")
            elif cls:
                label_parts.append(cls)
            if typ:
                label_parts.append(f"({typ})")
            label = " ".join(label_parts) or scen
            line = f"- {label}"
            if scen and label != scen:
                line += f" : {scen}"
            if validates_rg:
                line += f"  [valide : {', '.join(validates_rg)}]"
            lines.append(line)
        lines.append("")

    # 8. INCIDENTS SIMILAIRES (NOUVEAU v9.16.11 - mirror MR section)
    similar = rovo.get("similar_past_incidents") or []
    similar = [s for s in similar if isinstance(s, dict) and (s.get("ticket_key") or "").strip()]
    if similar:
        lines.append("**=== INCIDENTS SIMILAIRES ===**")
        for s in similar[:5]:  # cap : 5 max pour eviter la pollution
            tk = (s.get("ticket_key") or "").strip()
            reason = (s.get("similarity_reason") or "").strip()
            pattern = (s.get("fix_pattern_applied") or "").strip()
            line = f"- {tk}"
            if reason:
                line += f" : {reason}"
            if pattern:
                line += f"  [pattern : {pattern}]"
            lines.append(line)
        lines.append("")

    # 9. RISQUES ET INCERTITUDES (NOUVEAU v9.16.11 - mirror MR section)
    risks = rovo.get("risks_and_uncertainties") or []
    risks = [r for r in risks if isinstance(r, dict) and (r.get("description") or "").strip()]
    if risks:
        lines.append("**=== RISQUES ET INCERTITUDES ===**")
        for r in risks[:8]:  # cap : 8 max
            cat = (r.get("category") or "").strip()
            sev = (r.get("severity") or "").strip()
            desc = (r.get("description") or "").strip()
            prefix_parts = []
            if cat:
                prefix_parts.append(cat)
            if sev:
                prefix_parts.append(f"severite={sev}")
            prefix = f"[{', '.join(prefix_parts)}] " if prefix_parts else ""
            lines.append(f"- {prefix}{desc}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_solution_block(
    contract: dict[str, Any], plan: dict[str, Any], fix_reco: dict[str, Any]
) -> list[str]:
    """Construit le bloc SOLUTION DETAILLEE.

    Mirror simplifie de _build_solution_section() de mr_description_builder.py,
    mais en texte plat compatible ADF (pas de code blocks Java, qui necessiteraient
    un parser ADF plus complexe). Le BA peut cliquer sur la MR pour voir le code.
    Retourne une liste vide si aucun champ utile n'est present.
    """
    lines: list[str] = []

    # Methode modifiee (depuis contract)
    must_touch_method = (contract.get("must_touch_method") or "").strip()
    must_touch = contract.get("must_touch") or []
    if must_touch_method and must_touch:
        primary_file = must_touch[0] if isinstance(must_touch, list) and must_touch else ""
        if primary_file:
            lines.append(f"- Methode modifiee : {primary_file} -> {must_touch_method}()")
        else:
            lines.append(f"- Methode modifiee : {must_touch_method}()")
    elif must_touch_method:
        lines.append(f"- Methode modifiee : {must_touch_method}()")

    # Approche (issue fix_recommendation Rovo)
    approach = (fix_reco.get("approach_summary") or "").strip()
    if approach:
        lines.append(f"- Approche : {approach}")

    # Resume du correctif : plan.fix_summary (post-LLM) ou contract.problem_summary
    fix_summary = (plan.get("fix_summary") or contract.get("problem_summary") or "").strip()
    if fix_summary:
        lines.append(f"- Resume du correctif : {fix_summary}")

    # Justification metier (depuis Rovo)
    rationale = (contract.get("rovo_rationale_business") or fix_reco.get("rationale_business") or "").strip()
    if rationale:
        lines.append(f"- Justification metier : {rationale}")

    # Score de validation si disponible (donne au BA un signal de qualite)
    score = plan.get("validation_score")
    if isinstance(score, (int, float)):
        lines.append(f"- Score de validation : {int(score)} / 100")

    return lines


def post_run_comment_if_applicable(
    ticket_key: str | None,
    run_id: str,
    mr_url: str | None,
    mr_branch: str | None,
    mr_kind: str,
    rovo_json: dict[str, Any] | None,
    hint_used: bool,
    logger_fn=None,
    contract: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> str | None:
    """Point d'entree haut niveau utilisable depuis agent_service.

    Encapsule : (1) lecture config env, (2) verification config utilisable,
    (3) Q3 : commenter seulement si MR creee, (4) Q4 : idempotence par run_id,
    (5) gestion d'erreur non bloquante.

    v9.16.11 : accepte `contract` et `plan` optionnels pour permettre au
    commentaire Jira de mirroir l'analyse complete de la MR GitLab (sections
    SOLUTION DETAILLEE, INCIDENTS SIMILAIRES, RISQUES). Si absents, le
    commentaire degrade gracieusement vers la version v9.16.10.

    Retourne l'URL du commentaire poste, ou None (config absente / skip / erreur).
    """
    log = logger_fn or (lambda m: None)

    # Q3 : commenter seulement si MR creee
    if not mr_url:
        log("[JIRA] no MR created, skipping comment (decision Q3)")
        return None
    if not ticket_key:
        log("[JIRA] no ticket_key resolved, skipping comment")
        return None

    config = JiraConfig.from_env()
    usable, reason = config.is_usable()
    if not usable:
        log(f"[JIRA] commenter disabled: {reason} (set JIRA_* in secrets.env to enable)")
        return None

    payload = CommentPayload(
        ticket_key=ticket_key,
        run_id=run_id or "unknown",
        mr_url=mr_url,
        mr_branch=mr_branch or "",
        mr_kind=mr_kind or "MR",
        rovo_json=rovo_json or {},
        hint_used=bool(hint_used),
        contract=contract or {},
        plan=plan or {},
    )

    try:
        commenter = JiraCommenter(config)
        url = commenter.post_run_comment(payload)
        if url is None:
            log(f"[JIRA] comment already posted for run_id={payload.run_id}, skipped (idempotence)")
        else:
            log(f"[JIRA] comment posted on {ticket_key}: {url}")
        return url
    except JiraCommenterError as exc:
        # Non bloquant : si Jira est cassé, le run reste valide cote GitLab
        log(f"[JIRA] WARNING: could not post comment on {ticket_key}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        log(f"[JIRA] WARNING: unexpected error posting comment: {exc}")
        return None
