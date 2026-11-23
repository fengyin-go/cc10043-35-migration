#!/usr/bin/env python3
"""检查点时间字段迁移端到端核对脚本（仅用标准库，python3 -I 运行）。

覆盖任务要求的完整链路：
1. 固定统一基准时刻；
2. 分批回填：日期、带偏移 ISO 时间、坏历史值三种形态；
3. 横跨 30 天边界的日粒度记录 → review，不直接清退；
4. 同日两种精度 → same_day_unknown，不当成两个零点；
5. 带时区确认点用于续采资格判断，与清退共用同一时间依据；
6. 清退扫描中断 → 重开续采 → 回退读法（legacy）→ 再切回；
7. 核对可用点明细、过期结果、迁移前原始值；
8. 新创建入口保存带时区时间。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8017/api"
NOW = "2026-11-23T12:00:00+00:00"

PASS = 0
FAIL = 0


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode()}") from exc


def check(name: str, condition: bool, detail: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} :: {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    # 0. 初始状态：默认混合读法
    section("0. 初始迁移状态")
    prog = call("GET", "/record-checkpoint-migration/progress")
    print(" ", json.dumps(prog, ensure_ascii=False))
    check("默认混合读法", prog["read_mode"] == "mixed")
    check("检查点共9条（3旧+6演练）", prog["total"] == 9, prog["total"])
    check("9条全部待回填", prog["pending"] == 9, prog)

    # 1. 固定统一基准时刻（续采与清退共用）
    section("1. 固定统一基准时刻")
    r = call("POST", "/record-checkpoint-migration/reference-now", {"now": NOW})
    check("基准时刻固定成功", r["ok"] and r["reference_now"] == NOW, r)

    # 2. 分批回填（每批2条），覆盖三种形态
    section("2. 分批回填（limit=2）")
    b1 = call("POST", "/record-checkpoint-migration/backfill", {"limit": 2})
    print(" ", json.dumps(b1, ensure_ascii=False))
    check("第1批处理2条", b1["batch_size"] == 2 and b1["migrated_ids"] == [1, 2], b1)
    check("第1批无隔离", b1["quarantined"] == [], b1)

    b2 = call("POST", "/record-checkpoint-migration/backfill", {"limit": 2})
    check("第2批处理id3、id4", b2["migrated_ids"] == [3, 4], b2)

    b3 = call("POST", "/record-checkpoint-migration/backfill", {"limit": 2})
    print(" ", json.dumps(b3, ensure_ascii=False))
    check("第3批迁移id5（日粒度）", b3["migrated_ids"] == [5], b3)
    check("第3批隔离id6（坏历史值）", [q["id"] for q in b3["quarantined"]] == [6], b3)
    check("隔离原因可读", b3["quarantined"][0]["reason"] is not None, b3)

    b4 = call("POST", "/record-checkpoint-migration/backfill", {"limit": 2})
    check("第4批迁移id7、id8", b4["migrated_ids"] == [7, 8], b4)

    b5 = call("POST", "/record-checkpoint-migration/backfill", {"limit": 2})
    print(" ", json.dumps(b5, ensure_ascii=False))
    check("第5批隔离id9（裸时间无时区）", [q["id"] for q in b5["quarantined"]] == [9], b5)
    check("回填清零", b5["remaining"] == 0 and b5["batch_size"] == 1, b5)

    b6 = call("POST", "/record-checkpoint-migration/backfill", {"limit": 2})
    check("回填幂等：无新批次可处理", b6["batch_size"] == 0, b6)

    prog = call("GET", "/record-checkpoint-migration/progress")
    print(" ", json.dumps(prog, ensure_ascii=False))
    check("7条迁移、2条隔离", prog["migrated"] == 7 and prog["quarantined"] == 2, prog)

    # 3. 隔离区明细
    section("3. 坏历史值隔离区")
    q = call("GET", "/record-checkpoint-migration/quarantine")
    print(" ", json.dumps(q, ensure_ascii=False))
    check("隔离区2条", q["total"] == 2, q)
    raw_bad = {item["来源记录ID"]: item["原始值"] for item in q["items"]}
    check("保留坏值原文6", raw_bad.get(6) == "2026-13-40 无法解析的坏历史值", raw_bad)
    check("保留坏值原文9（裸时间）", raw_bad.get(9) == "2026-11-01T09:00:00", raw_bad)
    check("隔离状态待核查", all(i["处理状态"] == "待核查" for i in q["items"]), q)

    # 4. 可用点明细 + 精度事实
    section("4. 可用点明细（混合读法）")
    pts = call("GET", "/record-checkpoint-migration/available-points?task=" +
               urllib.parse.quote("订单流接入"))
    by_id = {item["id"]: item for item in pts["items"]}
    print(" ", json.dumps(by_id, ensure_ascii=False, indent=1))
    check("订单流3个检查点", pts["total"] == 3, pts)
    check("id4 是instant精度、保留+08:00偏移信息",
          by_id[4]["precision"] == "instant" and by_id[4]["usable"], by_id[4])
    check("id4 时刻换算为UTC 02:00",
          by_id[4]["resolved_instant"] == "2026-10-25T02:00:00+00:00", by_id[4])
    check("id5 是day精度（保留只有日期的事实）",
          by_id[5]["precision"] == "day", by_id[5])
    check("id5 规范化仍是纯日期", by_id[5]["resolved_instant"] == "2026-10-24", by_id[5])
    check("id8 是day精度（边界日）", by_id[8]["precision"] == "day", by_id[8])

    # 5. 横跨30天清退边界：id5 = 2026-10-24 日粒度（基准 2026-11-23T12:00Z）
    section("5. 横跨30天清退边界的日粒度记录")
    d5 = by_id[5]["expiry"]
    print(" id5:", json.dumps(d5, ensure_ascii=False))
    check("id5（10-24）自然日横跨30天边界 → review，不直接清退",
          d5["decision"] == "review", d5)
    check("id5 年龄窗口横跨30（29.5~30.5）",
          d5["age_window_days"][0] < 30 <= d5["age_window_days"][1], d5)
    check("id5 原因解释了此前是否应清退", "横跨30天清退边界" in d5["reason"], d5)
    d8 = by_id[8]["expiry"]
    print(" id8:", json.dumps(d8, ensure_ascii=False))
    check("id8（10-25）全天都未满30天 → keep", d8["decision"] == "keep", d8)
    check("id4（带时区10-25 02:00 UTC，29.42天）确定保留",
          by_id[4]["expiry"]["decision"] == "keep", by_id[4])
    check("id4 与 id8 同处10-25自然日，但一个instant一个day，未被当成两个零点",
          by_id[4]["precision"] == "instant" and by_id[8]["precision"] == "day")

    # 6. 同日两种精度不可判定先后
    section("6. 同日 instant/day 先后不可判定")
    latest = call("GET", "/record-checkpoint-migration/latest-explained?task=" +
                  urllib.parse.quote("订单流接入"))
    print(" ", json.dumps(latest, ensure_ascii=False, indent=1))
    check("识别出同日不可判定", latest["basis"]["indeterminate_same_day"] is True, latest)
    pair_ids = {(p["left_id"], p["right_id"]) for p in latest["basis"]["indeterminate_pairs"]}
    check("不可判定对包含 id4/id8", (4, 8) in pair_ids or (8, 4) in pair_ids, pair_ids)
    check("同日时优先采用instant精度点 id4", latest["basis"]["chosen_id"] == 4, latest)
    check("选择依据标注来源字段", latest["basis"]["source_field"] == "迁移创建时刻", latest)

    # 7. 续采资格：用迁移后的检查点，与清退共用同一基准
    section("7. 续采资格判断")
    elig = call("GET", "/record-checkpoint-migration/resume-eligibility?task=" +
                urllib.parse.quote("订单流接入"))
    print(" ", json.dumps(elig, ensure_ascii=False, indent=1))
    check("订单流可续采（id4 未过期）", elig["eligible"] is True, elig)
    check("资格判断使用同一基准时刻", elig["reference_now"] == NOW, elig)
    check("资格基于迁移字段", elig["source_field"] == "迁移创建时刻", elig)

    elig2 = call("GET", "/record-checkpoint-migration/resume-eligibility?task=" +
                 urllib.parse.quote("库存流接入"))
    print(" ", json.dumps(elig2, ensure_ascii=False, indent=1))
    # 库存流：id6 隔离（待确认），id7 = 2026-09-01 已确认 → 确定过期
    check("库存流无续采资格（最新确认点已过期）", elig2["eligible"] is False, elig2)
    check("库存流过期依据为instant判断", elig2["expiry"]["decision"] == "expire", elig2)

    # 8. 清退扫描：可中断、凭游标续扫。待确认集合 = id1(过期),id5(边界复核),
    #    id6(坏值复核),id9(裸时间坏值复核)
    section("8. 清退扫描（中断后续扫）")
    s1 = call("POST", "/record-checkpoint-migration/expiry-scan", {"limit": 2})
    print(" ", json.dumps(s1, ensure_ascii=False))
    check("第1批扫描2条", len(s1["scanned"]) == 2, s1)
    check("第1批未结束、游标推进", s1["finished"] is False and s1["cursor"] > 0, s1)
    check("第1批：id1 确定清退、id5 边界复核",
          s1["expired_ids"] == [1] and s1["review_ids"] == [5], s1)
    cursor_after_s1 = s1["cursor"]

    # 模拟扫描中断：先只续扫 1 条，证明游标不回退
    s2 = call("POST", "/record-checkpoint-migration/expiry-scan", {"limit": 1})
    print(" ", json.dumps(s2, ensure_ascii=False))
    check("中断后续扫：游标不回退", s2["cursor"] > cursor_after_s1, (s2, cursor_after_s1))

    # 继续扫到结束，累计所有批次结果
    all_expired = set(s1["expired_ids"]) | set(s2["expired_ids"])
    all_review = set(s1["review_ids"]) | set(s2["review_ids"])
    final = s2
    while True:
        sn = call("POST", "/record-checkpoint-migration/expiry-scan", {"limit": 2})
        all_expired |= set(sn["expired_ids"])
        all_review |= set(sn["review_ids"])
        final = sn
        if sn["finished"]:
            break
    print(" 最终批:", json.dumps(final, ensure_ascii=False))
    check("仅 id1 被清退", all_expired == {1}, all_expired)
    check("id5 边界日转复核、未被清退", 5 in all_review and 5 not in all_expired,
          (all_expired, all_review))
    check("坏值 id6、id9 转复核（隔离不静默清退）",
          {6, 9} <= all_review, all_review)
    check("已确认的 id4/id8 不参与清退扫描",
          4 not in all_expired and 8 not in all_expired, all_expired)

    # 扫描后状态核对
    rows = call("GET", "/record_checkpoint?size=100")
    status_by_id = {int(r["id"]): r["status"] for r in rows["items"]}
    check("id1 状态已置已过期", status_by_id[1] == "已过期", status_by_id)
    check("id5 仍为待确认（等待复核，未被自动清退）",
          status_by_id[5] == "待确认", status_by_id)
    check("id4 仍为已确认", status_by_id[4] == "已确认", status_by_id)
    check("id8 仍为已确认", status_by_id[8] == "已确认", status_by_id)

    # 9. 中断后先重开续采，再回退读法
    section("9. 扫描中断恢复：先重开续采，再回退读法")
    r = call("POST", "/record-checkpoint-migration/expiry-scan/reset", {})
    check("清退游标重置", r["ok"] and r["cursor"] == 0, r)
    elig3 = call("GET", "/record-checkpoint-migration/resume-eligibility?task=" +
                 urllib.parse.quote("订单流接入"))
    check("重开续采：资格仍可判（共用同一基准）", elig3["eligible"] is True, elig3)

    # 10. 回退到 legacy 读法
    section("10. 回退 legacy 读法并核对")
    rm = call("POST", "/record-checkpoint-migration/read-mode", {"mode": "legacy"})
    check("切到legacy", rm["current_mode"] == "legacy" and
          rm["previous_mode"] == "mixed", rm)
    pts_legacy = call("GET", "/record-checkpoint-migration/available-points?task=" +
                      urllib.parse.quote("订单流接入"))
    lb = {item["id"]: item for item in pts_legacy["items"]}
    check("legacy 下全部回退原始文本字段",
          all(i["source_field"] == "创建时间" for i in pts_legacy["items"]),
          pts_legacy)
    check("legacy 下日粒度id5仍解析为day", lb[5]["precision"] == "day", lb[5])
    check("legacy 下id4按原始偏移解析为instant", lb[4]["precision"] == "instant", lb[4])
    check("legacy 下id5（边界日）仍判review，可解释此前是否应清退",
          lb[5]["expiry"]["decision"] == "review", lb[5])
    check("legacy 下id8 仍判keep", lb[8]["expiry"]["decision"] == "keep", lb[8])
    check("legacy 下基准时刻未变", pts_legacy["reference_now"] == NOW, pts_legacy)

    # 11. 兼容接口：迁移前原始值全部可查
    section("11. 迁移前原始值兼容查询")
    raw = call("GET", "/record-checkpoint-migration/raw-values")
    raw_by_id = {int(i["id"]): i for i in raw["items"]}
    check("9条原始值都在", raw["total"] == 9, raw)
    check("原始日期值保留", raw_by_id[5]["创建时间"] == "2026-10-24", raw_by_id[5])
    check("原始带偏移值保留", raw_by_id[4]["创建时间"] == "2026-10-25T10:00:00+08:00",
          raw_by_id[4])
    check("原始坏值保留", raw_by_id[6]["创建时间"] == "2026-13-40 无法解析的坏历史值",
          raw_by_id[6])
    check("原始裸时间值保留", raw_by_id[9]["创建时间"] == "2026-11-01T09:00:00",
          raw_by_id[9])

    # 12. 切回 mixed，再验证 migrated 读法
    section("12. 切回 mixed / 验证 migrated 读法")
    call("POST", "/record-checkpoint-migration/read-mode", {"mode": "mixed"})
    call("POST", "/record-checkpoint-migration/read-mode", {"mode": "migrated"})
    pts_m = call("GET", "/record-checkpoint-migration/available-points?task=" +
                 urllib.parse.quote("物流回传接入"))
    check("migrated 下隔离记录不可用",
          pts_m["items"][0]["usable"] is False and
          pts_m["items"][0]["migration_state"] == "quarantined", pts_m)
    call("POST", "/record-checkpoint-migration/read-mode", {"mode": "mixed"})

    # 13. 新创建入口：带时区时间、迁移字段一次到位
    section("13. 新创建入口保存带时区时间")
    created = call("POST", "/record_checkpoint", {"values": {
        "关联接入任务": "订单流接入", "检查点类型": "偏移量检查点",
        "检查点值": "2048", "创建时间": "2026-11-23T20:00:00+08:00"}})
    check("登记成功", created["ok"], created)
    entry = created["entry"]
    check("新记录迁移状态migrated", entry["迁移状态"] == "migrated", entry)
    check("新记录精度instant", entry["时间精度"] == "instant", entry)
    check("新记录UTC时刻为12:00",
          entry["迁移创建时刻"] == "2026-11-23T12:00:00+00:00", entry)
    check("新记录时区偏移+08:00", entry["时区偏移"] == "+08:00", entry)

    bad_create = call("POST", "/record_checkpoint", {"values": {
        "关联接入任务": "订单流接入", "检查点类型": "偏移量检查点",
        "检查点值": "2049", "创建时间": "not-a-time"}})
    check("坏时间被入口拒绝", bad_create["ok"] is False, bad_create)

    # 缺省创建时间：服务端生成
    auto = call("POST", "/record_checkpoint", {"values": {
        "关联接入任务": "订单流接入", "检查点类型": "偏移量检查点",
        "检查点值": "2050"}})
    check("缺省时间由服务端生成带时区值",
          auto["ok"] and auto["entry"]["时间精度"] == "instant" and
          auto["entry"]["迁移状态"] == "migrated", auto)

    # 14. 检查点链校验：跳过隔离点、报告同日不可判定
    section("14. 检查点链校验与隔离点解释")
    chain = call("GET", "/record-checkpoint-migration/chain/4")
    print(" ", json.dumps(chain, ensure_ascii=False))
    check("链含5个可解析点（原3 + 新建2）", chain["chain_length"] == 5, chain)
    check("无时间顺序异常（同日没有被当成零点误判）",
          all("时间顺序异常" not in gap for gap in chain["gaps"]), chain["gaps"])
    check("报告同日不可判定（id4/id8）",
          any("id=8" in note and "id=4" in note for note in chain["same_day_unknown"]),
          chain["same_day_unknown"])
    check("链校验标注当前读法", chain["read_mode"] == "mixed", chain)

    qpoint = call("GET", "/record-checkpoint-migration/points/6")
    check("隔离点解释为不可用+复核",
          qpoint["usable"] is False and qpoint["expiry"]["decision"] == "review", qpoint)

    chain2 = call("GET", "/record-checkpoint-migration/chain/6")
    check("含坏值的库存流链：坏点被跳过并列出原因",
          any(s["id"] == 6 for s in chain2["skipped_unresolved"]), chain2)

    print(f"\n==== 结果：{PASS} 通过 / {FAIL} 失败 ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
