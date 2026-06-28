from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from app.utils.ticket_utils import extract_current_ticket_key

STANDARD_JAVA_EXCEPTIONS = {
    "exception",
    "runtimeexception",
    "nullpointerexception",
    "illegalargumentexception",
    "illegalstateexception",
}

# v9.2: liste noire des préfixes de packages framework/JDK que l'on doit
# exclure des indices de localisation. Une frame de stack trace dont le
# qualified_class commence par un de ces préfixes ne sera pas utilisée pour
# le grep ni pour la sélection de fichiers candidats.
# Cette liste est volontairement conservatrice : on n'exclut que les
# packages dont on est certain qu'ils n'appartiennent JAMAIS au code
# your application. Toute extension de cette liste doit être justifiée
# par une observation réelle (frame qui pollue un run).
FRAMEWORK_PACKAGE_PREFIXES = (
    "java.",
    "javax.",
    "jakarta.",
    "sun.",
    "com.sun.",
    "jdk.",
    "org.springframework.",
    "org.apache.",
    "org.hibernate.",
    "org.slf4j.",
    "org.eclipse.",
    "org.junit.",
    "org.mockito.",
    "org.assertj.",
    "ch.qos.logback.",
    "io.netty.",
    "reactor.",
    "kotlin.",
    "groovy.",
    # CGLIB / proxies générés
    "org.springframework.cglib.",
    "org.springframework.aop.",
    "net.bytebuddy.",
)

SAFE_FIX_PATTERNS = {
    "minimal_local_null_guard": [
        "nullpointerexception",
        "null",
        "controle defensif",
        "contrôle défensif",
        "garde",
        "guard",
        "source == null",
        "source is null",
        "source peut être null",
        "source peut etre null",
        "ne pas lever npe",
        "optional.empty",
    ],
    "safe_filter_or_skip_invalid_item": [
        "ignorer l'élément",
        "ignorer l’élément",
        "exclure l'élément",
        "exclure l’élément",
        "filtrer",
        "optional.empty",
        "résultat cohérent",
        "resultat coherent",
    ],
    "api_contract_fix": [
        "responseentity",
        "contrat api",
        "schéma json",
        "schema json",
        "signature",
        "type de retour",
    ],
    "input_validation_guard": [
        "validation",
        "paramètre",
        "parametre",
        "requête",
        "requestparam",
        "pathvariable",
    ],
}

_STACKTRACE_FRAME_RE = re.compile(
    r"at\s+(?P<qualified>[\w.$]+)\.(?P<method>[A-Za-z_][A-Za-z0-9_]*)\((?P<file>[A-Z][A-Za-z0-9_]+\.java):(?P<line>\d+)\)"
)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = normalize_space(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def extract_section(text: str, title: str, stop_titles: list[str] | None = None) -> str:
    pattern = re.compile(rf"(?is)(?:^|\n)(?:\d+\.\s+)?{re.escape(title)}\s*(.*)")
    match = pattern.search(text)
    if not match:
        return ""
    remainder = match.group(1)
    stop_candidates = [r"\n\d+\.\s+[A-ZÉÈÊÀÂÎÏÙÛÇ].*"]
    for stop_title in stop_titles or []:
        stop_candidates.append(rf"\n(?:\d+\.\s+)?{re.escape(stop_title)}")
    stop_pattern = re.compile("|".join(stop_candidates), re.IGNORECASE)
    stop_match = stop_pattern.search(remainder)
    return remainder[: stop_match.start()] if stop_match else remainder


def extract_java_filenames(section_text: str) -> list[str]:
    raw = re.findall(r"\b([A-Z][A-Za-z0-9_]+\.java)\b", section_text or "")
    deduped = dedupe_keep_order(raw)
    # v9.2: exclure les basenames framework (MethodProxy.java, CglibAopProxy.java, etc.)
    return filter_application_basenames(deduped)


def normalized_method_names(analysis_text: str, *, application_prefixes: list[str] | None = None) -> list[str]:
    raw = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\([^\)]*\)", analysis_text or "")
    methods = [name for name in raw if len(name) >= 3]
    for frame in parse_stacktrace_frames(analysis_text, application_prefixes=application_prefixes):
        methods.append(frame["method"])
    return sorted(set(methods))


