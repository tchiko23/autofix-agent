from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from app.service.correction_contract_service import dedupe_keep_order
from app.service.llm_service import OllamaPlannerClient


class LocalizerAgent:
    def __init__(self, llm: OllamaPlannerClient | None, prompts_dir: Path, enabled: bool = True):
        self.llm = llm
        self.prompts_dir = prompts_dir
        self.enabled = enabled and llm is not None

    def localize(self, contract: dict[str, Any], selected_files: list[str], file_map: dict[str, str], diagnosis: dict[str, Any] | None = None) -> dict[str, Any]:
        target_file = self._deterministic_target_file(contract, selected_files, diagnosis=diagnosis)
        method_lock = str(contract.get('must_touch_method') or '').strip()
        target_method = method_lock or str(contract.get('crash_method') or '').strip()
        support_paths = [path for path in selected_files if path != target_file][:2]
        localization = {
            'expected_target_method': method_lock,
            'diagnosis_target_file': str((diagnosis or {}).get('recommended_target_file') or ''),
            'target_file': target_file,
            'target_method': target_method,
            'support_paths': support_paths,
            'planning_files': dedupe_keep_order([target_file] + support_paths),
            'mode': 'deterministic',
            'evidence': [str(x) for x in contract.get('immediate_cause_terms', [])[:8]],
            'replace_strategy_preference': str((diagnosis or {}).get('replace_strategy_preference') or contract.get('replace_strategy_preference') or 'whole_method'),
            'reasoning': [str(x) for x in (diagnosis or {}).get('diagnostic_rationale', [])[:4]],
        }
        if self.enabled and len(selected_files) > 1 and target_file and not method_lock:
            prompt = (self.prompts_dir / 'localization_prompt.md').read_text(encoding='utf-8')
            prompt = prompt.replace('{{CONTRACT}}', json.dumps(contract, ensure_ascii=False, indent=2))
            prompt = prompt.replace('{{SELECTED_FILES}}', json.dumps(selected_files, ensure_ascii=False, indent=2))
            prompt = prompt.replace('{{FILE_CONTEXT}}', json.dumps({k: file_map.get(k, '')[:2500] for k in localization['planning_files']}, ensure_ascii=False, indent=2))
            try:
                response = self.llm.generate_json(prompt=prompt, system='Réponds uniquement en JSON strict valide.')
                chosen = str(response.get('target_file', '')).replace('\\', '/').strip()
                if chosen in selected_files:
                    localization['target_file'] = chosen
                    localization['mode'] = 'llm_verified'
                chosen_method = str(response.get('target_method', '')).strip()
                if chosen_method:
                    localization['llm_target_method'] = chosen_method
                    if not method_lock:
                        localization['target_method'] = chosen_method
                localization['support_paths'] = [p for p in selected_files if p != localization['target_file']][:2]
                localization['planning_files'] = dedupe_keep_order([localization['target_file']] + localization['support_paths'])
                llm_evidence = response.get('evidence', [])
                if isinstance(llm_evidence, list):
                    localization['evidence'] = dedupe_keep_order(localization['evidence'] + [str(x) for x in llm_evidence if str(x).strip()])[:12]
            except Exception:
                localization['mode'] = 'deterministic_fallback'
        elif method_lock:
            localization['mode'] = 'deterministic_method_lock'
        localization['target_basename'] = PurePosixPath(localization['target_file']).name if localization['target_file'] else ''
        return localization

    def _deterministic_target_file(self, contract: dict[str, Any], selected_files: list[str], diagnosis: dict[str, Any] | None = None) -> str:
        # Priorité 1 : recommandation explicite du diagnosis agent si applicable.
        chosen = str((diagnosis or {}).get('recommended_target_file') or '').replace('\\', '/').strip()
        if chosen in selected_files:
            return chosen
        # Priorité 2 : crash_file issu de la stack trace (source de vérité la plus fiable).
        # Corrige le cas où `must_touch` contient plusieurs fichiers et où `git ls-files`
        # réordonne la sélection dans un ordre qui ne reflète pas la priorité métier.
        crash_file = str(contract.get('crash_file') or '').lower()
        if crash_file:
            for path in selected_files:
                if PurePosixPath(path).name.lower() == crash_file:
                    return path
        # Priorité 3 : premier fichier `must_touch` trouvé dans la sélection (fallback).
        must_touch = {str(x).lower() for x in contract.get('must_touch', []) if str(x).strip()}
        for path in selected_files:
            if PurePosixPath(path).name.lower() in must_touch:
                return path
        return selected_files[0] if selected_files else ''
