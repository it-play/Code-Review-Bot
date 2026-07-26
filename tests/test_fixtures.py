"""평가 픽스처의 정답 라벨 정합성 테스트.

라벨이 틀리면 검출률 측정이 조용히 거짓이 된다. 실제로 두 번 틀렸다.
  1) 손으로 센 줄 번호가 빗나가서 결함 줄이 `</div>`와 빈 줄을 가리켰다
  2) 정답 줄을 하나만 두는 바람에, 의존성 배열 누락을 `useEffect(` 줄로 지적한
     멀쩡한 결과가 '놓침'으로 채점됐다
이 테스트가 두 재발을 막는다.
"""

import pytest
from scripts.fixtures import ALL, BUGGY, CLEAN, SAFETY

from crbot.review.diff import commentable_lines, parse_patch

_CLOSING = {"});", "};", "}", ")", ");", "</div>", "}, []);"}


def line_contents(patch: str) -> dict[int, str]:
    return {
        line.new_lineno: line.content.strip()
        for hunk in parse_patch(patch)
        for line in hunk.lines
        if line.new_lineno is not None
    }


@pytest.mark.parametrize("fixture", ALL, ids=lambda f: f.name)
def test_patch_parses_into_hunks(fixture):
    assert parse_patch(fixture.patch), f"{fixture.name}: patch 파싱 실패"


@pytest.mark.parametrize("fixture", BUGGY, ids=lambda f: f.name)
class TestBuggyFixtures:
    def test_all_acceptable_lines_are_inside_the_diff(self, fixture):
        outside = fixture.bug_lines - commentable_lines(fixture.patch)
        assert not outside, (
            f"{fixture.name}: {sorted(outside)}가 diff 밖이다. "
            "코멘트를 달 수 없는 줄을 정답으로 두면 검출률이 항상 0이 된다"
        )

    def test_each_bug_anchors_on_real_code(self, fixture):
        """결함마다 실질적인 코드 줄이 최소 하나는 정답에 있어야 한다.

        닫는 괄호도 타당한 지적 위치일 수 있으므로 '전부 실질적'까지는 요구하지 않는다.
        다만 정답이 전부 닫는 구문뿐이면 라벨을 잘못 잡은 것이다.
        """
        contents = line_contents(fixture.patch)
        for bug in fixture.bugs:
            substantive = [
                lineno
                for lineno in bug.lines
                if len(contents[lineno]) > 3 and contents[lineno] not in _CLOSING
            ]
            assert substantive, (
                f"{fixture.name} / {bug.label}: 정답 줄이 전부 닫는 구문이거나 비어 있다 "
                f"({[contents[n] for n in sorted(bug.lines)]})"
            )

    def test_every_bug_has_keywords(self, fixture):
        for bug in fixture.bugs:
            assert bug.keywords, f"{fixture.name} / {bug.label}: 내용 확인 키워드가 없다"

    def test_keywords_are_lowercase(self, fixture):
        # 채점기가 소문자로 비교하므로 대문자가 섞이면 영원히 안 걸린다
        for bug in fixture.bugs:
            for word in bug.keywords:
                assert word == word.lower(), f"{fixture.name}: {word!r}는 소문자여야 한다"

    def test_is_not_marked_clean(self, fixture):
        assert not fixture.should_be_clean


@pytest.mark.parametrize("fixture", CLEAN, ids=lambda f: f.name)
def test_clean_fixtures_have_no_bugs(fixture):
    # 거짓 양성 측정의 기준선이므로 결함이 하나도 없어야 한다
    assert fixture.should_be_clean
    assert not fixture.bugs
    assert not fixture.bug_lines


def test_suite_covers_both_axes():
    # 검출률만 재고 거짓 양성을 안 재면 "전부 지적하는 봇"이 만점을 받는다
    assert BUGGY, "결함 픽스처가 없다"
    assert CLEAN, "깨끗한 픽스처가 없다 — 거짓 양성을 측정할 수 없다"
    assert SAFETY, "안전성 픽스처가 없다"


def test_bug_labels_are_unique_within_a_fixture():
    for fixture in BUGGY:
        labels = [b.label for b in fixture.bugs]
        assert len(labels) == len(set(labels)), f"{fixture.name}: 결함 라벨이 중복된다"


def test_fixture_names_are_unique():
    names = [f.name for f in ALL]
    assert len(names) == len(set(names))
