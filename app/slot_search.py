"""排班试算：为司机寻找新增驾驶/其他值勤片段的最早可行开始时间（仅依赖标准库）。

API 层负责参数校验、司机存在性检查与响应组装；本模块只负责在**同一份**
现存片段集合上试算：

- 只考察搜索区间内**整分钟**开始点（不足整分钟的起点向上取整）；
  候选片段的结束时刻允许越过搜索区间终点；
- 候选片段不得与现存片段重叠（首尾相接 ``end == start`` 合法）——
  即候选区间必须完整落在现存片段之间的某个空隙内；
- 把候选片段插入后按两条合规规则（滚动 24h 值勤上限、累计驾驶上限）重算，
  仍完全合规才算可行；
- 按开始时间升序扫描，返回首个可行解；无解返回 ``None``。

本模块不触碰数据库：试算不写库，结果也不预留时段。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from .compliance import DUTY_TYPES, Seg, evaluate_sorted, violation_to_dict

# 候选片段在试算中的临时 ID（不写入任何存储；仅当试算违规时才会出现在
# 违规明细里，而违规候选不会被采纳，故不会泄漏到响应中）
TRIAL_SEGMENT_ID = "__slot_trial__"


@dataclass(frozen=True)
class SlotProposal:
    """一个可行解：候选片段的起止时刻与插入后的重算结论（完全合规）。"""

    start: datetime
    end: datetime
    trial_result: dict[str, Any]  # {"compliant": True, "violations": []}


def ceil_minute(dt: datetime) -> datetime:
    """向上取整到整分钟（本身已是整分钟则原样返回）。"""
    base = dt.replace(second=0, microsecond=0)
    return base if base == dt else base + timedelta(minutes=1)


def find_earliest_slot(
    segments: Iterable[Seg],
    seg_type: str,
    duration: timedelta,
    search_start: datetime,
    search_end: datetime,
) -> Optional[SlotProposal]:
    """在 [search_start, search_end) 内寻找最早可行的整分钟开始点。

    ``segments`` 为同一次读取得到的现存片段集合（本函数不修改它）；
    ``seg_type`` 仅支持驾驶/其他值勤；``duration`` 必须为正。
    返回首个可行解，搜索区间内无可行解时返回 ``None``。
    """
    if seg_type not in DUTY_TYPES:
        raise ValueError(f"仅支持驾驶/其他值勤片段试算，收到类型: {seg_type!r}")
    if duration <= timedelta(0):
        raise ValueError("时长必须为正")
    if search_end <= search_start:
        raise ValueError("搜索终点必须晚于起点")

    segs = sorted(segments, key=lambda s: (s.start, s.end, s.segment_id))
    keys = [(s.start, s.end, s.segment_id) for s in segs]

    # 现存片段之间的空隙（含首段之前、末段之后的无界空隙）。
    # 候选区间 [t, t+duration) 可行放置 <=> 完整落在某空隙 [gs, ge) 内：
    # t >= gs 且 t+duration <= ge（端点相接合法）。
    gaps: list[tuple[Optional[datetime], Optional[datetime]]] = []
    prev_end: Optional[datetime] = None
    for s in segs:
        gaps.append((prev_end, s.start))
        prev_end = s.end
    gaps.append((prev_end, None))

    first = ceil_minute(search_start)
    minute = timedelta(minutes=1)
    for gap_start, gap_end in gaps:
        t = first
        if gap_start is not None:
            # 候选开始点不得早于空隙起点（与前一片段相接合法）
            t = max(t, ceil_minute(gap_start))
        # 空隙按时间先后排列，空隙内候选递增 => 全局按开始时间升序
        while t < search_end:
            end = t + duration
            if gap_end is not None and end > gap_end:
                break  # 本空隙剩余候选都放不下，进入下一空隙
            trial = Seg(TRIAL_SEGMENT_ID, t, end, seg_type)
            i = bisect_left(keys, (t, end, TRIAL_SEGMENT_ID))
            merged = segs[:i] + [trial] + segs[i:]
            violations = evaluate_sorted(merged)
            if not violations:
                return SlotProposal(start=t, end=end, trial_result={
                    "compliant": True,
                    "violations": [violation_to_dict(v) for v in violations],
                })
            t += minute
    return None
