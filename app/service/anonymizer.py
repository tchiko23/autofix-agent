"""app.service.anonymizer — Anonymisation v9.13.1.

Motivations :
1. RGPD : ne pas exposer de donnees personnelles dans les MR automatiques
2. Qualite technique : retirer le bruit informationnel inutile au pipeline
3. Professionnalisme : eviter de citer des employes dans des MR automatiques
4. Genericite : rendre le bundle independant des noms d'equipes specifiques

5 categories de donnees traitees :
  - Cat 1 : Noms de personnes cites (Prenom NOM, M./Mme. xxx)         → [personne]
  - Cat 2 : Auteurs de commits Git (Author: xxx, by xxx)              → [auteur]
  - Cat 3 : Adresses email                                            → [email]
  - Cat 4 : Identifiants utilisateurs production (format s1lNNNN, c:HHHHHHHHHHHHHHHH)       → [user_id_hash:xxx]
  - Cat 5 : Names of third-party organizations                        → PRESERVES (contexte metier)
"""
from __future__ import annotations

import hashlib
import re


# Cat 3 : email
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# Cat 2 : auteurs commits Git
COMMIT_AUTHOR_PATTERNS = [
    re.compile(r"Author:\s*[^\n<]+(?:<[^>]+>)?", re.IGNORECASE),
    re.compile(r"\bpar\s+([A-Z][a-z]+\s+[A-Z][a-z]+|[A-Z]{2,}\s+[A-Z][a-z]+)\b"),
    re.compile(r"\bauthored by\s+([A-Z][a-z]+\s+[A-Z][a-z]+|[A-Z]{2,}\s+[A-Z][a-z]+)\b", re.IGNORECASE),
    re.compile(r"\bcommit by\s+([A-Z][a-z]+\s+[A-Z][a-z]+|[A-Z]{2,}\s+[A-Z][a-z]+)\b", re.IGNORECASE),
]

# Cat 1 : noms de personnes - patterns francais courants
# On cherche des expressions "Le commentaire de XXX", "selon YYY", "M. ZZZ"
PERSON_NAME_PATTERNS = [
    # "Le commentaire d'<Prenom NOM>" / "Le commentaire de <Prenom NOM>"
    # Suivi par n'importe quoi (verbe, ponctuation, parenthese)
    re.compile(r"(?:Le\s+)?commentaire\s+d['e]\s*([A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z]{2,}(?:\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)*|[A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)", re.IGNORECASE),
    # "selon <Prenom NOM>"
    re.compile(r"\bselon\s+([A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z]{2,}(?:\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)*|[A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)", re.IGNORECASE),
    # "M. <Nom>" / "Mme <Nom>"
    re.compile(r"\b(?:M\.|Mme|Mlle|Mr|Mrs)\s+[A-Z][a-zéèêëàâäîïôöùûüç]+(?:\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)?"),
    # "<Prenom> <NOM> a indique" / "<Prenom> <NOM> indique"
    re.compile(r"\b([A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z]{2,})\s+(?:a\s+)?(?:indique|signale|precise|confirme|propose|suggere|mentionne)", re.IGNORECASE),
    # "le ticket de <Prenom NOM>"
    re.compile(r"\bticket\s+de\s+([A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z]{2,}(?:\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)*|[A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)", re.IGNORECASE),
    # "de la part de <Prenom NOM>"
    re.compile(r"\bde\s+la\s+part\s+de\s+([A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z]{2,}(?:\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)*|[A-Z][a-zéèêëàâäîïôöùûüç]+\s+[A-Z][a-zéèêëàâäîïôöùûüç]+)", re.IGNORECASE),
]

# Cat 4 : Identifiants utilisateurs production (format example: s1l9999, c:0000000000000000)
USER_ID_PATTERNS = [
    # Pattern type "s1l<digits>" (application user identifiers)
    re.compile(r"\bs\d+l\d{3,}\b"),
    # Pattern type "c:<16+ hex chars>" (application client identifiers)
    re.compile(r"\bc:[0-9a-f]{12,}\b"),
    # Pattern type "user_<digits>" / "user_id=<digits>" (générique)
    re.compile(r"\buser_(?:id[=:])?\d{4,}\b", re.IGNORECASE),
]


def _hash_user_id(user_id: str) -> str:
    """Hash court (8 chars) d'un user_id pour garder la tracabilite sans exposer l'ID brut."""
    h = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]
    return f"[user_id_hash:{h}]"


