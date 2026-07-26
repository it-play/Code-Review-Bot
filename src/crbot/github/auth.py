"""GitHub App 인증: 앱 JWT -> 설치 액세스 토큰.

githubkit 대신 httpx+pyjwt를 직접 쓴다. 필요한 엔드포인트가 여섯 개뿐이고,
30초 SLO를 계측하려면 타임아웃과 헤더를 직접 잡는 편이 예측 가능하다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import jwt

GITHUB_API = "https://api.github.com"

# 토큰은 1시간 유효하지만 만료 직전에 쓰면 요청 도중 만료될 수 있다.
_EXPIRY_MARGIN_S = 300


@dataclass
class _CachedToken:
    token: str
    expires_at: float

    @property
    def valid(self) -> bool:
        return time.time() < self.expires_at - _EXPIRY_MARGIN_S


class AppAuth:
    """설치별 액세스 토큰을 발급하고 캐시한다."""

    def __init__(self, app_id: str, private_key: str, client: httpx.AsyncClient) -> None:
        self.app_id = app_id
        self.private_key = private_key
        self._client = client
        self._cache: dict[int, _CachedToken] = {}

    def app_jwt(self) -> str:
        """앱 자신을 증명하는 JWT. 유효기간 상한은 10분이라 9분으로 잡는다.

        iat를 60초 당기는 것은 GitHub 서버와의 시계 오차 대비 (공식 권장).
        """
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.app_id},
            self.private_key,
            algorithm="RS256",
        )

    async def installation_token(self, installation_id: int) -> str:
        cached = self._cache.get(installation_id)
        if cached and cached.valid:
            return cached.token

        response = await self._client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        data = response.json()

        # "2026-07-25T12:00:00Z" -> epoch
        expires_at = _parse_iso8601(data["expires_at"])
        self._cache[installation_id] = _CachedToken(data["token"], expires_at)
        return data["token"]


def _parse_iso8601(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
