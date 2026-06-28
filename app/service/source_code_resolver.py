"""app.service.source_code_resolver — Lecture du code source reel v9.13.

Quand Rovo ne fournit pas de code (le cas par defaut en v9.13 car Rovo n'a
pas acces au your GitLab instance), le bundle local doit lire le code source
lui-meme et enrichir le contrat avec les vraies informations :

  - target_method_source : corps complet de la methode cible
  - target_method_signature : signature parsee
  - target_class_fields : liste des champs d'instance avec type + nom
  - target_class_imports : liste des imports presents dans le fichier
  - target_class_methods_adjacent : noms des autres methodes de la classe

Ces informations sont ensuite utilisees par :
  - pattern_based_fix_generator pour produire un fix syntaxiquement correct
  - PatchPlannerAgent (mode legacy) pour avoir un prompt encore plus precis
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# Java import statement
_IMPORT_PATTERN = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;", re.MULTILINE)

# Java field declaration (instance fields, not local variables)
# Examples:
#   private String name;
#   protected final List<String> items = new ArrayList<>();
#   @Autowired private MyService service;
_FIELD_PATTERN = re.compile(
    r"(?ms)^\s*"
    r"(?:@[\w.]+(?:\([^)]*\))?\s*)*"                                # annotations
    r"(?:public|protected|private)?\s+"                              # visibility (often required for fields)
    r"(?:static\s+)?(?:final\s+)?(?:volatile\s+)?(?:transient\s+)?"  # modifiers
    r"([\w.<>\[\],\s]+?)\s+"                                         # type (group 1)
    r"(\w+)"                                                          # name (group 2)
    r"\s*(?:=\s*[^;]+)?"                                              # optional initializer
    r"\s*;"
)

# Java method declaration
_METHOD_PATTERN = re.compile(
    r"(?ms)"
    r"(?:@[\w.]+(?:\([^)]*\))?\s*\n?\s*)*"                       # annotations
    r"(?:public|protected|private)?\s*"                           # visibility
    r"(?:static\s+|final\s+|synchronized\s+|abstract\s+)*"        # modifiers
    r"(?:<[^>]+>\s*)?"                                            # generics
    r"([\w.<>\[\],\s]+?)\s+"                                      # return type (group 1)
    r"(\w+)\s*"                                                   # method name (group 2)
    r"\(([^)]*)\)"                                                # parameters (group 3)
    r"\s*(?:throws\s+[\w.,\s]+)?"
    r"\s*\{"
)


def _split_qualified_method(qualified: str) -> tuple[str, str]:
    """Decompose 'OrderResponseDto.getStatusMessage' en (class, method).

    Retourne ('', method) si pas de classe en prefixe.
    """
    if not qualified:
        return "", ""
    if "." not in qualified:
        return "", qualified
    parts = qualified.split(".")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", qualified


def extract_method_source(file_content: str, method_name: str) -> dict[str, Any]:
    """Extrait le source d'une methode Java avec sa signature et son corps.

    Retourne dict avec :
      - found: bool
      - source: str (methode complete avec signature + corps)
      - signature: str (juste la signature, p.ex. "private String getStatusMessage()")
      - body: str (corps entre { et } inclus)
      - return_type: str
      - parameters: str (liste textuelle des parametres)
      - annotations: list[str] (@JsonIgnore, @Override, etc.)
      - start_offset: int
      - end_offset: int
    """
    if not file_content or not method_name:
        return {"found": False}

    # Si method_name est qualifie (Class.method), prendre seulement la partie methode
    _, method_local = _split_qualified_method(method_name)
    target = method_local or method_name

    # Chercher chaque occurrence de signature et garder celle qui correspond
    for m in _METHOD_PATTERN.finditer(file_content):
        return_type = m.group(1).strip()
        name = m.group(2).strip()
        params = m.group(3).strip()
        if name != target:
            continue

        # On a trouve. Recuperer les annotations qui sont DANS le match
        # (le pattern regex capture deja les annotations en prefixe).
        sig_start = m.start()
        matched_text = file_content[sig_start:m.end()]
        # Extraire les annotations du texte matche
        annotations = re.findall(r"@[\w.]+(?:\([^)]*\))?", matched_text)
        # Aussi remonter au cas ou des annotations sont sur des lignes au-dessus
        # qui n'ont pas ete captures (cas tres rare)
        line_start = file_content.rfind("\n", 0, sig_start) + 1
        cursor = sig_start
        # Si la signature commence par une annotation, line_start pointe deja dessus.
        # Si elle commence par "private" ou similaire, on remonte pour voir si une
        # annotation existe au-dessus.
        leading = file_content[line_start:sig_start].strip()
        if not leading.startswith("@") and not annotations:
            # Cas ou la signature commence directement par "private" - regarder au-dessus
            scan = line_start
            while scan > 0:
                prev_end = scan - 1
                prev_start = file_content.rfind("\n", 0, prev_end) + 1
                prev_line = file_content[prev_start:prev_end].strip()
                if prev_line.startswith("@"):
                    extra = re.findall(r"@[\w.]+(?:\([^)]*\))?", prev_line)
                    annotations = extra + annotations
                    scan = prev_start
                    cursor = prev_start
                else:
                    break
        else:
            # Annotations sont dans le match, cursor = sig_start
            cursor = sig_start
            # Mais on veut pointer sur le DEBUT de la ligne de l'annotation
            line_start_of_match = file_content.rfind("\n", 0, sig_start) + 1
            cursor = line_start_of_match

        # Trouver l'accolade ouvrante et matcher la fermante
        brace_start = file_content.find("{", m.start())
        if brace_start < 0:
            continue
        depth = 0
        in_string = False
        in_char = False
        escape = False
        body_end = -1
        for i in range(brace_start, len(file_content)):
            ch = file_content[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if not in_char and ch == '"':
                in_string = not in_string
                continue
            if not in_string and ch == "'":
                in_char = not in_char
                continue
            if in_string or in_char:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body_end = i
                    break
        if body_end < 0:
            continue

        # Signature pure : on cherche la ligne contenant le nom de méthode + (
        # car le `(` est juste avant brace_start.
        paren_start = file_content.rfind("(", 0, brace_start)
        sig_line_start = file_content.rfind("\n", 0, paren_start) + 1
        signature_line = file_content[sig_line_start:brace_start].strip()
        # Source complete = depuis cursor (1re annotation) jusqu'a body_end
        source = file_content[cursor:body_end + 1]
        body = file_content[brace_start:body_end + 1]

        return {
            "found": True,
            "source": source,
            "signature": signature_line,
            "body": body,
            "return_type": return_type,
            "parameters": params,
            "annotations": annotations,
            "method_name": target,
            "start_offset": cursor,
            "end_offset": body_end + 1,
        }

    return {"found": False, "method_name": target}


def extract_class_fields(file_content: str) -> list[dict[str, str]]:
    """Extrait les champs d'instance de la classe Java.

    Retourne liste de {type, name}.

    Note: il y a une limite a notre regex - les fields tres complexes avec
    generics imbriques peuvent ne pas etre captures. On filtre aussi les
    declarations qui ressemblent a des variables locales (lignes a l'interieur
    de blocs).
    """
    fields = []
    # On filtre en cherchant uniquement les declarations au niveau de la classe
    # Heuristique simple : doit etre au niveau "indent <= 8 espaces ou <=2 tabs"
    # (les variables locales sont generalement plus indentees)
    for m in _FIELD_PATTERN.finditer(file_content):
        # Verifier que la declaration n'est PAS dans une methode (compter les accolades avant)
        before = file_content[:m.start()]
        open_braces = 0
        in_string = False
        in_char = False
        escape = False
        for ch in before:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if not in_char and ch == '"':
                in_string = not in_string
                continue
            if not in_string and ch == "'":
                in_char = not in_char
                continue
            if in_string or in_char:
                continue
            if ch == "{":
                open_braces += 1
            elif ch == "}":
                open_braces -= 1
        # Le niveau classe est a depth 1 (le bloc de la classe est ouvert mais
        # pas une methode/bloc imbrique)
        if open_braces != 1:
            continue

        type_str = m.group(1).strip()
        name = m.group(2).strip()
        # Filtrer les faux positifs courants (mots cles)
        if name in {"if", "for", "while", "switch", "return", "throw", "new", "this"}:
            continue
        if type_str in {"return", "throw"}:
            continue
        fields.append({"type": type_str, "name": name})
    return fields


def extract_class_imports(file_content: str) -> list[str]:
    """Liste les imports presents en haut du fichier Java."""
    return _IMPORT_PATTERN.findall(file_content or "")


def extract_class_method_names(file_content: str) -> list[str]:
    """Liste tous les noms de methodes presents dans la classe."""
    return sorted(set(m.group(2) for m in _METHOD_PATTERN.finditer(file_content or "")))


def enrich_contract_with_source(
    contract: dict[str, Any],
    repo_path: Path | str,
    log=print,
) -> dict[str, Any]:
    """Enrichit le contrat avec les vraies informations du code source.

    Lit le fichier `must_touch_paths[0]` (ou must_touch[0]) dans le repo
    et y extrait :
      - target_method_source (corps de la methode cible)
      - target_method_signature
      - target_class_fields (List[{type, name}])
      - target_class_imports (List[str])
      - target_class_methods_adjacent (List[str])

    Si le fichier est introuvable ou la methode introuvable, le contrat est
    retourne inchange avec un flag `source_resolution_failed`.
    """
    repo_path = Path(repo_path)
    enriched = dict(contract)

    # Trouver le path complet du fichier cible
    target_path = None
    must_touch_paths = contract.get("must_touch_paths") or []
    if must_touch_paths:
        candidate = repo_path / must_touch_paths[0]
        if candidate.is_file():
            target_path = candidate
    if target_path is None:
        # Fallback: chercher par nom de fichier (basename) dans le repo
        must_touch = contract.get("must_touch") or []
        if must_touch:
            basename = must_touch[0]
            matches = list(repo_path.rglob(basename))
            if matches:
                target_path = matches[0]

    if target_path is None or not target_path.is_file():
        log(f"[RESOLVER] target file not found in repo (must_touch={contract.get('must_touch')}, paths={contract.get('must_touch_paths')})")
        enriched["source_resolution_failed"] = "file_not_found"
        return enriched

    try:
        file_content = target_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError) as exc:
        log(f"[RESOLVER] cannot read {target_path}: {exc}")
        enriched["source_resolution_failed"] = "file_read_error"
        return enriched

    enriched["target_file_full_path"] = str(target_path)
    enriched["target_file_content"] = file_content
    enriched["target_class_imports"] = extract_class_imports(file_content)
    enriched["target_class_fields"] = extract_class_fields(file_content)
    enriched["target_class_methods_adjacent"] = extract_class_method_names(file_content)

    target_method = contract.get("must_touch_method") or contract.get("crash_method") or ""
    if not target_method:
        log("[RESOLVER] no target method in contract")
        enriched["source_resolution_failed"] = "no_target_method"
        return enriched

    method_info = extract_method_source(file_content, target_method)
    if not method_info.get("found"):
        log(f"[RESOLVER] method '{target_method}' not found in {target_path.name}")
        enriched["source_resolution_failed"] = "method_not_found"
        return enriched

    enriched["target_method_source"] = method_info["source"]
    enriched["target_method_signature"] = method_info["signature"]
    enriched["target_method_body"] = method_info["body"]
    enriched["target_method_return_type"] = method_info["return_type"]
    enriched["target_method_parameters"] = method_info["parameters"]
    enriched["target_method_annotations"] = method_info["annotations"]
    enriched["source_resolution_failed"] = False

    log(
        f"[RESOLVER] resolved {target_path.name}::{method_info['method_name']} "
        f"({len(method_info['source'])} chars, "
        f"{len(enriched['target_class_fields'])} fields, "
        f"{len(enriched['target_class_methods_adjacent'])} methods)"
    )
    return enriched