def extract_class_names(analysis_text: str, java_files: list[str]) -> list[str]:
    from_files = [name[:-5] for name in java_files if name.endswith(".java")]
    raw = re.findall(
        r"\b([A-Z][A-Za-z0-9_]+(?:Util|Service|Controller|Repository|Dao|Entity|Response|Request|Test|Mapper|Impl|Facade))\b",
        analysis_text or "",
    )
    combined: list[str] = []
    for value in list(raw) + from_files:
        lower = value.lower()
        if lower in STANDARD_JAVA_EXCEPTIONS:
            continue
        combined.append(value)
    return sorted(set(combined))


# v9.2: noms de classes/fichiers connus comme appartenant aux frameworks
# couramment vus dans les stack traces Spring. Liste construite à partir
# des observations terrain (notamment TICKET-NNNNN).
# Ces basenames seront exclus des extractions par regex sur le texte brut
# (re.findall sur \b[A-Z]\w+\.java\b) qui sinon ramassent du bruit.
FRAMEWORK_BASENAMES = {
    "MethodProxy.java",
    "CglibAopProxy.java",
    "ReflectiveMethodInvocation.java",
    "ExposeInvocationInterceptor.java",
    "InvocableHandlerMethod.java",
    "ServletInvocableHandlerMethod.java",
    "RequestMappingHandlerAdapter.java",
    "DispatcherServlet.java",
    "FrameworkServlet.java",
    "DelegatingMethodAccessorImpl.java",
    "NativeMethodAccessorImpl.java",
    "Method.java",
    "Constructor.java",
    "Filter.java",
    "FilterChain.java",
    "OncePerRequestFilter.java",
    "AbstractHandlerMapping.java",
    "AbstractHandlerMethodAdapter.java",
    "Thread.java",
    "ThreadPoolExecutor.java",
    "ScheduledThreadPoolExecutor.java",
    "FutureTask.java",
}


def filter_application_basenames(basenames: list[str]) -> list[str]:
    """Retire de la liste les basenames Java reconnus comme appartenant
    aux frameworks. Préserve l'ordre."""
    return [b for b in basenames if b not in FRAMEWORK_BASENAMES]


def is_framework_frame(qualified_class: str, application_prefixes: list[str] | None = None) -> bool:
    """Renvoie True si la frame doit être considérée comme NON applicative
    (donc à exclure des indices de localisation).

    Logique de classification :
    - Si une whitelist `application_prefixes` est fournie ET non vide :
        → la frame est applicative SSI son qualifier matche un de ces préfixes.
          Sinon elle est traitée comme framework (par défaut : exclue).
    - Sinon (pas de whitelist, comportement v9.2 par défaut) :
        → on n'utilise que la blacklist FRAMEWORK_PACKAGE_PREFIXES.
          Toute frame dont le qualifier matche un préfixe blacklist est rejetée.
          Tout le reste est considéré applicatif.

    Cette double logique permet :
    - le mode v9.2 (blacklist seule) qui marche out-of-the-box sans configuration
    - le mode v9.3 (whitelist auto-détectée) qui est plus robuste contre
      les frames de bibliothèques tierces non listées dans la blacklist
    """
    if not qualified_class:
        return False
    # Mode whitelist (v9.3) : si fournie et non vide, elle prime
    if application_prefixes:
        if any(qualified_class.startswith(p) for p in application_prefixes):
            return False  # frame applicative confirmée
        # Frame qui ne matche aucun préfixe applicatif : rejetée
        return True
    # Mode blacklist seule (v9.2 fallback) : rejette uniquement les frameworks connus
    return any(qualified_class.startswith(p) for p in FRAMEWORK_PACKAGE_PREFIXES)


