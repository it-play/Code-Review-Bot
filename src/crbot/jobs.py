"""백그라운드 리뷰 잡.

GitHub은 웹훅 응답을 10초 안에 요구한다. 리뷰는 그보다 오래 걸리므로 반드시
응답을 먼저 돌려주고 여기서 이어서 처리한다.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from crbot.config import Settings
from crbot.github.auth import AppAuth
from crbot.github.client import GitHubClient, LineComment
from crbot.github.progress import ProgressReporter, Stage
from crbot.github.webhook import ReviewRequest
from crbot.review.engine import ReviewEngine
from crbot.review.models import ReviewResult

log = logging.getLogger("crbot.jobs")


class JobRunner:
    def __init__(
        self,
        engine: ReviewEngine,
        auth: AppAuth | None,
        http: httpx.AsyncClient,
        settings: Settings,
    ) -> None:
        self._engine = engine
        self._auth = auth
        self._http = http
        self._settings = settings
        self._tasks: dict[tuple[str, str, int], asyncio.Task[None]] = {}

    @property
    def active(self) -> int:
        return len(self._tasks)

    def submit(self, request: ReviewRequest) -> bool:
        """리뷰를 예약한다. 이미 같은 PR을 돌고 있으면 거절한다.

        중복 트리거를 허용하면 같은 PR에 리뷰가 두 번 달리고 GPU도 두 배로 쓴다.
        """
        key = (request.owner, request.repo, request.pr_number)
        if key in self._tasks:
            log.info("중복 트리거 무시: %s/%s#%d", *key)
            return False

        task = asyncio.create_task(self._run(request))
        self._tasks[key] = task
        task.add_done_callback(lambda _: self._tasks.pop(key, None))
        return True

    async def drain(self) -> None:
        """종료 시 진행 중인 잡을 기다린다. 안 기다리면 진행 상태 코멘트가 멈춘 채 남는다."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def _run(self, request: ReviewRequest) -> None:
        assert self._auth is not None
        gh = GitHubClient(self._auth, request.installation_id, self._http)
        progress = ProgressReporter(
            gh,
            request.owner,
            request.repo,
            model=self._settings.llm_model,
            debounce_s=self._settings.progress_debounce_s,
        )

        try:
            await gh.add_reaction(request.owner, request.repo, request.comment_id)
            await progress.start(request.pr_number)

            await progress.set_stage(Stage.FETCHING)
            pr = await gh.get_pull_request(request.owner, request.repo, request.pr_number)
            files = await gh.list_pull_request_files(
                request.owner, request.repo, request.pr_number
            )
            if not files:
                await progress.finish("변경된 파일이 없어 리뷰할 내용이 없습니다.")
                return

            result = await self._engine.review(pr, files, progress)
            await self._publish(gh, request, pr.head_sha, result, progress)

        except httpx.HTTPStatusError as exc:
            log.exception("GitHub API 오류")
            await self._fail(progress, f"GitHub API 오류 ({exc.response.status_code})")
        except httpx.HTTPError:
            log.exception("LLM 또는 네트워크 오류")
            await self._fail(progress, "LLM 서버에 연결할 수 없습니다.")
        except Exception:
            log.exception("리뷰 실패")
            await self._fail(progress, "예상치 못한 오류가 발생했습니다.")

    async def _publish(
        self,
        gh: GitHubClient,
        request: ReviewRequest,
        head_sha: str,
        result: ReviewResult,
        progress: ProgressReporter,
    ) -> None:
        comments = [
            LineComment(path=f.path, line=f.line, body=f.render()) for f in result.findings
        ]
        if comments:
            try:
                await gh.create_review(
                    request.owner,
                    request.repo,
                    request.pr_number,
                    commit_id=head_sha,
                    body=result.summary,
                    comments=comments,
                )
            except httpx.HTTPStatusError as exc:
                # 422는 대개 줄 번호가 diff 밖이라는 뜻이다. 라인 코멘트를 포기하고
                # 요약만이라도 남긴다 — 침묵하는 것보다 낫다.
                if exc.response.status_code != 422:
                    raise
                log.warning("라인 코멘트 거절(422), 요약만 게시한다")
                await progress.finish(_summary_with_inline(result))
                return

        body = result.summary if comments else _summary_with_inline(result)
        await progress.finish(body)

    async def _fail(self, progress: ProgressReporter, message: str) -> None:
        try:
            await progress.fail(message)
        except Exception:
            log.exception("실패 상태 기록도 실패")


def _summary_with_inline(result: ReviewResult) -> str:
    """라인 코멘트를 못 다는 경우 지적을 요약 본문에 담는다."""
    parts = [result.summary]
    if result.findings:
        for finding in result.findings:
            # alert 블록은 목록 항목 안에 넣으면 렌더링이 깨진다. 위치를 별도 줄로 뺀다.
            parts += ["", f"`{finding.path}:{finding.line}`", finding.render()]
    if result.skipped:
        parts.append("")
        skipped = ", ".join(f"`{name}`({reason})" for name, reason in result.skipped[:10])
        parts.append(f"<sub>건너뜀: {skipped}</sub>")
    return "\n".join(parts)
