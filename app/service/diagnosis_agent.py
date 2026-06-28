from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.service.correction_contract_service import dedupe_keep_order
from app.service.llm_service import OllamaPlannerClient


class DiagnosisAgent:
    def __init__(self, llm: OllamaPlannerClient | None, prompts_dir: Path, enabled: bool = True):
        self.llm = llm
        self.prompts_dir = prompts_dir
        self.enabled = enabled and llm is not None

    def diagnose(self, analysis_text: str, contract: dict[str, Any], selected_files: list[str], file_map: dict[str, str]) -> dict[str, Any]:
        target_method = str(contract.get("must_touch_method") or contract.get("crash_method") or "").strip()
        target_file = ""
        must_touch = {str(x).strip().lower() for x in contract.get("must_touch", []) if str(x).strip()}
        for path in selected_files:
            if path.split('/')[-1].lower() in must_touch:
                target_file = path
                break
        diagnosis = {
            "recommended_target_file": target_file,
            "recommended_target_method": target_method,
            "problem_summary": str(contract.get("problem_summary") or "").strip(),
            "immediate_cause": str(contract.get("immediate_cause") or "").strip(),
            "fix_style": str(contract.get("fix_style") or "minimal_local_null_guard").strip(),
            "acceptance_criteria": list(contract.get("acceptance_criteria", [])[:6]),
            "oracle_tests": list(contract.get("oracle_tests", [])[:6]),
            "diagnostic_rationale": [str(x) for x in contract.get("immediate_cause_terms", [])[:6]],
            "replace_strategy_preference": "whole_method",
        }
        strong_contract = bool(target_file and target_method and diagnosis['problem_summary'] and diagnosis['immediate_cause'] and diagnosis['acceptance_criteria'] and diagnosis['oracle_tests'])
        if not self.enabled or not selected_files or strong_contract:
            diagnosis["mode"] = "deterministic" if strong_contract or not self.enabled else "deterministic"
            return diagnosis
        prompt = (self.prompts_dir / 'diagnosis_prompt.md').read_text(encoding='utf-8')
        prompt = prompt.replace('{{ANALYSIS_TEXT}}', analysis_text)
        prompt = prompt.replace('{{CONTRACT}}', json.dumps(contract, ensure_ascii=False, indent=2))
        prompt = prompt.replace('{{SELECTED_FILES}}', json.dumps(selected_files, ensure_ascii=False, indent=2))
        prompt = prompt.replace('{{FILE_CONTEXT}}', json.dumps({k: file_map.get(k, '')[:3500] for k in selected_files[:4]}, ensure_ascii=False, indent=2))
        try:
            response = self.llm.generate_json(prompt=prompt, system='Réponds uniquement en JSON strict valide.')
        except Exception:
            diagnosis["mode"] = "deterministic_fallback"
            return diagnosis
        if isinstance(response, dict):
            chosen = str(response.get('recommended_target_file', '')).replace('\\','/').strip()
            if chosen in selected_files:
                diagnosis['recommended_target_file'] = chosen
            meth = str(response.get('recommended_target_method', '')).strip()
            if meth:
                diagnosis['recommended_target_method'] = meth
            for key in ['problem_summary','immediate_cause','fix_style']:
                value = str(response.get(key, '')).strip()
                if value:
                    diagnosis[key] = value
            for key in ['acceptance_criteria','oracle_tests','diagnostic_rationale']:
                raw = response.get(key, [])
                if isinstance(raw, list):
                    diagnosis[key] = dedupe_keep_order([str(x) for x in diagnosis.get(key, []) + raw if str(x).strip()])[:8]
            strategy = str(response.get('replace_strategy_preference', '')).strip().lower()
            if strategy in {'whole_method','search_replace'}:
                diagnosis['replace_strategy_preference'] = strategy
        diagnosis['mode'] = 'llm'
        return diagnosis
