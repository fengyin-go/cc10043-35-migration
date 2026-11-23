"""检查点时间字段迁移专用接口。

与通用资源路由并列，覆盖：分批回填、隔离核查、读法切换/回滚、统一基准时刻、
可用点明细、续采资格、可中断清退扫描、迁移前原始值兼容查询。

路径前缀刻意区别于通用 ``/api/{module}``，避免被资源路由动态匹配。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.checkpoint_migration import checkpoint_migration

router = APIRouter(prefix="/api/record-checkpoint-migration", tags=["检查点时间迁移"])


class ReadModePayload(BaseModel):
    mode: Literal["legacy", "mixed", "migrated"]


class BackfillPayload(BaseModel):
    limit: int = Field(default=2, ge=1, le=200)


class ScanPayload(BaseModel):
    limit: int = Field(default=2, ge=1, le=200)


class ReferenceNowPayload(BaseModel):
    now: str | None = None


class TaskPayload(BaseModel):
    task: str


@router.get("/progress")
def progress() -> dict[str, Any]:
    """迁移进度、当前读法、隔离数、清退扫描游标与统一基准时刻。"""
    return checkpoint_migration.migration_progress()


@router.post("/backfill")
def backfill(payload: BackfillPayload) -> dict[str, Any]:
    """执行一批回填（幂等），返回迁移/隔离明细。"""
    return checkpoint_migration.backfill_batch(limit=payload.limit)


@router.post("/backfill-all")
def backfill_all(payload: BackfillPayload) -> dict[str, Any]:
    """一次性分批回填完所有 pending 记录，返回每批报告。"""
    reports = checkpoint_migration.backfill_all(batch_size=payload.limit)
    return {"ok": True, "batches": reports, "progress": checkpoint_migration.migration_progress()}


@router.get("/quarantine")
def quarantine() -> dict[str, Any]:
    """无法解读的历史值隔离区。"""
    items = checkpoint_migration.quarantine_list()
    return {"items": items, "total": len(items)}


@router.get("/raw-values")
def raw_values() -> dict[str, Any]:
    """兼容接口：可查到所有迁移前的原始文本值。"""
    items = checkpoint_migration.raw_legacy_view()
    return {"items": items, "total": len(items)}


@router.post("/read-mode")
def set_read_mode(payload: ReadModePayload) -> dict[str, Any]:
    """切换读法（legacy 即回滚读法），返回切换前后状态。"""
    return checkpoint_migration.set_read_mode(payload.mode)


@router.post("/reference-now")
def set_reference_now(payload: ReferenceNowPayload) -> dict[str, Any]:
    """固定/恢复续采与清退共用的基准时刻。"""
    try:
        current = checkpoint_migration.set_reference_now(payload.now)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "reference_now": current.isoformat()}


@router.get("/available-points")
def available_points(task: str) -> dict[str, Any]:
    """某任务每条检查点的可用点明细：用了哪个字段、精度、清退判断。"""
    items = checkpoint_migration.explain_available_points(task)
    return {"task": task, "read_mode": checkpoint_migration.read_mode,
            "reference_now": checkpoint_migration.reference_now().isoformat(),
            "items": items, "total": len(items)}


@router.get("/resume-eligibility")
def resume_eligibility(task: str) -> dict[str, Any]:
    """续采资格：使用迁移后的检查点，与清退共用同一时间依据。"""
    service = service_for("record_checkpoint")
    return service.evaluate_resume_eligibility(task)


@router.get("/latest-explained")
def latest_explained(task: str) -> dict[str, Any]:
    """最新已确认检查点的选择解释（含同日不可判定明细）。"""
    service = service_for("record_checkpoint")
    return service.get_latest_checkpoint_explained(task)


@router.post("/expiry-scan")
def expiry_scan(payload: ScanPayload) -> dict[str, Any]:
    """一批清退扫描；凭游标可在中断后续扫。"""
    return checkpoint_migration.scan_expiry_batch(limit=payload.limit)


@router.post("/expiry-scan/reset")
def expiry_scan_reset() -> dict[str, Any]:
    """清退扫描中断后重置游标。"""
    return checkpoint_migration.reset_expiry_scan()


@router.get("/points/{entry_id}")
def point_detail(entry_id: int) -> dict[str, Any]:
    """单条检查点的可用点与清退解释。"""
    service = service_for("record_checkpoint")
    return service.explain_point(entry_id)


@router.get("/chain/{entry_id}")
def chain_detail(entry_id: int) -> dict[str, Any]:
    """检查点链校验：跳过隔离点、报告同日不可判定，不把日期当零点。"""
    service = service_for("record_checkpoint")
    return service.validate_checkpoint_chain(entry_id)
