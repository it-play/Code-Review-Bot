"""unified diff 파서.

이 모듈이 틀리면 GitHub이 리뷰 전체를 422로 거절한다. 리뷰 코멘트의 `line`은
diff 안의 위치가 아니라 **파일(head 기준)의 실제 줄 번호**를 가리켜야 하고,
그 줄은 반드시 diff에 포함된 줄이어야 한다. 그래서 여기가 L1 테스트 최우선 대상이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# @@ -12,7 +12,9 @@ export function foo() {
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class DiffLine:
    content: str
    kind: str
    """'add' | 'del' | 'context'"""
    new_lineno: int | None
    """head 기준 줄 번호. 삭제된 줄은 None."""
    old_lineno: int | None

    @property
    def is_add(self) -> bool:
        return self.kind == "add"


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str = ""
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added_lines(self) -> list[DiffLine]:
        return [line for line in self.lines if line.is_add]

    @property
    def commentable_lines(self) -> set[int]:
        """리뷰 코멘트를 달 수 있는 head 기준 줄 번호.

        추가된 줄과 문맥 줄만 해당한다. 삭제된 줄은 head에 존재하지 않으므로
        RIGHT side로 코멘트를 달 수 없다.
        """
        return {line.new_lineno for line in self.lines if line.new_lineno is not None}

    def render(self, *, with_lineno: bool = True) -> str:
        """LLM에 넣을 형태로 렌더링한다.

        줄 번호를 붙이는 이유: 모델이 지적 위치를 숫자로 지목하게 해야
        코멘트를 정확한 줄에 달 수 있다. 안 붙이면 모델이 위치를 지어낸다.
        """
        out = [f"@@ {self.header}".rstrip()]
        for line in self.lines:
            prefix = {"add": "+", "del": "-", "context": " "}[line.kind]
            if with_lineno and line.new_lineno is not None:
                out.append(f"{line.new_lineno:>5} {prefix}{line.content}")
            else:
                out.append(f"      {prefix}{line.content}")
        return "\n".join(out)


def parse_patch(patch: str) -> list[Hunk]:
    """GitHub이 주는 파일별 patch 문자열을 hunk 목록으로 파싱한다."""
    hunks: list[Hunk] = []
    current: Hunk | None = None
    old_no = new_no = 0

    for raw in patch.splitlines():
        match = _HUNK_RE.match(raw)
        if match:
            old_start = int(match.group(1))
            old_count = int(match.group(2) or 1)
            new_start = int(match.group(3))
            new_count = int(match.group(4) or 1)
            current = Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                header=raw[2:].strip() if raw.startswith("@@") else raw,
            )
            hunks.append(current)
            old_no, new_no = old_start, new_start
            continue

        if current is None:
            continue

        if raw.startswith("+"):
            current.lines.append(DiffLine(raw[1:], "add", new_no, None))
            new_no += 1
        elif raw.startswith("-"):
            current.lines.append(DiffLine(raw[1:], "del", None, old_no))
            old_no += 1
        elif raw.startswith("\\"):
            # "\ No newline at end of file" — 줄 번호를 소비하지 않는다
            continue
        else:
            # 문맥 줄. 앞의 공백 한 칸이 관례지만 빈 줄로 올 때도 있다.
            content = raw[1:] if raw.startswith(" ") else raw
            current.lines.append(DiffLine(content, "context", new_no, old_no))
            old_no += 1
            new_no += 1

    return hunks


def commentable_lines(patch: str) -> set[int]:
    """이 파일에서 리뷰 코멘트를 달 수 있는 head 기준 줄 번호 전체."""
    lines: set[int] = set()
    for hunk in parse_patch(patch):
        lines |= hunk.commentable_lines
    return lines


def snap_to_commentable(line: int, allowed: set[int]) -> int | None:
    """모델이 지목한 줄을 실제로 코멘트 가능한 줄로 보정한다.

    모델은 diff에 없는 줄을 지목하기도 한다. 그대로 올리면 리뷰 전체가 422로 거절되므로,
    가장 가까운 유효한 줄로 당긴다. 너무 멀면(±3줄 초과) 지적을 버린다 —
    엉뚱한 위치에 달린 코멘트는 없는 것만 못하다.
    """
    if not allowed:
        return None
    if line in allowed:
        return line
    nearest = min(allowed, key=lambda candidate: (abs(candidate - line), candidate))
    return nearest if abs(nearest - line) <= 3 else None
