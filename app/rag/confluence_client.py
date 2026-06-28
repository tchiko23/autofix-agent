"""app.rag.confluence_client — Client Confluence pour recuperer les pages
des Regles de Gestion <DOMAIN>.RG<N>.

Utilise l'API CQL pour rechercher les pages par titre exact.
Re-utilise les credentials Jira si Confluence partage la meme auth (cas Atlassian Cloud).
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from typing import Any

from app.rag.models import RuleOfBusiness


# Regex de nettoyage HTML Confluence (storage format)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class ConfluenceClient:
    """Client minimaliste pour l'API Confluence REST."""

    def __init__(
        self,
        base_url: str,
        user: str | None = None,
        token: str | None = None,
        auth_type: str = "basic",
        rate_limit_per_sec: int = 10,
        timeout_sec: int = 30,
        log=print,
    ):
        self.base_url = base_url.rstrip("/")
        self.user = user or ""
        self.token = token or ""
        self.auth_type = (auth_type or "basic").lower()
        self.rate_limit_per_sec = max(1, rate_limit_per_sec)
        self.timeout_sec = timeout_sec
        self.log = log
        self._last_call_ts = 0.0

        if not self.base_url:
            raise ValueError("CONFLUENCE_BASE_URL is required")
        if not self.token:
            raise ValueError("CONFLUENCE_TOKEN is required")

    def _auth_header(self) -> str:
        if self.auth_type == "bearer":
            return f"Bearer {self.token}"
        creds = f"{self.user}:{self.token}".encode("utf-8")
        return f"Basic {base64.b64encode(creds).decode('ascii')}"

    def _throttle(self) -> None:
        min_interval = 1.0 / self.rate_limit_per_sec
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call_ts = time.monotonic()

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        self._throttle()
        # Confluence Cloud utilise /wiki/rest/api/, Confluence Server /rest/api/
        # On essaie /wiki/rest/api/ d'abord (Cloud), fallback /rest/api/ (Server)
        for path_prefix in ("/wiki/rest/api", "/rest/api"):
            full_path = path_prefix + path
            url = self.base_url + full_path
            if params:
                url += "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url)
            req.add_header("Authorization", self._auth_header())
            req.add_header("Accept", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    if resp.status >= 400:
                        continue
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                self.log(f"[CONFLUENCE] HTTP {exc.code} on {full_path}")
                continue
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
                continue
        return None

    def find_rule_of_business(
        self,
        reference: str,
        space_keys: list[str] | None = None,
    ) -> RuleOfBusiness | None:
        """Cherche une page Confluence dont le titre matche la reference RG."""
        cql_parts = [f'title = "{reference}"', 'type = "page"']
        if space_keys:
            spaces_clause = " OR ".join(f'space.key = "{s}"' for s in space_keys)
            cql_parts.append(f"({spaces_clause})")
        cql = " AND ".join(cql_parts)

        data = self._get(
            "/content/search",
            params={"cql": cql, "expand": "body.storage,space,version", "limit": "3"},
        )
        if not data:
            return None

        results = data.get("results") or []
        if not results:
            return None

        page = results[0]
        body = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
        plain_text = self._html_to_text(body)
        space_key = ((page.get("space") or {}).get("key") or "")
        last_modified = ((page.get("version") or {}).get("when") or "")

        # URL publique de la page
        webui = ((page.get("_links") or {}).get("webui") or "")
        url = (self.base_url.rstrip("/") + webui) if webui else ""

        return RuleOfBusiness(
            reference=reference,
            title=str(page.get("title") or reference),
            content=plain_text[:5000],  # Tronque pour eviter explosion
            url=url,
            space_key=space_key,
            last_modified=last_modified,
        )

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Convertit du HTML Confluence storage format en texte brut."""
        if not html:
            return ""
        # Remplacer les balises de paragraphe / saut de ligne par \n
        text = re.sub(r"<(p|br|li|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"</(p|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
        # Strip toutes les autres balises
        text = _HTML_TAG_RE.sub(" ", text)
        # Decoder les entites HTML
        text = unescape(text)
        # Normaliser les espaces
        text = _WHITESPACE_RE.sub(" ", text)
        # Restaurer les vrais newlines (encodes comme " \n ")
        text = re.sub(r"\s*\n\s*", "\n", text)
        return text.strip()
