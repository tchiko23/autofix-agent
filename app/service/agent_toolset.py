"""app.service.agent_toolset — 11 outils en lecture seule pour l'agent v9.14.

Tous les outils :
  - Sont en LECTURE SEULE (aucune modification du repo possible)
  - Passent par AgentSandbox pour la validation des chemins
  - Sont loggues dans sandbox.state.operations_log (directive B v9.14)
  - Ont un timeout dur (config.command_timeout_seconds)

Liste des outils disponibles pour l agent :
  1. read_file                : lit un fichier complet
  2. read_file_range          : lit lignes [start, end]
  3. list_dir                 : liste un repertoire
  4. find_files               : trouve fichiers par glob
  5. grep                     : cherche un pattern dans fichiers
  6. grep_method              : cherche definitions/appels d'une methode Java
  7. git_log                  : historique commits d'un fichier
  8. git_blame                : auteur ligne par ligne (anonymise)
  9. git_show                 : diff d'un commit
  10. find_callers            : trouve qui appelle une methode
  11. find_implementations    : trouve les implementations d'une interface

NON autorises (par design v9.14) :
  - bash arbitraire
  - write_file / modification
  - exec de scripts
  - acces reseau
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.service.agent_sandbox import AgentSandbox, SandboxViolation
from app.service.anonymizer import anonymize_text

logger = logging.getLogger("autofix-agent.toolset")


@dataclass
class ToolResult:
    """Resultat standardise d'une operation outil."""
    success: bool
    output: str
    metadata: dict[str, Any]
    error: str = ""


