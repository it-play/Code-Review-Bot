"""GitHub REST 클라이언트. 필요한 엔드포인트만 얇게 감싼다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from crbot.github.auth import GITHUB_API, AppAuth

_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class PullRequestFile:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    previous_filename: str | None = None

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    @property
    def is_reviewable(self) -> bool:
        """patch가 없으면 리뷰할 수 없다 — 바이너리이거나 GitHub가 너무 커서 생략한 파일."""
        return bool(self.patch) and self.status != "removed"


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    head_sha: str
    base_sha: str
    draft: bool


@dataclass(frozen=True)
class LineComment:
    """PR 리뷰의 라인 코멘트 하나.

    line/side는 diff의 위치가 아니라 파일의 위치를 가리킨다. 여기가 어긋나면
    GitHub이 리뷰 전체를 422로 거절한다 (PLAN.md L1 최우선 테스트 대상).
    """

    path: str
    line: int
    body: str
    side: str = "RIGHT"
    start_line: int | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "body": self.body,
        }
        if self.start_line is not None and self.start_line < self.line:
            payload["start_line"] = self.start_line
            payload["start_side"] = self.side
        return payload


class GitHubClient:
    def __init__(self, auth: AppAuth, installation_id: int, client: httpx.AsyncClient) -> None:
        self._auth = auth
        self._installation_id = installation_id
        self._client = client

    async def _headers(self) -> dict[str, str]:
        token = await self._auth.installation_token(self._installation_id)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._client.request(
            method, f"{GITHUB_API}{path}", headers=await self._headers(), **kwargs
        )
        response.raise_for_status()
        return response

    async def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        data = (await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")).json()
        return PullRequest(
            number=data["number"],
            title=data.get("title") or "",
            body=data.get("body") or "",
            head_sha=data["head"]["sha"],
            base_sha=data["base"]["sha"],
            draft=bool(data.get("draft")),
        )

    async def list_pull_request_files(
        self, owner: str, repo: str, number: int, *, max_files: int = 300
    ) -> list[PullRequestFile]:
        files: list[PullRequestFile] = []
        page = 1
        while len(files) < max_files:
            response = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            batch = response.json()
            if not batch:
                break
            for item in batch:
                files.append(
                    PullRequestFile(
                        filename=item["filename"],
                        status=item["status"],
                        additions=item.get("additions", 0),
                        deletions=item.get("deletions", 0),
                        patch=item.get("patch"),
                        previous_filename=item.get("previous_filename"),
                    )
                )
            if len(batch) < 100:
                break
            page += 1
        return files[:max_files]

    async def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> int:
        response = await self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body}
        )
        return int(response.json()["id"])

    async def update_issue_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> None:
        await self._request(
            "PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment_id}", json={"body": body}
        )

    async def add_reaction(
        self, owner: str, repo: str, comment_id: int, content: str = "eyes"
    ) -> None:
        """트리거 코멘트에 즉시 반응을 남긴다. 진행률 코멘트보다 먼저 보이는 첫 신호."""
        try:
            await self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions",
                json={"content": content},
            )
        except httpx.HTTPStatusError:
            # 반응은 부가 기능이다. 실패해도 리뷰는 계속 진행해야 한다.
            pass

    async def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[LineComment],
    ) -> None:
        """라인 코멘트를 묶어 리뷰 하나로 게시한다.

        코멘트를 개별로 올리면 요청 수만큼 알림이 가고 rate limit도 빨리 닳는다.
        """
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "body": body,
            "event": "COMMENT",
            "comments": [c.to_payload() for c in comments],
        }
        await self._request("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload)
