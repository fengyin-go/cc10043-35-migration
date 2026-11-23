"""检查点时间字段的领域时间模型。

迁移把历史上混在一个文本字段里的「创建时间」拆成两类可解释的时间点：

- ``instant`` 精度：带时区/偏移的 ISO-8601 时间，对应一个确定的实际时刻；
- ``day`` 精度：只有日期的老记录，只知道发生在某一自然日，时刻未知。

关键约定（对应迁移需求）：

1. 新值按实际时刻比较；同一自然日里一个日粒度值和一个 instant 无法判断先后，
   比较结果显式返回 ``same_day_unknown``，绝不把日期偷偷当成零点去比较。
2. 日粒度值保留「只有日期」这一事实，用 :class:`TimePoint.precision` 区分，
   不提升精度。
3. 无法解读的历史值由 ``parse_time_point`` 返回解析错误，交由迁移流程隔离核查，
   不会被静默当成任意时刻。
4. 30 天清退按同一套时间依据判定，并把「横跨清退边界」的日粒度记录识别为
   不确定、需要人工复核，而不是直接清退或直接保留。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

Precision = Literal["instant", "day"]
CompareResult = Literal["before", "after", "equal", "same_day_unknown", "incomparable"]
Decision = Literal["expire", "keep", "review"]

# 仅允许纯日期：YYYY-MM-DD（带分隔符的完整形式）。
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
EXPIRE_DAYS = 30


@dataclass(frozen=True)
class TimePoint:
    """可解释的检查点时间点。

    ``instant`` 永远是带 UTC 时区的确定时刻：

    - instant 精度：就是原始时刻换算到 UTC 的结果；
    - day 精度：取该自然日 UTC 零点，仅作存储/排序锚点，真实时刻未知，
      任何判断都必须结合 ``precision == "day"`` 一起解释。
    """

    instant: datetime
    precision: Precision
    raw: str
    tz_offset: str | None = None

    @property
    def day_start(self) -> datetime:
        """该时间点所在自然日（UTC）的零点。"""
        return self.instant.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)

    @property
    def day_end(self) -> datetime:
        """该时间点所在自然日（UTC）的结束边界（次日零点，开区间）。"""
        return self.day_start + timedelta(days=1)

    def is_day_precision(self) -> bool:
        return self.precision == "day"

    def iso_text(self) -> str:
        """规范化后的存储文本。日粒度仍输出纯日期，保留精度事实。"""
        if self.precision == "day":
            return self.day_start.date().isoformat()
        return self.instant.astimezone(timezone.utc).isoformat()


def parse_time_point(value: Any) -> tuple[TimePoint | None, str | None]:
    """解析单个历史创建时间文本。

    返回 ``(time_point, error)``：

    - 成功：``(TimePoint, None)``；
    - 无法解读：``(None, 原因)``，调用方负责隔离核查。

    带时间的文本必须携带时区/偏移（``Z`` 或 ``±HH:MM``）；裸时间（naive）
    无法定位实际时刻，按坏值隔离，而不是擅自套用本地时区。
    """
    if value is None:
        return None, "空值无法解读为时间"
    text = str(value).strip()
    if not text:
        return None, "空值无法解读为时间"

    match = _DATE_RE.match(text)
    if match:
        try:
            parsed_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError as exc:
            return None, f"非法日期: {exc}"
        return TimePoint(
            instant=datetime.combine(parsed_date, datetime.min.time(), tzinfo=timezone.utc),
            precision="day",
            raw=text,
        ), None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, f"无法解析为ISO时间: {text!r}"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, f"带时间的值缺少时区/偏移，无法定位实际时刻: {text!r}"

    offset = parsed.utcoffset()
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        total_minutes = abs(total_minutes)
        tz_offset = f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"
    else:
        tz_offset = None
    return TimePoint(
        instant=parsed.astimezone(timezone.utc),
        precision="instant",
        raw=text,
        tz_offset=tz_offset,
    ), None


def compare_points(left: TimePoint, right: TimePoint) -> CompareResult:
    """按实际时刻比较两个时间点。

    - 两个 instant：严格按 UTC 时刻比较；
    - 涉及日粒度：若另一时刻落在该自然日内，先后不可判定，返回
      ``same_day_unknown``（不把日期当成零点）；落在自然日之前/之后才可判定；
    - 两个日粒度且同一天：同样不可判定。
    """
    left_utc = left.instant.astimezone(timezone.utc)
    right_utc = right.instant.astimezone(timezone.utc)

    if left.precision == "instant" and right.precision == "instant":
        if left_utc < right_utc:
            return "before"
        if left_utc > right_utc:
            return "after"
        return "equal"

    # 至少一个是日粒度：先按自然日归属判断。
    left_day = left.day_start
    right_day = right.day_start
    if left_day < right_day:
        return "before"
    if left_day > right_day:
        return "after"
    # 同一天且至少一个只有日粒度 → 时刻先后无法判断。
    return "same_day_unknown"


def evaluate_expiry(point: TimePoint, now: datetime) -> dict[str, Any]:
    """以 ``now`` 为统一时间依据判断 30 天清退。

    返回决策及可解释明细：

    - ``expire``：确定超过 30 天；
    - ``keep``：确定未超过 30 天；
    - ``review``：日粒度记录的自然日横跨 30 天边界，无法确定此前是否应清退。
    """
    now_utc = now.astimezone(timezone.utc)
    if point.precision == "instant":
        age = now_utc - point.instant.astimezone(timezone.utc)
        decision: Decision = "expire" if age > timedelta(days=EXPIRE_DAYS) else "keep"
        return {
            "decision": decision,
            "precision": "instant",
            "age_days": age.total_seconds() / 86400,
            "threshold_days": EXPIRE_DAYS,
            "reason": (
                f"带时区时刻 {point.raw} 距基准时刻 {age.total_seconds() / 86400:.2f} 天，"
                + ("超过30天，应清退" if decision == "expire" else "未超过30天，保留")
            ),
        }

    # 日粒度：真实时刻落在 [day_start, day_end) 内，整段都 >30 天才算确定过期，
    # 整段都 <=30 天才算确定保留，跨边界则必须人工复核。
    # 事件最早可能发生在 day_start → 年龄最小；最晚可能发生在 day_end → 年龄最大。
    min_age_days = (now_utc - point.day_end).total_seconds() / 86400
    max_age_days = (now_utc - point.day_start).total_seconds() / 86400
    if min_age_days > EXPIRE_DAYS:
        decision = "expire"
        reason = (
            f"日粒度值 {point.raw} 即使按当天最晚时刻计算也已超过30天"
            f"（可能年龄 {min_age_days:.2f}~{max_age_days:.2f} 天），应清退"
        )
    elif max_age_days <= EXPIRE_DAYS:
        decision = "keep"
        reason = (
            f"日粒度值 {point.raw} 即使按当天最早时刻计算仍未满30天"
            f"（可能年龄 {min_age_days:.2f}~{max_age_days:.2f} 天），保留"
        )
    else:
        decision = "review"
        reason = (
            f"日粒度值 {point.raw} 的自然日横跨30天清退边界"
            f"（可能年龄 {min_age_days:.2f}~{max_age_days:.2f} 天），"
            "无法确定此前是否应清退，转人工复核"
        )
    return {
        "decision": decision,
        "precision": "day",
        "age_days": None,
        "age_window_days": [min_age_days, max_age_days],
        "threshold_days": EXPIRE_DAYS,
        "reason": reason,
    }
