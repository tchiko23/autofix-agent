"""app.service.pattern_based_fix_generator — Génération de fix par pattern v9.13.

Le bundle reconnait des patterns d'exception Java courants et produit un fix
syntaxiquement correct SANS appel LLM. Le fix est ensuite passe a l'applicateur
deterministe v9.12 pour application.

Patterns supportes :
  1. NullPointerException : null guard sur l'expression coupable
  2. StringIndexOutOfBoundsException : guard de longueur avant .substring()
  3. ClassCastException : instanceof check avant cast
  4. NumberFormatException : try/catch avec valeur par defaut

Strategie A (validee user) : guard inline, minimal, chirurgical.
On ne change pas la signature, on n'ajoute pas de log, on touche au
minimum de lignes possible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# Mapping exception class -> pattern handler
SUPPORTED_EXCEPTIONS = {
    "java.lang.NullPointerException": "null_pointer",
    "NullPointerException": "null_pointer",
    "java.lang.StringIndexOutOfBoundsException": "string_index_oob",
    "StringIndexOutOfBoundsException": "string_index_oob",
    "java.lang.IndexOutOfBoundsException": "string_index_oob",
    "java.lang.ClassCastException": "class_cast",
    "ClassCastException": "class_cast",
    "java.lang.NumberFormatException": "number_format",
    "NumberFormatException": "number_format",
}


@dataclass
class PatternFixResult:
    """Resultat d'une tentative de generation de fix par pattern."""
    success: bool
    pattern_id: str = ""
    new_method_source: str = ""
    explanation: str = ""
    reason_failed: str = ""


def detect_pattern(exception_class: str) -> str | None:
    """Identifie le pattern depuis le nom de la classe d'exception."""
    if not exception_class:
        return None
    return SUPPORTED_EXCEPTIONS.get(exception_class.strip())


def generate_fix(
    pattern_id: str,
    method_source: str,
    exception_message: str = "",
    crash_line_offset: int = -1,
) -> PatternFixResult:
    """Genere un nouveau source de methode avec un fix applique au pattern."""
    if not method_source:
        return PatternFixResult(False, reason_failed="empty_method_source")

    handlers = {
        "null_pointer": _fix_null_pointer,
        "string_index_oob": _fix_string_index_oob,
        "class_cast": _fix_class_cast,
        "number_format": _fix_number_format,
    }
    handler = handlers.get(pattern_id)
    if not handler:
        return PatternFixResult(False, reason_failed=f"unknown_pattern_{pattern_id}")

    try:
        return handler(method_source, exception_message, crash_line_offset)
    except Exception as exc:
        return PatternFixResult(False, reason_failed=f"handler_exception:{exc}")


# -----------------------------------------------------------------------------
# Pattern 1 : NullPointerException
# -----------------------------------------------------------------------------