def parse_stacktrace_frames(
    analysis_text: str,
    *,
    include_framework: bool = False,
    application_prefixes: list[str] | None = None,
) -> list[dict[str, str]]:
    """Extrait les frames de stack trace sous forme de dicts structurés.

    Par défaut (include_framework=False), filtre les frames non applicatives :
    - Si application_prefixes est fourni : seules les frames dont le qualifier
      matche un de ces préfixes sont retenues (mode whitelist).
    - Sinon : on rejette les frames dont le qualifier matche
      FRAMEWORK_PACKAGE_PREFIXES (mode blacklist seule, v9.2).

    Si include_framework=True, retourne TOUTES les frames (utile pour
    l'audit ou pour reconstituer la stack complète dans le rapport).
    """
    frames: list[dict[str, str]] = []
    for match in _STACKTRACE_FRAME_RE.finditer(analysis_text or ""):
        qualified = match.group("qualified")
        if not include_framework and is_framework_frame(qualified, application_prefixes):
            continue
        class_name = qualified.split(".")[-1]
        frames.append(
            {
                "qualified_class": qualified,
                "class_name": class_name,
                "method": match.group("method"),
                "file": match.group("file"),
                "line": match.group("line"),
            }
        )
    return frames


def parse_rejected_framework_frames(
    analysis_text: str,
    *,
    application_prefixes: list[str] | None = None,
) -> list[dict[str, str]]:
    """Renvoie les frames qui SONT considérées non-applicatives — pour audit
    dans le contrat. Utilise la même logique que parse_stacktrace_frames."""
    rejected: list[dict[str, str]] = []
    for match in _STACKTRACE_FRAME_RE.finditer(analysis_text or ""):
        qualified = match.group("qualified")
        if is_framework_frame(qualified, application_prefixes):
            rejected.append(
                {
                    "qualified_class": qualified,
                    "class_name": qualified.split(".")[-1],
                    "method": match.group("method"),
                    "file": match.group("file"),
                    "line": match.group("line"),
                }
            )
    return rejected


def extract_hints(analysis_text: str, *, application_prefixes: list[str] | None = None) -> dict[str, list[str]]:
    raw_java_files = sorted(set(re.findall(r"\b([A-Z][A-Za-z0-9_]+\.java)\b", analysis_text or "")))
    java_files = filter_application_basenames(raw_java_files)  # v9.2: exclure frameworks
    frames = parse_stacktrace_frames(analysis_text, application_prefixes=application_prefixes)
    class_names = extract_class_names(analysis_text, java_files)
    methods = normalized_method_names(analysis_text, application_prefixes=application_prefixes)
    modules = sorted(set(re.findall(r"\b([a-z_][\w]*(?:\.[A-Za-z_][\w]*){2,})\b", analysis_text or "")))

    probable_zone = extract_section(
        analysis_text,
        "Zones probables de correction",
        stop_titles=["Zones à NE PAS modifier sans preuve", "PROPOSITION DE SOLUTION", "REGLES DE GESTION A VERIFIER"],
    )
    summary_zone = extract_section(analysis_text, "SYNTHESE EXECUTIVE")
    do_not_touch_zone = extract_section(
        analysis_text,
        "Zones à NE PAS modifier sans preuve",
        stop_titles=["PROPOSITION DE SOLUTION", "REGLES DE GESTION A VERIFIER"],
    )
    stacktrace_zone = extract_section(
        analysis_text,
        "Stack trace pertinente",
        stop_titles=["Classes, fichiers et méthodes explicitement impliqués", "Fichiers nommés dans les preuves"],
    )
    evidence_zone = extract_section(analysis_text, "Fichiers nommés dans les preuves", stop_titles=["Classes impliquées"])

    probable_java_files = extract_java_filenames(probable_zone + "\n" + summary_zone)
    crash_line_files_raw = dedupe_keep_order(re.findall(r"\b([A-Z][A-Za-z0-9_]+\.java):\d+\b", analysis_text or ""))
    # v9.2: exclure frameworks (MethodProxy.java:218, CglibAopProxy.java:792, etc.)
    crash_line_files = [f for f in crash_line_files_raw if f not in FRAMEWORK_BASENAMES]
    stacktrace_java_files = extract_java_filenames(stacktrace_zone)
    if not stacktrace_java_files:
        stacktrace_java_files = dedupe_keep_order([frame["file"] for frame in frames])
    evidence_java_files = extract_java_filenames(evidence_zone)
    do_not_touch_files = extract_java_filenames(do_not_touch_zone)

    priority_java_files = probable_java_files or crash_line_files or stacktrace_java_files or evidence_java_files or java_files
    support_java_files = dedupe_keep_order(priority_java_files + stacktrace_java_files + evidence_java_files + java_files)

    quoted = sorted(set(re.findall(r"'([^']+)'", analysis_text or "") + re.findall(r'"([^"]+)"', analysis_text or "")))
    anchor_terms: list[str] = []
    for value in priority_java_files + support_java_files:
        anchor_terms.append(value)
        if value.endswith(".java"):
            anchor_terms.append(value[:-5])
    for frame in frames[:6]:
        anchor_terms.extend([
            frame["file"],
            frame["method"],
            f"{frame['method']}(",
            f"{frame['file']}:{frame['line']}",
        ])
    for value in methods + class_names:
        anchor_terms.append(value)
    for value in quoted:
        trimmed = value.strip()
        if 4 <= len(trimmed) <= 160:
            anchor_terms.append(trimmed)

    return {
        "class_names": class_names,
        "java_files": java_files,
        "probable_java_files": probable_java_files,
        "stacktrace_java_files": stacktrace_java_files,
        "crash_line_files": crash_line_files,
        "evidence_java_files": evidence_java_files,
        "priority_java_files": priority_java_files,
        "support_java_files": support_java_files,
        "do_not_touch_files": do_not_touch_files,
        "modules": modules,
        "methods": methods,
        "stacktrace_frames": [
            f"{frame['qualified_class']}.{frame['method']}({frame['file']}:{frame['line']})" for frame in frames
        ],
        "anchor_terms": dedupe_keep_order([term for term in anchor_terms if len(term) >= 3])[:60],
    }


