from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass(slots=True)
class OllamaPlannerClient:
    base_url: str
    model: str
    temperature: float = 0.0
    num_predict: int = 4096
    disable_thinking: bool = True
    last_raw_response: str = field(default="", init=False)
    last_repaired_response: str = field(default="", init=False)
    last_error: str = field(default="", init=False)

    def generate_json(self, prompt: str, system: str | None = None) -> dict[str, Any]:
        self.last_error = ""
        self.last_repaired_response = ""
        raw = self._generate_raw(prompt=prompt, system=system, expect_json=True)
        self.last_raw_response = raw
        parsed = self._parse_json_candidates(raw)
        if parsed is not None:
            return parsed

        repaired = self._repair_json(raw)
        if repaired:
            self.last_repaired_response = repaired
            parsed = self._parse_json_candidates(repaired)
            if parsed is not None:
                return parsed

        self.last_error = f"Model did not return valid JSON. Model={self.model!r}."
        raise RuntimeError(
            f"Model did not return valid JSON. Model={self.model!r}. Raw output starts with: {raw[:500]!r}"
        )

    def generate_text(self, prompt: str, system: str | None = None) -> str:
        """Génération de texte libre (pas de JSON forcé côté Ollama).

        Utilisé par le fallback method-only où le modèle doit produire du code Java brut.
        Le caller est responsable d'extraire ce qu'il veut de la sortie.
        """
        self.last_error = ""
        raw = self._generate_raw(prompt=prompt, system=system, expect_json=False)
        self.last_raw_response = raw
        return raw

    def _generate_raw(self, prompt: str, system: str | None = None, *, expect_json: bool = True) -> str:
        # Injection du marqueur /no_think pour les modèles Qwen3 qui, par défaut, émettent un
        # bloc <think>...</think> avant la vraie réponse. Sur CPU et avec num_predict limité,
        # ce bloc peut saturer le budget de tokens avant que le code utile ne soit généré,
        # renvoyant une sortie tronquée (observé TICKET-NNNNN: 5 à 11 caractères après 3h+).
        # Ce marqueur est ignoré par les modèles qui ne supportent pas le thinking mode.
        effective_system = system or ""
        if self.disable_thinking and "/no_think" not in effective_system:
            effective_system = f"/no_think {effective_system}".strip()

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.num_predict,
            },
        }
        if expect_json:
            # Contraint Ollama à produire du JSON valide.
            # On le désactive pour les générations de texte libre (ex: code Java brut)
            # car forcer le format JSON dans ce cas fait dérailler le modèle.
            payload["format"] = "json"
        if effective_system:
            payload["system"] = effective_system
        try:
            response = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=None)
        except requests.RequestException as exc:
            raise RuntimeError(f"Ollama is unreachable on {self.base_url}. Start `ollama serve` and verify the port.") from exc
        if response.status_code == 404:
            available = self._available_models_safe()
            raise RuntimeError(
                f"Ollama returned 404 on /api/generate. Configured model={self.model!r}. Available models={available}"
            )
        response.raise_for_status()
        raw = response.json().get("response", "")
        # Nettoyage défensif : si le modèle a quand même émis un bloc <think>...</think>,
        # on le retire pour ne garder que la réponse utile. Gère aussi le cas d'un bloc
        # <think> non refermé (sortie tronquée) en prenant ce qui suit une éventuelle </think>.
        raw = self._strip_thinking_blocks(raw)
        if not raw:
            raise RuntimeError(f"Empty response from Ollama for model {self.model!r}")
        return raw

    @staticmethod
    def _strip_thinking_blocks(text: str) -> str:
        if not text:
            return text
        cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Cas d'un <think> non refermé: on garde ce qu'il y a après la dernière </think>
        # (ou la sortie telle quelle si aucune balise).
        if "<think>" in cleaned.lower() and "</think>" in cleaned.lower():
            idx = cleaned.lower().rfind("</think>")
            if idx >= 0:
                cleaned = cleaned[idx + len("</think>"):]
        return cleaned.strip()

    def _parse_json_candidates(self, raw: str) -> dict[str, Any] | None:
        for candidate in self._json_candidates(raw):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _json_candidates(self, raw: str) -> list[str]:
        text = (raw or "").strip()
        candidates: list[str] = []
        if not text:
            return candidates
        candidates.append(text)
        if text.startswith("```"):
            for part in text.split("```"):
                trimmed = part.strip()
                if trimmed.startswith("json"):
                    trimmed = trimmed[4:].strip()
                if trimmed.startswith("{") and trimmed.endswith("}"):
                    candidates.append(trimmed)
        extracted = self._extract_first_json_object(text)
        if extracted and extracted not in candidates:
            candidates.append(extracted)
        normalized = text.replace("\r\n", "\n").strip()
        if normalized not in candidates:
            candidates.append(normalized)
        return candidates

    def _extract_first_json_object(self, text: str) -> str | None:
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:idx + 1]
        return None

    def _repair_json(self, raw: str) -> str:
        repair_prompt = (
            "Transform the following malformed JSON-like content into one strict valid JSON object. "
            "Preserve the same meaning and keys when possible. Output only JSON.\n\n"
            f"MALFORMED_INPUT:\n{raw}"
        )
        try:
            return self._generate_raw(
                prompt=repair_prompt,
                system="You repair malformed JSON. Output a single strict JSON object only.",
            )
        except Exception:
            return ""

    def _available_models_safe(self) -> list[str]:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=15)
            response.raise_for_status()
            payload = response.json()
            return [m.get("name", "") for m in payload.get("models", [])]
        except Exception:
            return []