def _fix_null_pointer(method_source: str, exc_message: str, crash_line: int) -> PatternFixResult:
    """Ajoute un null guard sur l'expression la plus probable.

    Strategie : on cherche l'expression qui derefe (.foo() ou .field) sur un
    objet qui peut etre null. Cas typiques :
      - x.foo() ou x est un parametre -> if (x == null) return defaultValue;
      - this.field.foo() -> if (this.field == null) return defaultValue;
      - getX().foo() -> Object x = getX(); if (x == null) ...; on extrait l'appel

    Si la signature retourne void -> return; sinon -> return defaultValue selon le type.
    """
    # Extraire signature, type retour, corps
    sig_match = re.match(
        r"(?ms)^\s*"
        r"((?:@[\w.]+(?:\([^)]*\))?\s*\n?\s*)*)"
        r"((?:public|protected|private)?\s*"
        r"(?:static\s+|final\s+|synchronized\s+|abstract\s+)*"
        r"(?:<[^>]+>\s*)?)"
        r"([\w.<>\[\],\s]+?)\s+"
        r"(\w+)\s*"
        r"\(([^)]*)\)"
        r"(\s*(?:throws\s+[\w.,\s]+)?)"
        r"\s*\{",
        method_source,
    )
    if not sig_match:
        return PatternFixResult(False, reason_failed="signature_unparseable")

    annotations = sig_match.group(1)
    modifiers = sig_match.group(2)
    return_type = sig_match.group(3).strip()
    method_name = sig_match.group(4)
    params = sig_match.group(5)
    throws = sig_match.group(6)

    brace_start = method_source.find("{", sig_match.end() - 1)
    body_content = method_source[brace_start + 1:-1].rstrip()

    # Chercher la 1ere expression `name.method(` ou `name.field` dans le corps
    # Heuristique simple : la premiere variable qui derefere
    deref_match = re.search(r"\b(\w+)\s*\.\s*\w+\s*\(", body_content)
    if not deref_match:
        deref_match = re.search(r"\b(\w+)\s*\.\s*\w+", body_content)
    if not deref_match:
        return PatternFixResult(False, reason_failed="no_dereference_found")

    var_name = deref_match.group(1)
    # Skip si c'est 'this' ou un keyword
    if var_name in {"this", "super", "if", "for", "while", "return", "new"}:
        # Chercher la suivante
        for m in re.finditer(r"\b(\w+)\s*\.\s*\w+\s*\(", body_content):
            if m.group(1) not in {"this", "super", "if", "for", "while", "return", "new", "String", "Integer", "Long", "Double", "List", "Map", "Optional"}:
                var_name = m.group(1)
                break
        else:
            return PatternFixResult(False, reason_failed="no_safe_dereference_target")

    # Determiner la valeur de retour selon le type
    default_value = _default_value_for_type(return_type)
    if return_type == "void":
        guard = f"        if ({var_name} == null) {{\n            return;\n        }}"
    else:
        guard = f"        if ({var_name} == null) {{\n            return {default_value};\n        }}"

    # Detecter l'indentation du corps existant (pour matcher le style)
    body_indent = _detect_body_indent(body_content)
    if body_indent and body_indent != "        ":
        # Reindenter le guard avec le bon style
        guard = guard.replace("        ", body_indent)

    # Construire nouveau corps : guard + ancien corps
    new_body = guard + "\n" + body_content

    # Reconstruire la methode
    sig_line = (modifiers + return_type + " " + method_name + "(" + params + ")" + throws).strip()
    new_source = annotations + sig_line + " {\n" + new_body + "\n" + _get_close_indent(method_source) + "}"

    return PatternFixResult(
        success=True,
        pattern_id="null_pointer",
        new_method_source=new_source,
        explanation=f"Null guard ajoute sur la variable '{var_name}' avant son dereferencement",
    )


# -----------------------------------------------------------------------------
# Pattern 2 : StringIndexOutOfBoundsException
# -----------------------------------------------------------------------------

def _fix_string_index_oob(method_source: str, exc_message: str, crash_line: int) -> PatternFixResult:
    """Ajoute un guard de longueur avant un .substring() ou un .charAt().

    Strategie A (chirurgicale) : on cherche `<expr>.substring(N)` ou `<expr>.substring(N, M)`
    et on transforme en `(<expr>.length() > N ? <expr>.substring(N) : "")`.

    Example case :
      String aggregated = getItems().stream().reduce(...).substring(1);
    devient :
      String aggregated = (getItems().stream().reduce(...).length() > 1
              ? getItems().stream().reduce(...).substring(1) : "");

    Mais ca duplique l'expression. Meilleure approche : extraire dans une variable temporaire.
    """
    sig_match = re.match(
        r"(?ms)^\s*"
        r"((?:@[\w.]+(?:\([^)]*\))?\s*\n?\s*)*)"
        r"((?:public|protected|private)?\s*"
        r"(?:static\s+|final\s+|synchronized\s+|abstract\s+)*"
        r"(?:<[^>]+>\s*)?)"
        r"([\w.<>\[\],\s]+?)\s+"
        r"(\w+)\s*"
        r"\(([^)]*)\)"
        r"(\s*(?:throws\s+[\w.,\s]+)?)"
        r"\s*\{",
        method_source,
    )
    if not sig_match:
        return PatternFixResult(False, reason_failed="signature_unparseable")

    brace_start = method_source.find("{", sig_match.end() - 1)
    body_content = method_source[brace_start + 1:-1].rstrip()

    # Strategie pour StringIndexOutOfBoundsException :
    # On cherche une expression assignee a une variable, dont la valeur est
    # <truc>.substring(N). Example case :
    #   String aggregated = <complex_expr>.substring(1);
    # devient :
    #   String <var>Reduced = <complex_expr>;
    #   String aggregated = <var>Reduced.length() > 1 ? <var>Reduced.substring(1) : "";

    # Pattern multiline : capture l'assignement qui se termine par .substring(N);
    # Le .+? matche tout y compris les sauts de ligne (re.DOTALL)
    assign_pattern = re.compile(
        r"^(\s*)([\w<>,\[\]\s]+?)\s+(\w+)\s*=\s*(.+?)\.substring\((\d+)\)\s*;",
        re.MULTILINE | re.DOTALL,
    )
    m = assign_pattern.search(body_content)
    if m:
        indent = m.group(1)
        var_type = m.group(2).strip()
        var_name = m.group(3)
        expr = m.group(4).strip()
        start_idx = int(m.group(5))
        # Generer le replacement avec variable temporaire
        tmp_name = f"_reduced_{var_name}"
        new_block = (
            f"{indent}String {tmp_name} = {expr};\n"
            f"{indent}{var_type} {var_name} = {tmp_name}.length() > {start_idx} "
            f"? {tmp_name}.substring({start_idx}) : \"\";"
        )
        new_body = body_content[:m.start()] + new_block + body_content[m.end():]
    else:
        # Cas plus simple : un .substring(N) sans assignment ni complexite
        simple_pattern = re.compile(r"(\b\w+)\.substring\((\d+)\)")
        sm = simple_pattern.search(body_content)
        if not sm:
            return PatternFixResult(False, reason_failed="no_substring_call_found")
        var = sm.group(1)
        idx = int(sm.group(2))
        # Wrap toute l'expression dans un guard
        # Strategie minimale : remplacer X.substring(N) par (X != null && X.length() > N ? X.substring(N) : "")
        new_call = f"({var} != null && {var}.length() > {idx} ? {var}.substring({idx}) : \"\")"
        new_body = body_content[:sm.start()] + new_call + body_content[sm.end():]

    # Reconstruire la methode
    annotations = sig_match.group(1)
    modifiers = sig_match.group(2)
    return_type = sig_match.group(3).strip()
    method_name = sig_match.group(4)
    params = sig_match.group(5)
    throws = sig_match.group(6)
    sig_line = (modifiers + return_type + " " + method_name + "(" + params + ")" + throws).strip()
    new_source = annotations + sig_line + " {\n" + new_body + "\n" + _get_close_indent(method_source) + "}"

    return PatternFixResult(
        success=True,
        pattern_id="string_index_oob",
        new_method_source=new_source,
        explanation=f"Guard de longueur ajoute avant .substring()",
    )


