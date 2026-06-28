"""app.service.agent_sandbox — Sandbox de securite pour l'exploration agent v9.14.

Fournit une couche de protection autour des operations de fouille du repo :
  - Verifie que tous les chemins restent dans REPO_DIR (pas d'evasion ../../etc/passwd)
  - Applique des timeouts par commande
  - Limite la taille des contenus lus (anti-OOM)
  - Compte les tours d'exploration (anti-boucle infinie)
  - Limite le budget de tokens cumules

Toutes les operations agent passent OBLIGATOIREMENT par ce sandbox.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("autofix-agent.sandbox")


class SandboxViolation(Exception):
    """Levee quand l'agent tente une operation hors perimetre autorise."""


class SandboxLimitExceeded(Exception):
    """Levee quand une limite (tours, tokens, taille) est depassee."""


@dataclass
class SandboxConfig:
    """Configuration du sandbox - tous les seuils sont ici."""
    repo_root: Path
    max_file_size_bytes: int = 100 * 1024  # 100 Ko par fichier lu
    max_grep_matches: int = 200  # max de matches retournes par grep
    max_list_entries: int = 500  # max d'entrees retournees par list_dir
    command_timeout_seconds: int = 10  # timeout par commande
    max_exploration_turns: int = 20  # max de tours d'exploration
    max_total_tokens: int = 50_000  # budget cumule de tokens

    def __post_init__(self):
        self.repo_root = Path(self.repo_root).resolve()


@dataclass
class SandboxState:
    """Etat mutable du sandbox pendant l'exploration."""
    turns_used: int = 0
    tokens_used: int = 0
    started_at: float = field(default_factory=time.time)
    operations_log: list[dict] = field(default_factory=list)

    def log_operation(self, tool: str, args: dict, result_preview: str, duration_ms: int,
                      success: bool = True, error: str | None = None) -> None:
        """v9.14 : enregistre chaque operation pour audit/debugging (directive B).

        Le log est ensuite ecrit dans exploration_trace.json a la fin du run
        pour pouvoir justifier en revue de MR ce que l'agent a fait.
        """
        entry = {
            "turn": self.turns_used,
            "tool": tool,
            "args": args,
            "result_preview": result_preview[:200] if isinstance(result_preview, str) else str(result_preview)[:200],
            "duration_ms": duration_ms,
            "elapsed_total_s": int(time.time() - self.started_at),
            "tokens_used_cumulative": self.tokens_used,
            "success": success,
        }
        if error:
            entry["error"] = error
        self.operations_log.append(entry)


class AgentSandbox:
    """Sandbox de securite pour les operations agent.

    Usage:
        sandbox = AgentSandbox(SandboxConfig(repo_root=Path("/path/to/repo")))
        safe_path = sandbox.resolve_path("src/main/java/Foo.java")  # leve SandboxViolation si evade
        sandbox.check_turn_limit()  # leve SandboxLimitExceeded si depassee
        sandbox.consume_tokens(estimated_tokens)  # idem
    """

    def __init__(self, config: SandboxConfig):
        self.config = config
        self.state = SandboxState()
        logger.info(
            "[SANDBOX] initialized repo_root=%s max_turns=%d max_tokens=%d",
            self.config.repo_root, self.config.max_exploration_turns, self.config.max_total_tokens,
        )

    def resolve_path(self, user_provided_path: str) -> Path:
        """Resout un chemin utilisateur en chemin absolu, en verifiant qu'il
        reste dans REPO_DIR. Leve SandboxViolation sinon.

        Protege contre :
          - chemins absolus (/etc/passwd, C:\\Windows\\...)
          - chemins relatifs avec ../../ (evasion)
          - symlinks pointant hors repo (resolve() suit les symlinks)
        """
        if not user_provided_path:
            raise SandboxViolation("empty path")

        # Si chemin absolu fourni, on verifie qu'il est dans repo_root
        candidate = Path(user_provided_path)
        if not candidate.is_absolute():
            candidate = self.config.repo_root / candidate

        # resolve() suit les symlinks et normalise les ../
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise SandboxViolation(f"path resolution failed: {exc}") from exc

        # Verifier que le chemin resolu est sous repo_root
        try:
            resolved.relative_to(self.config.repo_root)
        except ValueError:
            raise SandboxViolation(
                f"path escapes sandbox: requested={user_provided_path!r} "
                f"resolved={resolved} repo_root={self.config.repo_root}"
            )

        return resolved

    def check_turn_limit(self) -> None:
        """Verifie qu'on n'a pas depasse le nombre max de tours."""
        if self.state.turns_used >= self.config.max_exploration_turns:
            raise SandboxLimitExceeded(
                f"max_exploration_turns exceeded: {self.state.turns_used}/{self.config.max_exploration_turns}"
            )

    def consume_turn(self) -> None:
        """Incremente le compteur de tours."""
        self.state.turns_used += 1
        self.check_turn_limit()

    def consume_tokens(self, estimated_tokens: int) -> None:
        """Decremente le budget de tokens, leve si depasse."""
        self.state.tokens_used += max(0, int(estimated_tokens))
        if self.state.tokens_used > self.config.max_total_tokens:
            raise SandboxLimitExceeded(
                f"max_total_tokens exceeded: {self.state.tokens_used}/{self.config.max_total_tokens}"
            )

    def cap_content(self, content: str, max_bytes: int | None = None) -> str:
        """Tronque un contenu a une taille raisonnable."""
        cap = max_bytes if max_bytes is not None else self.config.max_file_size_bytes
        if len(content.encode("utf-8")) > cap:
            truncated = content[:cap]
            return truncated + f"\n\n[TRUNCATED at {cap} bytes by sandbox]"
        return content

    def summary(self) -> dict:
        """Resume final de l'exploration pour exploration_trace.json."""
        return {
            "turns_used": self.state.turns_used,
            "tokens_used": self.state.tokens_used,
            "max_turns": self.config.max_exploration_turns,
            "max_tokens": self.config.max_total_tokens,
            "duration_seconds": int(time.time() - self.state.started_at),
            "operations_count": len(self.state.operations_log),
            "operations_log": self.state.operations_log,
        }
