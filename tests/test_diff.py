"""diff 파서 테스트 (PLAN.md L1 최우선).

줄 번호가 하나라도 틀리면 GitHub이 리뷰 전체를 422로 거절한다.
"""

from crbot.review.diff import (
    commentable_lines,
    parse_patch,
    snap_to_commentable,
)
from crbot.review.models import count_hunks

SIMPLE_PATCH = """@@ -1,4 +1,6 @@
 import { useState } from "react";
+import { useEffect } from "react";

 export function useAuth() {
-  const [user, setUser] = useState(null);
+  const [user, setUser] = useState<User | null>(null);
+  const [loading, setLoading] = useState(true);"""


def test_line_numbers_track_head_side():
    hunks = parse_patch(SIMPLE_PATCH)
    assert len(hunks) == 1
    hunk = hunks[0]
    assert (hunk.old_start, hunk.old_count) == (1, 4)
    assert (hunk.new_start, hunk.new_count) == (1, 6)

    numbered = [(line.kind, line.new_lineno) for line in hunk.lines]
    assert numbered == [
        ("context", 1),
        ("add", 2),
        ("context", 3),
        ("context", 4),
        ("del", None),  # 삭제된 줄은 head에 없다
        ("add", 5),
        ("add", 6),
    ]


def test_deleted_lines_are_not_commentable():
    assert commentable_lines(SIMPLE_PATCH) == {1, 2, 3, 4, 5, 6}


def test_added_lines_only():
    hunk = parse_patch(SIMPLE_PATCH)[0]
    assert [line.new_lineno for line in hunk.added_lines] == [2, 5, 6]


MULTI_HUNK_PATCH = """@@ -10,3 +10,4 @@ function a() {
 const x = 1;
+const y = 2;
 return x;
@@ -50,2 +51,3 @@ function b() {
 const z = 3;
+const w = 4;"""


def test_multiple_hunks_keep_independent_offsets():
    hunks = parse_patch(MULTI_HUNK_PATCH)
    assert len(hunks) == 2
    assert [line.new_lineno for line in hunks[0].added_lines] == [11]
    assert [line.new_lineno for line in hunks[1].added_lines] == [52]
    assert count_hunks(MULTI_HUNK_PATCH) == 2


NEW_FILE_PATCH = """@@ -0,0 +1,3 @@
+export const a = 1;
+export const b = 2;
+export const c = 3;"""


def test_new_file_starts_at_line_one():
    assert commentable_lines(NEW_FILE_PATCH) == {1, 2, 3}


NO_NEWLINE_PATCH = """@@ -1,2 +1,2 @@
-const a = 1;
\\ No newline at end of file
+const a = 2;
\\ No newline at end of file"""


def test_no_newline_marker_does_not_consume_a_line():
    hunk = parse_patch(NO_NEWLINE_PATCH)[0]
    assert [line.new_lineno for line in hunk.added_lines] == [1]


def test_single_line_hunk_header_without_count():
    # @@ -5 +5 @@ 형태 — count가 생략되면 1로 본다
    hunks = parse_patch("@@ -5 +5 @@\n-old\n+new")
    assert (hunks[0].old_count, hunks[0].new_count) == (1, 1)
    assert [line.new_lineno for line in hunks[0].added_lines] == [5]


def test_empty_context_line_without_leading_space():
    # 일부 도구는 빈 문맥 줄을 공백 없이 내보낸다
    hunks = parse_patch("@@ -1,3 +1,3 @@\n const a = 1;\n\n+const b = 2;")
    assert [line.new_lineno for line in hunks[0].added_lines] == [3]


class TestSnapToCommentable:
    allowed = {10, 11, 12, 20}

    def test_exact_hit_is_kept(self):
        assert snap_to_commentable(11, self.allowed) == 11

    def test_near_miss_snaps_to_nearest(self):
        assert snap_to_commentable(13, self.allowed) == 12

    def test_far_miss_is_dropped(self):
        # 엉뚱한 위치에 달린 코멘트는 없는 것만 못하다
        assert snap_to_commentable(50, self.allowed) is None

    def test_empty_allowed_set_drops(self):
        assert snap_to_commentable(5, set()) is None
