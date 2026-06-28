from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from app.config.settings import ForgeSettings
from app.utils.repo_utils import RepoOps


@dataclass(slots=True)
class ChangeRequestResult:
    kind: str
    web_url: str
    raw: dict[str, Any]
    reused_existing: bool = False


@dataclass(slots=True)
class GitLabService:
    settings: ForgeSettings
    repo: RepoOps
    headers: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        if not self.settings.token:
            raise RuntimeError("FORGE_TOKEN / GITLAB_TOKEN manquant dans secrets/.secrets.env")
        self.headers = {
            "PRIVATE-TOKEN": self.settings.token,
            "Accept": "application/json",
        }

    def _project_ref(self) -> str:
        project = self.settings.project_id or self.repo.infer_gitlab_project_path()
        if not project:
            raise RuntimeError("Impossible de déterminer le projet GitLab. Renseigner FORGE_PROJECT_ID ou configurer origin vers GitLab.")
        return quote(project, safe="")

    def _resolve_user_id(self, username: str) -> int | None:
        if not username:
            return None
        response = requests.get(
            f"{self.settings.api_url}/users",
            headers=self.headers,
            params={"username": username},
            timeout=30,
        )
        response.raise_for_status()
        users = response.json()
        if not users:
            return None
        return int(users[0]["id"])

    def _ensure_project_access(self) -> None:
        project_ref = self._project_ref()
        url = f"{self.settings.api_url}/projects/{project_ref}"
        response = requests.get(
            url,
            headers=self.headers,
            timeout=30,
        )
        if response.status_code == 200:
            return
        # v9.16.4 : message d'erreur DETAILLE pour diagnostiquer precisement.
        # Avant, on masquait tout derriere un message generique. Desormais le
        # code HTTP exact dit lequel des 3 problemes on a :
        #   401 -> token invalide ou expire
        #   403 -> token valide mais SANS le scope 'api' (ou pas les droits projet)
        #   404 -> FORGE_PROJECT_ID errone, ou FORGE_API_URL mal formee
        body_excerpt = (response.text or "")[:300]
        hint = {
            401: "token invalide ou expire (verifier FORGE_TOKEN dans secrets.env)",
            403: "token sans le scope 'api', ou sans droit sur ce projet "
                 "(recreer un token avec le scope 'api' coche)",
            404: "FORGE_PROJECT_ID errone ou FORGE_API_URL mal formee "
                 "(l'URL doit finir par /api/v4 ; le project id doit etre "
                 "l'ID numerique exact ou le chemin groupe/projet)",
        }.get(response.status_code, "cause inconnue")
        raise RuntimeError(
            f"Acces projet GitLab refuse. HTTP {response.status_code}. "
            f"URL appelee: {url}. Indice: {hint}. Reponse GitLab: {body_excerpt}"
        )

    def find_existing_change_request(self, source_branch: str, target_branch: str) -> ChangeRequestResult | None:
        self._ensure_project_access()
        project_ref = self._project_ref()
        response = requests.get(
            f"{self.settings.api_url}/projects/{project_ref}/merge_requests",
            headers=self.headers,
            params={
                'state': 'opened',
                'source_branch': source_branch,
                'target_branch': target_branch,
                'per_page': 20,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json() or []
        if not data:
            return None
        mr = data[0]
        return ChangeRequestResult(kind='merge_request', web_url=mr.get('web_url', ''), raw=mr, reused_existing=True)

    def create_change_request(self, source_branch: str, target_branch: str, title: str, body: str, extra_labels: list[str] | None = None) -> ChangeRequestResult:
        existing = self.find_existing_change_request(source_branch, target_branch)
        if existing is not None:
            return existing
        project_ref = self._project_ref()
        effective_title = title.strip()
        if self.settings.draft and not effective_title.lower().startswith("draft:"):
            effective_title = f"Draft: {effective_title}"

        assignee_ids: list[int] = []
        reviewer_ids: list[int] = []
        assignee_id = self._resolve_user_id(self.settings.assignee_username)
        if assignee_id is not None:
            assignee_ids.append(assignee_id)
        for username in self.settings.reviewers:
            reviewer_id = self._resolve_user_id(username)
            if reviewer_id is not None:
                reviewer_ids.append(reviewer_id)

        # v9.13.2 : labels fusionnes (config + extras dynamiques)
        all_labels = list(self.settings.labels or [])
        if extra_labels:
            for lbl in extra_labels:
                if lbl and lbl not in all_labels:
                    all_labels.append(lbl)

        payload: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": effective_title,
            "description": body,
            "remove_source_branch": False,
        }
        if all_labels:
            payload["labels"] = ",".join(all_labels)
        if assignee_ids:
            payload["assignee_ids"] = assignee_ids
        if reviewer_ids:
            payload["reviewer_ids"] = reviewer_ids

        response = requests.post(
            f"{self.settings.api_url}/projects/{project_ref}/merge_requests",
            headers=self.headers,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return ChangeRequestResult(kind="merge_request", web_url=data["web_url"], raw=data)
