# -*- coding: utf-8 -*-
"""数据层自测：跑一遍核心账务逻辑，确认结存永远跟流水对得上。"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import Store, fmt_qty, TYPE_LABELS

ok = 0
fail = []


def check(cond, msg):
    global ok
    if cond:
        ok += 1
    else:
        fail.append(msg)
        print("  [FAIL]", msg)


def main():
    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "t.db")
    s = Store(dbp)

    # --- 基础增删
    mid = s.add_material("不锈钢板", spec="304/2mm", unit="千克", min_qty=100)
    f1 = s.add_factory("东莞A厂", contact="张三")
    f2 = s.add_factory("惠州B厂")
    check(mid > 0 and f1 > 0 and f2 > 0, "新增材料/工厂")
    check(len(s.list_materials()) == 1 and len(s.list_factories()) == 2, "列表数量")

    # 重名拦截
    try:
        s.add_material("不锈钢板", spec="304/2mm")
        check(False, "重名材料应被拦截")
    except ValueError:
        check(True, "")

    # 同名不同规格 → 可以是两种材料
    mid2 = s.add_material("不锈钢板", spec="201/1mm", unit="千克")
    check(mid2 != mid, "同名不同规格可共存")

    # --- 入库
    s.add_record("IN", mid, f1, 500, ts="2026-01-05 09:00", batch="PO001")
    s.add_record("IN", mid, f1, 300, ts="2026-01-06 09:00")
    check(abs(s.balance(mid, f1) - 800) < 1e-6, "入库累计 = 800")

    # --- 消耗
    s.add_record("OUT", mid, f1, 250, ts="2026-01-07 10:00")
    check(abs(s.balance(mid, f1) - 550) < 1e-6, "消耗后 = 550")

    # --- 另一厂独立
    s.add_record("IN", mid, f2, 100)
    check(abs(s.balance(mid, f2) - 100) < 1e-6, "B厂 = 100")
    check(abs(s.material_total(mid) - 650) < 1e-6, "材料总 = 650")

    # --- 退回
    s.add_record("RETURN", mid, f1, 50)
    check(abs(s.balance(mid, f1) - 500) < 1e-6, "退回后 = 500")

    # --- 盘点：实盘 480 → 差 -20
    rid = s.add_record("ADJUST", mid, f1, 0, target_qty=480)
    check(abs(s.balance(mid, f1) - 480) < 1e-6, "盘点后 = 480")
    rec = s.record_by_id(rid)
    check(abs(rec["delta"] + 20) < 1e-6, "盘点差额 = -20")

    # 盘点无变化应报错
    try:
        s.add_record("ADJUST", mid, f1, 0, target_qty=480)
        check(False, "盘点无变化应报错")
    except ValueError:
        check(True, "")

    # --- 作废回滚
    s.void_record(rid, True)
    check(abs(s.balance(mid, f1) - 500) < 1e-6, "作废盘点后回到 500")
    s.void_record(rid, False)
    check(abs(s.balance(mid, f1) - 480) < 1e-6, "恢复后回到 480")

    # --- 小数
    s.add_record("IN", mid2, f2, 12.5)
    check(abs(s.balance(mid2, f2) - 12.5) < 1e-6, "小数入库")

    # --- 非法数量
    for bad in (-5, 0):
        try:
            s.add_record("IN", mid, f1, bad)
            check(False, "非正数量应报错 %s" % bad)
        except ValueError:
            check(True, "")

    # --- 安全库存预警（不锈钢板总 580 > 100 不报警；再造一个低于安全库存的）
    check(len(s.low_stock_alerts()) == 0, "高于安全库存时不报警")
    mid3 = s.add_material("包装膜", spec="PE", unit="卷", min_qty=50)
    s.add_record("IN", mid3, f1, 30)
    lows = s.low_stock_alerts()
    check(len(lows) == 1 and lows[0]["material"]["id"] == mid3, "安全库存预警命中 1 条")
    check(abs(lows[0]["qty"] - 30) < 1e-6, "预警里数量正确")

    # --- 总览结构
    ov = s.stock_overview()
    check(len(ov) == 3, "总览三种材料")
    first = [o for o in ov if o["material"]["id"] == mid][0]
    check(len(first["children"]) == 2, "不锈钢板挂在两个厂下面")
    check(abs(first["total"] - 580) < 1e-6, "总览合计 = 480+100")

    # --- 筛选
    check(len(s.list_records(date_from="2026-01-06", date_to="2026-01-06")) == 1, "按日期筛选")
    check(len(s.list_records(rtype="OUT")) == 1, "按类型筛选")
    check(len(s.list_records(keyword="PO001")) == 1, "按关键词筛选")
    check(len(s.list_records(factory_id=f2)) == 2, "按工厂筛选")

    # --- 导出
    csv1 = os.path.join(tmp, "a.csv")
    n = s.export_records_csv(csv1)
    check(n > 0 and os.path.exists(csv1), "导出流水 CSV")
    csv2 = os.path.join(tmp, "b.csv")
    s.export_stock_csv(csv2)
    check(os.path.exists(csv2), "导出库存 CSV")

    # --- 停用 / 启用
    s.set_material_active(mid2, False)
    check(len(s.list_materials()) == 2, "停用后不在默认列表")
    check(len(s.list_materials(active_only=False)) == 3, "含停用的列表还是 3")
    check(s.add_material("不锈钢板", spec="201/1mm") == mid2, "重新添加同名材料会复活原条目")
    check(len(s.list_materials()) == 3, "复活后回到 3")

    # --- 备份
    s.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dst = s.backup()
    check(os.path.exists(dst), "备份文件生成")

    # --- 设置项
    s.set_setting("k", "v")
    check(s.get_setting("k") == "v", "设置项读写")
    s.close()

    # --- 重开库，数据还在
    s2 = Store(dbp)
    check(abs(s2.balance(mid, f1) - 480) < 1e-6, "重开数据库数据仍在")
    s2.close()

    # --- 格式化
    check(fmt_qty(100) == "100", "fmt 整数")
    check(fmt_qty(12.5) == "12.5", "fmt 小数")
    check(fmt_qty(0) == "0", "fmt 零")
    check(fmt_qty(-20) == "-20", "fmt 负数")

    print("\n通过 %d 项，失败 %d 项" % (ok, len(fail)))
    if fail:
        for f in fail:
            print("  ✗", f)
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
