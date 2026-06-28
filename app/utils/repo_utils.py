from __future__ import annotations

import hashlib
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable


# v9.3: extraction du plus long préfixe commun entre plusieurs groupIds.
# Exemple: ["com.example.module1", "com.example.common", "com.example.shared"]
# → "com.example." (préfixe normalisé avec point de séparation final).
def _common_prefix_with_dot(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        # Pour un seul groupId, on garde les deux premiers segments si
        # disponibles (ex: "com.example") sinon on garde tel quel.
        parts = values[0].split(".")
        return ".".join(parts[:2]) + "." if len(parts) >= 2 else values[0] + "."
    # Préfixe commun caractère par caractère, puis ramené à la dernière
    # frontière de package (le dernier point précédant la divergence).
    common = values[0]
    for v in values[1:]:
        max_len = min(len(common), len(v))
        i = 0
        while i < max_len and common[i] == v[i]:
            i += 1
        common = common[:i]
    if not common:
        return ""
    # Tronque au dernier point pour garder une frontière de package nette.
    if "." in common:
        common = common.rsplit(".", 1)[0] + "."
    return common


def autodetect_application_package_prefixes(repo_path: Path, log: Callable[[str], None] | None = None) -> list[str]:
    """Scanne tous les pom.xml du dépôt cible et extrait les groupId Maven.

    Stratégie :
    1. Trouve tous les pom.xml (récursif, mais s'arrête à .git/, target/, node_modules/, build/).
    2. Pour chacun, extrait <groupId> direct, ou hérité du <parent>.
    3. Regroupe les groupIds par préfixe commun.

    Retourne une liste de préfixes prêts à l'emploi (avec point de séparation
    final). Liste vide si aucun pom.xml n'est trouvé ou si aucun groupId n'est
    extractible — auquel cas le bundle retombera sur le filtre blacklist seule.
    """
    logger = log or (lambda _: None)
    repo_path = Path(repo_path)
    if not repo_path.is_dir():
        return []

    SKIP_DIRS = {".git", "target", "node_modules", "build", "dist", ".idea", ".vscode"}

    # Namespace Maven 4
    NS = {"m": "http://maven.apache.org/POM/4.0.0"}

    group_ids: set[str] = set()
    pom_count = 0

    for pom in repo_path.rglob("pom.xml"):
        # Skip si dans un répertoire interdit
        if any(part in SKIP_DIRS for part in pom.parts):
            continue
        pom_count += 1
        try:
            tree = ET.parse(pom)
            root = tree.getroot()
        except (ET.ParseError, OSError) as exc:
            logger(f"[AUTODETECT] pom.xml unreadable: {pom} ({exc})")
            continue

        # On essaie avec namespace Maven, puis sans (vieux pom)
        gid = None
        for path_pat in ("m:groupId", "groupId"):
            ns = NS if path_pat.startswith("m:") else {}
            elem = root.find(path_pat, ns)
            if elem is not None and elem.text and elem.text.strip():
                gid = elem.text.strip()
                break
        # Si pas de groupId direct, on prend celui hérité du <parent>
        if not gid:
            for path_pat in ("m:parent/m:groupId", "parent/groupId"):
                ns = NS if path_pat.startswith("m:") else {}
                elem = root.find(path_pat, ns)
                if elem is not None and elem.text and elem.text.strip():
                    gid = elem.text.strip()
                    break
        if gid:
            group_ids.add(gid)

    logger(f"[AUTODETECT] scanned {pom_count} pom.xml, found groupIds: {sorted(group_ids)}")

    if not group_ids:
        return []

    # Stratégie : si tous les groupIds partagent un préfixe ≥ 2 segments,
    # on retourne ce seul préfixe. Sinon on retourne chaque groupId distinct
    # comme préfixe individuel (cas multi-organisation rare).
    common = _common_prefix_with_dot(sorted(group_ids))
    if common and common.count(".") >= 2:
        # Le préfixe commun a au moins 2 segments + son point final
        # (ex: "com.example." est valide, "com." trop générique).
        return [common]

    # Fallback : retourne chaque groupId comme préfixe distinct
    return sorted({gid + "." for gid in group_ids})


class RepoOps:
    def __init__(self, repo_path: Path, log: Callable[[str], None] | None = None):
        self.repo_path = Path(repo_path)
        self._tracked_files_cache: list[str] | None = None
        self.log = log or print

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_path,
            text=True,
            encoding="utf-8",
            errors="ignore",
            capture_output=True,
            check=check,
        )

    def current_branch(self) -> str:
        return (self.git("branch", "--show-current", check=False).stdout or "").strip()

    def diff_name_only(self, *, cached: bool = False) -> list[str]:
        args = ["diff", "--name-only"]
        if cached:
            args.append("--cached")
        output = self.git(*args, check=False).stdout or ""
        return [line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()]

    def has_diff(self, *, cached: bool = False) -> bool:
        return bool(self.diff_name_only(cached=cached))

    def verify_repo(self) -> None:
        probe = self.git("rev-parse", "--is-inside-work-tree", check=False)
        if probe.returncode != 0 or probe.stdout.strip().lower() != "true":
            raise RuntimeError(f"Le chemin {self.repo_path} n'est pas un dépôt Git exploitable.")

    def status_porcelain(self) -> list[str]:
        output = self.git("status", "--porcelain", check=False).stdout or ""
        return [line.rstrip("\n") for line in output.splitlines() if line.strip()]

    def restore_generated_artifacts(self, workdir_name: str = ".work") -> list[str]:
        """Nettoie/restaure les artefacts generes par le bundle.

        v9.11.1 HOTFIX : on traite aussi `.fixagent/` comme un artefact du
        bundle (pas une modification du depot). En v9.9, on ajoute `.fixagent/`
        au .gitignore du repo cible automatiquement. Resultat : Git ne le
        tracke plus, et le dossier reste sur le filesystem entre deux runs.
        A historical crash bug venait de la : `?? .fixagent/` faisait
        echouer ensure_clean.

        Le bundle considere `.fixagent/` comme un dossier technique a nettoyer
        au demarrage de chaque run (idempotence).
        """
        dirty = self.status_porcelain()
        if not dirty:
            return []

        restored: list[str] = []
        restore_targets: list[str] = []
        clean_targets: list[str] = []

        # v9.11.1: liste des artefacts bundle a nettoyer/restaurer automatiquement.
        # workdir (.work par defaut) ET .fixagent qui est un artefact bundle.
        bundle_artifact_prefixes = [workdir_name, ".fixagent"]

        for line in dirty:
            path = line[3:].strip().replace("\\", "/") if len(line) > 3 else ""
            if not path:
                continue
            normalized = path.replace("\\", "/")
            # v9.11.1: matcher contre TOUS les prefixes d'artefacts bundle
            is_bundle_artifact = False
            for prefix in bundle_artifact_prefixes:
                if normalized == prefix or normalized.startswith(f"{prefix}/") or f"/{prefix}/" in normalized:
                    is_bundle_artifact = True
                    break
            if is_bundle_artifact:
                restored.append(normalized)
                status = line[:2]
                if "?" in status:
                    clean_targets.append(normalized)
                else:
                    restore_targets.append(normalized)

        if restore_targets:
            self.git("restore", "--staged", "--worktree", "--", *restore_targets, check=False)
        if clean_targets:
            self.git("clean", "-fd", "--", *clean_targets, check=False)
        if restored:
            self.log(f"[REPO] cleaned/restored bundle artifacts: {restored}")
        return restored

    def ensure_clean(self, *, auto_restore_workdir: bool = True, workdir_name: str = ".work") -> None:
        if auto_restore_workdir:
            self.restore_generated_artifacts(workdir_name=workdir_name)
        dirty = self.status_porcelain()
        if dirty:
            raise RuntimeError(
                "Le dépôt cible n'est pas propre. Nettoyer, commit, stash ou restaurer les changements avant de lancer FixAgent.\n"
                + "\n".join(dirty)
            )

    def tracked_files(self) -> list[str]:
        if self._tracked_files_cache is not None:
            return self._tracked_files_cache
        result = self.git("ls-files")
        files = [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]
        self._tracked_files_cache = [path for path in files if self._is_candidate_source_file(path)]
        return self._tracked_files_cache

    def grep(self, pattern: str) -> str:
        self.log(f"[REPO] grep: {pattern}")
        rg_cmd = [
            "rg",
            "-n",
            "-F",
            "--hidden",
            "--glob",
            "!.git",
            "--glob",
            "!**/.gitignore",
            "--glob",
            "!**/target/**",
            "--glob",
            "!**/build/**",
            "--glob",
            "!**/.idea/**",
            "--glob",
            "!**/.settings/**",
            "--glob",
            "!**/node_modules/**",
            pattern,
            ".",
        ]
        try:
            result = subprocess.run(
                rg_cmd,
                cwd=self.repo_path,
                text=True,
                encoding="utf-8",
                errors="ignore",
                capture_output=True,
                check=False,
            )
            if result.stdout:
                return result.stdout
        except FileNotFoundError:
            pass

        result = subprocess.run(
            ["git", "grep", "-n", "-I", "-F", pattern, "--"],
            cwd=self.repo_path,
            text=True,
            encoding="utf-8",
            errors="ignore",
            capture_output=True,
            check=False,
        )
        if result.stdout:
            return result.stdout

        matches: list[str] = []
        needle = pattern.lower()
        for rel in self.tracked_files():
            path = self.repo_path / rel
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for lineno, line in enumerate(content.splitlines(), start=1):
                if needle in line.lower():
                    matches.append(f"{rel}:{lineno}:{line}")
                    if len(matches) >= 500:
                        return "\n".join(matches)
        return "\n".join(matches)

    def read_relevant_file(self, relative_path: str, *, anchors: list[str] | None = None, limit: int = 12000) -> str:
        path = self.repo_path / relative_path
        content = path.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")
        if len(content) <= limit:
            return content

        basename = Path(relative_path).stem
        effective_anchors = [basename, f"class {basename}"]
        for anchor in anchors or []:
            cleaned = anchor.strip()
            if cleaned and cleaned not in effective_anchors:
                effective_anchors.append(cleaned)

        windows: list[tuple[int, int]] = []
        for anchor in effective_anchors[:24]:
            for match in list(re.finditer(re.escape(anchor), content, flags=re.IGNORECASE))[:3]:
                windows.append((max(0, match.start() - 1100), min(len(content), match.end() + 1800)))
            if len(windows) >= 12:
                break

        if not windows:
            return content[:limit]

        windows.sort()
        merged: list[list[int]] = []
        for start, end in windows:
            if not merged or start > merged[-1][1] + 250:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)

        parts: list[str] = []
        header_limit = min(700, max(250, limit // 5))
        header = content[:header_limit].strip()
        if header:
            parts.append(header)
        used = sum(len(part) for part in parts)

        for start, end in merged:
            if end <= header_limit + 100:
                continue
            snippet = content[start:end].strip()
            if not snippet:
                continue
            candidate = "\n\n// SNIPPET PERTINENT\n" + snippet
            if used + len(candidate) > limit:
                remaining = limit - used
                if remaining < 300:
                    break
                candidate = candidate[:remaining]
            parts.append(candidate)
            used += len(candidate)
            if used >= limit:
                break

        return "".join(parts)[:limit]

    def read_files(self, paths: Iterable[str], limit: int = 12000, anchors: list[str] | None = None) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in paths:
            full = self.repo_path / path
            if full.exists() and full.is_file():
                out[path] = self.read_relevant_file(path, anchors=anchors, limit=limit)
        return out

    def remote_origin_url(self) -> str:
        result = self.git("remote", "get-url", "origin", check=False)
        return (result.stdout or "").strip()

    def infer_gitlab_project_path(self) -> str | None:
        remote = self.remote_origin_url()
        patterns = [
            r"gitlab\.com[:/](?P<path>.+?)(?:\.git)?$",
            r"/(?P<path>[^\s]+?)(?:\.git)?$",
        ]
        for pattern in patterns:
            match = re.search(pattern, remote)
            if match:
                value = match.group("path")
                if value and "/" in value:
                    return value
        return None

    def local_branch_exists(self, branch: str) -> bool:
        return bool(self.git("branch", "--list", branch, check=False).stdout.strip())

    def remote_branch_exists(self, branch: str) -> bool:
        output = self.git("branch", "-r", check=False).stdout or ""
        return f"origin/{branch}" in output

    def checkout_base(self, branch: str) -> None:
        fetch = self.git("fetch", "origin", check=False)
        if fetch.returncode != 0:
            raise RuntimeError(fetch.stderr.strip() or fetch.stdout.strip() or "git fetch origin failed")

        if self.local_branch_exists(branch):
            checkout = self.git("checkout", branch, check=False)
            if checkout.returncode != 0:
                raise RuntimeError(
                    f"Unable to checkout local branch '{branch}': {checkout.stderr.strip() or checkout.stdout.strip()}"
                )
            pull = self.git("pull", "--ff-only", "origin", branch, check=False)
            if pull.returncode != 0:
                raise RuntimeError(
                    f"Unable to fast-forward branch '{branch}': {pull.stderr.strip() or pull.stdout.strip()}"
                )
            return

        if self.remote_branch_exists(branch):
            checkout = self.git("checkout", "-B", branch, f"origin/{branch}", check=False)
            if checkout.returncode != 0:
                raise RuntimeError(
                    f"Unable to create local branch '{branch}' from origin/{branch}: {checkout.stderr.strip() or checkout.stdout.strip()}"
                )
            return

        raise RuntimeError(
            f"Base branch '{branch}' not found locally or on origin. fetch stderr={fetch.stderr.strip()!r}"
        )

    def _remote_ref_is_valid(self, branch: str) -> bool:
        """Vérifie qu'origin/{branch} pointe vers un objet commit accessible.

        La simple présence d'une ref dans refs/remotes/origin/ ne garantit pas
        que l'objet commit associé existe encore (cas d'une branche distante
        supprimée puis GC locale partielle). On teste explicitement avec
        rev-parse --verify ^{commit} qui échoue proprement si la ref est orpheline.
        """
        result = self.git("rev-parse", "--verify", "--quiet", f"origin/{branch}^{{commit}}", check=False)
        return result.returncode == 0

    def create_branch(self, branch: str, base_branch: str) -> None:
        self.checkout_base(base_branch)
        if self.local_branch_exists(branch):
            checkout = self.git("checkout", branch, check=False)
        elif self.remote_branch_exists(branch) and self._remote_ref_is_valid(branch):
            # La ref distante est saine : on la matérialise en branche locale traçante.
            checkout = self.git("checkout", "-B", branch, f"origin/{branch}", check=False)
        else:
            # Cas nominal ou ref distante orpheline: on crée la branche depuis HEAD
            # (qui est base_branch après checkout_base). Si remote_branch_exists retournait
            # True avec une ref orpheline, on crée quand même localement — le push suivant
            # régénérera l'objet côté serveur. Sinon erreur Git bloquante (cas observé
            # TICKET-NNNNN avec ref hotfix/TICKET-NNNNN-8f1c33e8 orpheline en local).
            checkout = self.git("checkout", "-b", branch, check=False)
            # Si la branche locale existait déjà (cas de course : créée entre local_branch_exists
            # et ce point), on fallback proprement.
            if checkout.returncode != 0 and "already exists" in (checkout.stderr or "").lower():
                checkout = self.git("checkout", branch, check=False)
        if checkout.returncode != 0:
            raise RuntimeError(
                f"Unable to create or checkout branch '{branch}': {checkout.stderr.strip() or checkout.stdout.strip()}"
            )

    def _find_whitespace_insensitive_span(self, content: str, search: str) -> tuple[int, int] | None:
        chunks: list[str] = []
        in_ws = False
        for ch in search:
            if ch.isspace():
                if not in_ws:
                    chunks.append(r"\s+")
                    in_ws = True
            else:
                chunks.append(re.escape(ch))
                in_ws = False
        pattern = "".join(chunks)
        if not pattern:
            return None
        match = re.search(pattern, content, flags=re.MULTILINE | re.DOTALL)
        if not match:
            return None
        return match.start(), match.end()

    def _method_span(self, content: str, method_name: str) -> tuple[int, int] | None:
        if not method_name:
            return None
        # v9.13: si le nom est qualifie (Class.method), on prend seulement la
        # partie methode pour la recherche (Java n'ecrit pas la classe devant
        # la methode dans sa propre definition).
        # Example case: method_name = "OrderResponseDto.getStatusMessage"
        # → on cherche juste "getStatusMessage"
        local_name = method_name.rsplit(".", 1)[-1] if "." in method_name else method_name
        # v9.9: regex corrigee (le ']' en trop de la classe negative [^\n{;]]* etait
        # un bug ancien qui faisait derailler le matching de signature en presence
        # d'annotations multiples. On simplifie en `[^\n]*` pour la portee de la
        # ligne de signature: c'est suffisant car le pattern \b{method_name}\s*\(
        # est tres specifique.
        sig = re.search(
            rf"(?m)^\s*(?:public|protected|private)?[^\n]*\b{re.escape(local_name)}\s*\([^\n]*\)\s*(?:throws[^\n{{]+)?\{{",
            content,
        )
        if not sig:
            sig = re.search(rf"\b{re.escape(local_name)}\s*\([^\n]*\)\s*\{{", content)
        if not sig:
            return None
        brace_start = content.find("{", sig.start())
        if brace_start < 0:
            return None
        depth = 0
        for idx in range(brace_start, len(content)):
            ch = content[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    line_start = content.rfind("\n", 0, sig.start()) + 1
                    return line_start, idx + 1
        return None

    @staticmethod
    def _detect_indent_style(content: str) -> tuple[str, int]:
        """v9.9: detecte le style d'indentation utilise dans un fichier.

        Retourne (unit, size) ou:
          - unit = "\\t" ou " " (caractere d'indentation)
          - size = nombre de caracteres par niveau d'indentation

        Heuristique: on examine les 200 premieres lignes indentees du fichier.
        Si majorite de tabs => ("\\t", 1). Si espaces, on calcule le PGCD des
        indentations vues (typique: 2 ou 4 espaces).
        """
        lines = content.splitlines()[:500]
        tab_lines = 0
        space_indents: list[int] = []
        for line in lines:
            if not line or not line[0] in (" ", "\t"):
                continue
            if line.startswith("\t"):
                tab_lines += 1
                continue
            # Compter les espaces de tete
            count = 0
            for c in line:
                if c == " ":
                    count += 1
                else:
                    break
            if count > 0:
                space_indents.append(count)
        if tab_lines > len(space_indents):
            return ("\t", 1)
        if not space_indents:
            return (" ", 4)  # defaut Java standard
        # PGCD des indentations observees
        from math import gcd
        g = space_indents[0]
        for s in space_indents[1:]:
            g = gcd(g, s)
            if g == 1:
                break
        # Si on tombe sur g=1 c'est probablement du bruit, on retombe sur 4
        if g <= 1:
            return (" ", 4)
        return (" ", g)

    @staticmethod
    def _extract_line_indent(line: str) -> str:
        """Retourne la chaine d'indentation au debut d'une ligne (tabs/espaces)."""
        indent = []
        for c in line:
            if c in (" ", "\t"):
                indent.append(c)
            else:
                break
        return "".join(indent)

    @classmethod
    def _reindent_replacement_block(
        cls,
        replacement: str,
        target_indent: str,
        indent_unit: str,
        indent_size: int,
    ) -> str:
        """v9.9: reindente un bloc de code pour matcher l'indentation cible.

        Strategie:
        1. Trouve l'indentation MINIMALE non-vide des lignes du bloc (= le
           niveau de base du LLM, souvent 0 ou rarement autre chose).
        2. Pour chaque ligne du bloc, calcule son indentation RELATIVE au minimum.
        3. Re-construit chaque ligne avec target_indent + (relative_indent
           normalise au style du fichier).

        Exemple concret:
          replacement produit par LLM (a indent 0):
              public boolean foo(Long id) {
                  if (id == null) return false;
                  return bar(id);
              }

          target_indent = "\\t\\t\\t" (3 tabs), style fichier = tab

          resultat:
              \\t\\t\\tpublic boolean foo(Long id) {
              \\t\\t\\t\\tif (id == null) return false;
              \\t\\t\\t\\treturn bar(id);
              \\t\\t\\t}
        """
        if not replacement.strip():
            return replacement

        lines = replacement.replace("\r\n", "\n").split("\n")

        # Etape 1: trouver l'indent minimal des lignes non-vides
        non_empty_indents: list[str] = []
        for line in lines:
            if line.strip():  # ignore lignes vides
                non_empty_indents.append(cls._extract_line_indent(line))
        if not non_empty_indents:
            return replacement
        # L'indent minimal correspond au plus court prefix commun
        base_indent = min(non_empty_indents, key=len)
        base_len = len(base_indent)

        # Etape 2: pour chaque ligne, calculer l'indent relatif puis reconstruire
        out_lines: list[str] = []
        for line in lines:
            if not line.strip():
                # Ligne vide: on la garde vide (pas d'indent inutile)
                out_lines.append("")
                continue
            line_indent = cls._extract_line_indent(line)
            line_body = line[len(line_indent):]

            # Indent relatif: ce qui depasse base_indent
            if line_indent.startswith(base_indent):
                relative = line_indent[base_len:]
            else:
                # Ligne moins indentee que base (cas pathologique), pas d'indent relatif
                relative = ""

            # Normaliser le relative au style cible
            # Compter les "niveaux" dans relative en supposant le style du LLM:
            # si relative contient des tabs, 1 tab = 1 niveau
            # sinon nombre d'espaces / indent_size = niveau
            if "\t" in relative:
                # LLM a utilise des tabs
                levels = relative.count("\t")
            else:
                # LLM a utilise des espaces
                # Heuristique: detecter la taille du LLM via la premiere ligne
                # relativement indentee. Souvent c'est 4 (Java) ou 2 (JS).
                spaces_count = len(relative)
                # On essaie 4 d'abord, puis 2, puis tel quel
                if spaces_count % 4 == 0:
                    levels = spaces_count // 4
                elif spaces_count % 2 == 0:
                    levels = spaces_count // 2
                else:
                    levels = 1 if spaces_count > 0 else 0

            # Reconstruire avec le style cible
            normalized_relative = (indent_unit * indent_size) * levels
            out_lines.append(target_indent + normalized_relative + line_body)

        return "\n".join(out_lines)

    def read_method_snapshot(self, relative_path: str, method_name: str, *, context_lines: int = 24) -> dict[str, str | int | bool]:
        if not relative_path:
            return {"method_found": False, "reason": "no_target_file"}
        path = self.repo_path / relative_path
        if not path.exists():
            return {"method_found": False, "reason": "file_not_found", "path": relative_path}
        content = path.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")
        span = self._method_span(content, method_name) if method_name else None
        if not span:
            # La méthode cible n'a pas été trouvée dans le fichier : on retourne un snapshot
            # dégradé ET on pose method_found=False pour permettre au pipeline de réagir
            # (ex: rechercher la méthode dans les fichiers de support, rejeter la localisation).
            return {
                "path": relative_path,
                "method_name": method_name,
                "method_found": False,
                "reason": "method_not_found_in_target_file",
                "fingerprint": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "method_source": "",
                "method_source_with_context": content[:8000],
                "start_line": 1,
                "end_line": min(len(content.splitlines()), 1),
            }
        start, end = span
        method_source = content[start:end]
        lines = content.splitlines()
        start_line = content[:start].count("\n") + 1
        end_line = content[:end].count("\n") + 1
        from_line = max(1, start_line - context_lines)
        to_line = min(len(lines), end_line + context_lines)
        with_context = "\n".join(lines[from_line - 1:to_line])
        return {
            "path": relative_path,
            "method_name": method_name,
            "method_found": True,
            "fingerprint": hashlib.sha256(method_source.encode("utf-8")).hexdigest(),
            "method_source": method_source,
            "method_source_with_context": with_context,
            "start_line": start_line,
            "end_line": end_line,
        }

    def find_method_in_files(self, candidate_paths: list[str], method_name: str) -> str:
        """Cherche la méthode `method_name` dans la liste de fichiers candidats.

        Retourne le premier chemin où la méthode est trouvée, ou chaîne vide sinon.
        Utilisé par le pipeline pour rattraper un target_file incorrect lorsque la
        méthode verrouillée (must_touch_method) n'existe pas dans le fichier choisi.
        """
        if not method_name:
            return ""
        for rel in candidate_paths:
            if not rel:
                continue
            path = self.repo_path / rel
            if not path.exists() or not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")
            except Exception:
                continue
            if self._method_span(content, method_name):
                return rel
        return ""

    def _apply_single_edit(self, content: str, edit: dict[str, str]) -> tuple[str, bool, str]:
        search = str(edit.get("search", ""))
        replace = str(edit.get("replace", ""))
        method_hint = str(edit.get("match_hint_method", "") or edit.get("target_method", "")).strip()
        strategy = str(edit.get("replace_strategy", "")).strip().lower()
        expected_fp = str(edit.get("original_method_fingerprint", "")).strip().replace("sha256:", "")

        if search and search in content:
            return content.replace(search, replace, 1), True, "exact_search"

        norm_content = content.replace("\r\n", "\n")
        norm_search = search.replace("\r\n", "\n")
        norm_replace = replace.replace("\r\n", "\n")

        if norm_search and norm_search in norm_content:
            return norm_content.replace(norm_search, norm_replace, 1), True, "normalized_newlines"

        if norm_search:
            span = self._find_whitespace_insensitive_span(norm_content, norm_search)
            if span:
                start, end = span
                return norm_content[:start] + norm_replace + norm_content[end:], True, "fuzzy_whitespace"

        if strategy == "whole_method" and method_hint:
            span = self._method_span(norm_content, method_hint)
            if span:
                start, end = span
                current_method = norm_content[start:end]
                if expected_fp and hashlib.sha256(current_method.encode("utf-8")).hexdigest() != expected_fp:
                    return content, False, "method_fingerprint_mismatch"
                replacement = norm_replace or replace
                if not replacement.strip():
                    return content, False, "empty_new_method_body"
                # v9.9: REINDENTATION CRITIQUE.
                # Le LLM produit souvent la nouvelle methode a indent 0 (collee a la marge)
                # ou avec un style d'indentation different (espaces vs tabs, 2 vs 4).
                # Si on l'insere telle quelle, la classe Java est cassee visuellement
                # et peut casser au build. On reindente la methode pour matcher
                # exactement le niveau de l'original.
                # Etape 1: trouver l'indent de la signature originale
                # (= debut de la ligne contenant la signature dans norm_content)
                original_first_line_end = norm_content.find("\n", start)
                if original_first_line_end < 0:
                    original_first_line_end = end
                original_signature_line = norm_content[start:original_first_line_end]
                target_indent = self._extract_line_indent(original_signature_line)
                # Etape 2: detecter le style global du fichier (tabs/espaces, taille)
                indent_unit, indent_size = self._detect_indent_style(norm_content)
                # Etape 3: reindenter le bloc
                reindented = self._reindent_replacement_block(
                    replacement, target_indent, indent_unit, indent_size
                )
                # On preserve un trailing newline si l'original en avait un
                if current_method.endswith("\n") and not reindented.endswith("\n"):
                    reindented += "\n"
                return norm_content[:start] + reindented + norm_content[end:], True, "whole_method_reindented"

        return content, False, "search_snippet_not_found"

    def apply_edits(self, edits: list[dict]) -> tuple[bool, str, list[str]]:
        changed_files: list[str] = []
        applied_modes: list[str] = []
        for edit in edits:
            file_path = str(edit.get("file", "")).replace("\\", "/")
            search = str(edit.get("search", ""))
            replace = str(edit.get("replace", ""))
            if not file_path or (not search and not replace):
                return False, f"Invalid edit payload: {edit}", changed_files
            path = self.repo_path / file_path
            if not path.exists():
                return False, f"File not found: {file_path}", changed_files
            content = path.read_text(encoding="utf-8", errors="ignore")
            updated, applied, mode = self._apply_single_edit(content, edit)
            if not applied:
                return False, f"Search snippet not found in {file_path}", changed_files
            if updated != content:
                path.write_text(updated, encoding="utf-8", newline="\n")
                if file_path not in changed_files:
                    changed_files.append(file_path)
                applied_modes.append(mode)
        if not changed_files:
            return True, "No effective file changes were necessary.", changed_files
        return True, f"Edits applied successfully ({', '.join(applied_modes)}).", changed_files

    def commit_all(self, message: str) -> tuple[bool, str]:
        self.git("add", "-A")
        if not self.has_diff(cached=True):
            return False, ""
        commit = self.git("commit", "-m", message, check=False)
        if commit.returncode != 0:
            stderr = (commit.stderr or "").strip()
            stdout = (commit.stdout or "").strip()
            merged = f"{stderr}\n{stdout}".lower()
            if "nothing to commit" in merged:
                return False, ""
            raise RuntimeError(f"git commit failed: {stderr or stdout}")
        sha = (self.git("rev-parse", "HEAD", check=False).stdout or "").strip()
        return True, sha

    def push(self, branch: str) -> None:
        push = self.git("push", "-u", "origin", branch, check=False)
        if push.returncode != 0:
            raise RuntimeError(f"git push failed: {push.stderr.strip() or push.stdout.strip()}")

    def purge_fixagent_from_index(self) -> tuple[bool, str]:
        """v9.11 TRIPLE CEINTURE : retire .fixagent/ de l'index Git AVANT push.

        Triple protection contre le commit accidentel de fichiers techniques
        sur une MR de patch :
          1. v9.9 - Garde dans _write_diagnostic_file (refuse selon delivery_status)
          2. v9.9 - .gitignore automatique sur le repo cible
          3. v9.11 - CETTE methode : `git rm --cached -r .fixagent/`
                     appellee explicitement avant chaque push de patch flow.

        Retourne (removed, info). removed=True si au moins un fichier .fixagent/
        a ete retire de l'index.
        """
        # Verifier d'abord si quelque chose .fixagent/ est dans l'index
        ls = self.git("ls-files", "--cached", ".fixagent/", check=False)
        cached_files = [f for f in (ls.stdout or "").strip().splitlines() if f]
        if not cached_files:
            return False, "no .fixagent/ files in index"
        # Retirer de l'index (sans toucher au filesystem)
        result = self.git("rm", "--cached", "-r", "--ignore-unmatch", ".fixagent/", check=False)
        if result.returncode != 0:
            return False, f"git rm --cached failed: {result.stderr.strip() or result.stdout.strip()}"
        return True, f"removed {len(cached_files)} .fixagent/ entries from index"

    @staticmethod
    def _is_candidate_source_file(path: str) -> bool:
        p = path.replace("\\", "/").lower()
        if p == ".gitignore" or p.endswith("/.gitignore"):
            return False
        if "/target/" in p or "/build/" in p or "/.idea/" in p or "/.settings/" in p or "/node_modules/" in p:
            return False
        if "/src/main/resources/" in p or "/src/test/resources/" in p:
            return False
        return p.endswith((".java", ".kt", ".groovy", ".xml", ".yml", ".yaml", ".properties", ".md"))
