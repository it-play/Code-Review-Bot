"""진행 상태 코멘트.

spec의 "코드 리뷰 시 진행률이 보여야 한다"(gemini-code-assist에서 겪은 '언제 끝나는지
모름' 문제)에 대응한다. 다만 표시하는 것은 진행'률'이 아니라 진행'상태'다.

이 구분이 설계의 전부다:

  셀 수 있는 층 — 리뷰를 시작하기 전에 diff에서 "12개 파일 / 34개 hunk / 287라인"이
    이미 확정된다. 작업 단위가 시작 시점에 셀 수 있으므로 `7/12 파일`은 추정이 아니라
    사실이다. 이 층은 체크리스트로 표시한다.

  셀 수 없는 층 — 한 파일에 대한 LLM 호출 내부. 몇 토큰이 나올지는 끝나봐야 안다.
    여기에 %를 붙이면 그건 지어낸 숫자다. 경과 시간과 "지금 무엇을 하는 중인지"만 쓴다.

분모는 파일 개수가 아니라 변경 라인 수로 가중한다. 3라인짜리 파일과 200라인짜리
파일이 같은 한 칸을 먹으면 표시가 체감과 어긋난다.

갱신은 debounce 한다. GitHub의 secondary rate limit은 짧은 시간에 몰린 쓰기에
민감해서, 단위마다 갱신하면 큰 PR에서 403으로 막힌다. 어차피 갱신이 발생하는
지점은 띄엄띄엄한 체크포인트라 이게 자연스러운 입자 크기이기도 하다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from crbot.github.client import GitHubClient

# 체크리스트를 통째로 그리면 큰 PR에서 코멘트가 화면을 덮는다.
_MAX_LISTED_UNITS = 12


class Stage(StrEnum):
    QUEUED = "대기 중"
    FETCHING = "변경 사항 수집 중"
    INDEXING = "코드베이스 맥락 검색 중"
    REVIEWING = "코드 분석 중"
    COMPOSING = "리뷰 작성 중"
    DONE = "완료"
    FAILED = "실패"


@dataclass
class WorkUnit:
    """리뷰 작업 단위 하나(파일). 시작 전에 diff에서 전부 확정된다."""

    key: str
    weight: int
    """변경 라인 수. 진행 분모의 가중치."""
    hunks: int = 0
    done: bool = False
    findings: int = 0
    skipped_reason: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.skipped_reason)


@dataclass
class ProgressState:
    stage: Stage = Stage.QUEUED
    units: list[WorkUnit] = field(default_factory=list)
    active_key: str = ""
    note: str = ""

    @property
    def total_weight(self) -> int:
        return sum(u.weight for u in self.units)

    @property
    def done_weight(self) -> int:
        return sum(u.weight for u in self.units if u.done)

    @property
    def total_units(self) -> int:
        return len(self.units)

    @property
    def done_units(self) -> int:
        return sum(1 for u in self.units if u.done)

    @property
    def total_hunks(self) -> int:
        return sum(u.hunks for u in self.units)

    @property
    def total_findings(self) -> int:
        return sum(u.findings for u in self.units)


class ProgressReporter:
    """코멘트 하나를 만들고 계속 PATCH 해서 덮어쓴다.

    새 코멘트를 쌓으면 PR 대화가 오염되므로 반드시 같은 코멘트를 갱신한다.
    """

    def __init__(
        self,
        gh: GitHubClient,
        owner: str,
        repo: str,
        *,
        model: str,
        debounce_s: float = 2.0,
    ) -> None:
        self._gh = gh
        self._owner = owner
        self._repo = repo
        self._model = model
        self._debounce_s = debounce_s
        self._comment_id: int | None = None
        self._state = ProgressState()
        self._started = time.monotonic()
        self._last_push = 0.0
        self._dirty = False

    @property
    def comment_id(self) -> int | None:
        return self._comment_id

    @property
    def state(self) -> ProgressState:
        return self._state

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started

    async def start(self, pr_number: int) -> int:
        """웹훅 수신 직후 즉시 호출한다. 아직 분모를 모르는 상태로 시작한다."""
        self._started = time.monotonic()
        self._comment_id = await self._gh.create_issue_comment(
            self._owner, self._repo, pr_number, self._render()
        )
        self._last_push = time.monotonic()
        return self._comment_id

    async def plan(self, units: list[WorkUnit]) -> None:
        """diff에서 뽑은 작업 단위를 등록한다. 여기서 분모가 사실로 확정된다."""
        self._state.units = units
        self._state.stage = Stage.REVIEWING
        self._dirty = True
        await self.flush()

    async def set_stage(self, stage: Stage, note: str = "") -> None:
        self._state.stage = stage
        self._state.note = note
        self._dirty = True
        await self.flush()

    async def begin_unit(self, key: str) -> None:
        """단위 하나를 시작한다. 이 안에서는 진척도를 알 수 없으므로 표시도 하지 않는다."""
        self._state.active_key = key
        self._dirty = True
        await self._maybe_flush()

    async def complete_unit(self, key: str, *, findings: int = 0, skipped: str = "") -> None:
        for unit in self._state.units:
            if unit.key == key:
                unit.done = True
                unit.findings = findings
                unit.skipped_reason = skipped
                break
        if self._state.active_key == key:
            self._state.active_key = ""
        self._dirty = True
        await self._maybe_flush()

    async def _maybe_flush(self) -> None:
        if (time.monotonic() - self._last_push) >= self._debounce_s:
            await self.flush()

    async def flush(self) -> None:
        """보류 중인 상태를 실제로 반영한다."""
        if self._comment_id is None or not self._dirty:
            return
        await self._gh.update_issue_comment(
            self._owner, self._repo, self._comment_id, self._render()
        )
        self._last_push = time.monotonic()
        self._dirty = False

    async def finish(self, body: str) -> None:
        """진행 상태 코멘트를 최종 결과로 교체한다."""
        if self._comment_id is None:
            return
        await self._gh.update_issue_comment(self._owner, self._repo, self._comment_id, body)
        self._dirty = False

    async def fail(self, reason: str) -> None:
        self._state.stage = Stage.FAILED
        self._state.note = reason
        self._dirty = True
        await self.flush()

    # --- 렌더링 ---

    def _render(self) -> str:
        state = self._state
        if state.stage is Stage.FAILED:
            return (
                "### 코드 리뷰 실패\n\n"
                f"{state.note or '알 수 없는 오류'}\n\n"
                f"<sub>경과 {self.elapsed_s:.0f}초 · `{self._model}`</sub>"
            )

        lines = ["### 코드 리뷰", "", self._headline(), ""]
        if state.units:
            lines += self._checklist()
            lines.append("")
        if state.note:
            lines += [state.note, ""]
        lines.append(f"<sub>`{self._model}`</sub>")
        return "\n".join(lines)

    def _headline(self) -> str:
        """분수는 전부 사실이다. 추정치는 쓰지 않는다."""
        state = self._state
        parts = [f"**{state.stage.value}**"]
        if state.units:
            parts.append(f"{state.done_units}/{state.total_units} 파일")
            # 라인 수가 실제 작업량에 훨씬 가깝다
            parts.append(f"{state.done_weight}/{state.total_weight} 라인")
        # 남은 시간은 추정할 수 없다. 경과 시간만 쓴다.
        parts.append(f"경과 {self.elapsed_s:.0f}초")
        return " · ".join(parts)

    def _checklist(self) -> list[str]:
        state = self._state
        rows: list[str] = []

        # 완료 + 현재 진행 중인 것을 우선 보여주고, 나머지는 개수로 접는다
        shown = 0
        pending_overflow = 0
        for unit in state.units:
            is_active = unit.key == state.active_key
            if not unit.done and not is_active and shown >= _MAX_LISTED_UNITS:
                pending_overflow += 1
                continue
            rows.append(self._render_unit(unit, is_active))
            shown += 1

        if pending_overflow:
            rows.append(f"- 외 {pending_overflow}개 파일 대기")
        return rows

    def _render_unit(self, unit: WorkUnit, is_active: bool) -> str:
        if unit.done:
            if unit.skipped:
                return f"- [x] ~~`{unit.key}`~~ — {unit.skipped}"
            note = f"{unit.findings}건" if unit.findings else "지적 없음"
            return f"- [x] `{unit.key}` — {note}"
        if is_active:
            # 이 단위 내부의 진척도는 알 수 없다. 진행 중이라는 사실만 표시한다.
            return f"- [ ] `{unit.key}` ← 분석 중"
        return f"- [ ] `{unit.key}`"
