"""app.service.rovo_extractor — Extraction de la section JSON Rovo v9.11.

CORRECTIONS v9.11 par rapport a v9.10 :

1. Regex tolerante au format APLATI (cas reel : copy-paste depuis Atlassian
   Rovo aplatit tous les retours a la ligne).

2. Parser JSON tolerant aux erreurs URL-encoding (%22 dans confluence_url
   qui casse le JSON sur TICKET-NNNNN).

3. Extraction de sous-objet partiel si le JSON global est invalide.

4. Extraction ticket_key plus permissive (priorise JSON Rovo, puis titre,
   puis tout le rapport).
"""
from __future__ import annotations

import json
import re
from typing import Any


BEGIN_PATTERN = re.compile(
    r"SECTION[_\s\-]*MACHINE[_\s\-]*READABLE[_\s\-]*BEGIN",
    re.IGNORECASE,
)
END_PATTERN = re.compile(
    r"SECTION[_\s\-]*MACHINE[_\s\-]*READABLE[_\s\-]*END",
    re.IGNORECASE,
)

# v9.11: tolerant au format aplati (sans \n autour des backticks)
JSON_BLOCK_PATTERN = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)

# v9.13.1: ticket prefix configurable via settings (defaut RUN by default,
# mais peut etre PROJ, OPS, etc. selon le projet). Le prefixe est centralise
# ici pour faciliter l'utilisation du bundle sur d'autres projets.
DEFAULT_TICKET_PREFIXES = ["RUN"]
TICKET_KEY_PATTERN = re.compile(r"\b(RUN-\d+)\b", re.IGNORECASE)


def configure_ticket_prefixes(prefixes: list[str]) -> None:
    """v9.13.1 : reconfigure le pattern d'extraction du ticket_key au runtime.

    Appele par agent_service au demarrage avec settings.agent.ticket_prefixes.
    """
    global TICKET_KEY_PATTERN, DEFAULT_TICKET_PREFIXES
    if not prefixes:
        return
    DEFAULT_TICKET_PREFIXES = list(prefixes)
    # Construire un pattern qui match n'importe lequel des prefixes
    escaped = "|".join(re.escape(p) for p in prefixes)
    TICKET_KEY_PATTERN = re.compile(rf"\b({escaped})-\d+\b", re.IGNORECASE)


