"""app.service.llm_providers — Provider LLM abstrait v9.16.6.

Permet de basculer entre Ollama local (defaut) et un LLM externe via API
(Anthropic Claude, OpenAI GPT) sans toucher au reste du code.

Securite : le provider externe est OPT-IN explicite. Le defaut reste
Ollama local pour preserver la confidentialite du code your-company.

Configuration via config.env :
  LLM_PROVIDER=ollama           # defaut, local
  LLM_PROVIDER=anthropic        # externe, exige ANTHROPIC_API_KEY
  LLM_PROVIDER=openai           # externe, exige OPENAI_API_KEY

Tous les providers exposent generate_text(prompt, system=None) -> str
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

import requests

logger = logging.getLogger("autofix-agent.llm_providers")


class LLMProviderError(Exception):
    """Erreur generique d'un provider LLM."""


class LLMProvider(ABC):
    """Interface commune a tous les providers LLM."""

    name: str = "abstract"
    is_external: bool = False

    @abstractmethod
    def generate_text(self, prompt: str, system: str | None = None) -> str:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class OllamaProvider(LLMProvider):
    """Provider local Ollama (defaut). Tout reste sur la local infrastructure."""

    name = "ollama"
    is_external = False

    def __init__(self, base_url: str, model: str, temperature: float = 0,
                 num_predict: int = 4096, timeout: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_predict = num_predict
        self.timeout = timeout

    def generate_text(self, prompt: str, system: str | None = None) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.num_predict,
            },
        }
        if system:
            payload["system"] = system
        try:
            response = requests.post(
                f"{self.base_url}/api/generate", json=payload, timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json().get("response", "")
        except requests.RequestException as exc:
            raise LLMProviderError(f"ollama call failed: {exc}") from exc

    def describe(self) -> str:
        return f"ollama (local, model={self.model}, url={self.base_url})"


class AnthropicProvider(LLMProvider):
    """Provider Anthropic Claude API.

    ATTENTION : envoie le contenu des prompts (qui peut contenir du code source
    your-company) a api.anthropic.com. Necessite un contrat Zero Data Retention
    valide on your side avant utilisation en production.
    """

    name = "anthropic"
    is_external = True

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5",
                 max_tokens: int = 4096, base_url: str = "https://api.anthropic.com"):
        if not api_key:
            raise LLMProviderError(
                "ANTHROPIC_API_KEY est vide. Renseigner la cle dans secrets.env "
                "avant de basculer LLM_PROVIDER=anthropic."
            )
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")

    def generate_text(self, prompt: str, system: str | None = None) -> str:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        try:
            response = requests.post(
                f"{self.base_url}/v1/messages",
                json=payload, headers=headers, timeout=300,
            )
            response.raise_for_status()
            data = response.json()
            blocks = data.get("content") or []
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            return "".join(texts)
        except requests.RequestException as exc:
            raise LLMProviderError(f"anthropic call failed: {exc}") from exc

    def describe(self) -> str:
        return (f"anthropic (EXTERNAL, model={self.model}) "
                f"— prompts envoyes a api.anthropic.com")


class OpenAIProvider(LLMProvider):
    """Provider OpenAI GPT API. ATTENTION : envoie les prompts au cloud OpenAI."""

    name = "openai"
    is_external = True

    def __init__(self, api_key: str, model: str = "gpt-4o",
                 max_tokens: int = 4096, base_url: str = "https://api.openai.com"):
        if not api_key:
            raise LLMProviderError(
                "OPENAI_API_KEY est vide. Renseigner la cle dans secrets.env "
                "avant de basculer LLM_PROVIDER=openai."
            )
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")

    def generate_text(self, prompt: str, system: str | None = None) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        try:
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload, headers=headers, timeout=300,
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            return choices[0].get("message", {}).get("content", "")
        except requests.RequestException as exc:
            raise LLMProviderError(f"openai call failed: {exc}") from exc

    def describe(self) -> str:
        return (f"openai (EXTERNAL, model={self.model}) "
                f"— prompts envoyes a api.openai.com")


def build_provider_from_env(settings) -> LLMProvider:
    """Construit le bon provider selon la config.

    Lit LLM_PROVIDER (defaut: ollama). Si externe, recupere la cle API
    depuis l'environnement. Affiche un warning explicite si externe.
    """
    provider_name = (os.environ.get("LLM_PROVIDER") or "ollama").strip().lower()

    if provider_name == "ollama":
        return OllamaProvider(
            base_url=settings.ollama.base_url,
            model=settings.ollama.model,
            temperature=settings.ollama.temperature,
            num_predict=settings.ollama.num_predict,
            timeout=None,
        )

    if provider_name == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        return AnthropicProvider(api_key=api_key, model=model)

    if provider_name == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        model = os.environ.get("OPENAI_MODEL", "gpt-4o")
        return OpenAIProvider(api_key=api_key, model=model)

    raise LLMProviderError(
        f"LLM_PROVIDER inconnu: '{provider_name}'. "
        f"Valeurs supportees : ollama, anthropic, openai."
    )
