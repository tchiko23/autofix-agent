from __future__ import annotations

from typing import Any


class ValidatorAgent:
    """Validation reporter.

    Les garde-fous bloquants sont appliqués en amont dans normalize_and_validate_plan().
    Cet agent synthétise surtout l'état du plan et les raisons de blocage pour le debug,
    l'observabilité et la livraison diagnostique éventuelle.
    """

    def __init__(self, min_patch_score: int, min_autocommit_score: int):
        self.min_patch_score = min_patch_score
        self.min_autocommit_score = min_autocommit_score

    def validate(self, plan: dict[str, Any], contract: dict[str, Any], localization: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        validation_score = int(plan.get('validation_score', 0) or 0)
        confidence_score = int(plan.get('confidence_score', 0) or 0)
        if plan.get('unable_to_fix'):
            reasons.append(str(plan.get('unable_to_fix_reason') or 'plan_unable_to_fix'))
        if plan.get('already_fixed'):
            reasons.append('already_fixed')
        if not plan.get('edits'):
            reasons.append('no_validated_edits')
        if validation_score < self.min_patch_score:
            reasons.append(f'validation_score_below_patch_threshold:{validation_score}<{self.min_patch_score}')
        if confidence_score < self.min_autocommit_score:
            reasons.append(f'model_confidence_below_autocommit_threshold:{confidence_score}<{self.min_autocommit_score}')
        can_apply_patch = (
            not plan.get('unable_to_fix')
            and not plan.get('already_fixed')
            and bool(plan.get('edits'))
            and validation_score >= self.min_patch_score
        )
        can_autocommit = (
            can_apply_patch
            and validation_score >= self.min_autocommit_score
            and confidence_score >= self.min_autocommit_score
        )
        delivery_recommendation = 'patch' if can_apply_patch else 'diagnostic_mr'
        return {
            'can_apply_patch': can_apply_patch,
            'can_autocommit': can_autocommit,
            'validation_score': validation_score,
            'model_confidence': confidence_score,
            'target_file': localization.get('target_file'),
            'target_method': localization.get('target_method'),
            'must_touch': contract.get('must_touch', []),
            'must_touch_method': contract.get('must_touch_method'),
            'reasons': reasons,
            'delivery_recommendation': delivery_recommendation,
        }
