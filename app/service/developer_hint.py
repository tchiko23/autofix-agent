"""app.service.developer_hint — Validation et exploitation du hint dev/expert v9.16.6.

Permet a un developpeur/expert de glisser dans le JSON Rovo un champ
`developer_hint` indiquant ou et comment corriger. Le bundle valide
factuellement le hint, et si OK part en mode "court-circuit" : un seul
appel LLM cible sur la methode indiquee, au lieu de 20 tours d'agent.

Format du hint dans le JSON :

  "developer_hint": {
    "fix_intent": "Ajouter DISTINCT sur la requete JPQL",
    "target_class": "SupportPermissionDaoImpl",
    "target_method": "requeteRecupererLaListeDesSupportsParLibellesOuIsin",
    "target_file_hint": "SupportPermissionDaoImpl.java",
    "fix_type": "add_distinct|null_check|business_logic|other",
    "rationale": "Le doublon vient de la jointure avec support_permission_produit"
  }

Validation factuelle (decision validee Q2) :
  1. Le fichier existe dans le repo (recherche par nom)
  2. La methode existe dans ce fichier (grep)
Si les 2 passent : le hint est utilisable -> patch cible.
Sinon : fallback cascade normale (decision validee Q3 option C).

PAS de validation LLM (decision validee Q4) : trop couteuse pour le gain.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("autofix-agent.developer_hint")

# Types de fix acceptes. Tout autre valeur tombe dans "other".
KNOWN_FIX_TYPES = {
    "add_distinct",
    "null_check",
    "business_logic",
    "validation",
    "exception_handling",
    "other",
}


@dataclass
class DeveloperHint:
    """Hint structure tel que recupere du JSON."""

    fix_intent: str = ""
    target_class: str = ""
    target_method: str = ""
    target_file_hint: str = ""
    fix_type: str = "other"
    rationale: str = ""

    def is_minimally_populated(self) -> bool:
        """Le hint a-t-il assez d'info pour etre exploitable ?

        On exige a minima : target_method ET (target_file_hint OU target_class).
        Sans cible identifiable, le hint est inutilisable.
        """
        return bool(self.target_method) and bool(
            self.target_file_hint or self.target_class
        )


@dataclass
class HintValidationResult:
    """Resultat de la validation factuelle du hint."""

    is_valid: bool
    resolved_file: Path | None = None
    resolved_method: str = ""
    failed_checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "resolved_file": str(self.resolved_file) if self.resolved_file else None,
            "resolved_method": self.resolved_method,
            "failed_checks": list(self.failed_checks),
            "warnings": list(self.warnings),
        }


def extract_hint(rovo_json: dict[str, Any]) -> DeveloperHint | None:
    """Lit le champ developer_hint du JSON Rovo.

    Retourne None si absent ou pas un dict. Retourne aussi None si le
    hint est present mais ne contient AUCUNE info utile (toutes valeurs
    vides) - c'est le cas par defaut quand le champ existe juste comme
    placeholder.
    """
    raw = rovo_json.get("developer_hint") if rovo_json else None
    if not isinstance(raw, dict):
        return None

    hint = DeveloperHint(
        fix_intent=(raw.get("fix_intent") or "").strip(),
        target_class=(raw.get("target_class") or "").strip(),
        target_method=(raw.get("target_method") or "").strip(),
        target_file_hint=(raw.get("target_file_hint") or "").strip(),
        fix_type=(raw.get("fix_type") or "other").strip().lower(),
        rationale=(raw.get("rationale") or "").strip(),
    )
    # Normaliser fix_type sur valeur inconnue
    if hint.fix_type not in KNOWN_FIX_TYPES:
        hint.fix_type = "other"

    # Si tous les champs sont vides, considerer comme absent
    if not (hint.fix_intent or hint.target_class or hint.target_method
            or hint.target_file_hint or hint.rationale):
        return None

    return hint


def validate_hint(hint: DeveloperHint, repo_root: Path) -> HintValidationResult:
    """Valide factuellement le hint contre le repo.

    Q2 validee : on exige seulement que (1) le fichier existe ET
    (2) la methode existe dans ce fichier. Si les 2 passent : valide.
    """
    result = HintValidationResult(is_valid=False)

    if not hint.is_minimally_populated():
        result.failed_checks.append(
            "hint_underpopulated: target_method ou (target_file_hint/target_class) manquant"
        )
        return result

    # 1) Trouver le fichier
    resolved = _find_file(repo_root, hint.target_file_hint, hint.target_class)
    if not resolved:
        cible = hint.target_file_hint or f"{hint.target_class}.java"
        result.failed_checks.append(f"file_not_found: '{cible}' introuvable dans {repo_root}")
        return result
    result.resolved_file = resolved

    # 2) Verifier que la methode existe dans ce fichier
    if not _method_exists_in_file(resolved, hint.target_method):
        result.failed_checks.append(
            f"method_not_found: '{hint.target_method}' absente de {resolved.name}"
        )
        return result
    result.resolved_method = hint.target_method

    # Avertissements non bloquants
    if hint.fix_type == "other":
        result.warnings.append("fix_type='other' (pas de pattern dedie disponible)")
    if not hint.fix_intent:
        result.warnings.append("fix_intent vide : moins de contexte pour le LLM")

    result.is_valid = True
    return result


def _find_file(repo_root: Path, file_hint: str, class_name: str) -> Path | None:
    """Cherche le fichier indique par le hint dans le repo.

    Strategie :
      1. Si file_hint contient un separateur de chemin, essayer comme path direct
      2. Sinon, rglob par nom de fichier
      3. Si rien et class_name fourni, rglob '<class_name>.java'
    """
    candidates: list[Path] = []

    if file_hint:
        # Path direct
        direct = repo_root / file_hint
        if direct.is_file():
            return direct
        # Recherche par nom de fichier (basename)
        basename = Path(file_hint).name
        candidates.extend(repo_root.rglob(basename))

    if not candidates and class_name:
        candidates.extend(repo_root.rglob(f"{class_name}.java"))

    # Filtrer pour ne garder que les vrais fichiers (pas les repertoires)
    files = [p for p in candidates if p.is_file()]
    if not files:
        return None

    # Heuristique : si plusieurs candidats, preferer ceux dans /main/ vs /test/
    main_files = [p for p in files if "/test/" not in p.as_posix()
                  and "\\test\\" not in str(p)]
    if main_files:
        return main_files[0]
    return files[0]


def _method_exists_in_file(file_path: Path, method_name: str) -> bool:
    """Verifie par recherche texte que la methode existe dans le fichier."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # Cherche le pattern <method_name>( en debut de mot, eventuellement precede
    # de modificateurs Java. Suffisant pour Java a 99%.
    pattern = re.compile(rf"\b{re.escape(method_name)}\s*\(", re.MULTILINE)
    return bool(pattern.search(content))


def build_hint_summary_for_log(hint: DeveloperHint,
                                validation: HintValidationResult) -> str:
    """Resume textuel pour le log au moment du traitement."""
    if validation.is_valid:
        return (f"[HINT] valide -> court-circuit sur "
                f"{validation.resolved_file.name}::{validation.resolved_method} "
                f"(fix_type={hint.fix_type})")
    return (f"[HINT] invalide -> fallback cascade normale. "
            f"raisons: {', '.join(validation.failed_checks)}")
