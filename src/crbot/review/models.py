"""리뷰 도메인 타입과 작업 단위 산출.

`build_work_units`는 순수 함수로 둔다. 진행 상태의 분모가 실제 diff와 일치하는지를
네트워크 없이 검증해야 하기 때문이다 (PLAN.md L2 — "지어낸 값이 섞이면 실패로 본다").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from crbot.github.client import PullRequestFile
from crbot.github.progress import WorkUnit

# 리뷰해도 얻을 게 없는 파일들. 진행 표시에는 남기되 건너뛴 이유를 붙인다.
_SKIP_SUFFIXES = (
    ".lock",
    ".snap",
    ".min.js",
    ".map",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".pdf",
)
_SKIP_NAMES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
    }
)


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def label(self) -> str:
        return {"high": "심각", "medium": "보통", "low": "사소"}[self.value]

    @property
    def alert(self) -> str:
        """GitHub alert 종류.

        GitHub은 `> [!TYPE]` 블록을 아이콘과 색으로 렌더링한다. 심각도를 글자로만
        쓰면 코멘트가 줄줄이 달렸을 때 뭐가 급한지 한눈에 안 들어온다.

        CAUTION(빨강) / WARNING(주황) / NOTE(파랑) 순으로 눈에 띈다.
        """
        return {"high": "CAUTION", "medium": "WARNING", "low": "NOTE"}[self.value]


@dataclass
class Finding:
    """리뷰 지적 하나."""

    path: str
    line: int
    severity: Severity
    title: str
    body: str

    def render(self) -> str:
        """GitHub alert 블록으로 렌더링한다.

        alert 안의 모든 줄은 `>` 로 시작해야 한다. 한 줄이라도 빠지면 블록이
        거기서 끊기고 나머지가 평문으로 새어 나온다.
        """
        lines = [f"> [!{self.severity.alert}]", f"> **{self.title}**"]
        if self.body:
            lines.append(">")
            lines += [f"> {line}" if line.strip() else ">" for line in self.body.splitlines()]
        return "\n".join(lines)


@dataclass
class ReviewResult:
    findings: list[Finding]
    summary: str = ""
    fallback_mode: bool = False
    """변경 규모가 커서 요약 리뷰로 폴백했는지."""

    skipped: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []


def skip_reason(file: PullRequestFile) -> str:
    """리뷰 대상에서 제외할 이유. 없으면 빈 문자열."""
    name = file.filename.rsplit("/", 1)[-1]
    if name in _SKIP_NAMES:
        return "잠금 파일"
    if file.filename.endswith(_SKIP_SUFFIXES):
        return "리뷰 대상 아님"
    if file.status == "removed":
        return "삭제됨"
    if not file.patch:
        # GitHub은 바이너리이거나 diff가 너무 크면 patch를 주지 않는다
        return "diff 없음"
    return ""


def build_work_units(files: list[PullRequestFile]) -> list[WorkUnit]:
    """diff에서 작업 단위를 뽑는다. 여기서 진행 상태의 분모가 사실로 확정된다.

    가중치는 파일 개수가 아니라 변경 라인 수다. 3라인짜리 파일과 200라인짜리 파일이
    같은 한 칸을 차지하면 표시가 체감과 어긋난다.
    """
    units: list[WorkUnit] = []
    for file in files:
        reason = skip_reason(file)
        units.append(
            WorkUnit(
                key=file.filename,
                # 건너뛸 파일은 분모에 넣지 않는다. lockfile 5천 라인이 분모를
                # 지배하면 남은 분수가 의미를 잃는다.
                weight=0 if reason else max(file.changed_lines, 1),
                hunks=0 if reason else count_hunks(file.patch or ""),
                skipped_reason=reason,
            )
        )
    return units


def count_hunks(patch: str) -> int:
    """unified diff의 hunk 개수. 파일보다 세밀한 작업 단위."""
    return sum(1 for line in patch.splitlines() if line.startswith("@@"))
