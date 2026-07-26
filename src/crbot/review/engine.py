"""리뷰 오케스트레이션.

파일 단위로 돌면서 진행 상태를 갱신하고 지적을 모은다.
Analyzer를 프로토콜로 분리해서 Phase 1(더미)과 Phase 2(LLM)가 같은 흐름을 쓴다.
"""

from __future__ import annotations

import logging
from typing import Protocol

from crbot.config import Settings
from crbot.github.client import PullRequest, PullRequestFile
from crbot.github.progress import ProgressReporter, Stage, WorkUnit
from crbot.llm import LLMClient
from crbot.review.diff import Hunk, commentable_lines, parse_patch, snap_to_commentable
from crbot.review.models import Finding, ReviewResult, Severity, build_work_units
from crbot.review.prompt import MAX_FINDINGS_PER_FILE, build_messages, parse_findings

log = logging.getLogger("crbot.review")


class Analyzer(Protocol):
    async def analyze(self, path: str, hunks: list[Hunk], context: str = "") -> list[Finding]: ...


class DummyAnalyzer:
    """Phase 1 전용. LLM 없이 GitHub 연동 경로를 관통시킨다.

    변경된 첫 줄에 고정 지적을 단다 — 줄 번호 매핑이 실제로 맞는지 확인하는 게 목적이다.
    """

    async def analyze(self, path: str, hunks: list[Hunk], context: str = "") -> list[Finding]:
        from crbot.review.models import Severity

        for hunk in hunks:
            for line in hunk.added_lines:
                return [
                    Finding(
                        path=path,
                        line=line.new_lineno or 1,
                        severity=Severity.LOW,
                        title="더미 리뷰 (Phase 1)",
                        body="LLM 미연결 상태의 확인용 코멘트입니다.",
                    )
                ]
        return []


class LLMAnalyzer:
    def __init__(
        self,
        llm: LLMClient,
        *,
        max_findings: int = MAX_FINDINGS_PER_FILE,
        max_output_tokens: int = 400,
    ) -> None:
        self._llm = llm
        self._max_findings = max_findings
        self._max_output_tokens = max_output_tokens

    async def analyze(self, path: str, hunks: list[Hunk], context: str = "") -> list[Finding]:
        messages = build_messages(
            path=path, hunks=hunks, context=context, max_findings=self._max_findings
        )
        # max_tokens는 30초 예산의 직접적인 레버다. 상한이 없으면 모델이 장문을 쓴다.
        result = await self._llm.complete(
            messages, max_tokens=self._max_output_tokens, temperature=0.1
        )
        log.debug(
            "%s: %d토큰, %.1f tok/s, prefill %.2fs, 사고 %.2fs",
            path,
            result.completion_tokens,
            result.tokens_per_s,
            result.first_token_s,
            result.thinking_s,
        )
        if result.truncated:
            # 잘린 응답은 마지막 블록이 깨져서 조용히 유실된다. 원인을 남긴다.
            log.warning(
                "%s: 출력이 max_tokens(%d)에 걸려 잘렸다", path, self._max_output_tokens
            )
        return parse_findings(result.text, path)[: self._max_findings]


class ReviewEngine:
    def __init__(
        self,
        llm: LLMClient,
        settings: Settings,
        analyzer: Analyzer | None = None,
    ) -> None:
        self._settings = settings
        self._analyzer = analyzer or LLMAnalyzer(
            llm,
            max_findings=settings.review_max_findings_per_file,
            max_output_tokens=settings.review_max_output_tokens,
        )

    async def review(
        self,
        pr: PullRequest,
        files: list[PullRequestFile],
        progress: ProgressReporter,
    ) -> ReviewResult:
        units = build_work_units(files)
        await progress.plan(units)

        by_name = {f.filename: f for f in files}
        total_weight = sum(u.weight for u in units)
        # 규모가 크면 파일당 지적 수를 줄여 30초 예산을 지킨다
        fallback = total_weight > self._settings.review_max_lines

        findings: list[Finding] = []
        skipped: list[tuple[str, str]] = []

        for unit in units:
            if unit.skipped:
                skipped.append((unit.key, unit.skipped_reason))
                await progress.complete_unit(unit.key, skipped=unit.skipped_reason)
                continue

            await progress.begin_unit(unit.key)
            file = by_name[unit.key]
            file_findings = await self._review_file(file, fallback=fallback)
            findings.extend(file_findings)
            await progress.complete_unit(unit.key, findings=len(file_findings))

        await progress.set_stage(Stage.COMPOSING)
        return ReviewResult(
            findings=findings,
            summary=self._summarize(pr, findings, units, fallback=fallback),
            fallback_mode=fallback,
            skipped=skipped,
        )

    async def _review_file(self, file: PullRequestFile, *, fallback: bool) -> list[Finding]:
        patch = file.patch or ""
        hunks = parse_patch(patch)
        if not hunks:
            return []

        try:
            raw = await self._analyzer.analyze(file.filename, hunks)
        except Exception:
            # 파일 하나가 실패해도 나머지 리뷰는 계속한다
            log.exception("파일 분석 실패: %s", file.filename)
            return []

        allowed = commentable_lines(patch)
        valid: list[Finding] = []
        for finding in raw:
            snapped = snap_to_commentable(finding.line, allowed)
            if snapped is None:
                # 모델이 diff에 없는 줄을 지목했다. 그대로 올리면 리뷰 전체가 422다.
                log.info("범위 밖 지적 폐기: %s:%d", finding.path, finding.line)
                continue
            finding.line = snapped
            valid.append(finding)

        limit = 2 if fallback else self._settings.review_max_findings_per_file
        return valid[:limit]

    def _summarize(
        self,
        pr: PullRequest,
        findings: list[Finding],
        units: list[WorkUnit],
        *,
        fallback: bool,
    ) -> str:
        reviewed = [u for u in units if not u.skipped]
        lines = sum(u.weight for u in reviewed)

        parts = ["## 코드 리뷰 결과", ""]

        if not findings:
            parts += [
                "> [!TIP]",
                "> 변경 사항을 확인했고, 지적할 만한 문제를 찾지 못했습니다.",
                "",
            ]
        else:
            counts: dict[Severity, int] = {}
            for finding in findings:
                counts[finding.severity] = counts.get(finding.severity, 0) + 1
            # 심각한 것부터 센다
            breakdown = ", ".join(
                f"{s.label} {counts[s]}건" for s in Severity if s in counts
            )
            parts += [f"{len(findings)}건을 지적했습니다 ({breakdown}).", ""]

        parts.append(f"검토 범위: {len(reviewed)}개 파일 / {lines}줄")

        if fallback:
            # 지적이 없을 때야말로 이 안내가 중요하다. 축소된 리뷰가 아무것도 못 찾은 것과
            # 온전한 리뷰가 아무것도 못 찾은 것은 신뢰도가 다르다.
            parts += [
                "",
                "> [!NOTE]",
                f"> 변경 규모가 기준({self._settings.review_max_lines}줄)을 넘어 "
                "파일당 지적 수를 줄인 요약 모드로 리뷰했습니다.",
            ]
        return "\n".join(parts)