def _extract_lines_with_prefix(section_text: str, prefixes: tuple[str, ...] = ("-",)) -> list[str]:
    lines: list[str] = []
    unprefixed: list[str] = []
    for raw in (section_text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith(prefixes):
            value = stripped.lstrip("-•0123456789. ").strip()
            if value and not value.endswith(":"):
                lines.append(value)
        elif not stripped.endswith(":") and len(stripped) > 6:
            unprefixed.append(stripped)
    return dedupe_keep_order(lines or unprefixed)


def _meaningful_line_candidates(section_text: str) -> list[str]:
    lines = []
    for raw in (section_text or "").splitlines():
        cleaned = normalize_space(raw)
        if not cleaned:
            continue
        lower = cleaned.lower()
        if lower in {"constat :", "preuves :", "non etabli", "non établi"}:
            continue
        if lower.startswith("source :"):
            continue
        if cleaned.endswith(":") and len(cleaned) < 80:
            continue
        lines.append(cleaned)
    return lines


def _first_non_empty_line(section_text: str) -> str:
    for cleaned in _meaningful_line_candidates(section_text):
        return cleaned
    return ""


def _extract_problem_summary(analysis_text: str, frames: list[dict[str, str]]) -> str:
    for label in ["Résumé du bug", "Comportement observé", "Symptôme technique principal", "Ticket Jira courant"]:
        line = _first_non_empty_line(extract_section(analysis_text, label))
        if line:
            return line
    npe_line = re.search(r"java\.lang\.[A-Za-z0-9_]+Exception: .*", analysis_text or "")
    if npe_line:
        return normalize_space(npe_line.group(0))
    if frames:
        frame = frames[0]
        return (
            f"Incident sur {frame['file']}:{frame['line']} dans {frame['method']}() à partir de la stack trace du ticket."
        )
    return ""


def _extract_acceptance_criteria(analysis_text: str, frames: list[dict[str, str]]) -> list[str]:
    criteria = _extract_lines_with_prefix(
        extract_section(analysis_text, "Critères d’acceptation", stop_titles=["Observabilité attendue", "SYNTHESE EXECUTIVE"])
    )
    if criteria:
        return criteria[:8]

    fallback: list[str] = []
    expected_section = extract_section(analysis_text, "Comportement attendu", stop_titles=["Comportement observé", "Impact métier"])
    fallback.extend(_extract_lines_with_prefix(expected_section))

    solution_section = extract_section(
        analysis_text,
        "Correction recommandée (minimale, conforme aux contraintes)",
        stop_titles=["Nature de la correction", "Temps estimé", "Prérequis", "Portée de modification", "Risques associés", "PLAN DE VALIDATION"],
    )
    if solution_section:
        for candidate in _meaningful_line_candidates(solution_section):
            lower = candidate.lower()
            if any(token in lower for token in ["comportement attendu", "ne pas lever", "exclure", "preserver", "préserver", "pas d'exception", "pas d’exception", "json", "erreur 500"]):
                fallback.append(candidate)

    plan_validation = extract_section(analysis_text, "PLAN DE VALIDATION", stop_titles=["SYNTHESE EXECUTIVE"])
    for label in ["Cas nominal", "Cas dégradé", "Cas limites"]:
        section = extract_section(plan_validation, label)
        fallback.extend(_extract_lines_with_prefix(section))

    allowed_tokens = ('npe', 'null', 'erreur', '500', 'json', 'contrat', 'log', 'warn', 'optional', 'résultat', 'resultat', 'exception')
    fallback = [
        item
        for item in dedupe_keep_order(fallback)
        if len(item) <= 220
        and not item.lower().startswith(("nature de la correction", "temps estimé", "prérequis", "remarque :"))
        and any(token in item.lower() for token in allowed_tokens)
        and not re.fullmatch(r"[A-Za-z0-9_$.:-]{3,}", item)
        and len(item.split()) >= 3
    ]
    if not fallback and frames:
        frame = frames[0]
        fallback = [
            f"Ne plus lever d'exception sur {frame['file']}:{frame['line']}.",
            "Préserver le contrat public existant et éviter une erreur 500.",
        ]
    return fallback[:8]


def _extract_oracle_tests(analysis_text: str, frames: list[dict[str, str]], acceptance_criteria: list[str]) -> list[str]:
    tests = []
    for label in [
        "Tests unitaires indispensables",
        "Tests d’intégration / service",
        "Cas nominal",
        "Cas dégradé",
        "Cas limites",
        "Observabilité attendue",
    ]:
        section = extract_section(analysis_text, label)
        tests.extend(_extract_lines_with_prefix(section))
    tests = dedupe_keep_order(tests)
    if tests:
        return tests[:14]

    if acceptance_criteria:
        generated = [
            f"Vérifier: {criterion}" for criterion in acceptance_criteria[:5]
        ]
        return generated[:10]

    if frames:
        frame = frames[0]
        return [
            f"Tester le cas nominal du flux sans modification de comportement hors {frame['file']}.",
            f"Tester le cas dégradé reproduisant le crash {frame['file']}:{frame['line']} sans NPE.",
        ]
    return []


def _extract_immediate_cause(analysis_text: str, frames: list[dict[str, str]]) -> str:
    priority_labels = [
        "Diagnostic détaillé (pourquoi l’erreur se produit)",
        "Cause racine probable",
        "Cause racine la plus probable",
        "Correction minimale recommandée",
    ]
    for label in priority_labels:
        section = extract_section(analysis_text, label)
        for line in _meaningful_line_candidates(section):
            lower = line.lower()
            if lower in {"(hypothèses classées)", "hypothèses classées"}:
                continue
            if any(token in lower for token in ["null", "npe", "source", "client", "get", "absence", "incomplet", "invalide"]):
                return line
        first = _first_non_empty_line(section)
        if first and first.lower() not in {"(hypothèses classées)", "hypothèses classées"}:
            return first

    npe_line = re.search(r"java\.lang\.NullPointerException: .*", analysis_text or "")
    if npe_line:
        return normalize_space(npe_line.group(0))

    if frames:
        frame = frames[0]
        return f"La cause immédiate semble se situer dans {frame['file']}:{frame['line']} au niveau de {frame['method']}()."
    return ""


def _extract_immediate_cause_terms(analysis_text: str, hints: dict[str, list[str]], frames: list[dict[str, str]]) -> list[str]:
    terms: list[str] = []
    for regex in [
        r"\b([A-Z][A-Za-z0-9_]+\.java:\d+)\b",
        r"\b([A-Za-z_][A-Za-z0-9_]*\([^\)]*\))",
        r"\b([A-Za-z_][A-Za-z0-9_]*\.get[A-Za-z0-9_]+\(\))",
        r"\b([A-Za-z_][A-Za-z0-9_]*\s*==\s*null)\b",
    ]:
        for match in re.findall(regex, analysis_text or ""):
            cleaned = normalize_space(match)
            if 3 <= len(cleaned) <= 160:
                # v9.2: exclure les frames framework type "MethodProxy.java:218"
                # qui sinon polluent les indices passés au grep et brouillent
                # la sélection de fichiers candidats.
                basename_match = re.match(r"^([A-Z][A-Za-z0-9_]+\.java)(:\d+)?$", cleaned)
                if basename_match and basename_match.group(1) in FRAMEWORK_BASENAMES:
                    continue
                # v9.2: exclure aussi les expressions parenthésées contenant un
                # basename framework (ex: "invoke(MethodProxy.java:218)").
                if any(fb in cleaned for fb in FRAMEWORK_BASENAMES):
                    continue
                # v9.2: exclure les appels Native Method (méthodes JDK natives)
                if "Native Method" in cleaned or "<generated>" in cleaned:
                    continue
                terms.append(cleaned)
    lowered = (analysis_text or "").lower()
    for raw in [
        "nullpointerexception",
        "source is null",
        "source == null",
        "source peut être null",
        "source peut etre null",
        "_source",
    ]:
        if raw in lowered:
            terms.append(raw)
    for frame in frames[:6]:
        terms.extend([frame["file"], frame["method"], f"{frame['file']}:{frame['line']}"])
    terms.extend(hints["methods"][:12])
    terms.extend(hints["class_names"][:10])
    return dedupe_keep_order(terms)[:30]


def _extract_priority_reasons(analysis_text: str, basenames: list[str]) -> dict[str, str]:
    zone = extract_section(
        analysis_text,
        "Zones probables de correction",
        stop_titles=["Zones à NE PAS modifier sans preuve", "PROPOSITION DE SOLUTION"],
    )
    reasons: dict[str, str] = {}
    lines = [normalize_space(line) for line in zone.splitlines() if normalize_space(line)]
    for basename in basenames:
        lower = basename.lower()
        reason = ""
        for index, line in enumerate(lines):
            if lower in line.lower():
                tail = lines[index : index + 4]
                reason = " | ".join(tail)
                break
        if reason:
            reasons[basename] = reason
    return reasons


def _infer_fix_style(analysis_text: str, immediate_cause: str, acceptance_criteria: list[str]) -> str:
    haystack = normalize_space(" ".join([analysis_text, immediate_cause, " ".join(acceptance_criteria)])).lower()
    best = "minimal_local_change"
    best_score = -1
    for pattern_name, keywords in SAFE_FIX_PATTERNS.items():
        score = sum(2 if keyword in immediate_cause.lower() else 1 for keyword in keywords if keyword in haystack)
        if score > best_score:
            best = pattern_name
            best_score = score
    return best


def _infer_discipline(hints: dict[str, list[str]], analysis_text: str, frames: list[dict[str, str]]) -> str:
    haystack = normalize_space(analysis_text).lower()
    if hints["probable_java_files"]:
        return "strict"
    if frames and len({frame['file'] for frame in frames[:3]}) <= 2:
        return "strict"
    if "doit rester limitée" in haystack or "doit rester limitee" in haystack or "ne pas introduire de refonte" in haystack:
        return "strict"
    if len(hints["stacktrace_java_files"]) <= 3:
        return "local"
    return "extended"


def _extract_must_touch_method(frames: list[dict[str, str]], must_touch: list[str], methods: list[str]) -> str:
    wanted = {name.lower() for name in must_touch}
    for frame in frames:
        if frame["file"].lower() in wanted:
            return frame["method"]
    return methods[0] if methods else ""


def build_correction_contract(analysis_text: str, *, application_prefixes: list[str] | None = None) -> dict[str, Any]:
    hints = extract_hints(analysis_text, application_prefixes=application_prefixes)
    frames = parse_stacktrace_frames(analysis_text, application_prefixes=application_prefixes)
    ticket_key = extract_current_ticket_key(analysis_text)
    problem_summary = _extract_problem_summary(analysis_text, frames)
    immediate_cause = _extract_immediate_cause(analysis_text, frames)
    acceptance_criteria = _extract_acceptance_criteria(analysis_text, frames)
    oracle_tests = _extract_oracle_tests(analysis_text, frames, acceptance_criteria)
    immediate_cause_terms = _extract_immediate_cause_terms(analysis_text, hints, frames)
    fix_style = _infer_fix_style(analysis_text, immediate_cause, acceptance_criteria)
    discipline = _infer_discipline(hints, analysis_text, frames)

    crash_file = frames[0]["file"] if frames else ""
    crash_line = frames[0]["line"] if frames else ""
    crash_method = frames[0]["method"] if frames else ""

    must_touch = hints["probable_java_files"] or ([crash_file] if crash_file else []) or hints["priority_java_files"][:1]
    if not must_touch and hints["stacktrace_java_files"]:
        must_touch = hints["stacktrace_java_files"][:1]
    must_touch = dedupe_keep_order(must_touch)
    must_touch_method = _extract_must_touch_method(frames, must_touch, hints["methods"])

    must_not_touch = [item for item in hints["do_not_touch_files"] if item not in must_touch]

    may_touch = []
    for value in dedupe_keep_order(hints["stacktrace_java_files"] + hints["evidence_java_files"] + hints["support_java_files"] + hints["java_files"]):
        if value in must_touch or value in must_not_touch:
            continue
        may_touch.append(value)
    if discipline == "strict":
        may_touch = may_touch[:2]
    elif discipline == "local":
        may_touch = may_touch[:4]
    else:
        may_touch = may_touch[:6]

    # v9.4: calcule les basenames .java des frames qui ont été rejetées par le filtre framework.
    # Ces basenames seront filtrés des localization_search_terms et anchor_terms pour ne pas
    # qu'ils servent de cible aux greps. Ce filtre dynamique complète la blacklist statique
    # FRAMEWORK_BASENAMES qui ne peut pas couvrir toutes les classes des libs tierces (Hibernate,
    # MySQL Connector, QueryDSL, Spring DAO, etc).
    rejected_frames_objs = parse_rejected_framework_frames(analysis_text, application_prefixes=application_prefixes)
    rejected_basenames = {f["file"] for f in rejected_frames_objs}
    # v9.4: on dérive aussi les noms de classes nues (sans .java) pour matcher
    # les termes type "ObjectMapper", "MethodProxy", "Loader" qui apparaissent
    # comme anchor_terms via les regex de noms de classes.
    rejected_class_names = {bn[:-5] for bn in rejected_basenames if bn.endswith(".java")}

    def _drop_rejected_basenames(terms: list[str]) -> list[str]:
        """Retire les termes contenant un basename .java rejeté ou un nom
        de classe rejeté.

        Cible :
        - 'Foo.java', 'Foo.java:42', 'method(Foo.java:42)' → match basename
        - 'Foo' (token bare) → match nom de classe pour les anchor_terms

        On utilise un match par mot pour les noms de classe, sinon
        'AbstractStandardBasicType' matcherait 'AbstractStandardBasicTypeXxx'
        ce qui pourrait éliminer du code applicatif par erreur.
        """
        if not rejected_basenames and not rejected_class_names:
            return terms
        cleaned: list[str] = []
        for term in terms:
            # Match par basename (avec ou sans :ligne, avec ou sans wrapping method())
            if any(b in term for b in rejected_basenames):
                continue
            # Match par nom de classe nu (ex: term == "ObjectMapper")
            if term in rejected_class_names:
                continue
            cleaned.append(term)
        return cleaned

    localization_search_terms = dedupe_keep_order(
        [must_touch_method, crash_method, crash_file, f"{crash_file}:{crash_line}" if crash_file and crash_line else ""]
        + immediate_cause_terms
        + hints["anchor_terms"]
    )[:20]
    localization_search_terms = _drop_rejected_basenames(localization_search_terms)

    priority_reasons = _extract_priority_reasons(analysis_text, must_touch)
    if crash_file and crash_file not in priority_reasons:
        frame_bits = [crash_file]
        if crash_line:
            frame_bits[-1] = f"{crash_file}:{crash_line}"
        if crash_method:
            frame_bits.append(f"méthode={crash_method}()")
        priority_reasons[crash_file] = "Premier point de crash issu de la stack trace: " + " | ".join(frame_bits)

    contract = {
        "ticket_key": ticket_key,
        "discipline": discipline,
        "problem_summary": problem_summary,
        "immediate_cause": immediate_cause,
        "immediate_cause_terms": immediate_cause_terms,
        "fix_style": fix_style,
        "safe_fix_patterns": sorted(SAFE_FIX_PATTERNS.keys()),
        "must_touch": must_touch,
        "must_touch_method": must_touch_method,
        "may_touch": may_touch,
        "must_not_touch": must_not_touch,
        "stacktrace_files": hints["stacktrace_java_files"],
        "evidence_files": hints["evidence_java_files"],
        "support_files": dedupe_keep_order(must_touch + may_touch + hints["support_java_files"])[:10],
        "acceptance_criteria": acceptance_criteria,
        "oracle_tests": oracle_tests,
        "methods": hints["methods"],
        "class_names": hints["class_names"],
        "modules": hints["modules"],
        "anchor_terms": _drop_rejected_basenames(dedupe_keep_order(localization_search_terms + hints["anchor_terms"])[:60]),
        "priority_reasons": priority_reasons,
        "crash_file": crash_file,
        "crash_line": crash_line,
        "crash_method": crash_method,
        "stacktrace_frames": hints["stacktrace_frames"],
        "rejected_framework_frames": [
            f"{f['qualified_class']}.{f['method']}({f['file']}:{f['line']})"
            for f in rejected_frames_objs
        ],
        "localization_search_terms": localization_search_terms,
        "problem_scope": {
            "must_edit_priority_file": bool(must_touch),
            "reject_must_not_touch_without_proof": bool(must_not_touch),
            "prefer_immediate_cause_first": True,
            "method_lock_enabled": bool(must_touch_method),
        },
    }
    return contract


def contract_to_prompt_block(contract: dict[str, Any]) -> str:
    lines = [
        f"- ticket_key: {contract.get('ticket_key') or 'NON_ETABLI'}",
        f"- discipline: {contract.get('discipline')}",
        f"- problem_summary: {contract.get('problem_summary') or 'NON_ETABLI'}",
        f"- immediate_cause: {contract.get('immediate_cause') or 'NON_ETABLI'}",
        f"- crash_file: {contract.get('crash_file') or 'NON_ETABLI'}",
        f"- crash_method: {contract.get('crash_method') or 'NON_ETABLI'}",
        f"- must_touch_method: {contract.get('must_touch_method') or 'NON_ETABLI'}",
        f"- fix_style: {contract.get('fix_style')}",
        f"- must_touch: {', '.join(contract.get('must_touch', [])) or '(aucun)'}",
        f"- may_touch: {', '.join(contract.get('may_touch', [])) or '(aucun)'}",
        f"- must_not_touch: {', '.join(contract.get('must_not_touch', [])) or '(aucun)'}",
        f"- immediate_cause_terms: {', '.join(contract.get('immediate_cause_terms', [])) or '(aucun)'}",
        f"- acceptance_criteria: {' | '.join(contract.get('acceptance_criteria', [])) or '(aucun)'}",
        f"- oracle_tests: {' | '.join(contract.get('oracle_tests', [])) or '(aucun)'}",
        f"- localization_search_terms: {', '.join(contract.get('localization_search_terms', [])) or '(aucun)'}",
        f"- safe_fix_patterns: {', '.join(contract.get('safe_fix_patterns', []))}",
    ]
    reasons = contract.get("priority_reasons", {}) or {}
    for basename, reason in reasons.items():
        lines.append(f"- priority_reason[{basename}]: {reason}")
    return "\n".join(lines)


def basenames_to_paths(tracked: list[str], basenames: list[str]) -> list[str]:
    wanted = {name.lower() for name in basenames}
    if not wanted:
        return []
    return [path for path in tracked if PurePosixPath(path).name.lower() in wanted]