class AgentToolset:
    """Boite a outils en lecture seule pour l'agent d'exploration."""

    def __init__(self, sandbox: AgentSandbox):
        self.sandbox = sandbox
        self._git_available = self._check_git()

    def _check_git(self) -> bool:
        """Verifie une fois pour toutes si git est disponible."""
        try:
            result = subprocess.run(
                ["git", "--version"], capture_output=True, timeout=2, check=False,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _run_subprocess(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str, str]:
        """Helper subprocess avec timeout et capture."""
        try:
            result = subprocess.run(
                args, capture_output=True, text=True,
                timeout=self.sandbox.config.command_timeout_seconds,
                cwd=cwd or self.sandbox.config.repo_root, check=False,
                encoding="utf-8", errors="replace",
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", f"timeout after {self.sandbox.config.command_timeout_seconds}s"
        except FileNotFoundError as exc:
            return False, "", f"command not found: {exc}"

    def _log_and_return(self, tool: str, args: dict, result: ToolResult, started_at: float) -> ToolResult:
        """Log de l'operation (directive B v9.14) + retour."""
        duration_ms = int((time.time() - started_at) * 1000)
        self.sandbox.state.log_operation(
            tool=tool, args=args, result_preview=result.output,
            duration_ms=duration_ms, success=result.success, error=result.error or None,
        )
        # Estimation grossiere de tokens (1 token ~ 4 chars)
        estimated_tokens = max(50, len(result.output) // 4)
        try:
            self.sandbox.consume_tokens(estimated_tokens)
        except Exception:
            # On laisse remonter au caller pour qu'il arrete proprement
            raise
        return result

    # =========================================================================
    # OUTIL 1 : read_file
    # =========================================================================
    def read_file(self, path: str) -> ToolResult:
        """Lit un fichier complet (jusqu'a max_file_size_bytes)."""
        started_at = time.time()
        try:
            safe_path = self.sandbox.resolve_path(path)
            if not safe_path.is_file():
                result = ToolResult(False, "", {"path": str(safe_path)}, error="not_a_file")
                return self._log_and_return("read_file", {"path": path}, result, started_at)
            content = safe_path.read_text(encoding="utf-8", errors="replace")
            content = self.sandbox.cap_content(content)
            result = ToolResult(True, content, {"path": str(safe_path), "size": len(content)})
        except SandboxViolation as exc:
            result = ToolResult(False, "", {}, error=f"sandbox_violation: {exc}")
        except Exception as exc:
            result = ToolResult(False, "", {}, error=f"read_failed: {exc}")
        return self._log_and_return("read_file", {"path": path}, result, started_at)

    # =========================================================================
    # OUTIL 2 : read_file_range
    # =========================================================================
    def read_file_range(self, path: str, start_line: int, end_line: int) -> ToolResult:
        """Lit lignes [start_line, end_line] (1-indexed inclusives)."""
        started_at = time.time()
        try:
            safe_path = self.sandbox.resolve_path(path)
            if not safe_path.is_file():
                result = ToolResult(False, "", {}, error="not_a_file")
                return self._log_and_return("read_file_range", {"path": path, "start": start_line, "end": end_line}, result, started_at)
            lines = safe_path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(1, int(start_line)) - 1
            end = min(len(lines), int(end_line))
            extract = "\n".join(f"{i+1}: {lines[i]}" for i in range(start, end))
            extract = self.sandbox.cap_content(extract)
            result = ToolResult(True, extract, {"path": str(safe_path), "lines_returned": end - start})
        except SandboxViolation as exc:
            result = ToolResult(False, "", {}, error=f"sandbox_violation: {exc}")
        except Exception as exc:
            result = ToolResult(False, "", {}, error=f"read_range_failed: {exc}")
        return self._log_and_return("read_file_range", {"path": path, "start": start_line, "end": end_line}, result, started_at)

    # =========================================================================
    # OUTIL 3 : list_dir
    # =========================================================================
    def list_dir(self, path: str = ".") -> ToolResult:
        """Liste un repertoire (fichiers + sous-dossiers)."""
        started_at = time.time()
        try:
            safe_path = self.sandbox.resolve_path(path)
            if not safe_path.is_dir():
                result = ToolResult(False, "", {}, error="not_a_directory")
                return self._log_and_return("list_dir", {"path": path}, result, started_at)
            entries = []
            for entry in sorted(safe_path.iterdir()):
                rel = entry.relative_to(self.sandbox.config.repo_root)
                kind = "DIR" if entry.is_dir() else "FILE"
                size = entry.stat().st_size if entry.is_file() else 0
                entries.append(f"{kind} {rel} ({size}B)" if kind == "FILE" else f"{kind} {rel}/")
                if len(entries) >= self.sandbox.config.max_list_entries:
                    entries.append(f"... (truncated at {self.sandbox.config.max_list_entries} entries)")
                    break
            output = "\n".join(entries)
            result = ToolResult(True, output, {"path": str(safe_path), "count": len(entries)})
        except SandboxViolation as exc:
            result = ToolResult(False, "", {}, error=f"sandbox_violation: {exc}")
        except Exception as exc:
            result = ToolResult(False, "", {}, error=f"list_failed: {exc}")
        return self._log_and_return("list_dir", {"path": path}, result, started_at)

    # =========================================================================
    # OUTIL 4 : find_files
    # =========================================================================
    def find_files(self, pattern: str, base_path: str = ".") -> ToolResult:
        """Cherche fichiers correspondant a un glob (*.java, *Service*.java)."""
        started_at = time.time()
        try:
            safe_base = self.sandbox.resolve_path(base_path)
            if not safe_base.is_dir():
                result = ToolResult(False, "", {}, error="base_not_a_directory")
                return self._log_and_return("find_files", {"pattern": pattern, "base": base_path}, result, started_at)
            matches: list[str] = []
            for f in safe_base.rglob(pattern):
                if not f.is_file():
                    continue
                try:
                    rel = f.relative_to(self.sandbox.config.repo_root)
                    matches.append(str(rel))
                except ValueError:
                    continue
                if len(matches) >= self.sandbox.config.max_list_entries:
                    matches.append(f"... (truncated at {self.sandbox.config.max_list_entries} matches)")
                    break
            output = "\n".join(matches) if matches else "no matches"
            result = ToolResult(True, output, {"pattern": pattern, "count": len(matches)})
        except SandboxViolation as exc:
            result = ToolResult(False, "", {}, error=f"sandbox_violation: {exc}")
        except Exception as exc:
            result = ToolResult(False, "", {}, error=f"find_failed: {exc}")
        return self._log_and_return("find_files", {"pattern": pattern, "base": base_path}, result, started_at)

    # =========================================================================
    # OUTIL 5 : grep
    # =========================================================================
    def grep(self, pattern: str, path_glob: str = "*.java") -> ToolResult:
        """Cherche un pattern texte dans fichiers (regex Python style)."""
        started_at = time.time()
        try:
            safe_root = self.sandbox.config.repo_root
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                result = ToolResult(False, "", {}, error=f"invalid_regex: {exc}")
                return self._log_and_return("grep", {"pattern": pattern, "glob": path_glob}, result, started_at)

            matches: list[str] = []
            for f in safe_root.rglob(path_glob):
                if not f.is_file():
                    continue
                try:
                    rel = f.relative_to(safe_root)
                except ValueError:
                    continue
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for ln, line in enumerate(content.splitlines(), 1):
                    if compiled.search(line):
                        matches.append(f"{rel}:{ln}: {line.strip()[:200]}")
                        if len(matches) >= self.sandbox.config.max_grep_matches:
                            matches.append(f"... (truncated at {self.sandbox.config.max_grep_matches} matches)")
                            break
                if len(matches) >= self.sandbox.config.max_grep_matches:
                    break

            output = "\n".join(matches) if matches else "no matches"
            result = ToolResult(True, output, {"pattern": pattern, "count": len(matches)})
        except SandboxViolation as exc:
            result = ToolResult(False, "", {}, error=f"sandbox_violation: {exc}")
        except Exception as exc:
            result = ToolResult(False, "", {}, error=f"grep_failed: {exc}")
        return self._log_and_return("grep", {"pattern": pattern, "glob": path_glob}, result, started_at)

    # =========================================================================
    # OUTIL 6 : grep_method (specialise Java)
    # =========================================================================
    def grep_method(self, method_name: str) -> ToolResult:
        """Cherche definitions ET appels d'une methode Java."""
        # On combine deux patterns : definition + appels
        pattern = rf"\b{re.escape(method_name)}\s*\("
        return self.grep(pattern, path_glob="*.java")

    # =========================================================================
    # OUTIL 7 : git_log
    # =========================================================================
    def git_log(self, path: str, max_entries: int = 10) -> ToolResult:
        """Historique des derniers commits sur un fichier."""
        started_at = time.time()
        if not self._git_available:
            result = ToolResult(False, "", {}, error="git_not_available")
            return self._log_and_return("git_log", {"path": path}, result, started_at)
        try:
            safe_path = self.sandbox.resolve_path(path)
            rel = safe_path.relative_to(self.sandbox.config.repo_root)
            ok, stdout, stderr = self._run_subprocess(
                ["git", "log", f"-n{max_entries}", "--oneline", "--", str(rel)],
            )
            if ok:
                output = anonymize_text(stdout)  # anonymisation auteurs si presents
                result = ToolResult(True, output, {"path": str(rel)})
            else:
                result = ToolResult(False, "", {}, error=f"git_log_failed: {stderr.strip()}")
        except SandboxViolation as exc:
            result = ToolResult(False, "", {}, error=f"sandbox_violation: {exc}")
        except Exception as exc:
            result = ToolResult(False, "", {}, error=f"git_log_exception: {exc}")
        return self._log_and_return("git_log", {"path": path, "max": max_entries}, result, started_at)

    # =========================================================================
    # OUTIL 8 : git_blame (anonymise)
    # =========================================================================
    def git_blame(self, path: str, line: int) -> ToolResult:
        """Auteur d'une ligne (anonymise par anonymize_text)."""
        started_at = time.time()
        if not self._git_available:
            result = ToolResult(False, "", {}, error="git_not_available")
            return self._log_and_return("git_blame", {"path": path, "line": line}, result, started_at)
        try:
            safe_path = self.sandbox.resolve_path(path)
            rel = safe_path.relative_to(self.sandbox.config.repo_root)
            ok, stdout, stderr = self._run_subprocess(
                ["git", "blame", "-L", f"{line},{line}", "--", str(rel)],
            )
            if ok:
                # ANONYMISATION OBLIGATOIRE de l'auteur cat 2 (v9.13.1)
                output = anonymize_text(stdout)
                result = ToolResult(True, output, {"path": str(rel), "line": line})
            else:
                result = ToolResult(False, "", {}, error=f"git_blame_failed: {stderr.strip()}")
        except SandboxViolation as exc:
            result = ToolResult(False, "", {}, error=f"sandbox_violation: {exc}")
        except Exception as exc:
            result = ToolResult(False, "", {}, error=f"git_blame_exception: {exc}")
        return self._log_and_return("git_blame", {"path": path, "line": line}, result, started_at)

    # =========================================================================
    # OUTIL 9 : git_show (diff d'un commit)
    # =========================================================================
    def git_show(self, commit_sha: str) -> ToolResult:
        """Affiche le diff d'un commit precis."""
        started_at = time.time()
        if not self._git_available:
            result = ToolResult(False, "", {}, error="git_not_available")
            return self._log_and_return("git_show", {"sha": commit_sha}, result, started_at)
        # Valider la forme du SHA (anti-injection)
        if not re.match(r"^[0-9a-f]{4,40}$", commit_sha.lower()):
            result = ToolResult(False, "", {}, error="invalid_sha_format")
            return self._log_and_return("git_show", {"sha": commit_sha}, result, started_at)
        try:
            ok, stdout, stderr = self._run_subprocess(
                ["git", "show", "--stat", commit_sha],
            )
            if ok:
                output = anonymize_text(self.sandbox.cap_content(stdout, 30_000))
                result = ToolResult(True, output, {"sha": commit_sha})
            else:
                result = ToolResult(False, "", {}, error=f"git_show_failed: {stderr.strip()}")
        except Exception as exc:
            result = ToolResult(False, "", {}, error=f"git_show_exception: {exc}")
        return self._log_and_return("git_show", {"sha": commit_sha}, result, started_at)

    # =========================================================================
    # OUTIL 10 : find_callers
    # =========================================================================
    def find_callers(self, method_name: str) -> ToolResult:
        """Trouve qui appelle cette methode (recherche pragmatique)."""
        # Strategie : grep `\.methodName(` ou ` methodName(` (pas en debut de
        # ligne pour eviter de matcher les definitions)
        pattern = rf"(?:\.|\s){re.escape(method_name)}\s*\("
        # On reutilise grep mais on filtre le resultat pour exclure les
        # definitions (lignes contenant "public/private/protected")
        result = self.grep(pattern, path_glob="*.java")
        if not result.success:
            return result

        # Filtrer les definitions (heuristique : lignes contenant un modificateur d'access)
        filtered_lines = []
        for line in result.output.splitlines():
            if re.search(r"\b(public|private|protected|static)\b", line):
                continue
            filtered_lines.append(line)
        filtered_output = "\n".join(filtered_lines) if filtered_lines else "no callers found"
        return ToolResult(True, filtered_output, {**result.metadata, "filtered_count": len(filtered_lines)})

    # =========================================================================
    # OUTIL 11 : find_implementations
    # =========================================================================
    def find_implementations(self, interface_name: str) -> ToolResult:
        """Trouve les classes Java qui implementent une interface."""
        pattern = rf"\bimplements\s+(?:[A-Za-z0-9_,\s]+,\s*)?{re.escape(interface_name)}\b"
        return self.grep(pattern, path_glob="*.java")
