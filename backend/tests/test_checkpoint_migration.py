"""迁移协调器的单元测试：回填、隔离、三种读法、可中断清退与原始值兼容。

直接驱动内存仓库与迁移状态机，不经过 HTTP；每个用例在 setUp 重建受控数据集。
"""
from __future__ import annotations

import unittest
from datetime import timezone

from app.services.checkpoint_migration import (
    F_INSTANT,
    F_PRECISION,
    F_STATE,
    QUARANTINE_MODULE,
    checkpoint_migration,
)
from app.store import store

MODULE = "record_checkpoint"
NOW = "2026-11-23T12:00:00+00:00"


def _row(row_id: int, created: str, status: str = "待确认",
         task: str = "订单流接入") -> dict:
    return {
        "id": row_id, "status": status, "pending": status == "待确认",
        "abnormal": False, "关联接入任务": task, "检查点类型": "偏移量检查点",
        "检查点值": str(1000 + row_id), "创建时间": created, "确认状态": status,
    }


class MigrationCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        checkpoints = store.rows(MODULE)
        checkpoints.clear()
        checkpoints.extend([
            _row(1, "2026-10-25T10:00:00+08:00", status="已确认"),  # instant
            _row(2, "2026-10-24"),                                  # day, 跨边界
            _row(3, "2026-13-40 坏值"),                             # 坏值
            _row(4, "2026-11-01T09:00:00"),                         # 裸时间坏值
        ])
        store.rows(QUARANTINE_MODULE).clear()
        checkpoint_migration.reset_runtime()
        checkpoint_migration.set_reference_now(NOW)

    # ---- 回填与隔离 ----

    def test_backfill_classifies_three_shapes_and_is_idempotent(self) -> None:
        first = checkpoint_migration.backfill_batch(limit=2)
        self.assertEqual(first["migrated_ids"], [1, 2])
        second = checkpoint_migration.backfill_batch(limit=10)
        self.assertEqual(second["migrated_ids"], [])
        self.assertEqual([q["id"] for q in second["quarantined"]], [3, 4])
        third = checkpoint_migration.backfill_batch(limit=10)
        self.assertEqual(third["batch_size"], 0)  # 幂等：无重复处理

        rows = {r["id"]: r for r in store.rows(MODULE)}
        self.assertEqual(rows[1][F_PRECISION], "instant")
        self.assertEqual(rows[1][F_INSTANT], "2026-10-25T02:00:00+00:00")
        self.assertEqual(rows[2][F_PRECISION], "day")
        self.assertEqual(rows[2][F_INSTANT], "2026-10-24")  # 仍是纯日期
        self.assertEqual(rows[3][F_STATE], "quarantined")
        self.assertEqual(rows[4][F_STATE], "quarantined")
        self.assertEqual(len(store.rows(QUARANTINE_MODULE)), 2)

    # ---- 读法切换 / 回滚 ----

    def test_legacy_mode_ignores_migrated_field_and_keeps_raw(self) -> None:
        checkpoint_migration.backfill_batch(limit=10)
        row = store.find(MODULE, 1)
        # 人为篡改迁移字段，legacy 必须仍以原始文本为准。
        row[F_INSTANT] = "2030-01-01T00:00:00+00:00"
        checkpoint_migration.set_read_mode("legacy")
        point, basis = checkpoint_migration.resolve_point(row)
        self.assertEqual(basis["source_field"], "创建时间")
        self.assertEqual(point.precision, "instant")
        self.assertEqual(point.iso_text(), "2026-10-25T02:00:00+00:00")

    def test_mixed_prefers_migrated_and_falls_back_for_pending(self) -> None:
        checkpoint_migration.backfill_batch(limit=2)  # 只迁 1、2
        migrated_row = store.find(MODULE, 1)
        point, basis = checkpoint_migration.resolve_point(migrated_row)
        self.assertEqual(basis["source_field"], F_INSTANT)
        self.assertEqual(point.precision, "instant")

        pending_row = store.find(MODULE, 3)  # 仍是 pending（且是坏值）
        point, basis = checkpoint_migration.resolve_point(pending_row)
        self.assertIn("未回填回退", basis["source_field"])
        self.assertIsNone(point)  # 坏值现场解析仍失败

    def test_migrated_mode_rejects_pending_and_quarantined(self) -> None:
        checkpoint_migration.backfill_batch(limit=10)
        checkpoint_migration.set_read_mode("migrated")
        quarantined = store.find(MODULE, 3)
        point, basis = checkpoint_migration.resolve_point(quarantined)
        self.assertIsNone(point)
        self.assertFalse(basis["usable"])

    def test_quarantine_excluded_in_all_non_legacy_modes(self) -> None:
        checkpoint_migration.backfill_batch(limit=10)
        for mode in ("mixed", "migrated"):
            checkpoint_migration.set_read_mode(mode)
            point, basis = checkpoint_migration.resolve_point(store.find(MODULE, 4))
            self.assertIsNone(point, mode)
            self.assertFalse(basis["usable"], mode)

    # ---- 统一时间依据与清退 ----

    def test_expiry_scan_respects_shared_now_and_cursor(self) -> None:
        checkpoint_migration.backfill_batch(limit=10)
        first = checkpoint_migration.scan_expiry_batch(limit=1)
        self.assertEqual([s["id"] for s in first["scanned"]], [2])
        self.assertEqual(first["scanned"][0]["decision"], "review")  # 跨边界不自动清退
        self.assertGreater(first["cursor"], 0)
        self.assertFalse(first["finished"])

        # 继续扫描直到游标耗尽：finished 只在没有新记录可处理的那一拍置位。
        review_ids = set(first["review_ids"])
        expired_ids = set(first["expired_ids"])
        report = first
        for _ in range(10):
            report = checkpoint_migration.scan_expiry_batch(limit=10)
            review_ids.update(report["review_ids"])
            expired_ids.update(report["expired_ids"])
            if report["finished"]:
                break
        self.assertTrue(report["finished"])
        # 跨边界的 id2 与两个坏值 id3/id4 全部转复核；没有任何一条被自动清退。
        self.assertEqual(expired_ids, set())
        self.assertEqual(review_ids, {2, 3, 4})
        self.assertEqual(store.find(MODULE, 2)["status"], "待确认")

        reset = checkpoint_migration.reset_expiry_scan()
        self.assertEqual(reset["cursor"], 0)
        self.assertFalse(reset["finished"])

    def test_reference_now_is_shared_between_eligibility_and_scan(self) -> None:
        now = checkpoint_migration.set_reference_now(NOW)
        self.assertEqual(now.tzinfo, timezone.utc)
        self.assertEqual(checkpoint_migration.reference_now().isoformat(), NOW)

    # ---- 兼容接口 ----

    def test_raw_legacy_view_returns_all_originals(self) -> None:
        checkpoint_migration.backfill_batch(limit=10)
        view = {item["id"]: item for item in checkpoint_migration.raw_legacy_view()}
        self.assertEqual(len(view), 4)
        self.assertEqual(view[1]["创建时间"], "2026-10-25T10:00:00+08:00")
        self.assertEqual(view[2]["创建时间"], "2026-10-24")
        self.assertEqual(view[3]["创建时间"], "2026-13-40 坏值")
        self.assertEqual(view[4]["创建时间"], "2026-11-01T09:00:00")


if __name__ == "__main__":
    unittest.main()
