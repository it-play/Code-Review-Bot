"""테스트 공용 픽스처.

GitHub API를 스텁으로 대체해서 네트워크 없이 진행 상태와 엔진 흐름을 전부 검증한다.
"""

import pytest

from crbot.github.client import PullRequest, PullRequestFile
from crbot.github.progress import ProgressReporter


class FakeGitHub:
    """create/update 호출을 기록하는 스텁."""

    def __init__(self) -> None:
        self.bodies: list[str] = []
        self.update_count = 0

    async def create_issue_comment(self, owner, repo, number, body) -> int:
        self.bodies.append(body)
        return 1234

    async def update_issue_comment(self, owner, repo, comment_id, body) -> None:
        self.bodies.append(body)
        self.update_count += 1


def make_file(
    name: str = "src/a.ts",
    additions: int = 2,
    deletions: int = 0,
    patch: str | None = "@@ -1 +1 @@\n+x",
    status: str = "modified",
) -> PullRequestFile:
    return PullRequestFile(
        filename=name, status=status, additions=additions, deletions=deletions, patch=patch
    )


@pytest.fixture
def gh() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
async def reporter(gh: FakeGitHub) -> ProgressReporter:
    # debounce 0 — 테스트에서는 모든 갱신을 즉시 반영시켜 렌더링을 관찰한다
    reporter = ProgressReporter(gh, "GSMSV", "frontend", model="gemma4:26b", debounce_s=0.0)
    await reporter.start(42)
    return reporter


@pytest.fixture
def pull_request() -> PullRequest:
    return PullRequest(
        number=42, title="t", body="", head_sha="abc123", base_sha="def456", draft=False
    )
