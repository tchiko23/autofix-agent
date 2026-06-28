from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.service.correction_contract_service import build_correction_contract, dedupe_keep_order
from app.service.llm_service import OllamaPlannerClient


class NormalizerAgent:
    def __init__(self, llm: OllamaPlannerClient | None, prompts_dir: Path, enabled: bool = True):
        self.llm = llm
        self.prompts_dir = prompts_dir
        self.enabled = enabled and llm is not None

    def normalize(self, analysis_text: str, *, application_prefixes: list[str] | None = None) -> dict[str, Any]:
        contract = build_correction_contract(analysis_text, application_prefixes=application_prefixes)
        self._repair_contract(contract)
        if self.enabled and self._needs_llm_enrichment(contract):
            prompt = (self.prompts_dir / 'normalizer_prompt.md').read_text(encoding='utf-8')
            prompt = prompt.replace('{{ANALYSIS_TEXT}}', analysis_text)
            prompt = prompt.replace('{{BASE_CONTRACT}}', json.dumps(contract, ensure_ascii=False, indent=2))
            try:
                response = self.llm.generate_json(prompt=prompt, system='Réponds uniquement en JSON strict valide.')
                self._merge_enrichment(contract, response)
                self._repair_contract(contract)
            except Exception:
                contract.setdefault('normalizer_notes', []).append('llm_normalizer_failed')
        contract.setdefault('normalizer_notes', []).append('normalizer_completed')
        return contract

    def _needs_llm_enrichment(self, contract: dict[str, Any]) -> bool:
        weak = [
            not str(contract.get('problem_summary') or '').strip(),
            not str(contract.get('immediate_cause') or '').strip() or str(contract.get('immediate_cause')).strip() in {'(hypothèses classées)', '(hypotheses classees)'},
            not contract.get('acceptance_criteria'),
            not contract.get('oracle_tests'),
        ]
        return any(weak)

    def _merge_enrichment(self, contract: dict[str, Any], data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        for key in ['problem_summary', 'immediate_cause', 'must_touch_method', 'fix_style']:
            value = str(data.get(key, '')).strip()
            if value:
                contract[key] = value
        for key in ['acceptance_criteria', 'oracle_tests', 'must_touch', 'may_touch', 'must_not_touch', 'localization_search_terms']:
            raw = data.get(key, [])
            if isinstance(raw, list):
                merged = dedupe_keep_order([str(x) for x in (contract.get(key, []) or []) + raw if str(x).strip()])
                if merged:
                    contract[key] = merged

    def _repair_contract(self, contract: dict[str, Any]) -> None:
        crash_file = str(contract.get('crash_file') or '').strip()
        crash_method = str(contract.get('crash_method') or '').strip()
        must_touch = [str(x) for x in contract.get('must_touch', []) if str(x).strip()]
        if not must_touch and crash_file:
            must_touch = [crash_file]
        contract['must_touch'] = dedupe_keep_order(must_touch)
        if not str(contract.get('must_touch_method') or '').strip():
            contract['must_touch_method'] = crash_method or (contract.get('methods') or [''])[0]
        if not str(contract.get('problem_summary') or '').strip():
            if crash_file and crash_method:
                contract['problem_summary'] = f'Corriger le crash localisé dans {crash_file}::{crash_method}().'
            elif crash_file:
                contract['problem_summary'] = f'Corriger le crash localisé dans {crash_file}.'
        immediate = str(contract.get('immediate_cause') or '').strip()
        if not immediate or immediate.lower() in {'(hypothèses classées)', '(hypotheses classees)'}:
            terms = [str(x) for x in contract.get('immediate_cause_terms', []) if str(x).strip()]
            if terms:
                contract['immediate_cause'] = f"Cause immédiate probable: {' ; '.join(terms[:4])}"
            elif crash_file and crash_method:
                contract['immediate_cause'] = f'Cause immédiate probable dans {crash_file}::{crash_method}().'
        if not contract.get('acceptance_criteria'):
            criteria = []
            if crash_file:
                criteria.append(f"Ne plus lever l'exception sur {crash_file}.")
            if contract.get('immediate_cause_terms'):
                criteria.append(f"Traiter explicitement: {contract['immediate_cause_terms'][0]}")
            criteria.append('Préserver le contrat existant et limiter le correctif au périmètre local.')
            contract['acceptance_criteria'] = dedupe_keep_order(criteria)[:5]
        if not contract.get('oracle_tests'):
            contract['oracle_tests'] = [f'Verifier: {item}' for item in contract.get('acceptance_criteria', [])[:4]]
        contract['localization_search_terms'] = dedupe_keep_order([
            str(contract.get('must_touch_method') or ''),
            crash_method,
            crash_file,
            *[str(x) for x in contract.get('localization_search_terms', [])],
            *[str(x) for x in contract.get('immediate_cause_terms', [])],
        ])[:24]