# -----------------------------------------------------------------------------
# Pattern 3 : ClassCastException
# -----------------------------------------------------------------------------

def _fix_class_cast(method_source: str, exc_message: str, crash_line: int) -> PatternFixResult:
    """Ajoute un instanceof check avant un cast."""
    sig_match = re.match(
        r"(?ms)^\s*"
        r"((?:@[\w.]+(?:\([^)]*\))?\s*\n?\s*)*)"
        r"((?:public|protected|private)?\s*"
        r"(?:static\s+|final\s+|synchronized\s+|abstract\s+)*"
        r"(?:<[^>]+>\s*)?)"
        r"([\w.<>\[\],\s]+?)\s+"
        r"(\w+)\s*"
        r"\(([^)]*)\)"
        r"(\s*(?:throws\s+[\w.,\s]+)?)"
        r"\s*\{",
        method_source,
    )
    if not sig_match:
        return PatternFixResult(False, reason_failed="signature_unparseable")

    brace_start = method_source.find("{", sig_match.end() - 1)
    body_content = method_source[brace_start + 1:-1].rstrip()

    # Chercher un cast : (TypeName) <expr>
    cast_pattern = re.compile(r"\(\s*([A-Z]\w+)\s*\)\s*(\w+)")
    m = cast_pattern.search(body_content)
    if not m:
        return PatternFixResult(False, reason_failed="no_cast_found")
    cast_type = m.group(1)
    var_name = m.group(2)

    return_type = sig_match.group(3).strip()
    default_value = _default_value_for_type(return_type)
    indent = _detect_body_indent(body_content) or "        "

    if return_type == "void":
        guard = f"{indent}if (!({var_name} instanceof {cast_type})) {{\n{indent}    return;\n{indent}}}"
    else:
        guard = f"{indent}if (!({var_name} instanceof {cast_type})) {{\n{indent}    return {default_value};\n{indent}}}"

    new_body = guard + "\n" + body_content

    annotations = sig_match.group(1)
    modifiers = sig_match.group(2)
    method_name = sig_match.group(4)
    params = sig_match.group(5)
    throws = sig_match.group(6)
    sig_line = (modifiers + return_type + " " + method_name + "(" + params + ")" + throws).strip()
    new_source = annotations + sig_line + " {\n" + new_body + "\n" + _get_close_indent(method_source) + "}"

    return PatternFixResult(
        success=True,
        pattern_id="class_cast",
        new_method_source=new_source,
        explanation=f"instanceof check ajoute sur '{var_name}' avant cast vers {cast_type}",
    )


# -----------------------------------------------------------------------------
# Pattern 4 : NumberFormatException
# -----------------------------------------------------------------------------

