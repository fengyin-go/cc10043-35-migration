"""检查点创建时间字段的迁移协调器。

职责覆盖上线全周期：

- 分批回填：把文本「创建时间」解析为带时区的时间字段，日粒度老值保留精度事实；
- 坏值隔离：无法解读的历史值进入隔离表供人工核查，不参与续采与清退；
- 读法切换：``legacy``（回滚，只认原始文本）/ ``mixed``（迁移中，优先迁移字段，
  缺省回退原始值）/ ``migrated``（只认迁移后的时间字段）；
- 统一时间依据：续采资格判断与过期清退共用同一个基准时刻，避免混合格式各算各的；
- 可解释：每次选用了哪个点、依据哪个读法、此前是否应清退，都能输出明细；
- 可回滚：原始文本字段「创建时间」始终保留，切回 legacy 即恢复迁移前读法。

存储为进程内单例，和 :mod:`app.store` 的内存仓库一致；真实环境里这里换成
带版本号的迁移表即可，字段语义不变。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from app.services.checkpoint_time import (
    TimePoint,
    evaluate_expiry,
    parse_time_point,
)
from app.store import store

ReadMode = Literal["legacy", "mixed", "migrated"]
MigrationState = Literal["pending", "migrated", "quarantined"]

# 迁移后新增的结构化字段；原始文本字段「创建时间」保留不动。
F_INSTANT = "迁移创建时刻"       # 带时区的 ISO-8601 UTC 时刻（日粒度为当日零点锚点）
F_PRECISION = "时间精度"         # instant | day
F_TZ_OFFSET = "时区偏移"         # 原始偏移，如 +08:00；日粒度为 None
F_STATE = "迁移状态"             # pending | migrated | quarantined
F_REASON = "迁移说明"            # 解析/隔离原因，便于审计

MODULE = "record_checkpoint"
QUARANTINE_MODULE = "checkpoint_time_quarantine"


class CheckpointMigration:
    """进程内迁移状态机。默认从混合读法启动，兼容尚未回填的记录。"""

    def __init__(self) -> None:
        self._read_mode: ReadMode = "mixed"
        self._now: datetime | None = None  # 测试可固定的统一基准时刻
        self.backfill_batches = 0
        self.expiry_scan_cursor = 0
        self.expiry_scan_finished = False

    # ---- 读法与统一时间依据 ----

    @property
    def read_mode(self) -> ReadMode:
        return self._read_mode

    def set_read_mode(self, mode: ReadMode) -> dict[str, Any]:
        if mode not in ("legacy", "mixed", "migrated"):
            raise ValueError(f"未知读法: {mode}")
        previous = self._read_mode
        self._read_mode = mode
        return {
            "ok": True,
            "previous_mode": previous,
            "current_mode": mode,
            "note": self._mode_note(mode),
        }

    @staticmethod
    def _mode_note(mode: ReadMode) -> str:
        if mode == "legacy":
            return "回滚读法：仅使用原始文本字段「创建时间」，迁移字段忽略"
        if mode == "mixed":
            return "混合读法：已回填记录用迁移字段，未回填记录回退原始文本"
        return "迁移读法：仅使用迁移后的时间字段，未回填/隔离记录不参与时间判断"

    def reference_now(self) -> datetime:
        """续采资格与过期清退共用的基准时刻（UTC，带时区）。"""
        return self._now if self._now is not None else datetime.now(timezone.utc)

    def set_reference_now(self, value: datetime | str | None) -> datetime:
        """固定/恢复基准时刻，供分批回填与中断恢复保持同一时间依据。"""
        if value is None:
            self._now = None
        elif isinstance(value, datetime):
            self._now = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        else:
            parsed, error = parse_time_point(value)
            if error or parsed is None:
                raise ValueError(f"基准时刻无法解析: {error}")
            self._now = parsed.instant
        return self.reference_now()

    # ---- 回填 ----

    def backfill_batch(self, limit: int = 2) -> dict[str, Any]:
        """回填一批仍处于 pending 的检查点，返回本批处理明细。

        幂等：已迁移/已隔离的记录不会重复处理。坏值进隔离表并打上 quarantined。
        """
        rows = store.rows(MODULE)
        batch = [r for r in rows if r.get(F_STATE, "pending") == "pending"][:limit]
        migrated: list[int] = []
        quarantined: list[dict[str, Any]] = []
        for row in batch:
            point, error = parse_time_point(row.get("创建时间"))
            cp_id = int(row.get("id", 0))
            if error or point is None:
                row[F_STATE] = "quarantined"
                row[F_REASON] = error
                entry = {
                    "id": self._next_quarantine_id(),
                    "来源模块": MODULE,
                    "来源记录ID": cp_id,
                    "原始字段": "创建时间",
                    "原始值": str(row.get("创建时间", "")),
                    "隔离原因": error,
                    "处理状态": "待核查",
                    "status": "待处理",
                    "pending": True,
                    "abnormal": True,
                }
                store.rows(QUARANTINE_MODULE).append(entry)
                quarantined.append({"id": cp_id, "raw": entry["原始值"], "reason": error})
                continue
            row[F_INSTANT] = point.iso_text()
            row[F_PRECISION] = point.precision
            row[F_TZ_OFFSET] = point.tz_offset
            row[F_STATE] = "migrated"
            row[F_REASON] = (
                "带时区时刻，按实际时刻迁移"
                if point.precision == "instant"
                else "日粒度老值，仅知自然日，时刻未知，精度保持day"
            )
            migrated.append(cp_id)
        if batch:
            self.backfill_batches += 1
        return {
            "ok": True,
            "batch_size": len(batch),
            "migrated_ids": migrated,
            "quarantined": quarantined,
            "remaining": sum(1 for r in rows if r.get(F_STATE, "pending") == "pending"),
            "read_mode": self._read_mode,
            "reference_now": self.reference_now().isoformat(),
        }

    def backfill_all(self, batch_size: int = 2) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        while True:
            report = self.backfill_batch(limit=batch_size)
            if report["batch_size"] == 0:
                break
            reports.append(report)
        return reports

    def migration_progress(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        by_state = {"pending": 0, "migrated": 0, "quarantined": 0}
        for row in rows:
            by_state[str(row.get(F_STATE, "pending"))] = (
                by_state.get(str(row.get(F_STATE, "pending")), 0) + 1
            )
        return {
            "read_mode": self._read_mode,
            "reference_now": self.reference_now().isoformat(),
            "total": len(rows),
            **by_state,
            "quarantine_total": len(store.rows(QUARANTINE_MODULE)),
            "backfill_batches": self.backfill_batches,
            "expiry_scan_cursor": self.expiry_scan_cursor,
            "expiry_scan_finished": self.expiry_scan_finished,
        }

    # ---- 混合读 / 回滚读 ----

    def resolve_point(self, row: dict[str, Any]) -> tuple[TimePoint | None, dict[str, Any]]:
        """按当前读法解析一条检查点的可用时间点，并返回解释明细。

        隔离记录在任何非 legacy 读法下都不提供可用点（隔离核查，不参与判断）。
        """
        raw = str(row.get("创建时间", ""))
        state = str(row.get(F_STATE, "pending"))
        mode = self._read_mode

        if mode == "legacy":
            point, error = parse_time_point(raw)
            return point, {
                "read_mode": "legacy",
                "source_field": "创建时间",
                "raw_value": raw,
                "usable": point is not None,
                "error": error,
                "note": "回滚读法，仅解析原始文本",
            }

        if state == "quarantined":
            return None, {
                "read_mode": mode,
                "source_field": "创建时间",
                "raw_value": raw,
                "usable": False,
                "migration_state": state,
                "error": row.get(F_REASON, "坏历史值已隔离核查"),
                "note": "隔离记录不参与续采与清退判断",
            }

        if mode == "migrated":
            if state != "migrated":
                return None, {
                    "read_mode": "migrated",
                    "source_field": F_INSTANT,
                    "raw_value": raw,
                    "usable": False,
                    "migration_state": state,
                    "error": "记录尚未迁移，迁移读法下不可用",
                }
            point, error = parse_time_point(row.get(F_INSTANT))
            if point is not None and row.get(F_PRECISION) == "day":
                point = TimePoint(
                    instant=point.instant, precision="day", raw=raw, tz_offset=None)
            return point, {
                "read_mode": "migrated",
                "source_field": F_INSTANT,
                "raw_value": raw,
                "usable": point is not None,
                "migration_state": state,
                "precision": point.precision if point else None,
                "error": error,
            }

        # mixed：优先迁移字段，未回填则回退原始文本现场解析。
        if state == "migrated":
            point, error = parse_time_point(row.get(F_INSTANT))
            if point is not None and row.get(F_PRECISION) == "day":
                point = TimePoint(
                    instant=point.instant, precision="day", raw=raw, tz_offset=None)
            return point, {
                "read_mode": "mixed",
                "source_field": F_INSTANT,
                "raw_value": raw,
                "usable": point is not None,
                "migration_state": state,
                "precision": point.precision if point else None,
                "error": error,
            }
        point, error = parse_time_point(raw)
        return point, {
            "read_mode": "mixed",
            "source_field": "创建时间(未回填回退)",
            "raw_value": raw,
            "usable": point is not None,
            "migration_state": state,
            "precision": point.precision if point else None,
            "error": error,
        }

    def explain_available_points(self, task_name: str) -> list[dict[str, Any]]:
        """输出某任务下每条检查点的可用点明细（混合读/回滚期间可解释用了哪个点）。"""
        details: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            if task_name not in str(row.get("关联接入任务", "")):
                continue
            point, basis = self.resolve_point(row)
            detail = {
                "id": int(row.get("id", 0)),
                "task": row.get("关联接入任务", ""),
                "检查点类型": row.get("检查点类型", ""),
                "检查点值": row.get("检查点值", ""),
                "status": row.get("status", ""),
                "raw_value": basis["raw_value"],
                "read_mode": basis["read_mode"],
                "source_field": basis["source_field"],
                "migration_state": row.get(F_STATE, "pending"),
                "usable": basis["usable"],
                "precision": point.precision if point else None,
                "resolved_instant": point.iso_text() if point else None,
            }
            if point is not None:
                expiry = evaluate_expiry(point, self.reference_now())
                detail["expiry"] = expiry
            else:
                detail["expiry"] = {
                    "decision": "review", "precision": None,
                    "reason": basis.get("error", "无可用时间点，隔离核查"),
                }
            details.append(detail)
        return details

    # ---- 过期清退（可中断、可续扫，共用同一时间依据）----

    def scan_expiry_batch(self, limit: int = 2) -> dict[str, Any]:
        """按 ID 顺序扫描待确认检查点并清退；游标持久化，中断后可续扫。

        只处理「待确认」状态；确定过期才置「已过期」，跨边界转复核，坏值已隔离。
        """
        rows = sorted(store.rows(MODULE), key=lambda r: int(r.get("id", 0)))
        pending_rows = [r for r in rows if r.get("status") == "待确认"]
        scanned: list[dict[str, Any]] = []
        expired: list[int] = []
        review: list[int] = []
        processed = 0
        for row in pending_rows:
            cp_id = int(row.get("id", 0))
            if cp_id <= self.expiry_scan_cursor:
                continue
            if processed >= limit:
                break
            self.expiry_scan_cursor = cp_id
            processed += 1
            point, basis = self.resolve_point(row)
            if point is None:
                review.append(cp_id)
                scanned.append({
                    "id": cp_id, "decision": "review",
                    "reason": basis.get("error", "无可用时间点"),
                })
                continue
            verdict = evaluate_expiry(point, self.reference_now())
            scanned.append({"id": cp_id, **verdict})
            if verdict["decision"] == "expire":
                row["status"] = "已过期"
                row["pending"] = False
                expired.append(cp_id)
            elif verdict["decision"] == "review":
                review.append(cp_id)

        if processed == 0:
            self.expiry_scan_finished = True
        self._write_audit_if_any(expired)
        return {
            "ok": True,
            "scanned": scanned,
            "expired_ids": expired,
            "review_ids": review,
            "cursor": self.expiry_scan_cursor,
            "finished": self.expiry_scan_finished,
            "read_mode": self._read_mode,
            "reference_now": self.reference_now().isoformat(),
        }

    def reset_expiry_scan(self) -> dict[str, Any]:
        """清退扫描中断后重置游标，便于重新扫描（先重开续采再回退读法的编排由调用方负责）。"""
        self.expiry_scan_cursor = 0
        self.expiry_scan_finished = False
        return {"ok": True, "cursor": 0, "finished": False}

    def _write_audit_if_any(self, expired_ids: list[int]) -> None:
        if not expired_ids:
            return
        audit_rows = store.rows("stream_audit")
        aid = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        audit_rows.append({
            "id": aid,
            "处理时间": self.reference_now().isoformat(),
            "数据源": "record_checkpoint",
            "消息总数": str(len(expired_ids)),
            "成功数": str(len(expired_ids)),
            "失败原因分布": "",
            "status": "正常",
            "pending": True,
            "abnormal": False,
        })

    # ---- 兼容接口：迁移前原始值 ----

    def raw_legacy_view(self) -> list[dict[str, Any]]:
        """返回所有检查点迁移前的原始文本值，兼容旧接口的完整可查性。"""
        return [
            {
                "id": int(row.get("id", 0)),
                "创建时间": row.get("创建时间", ""),
                "迁移状态": row.get(F_STATE, "pending"),
                "时间精度": row.get(F_PRECISION),
                "迁移创建时刻": row.get(F_INSTANT),
            }
            for row in store.rows(MODULE)
        ]

    def quarantine_list(self) -> list[dict[str, Any]]:
        return store.rows(QUARANTINE_MODULE)

    # ---- 内部 ----

    def _next_quarantine_id(self) -> int:
        return max(
            (int(r.get("id", 0)) for r in store.rows(QUARANTINE_MODULE)), default=0) + 1

    def reset_runtime(self) -> None:
        """测试支撑：重建仓库后把迁移状态机恢复到初始值。"""
        self._read_mode = "mixed"
        self._now = None
        self.backfill_batches = 0
        self.expiry_scan_cursor = 0
        self.expiry_scan_finished = False


# 单例：和 store 一样进程内共享。
checkpoint_migration = CheckpointMigration()