def anonymize_text(text: str, *, replacement_person: str = "[personne]",
                   replacement_email: str = "[email]",
                   replacement_author: str = "[auteur]") -> str:
    """Anonymise un texte selon les 4 categories de la v9.13.1.

    Cat 5 (names of third-party organizations) est PRESERVEE car elle fait
    partie du contexte metier necessaire au diagnostic. On ne touche pas a
    "ExampleCorp", "Acme Bank", "Globex", etc.

    Args:
        text: texte a anonymiser
        replacement_person/email/author: chaines de remplacement personnalisables

    Returns:
        texte anonymise
    """
    if not text:
        return text

    result = text

    # Cat 3 : emails (prioritaire car contient parfois prenom.nom@)
    result = EMAIL_PATTERN.sub(replacement_email, result)

    # Cat 2 : auteurs commits Git
    for pattern in COMMIT_AUTHOR_PATTERNS:
        result = pattern.sub(replacement_author, result)

    # Cat 1 : noms de personnes - on capture le contexte + le nom
    # Strategie : on remplace toute la sequence (commentaire de X + nom)
    for pattern in PERSON_NAME_PATTERNS:
        result = pattern.sub(lambda m: _replace_person_match(m, replacement_person), result)

    # Cat 4 : IDs utilisateurs production - hash
    for pattern in USER_ID_PATTERNS:
        result = pattern.sub(lambda m: _hash_user_id(m.group(0)), result)

    return result


def _replace_person_match(match: re.Match, replacement: str) -> str:
    """Remplace une occurrence "le commentaire de X" en "le commentaire de [personne]".

    On garde le contexte (verbe, preposition) et on substitue uniquement le nom.
    """
    full_match = match.group(0)
    # Si on a un groupe capture (le nom), on le remplace dans le match complet
    if match.lastindex and match.lastindex >= 1:
        name = match.group(1)
        return full_match.replace(name, replacement)
    # Sinon (M./Mme), on remplace tout le match
    return replacement


def anonymize_rovo_extraction(rovo: dict) -> dict:
    """Anonymise les champs textuels d'un JSON Rovo extrait.

    Champs anonymises (texte libre) :
      - diagnosis.immediate_cause
      - diagnosis.root_cause_hypothesis
      - fix_recommendation.approach_summary
      - fix_recommendation.rationale_business
      - rules_of_business[].summary_extract
      - similar_past_incidents[].similarity_reason
      - similar_past_incidents[].fix_pattern_applied
      - risks_and_uncertainties[].description
      - tests_to_add[].scenario

    Champs PRESERVES (techniques) :
      - crash.* (sauf exception_message qui peut contenir des PII)
      - localization.*
      - acceptance_criteria[].criterion (technique)
    """
    if not isinstance(rovo, dict):
        return rovo

    out = dict(rovo)

    # diagnosis
    if "diagnosis" in out and isinstance(out["diagnosis"], dict):
        diag = dict(out["diagnosis"])
        for k in ("immediate_cause", "root_cause_hypothesis"):
            if k in diag and isinstance(diag[k], str):
                diag[k] = anonymize_text(diag[k])
        out["diagnosis"] = diag

    # fix_recommendation
    if "fix_recommendation" in out and isinstance(out["fix_recommendation"], dict):
        fix = dict(out["fix_recommendation"])
        for k in ("approach_summary", "rationale_business"):
            if k in fix and isinstance(fix[k], str):
                fix[k] = anonymize_text(fix[k])
        out["fix_recommendation"] = fix

    # rules_of_business
    if "rules_of_business" in out and isinstance(out["rules_of_business"], list):
        rules = []
        for rg in out["rules_of_business"]:
            if isinstance(rg, dict):
                rg = dict(rg)
                if "summary_extract" in rg and isinstance(rg["summary_extract"], str):
                    rg["summary_extract"] = anonymize_text(rg["summary_extract"])
            rules.append(rg)
        out["rules_of_business"] = rules

    # similar_past_incidents
    if "similar_past_incidents" in out and isinstance(out["similar_past_incidents"], list):
        incidents = []
        for inc in out["similar_past_incidents"]:
            if isinstance(inc, dict):
                inc = dict(inc)
                for k in ("similarity_reason", "fix_pattern_applied"):
                    if k in inc and isinstance(inc[k], str):
                        inc[k] = anonymize_text(inc[k])
            incidents.append(inc)
        out["similar_past_incidents"] = incidents

    # risks_and_uncertainties
    if "risks_and_uncertainties" in out and isinstance(out["risks_and_uncertainties"], list):
        risks = []
        for r in out["risks_and_uncertainties"]:
            if isinstance(r, dict):
                r = dict(r)
                if "description" in r and isinstance(r["description"], str):
                    r["description"] = anonymize_text(r["description"])
            risks.append(r)
        out["risks_and_uncertainties"] = risks

    # tests_to_add
    if "tests_to_add" in out and isinstance(out["tests_to_add"], list):
        tests = []
        for t in out["tests_to_add"]:
            if isinstance(t, dict):
                t = dict(t)
                if "scenario" in t and isinstance(t["scenario"], str):
                    t["scenario"] = anonymize_text(t["scenario"])
            tests.append(t)
        out["tests_to_add"] = tests

    # Exception message peut contenir des PII (rare mais possible)
    if "crash" in out and isinstance(out["crash"], dict):
        crash = dict(out["crash"])
        if "exception_message" in crash and isinstance(crash["exception_message"], str):
            crash["exception_message"] = anonymize_text(crash["exception_message"])
        out["crash"] = crash

    return out