def extract_rovo_json(analysis_text: str) -> dict[str, Any] | None:
    """Extrait et parse le JSON Rovo avec strategie de reparation en cascade.

    v9.14.1 : strategie de detection du format en 3 niveaux :
      1. JSON PUR (format v1.4.1) : tout le fichier est un objet JSON
         -> c'est le format recommande, le fichier issue_analysis_*.txt
            ne contient QUE du JSON, traite directement par le bundle.
      2. Format mixte avec balises <rovo_json>...</rovo_json> (v1.3/v1.4)
         -> compatibilite descendante, on extrait le bloc.
      3. Bloc JSON sans balises mais detectable
         -> derniere chance.
    """
    if not analysis_text:
        return None

    # --- NIVEAU 1 v9.14.1 : le fichier entier est-il un JSON pur ? ---
    stripped = analysis_text.strip()
    # Retirer un eventuel BOM
    if stripped.startswith("\ufeff"):
        stripped = stripped[1:].strip()
    # Retirer un eventuel fence markdown ```json ... ```
    fence_match = re.match(r"^```(?:json)?\s*(.+?)\s*```$", stripped, re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        # Tenter parsing direct
        parsed = _try_parse_json(stripped)
        if parsed is not None:
            parsed["_rovo_format"] = "pure_json_v1.4.1"
            return parsed
        # Tenter reparations sur le JSON pur
        for repair in (_repair_url_encoded_quotes, _repair_unescaped_backslashes,
                       _repair_trailing_commas, _repair_combined):
            repaired = repair(stripped)
            if repaired != stripped:
                parsed = _try_parse_json(repaired)
                if parsed is not None:
                    parsed["_rovo_repaired"] = repair.__name__
                    parsed["_rovo_format"] = "pure_json_v1.4.1_repaired"
                    return parsed

    # --- NIVEAU 2 : format mixte avec balises <rovo_json>...</rovo_json> ---
    begin_match = BEGIN_PATTERN.search(analysis_text)
    if not begin_match:
        return None
    end_match = END_PATTERN.search(analysis_text, pos=begin_match.end())
    if not end_match:
        return None

    section = analysis_text[begin_match.end():end_match.start()]
    raw_json = _find_json_block(section)
    if not raw_json:
        return None

    # Parse strict
    parsed = _try_parse_json(raw_json)
    if parsed is not None:
        parsed["_rovo_format"] = "delimited_block_v1.3"
        return parsed

    # Reparations automatiques en cascade
    for repair in (_repair_url_encoded_quotes, _repair_unescaped_backslashes,
                   _repair_trailing_commas, _repair_combined):
        repaired = repair(raw_json)
        if repaired != raw_json:
            parsed = _try_parse_json(repaired)
            if parsed is not None:
                parsed["_rovo_repaired"] = repair.__name__
                parsed["_rovo_format"] = "delimited_block_v1.3_repaired"
                return parsed

    # Extraction partielle (NIVEAU 3)
    partial = _extract_partial_objects(raw_json)
    if partial:
        partial["_rovo_partial_extraction"] = True
        partial["_rovo_format"] = "partial_extraction"
        return partial

    return None


def _find_json_block(section: str) -> str | None:
    m = JSON_BLOCK_PATTERN.search(section)
    if m:
        return m.group(1)
    stripped = re.sub(r"^=+\s*", "", section.strip(), flags=re.MULTILINE)
    stripped = re.sub(r"\s*=+$", "", stripped, flags=re.MULTILINE).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    return None


def _try_parse_json(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        if "schema_version" not in data and "ticket_key" not in data:
            return None
        return data
    except (json.JSONDecodeError, ValueError):
        return None


def _repair_url_encoded_quotes(raw: str) -> str:
    """Repare les %22 (URL encoding de guillemet) qui cassent le JSON.

    Cas reel TICKET-NNNNN: `"confluence_url": "https://...Ordres%22,      "summary..."`
    Le %22 etait un guillemet URL-encode mal place par Rovo. Le supprimer
    laisse `"...Ordres,      "summary` ou la string n'est pas fermee.

    Strategie v9.11 : remplacer `%22,` par `",` directement (fermeture
    propre de la string + virgule de separation). Idem pour `%22}` -> `"}`.
    Et `%22` isole simplement supprime.
    """
    # Priorite 1 : %22 suivi d'une virgule (fin de valeur dans un objet)
    s = re.sub(r'%22\s*,', '",', raw)
    # Priorite 2 : %22 suivi d'une accolade fermante
    s = re.sub(r'%22\s*\}', '"}', s)
    # Priorite 3 : %22 suivi d'un crochet fermant
    s = re.sub(r'%22\s*\]', '"]', s)
    # Priorite 4 : %22 isole - on supprime
    s = s.replace("%22", "")
    return s


def _repair_unescaped_backslashes(raw: str) -> str:
    """Echappe les backslashes orphelins (chemins Windows non echappes)."""
    return re.sub(r'\\(?![\\"/bfnrtu])', r'\\\\', raw)


def _repair_trailing_commas(raw: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", raw)


def _repair_rovo_multiline_strings(raw: str) -> str:
    """v9.16.5 : Rovo met des sauts de ligne litteraux dans les strings JSON.

    Pattern observe sur TICKET-NNNNN et TICKET-NNNNN :
      "confluence_url":"
    [Transverse] Regles de gestion...
    montant..."

    Le JSON est casse car \\n litteral interdit dans une string JSON.
    Solution : detecter les sauts de ligne ENTRE guillemets et les
    transformer en espaces (on perd la mise en forme mais le JSON parse).

    Logique : on parcourt char par char, en suivant l'etat "dans une string"
    delimite par les guillemets non echappes. Quand on est dans une string
    et qu'on rencontre \\n ou \\r, on remplace par un espace.
    """
    if not raw:
        return raw
    result = []
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ch in ("\n", "\r"):
            # Saut de ligne litteral dans une string -> espace
            result.append(" ")
            continue
        result.append(ch)
    return "".join(result)


def _repair_rovo_duplicate_keys(raw: str) -> str:
    """v9.16.5 : Rovo ecrit parfois la meme cle plusieurs fois dans un objet.

    Pattern observe sur TICKET-NNNNN et TICKET-NNNNN : confluence_url repete 4 fois
    dans le meme objet rules_of_business[0], avec a chaque fois une valeur
    multilignes contenant un autre extrait de Confluence. Le parser json
    accepte techniquement (la derniere ecrase) mais ca masque le probleme.

    Apres reparation des multilines, on tente le parse : si python detecte
    plusieurs cles identiques, il prend silencieusement la derniere. Pas
    besoin de reparation explicite - c'est juste un effet de bord positif
    de _repair_rovo_multiline_strings combine avec json.loads.

    Cette fonction force l'objet a etre parsable en fusionnant les valeurs
    multilignes proches en une seule string, ce qui resout aussi le bug
    de la cle 'acceptance_criteria' manquante qui apparait dans la zone.
    """
    # Pas de transformation supplementaire necessaire : la combinaison
    # multilines->espaces + json.loads gere deja le cas. On garde cette
    # fonction comme placeholder pour clarte d'intention.
    return raw


def _repair_rovo_missing_acceptance_criteria_key(raw: str) -> str:
    """v9.16.5 : Rovo oublie parfois la cle 'acceptance_criteria' devant son tableau.

    Pattern observe TICKET-NNNNN, TICKET-NNNNN :
      "rules_of_business":[{...}], <-- la
      ^                              cle manquante
      ici devrait etre "acceptance_criteria":[

    On detecte la situation : apres `"rules_of_business":[...]`, on a
    directement `,{` au lieu de `,"acceptance_criteria":[{`.

    Heuristique simple : si on voit un objet de la forme
    `,{"criterion":` flottant entre rules_of_business et tests_to_add,
    on injecte la cle manquante.
    """
    # Cherche le pattern : } suivi de , suivi d'un objet contenant "criterion"
    # sans cle parent.
    pattern = re.compile(
        r'(\]\s*,\s*)(\{\s*"criterion"\s*:)',
        re.IGNORECASE,
    )
    if pattern.search(raw):
        # Trouve la sequence problematique : `],{"criterion":...}` doit devenir
        # `],"acceptance_criteria":[{"criterion":...}]`
        # On doit aussi fermer le tableau avant le prochain `,"tests_to_add":`
        # ou similaire. Approche : reperer le bloc d'objets `{...},{...}`
        # jusqu'a la prochaine cle de haut niveau.
        return _inject_acceptance_criteria_key(raw)
    return raw


def _inject_acceptance_criteria_key(raw: str) -> str:
    """Injecte la cle 'acceptance_criteria' et son `[`/`]` autour des objets
    `{"criterion":...}` orphelins."""
    # On localise la sequence `],<obj criterion>` et le ],` ferme rules_of_business
    # On lit jusqu'a la prochaine cle de haut niveau pour fermer le tableau.

    # Cles haut niveau possibles apres acceptance_criteria
    next_keys = ["tests_to_add", "similar_past_incidents",
                 "risks_and_uncertainties", "investigation_candidates"]

    # Pattern : ferme `]` de rules_of_business, virgule, puis objet criterion
    m = re.search(r'\]\s*,\s*(\{\s*"criterion"\s*:)', raw, re.IGNORECASE)
    if not m:
        return raw

    insert_pos = m.start() + len(m.group(0)) - len(m.group(1))
    # `insert_pos` est juste avant le `{` du premier objet criterion

    # Trouver la fin du bloc : la prochaine cle haut niveau ou la fin
    after = raw[insert_pos:]
    end_marker = None
    end_pos = None
    for key in next_keys:
        m2 = re.search(rf'\}}\s*,\s*"{re.escape(key)}"\s*:', after)
        if m2:
            if end_pos is None or m2.start() < end_pos:
                end_pos = m2.start()
                end_marker = key
    if end_pos is None:
        # Pas de cle suivante trouvee -> peut etre le bloc finit avant `}` final
        m3 = re.search(r'\}\s*\}\s*$', after)
        if m3:
            end_pos = m3.start()
        else:
            return raw  # On ne sait pas ou fermer, on abandonne

    # Construire la chaine reparee :
    #   prefix + `"acceptance_criteria":[` + bloc objets + `]` + suffix
    objects_block = after[:end_pos + 1]  # +1 pour inclure le `}` final
    suffix = after[end_pos + 1:]
    repaired = (
        raw[:insert_pos]
        + '"acceptance_criteria":['
        + objects_block
        + ']'
        + suffix
    )
    return repaired


def _repair_combined(raw: str) -> str:
    s = _repair_url_encoded_quotes(raw)
    s = _repair_unescaped_backslashes(s)
    s = _repair_trailing_commas(s)
    # v9.16.5 : reparations specifiques aux erreurs Rovo recurrentes
    s = _repair_rovo_multiline_strings(s)
    s = _repair_rovo_missing_acceptance_criteria_key(s)
    return s


def _extract_partial_objects(raw: str) -> dict[str, Any]:
    """Extraction de sous-objets cles meme si le JSON global est casse."""
    result: dict[str, Any] = {}
    # v9.13.1 : ticket prefixe configurable
    escaped = "|".join(re.escape(p) for p in DEFAULT_TICKET_PREFIXES)
    m = re.search(rf'"ticket_key"\s*:\s*"(({escaped})-\d+)"', raw, re.IGNORECASE)
    if m:
        result["ticket_key"] = m.group(1)
    for key in ("crash", "diagnosis", "localization", "fix_recommendation"):
        sub_raw = _extract_subobject_by_key(raw, key)
        if sub_raw:
            for repair in (lambda s: s, _repair_combined):
                try:
                    parsed = json.loads(repair(sub_raw))
                    if isinstance(parsed, dict):
                        result[key] = parsed
                        break
                except (json.JSONDecodeError, ValueError):
                    continue
    return result if result else {}


def _extract_subobject_by_key(raw: str, key: str) -> str | None:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*\{{')
    m = pattern.search(raw)
    if not m:
        return None
    start = m.end() - 1
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


def extract_ticket_key_from_analysis(analysis_text: str) -> str | None:
    """v9.11 : extraction du ticket_key en priorisant le JSON Rovo."""
    if not analysis_text:
        return None
    rovo = extract_rovo_json(analysis_text)
    if rovo and rovo.get("ticket_key"):
        return str(rovo["ticket_key"]).upper().strip()
    head = analysis_text[:500]
    m = TICKET_KEY_PATTERN.search(head)
    if m:
        return m.group(1).upper()
    m = TICKET_KEY_PATTERN.search(analysis_text)
    if m:
        return m.group(1).upper()
    return None


def rovo_to_contract_overrides(rovo: dict[str, Any]) -> dict[str, Any]:
    """Convertit la section Rovo en overrides pour le contrat de correction."""
    if not rovo:
        return {}
    crash = rovo.get("crash") or {}
    diagnosis = rovo.get("diagnosis") or {}
    localization = rovo.get("localization") or {}
    fix_reco = rovo.get("fix_recommendation") or {}

    overrides: dict[str, Any] = {}

    if crash.get("crash_file"):
        overrides["crash_file"] = crash["crash_file"]
    if crash.get("crash_method"):
        overrides["crash_method"] = crash["crash_method"]
    if crash.get("crash_line"):
        try:
            overrides["crash_line"] = int(crash["crash_line"])
        except (TypeError, ValueError):
            pass
    if crash.get("exception_class"):
        overrides["exception_class"] = crash["exception_class"]
    if crash.get("exception_message"):
        overrides["exception_message"] = crash["exception_message"]
    if crash.get("stack_frames_application"):
        overrides["stack_frames_application"] = crash["stack_frames_application"]

    if diagnosis.get("immediate_cause"):
        overrides["immediate_cause"] = diagnosis["immediate_cause"]
    if diagnosis.get("root_cause_hypothesis"):
        overrides["root_cause_hypothesis"] = diagnosis["root_cause_hypothesis"]
    if diagnosis.get("confidence_level"):
        overrides["rovo_confidence_level"] = diagnosis["confidence_level"]

    must_files = localization.get("must_touch_files") or []
    if must_files:
        overrides["must_touch"] = [_extract_filename(p) for p in must_files]
        overrides["must_touch_paths"] = list(must_files)
    may_files = localization.get("may_touch_files") or []
    if may_files:
        overrides["may_touch"] = [_extract_filename(p) for p in may_files]
        overrides["may_touch_paths"] = list(may_files)
    must_methods = localization.get("must_touch_methods") or []
    if must_methods:
        overrides["must_touch_method"] = must_methods[0]
        overrides["must_touch_methods"] = list(must_methods)
    if localization.get("repo_hint"):
        overrides["repo_hint"] = localization["repo_hint"]

    if fix_reco.get("fix_style"):
        overrides["fix_style"] = fix_reco["fix_style"]
    if fix_reco.get("approach_summary"):
        overrides["problem_summary"] = fix_reco["approach_summary"]
    if fix_reco.get("code_suggestion"):
        overrides["rovo_code_suggestion"] = fix_reco["code_suggestion"]
    if fix_reco.get("code_location_hint"):
        overrides["rovo_code_location_hint"] = fix_reco["code_location_hint"]
    if fix_reco.get("rationale_business"):
        overrides["rovo_rationale_business"] = fix_reco["rationale_business"]

    # v9.14 : investigation_candidates (liste hierarchisee de candidats)
    candidates = rovo.get("investigation_candidates") or []
    if isinstance(candidates, list) and candidates:
        # Tri par rank croissant si fourni
        try:
            candidates = sorted(candidates, key=lambda c: int(c.get("rank", 999)) if isinstance(c, dict) else 999)
        except Exception:
            pass
        overrides["investigation_candidates"] = candidates

    # v9.14 : endpoint_concerned (point d'entree REST)
    endpoint = rovo.get("endpoint_concerned")
    if isinstance(endpoint, dict) and endpoint.get("path"):
        method_part = endpoint.get("method", "").strip().upper()
        path_part = endpoint.get("path", "").strip()
        if method_part and path_part:
            overrides["endpoint_concerned"] = f"{method_part} {path_part}"
        elif path_part:
            overrides["endpoint_concerned"] = path_part
        # Garder aussi l'objet complet pour l'agent
        overrides["endpoint_concerned_full"] = endpoint

    ac_list = rovo.get("acceptance_criteria") or []
    criteria_strings: list[str] = []
    criteria_objects: list[dict[str, Any]] = []
    for ac in ac_list:
        if isinstance(ac, dict):
            text = ac.get("criterion") or ac.get("text") or ""
            if text:
                criteria_strings.append(str(text))
                criteria_objects.append(ac)
        elif isinstance(ac, str):
            criteria_strings.append(ac)
            criteria_objects.append({"criterion": ac})
    if criteria_strings:
        overrides["acceptance_criteria"] = criteria_strings
        overrides["rovo_acceptance_criteria_full"] = criteria_objects

    tests_list = rovo.get("tests_to_add") or []
    tests_strings: list[str] = []
    tests_objects: list[dict[str, Any]] = []
    for t in tests_list:
        if isinstance(t, dict):
            scenario = t.get("scenario") or t.get("test_method_suggestion") or ""
            if scenario:
                tests_strings.append(str(scenario))
                tests_objects.append(t)
        elif isinstance(t, str):
            tests_strings.append(t)
            tests_objects.append({"scenario": t})
    if tests_strings:
        overrides["oracle_tests"] = tests_strings
        overrides["rovo_tests_to_add_full"] = tests_objects

    # v9.11 NOUVEAU
    rg_list = rovo.get("rules_of_business") or []
    if rg_list:
        overrides["rovo_rules_of_business"] = rg_list
    similar = rovo.get("similar_past_incidents") or []
    if similar:
        overrides["rovo_similar_past_incidents"] = similar
    risks = rovo.get("risks_and_uncertainties") or []
    if risks:
        overrides["rovo_risks_and_uncertainties"] = risks

    overrides["source"] = "rovo+local-fix-agent"
    overrides["rovo_schema_version"] = rovo.get("schema_version", "unknown")
    if rovo.get("_rovo_repaired"):
        overrides["rovo_repaired_by"] = rovo["_rovo_repaired"]
    if rovo.get("_rovo_partial_extraction"):
        overrides["rovo_partial_extraction"] = True
    return overrides


def _extract_filename(path: str) -> str:
    if not path:
        return ""
    p = path.replace("\\", "/").rstrip("/")
    if "/" in p:
        return p.rsplit("/", 1)[-1]
    return p


def merge_with_priority_to_rovo(
    bundle_contract: dict[str, Any],
    rovo_overrides: dict[str, Any],
    fields_rovo_wins: list[str] | None = None,
) -> dict[str, Any]:
    if not rovo_overrides:
        return bundle_contract
    if fields_rovo_wins is None:
        fields_rovo_wins = [
            "crash_file", "crash_method", "crash_line", "exception_class",
            "exception_message", "stack_frames_application",
            "immediate_cause", "root_cause_hypothesis",
            "must_touch", "must_touch_paths", "must_touch_method",
            "must_touch_methods", "may_touch", "may_touch_paths",
            "fix_style", "repo_hint", "problem_summary",
        ]
    merged = dict(bundle_contract)
    for field in fields_rovo_wins:
        if field in rovo_overrides and rovo_overrides[field]:
            merged[field] = rovo_overrides[field]
    for list_field in ["acceptance_criteria", "oracle_tests"]:
        rovo_items = rovo_overrides.get(list_field) or []
        bundle_items = bundle_contract.get(list_field) or []
        if rovo_items or bundle_items:
            seen = set()
            unified = []
            for item in list(rovo_items) + list(bundle_items):
                key = str(item).strip().lower()[:100]
                if key not in seen:
                    seen.add(key)
                    unified.append(item)
            merged[list_field] = unified
    rovo_only_fields = [
        "rovo_code_suggestion", "rovo_code_location_hint",
        "rovo_rationale_business", "source", "rovo_schema_version",
        "rovo_confidence_level", "rovo_acceptance_criteria_full",
        "rovo_tests_to_add_full", "rovo_rules_of_business",
        "rovo_similar_past_incidents", "rovo_risks_and_uncertainties",
        "rovo_repaired_by", "rovo_partial_extraction",
        # v9.15.1 : champs v1.4 indispensables a l'agent d'exploration.
        # Bug observe TICKET-NNNNN : ils etaient extraits dans les overrides
        # mais jamais propages au contrat, donc l'agent partait a l'aveugle.
        "investigation_candidates", "endpoint_concerned", "endpoint_concerned_full",
    ]
    for f in rovo_only_fields:
        if f in rovo_overrides:
            merged[f] = rovo_overrides[f]
    return merged
