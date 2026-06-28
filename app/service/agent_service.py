from __future__ import annotations

import hashlib
import json
import re
import traceback
from pathlib import Path

import requests

from app.config.settings import Settings
from app.service.change_request_service import render_change_request
from app.service.forge.gitlab_service import GitLabService
from app.service.llm_service import OllamaPlannerClient
from app.service.diagnosis_agent import DiagnosisAgent
from app.service.localizer_agent import LocalizerAgent
from app.service.normalizer_agent import NormalizerAgent
from app.service.patch_planner_agent import PatchPlannerAgent
from app.service.planner_service import normalize_and_validate_plan, select_candidate_files
from app.service.validator_agent import ValidatorAgent
from app.service.solution_proposal_agent import SolutionProposalAgent
from app.service.proposal_render import render_proposal_markdown
from app.service.test_skeleton_agent import TestSkeletonAgent
from app.utils.repo_utils import RepoOps, autodetect_application_package_prefixes
from app.utils.run_context import RunContext
from app.utils.ticket_utils import extract_current_ticket_key
from app.utils.timing import Timer, timed


class FixAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        self.llm = OllamaPlannerClient(
            base_url=settings.ollama.base_url,
            model=settings.ollama.model,
            temperature=settings.ollama.temperature,
            num_predict=settings.ollama.num_predict,
            disable_thinking=settings.ollama.disable_thinking,
        )
        llm_or_none = self.llm if settings.agent.enable_multi_agent else None
        self.normalizer = NormalizerAgent(llm_or_none, self.prompts_dir, enabled=settings.agent.enable_llm_normalizer)
        self.diagnoser = DiagnosisAgent(llm_or_none, self.prompts_dir, enabled=settings.agent.enable_llm_diagnosis)
        self.localizer = LocalizerAgent(llm_or_none, self.prompts_dir, enabled=settings.agent.enable_llm_localizer)

        # v9.8: instantiation du RAGRetriever si la config l'active.
        # En cas d'echec d'import ou d'index absent, le PatchPlanner se
        # comporte exactement comme en v9.7 (pas de regression).
        rag_retriever = None
        if getattr(settings, "rag", None) and settings.rag.enabled:
            try:
                from app.rag.retriever import RAGRetriever
                rag_root = Path(settings.rag.index_path).resolve()
                rag_retriever = RAGRetriever(
                    rag_root=rag_root,
                    embedding_model=settings.rag.embedding_model,
                    enabled=settings.rag.enabled,
                )
            except Exception as exc:
                # Le bundle continue sans RAG. C'est un fail-safe critique
                # pour ne jamais bloquer le pipeline a cause du RAG.
                print(f"[RAG] disabled (init error): {exc}")
                rag_retriever = None

        self.patch_planner = PatchPlannerAgent(self.llm, self.prompts_dir, rag_retriever=rag_retriever)
        # v9.12: le logger est injecte par run() au demarrage de chaque ticket
        # pour que les logs structures (CONTEXT_BREAKDOWN, PROMPT_LLM, LLM_RESPONSE)
        # se retrouvent dans le bon run.log.
        self.validator = ValidatorAgent(settings.agent.min_patch_score, settings.agent.min_autocommit_score)
        self.solution_proposal_agent = SolutionProposalAgent(self.llm, self.prompts_dir)
        self.test_skeleton_agent = TestSkeletonAgent(self.llm, self.prompts_dir)

    def run(self, options) -> dict[str, object]:
        analysis_path = Path(options.analysis_file)
        analysis_text = analysis_path.read_text(encoding="utf-8", errors="ignore")
        # v9.11: extraction du ticket_key avec priorite Rovo JSON, puis titre,
        # puis fallback regex bundle. Resout le cas reel TICKET-NNNNN ou le bundle
        # extrayait NO_TICKET alors que le rapport commence par "RAPPORT D'ANALYSE — TICKET-NNNNN"
        # et contient `"ticket_key": "TICKET-NNNNN"` dans la section Rovo.
        ticket_key = extract_current_ticket_key(analysis_text)
        if not ticket_key:
            try:
                from app.service.rovo_extractor import extract_ticket_key_from_analysis
                ticket_key = extract_ticket_key_from_analysis(analysis_text)
            except Exception:
                pass
        base_branch = getattr(options, "base_branch", None) or self.settings.branches.base_branch
        target_branch = getattr(options, "target_branch", None) or self.settings.branches.target_branch
        idempotency_key = self._build_idempotency_key(ticket_key, base_branch, analysis_text)
        context = RunContext.create(self.settings.agent.artifacts_root(), ticket_key=ticket_key)
        logger = context.log
        timer = Timer(enabled=self.settings.agent.enable_timing_logs, sink=logger)

        repo_path = Path(options.repo)
        repo = RepoOps(repo_path, log=logger)

        # v9.3: détermination de la whitelist applicative pour filtrer les frames
        # de stack trace non-applicatives (Spring/CGLIB/JDK + libs tierces inconnues).
        # Priorité : (1) override explicite via agent.application_package_prefixes
        # dans application.properties ; (2) auto-détection depuis les pom.xml du
        # repo cible ; (3) fallback liste vide → blacklist FRAMEWORK_PACKAGE_PREFIXES
        # seule (comportement v9.2).
        configured_prefixes = list(getattr(self.settings.agent, "application_package_prefixes", []) or [])
        if configured_prefixes:
            application_prefixes = configured_prefixes
            logger(f"[AGENT] application_package_prefixes={application_prefixes} (from config)")
        else:
            application_prefixes = autodetect_application_package_prefixes(repo_path, log=logger)
            if application_prefixes:
                logger(f"[AGENT] application_package_prefixes={application_prefixes} (autodetected)")
            else:
                logger("[AGENT] application_package_prefixes=[] (fallback to blacklist-only filtering)")

        logger("[AGENT] start")
        logger(f"[AGENT] run_id={context.run_id}")
        logger(f"[AGENT] repo={repo_path}")
        logger(f"[AGENT] analysis_file={analysis_path}")
        logger(f"[AGENT] ticket_key={ticket_key or 'NON_ETABLI'}")
        logger(f"[AGENT] idempotency_key={idempotency_key}")
        context.write_text("analysis_input.txt", analysis_text)

        result: dict[str, object] = {
            "run_id": context.run_id,
            "ticket_key": ticket_key,
            "artifacts_dir": str(context.artifact_dir),
            "log_file": str(context.log_path),
            "dry_run": bool(getattr(options, "dry_run", False)),
            "model": self.settings.ollama.model,
            "base_branch": base_branch,
            "target_branch": target_branch,
            "idempotency_key": idempotency_key,
            "patch_result": "Edits not applied.",
            "pr_url": None,
            "change_request_kind": None,
            "delivery_status": "STARTED",
            "mr_blocked_reason": None,
        }

        try:
            self._validate_runtime_options(options)
            repo.verify_repo()
            if self.settings.agent.require_clean_repo:
                repo.ensure_clean(
                    auto_restore_workdir=self.settings.agent.auto_restore_work_artifacts,
                    workdir_name=self.settings.agent.workdir_name,
                )
            result["current_branch_before"] = repo.current_branch()

            with timed(timer, "normalizer_call", model=self.settings.ollama.model):
                contract = self.normalizer.normalize(analysis_text, application_prefixes=application_prefixes)

            # v9.10: detection de la section JSON Rovo dans le rapport d'analyse.
            # Si Rovo a produit le format dual (humain + machine), on fusionne ses
            # extractions avec le contrat du Normalizer en privilegiant Rovo sur
            # les champs critiques (crash, must_touch, fix_style, immediate_cause).
            # Le bundle continue avec un contrat enrichi, sans changement de pipeline.
            try:
                from app.service.rovo_extractor import (
                    extract_rovo_json,
                    rovo_to_contract_overrides,
                    merge_with_priority_to_rovo,
                )
                rovo_json = extract_rovo_json(analysis_text)
                self._rovo_json = None  # v9.16.7 : init defaut au cas ou pas de rovo
                if rovo_json:
                    # v9.16.7 : stocker rovo_json sur self pour usage tardif
                    # par le commentaire Jira apres creation MR
                    self._rovo_json = rovo_json
                    # v9.14.1 : tracer le format detecte du fichier d'analyse
                    rovo_format = rovo_json.get("_rovo_format", "unknown")
                    logger(f"[ROVO] analysis file format detected: {rovo_format}")
                    if rovo_format.startswith("pure_json"):
                        logger("[ROVO] format v1.4.1 (JSON pur) - parsing optimal")
                    elif rovo_format.startswith("delimited"):
                        logger("[ROVO] format v1.3 (balises <rovo_json>) - compatibilite descendante OK")
                    elif rovo_format == "partial_extraction":
                        logger("[ROVO] WARNING: extraction partielle - le JSON Rovo etait mal forme")
                    # v9.13.1 : anonymisation RGPD + qualite + professionnalisme
                    # avant fusion dans le contrat. Cela impacte la description MR
                    # et le contexte LLM en aval.
                    if getattr(self.settings.agent, "anonymize_personal_data", True):
                        from app.service.anonymizer import anonymize_rovo_extraction
                        rovo_json = anonymize_rovo_extraction(rovo_json)
                        logger("[ANONYMIZER] personal data anonymized in rovo extraction")
                    # v9.16.6 : extraction et validation du developer_hint optionnel.
                    # Si un expert/dev a glisse un hint dans le JSON, on tente
                    # de le valider factuellement contre le repo. Si OK, le hint
                    # sera utilise plus tard pour court-circuiter l'exploration
                    # agent (au cout de quelques heures economisees sur CPU).
                    try:
                        from app.service.developer_hint import (
                            extract_hint, validate_hint, build_hint_summary_for_log,
                        )
                        _hint = extract_hint(rovo_json)
                        if _hint is not None:
                            _hint_validation = validate_hint(_hint, repo_path)
                            logger(build_hint_summary_for_log(_hint, _hint_validation))
                            # On stocke sur self pour usage plus tard dans le pipeline
                            self._developer_hint = _hint
                            self._developer_hint_validation = _hint_validation
                        else:
                            self._developer_hint = None
                            self._developer_hint_validation = None
                    except Exception as _hint_exc:
                        logger(f"[HINT] extraction/validation failed: {_hint_exc}")
                        self._developer_hint = None
                        self._developer_hint_validation = None
                    rovo_overrides = rovo_to_contract_overrides(rovo_json)
                    if rovo_overrides:
                        logger(f"[ROVO] section JSON detected, schema_version={rovo_json.get('schema_version')}")
                        logger(f"[ROVO] overrides applied: {sorted(k for k in rovo_overrides if not k.startswith('rovo_'))}")
                        contract = merge_with_priority_to_rovo(contract, rovo_overrides)
                    # v9.16.6 : si le developer_hint est valide, on l'injecte
                    # dans le contrat. Le fichier resolu devient must_touch (force
                    # le localizer/planner a se concentrer dessus), la methode
                    # devient must_touch_method. Le pipeline aval beneficie
                    # automatiquement de ce ciblage humain sans modification
                    # supplementaire.
                    if (self._developer_hint is not None
                            and self._developer_hint_validation is not None
                            and self._developer_hint_validation.is_valid):
                        _resolved = self._developer_hint_validation.resolved_file
                        # Chemin relatif au repo pour cohérence avec Rovo
                        try:
                            _rel = _resolved.relative_to(repo_path).as_posix()
                        except ValueError:
                            _rel = _resolved.as_posix()
                        existing_must = list(contract.get("must_touch") or [])
                        if _rel not in existing_must:
                            existing_must.insert(0, _rel)
                            contract["must_touch"] = existing_must
                        contract["must_touch_method"] = self._developer_hint.target_method
                        if self._developer_hint.fix_intent:
                            # Enrichir l'immediate_cause pour donner du contexte au LLM
                            _intent = self._developer_hint.fix_intent
                            existing_cause = contract.get("immediate_cause") or ""
                            contract["immediate_cause"] = (
                                f"{existing_cause}\n[DEVELOPER HINT] {_intent}".strip()
                            )
                        logger(f"[HINT] injection contract: must_touch={_rel}, "
                               f"must_touch_method={self._developer_hint.target_method}")
                        context.write_json("rovo_extraction.json", rovo_json)
                        context.write_json("rovo_overrides.json", rovo_overrides)
                        result["rovo_detected"] = True
                        result["rovo_extraction"] = rovo_json  # v9.12: accessible au gating
                        result["rovo_schema_version"] = rovo_json.get("schema_version", "")
                else:
                    logger("[ROVO] no machine-readable section detected, using bundle extraction only")
                    result["rovo_detected"] = False
            except Exception as exc:
                # Fail-safe : si l'extraction Rovo plante, on continue comme v9.9
                logger(f"[ROVO] extraction failed (non-fatal): {exc}")
                result["rovo_detected"] = False

            context.write_json("correction_contract.json", contract)
            result["correction_contract"] = contract
            logger(f"[AGENT] contract.must_touch={contract.get('must_touch', [])}")
            logger(f"[AGENT] contract.must_touch_method={contract.get('must_touch_method', '')}")
            logger(f"[AGENT] contract.must_not_touch={contract.get('must_not_touch', [])}")
            logger(f"[AGENT] contract.fix_style={contract.get('fix_style')}")

            with timed(timer, "repo_scan_select_files"):
                selected_files, file_map, grep_hits, contract = select_candidate_files(
                    repo=repo,
                    analysis_text=analysis_text,
                    max_files=self.settings.agent.max_files,
                    max_chars=self.settings.agent.max_file_chars,
                    max_support_files=self.settings.agent.max_support_files,
                    contract=contract,
                )
            logger(f"[AGENT] selected_files {selected_files}")
            context.write_json("selected_files.json", selected_files)
            context.write_json("grep_hits.json", grep_hits)
            context.write_json("correction_contract.json", contract)

            with timed(timer, "diagnosis_call", model=self.settings.ollama.model):
                diagnosis = self.diagnoser.diagnose(analysis_text, contract, selected_files, file_map)
            context.write_json("diagnosis.json", diagnosis)
            contract = self._merge_diagnosis_into_contract(contract, diagnosis, selected_files)
            context.write_json("correction_contract.json", contract)

            with timed(timer, "localizer_call", model=self.settings.ollama.model):
                localization = self.localizer.localize(contract, selected_files, file_map, diagnosis=diagnosis)
            expected_method = str(contract.get("must_touch_method") or contract.get("crash_method") or "").strip()
            if expected_method and str(localization.get("target_method") or "").strip() != expected_method:
                localization["llm_selected_target_method"] = localization.get("target_method")
                localization["target_method"] = expected_method
                localization["method_lock_mismatch"] = True
                logger(f"[LOCALIZER] target_method_mismatch expected={expected_method} selected={localization.get('llm_selected_target_method')} -> forcing lock")
            snapshot = repo.read_method_snapshot(localization.get("target_file", ""), localization.get("target_method", ""), context_lines=28)
            # Rescue: si la méthode verrouillée n'existe pas dans le target_file choisi,
            # on tente de la retrouver parmi les autres candidats (support_paths + selected_files).
            # C'est le filet de sécurité qui rattrape un mauvais choix de target_file en amont.
            if (not snapshot.get("method_found")) and expected_method:
                logger(f"[LOCALIZER] method_not_found_in_target expected={expected_method} target={localization.get('target_file')} reason={snapshot.get('reason')}")
                candidates = []
                for p in localization.get("support_paths", []) or []:
                    if p and p not in candidates and p != localization.get("target_file"):
                        candidates.append(p)
                for p in selected_files:
                    if p and p not in candidates and p != localization.get("target_file"):
                        candidates.append(p)
                relocated = repo.find_method_in_files(candidates, expected_method)
                if relocated:
                    logger(f"[LOCALIZER] relocated target_file={relocated} (method {expected_method} found there)")
                    localization["previous_target_file"] = localization.get("target_file")
                    localization["target_file"] = relocated
                    localization["target_relocated"] = True
                    localization["support_paths"] = [p for p in selected_files if p != relocated][:2]
                    localization["planning_files"] = [relocated] + localization["support_paths"]
                    localization["target_basename"] = Path(relocated).name
                    snapshot = repo.read_method_snapshot(relocated, expected_method, context_lines=28)
                else:
                    logger(f"[LOCALIZER] relocation_failed method={expected_method} not found in any candidate")
                    localization["target_relocated"] = False
            localization["snapshot"] = snapshot
            if snapshot.get("method_source") and localization.get("target_file"):
                localization.setdefault("file_map", {})[localization["target_file"]] = snapshot["method_source_with_context"]
            context.write_json("localization.json", localization)
            logger(f"[LOCALIZER] target_file={localization.get('target_file')}")
            logger(f"[LOCALIZER] target_method={localization.get('target_method')}")
            logger(f"[LOCALIZER] method_found={snapshot.get('method_found')}")
            logger(f"[LOCALIZER] method_span={snapshot.get('start_line')}-{snapshot.get('end_line')}")
            logger(f"[LOCALIZER] source_fingerprint={snapshot.get('fingerprint')}")
            if expected_method and localization.get("target_file"):
                planning_files = [localization["target_file"]]
            else:
                planning_files = [path for path in localization.get("planning_files", []) if path in selected_files] or selected_files
            planning_map = {path: localization.get('file_map', {}).get(path, file_map.get(path, '')) for path in planning_files if path in file_map or path in localization.get('file_map', {})}

            # v9.13 : enrichissement contract avec le code source REEL
            # (Rovo n'a pas acces au your GitLab instance, donc le bundle lit lui-meme)
            try:
                from app.service.source_code_resolver import enrich_contract_with_source
                contract_before_resolve = dict(contract)
                contract = enrich_contract_with_source(contract, repo.repo_path, log=logger)
                if not contract.get("source_resolution_failed"):
                    logger(f"[RESOLVER] enriched: method_source={len(contract.get('target_method_source', ''))} chars, "
                           f"fields={len(contract.get('target_class_fields', []))}, "
                           f"methods={len(contract.get('target_class_methods_adjacent', []))}")
                else:
                    logger(f"[RESOLVER] failed: {contract.get('source_resolution_failed')}")
                context.write_json("correction_contract.json", contract)
            except Exception as exc:
                logger(f"[RESOLVER] non-fatal exception: {exc}")

            # v9.13 : pipeline en 3 etages, en cascade
            #   Etage 1 (NEW v9.13) : pattern-based fix generator
            #   Etage 2 : LLM PatchPlanner avec prompt elague
            #   Etage 3 : Fallback method-only + MR proposition
            plan = None
            pattern_attempted = False
            pattern_succeeded = False

            # === ETAGE 1 v9.13 : pattern-based fix ===
            exception_class = contract.get("exception_class") or ""
            target_method_source = contract.get("target_method_source") or ""
            if exception_class and target_method_source:
                try:
                    from app.service.pattern_based_fix_generator import detect_pattern, generate_fix
                    pattern_id = detect_pattern(exception_class)
                    if pattern_id:
                        pattern_attempted = True
                        logger(f"[PATTERN] detected pattern={pattern_id} for exception={exception_class}")
                        fix_result = generate_fix(
                            pattern_id=pattern_id,
                            method_source=target_method_source,
                            exception_message=contract.get("exception_message", ""),
                        )
                        if fix_result.success:
                            pattern_succeeded = True
                            logger(f"[PATTERN] fix generated: {fix_result.explanation}")
                            context.write_text("pattern_fix.java", fix_result.new_method_source)
                            # Injecter dans le contract comme si c'etait une suggestion Rovo "propre"
                            contract["rovo_code_suggestion"] = fix_result.new_method_source
                            contract["rovo_code_location_hint"] = fix_result.explanation
                            contract["pattern_based_fix_applied"] = pattern_id
                            contract["pattern_based_fix_explanation"] = fix_result.explanation
                            # Forcer confidence_level=high pour permettre au gating de passer
                            contract["rovo_confidence_level"] = "high"
                            # source remplacee
                            contract["source"] = "pattern+local-fix-agent"
                            context.write_json("correction_contract.json", contract)
                        else:
                            logger(f"[PATTERN] fix failed: {fix_result.reason_failed}")
                    else:
                        logger(f"[PATTERN] no pattern handler for exception={exception_class}")
                except Exception as exc:
                    logger(f"[PATTERN] non-fatal exception: {exc}")

            # === ETAGE 1bis v9.12 : Applicateur deterministe ===
            # Si le pattern a reussi OU si Rovo avait fourni une suggestion propre,
            # le gating va passer et appliquer directement.
            if getattr(self.settings.agent, "deterministic_applicator_enabled", True):
                try:
                    from app.service.deterministic_applicator import (
                        check_gating, build_deterministic_plan,
                    )
                    # Recuperer le rovo_extraction (sauve juste apres normalize)
                    rovo_json_for_gating = result.get("rovo_extraction") if isinstance(result.get("rovo_extraction"), dict) else None
                    if rovo_json_for_gating is None:
                        try:
                            rovo_path = context.artifact_dir / "rovo_extraction.json"
                            if rovo_path.is_file():
                                import json as _json
                                rovo_json_for_gating = _json.loads(rovo_path.read_text(encoding="utf-8"))
                        except Exception:
                            rovo_json_for_gating = None
                    # Si pattern a injecte une suggestion, on accepte un rovo vide
                    if pattern_succeeded and not rovo_json_for_gating:
                        rovo_json_for_gating = {"schema_version": "pattern-1.0", "ticket_key": result.get("ticket_key", "")}

                    target_method_src = contract.get("target_method_source") or (snapshot.get("method_source") if isinstance(snapshot, dict) else "")
                    gating = check_gating(
                        contract=contract,
                        rovo=rovo_json_for_gating,
                        file_content="",
                        target_method_source=target_method_src or "",
                    )
                    context.write_json("rovo_gating.json", gating.to_dict())
                    for line in gating.to_log_lines():
                        logger(line)

                    if gating.passed:
                        target_file_path = localization.get("target_file") or (
                            (contract.get("must_touch_paths") or [None])[0]
                        )
                        target_method_name = contract.get("must_touch_method") or contract.get("crash_method") or ""
                        if "." in (target_method_name or ""):
                            target_method_name = target_method_name.rsplit(".", 1)[-1]
                        logger(f"[APPLICATOR] mode=deterministic target_file={target_file_path} target_method={target_method_name}")
                        plan = build_deterministic_plan(
                            contract=contract,
                            rovo=rovo_json_for_gating or {},
                            target_file_path=target_file_path or "",
                            target_method_name=target_method_name,
                        )
                        plan["deterministic_applied"] = True
                        plan["pattern_based"] = pattern_succeeded
                        result["deterministic_applied"] = True
                        result["pattern_based_fix"] = pattern_succeeded
                        context.write_json("applicator_plan.json", plan)
                        logger(f"[APPLICATOR] plan construit, edits={len(plan.get('edits', []))} pattern_based={pattern_succeeded}")
                except Exception as exc:
                    logger(f"[APPLICATOR] gating/build failed (non-fatal, fallback LLM): {exc}")
                    plan = None

            # 2b. Si pas de plan deterministe, on passe au LLM (chemin v9.11)
            if plan is None:
                # v9.12: injecter le logger pour les logs structures
                self.patch_planner.log = logger
                plan_generation_error = ""  # v9.15.1 : init pour usage hors bloc except
                try:
                    with timed(timer, "planning_call", model=self.settings.ollama.model):
                        plan = self.patch_planner.plan(
                            analysis_text,
                            planning_files,
                            planning_map,
                            contract,
                            localization,
                            require_file_overlap=self.settings.agent.require_plan_file_overlap,
                        )
                    # v9.12: persiste le breakdown du prompt pour analyse a posteriori
                    if getattr(self.patch_planner, "last_breakdown", None):
                        context.write_json("prompt_breakdown.json", self.patch_planner.last_breakdown)
                    if getattr(self.llm, 'last_raw_response', ''):
                        raw_path = context.write_text('plan_raw.txt', self.llm.last_raw_response)
                        logger(f"[LLM] raw_plan_saved_to={raw_path} (size={len(self.llm.last_raw_response)} chars)")
                except Exception as exc:
                    # v9.15.1 : capturer le message AVANT la fin du bloc except.
                    # En Python, la variable liee par 'as exc' est supprimee a la
                    # sortie du bloc except. La reutiliser plus bas (ligne ~431)
                    # levait 'cannot access local variable exc'.
                    plan_generation_error = str(exc)
                    if getattr(self.llm, 'last_raw_response', ''):
                        raw_path = context.write_text('plan_raw.txt', self.llm.last_raw_response)
                        logger(f"[LLM] raw_plan_saved_to={raw_path}")
                    if getattr(self.llm, 'last_repaired_response', ''):
                        repaired_path = context.write_text('plan_repaired_raw.txt', self.llm.last_repaired_response)
                        logger(f"[LLM] repaired_plan_saved_to={repaired_path}")
                    plan = self._plan_with_method_fallback(
                        analysis_text=analysis_text,
                        contract=contract,
                        localization=localization,
                        planning_files=planning_files,
                        planning_map=planning_map,
                        snapshot=snapshot,
                        logger=logger,
                    )
                if not plan:
                    result["delivery_status"] = "MR_BLOCKED"
                    result["mr_blocked_reason"] = "plan_generation_failed"
                    result["patch_result"] = f"Plan generation failed: {plan_generation_error}"
                    result["selected_files"] = selected_files
                    result["planning_files"] = planning_files
                    result["diagnosis"] = diagnosis
                    result["localization"] = localization
                    self._persist_outcome(context, result)
                    return result
                logger('[PLAN] fallback_method_only_plan=true')
            if (not plan.get("edits") or plan.get("unable_to_fix")) and snapshot.get("method_source"):
                fallback_plan = self._plan_with_method_fallback(
                    analysis_text=analysis_text,
                    contract=contract,
                    localization=localization,
                    planning_files=planning_files,
                    planning_map=planning_map,
                    snapshot=snapshot,
                    logger=logger,
                )
                # v9.7: dump systematique de la sortie LLM du fallback method-only.
                # Critique pour diagnostiquer pourquoi le fallback ne produit pas
                # toujours d'edit valide (cas observes sur TICKET-NNNNN et TICKET-NNNNN
                # ou le LLM a genere 3791 et 2434 chars mais 0 edit accepte).
                if getattr(self.llm, 'last_raw_response', ''):
                    method_raw_path = context.write_text('method_only_raw.txt', self.llm.last_raw_response)
                    logger(f"[LLM] method_only_raw_saved_to={method_raw_path} (size={len(self.llm.last_raw_response)} chars)")
                if fallback_plan and fallback_plan.get('edits'):
                    logger('[PLAN] replacing primary plan with method-only fallback')
                    plan = fallback_plan
                elif fallback_plan:
                    logger(f"[PLAN] fallback_method_only produced 0 valid edits (risk_notes={fallback_plan.get('risk_notes', [])[:3]})")
                else:
                    logger("[PLAN] fallback_method_only returned None (likely empty method extraction)")

            # v9.14 OPTION ALPHA : AGENT AUTONOME D'EXPLORATION
            # Remplace le filet LLM libre v9.13.2. Quand toute la cascade habituelle
            # echoue (pattern + LLM standard + fallback method-only), on lance un agent
            # autonome qui fouille le repo local avec 11 outils en lecture seule.
            # L'agent peut tracer la cascade depuis l'endpoint REST (RG Confluence)
            # ou depuis les investigation_candidates fournis par Rovo v1.4.
            # Philosophie : mieux vaut un correctif explore et justifie qu'une MR vide.
            agent_attempted = False
            agent_result_summary: dict[str, Any] = {}
            if (not plan.get("edits") or plan.get("unable_to_fix")) and getattr(self.settings.agent, "exploration_agent_enabled", True):
                try:
                    from app.service.exploration_agent import run_exploration_agent
                    agent_attempted = True
                    max_turns = getattr(self.settings.agent, "exploration_max_turns", 20)
                    min_score = getattr(self.settings.agent, "exploration_min_validation_score", 40)
                    logger(f"[AGENT] cascade exhausted, attempting autonomous exploration (max_turns={max_turns}, min_score={min_score})")

                    # Extraire les inputs v9.14 du contrat
                    inv_candidates = contract.get("investigation_candidates") or []
                    endpoint_hint = contract.get("endpoint_concerned") or ""

                    # v9.16 : callback de sauvegarde incrementale. Ecrit
                    # exploration_trace.json apres chaque tour de l'agent.
                    def _save_partial_trace(trace_dict):
                        context.write_json("exploration_trace.json", trace_dict)

                    agent_res = run_exploration_agent(
                        llm=self.llm,
                        repo_root=Path(repo.repo_path),
                        ticket_key=str(result.get("ticket_key") or ""),
                        immediate_cause=str(contract.get("immediate_cause", "")),
                        root_cause_hypothesis=str(contract.get("root_cause_hypothesis", "")),
                        investigation_candidates=inv_candidates,
                        endpoint_hint=endpoint_hint,
                        crash_method=str(contract.get("crash_method", "") or contract.get("must_touch_method", "")),
                        must_touch=list(contract.get("must_touch_paths") or contract.get("must_touch") or []),
                        max_turns=max_turns,
                        min_validation_score=min_score,
                        logger_fn=logger,
                        trace_save_fn=_save_partial_trace,
                    )

                    # Sauvegarde des artefacts d'exploration
                    if agent_res.exploration_trace:
                        context.write_json("exploration_trace.json", {
                            "summary": agent_res.summary,
                            "operations": agent_res.exploration_trace,
                        })
                    if agent_res.plan:
                        context.write_json("agent_plan.json", agent_res.plan)
                    agent_result_summary = {
                        "success": agent_res.success,
                        "validation_score": agent_res.validation_score,
                        "rejection_reason": agent_res.rejection_reason,
                        "turns_used": agent_res.summary.get("turns_used", 0) if agent_res.summary else 0,
                        "tokens_used": agent_res.summary.get("tokens_used", 0) if agent_res.summary else 0,
                    }
                    context.write_json("agent_result.json", agent_result_summary)

                    if agent_res.success and agent_res.plan:
                        logger(f"[AGENT] SUCCESS: validation_score={agent_res.validation_score}, replacing plan")
                        plan = agent_res.plan
                        # Marquage pour MR builder (preffixe [AGENT], label needs-review, mode draft)
                        result["agent_explored"] = True
                        result["agent_score"] = agent_res.validation_score
                    else:
                        logger(f"[AGENT] REJECTED: {agent_res.rejection_reason} (score={agent_res.validation_score})")
                except Exception as exc:
                    logger(f"[AGENT] non-fatal exception: {exc}")
                    import traceback
                    logger(traceback.format_exc())

            # v9.16.9 : ETAGE 3f — FILET LLM LIBRE (re-branche)
            # En v9.13.2 ce filet remplacait toute la fin de cascade ; en v9.14 il a
            # ete remplace par l'ExplorationAgent (3e). On le re-branche ici comme
            # ULTIME recours : il ne s'execute que si 3a-3e ont TOUS echoue.
            # Le LLM recoit la definition du bug (contrat : exception, classes,
            # methodes) + le source des fichiers candidats, avec carte blanche.
            # Si score >= seuil (AGENT_LLM_LIBRE_MIN_VALIDATION_SCORE, defaut 40),
            # le plan est adopte : le flag llm_libre_mode declenche en aval le
            # prefixe [LLM-LIBRE], le mode draft et le label needs-review.
            # Desactivable via AGENT_LLM_LIBRE_ENABLED=false.
            if (not plan.get("edits") or plan.get("unable_to_fix")) and getattr(
                self.settings.agent, "llm_libre_enabled", True
            ):
                try:
                    from app.service.llm_libre_fallback_agent import try_llm_libre

                    libre_min = getattr(self.settings.agent, "llm_libre_min_validation_score", 40)
                    logger(f"[LLM_LIBRE] stage 3f: cascade (incl. exploration) exhausted, attempting free-mode LLM (min_score={libre_min})")

                    # file_map : {chemin relatif: source} des fichiers candidats.
                    # Priorite aux must_touch du contrat, complete par selected_files.
                    repo_root = Path(repo.repo_path)
                    candidate_paths: list[str] = []
                    for src in (contract.get("must_touch") or []), (selected_files or []):
                        for item in src:
                            p = str(item.get("path") if isinstance(item, dict) else item).strip()
                            if p and p not in candidate_paths:
                                candidate_paths.append(p)
                    file_map: dict[str, str] = {}
                    for rel in candidate_paths[:6]:  # cap : 6 fichiers max
                        try:
                            fp = (repo_root / rel).resolve()
                            # Securite : ne pas sortir du repo
                            if not str(fp).startswith(str(repo_root.resolve())):
                                continue
                            if fp.is_file():
                                file_map[rel] = fp.read_text(encoding="utf-8", errors="replace")[:8000]
                        except OSError:
                            continue

                    if not file_map:
                        logger("[LLM_LIBRE] SKIPPED: no candidate source file readable (empty file_map)")
                    else:
                        libre_res = try_llm_libre(
                            llm=self.llm,
                            analysis_text=analysis_text,
                            contract=contract,
                            file_map=file_map,
                            min_score=libre_min,
                            logger=logger,
                        )
                        context.write_json("llm_libre_result.json", {
                            "success": libre_res.success,
                            "validation_score": libre_res.validation_score,
                            "rejection_reason": libre_res.rejection_reason,
                            "files_in_prompt": sorted(file_map.keys()),
                        })
                        if libre_res.success and libre_res.plan:
                            logger(f"[LLM_LIBRE] SUCCESS: validation_score={libre_res.validation_score}, adopting free-mode plan (draft MR, needs-review)")
                            plan = libre_res.plan  # llm_libre_mode=True deja pose par try_llm_libre
                            result["llm_libre_used"] = True
                            result["llm_libre_score"] = libre_res.validation_score
                        else:
                            logger(f"[LLM_LIBRE] REJECTED: {libre_res.rejection_reason} (score={libre_res.validation_score})")
                except Exception as exc:
                    logger(f"[LLM_LIBRE] non-fatal exception: {exc}")
                    import traceback
                    logger(traceback.format_exc())

            context.write_json("plan.json", plan)

            validator_report = self.validator.validate(plan, contract, localization)
            context.write_json("validator_report.json", validator_report)

            result["selected_files"] = selected_files
            result["planning_files"] = planning_files
            result["diagnosis"] = diagnosis
            result["localization"] = localization
            result["plan"] = plan
            result["validator"] = validator_report

            validation_score = int(plan.get("validation_score", 0) or 0)
            model_confidence = int(plan.get("confidence_score", 0) or 0)
            result["validation_score"] = validation_score
            result["model_confidence"] = model_confidence
            logger(f"[AGENT] validation_score={validation_score}")
            logger(f"[AGENT] model_confidence={model_confidence}")
            logger(f"[AGENT] validator.can_apply_patch={validator_report.get('can_apply_patch')}")
            logger(f"[AGENT] validator.can_autocommit={validator_report.get('can_autocommit')}")

            source_branch = getattr(options, "source_branch", None) or self._derive_source_branch(plan, analysis_text, idempotency_key, context.run_id)
            result["source_branch"] = source_branch

            if getattr(options, "dry_run", False):
                logger("[AGENT] dry-run done")
                result["delivery_status"] = "DRY_RUN_ONLY"
                result["patch_result"] = "Dry-run completed. No branch or patch was applied."
                self._persist_outcome(context, result)
                return result

            block_reason = self._compute_patch_block_reason(plan, validator_report, options)
            if block_reason:
                result["delivery_status"] = "MR_BLOCKED"
                result["mr_blocked_reason"] = block_reason
                result["patch_result"] = self._patch_result_for_block_reason(block_reason, plan)
                self._maybe_ship_diagnostic_mr(context, result, repo, plan, contract, localization, analysis_text)
                self._persist_outcome(context, result)
                return result

            edits = plan.get("edits") or []
            logger("[AGENT] create/apply branch and edits")
            with timed(timer, "git_branch_and_apply", source_branch=source_branch, base_branch=base_branch):
                repo.create_branch(source_branch, base_branch)
                ok, patch_result, changed_files = repo.apply_edits(edits)
                diff_files = repo.diff_name_only()
            result["patch_result"] = patch_result
            result["changed_files"] = changed_files
            result["diff_files_after_apply"] = diff_files
            result["current_branch_after"] = repo.current_branch()
            logger(f"[PATCH] apply_result={'success' if ok else 'failed'}")
            logger(f"[PATCH] patch_result={patch_result}")
            logger(f"[PATCH] changed_files={changed_files}")
            logger(f"[GIT] diff_files_after_apply={diff_files}")
            if not ok:
                result["delivery_status"] = "MR_BLOCKED"
                result["mr_blocked_reason"] = "edit_application_failed"
                self._maybe_ship_diagnostic_mr(context, result, repo, plan, contract, localization, analysis_text)
                self._persist_outcome(context, result)
                raise RuntimeError(f"Edit application failed: {patch_result}")
            if not diff_files:
                result["delivery_status"] = "MR_BLOCKED"
                result["mr_blocked_reason"] = "no_diff_after_patch"
                result["patch_result"] = "Patch blocked: no diff generated after patch application."
                self._maybe_ship_diagnostic_mr(context, result, repo, plan, contract, localization, analysis_text)
                self._persist_outcome(context, result)
                return result

            result["delivery_status"] = "PATCH_APPLIED"

            if getattr(options, "commit", False):
                if not validator_report.get("can_autocommit"):
                    result["delivery_status"] = "MR_BLOCKED"
                    result["mr_blocked_reason"] = "autocommit_threshold_not_met"
                    result["patch_result"] = "Patch applied on branch but auto-commit blocked by thresholds."
                    self._maybe_ship_diagnostic_mr(context, result, repo, plan, contract, localization, analysis_text)
                    self._persist_outcome(context, result)
                    return result
                logger("[AGENT] commit changes")
                with timed(timer, "git_commit"):
                    commit_created, commit_sha = repo.commit_all(plan.get("commit_message", "fix: apply automated code correction"))
                result["commit_created"] = commit_created
                result["commit_sha"] = commit_sha
                logger(f"[GIT] commit_created={commit_created}")
                logger(f"[GIT] commit_sha={commit_sha or ''}")
                if not commit_created:
                    result["delivery_status"] = "MR_BLOCKED"
                    result["mr_blocked_reason"] = "no_commit_after_patch"
                    result["patch_result"] = "Patch blocked: no commit created after patch application."
                    self._maybe_ship_diagnostic_mr(context, result, repo, plan, contract, localization, analysis_text)
                    self._persist_outcome(context, result)
                    return result
                result["delivery_status"] = "COMMITTED"

            # v9.13.1 : generation de tests JUnit automatises apres le fix
            # mais avant le push, pour qu'ils soient dans la meme MR.
            if (
                getattr(options, "apply_patch", False)
                and result.get("delivery_status") == "COMMITTED"
                and getattr(self.settings.agent, "generate_junit_tests", True)
            ):
                try:
                    self._maybe_generate_junit_tests(
                        context=context, repo=repo, contract=contract,
                        plan=plan, result=result, logger=logger,
                    )
                except Exception as exc:
                    logger(f"[TEST_GEN] non-fatal: {exc}")

            if getattr(options, "push", False):
                # v9.11 TRIPLE CEINTURE: avant chaque push de patch, retirer
                # de l'index Git tout fichier .fixagent/* qui aurait pu y etre
                # ajoute par accident. C'est la 3e protection apres :
                #   - garde dans _write_diagnostic_file (v9.9)
                #   - .gitignore auto sur repo cible (v9.9)
                removed, info = repo.purge_fixagent_from_index()
                if removed:
                    logger(f"[FIXAGENT] purged from index before push: {info}")
                    # Si on a retire des fichiers, il faut un commit pour materialiser
                    try:
                        repo.commit_all("chore: remove .fixagent technical artifacts from patch MR")
                    except Exception:
                        pass
                logger("[AGENT] push branch")
                with timed(timer, "git_push"):
                    repo.push(source_branch)
                result["delivery_status"] = "PUSHED"

            if getattr(options, "create_mr", False):
                if not changed_files:
                    result["delivery_status"] = "MR_BLOCKED"
                    result["mr_blocked_reason"] = "no_changed_files_for_code_mr"
                    self._maybe_ship_diagnostic_mr(context, result, repo, plan, contract, localization, analysis_text)
                    self._persist_outcome(context, result)
                    return result
                logger("[AGENT] create change request")
                # v9.1: Génération du squelette de tests JUnit (Given/When/Then + code prêt à coller)
                # juste avant la rédaction de la description de MR. Garde-fous contre l'hallucination
                # via TestSkeletonAgent (cf. _format_automated_tests dans change_request_service).
                automated_tests = None
                try:
                    snapshot = localization.get("snapshot") if isinstance(localization, dict) else None
                    method_source = ""
                    if isinstance(snapshot, dict):
                        method_source = str(snapshot.get("method_source") or "")
                    target_file_for_tests = str(localization.get("target_file") or "") if isinstance(localization, dict) else ""
                    target_method_for_tests = str(localization.get("target_method") or "") if isinstance(localization, dict) else ""
                    if target_file_for_tests and target_method_for_tests:
                        with timed(timer, "test_skeleton_generation"):
                            automated_tests = self.test_skeleton_agent.generate(
                                target_file=target_file_for_tests,
                                target_method=target_method_for_tests,
                                method_source=method_source,
                                fix_summary=str(plan.get("fix_summary") or ""),
                                tests_suggested=[str(x) for x in plan.get("tests_suggested", [])],
                                logger=logger,
                            )
                        # Persiste le squelette comme artefact pour audit
                        context.write_json("test_skeleton.json", automated_tests)
                        result["test_skeleton"] = automated_tests
                except Exception as exc:
                    logger(f"[AGENT] test_skeleton_generation failed (non-blocking): {exc}")
                    automated_tests = None
                # v9.11: utilise le nouveau builder de description MR structuree
                # qui exploite le JSON Rovo (tableau RG, AC, tests, incidents).
                # Fallback sur l'ancien render_change_request si erreur.
                try:
                    from app.service.mr_description_builder import build_mr_description
                    body = build_mr_description(
                        contract=contract,
                        localization=localization,
                        plan=plan,
                        result=result,
                        ticket_key=ticket_key or "",
                        is_patch_mr=True,
                        bundle_version="v9.11",
                        model_name=self.settings.ollama.model,
                        run_id=context.run_id,
                    )
                    logger("[MR] description built with v9.11 structured builder (patch MR)")
                except Exception as exc:
                    logger(f"[MR] v9.11 builder failed, fallback to legacy: {exc}")
                    body = self._build_change_request_body(plan, changed_files, ticket_key=ticket_key, automated_tests=automated_tests)
                with timed(timer, "change_request_create"):
                    # v9.14 : label needs-review pour MR issues de l'agent OU du LLM libre legacy
                    extra_labels = None
                    if plan.get("agent_explored") or plan.get("llm_libre_mode"):
                        extra_labels = ["needs-review"]
                    cr = self._create_change_request(source_branch, target_branch, self._build_change_request_title(plan, ticket_key=ticket_key), body, repo, extra_labels=extra_labels)
                result["pr_url"] = cr.get("url")
                result["change_request_kind"] = cr.get("kind")
                if cr.get("reused_existing"):
                    result["delivery_status"] = "MR_REUSED"
                else:
                    result["delivery_status"] = "MR_CREATED"
                # v9.16.7 : commentaire Jira (Q3 : seulement si MR creee)
                self._post_jira_comment_after_mr(
                    result, ticket_key, context, plan, cr,
                    mr_kind="correctif", logger_fn=logger,
                )

            self._persist_outcome(context, result)
            return result
        except Exception as exc:
            logger(f"[ERROR] {exc}")
            logger(traceback.format_exc())
            result["delivery_status"] = result.get("delivery_status") or "ERROR"
            result["error"] = str(exc)
            result["traceback"] = traceback.format_exc()
            self._persist_outcome(context, result)
            raise


    def _plan_with_method_fallback(
        self,
        *,
        analysis_text: str,
        contract: dict[str, object],
        localization: dict[str, object],
        planning_files: list[str],
        planning_map: dict[str, str],
        snapshot: dict[str, object],
        logger,
    ) -> dict[str, object] | None:
        target_file = str(localization.get("target_file") or "").strip()
        target_method = str(localization.get("target_method") or contract.get("must_touch_method") or "").strip()
        method_source = str(snapshot.get("method_source") or "")
        fingerprint = str(snapshot.get("fingerprint") or "")
        if not target_file or not target_method or not method_source:
            return None

        raw_text = self._rewrite_method_via_llm(
            analysis_text=analysis_text,
            contract=contract,
            target_file=target_file,
            target_method=target_method,
            method_source_with_context=str(snapshot.get("method_source_with_context") or method_source),
            logger=logger,
        )
        rewritten = self._extract_method_from_text(raw_text, target_method)
        if not rewritten:
            return None

        plan = {
            "problem_summary": str(contract.get("problem_summary") or ""),
            "root_cause": str(contract.get("immediate_cause") or ""),
            "fix_summary": f"Réécriture locale de {target_method} pour traiter la cause immédiate.",
            "fix_style": str(contract.get("fix_style") or "minimal_local_null_guard"),
            "files_to_change": [target_file],
            "edits": [
                {
                    "file": target_file,
                    "search": method_source,
                    "replace": rewritten,
                    "replace_strategy": "whole_method",
                    "match_hint_method": target_method,
                    "original_method_fingerprint": f"sha256:{fingerprint}" if fingerprint else "",
                }
            ],
            "edit_justifications": [
                {
                    "file": target_file,
                    "why_this_file": f"Méthode verrouillée par le contrat: {target_method}",
                    "ticket_evidence": [str(contract.get("immediate_cause") or ""), str(contract.get("crash_file") or "")],
                    "symptom_resolved": str(contract.get("immediate_cause") or ""),
                    "preserves_contract": "Correctif local sans élargissement de périmètre.",
                    "confidence": 70,
                }
            ],
            "tests_suggested": list(contract.get("oracle_tests", [])[:4]),
            "oracle_checks": list(contract.get("acceptance_criteria", [])[:4]),
            "risk_notes": ["Plan généré via fallback method-only."] ,
            "confidence_score": 70,
            "commit_message": f"fix: sécuriser {target_method}",
            "change_request_title": f"Corriger {target_method} dans {Path(target_file).name}",
            "short_description": f"Correction locale de {target_method}.",
            "already_fixed": False,
            "unable_to_fix": False,
            "unable_to_fix_reason": "",
        }
        normalized = normalize_and_validate_plan(
            plan,
            planning_files,
            planning_map,
            contract=contract,
            require_file_overlap=self.settings.agent.require_plan_file_overlap,
        )
        normalized["localization"] = {
            "target_file": target_file,
            "target_method": target_method,
            "support_paths": localization.get("support_paths", []),
            "anchors": localization.get("anchors", []),
        }
        return normalized

    def _rewrite_method_via_llm(
        self,
        *,
        analysis_text: str,
        contract: dict[str, object],
        target_file: str,
        target_method: str,
        method_source_with_context: str,
        logger,
    ) -> str:
        prompt = f"""Tu corriges une seule méthode Java.
Réécris UNIQUEMENT la méthode cible complète, avec sa signature et ses accolades.
Ne renvoie aucun JSON, aucun markdown, aucune explication.
Ne change ni le nom de la méthode, ni son type de retour, ni les autres méthodes.
Corrige d'abord la cause immédiate décrite ci-dessous avec un correctif local et défensif.

TICKET / ANALYSE:
{analysis_text}

CONTRAT:
- problem_summary: {contract.get('problem_summary')}
- immediate_cause: {contract.get('immediate_cause')}
- fix_style: {contract.get('fix_style')}
- must_touch: {contract.get('must_touch')}
- must_touch_method: {contract.get('must_touch_method')}
- acceptance_criteria: {contract.get('acceptance_criteria')}
- oracle_tests: {contract.get('oracle_tests')}

FICHIER CIBLE: {target_file}
METHODE CIBLE: {target_method}

CODE ACTUEL AUTOUR DE LA METHODE:
{method_source_with_context}
"""
        raw = self.llm.generate_text(
            prompt=prompt,
            system="Tu produis uniquement du code Java valide pour une seule méthode.",
        )
        if raw:
            logger(f"[LLM] method_only_raw_len={len(raw)}")
            # v9.7: dump systematique de la sortie LLM brute du fallback method-only
            # pour diagnostiquer pourquoi _extract_method_from_text echoue parfois
            # a recuperer la methode du texte produit par le LLM.
            try:
                from app.utils.run_context import _RunContextRegistry  # type: ignore
            except Exception:
                pass
            # On ecrit dans le repertoire d'artefacts du run courant. Comme ce
            # service ne tient pas le contexte, on log juste un marqueur : le
            # caller (FixAgent) peut decider de dumper s'il a acces a
            # self.llm.last_raw_response apres l'appel.
        return raw

    def _extract_method_from_text(self, raw_text: str, method_name: str) -> str:
        """v9.7: extraction plus robuste de la methode produite par le LLM.

        Le LLM (qwen3-coder:30b notamment) intercale parfois des annotations,
        modificateurs (final, static, synchronized), generics (<T>), du
        commentaire javadoc, etc. La regex precedente avait un bug (']' en trop
        dans la classe negative `[^\\n{;]]*`) qui faisait echouer le matching
        principal et tombait toujours sur le fallback simple, qui lui ne
        capturait pas la signature complete.

        On essaie maintenant 4 strategies en cascade :
        1. Match exact signature avec annotations + modifiers + generics
        2. Match basique nom_methode(...){
        3. Recherche directe du nom de methode suivi de '('
        4. Si toutes echouent : on retourne le texte tel quel s'il commence
           directement par du code Java vraisemblable (LLM tres minimal)
        """
        text = (raw_text or "").strip()
        if not text:
            return ""
        # Strip markdown code fences
        text = re.sub(r"^```(?:java)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\n```\s*$", "", text).strip()
        text = re.sub(r"```\s*$", "", text).strip()

        # Strategy 1: signature complete (annotations, modifiers, generics, throws)
        pattern_full = (
            r"(?ms)^\s*"
            r"(?:@[\w.]+(?:\([^)]*\))?\s*\n\s*)*"  # annotations
            r"(?:public|protected|private|\s)*"     # access modifiers
            r"(?:static|final|synchronized|abstract|\s)*"  # other modifiers
            r"(?:<[^>]+>\s*)?"                       # generics
            r"[\w.<>\[\],\s]+\s+"                    # return type
            + re.escape(method_name) +
            r"\s*\([^)]*\)"                          # parameter list
            r"\s*(?:throws\s+[\w.,\s]+)?"            # throws clause
            r"\s*\{"
        )
        sig = re.search(pattern_full, text)

        # Strategy 2: simple nom_methode(...){
        if not sig:
            sig = re.search(
                r"\b" + re.escape(method_name) + r"\s*\([^)]*\)\s*(?:throws[^{]+)?\{",
                text,
                flags=re.MULTILINE,
            )

        # Strategy 3: encore plus permissif - juste nom_methode + (
        if not sig:
            sig = re.search(r"\b" + re.escape(method_name) + r"\s*\(", text)

        if not sig:
            return ""

        # Find the first { after the signature start
        brace_start = text.find("{", sig.start())
        if brace_start < 0:
            return ""

        # Walk braces to find matching close
        depth = 0
        in_string = False
        in_char = False
        escape = False
        for idx in range(brace_start, len(text)):
            ch = text[idx]
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
                    # Remonter au debut de ligne pour inclure les annotations
                    # et la signature complete (pas seulement la ligne du nom).
                    line_start = sig.start()
                    # Reculer jusqu'a la premiere ligne qui n'est pas une annotation
                    # ni un modificateur, en remontant ligne par ligne.
                    text_before = text[:line_start]
                    lines = text_before.split("\n")
                    cut = len(lines)
                    for li in range(len(lines) - 1, -1, -1):
                        stripped = lines[li].strip()
                        if not stripped:
                            break
                        if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                            cut = li
                            continue
                        if stripped.startswith("@"):
                            cut = li
                            continue
                        # ligne avec contenu non-annotation: on s'arrete
                        break
                    final_start = sum(len(l) + 1 for l in lines[:cut])
                    return text[final_start:idx + 1].strip() + "\n"
        return ""

    def _merge_diagnosis_into_contract(self, contract: dict[str, object], diagnosis: dict[str, object], selected_files: list[str]) -> dict[str, object]:
        merged = dict(contract)
        chosen = str(diagnosis.get("recommended_target_file") or "").replace("\\", "/").strip()
        if chosen and chosen in selected_files and not merged.get("must_touch"):
            basename = Path(chosen).name
            merged["must_touch"] = [basename]
            merged["crash_file"] = basename
        method = str(diagnosis.get("recommended_target_method") or "").strip()
        if method and not str(merged.get("must_touch_method") or "").strip():
            merged["must_touch_method"] = method
            merged["crash_method"] = method
        for key in ["problem_summary", "immediate_cause"]:
            current = str(merged.get(key) or "").strip()
            value = str(diagnosis.get(key) or "").strip()
            if value and not current:
                merged[key] = value
        fix_style = str(diagnosis.get("fix_style") or "").strip()
        if fix_style and not str(merged.get("fix_style") or "").strip():
            merged["fix_style"] = fix_style
        for key in ["acceptance_criteria", "oracle_tests"]:
            current = merged.get(key, []) if isinstance(merged.get(key, []), list) else []
            raw = diagnosis.get(key, [])
            if isinstance(raw, list) and raw:
                merged[key] = list(dict.fromkeys([*map(str, current), *map(str, raw)]))[:8] if current else raw
        rationale = diagnosis.get("diagnostic_rationale", [])
        if isinstance(rationale, list) and rationale:
            merged["immediate_cause_terms"] = list(dict.fromkeys([*map(str, merged.get("immediate_cause_terms", [])), *map(str, rationale)]))[:16]
        if str(diagnosis.get("replace_strategy_preference") or "").strip() and not str(merged.get("replace_strategy_preference") or "").strip():
            merged["replace_strategy_preference"] = str(diagnosis.get("replace_strategy_preference")).strip()
        return merged

    def _persist_outcome(self, context: RunContext, result: dict[str, object]) -> None:
        context.write_json("result.json", result)
        summary = self._summary_payload(result)
        context.write_json("summary.json", summary)
        if self.settings.agent.enable_history:
            context.append_history(summary)

    def _validate_runtime_options(self, options) -> None:
        if getattr(options, "create_mr", False) and not getattr(options, "push", False):
            raise RuntimeError("--create-mr requires --push.")
        if getattr(options, "push", False) and not getattr(options, "commit", False):
            raise RuntimeError("--push requires --commit.")
        if any(getattr(options, name, False) for name in ("commit", "push", "create_mr")) and not getattr(options, "apply_patch", False):
            raise RuntimeError("--commit/--push/--create-mr require --apply-patch.")

    def _build_idempotency_key(self, ticket_key: str | None, base_branch: str, analysis_text: str) -> str:
        digest = hashlib.sha1(f"{ticket_key or 'NO_TICKET'}\n{base_branch}\n{self.settings.ollama.model}\n{analysis_text}".encode("utf-8")).hexdigest()[:12]
        return f"{ticket_key or 'NO_TICKET'}:{base_branch}:{digest}"

    def _derive_source_branch(self, plan: dict[str, object], analysis_text: str, idempotency_key: str, run_id: str) -> str:
        prefix = self.settings.branches.source_branch_prefix
        if not prefix.endswith("/"):
            prefix += "/"
        ticket_key = extract_current_ticket_key(analysis_text)
        branch_mode = self.settings.branches.source_branch_mode.lower().strip()
        digest = hashlib.sha1(idempotency_key.encode("utf-8")).hexdigest()[:8]
        if ticket_key:
            base = f"{prefix}{ticket_key}"
            if branch_mode == "stable":
                return base
            if branch_mode == "unique":
                return f"{base}-{run_id.replace('_', '-')}"
            return f"{base}-{digest}"
        hint = plan.get("change_request_title") or "auto-fix"
        normalized = re.sub(r"[^a-zA-Z0-9]+", "-", str(hint).strip().lower()).strip("-") or "auto-fix"
        if branch_mode == "stable":
            return prefix + normalized
        if branch_mode == "unique":
            return f"{prefix}{normalized}-{run_id.replace('_', '-')}"
        return f"{prefix}{normalized}-{digest}"

    def _build_change_request_title(self, plan: dict[str, object], ticket_key: str | None = None) -> str:
        # Format imposé v8.8 : "hotfix/{TICKET}" pour les MR de correctifs.
        # Si le ticket est inconnu (cas très rare), on retombe sur le titre proposé par le LLM.
        ticket = str(ticket_key or "").strip()
        if ticket:
            title = f"hotfix/{ticket}"
        else:
            title = str(plan.get("change_request_title") or "Automated corrective change")
        # v9.16.6 : prefixe [HINT] quand le run a ete guide par un developer_hint
        # valide. Tracabilite GitLab : on voit immediatement dans la liste des MR
        # quelles sont issues d'un guidage humain.
        _hint_guided = bool(getattr(self, "_developer_hint", None)) and bool(
            getattr(self, "_developer_hint_validation", None)
            and self._developer_hint_validation.is_valid
        )
        if _hint_guided:
            title = f"[HINT] {title}"
        # v9.14 : prefixe [AGENT] quand le plan vient de l'agent autonome d'exploration
        elif plan.get("agent_explored"):
            title = f"[AGENT] {title}"
        # v9.13.2 (legacy) : prefixe [LLM-LIBRE] - conserve pour compat mais non utilise en v9.14
        elif plan.get("llm_libre_mode"):
            title = f"[LLM-LIBRE] {title}"
        if self.settings.forge.draft and not title.lower().startswith("draft:"):
            title = f"Draft: {title}"
        # v9.14 : forcer le mode draft sur les MR agent explored meme si forge.draft=false
        elif (plan.get("agent_explored") or plan.get("llm_libre_mode")) and not title.lower().startswith("draft:"):
            title = f"Draft: {title}"
        return title

    def _build_change_request_body(self, plan: dict[str, object], changed_files: list[str], ticket_key: str | None = None, automated_tests: dict[str, object] | None = None) -> str:
        return render_change_request(
            self.prompts_dir / "change_request_template.md",
            title=self._build_change_request_title(plan, ticket_key=ticket_key),
            short_description=str(plan.get("short_description", "") or self.settings.forge.short_description),
            problem_summary=str(plan.get("problem_summary", "")),
            root_cause=str(plan.get("root_cause", "")),
            fix_summary=str(plan.get("fix_summary", "")),
            files_changed=changed_files,
            tests_suggested=[str(x) for x in plan.get("tests_suggested", [])],
            risk_notes=[str(x) for x in plan.get("risk_notes", [])],
            description_append=self.settings.forge.description_append,
            automated_tests=automated_tests,
        )

    def _generate_llm_proposal(self, analysis_text: str, result: dict[str, object], plan: dict[str, object], contract: dict[str, object], localization: dict[str, object]) -> dict[str, object]:
        selected_files = [str(x) for x in result.get("selected_files", [])] if isinstance(result.get("selected_files"), list) else []
        return self.solution_proposal_agent.propose(
            analysis_text=analysis_text,
            contract=contract,
            localization=localization,
            plan=plan,
            result=result,
            selected_files=selected_files,
        )

    def _build_diagnostic_plan(self, result: dict[str, object], plan: dict[str, object], contract: dict[str, object], localization: dict[str, object], proposal: dict[str, object] | None = None) -> dict[str, object]:
        proposal = proposal or {}
        reason = str(result.get("mr_blocked_reason") or result.get("patch_result") or "unable_to_ship")
        ticket = str(result.get("ticket_key") or "NO_TICKET")
        target_file = str(proposal.get("primary_target_file") or localization.get("target_file") or "NON_ETABLI")
        target_method = str(proposal.get("primary_target_method") or localization.get("target_method") or contract.get("must_touch_method") or "NON_ETABLI")
        title = f"Proposition automatique: correctif à valider pour {ticket}"
        proposed_solution = str(proposal.get("proposed_solution") or "Proposition technico-fonctionnelle générée automatiquement pour revue humaine.")
        return {
            "change_request_title": title,
            "short_description": f"Proposition de correctif à valider: {reason}",
            "problem_summary": str(proposal.get("problem_summary") or plan.get("problem_summary") or contract.get("problem_summary") or "Blocage de livraison du correctif automatique."),
            "root_cause": str(proposal.get("root_cause") or plan.get("unable_to_fix_reason") or contract.get("immediate_cause") or reason),
            "fix_summary": proposed_solution,
            "tests_suggested": [str(x) for x in (proposal.get("tests_to_add") or contract.get("oracle_tests") or [])[:8]],
            "risk_notes": [reason, f"target_file={target_file}", f"target_method={target_method}"] + [str(x) for x in (proposal.get("risks") or plan.get("risk_notes") or [])[:8]],
        }

    @staticmethod
    def _ensure_fixagent_gitignored(repo: RepoOps, context: RunContext) -> None:
        """v9.9: garantit que .fixagent/ est dans le .gitignore du repo cible.

        Idempotent: si l'entree existe deja, on ne fait rien. Si le fichier
        .gitignore n'existe pas, on le cree avec uniquement cette entree.

        ATTENTION: on n'ajoute PAS .gitignore au commit ici. C'est au commit_all
        suivant (sur la branche de proposition) de l'inclure si modifie. Sur une
        MR de patch automatique, on ne commit pas la modification du .gitignore
        car _write_diagnostic_file refuse de toute facon de tourner dans ce flow.
        """
        try:
            gitignore_path = repo.repo_path / ".gitignore"
            entry = ".fixagent/"
            if gitignore_path.is_file():
                content = gitignore_path.read_text(encoding="utf-8", errors="ignore")
                # Match exact ou en variant : .fixagent, .fixagent/, /.fixagent
                if any(
                    line.strip() in {entry, ".fixagent", "/.fixagent", "/.fixagent/"}
                    for line in content.splitlines()
                ):
                    return  # Deja present
                if not content.endswith("\n"):
                    content += "\n"
                content += f"# Added by autofix-agent: artifacts of the bug-fix agent\n{entry}\n"
                gitignore_path.write_text(content, encoding="utf-8")
                context.log("[FIXAGENT] .fixagent/ added to existing .gitignore")
            else:
                gitignore_path.write_text(
                    f"# Created by autofix-agent: artifacts of the bug-fix agent\n{entry}\n",
                    encoding="utf-8",
                )
                context.log("[FIXAGENT] .gitignore created with .fixagent/ entry")
        except Exception as exc:
            # Non bloquant: on log et on continue
            context.log(f"[FIXAGENT] WARN: cannot update .gitignore: {exc}")

    def _write_diagnostic_file(self, repo: RepoOps, context: RunContext, result: dict[str, object], plan: dict[str, object], contract: dict[str, object], localization: dict[str, object], proposal: dict[str, object] | None = None) -> list[str]:
        # v9.9 GARDE OPTION C: ces fichiers .fixagent/* sont DESTINES uniquement
        # aux MR de proposition (DIAGNOSTIC_MR_CREATED). Sur un flow de patch
        # automatique (MR_CREATED, PATCH_APPLIED, COMMITTED, PUSHED), le diff
        # de la MR doit etre minimal et net : aucun .fixagent ne doit polluer.
        # Aujourd'hui cette methode n'est appelee QUE depuis _maybe_ship_diagnostic_mr,
        # mais on ajoute une garde defensive au cas ou un refactor futur l'appellerait
        # ailleurs par erreur.
        current_status = str(result.get("delivery_status") or "").upper()
        patch_flow_statuses = {"PATCH_APPLIED", "COMMITTED", "PUSHED", "MR_CREATED", "MR_REUSED"}
        if current_status in patch_flow_statuses:
            context.log(f"[FIXAGENT] _write_diagnostic_file SKIPPED on patch flow (status={current_status})")
            return []

        proposal = proposal or {}
        rels: list[str] = []

        # v9.9 OPTION C: avant d'ecrire le premier .fixagent/*, on s'assure que
        # ce dossier est dans le .gitignore du repo cible. Si pas present, on
        # l'ajoute (idempotent). Cela protege le your repo d'un commit accidentel
        # de ces fichiers techniques par un dev qui ferait `git add -A` ailleurs.
        self._ensure_fixagent_gitignored(repo, context)

        rel = f".fixagent/diagnostics/{(result.get('ticket_key') or 'NO_TICKET')}-{context.run_id}.md"
        path = repo.repo_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Diagnostic FixAgent - {result.get('ticket_key') or 'NO_TICKET'}",
            "",
            f"- run_id: {result.get('run_id')}",
            f"- idempotency_key: {result.get('idempotency_key')}",
            f"- delivery_status: {result.get('delivery_status')}",
            f"- mr_blocked_reason: {result.get('mr_blocked_reason')}",
            f"- target_file: {localization.get('target_file')}",
            f"- target_method: {localization.get('target_method')}",
            f"- must_touch: {contract.get('must_touch', [])}",
            f"- must_touch_method: {contract.get('must_touch_method')}",
            "",
            "## Problem summary",
            str(proposal.get('problem_summary') or plan.get('problem_summary') or contract.get('problem_summary') or ''),
            "",
            "## Unable to fix reason",
            str(plan.get('unable_to_fix_reason') or result.get('patch_result') or ''),
            "",
            "## Validator reasons",
        ]
        lines.extend([f"- {x}" for x in (result.get('validator') or {}).get('reasons', [])])
        lines.extend(["", "## Suggested oracle tests"])
        lines.extend([f"- {x}" for x in (proposal.get('tests_to_add') or contract.get('oracle_tests', []))[:8]])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rels.append(rel.replace("\\", "/"))

        if proposal:
            proposal_rel_json = f".fixagent/proposals/{(result.get('ticket_key') or 'NO_TICKET')}-{context.run_id}.json"
            proposal_json_path = repo.repo_path / proposal_rel_json
            proposal_json_path.parent.mkdir(parents=True, exist_ok=True)
            proposal_json_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding='utf-8')
            rels.append(proposal_rel_json.replace("\\", "/"))

            proposal_rel_md = f".fixagent/proposals/{(result.get('ticket_key') or 'NO_TICKET')}-{context.run_id}.md"
            proposal_md_path = repo.repo_path / proposal_rel_md
            proposal_md_path.write_text(render_proposal_markdown(proposal, ticket_key=str(result.get('ticket_key') or '')), encoding='utf-8')
            rels.append(proposal_rel_md.replace("\\", "/"))
        return rels

    def _maybe_ship_diagnostic_mr(self, context: RunContext, result: dict[str, object], repo: RepoOps, plan: dict[str, object], contract: dict[str, object], localization: dict[str, object], analysis_text: str) -> None:
        if not self.settings.forge.always_ship_diagnostic_mr:
            return
        if result.get("pr_url"):
            return
        if not self.settings.forge.token:
            return
        proposal: dict[str, object] | None = None
        try:
            proposal = self._generate_llm_proposal(analysis_text, result, plan, contract, localization)
            context.write_json("proposal.json", proposal)
            context.write_text("proposal.md", render_proposal_markdown(proposal, ticket_key=str(result.get('ticket_key') or '')))
            result["proposal"] = proposal
            # v9.7: dump systematique de la sortie LLM brute du SolutionProposalAgent
            # pour diagnostiquer pourquoi les 5 categories d'alternative_solutions
            # ne se peuplent pas en pratique avec qwen3-coder:30b. Le LLM est appele
            # dans _generate_llm_proposal qui passe par self.llm.generate_json, donc
            # last_raw_response contient sa derniere sortie brute.
            if getattr(self.llm, 'last_raw_response', ''):
                proposal_raw_path = context.write_text('proposal_raw.txt', self.llm.last_raw_response)
                context.log(f"[LLM] raw_proposal_saved_to={proposal_raw_path} (size={len(self.llm.last_raw_response)} chars)")
            if getattr(self.llm, 'last_repaired_response', ''):
                proposal_repaired_path = context.write_text('proposal_repaired_raw.txt', self.llm.last_repaired_response)
                context.log(f"[LLM] repaired_proposal_saved_to={proposal_repaired_path}")
        except Exception as proposal_exc:
            result["proposal_error"] = str(proposal_exc)
            # v9.7: dump aussi en cas d'echec.
            if getattr(self.llm, 'last_raw_response', ''):
                try:
                    context.write_text('proposal_raw.txt', self.llm.last_raw_response)
                except Exception:
                    pass
        try:
            base_branch = str(result.get("base_branch") or self.settings.branches.base_branch)
            target_branch = str(result.get("target_branch") or self.settings.branches.target_branch)
            diagnostic_branch = f"{self._derive_source_branch(plan, analysis_text, str(result.get('idempotency_key')), str(result.get('run_id')))}-diagnostic"
            result["diagnostic_source_branch"] = diagnostic_branch
            repo.create_branch(diagnostic_branch, base_branch)
            diagnostic_rels = self._write_diagnostic_file(repo, context, result, plan, contract, localization, proposal=proposal)
            repo.commit_all(f"chore: proposal report for {result.get('ticket_key') or 'ticket'}")
            repo.push(diagnostic_branch)
            diag_plan = self._build_diagnostic_plan(result, plan, contract, localization, proposal=proposal)
            # v9.11: utilise le nouveau builder de description MR structuree
            # pour la MR de proposition aussi. Fallback sur l'ancien si erreur.
            try:
                from app.service.mr_description_builder import build_mr_description
                body = build_mr_description(
                    contract=contract,
                    localization=localization,
                    plan=plan,
                    proposal=proposal,
                    result=result,
                    ticket_key=str(result.get("ticket_key") or ""),
                    is_patch_mr=False,
                    bundle_version="v9.11",
                    model_name=self.settings.ollama.model,
                    run_id=str(result.get("run_id") or ""),
                )
                context.log("[MR] description built with v9.11 structured builder (diagnostic MR)")
            except Exception as exc:
                context.log(f"[MR] v9.11 builder failed for diag MR, fallback to legacy: {exc}")
                append = self.settings.forge.description_append
                if proposal:
                    append = (append + "\n\n" + render_proposal_markdown(proposal, ticket_key=str(result.get('ticket_key') or ''))).strip()
                body = render_change_request(
                    self.prompts_dir / "change_request_template.md",
                    title=self._build_change_request_title(diag_plan),
                    short_description=str(diag_plan.get("short_description", "")),
                    problem_summary=str(diag_plan.get("problem_summary", "")),
                    root_cause=str(diag_plan.get("root_cause", "")),
                    fix_summary=str(diag_plan.get("fix_summary", "")),
                    files_changed=diagnostic_rels,
                    tests_suggested=[str(x) for x in diag_plan.get("tests_suggested", [])],
                    risk_notes=[str(x) for x in diag_plan.get("risk_notes", [])],
                    description_append=append,
                )
            cr = self._create_change_request(diagnostic_branch, target_branch, self._build_change_request_title(diag_plan), body, repo)
            result["pr_url"] = cr.get("url")
            result["change_request_kind"] = cr.get("kind")
            result["delivery_status"] = "DIAGNOSTIC_MR_CREATED"
            result["mr_blocked_reason"] = result.get("mr_blocked_reason") or "automatic_code_patch_blocked_proposal_mr_created"
            # v9.16.7 : commentaire Jira pour MR de proposition (Q3 ok)
            ticket_key_for_jira = (
                contract.get("ticket_key")
                or result.get("ticket_key")
                or ""
            )
            try:
                self._post_jira_comment_after_mr(
                    result, str(ticket_key_for_jira), context, diag_plan, cr,
                    mr_kind="proposition",
                    logger_fn=lambda m: None,  # commenter silencieux ici
                )
            except Exception as _jira_exc:
                # Ne jamais bloquer la creation MR a cause de Jira
                result["jira_comment_error"] = str(_jira_exc)
        except Exception as diagnostic_exc:
            result["diagnostic_error"] = str(diagnostic_exc)

    def _compute_patch_block_reason(self, plan: dict[str, object], validator_report: dict[str, object], options) -> str | None:
        if plan.get("unable_to_fix"):
            return str(plan.get("unable_to_fix_reason") or "plan_unable_to_fix")
        if plan.get("already_fixed"):
            return "already_fixed"
        if not getattr(options, "apply_patch", False):
            return "apply_patch_not_requested"
        if not plan.get("edits"):
            return "no_validated_edits"
        if int(plan.get("validation_score", 0) or 0) < self.settings.agent.min_patch_score:
            return "validation_score_below_patch_threshold"
        if validator_report.get("reasons") and not validator_report.get("can_apply_patch"):
            return str((validator_report.get("reasons") or ["validator_blocked"])[0])
        return None

    def _patch_result_for_block_reason(self, reason: str, plan: dict[str, object]) -> str:
        if plan.get("unable_to_fix_reason") and reason == str(plan.get("unable_to_fix_reason")):
            return f"Unable to fix: {reason}".strip()
        mapping = {
            "already_fixed": "No effective change required; issue appears already fixed.",
            "apply_patch_not_requested": "Plan generated and validated. Add --apply-patch to create the branch and apply edits.",
            "no_validated_edits": "No effective edits generated.",
            "validation_score_below_patch_threshold": "Patch blocked: validation score below threshold.",
            "autocommit_threshold_not_met": "Patch applied on branch but auto-commit blocked by thresholds.",
            "edit_application_failed": "Patch application failed locally.",
        }
        return mapping.get(reason, f"Patch blocked: {reason}")

    def _maybe_generate_junit_tests(self, *, context, repo, contract, plan, result, logger) -> None:
        """v9.13.1 : genere et commite les classes de tests JUnit pour les
        patterns supportes apres le commit du fix.

        Conditions :
          - Le fix a ete applique avec succes (delivery_status=COMMITTED)
          - Le pattern utilise (si applicable) est dans les 4 supportes
          - Le contrat contient target_class_name + target_method_name
        """
        from app.service.test_generator import (
            generate_test_for_pattern, derive_test_path,
            extract_class_name_from_path, extract_package_from_file,
        )

        # Identifier le pattern utilise
        pattern_id = contract.get("pattern_based_fix_applied") or ""
        # Si pas de pattern (LLM a pris le relais), on regarde l'exception
        if not pattern_id:
            from app.service.pattern_based_fix_generator import detect_pattern
            pattern_id = detect_pattern(contract.get("exception_class", "")) or ""

        if not pattern_id:
            logger("[TEST_GEN] no recognized pattern, skip JUnit generation")
            return

        # Identifier le fichier et la methode cible
        target_file = ""
        must_touch_paths = contract.get("must_touch_paths") or []
        if must_touch_paths:
            target_file = must_touch_paths[0]
        elif contract.get("must_touch"):
            target_file = (contract.get("must_touch") or [""])[0]
        if not target_file:
            logger("[TEST_GEN] no target file in contract, skip")
            return

        target_class_name = extract_class_name_from_path(target_file)
        target_method_name = contract.get("must_touch_method") or contract.get("crash_method") or ""
        if "." in target_method_name:
            target_method_name = target_method_name.rsplit(".", 1)[-1]

        # Lire le contenu du fichier source pour extraire le package
        file_content = contract.get("target_file_content") or ""
        package_name = extract_package_from_file(file_content) if file_content else ""

        # Generer le test
        gen = generate_test_for_pattern(
            pattern_id=pattern_id,
            target_class_name=target_class_name,
            target_method_name=target_method_name,
            target_file_content=file_content,
            target_method_signature=contract.get("target_method_signature", ""),
            package_name=package_name,
        )
        if not gen.success:
            logger(f"[TEST_GEN] generation failed: {gen.error}")
            return

        # Ecrire le fichier de test dans le repo
        test_relative_path = derive_test_path(target_file)
        if not test_relative_path:
            logger("[TEST_GEN] cannot derive test path")
            return

        test_abs_path = repo.repo_path / test_relative_path
        try:
            test_abs_path.parent.mkdir(parents=True, exist_ok=True)
            # Si le test existe deja, ne pas l'ecraser
            if test_abs_path.exists():
                logger(f"[TEST_GEN] test file already exists at {test_relative_path}, skip")
                return
            test_abs_path.write_text(gen.test_source, encoding="utf-8")
            logger(f"[TEST_GEN] generated {gen.test_methods_count} JUnit tests at {test_relative_path}")
            # Commit separe pour les tests
            ticket = str(result.get("ticket_key") or "")
            commit_msg = f"test: ajout tests automatises {gen.pattern_used} pour {ticket}" if ticket else f"test: ajout tests automatises {gen.pattern_used}"
            repo.git("add", str(test_abs_path), check=False)
            repo.commit_all(commit_msg)
            logger(f"[TEST_GEN] tests committed: {commit_msg}")
            # Sauvegarder en artefact aussi
            context.write_text(f"generated_test_{target_class_name}Test.java", gen.test_source)
            result["junit_tests_generated"] = {
                "test_file": test_relative_path,
                "pattern": gen.pattern_used,
                "methods_count": gen.test_methods_count,
            }
        except Exception as exc:
            logger(f"[TEST_GEN] failed to write/commit test file: {exc}")

    def _create_change_request(self, source_branch: str, target_branch: str, title: str, body: str, repo: RepoOps, extra_labels: list[str] | None = None) -> dict[str, object]:
        provider = self.settings.forge.provider.lower()
        api_url = self.settings.forge.api_url.rstrip("/")
        token = self.settings.forge.token
        if not api_url or not token:
            raise RuntimeError("Missing forge API configuration (forge.api_url / FORGE_TOKEN).")
        if provider == "gitlab":
            service = GitLabService(self.settings.forge, repo)
            cr = service.create_change_request(source_branch, target_branch, title, body, extra_labels=extra_labels)
            return {"kind": cr.kind, "url": cr.web_url, "reused_existing": cr.reused_existing}
        if provider == "github":
            owner = self.settings.forge.owner
            repo_name = self.settings.forge.repo
            if not owner or not repo_name:
                raise RuntimeError("Missing forge.owner / forge.repo for GitHub.")
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            payload = {"title": title, "head": source_branch, "base": target_branch, "body": body, "draft": self.settings.forge.draft}
            response = requests.post(f"{api_url}/repos/{owner}/{repo_name}/pulls", headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            return {"kind": "pull_request", "url": data.get("html_url", ""), "reused_existing": False}
        raise RuntimeError(f"Unsupported forge provider: {provider}")

    def _post_jira_comment_after_mr(
        self, result, ticket_key, context, plan, cr, mr_kind, logger_fn=None,
    ):
        """v9.16.7 : post-traitement Jira apres creation MR.

        Decisions :
          Q3 : commenter seulement si une MR a ete creee (mr_url present)
          Q4 : idempotence par run_id (geree dans le module jira_commenter)
          Non bloquant : toute erreur Jira est loguee mais ne fait pas planter le run
        """
        try:
            from app.service.jira_commenter import post_run_comment_if_applicable
        except Exception as imp_exc:
            if logger_fn:
                logger_fn(f"[JIRA] module not loadable: {imp_exc}")
            return

        mr_url = (cr or {}).get("url") if cr else None
        if not mr_url:
            return

        # Branche : essayer de la reconstituer depuis le result ou le plan
        mr_branch = (
            result.get("source_branch")
            or (plan or {}).get("source_branch")
            or ""
        )
        run_id = result.get("run_id") or getattr(context, "run_id", "") or ""

        hint_used = bool(
            getattr(self, "_developer_hint", None)
            and getattr(self, "_developer_hint_validation", None)
            and self._developer_hint_validation.is_valid
        )

        try:
            # v9.16.11 : passer aussi contract+plan pour que le commentaire
            # Jira mirroir l'analyse complete de la MR GitLab.
            contract_for_jira = (
                result.get("correction_contract")
                if isinstance(result.get("correction_contract"), dict)
                else {}
            )
            plan_for_jira = plan if isinstance(plan, dict) else {}
            comment_url = post_run_comment_if_applicable(
                ticket_key=ticket_key or None,
                run_id=run_id,
                mr_url=mr_url,
                mr_branch=mr_branch,
                mr_kind=mr_kind,
                rovo_json=getattr(self, "_rovo_json", None),
                hint_used=hint_used,
                logger_fn=logger_fn,
                contract=contract_for_jira,
                plan=plan_for_jira,
            )
            if comment_url:
                result["jira_comment_url"] = comment_url
        except Exception as jira_exc:
            # Capture tout, n'echoue jamais
            if logger_fn:
                logger_fn(f"[JIRA] non-blocking error: {jira_exc}")
            result["jira_comment_error"] = str(jira_exc)

    @staticmethod
    def _summary_payload(result: dict[str, object]) -> dict[str, object]:
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        contract = result.get("correction_contract") if isinstance(result.get("correction_contract"), dict) else {}
        localization = result.get("localization") if isinstance(result.get("localization"), dict) else {}
        validator = result.get("validator") if isinstance(result.get("validator"), dict) else {}
        return {
            "run_id": result.get("run_id"),
            "ticket_key": result.get("ticket_key"),
            "source_branch": result.get("source_branch"),
            "target_branch": result.get("target_branch"),
            "idempotency_key": result.get("idempotency_key"),
            "delivery_status": result.get("delivery_status"),
            "mr_blocked_reason": result.get("mr_blocked_reason"),
            "model": result.get("model"),
            "selected_files_count": len(result.get("selected_files", [])) if isinstance(result.get("selected_files"), list) else 0,
            "planning_files_count": len(result.get("planning_files", [])) if isinstance(result.get("planning_files"), list) else 0,
            "plan_files_count": len(plan.get("files_to_change", [])) if isinstance(plan, dict) else 0,
            "unable_to_fix": plan.get("unable_to_fix") if isinstance(plan, dict) else None,
            "already_fixed": plan.get("already_fixed") if isinstance(plan, dict) else None,
            "validation_score": plan.get("validation_score") if isinstance(plan, dict) else None,
            "model_confidence": plan.get("confidence_score") if isinstance(plan, dict) else None,
            "must_touch": contract.get("must_touch") if isinstance(contract, dict) else [],
            "must_touch_method": contract.get("must_touch_method") if isinstance(contract, dict) else None,
            "target_file": localization.get("target_file") if isinstance(localization, dict) else None,
            "target_method": localization.get("target_method") if isinstance(localization, dict) else None,
            "validator_can_apply_patch": validator.get("can_apply_patch") if isinstance(validator, dict) else None,
            "validator_can_autocommit": validator.get("can_autocommit") if isinstance(validator, dict) else None,
            "validator_reasons": validator.get("reasons") if isinstance(validator, dict) else [],
            "patch_result": result.get("patch_result"),
            "pr_url": result.get("pr_url"),
            "diagnostic_error": result.get("diagnostic_error"),
            "proposal_error": result.get("proposal_error"),
            "proposal_primary_target_file": (result.get("proposal") or {}).get("primary_target_file") if isinstance(result.get("proposal"), dict) else None,
            "proposal_primary_target_method": (result.get("proposal") or {}).get("primary_target_method") if isinstance(result.get("proposal"), dict) else None,
            "error": result.get("error"),
            "artifacts_dir": result.get("artifacts_dir"),
        }
