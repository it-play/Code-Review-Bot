"""FastAPI 앱. 웹훅을 받고 즉시 응답한 뒤 리뷰는 백그라운드로 돌린다.

GitHub은 웹훅 응답을 10초 안에 요구한다. 리뷰를 동기로 처리하면 타임아웃으로
재전송이 발생하고 같은 PR을 중복 리뷰하게 된다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, Request, Response

from crbot.config import Settings, get_settings
from crbot.github.auth import AppAuth
from crbot.github.webhook import parse_trigger, verify_signature
from crbot.jobs import JobRunner
from crbot.llm import LLMClient
from crbot.review.engine import ReviewEngine

log = logging.getLogger("crbot")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        llm = LLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout_s=settings.llm_timeout_s,
            reasoning_effort=settings.llm_reasoning_effort,
            client=http,
        )
        app.state.settings = settings
        app.state.http = http
        app.state.llm = llm
        app.state.auth = _build_auth(settings, http)
        app.state.runner = JobRunner(
            engine=ReviewEngine(llm=llm, settings=settings),
            auth=app.state.auth,
            http=http,
            settings=settings,
        )
        try:
            yield
        finally:
            await app.state.runner.drain()


def _build_auth(settings: Settings, http: httpx.AsyncClient) -> AppAuth | None:
    """개인키가 없으면 인증을 구성하지 않는다.

    벤치마크와 유닛 테스트는 GitHub 자격 증명 없이도 앱을 띄울 수 있어야 한다.
    """
    if not settings.github_app_id or not settings.github_private_key_path.exists():
        log.warning("GitHub App 자격 증명 없음 — 웹훅 처리 비활성화")
        return None
    return AppAuth(settings.github_app_id, settings.github_private_key, http)


app = FastAPI(title="Code Review Bot", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    settings: Settings = app.state.settings
    llm: LLMClient = app.state.llm
    return {
        "ok": True,
        "llm_reachable": await llm.health(),
        "model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "github_configured": app.state.auth is not None,
        "active_jobs": app.state.runner.active,
    }


@app.post("/webhook")
async def webhook(
    request: Request,
    response: Response,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, str]:
    settings: Settings = app.state.settings
    body = await request.body()

    if not verify_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        response.status_code = 401
        return {"status": "invalid signature"}

    payload = await request.json()
    result = parse_trigger(
        x_github_event or "",
        payload,
        trigger=settings.review_trigger,
        bot_login=payload.get("_bot_login"),
    )

    if not result.accepted:
        # 무시한 이유를 남긴다. 조용히 넘기면 "왜 안 도나"를 추적할 수 없다.
        log.info("웹훅 무시: %s", result.decision.value)
        return {"status": result.decision.value}

    if app.state.auth is None:
        response.status_code = 503
        return {"status": "github not configured"}

    assert result.request is not None
    accepted = app.state.runner.submit(result.request)
    response.status_code = 202
    return {"status": "accepted" if accepted else "duplicate"}


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "code-review-bot"}


def main() -> None:
    import uvicorn

    uvicorn.run("crbot.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()


__all__ = ["app", "main"]