def _fix_number_format(method_source: str, exc_message: str, crash_line: int) -> PatternFixResult:
    """Wrap un parseInt/parseLong/parseDouble dans un try/catch."""
    sig_match = re.match(
        r"(?ms)^\s*"
        r"((?:@[\w.]+(?:\([^)]*\))?\s*\n?\s*)*)"
        r"((?:public|protected|private)?\s*"
        r"(?:static\s+|final\s+|synchronized\s+|abstract\s+)*"
        r"(?:<[^>]+>\s*)?)"
        r"([\w.<>\[\],\s]+?)\s+"
        r"(\w+)\s*"
        r"\(([^)]*)\)"
        r"(\s*(?:throws\s+[\w.,\s]+)?)"
        r"\s*\{",
        method_source,
    )
    if not sig_match:
        return PatternFixResult(False, reason_failed="signature_unparseable")

    brace_start = method_source.find("{", sig_match.end() - 1)
    body_content = method_source[brace_start + 1:-1].rstrip()

    # Pattern : assignement avec parseInt/Long/Double
    parse_pattern = re.compile(
        r"^(\s*)([\w<>,\[\]\s]+?)\s+(\w+)\s*=\s*"
        r"(Integer|Long|Double|Float|Short|Byte)\.parse\w+\((.+?)\)\s*;",
        re.MULTILINE,
    )
    m = parse_pattern.search(body_content)
    if not m:
        return PatternFixResult(False, reason_failed="no_parse_call_found")

    indent = m.group(1)
    var_type = m.group(2).strip()
    var_name = m.group(3)
    wrapper = m.group(4)
    parse_arg = m.group(5)

    # Valeur par defaut selon type
    if wrapper in {"Integer", "Short", "Byte"}:
        default = "0"
        ret_type = "int" if var_type in {"int", "Integer"} else var_type
    elif wrapper == "Long":
        default = "0L"
    elif wrapper in {"Double", "Float"}:
        default = "0.0"
    else:
        default = "0"

    new_block = (
        f"{indent}{var_type} {var_name};\n"
        f"{indent}try {{\n"
        f"{indent}    {var_name} = {wrapper}.parseInt({parse_arg});\n".replace("parseInt", f"parse{wrapper[0].upper()+wrapper[1:].lower()}" if wrapper != "Integer" else "parseInt")
        + f"{indent}}} catch (NumberFormatException e) {{\n"
        f"{indent}    {var_name} = {default};\n"
        f"{indent}}}"
    )
    # Simpler: re-construire avec la bonne methode parseXxx
    parse_method_map = {
        "Integer": "parseInt", "Long": "parseLong", "Double": "parseDouble",
        "Float": "parseFloat", "Short": "parseShort", "Byte": "parseByte",
    }
    parse_method = parse_method_map.get(wrapper, "parseInt")
    new_block = (
        f"{indent}{var_type} {var_name};\n"
        f"{indent}try {{\n"
        f"{indent}    {var_name} = {wrapper}.{parse_method}({parse_arg});\n"
        f"{indent}}} catch (NumberFormatException e) {{\n"
        f"{indent}    {var_name} = {default};\n"
        f"{indent}}}"
    )

    new_body = body_content[:m.start()] + new_block + body_content[m.end():]

    annotations = sig_match.group(1)
    modifiers = sig_match.group(2)
    return_type = sig_match.group(3).strip()
    method_name = sig_match.group(4)
    params = sig_match.group(5)
    throws = sig_match.group(6)
    sig_line = (modifiers + return_type + " " + method_name + "(" + params + ")" + throws).strip()
    new_source = annotations + sig_line + " {\n" + new_body + "\n" + _get_close_indent(method_source) + "}"

    return PatternFixResult(
        success=True,
        pattern_id="number_format",
        new_method_source=new_source,
        explanation=f"try/catch NumberFormatException ajoute autour de {wrapper}.{parse_method}()",
    )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _default_value_for_type(java_type: str) -> str:
    """Retourne une valeur par defaut Java pour un type donne."""
    t = java_type.strip()
    if t == "void":
        return ""
    if t == "boolean":
        return "false"
    if t in {"int", "short", "byte"}:
        return "0"
    if t == "long":
        return "0L"
    if t == "float":
        return "0.0f"
    if t == "double":
        return "0.0"
    if t == "char":
        return "'\\0'"
    if t == "String":
        return "\"\""
    # Type reference -> null
    return "null"


def _detect_body_indent(body: str) -> str:
    """Detecte l'indentation typique des lignes du corps de methode."""
    lines = body.splitlines()
    for line in lines:
        if line.strip():
            stripped = line.lstrip()
            return line[:len(line) - len(stripped)]
    return "        "


def _get_close_indent(method_source: str) -> str:
    """Detecte l'indentation de l'accolade fermante."""
    lines = method_source.rstrip().splitlines()
    if lines and lines[-1].strip() == "}":
        line = lines[-1]
        stripped = line.lstrip()
        return line[:len(line) - len(stripped)]
    return "    "
