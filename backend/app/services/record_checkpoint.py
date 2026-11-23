"""记录检查点领域服务：管理接入任务的断点位置，支持恢复和回溯，提供检查点的确认和过期管理。

数据链路位置：断点管理层，为接入任务提供可靠的断点记录和恢复依据，是续采策略的数据基础。

时间字段迁移说明：历史「创建时间」是混有纯日期、带偏移 ISO 时间和坏值的文本字段。
本服务的所有时间判断（最新检查点、续采资格、30 天清退）统一改走
:mod:`app.services.checkpoint_migration`，按实际时刻比较，日粒度保留精度事实，
坏值隔离核查，并支持 legacy/mixed/migrated 三种读法与回滚。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.checkpoint_migration import (
    F_INSTANT,
    F_PRECISION,
    F_STATE,
    F_TZ_OFFSET,
    checkpoint_migration,
)
from app.services.checkpoint_time import compare_points, evaluate_expiry, parse_time_point
from app.store import store


class RecordCheckpointService(BaseModuleService):
    MODULE = "record_checkpoint"
    ENTITY = "检查点"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["关联接入任务", "检查点类型", "检查点值", "创建时间", "确认状态"]
    REQUIRED_FIELDS = ["关联接入任务", "检查点类型", "检查点值"]
    STATUS_ORDER = ["待确认", "已确认", "已过期"]
    ACTION_RULES = {"确认检查点": "已确认", "废弃检查点": "已过期"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：断点记录与恢复 ----

    def create_checkpoint(self, task_name: str, point_type: str, point_value: str) -> dict[str, Any]:
        """创建新的检查点记录；新入口保存带时区的时间并直接写迁移字段。"""
        rows = store.rows(self.MODULE)
        cp_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        now = checkpoint_migration.reference_now()
        now_text = now.isoformat()
        checkpoint = {
            "id": cp_id, "关联接入任务": task_name, "检查点类型": point_type,
            "检查点值": point_value, "创建时间": now_text, "确认状态": "待确认",
            # 新记录天然是带时区的 instant，迁移字段一次性到位。
            F_INSTANT: now.astimezone(timezone.utc).isoformat(),
            F_PRECISION: "instant",
            F_TZ_OFFSET: _format_offset(now),
            F_STATE: "migrated",
            "status": "待确认", "pending": True, "abnormal": False,
        }
        rows.append(checkpoint)
        return {"ok": True, "message": "检查点已创建（带时区时间）", "checkpoint": checkpoint}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """通用登记入口：创建时间若由前端传入则校验，缺省由服务端生成带时区时间。"""
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})

        supplied = str(values.get("创建时间") or "").strip()
        if supplied:
            point, error = parse_time_point(supplied)
            if error or point is None:
                return None, [f"创建时间: {error}"]
            entry["创建时间"] = supplied
            entry[F_INSTANT] = point.iso_text()
            entry[F_PRECISION] = point.precision
            entry[F_TZ_OFFSET] = point.tz_offset
            entry[F_STATE] = "migrated"
        else:
            now = checkpoint_migration.reference_now()
            entry["创建时间"] = now.isoformat()
            entry[F_INSTANT] = now.astimezone(timezone.utc).isoformat()
            entry[F_PRECISION] = "instant"
            entry[F_TZ_OFFSET] = _format_offset(now)
            entry[F_STATE] = "migrated"

        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def get_latest_checkpoint(self, task_name: str) -> dict[str, Any] | None:
        """获取指定接入任务的最新已确认检查点（按实际时刻，经迁移层混合读）。"""
        chosen, _basis = self._latest_confirmed(task_name)
        return chosen

    def get_latest_checkpoint_explained(self, task_name: str) -> dict[str, Any]:
        """带解释的最新检查点选择：用了哪个点、什么读法、同日是否不可判定。"""
        chosen, basis = self._latest_confirmed(task_name)
        return {"checkpoint": chosen, "basis": basis}

    def _latest_confirmed(self, task_name: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        rows = store.rows(self.MODULE)
        confirmed = [r for r in rows if task_name in str(r.get("关联接入任务", ""))
                     and r.get("status") == "已确认"]
        if not confirmed:
            return None, {"task": task_name, "usable": 0, "reason": "无已确认检查点"}
        candidates: list[tuple[dict[str, Any], Any, dict[str, Any]]] = []
        for row in confirmed:
            point, basis = checkpoint_migration.resolve_point(row)
            if point is not None:
                candidates.append((row, point, basis))
        if not candidates:
            return None, {"task": task_name, "usable": 0,
                          "reason": "已确认检查点的时间点均不可用（隔离/未迁移）"}
        chosen_row, chosen_point, chosen_basis = candidates[0]
        comparisons = []
        for row, point, basis in candidates[1:]:
            result = compare_points(chosen_point, point)
            comparisons.append({
                "left_id": int(chosen_row.get("id", 0)),
                "right_id": int(row.get("id", 0)),
                "result": result,
            })
            if result == "before":
                chosen_row, chosen_point, chosen_basis = row, point, basis
            elif result == "same_day_unknown" and chosen_point.precision == "day" \
                    and point.precision == "instant":
                chosen_row, chosen_point, chosen_basis = row, point, basis
        indeterminate = [c for c in comparisons if c["result"] == "same_day_unknown"]
        return chosen_row, {
            "task": task_name,
            "usable": len(candidates),
            "chosen_id": int(chosen_row.get("id", 0)),
            "chosen_precision": chosen_point.precision,
            "read_mode": chosen_basis["read_mode"],
            "source_field": chosen_basis["source_field"],
            "raw_value": chosen_basis["raw_value"],
            "indeterminate_same_day": bool(indeterminate),
            "indeterminate_pairs": indeterminate,
            "all_comparisons": comparisons,
        }

    def evaluate_resume_eligibility(self, task_name: str) -> dict[str, Any]:
        """续采资格：使用迁移后的检查点判断，过期清退共用同一基准时刻。

        规则：取最新可用已确认检查点；该点在统一基准时刻下若确定已过期则无资格，
        跨 30 天边界/隔离则转复核，不允许静默续采。
        """
        now = checkpoint_migration.reference_now()
        chosen, basis = self._latest_confirmed(task_name)
        if chosen is None:
            return {"eligible": False, "reason": basis.get("reason", "无可用检查点"),
                    "read_mode": checkpoint_migration.read_mode, "reference_now": now.isoformat()}
        point, point_basis = checkpoint_migration.resolve_point(chosen)
        if point is None:
            return {
                "eligible": False, "needs_review": True,
                "checkpoint_id": int(chosen.get("id", 0)),
                "read_mode": point_basis["read_mode"],
                "source_field": point_basis["source_field"],
                "raw_value": point_basis["raw_value"],
                "reason": point_basis.get("error", "检查点时间不可用，转复核"),
                "reference_now": now.isoformat(),
            }
        verdict = evaluate_expiry(point, now)
        eligible = verdict["decision"] == "keep"
        return {
            "eligible": eligible,
            "needs_review": verdict["decision"] == "review",
            "checkpoint_id": int(chosen.get("id", 0)),
            "read_mode": point_basis["read_mode"],
            "source_field": point_basis["source_field"],
            "raw_value": point_basis["raw_value"],
            "precision": point.precision,
            "expiry": verdict,
            "selection": basis,
            "reference_now": now.isoformat(),
        }

    def validate_checkpoint_chain(self, entry_id: int) -> dict[str, Any]:
        """校验检查点链的完整性：是否存在断链或顺序异常。

        时间顺序改按实际时刻比较；同日不可判定的精度差异只告警、不算断链。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "检查点不存在"}
        task = entry.get("关联接入任务", "")
        rows = [r for r in store.rows(self.MODULE)
                if task in str(r.get("关联接入任务", ""))]
        chain: list[tuple[dict[str, Any], Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in rows:
            point, basis = checkpoint_migration.resolve_point(row)
            if point is None:
                skipped.append({"id": int(row.get("id", 0)), "reason": basis.get("error")})
            else:
                chain.append((row, point))
        # 以自然日锚点稳定排序，再用领域比较复核。
        chain.sort(key=lambda item: item[1].instant)
        gaps: list[str] = []
        same_day_notes: list[str] = []
        for i in range(1, len(chain)):
            prev_row, prev_point = chain[i - 1]
            curr_row, curr_point = chain[i]
            prev_val = prev_row.get("检查点值", "")
            curr_val = curr_row.get("检查点值", "")
            if prev_val >= curr_val:
                gaps.append(f"检查点值非递增: {prev_val} → {curr_val}")
            order = compare_points(prev_point, curr_point)
            if order == "after":
                gaps.append(
                    f"时间顺序异常: id={prev_row.get('id')} 晚于 id={curr_row.get('id')}")
            elif order == "same_day_unknown":
                same_day_notes.append(
                    f"id={prev_row.get('id')} 与 id={curr_row.get('id')} 同日但精度不同，"
                    "先后不可判定（未当作两个零点）")
        return {
            "ok": len(gaps) == 0,
            "task": task,
            "chain_length": len(chain),
            "skipped_unresolved": skipped,
            "gaps": gaps,
            "same_day_unknown": same_day_notes,
            "read_mode": checkpoint_migration.read_mode,
        }

    def route_to_resume(self, entry_id: int) -> dict[str, Any] | None:
        """检查点路由到续采策略，返回匹配的续采规则。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        point_type = entry.get("检查点类型", "")
        policies = store.rows("resume_policy")
        for policy in policies:
            if policy.get("status") == "已启用" and point_type in policy.get("续采起点类型", ""):
                return {"policy": policy, "checkpoint": entry}
        return None

    def audit_expired_checkpoints(self) -> list[int]:
        """兼容旧签名的过期审计：内部改走可中断的迁移清退扫描，直至扫完。"""
        checkpoint_migration.reset_expiry_scan()
        expired: list[int] = []
        while True:
            report = checkpoint_migration.scan_expiry_batch(limit=100)
            expired.extend(report["expired_ids"])
            if report["finished"]:
                break
        return expired

    def audit_expired_explained(self, limit: int = 2) -> dict[str, Any]:
        """带明细的一批清退扫描，支持中断后凭游标续扫。"""
        return checkpoint_migration.scan_expiry_batch(limit=limit)

    def explain_point(self, entry_id: int) -> dict[str, Any]:
        """单条检查点的可用点明细 + 清退判断，混合读/回滚期间用于解释。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "检查点不存在"}
        point, basis = checkpoint_migration.resolve_point(entry)
        payload: dict[str, Any] = {
            "ok": True,
            "id": entry_id,
            "raw_value": basis["raw_value"],
            "read_mode": basis["read_mode"],
            "source_field": basis["source_field"],
            "migration_state": entry.get(F_STATE, "pending"),
            "usable": basis["usable"],
            "resolved_instant": point.iso_text() if point else None,
            "precision": point.precision if point else None,
        }
        if point is not None:
            payload["expiry"] = evaluate_expiry(
                point, checkpoint_migration.reference_now())
        else:
            payload["expiry"] = {
                "decision": "review", "reason": basis.get("error", "无可用时间点")}
        return payload

    def commit_offset(self, entry_id: int) -> str | None:
        """将已确认检查点提交到偏移量管理。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已确认":
            return None
        offset_rows = store.rows("offset_manager")
        oid = max((int(r.get("id", 0)) for r in offset_rows), default=0) + 1
        now = checkpoint_migration.reference_now().isoformat()
        offset_rows.append({
            "id": oid, "消费组": entry.get("关联接入任务", ""),
            "Topic/Queue": entry.get("关联接入任务", ""),
            "分区/分片": "0", "当前Offset": entry.get("检查点值", "0"),
            "消费延迟": "0", "status": "正常", "pending": True, "abnormal": False,
        })
        return now


def _format_offset(moment: datetime) -> str:
    offset = moment.utcoffset() or timezone.utc.utcoffset(None)
    total_minutes = int((offset or timedelta(0)).total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"
