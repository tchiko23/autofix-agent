"""app.rag.jira_client — Client Jira pour enrichir les episodes avec les
descriptions, commentaires, etiquettes et composants des tickets RUN.

Supporte Jira Cloud (basic auth email+token) ET Jira Server/DC (bearer ou basic).
Rate limiting prudent: 10 req/sec max pour ne pas saturer l'your Jira instance.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.rag.models import JiraTicketInfo
from app.rag.git_crawler import extract_rg_references


# Champs Jira a recuperer pour chaque ticket
JIRA_FIELDS = [
    "summary",
    "description",
    "priority",
    "status",
    "resolution",
    "components",
    "labels",
    "fixVersions",
    "comment",
]


class JiraClient:
    """Client minimaliste pour l'API Jira REST v2.

    Pas de dependance externe (urllib stdlib). Auth basic ou bearer.
    """

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
            raise ValueError("JIRA_BASE_URL is required")
        if not self.token:
            raise ValueError("JIRA_TOKEN is required")
        if self.auth_type == "basic" and not self.user:
            raise ValueError("JIRA_USER is required for basic auth")

    def _auth_header(self) -> str:
        if self.auth_type == "bearer":
            return f"Bearer {self.token}"
        # Basic
        creds = f"{self.user}:{self.token}".encode("utf-8")
        return f"Basic {base64.b64encode(creds).decode('ascii')}"

    def _throttle(self) -> None:
        """Respect du rate limit (10 req/sec par defaut)."""
        min_interval = 1.0 / self.rate_limit_per_sec
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call_ts = time.monotonic()

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        self._throttle()
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth_header())
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                if resp.status >= 400:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Ticket inexistant ou access denied, on continue
                return None
            self.log(f"[JIRA] HTTP {exc.code} on {path}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.log(f"[JIRA] error on {path}: {exc}")
            return None

    def fetch_ticket(self, ticket_key: str) -> JiraTicketInfo | None:
        """Recupere les details d'un ticket et le convertit en JiraTicketInfo."""
        fields = ",".join(JIRA_FIELDS)
        data = self._get(
            f"/rest/api/2/issue/{ticket_key}",
            params={"fields": fields},
        )
        if not data:
            return None

        f = data.get("fields") or {}
        summary = str(f.get("summary") or "")
        description = self._normalize_text(f.get("description"))
        priority = self._extract_name(f.get("priority"))
        status = self._extract_name(f.get("status"))
        resolution = self._extract_name(f.get("resolution"))
        components = [self._extract_name(c) for c in (f.get("components") or [])]
        labels = list(f.get("labels") or [])
        fix_versions = [self._extract_name(v) for v in (f.get("fixVersions") or [])]

        # Commentaires (limites a 20 pour eviter explosion)
        comments_data = (f.get("comment") or {}).get("comments") or []
        comments = []
        for c in comments_data[:20]:
            body = self._normalize_text(c.get("body"))
            if body:
                comments.append(body)

        # Extraction des RG referencees dans description + commentaires
        full_text = description + "\n" + "\n".join(comments)
        rg_refs = extract_rg_references(full_text)

        return JiraTicketInfo(
            key=ticket_key.upper(),
            summary=summary,
            description=description[:6000],  # Tronque pour eviter explosion taille
            priority=priority,
            status=status,
            resolution=resolution,
            components=[c for c in components if c],
            labels=labels,
            fix_versions=[v for v in fix_versions if v],
            comments=[c[:2000] for c in comments],
            referenced_rg=rg_refs,
        )

    @staticmethod
    def _extract_name(obj: Any) -> str:
        """Extrait le champ 'name' d'un objet Jira typique (priority, status, etc.)."""
        if isinstance(obj, dict):
            return str(obj.get("name") or obj.get("value") or "")
        if isinstance(obj, str):
            return obj
        return ""

    @staticmethod
    def _normalize_text(value: Any) -> str:
        """Convertit du contenu Jira (ADF, wiki, ou plain) en texte brut.

        Pour Jira Cloud le champ description peut etre un objet ADF (Atlassian
        Document Format). On essaie de tomber sur un texte plat.
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            # Format ADF: on extrait tous les nodes texte
            return JiraClient._extract_adf_text(value).strip()
        return str(value).strip()

    @staticmethod
    def _extract_adf_text(node: Any) -> str:
        """Extrait recursivement le texte d'un noeud ADF Jira Cloud."""
        if isinstance(node, dict):
            if node.get("type") == "text":
                return str(node.get("text") or "")
            content = node.get("content") or []
            parts = [JiraClient._extract_adf_text(c) for c in content]
            sep = "\n" if node.get("type") in {"paragraph", "heading", "listItem"} else " "
            return sep.join(p for p in parts if p)
        if isinstance(node, list):
            return "\n".join(JiraClient._extract_adf_text(x) for x in node if x)
        return ""


def infer_repo_from_jira_ticket(
    ticket: JiraTicketInfo | None,
    fallback: str = "unknown",
) -> str:
    """Devine le repo (back/front/batch) depuis les composants ou labels Jira.

    Strategie:
    1. Si un label/composant contient explicitement 'back'/'front'/'batch', on l'utilise
    2. Sinon, fallback (qui peut etre l'inference depuis les fichiers du commit)
    """
    if not ticket:
        return fallback

    keywords = {"back": "back", "front": "front", "batch": "batch",
                "myapp-back": "back", "myapp-front": "front", "myapp-batch": "batch"}

    # Composants en priorite (plus fiable que les labels)
    for c in ticket.components:
        c_lower = c.lower().strip()
        for kw, repo in keywords.items():
            if kw in c_lower:
                return repo

    # Puis labels
    for lbl in ticket.labels:
        lbl_lower = lbl.lower().strip()
        for kw, repo in keywords.items():
            if kw in lbl_lower:
                return repo

    return fallback
