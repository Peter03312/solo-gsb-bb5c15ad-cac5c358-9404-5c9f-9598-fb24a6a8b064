"""排班试算纯逻辑单元测试：仅依赖标准库（与合规引擎测试相同）。

可用 `python3 -m unittest tests.test_slot_search` 或 pytest 运行。
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.compliance import Seg
from app.slot_search import ceil_minute, find_earliest_slot

BASE = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def t(h, m=0, s=0, day=0):
    return BASE + timedelta(days=day, hours=h, minutes=m, seconds=s)


def seg(sid, sh, eh, typ="DRIVING", day=0, sm=0, em=0, es=0):
    return Seg(sid, t(sh, sm, day=day), t(eh, em, es, day=day), typ)


def search(segs, typ, minutes, start, end):
    return find_earliest_slot(segs, typ, timedelta(minutes=minutes), start, end)


class CeilMinuteTest(unittest.TestCase):
    def test_exact_minute_unchanged(self):
        self.assertEqual(ceil_minute(t(8)), t(8))

    def test_seconds_round_up(self):
        self.assertEqual(ceil_minute(t(8, 0, 1)), t(8, 1))

    def test_59_seconds_round_up(self):
        self.assertEqual(ceil_minute(t(8, 30, 59)), t(8, 31))


class BasicPlacementTest(unittest.TestCase):
    def test_no_segments_starts_at_search_start(self):
        p = search([], "DRIVING", 60, t(8), t(20))
        self.assertIsNotNone(p)
        self.assertEqual((p.start, p.end), (t(8), t(9)))
        self.assertEqual(p.trial_result, {"compliant": True, "violations": []})

    def test_search_start_ceiled_to_whole_minute(self):
        # 起点不足整分钟：候选从下一整分钟开始
        p = search([], "DRIVING", 30, t(8, 0, 45), t(20))
        self.assertEqual((p.start, p.end), (t(8, 1), t(8, 31)))

    def test_end_may_pass_search_end(self):
        # 结束可越过区间终点：18:00 起 4h，22:00 结束 > 20:00 仍可行
        p = search([], "DRIVING", 240, t(18), t(20))
        self.assertEqual((p.start, p.end), (t(18), t(22)))

    def test_overlapping_candidates_skipped_touching_allowed(self):
        # 现存 08:00-10:00；搜索从 08:30 起，唯一可行是与它相接的 10:00
        p = search([seg("A", 8, 10, "OTHER_DUTY")], "DRIVING", 60, t(8, 30), t(12))
        self.assertEqual((p.start, p.end), (t(10), t(11)))

    def test_exact_fit_in_gap_touching_both_sides(self):
        # 两段休息之间 10:00-12:00 的空隙恰好放下 2h（两端相接合法）
        segs = [seg("R1", 8, 10, "REST"), seg("R2", 12, 14, "REST")]
        p = search(segs, "DRIVING", 120, t(9), t(20))
        self.assertEqual((p.start, p.end), (t(10), t(12)))

    def test_gap_start_ceiled_to_whole_minute(self):
        # 空隙从 10:00:30 开始：候选开始点向上取整到 10:01
        segs = [Seg("R1", t(8), t(10, 0, 30), "REST")]
        p = search(segs, "DRIVING", 30, t(9), t(20))
        self.assertEqual((p.start, p.end), (t(10, 1), t(10, 31)))

    def test_start_must_be_within_search_window(self):
        # 空隙在 20:00 之后；搜索终点 20:00 => 开始点 20:00 不在区间内，无解
        segs = [seg("R1", 8, 20, "REST")]
        self.assertIsNone(search(segs, "DRIVING", 30, t(19), t(20)))
        # 终点放宽到 20:01 后，20:00 成为可行开始点（结束越过终点）
        p = search(segs, "DRIVING", 30, t(19), t(20, 1))
        self.assertEqual((p.start, p.end), (t(20), t(20, 30)))

    def test_window_fully_occupied_no_solution(self):
        segs = [Seg("R1", t(0), t(0, day=1), "REST")]
        self.assertIsNone(search(segs, "DRIVING", 30, t(6), t(18)))

    def test_duration_longer_than_gap_uses_next_gap(self):
        segs = [seg("R1", 8, 10, "REST"), seg("R2", 11, 12, "REST")]
        # 08 点前空隙与 10-11 点空隙都放不下 90 分钟，落到 12 点之后
        p = search(segs, "DRIVING", 90, t(7), t(20))
        self.assertEqual((p.start, p.end), (t(12), t(13, 30)))

    def test_input_segments_not_mutated(self):
        segs = [seg("A", 8, 10, "OTHER_DUTY")]
        snapshot = list(segs)
        search(segs, "DRIVING", 60, t(8, 30), t(12))
        self.assertEqual(segs, snapshot)


class ComplianceInteractionTest(unittest.TestCase):
    def test_rolling_24h_pushes_slot_until_old_duty_slides_out(self):
        # 现存其他值勤 9h（D0 08:00-17:00）；新增 2h 会使滚动 24h 合计 >10h，
        # 直到 D1 09:00 旧值勤滑出到 8h：最早可行 D1 07:00-09:00（恰好 10h 合规）
        segs = [seg("A", 8, 17, "OTHER_DUTY")]
        p = search(segs, "OTHER_DUTY", 120, t(18), t(18, day=2))
        self.assertEqual((p.start, p.end), (t(7, day=1), t(9, day=1)))

    def test_driving_stint_without_reset_no_solution(self):
        # 已驾驶 2h 且无合格休息：再开 3h 累计 5h 必越限（无时间衰减）=> 无解
        segs = [seg("A", 8, 10)]
        self.assertIsNone(search(segs, "DRIVING", 180, t(10), t(20)))

    def test_qualifying_rest_unlocks_slot(self):
        # 30 分钟合格休息清零重计：休息结束后即可安排 3h 驾驶
        segs = [seg("A", 8, 10), seg("R", 10, 10, "REST", em=30)]
        p = search(segs, "DRIVING", 180, t(10), t(20))
        self.assertEqual((p.start, p.end), (t(10, 30), t(13, 30)))

    def test_trial_can_break_later_existing_segment(self):
        # 现存驾驶 1h + 3h（合计恰好 4h 合规）；插入 30 分钟驾驶会使
        # 后面的片段累计越限 => 区间内无解
        segs = [seg("A", 8, 9), seg("B", 14, 17)]
        self.assertIsNone(search(segs, "DRIVING", 30, t(9), t(13)))

    def test_rest_between_trial_and_later_segment_allows_insertion(self):
        # 同上，但 12:00-12:30 有合格休息：试算片段 09:00-09:30 可行
        segs = [seg("A", 8, 9), seg("R", 12, 12, "REST", em=30), seg("B", 14, 17)]
        p = search(segs, "DRIVING", 30, t(9), t(13))
        self.assertEqual((p.start, p.end), (t(9), t(9, 30)))

    def test_first_feasible_in_ascending_order(self):
        # 多个可行解时返回最早者：08:00-10:00 被占，09:00 前与 10:00 后都可行
        segs = [seg("A", 8, 10, "OTHER_DUTY")]
        p = search(segs, "DRIVING", 30, t(7), t(20))
        self.assertEqual((p.start, p.end), (t(7), t(7, 30)))


class ValidationTest(unittest.TestCase):
    def test_rest_type_rejected(self):
        with self.assertRaises(ValueError):
            search([], "REST", 60, t(8), t(20))

    def test_non_positive_duration_rejected(self):
        with self.assertRaises(ValueError):
            search([], "DRIVING", 0, t(8), t(20))
        with self.assertRaises(ValueError):
            search([], "DRIVING", -30, t(8), t(20))

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            search([], "DRIVING", 60, t(20), t(8))
        with self.assertRaises(ValueError):
            search([], "DRIVING", 60, t(8), t(8))


if __name__ == "__main__":
    unittest.main()
