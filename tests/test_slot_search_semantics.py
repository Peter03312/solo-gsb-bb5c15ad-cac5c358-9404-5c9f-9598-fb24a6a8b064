"""排班试算端到端语义参考实现（不依赖 FastAPI/SQLAlchemy，仅用标准库）。

在无法安装 Web 依赖的环境中，用内存存储复刻 app/api.py 中
``POST /drivers/{driver_id}/slot-search`` 处理器的判定顺序，覆盖需求中的关键场景：

- 司机不存在 -> 404 driver_not_found；
- 时长非正 / 起止无效 / 缺少时区 / 跨度超 7 天 / 类型非驾驶或其他值勤 -> 422；
- 现存记录已违规 -> 200 NOT_RECOMMENDABLE，附当前违规明细，不给候选；
- 搜索区间无解 -> 200 NO_FEASIBLE_SLOT；
- 找到最早可行解 -> 200 FOUND，附起止、试算结论与所用片段 (segment_id, revision)；
- 只读语义：任何分支都不写库（片段集合与修订号不变）、不预留时段。
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.acknowledgment import revision_set
from app.compliance import Seg, evaluate_driver
from app.slot_search import find_earliest_slot

BASE = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
MAX_SPAN = timedelta(days=7)


def t(h, m=0, day=0):
    return BASE + timedelta(days=day, hours=h, minutes=m)


class HttpError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class MemDB:
    """内存表（对应 drivers / segments 两表；本端点不做任何写入）。"""

    def __init__(self):
        self.drivers: dict[str, dict] = {}
        self.segments: dict[str, dict] = {}

    def add_driver(self, d):
        self.drivers[d] = {"driver_id": d, "name": d}

    def add_segment(self, sid, driver, sh, eh, typ="DRIVING", day=0, sm=0, em=0):
        self.segments[sid] = {
            "segment_id": sid,
            "driver_id": driver,
            "start": t(sh, sm, day=day),
            "end": t(eh, em, day=day),
            "type": typ,
            "revision": 1,
        }

    def snapshot(self):
        return {
            sid: (s["driver_id"], s["start"], s["end"], s["type"], s["revision"])
            for sid, s in self.segments.items()
        }


def slot_search_endpoint(db: MemDB, driver_id: str, body: dict) -> dict:
    """复刻 api.search_earliest_slot 的判定顺序（pydantic 形状校验 + 处理器）。"""
    # ---- 对应 schemas.SlotSearchCreate 的入参校验（失败 -> 422）
    seg_type = body.get("type")
    if seg_type not in ("DRIVING", "OTHER_DUTY"):
        raise HttpError(422, {"error": "validation", "message": "type 仅支持 DRIVING / OTHER_DUTY"})
    duration = body.get("duration_minutes")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise HttpError(422, {"error": "validation", "message": "duration_minutes 必须为正整数"})
    start = body.get("search_start_utc")
    end = body.get("search_end_utc")
    for v in (start, end):
        if not isinstance(v, datetime) or v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise HttpError(422, {"error": "validation", "message": "时间必须携带时区"})
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start:
        raise HttpError(422, {"error": "validation", "message": "search_end_utc 必须晚于 search_start_utc"})
    if end - start > MAX_SPAN:
        raise HttpError(422, {"error": "validation", "message": "搜索跨度不得超过 7 天"})

    # ---- 对应处理器：司机存在性 -> 404
    if driver_id not in db.drivers:
        raise HttpError(404, {"error": "driver_not_found", "driver_id": driver_id})

    # 同一次读取的现存片段集合：当前结论与全部候选试算都基于它
    segs = sorted(
        (s for s in db.segments.values() if s["driver_id"] == driver_id),
        key=lambda s: (s["start"], s["end"], s["segment_id"]),
    )
    views = [Seg(s["segment_id"], s["start"], s["end"], s["type"]) for s in segs]
    current = evaluate_driver(views)
    basis = revision_set((s["segment_id"], s["revision"]) for s in segs)

    status = "FOUND"
    proposal = None
    if not current["compliant"]:
        status = "NOT_RECOMMENDABLE"  # 现存记录已违规：不可推荐，不做候选试算
    else:
        found = find_earliest_slot(views, seg_type, timedelta(minutes=duration), start, end)
        if found is None:
            status = "NO_FEASIBLE_SLOT"
        else:
            proposal = {
                "start_utc": found.start,
                "end_utc": found.end,
                "trial_compliance": found.trial_result,
            }
    return {
        "driver_id": driver_id,
        "status": status,
        "type": seg_type,
        "duration_minutes": duration,
        "search_start_utc": start,
        "search_end_utc": end,
        "proposal": proposal,
        "current_compliance": current,
        "basis_segments": basis,
    }


def body(typ="DRIVING", minutes=60, start=None, end=None):
    return {
        "type": typ,
        "duration_minutes": minutes,
        "search_start_utc": start if start is not None else t(8),
        "search_end_utc": end if end is not None else t(20),
    }


class RejectionTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")

    def test_driver_not_found_404(self):
        with self.assertRaises(HttpError) as ctx:
            slot_search_endpoint(self.db, "D-NOPE", body())
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail["error"], "driver_not_found")

    def test_non_positive_duration_422(self):
        for bad in (0, -30):
            with self.assertRaises(HttpError) as ctx:
                slot_search_endpoint(self.db, "D1", body(minutes=bad))
            self.assertEqual(ctx.exception.status_code, 422)

    def test_naive_datetime_422(self):
        naive = datetime(2026, 10, 5, 8, 0)  # 缺少时区
        with self.assertRaises(HttpError) as ctx:
            slot_search_endpoint(self.db, "D1", body(start=naive))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_end_not_after_start_422(self):
        with self.assertRaises(HttpError) as ctx:
            slot_search_endpoint(self.db, "D1", body(start=t(20), end=t(8)))
        self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(HttpError) as ctx:
            slot_search_endpoint(self.db, "D1", body(start=t(8), end=t(8)))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_span_over_7_days_422(self):
        with self.assertRaises(HttpError) as ctx:
            slot_search_endpoint(self.db, "D1", body(start=t(0), end=t(0, 1, day=7)))
        self.assertEqual(ctx.exception.status_code, 422)
        # 恰好 7 天合法
        resp = slot_search_endpoint(self.db, "D1", body(start=t(0), end=t(0, day=7)))
        self.assertEqual(resp["status"], "FOUND")

    def test_rest_type_422(self):
        with self.assertRaises(HttpError) as ctx:
            slot_search_endpoint(self.db, "D1", body(typ="REST"))
        self.assertEqual(ctx.exception.status_code, 422)


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.db = MemDB()
        self.db.add_driver("D1")

    def test_found_returns_proposal_and_basis(self):
        self.db.add_segment("SEG-A", "D1", 8, 10, "OTHER_DUTY")
        self.db.segments["SEG-A"]["revision"] = 3  # 模拟经历过更正
        before = self.db.snapshot()
        resp = slot_search_endpoint(self.db, "D1", body(minutes=60, start=t(8, 30), end=t(12)))
        self.assertEqual(resp["status"], "FOUND")
        self.assertEqual(
            (resp["proposal"]["start_utc"], resp["proposal"]["end_utc"]), (t(10), t(11))
        )
        self.assertEqual(resp["proposal"]["trial_compliance"], {"compliant": True, "violations": []})
        self.assertTrue(resp["current_compliance"]["compliant"])
        # 所用片段 ID 与修订号（按片段 ID 排序）
        self.assertEqual(resp["basis_segments"], [{"segment_id": "SEG-A", "revision": 3}])
        # 只读：片段集合与修订号不变（不写库、不预留时段）
        self.assertEqual(self.db.snapshot(), before)

    def test_not_recommendable_when_currently_violating(self):
        # 现存记录已违规：驾驶 2h + 3h 无合格休息，累计 5h 越限
        self.db.add_segment("SEG-A", "D1", 8, 10)
        self.db.add_segment("SEG-B", "D1", 10, 13)
        before = self.db.snapshot()
        resp = slot_search_endpoint(self.db, "D1", body(minutes=30, start=t(13), end=t(20)))
        self.assertEqual(resp["status"], "NOT_RECOMMENDABLE")
        self.assertIsNone(resp["proposal"])
        current = resp["current_compliance"]
        self.assertFalse(current["compliant"])
        self.assertEqual(
            [v["rule"] for v in current["violations"]], ["CONTINUOUS_DRIVING_LIMIT"]
        )
        self.assertEqual(
            resp["basis_segments"],
            [{"segment_id": "SEG-A", "revision": 1}, {"segment_id": "SEG-B", "revision": 1}],
        )
        self.assertEqual(self.db.snapshot(), before)  # 不写库

    def test_no_feasible_slot(self):
        # 空隙都在搜索区间外：08:00-20:00 被休息占满
        self.db.add_segment("SEG-R", "D1", 8, 20, "REST")
        before = self.db.snapshot()
        resp = slot_search_endpoint(self.db, "D1", body(minutes=30, start=t(9), end=t(19)))
        self.assertEqual(resp["status"], "NO_FEASIBLE_SLOT")
        self.assertIsNone(resp["proposal"])
        self.assertTrue(resp["current_compliance"]["compliant"])
        self.assertEqual(self.db.snapshot(), before)  # 不写库

    def test_no_feasible_slot_due_to_compliance(self):
        # 已驾驶 2h 且无合格休息：再开 3h 必越限 => 无解
        self.db.add_segment("SEG-A", "D1", 8, 10)
        resp = slot_search_endpoint(self.db, "D1", body(minutes=180, start=t(10), end=t(20)))
        self.assertEqual(resp["status"], "NO_FEASIBLE_SLOT")

    def test_other_duty_type_supported(self):
        resp = slot_search_endpoint(self.db, "D1", body(typ="OTHER_DUTY", minutes=90))
        self.assertEqual(resp["status"], "FOUND")
        self.assertEqual(resp["type"], "OTHER_DUTY")
        self.assertEqual(
            (resp["proposal"]["start_utc"], resp["proposal"]["end_utc"]), (t(8), t(9, 30))
        )


if __name__ == "__main__":
    unittest.main()
