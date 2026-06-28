from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from app.config.settings import settings
from app.model.schemas import RunOptions
from app.service.agent_service import FixAgent


def _force_utf8_stdout() -> None:
    """v9.7: la console Windows par defaut est en cp1252 et plante sur les
    caracteres Unicode comme U+2248 (approximately) ou U+2192 (right arrow)
    qui peuvent apparaitre dans les sorties JSON. On reconfigure stdout en
    UTF-8 avec errors='replace' pour ne plus jamais planter sur l'encodage,
    quel que soit le contenu du resultat retourne par le pipeline.
    """
    try:
        # Python 3.7+: reconfigure() est la voie propre
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # Fallback: enrouler stdout dans un TextIOWrapper UTF-8
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
        except Exception:
            # Dernier recours: si meme l'enrobage echoue, on ne fait rien.
            # Le code main() utilisera ensure_ascii=True comme garde-fou.
            pass


def main() -> None:
    _force_utf8_stdout()

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--analysis-file", default="analysis/issue_analysis.txt")
    parser.add_argument("--base-branch", default=None)
    parser.add_argument("--source-branch", default=None)
    parser.add_argument("--target-branch", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply-patch", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--create-mr", action="store_true")
    args = parser.parse_args()

    options = RunOptions(
        repo=Path(args.repo),
        analysis_file=Path(args.analysis_file),
        base_branch=args.base_branch,
        source_branch=args.source_branch,
        target_branch=args.target_branch,
        dry_run=args.dry_run,
        apply_patch=args.apply_patch,
        commit=args.commit,
        push=args.push,
        create_mr=args.create_mr,
    )
    result = FixAgent(settings).run(options)
    # v9.7: ensure_ascii=True comme double garde-fou, au cas ou la
    # reconfiguration UTF-8 ci-dessus aurait echoue silencieusement.
    # ascii-safe est plus laid mais ne plantera JAMAIS la sortie batch.
    try:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except UnicodeEncodeError:
        print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
