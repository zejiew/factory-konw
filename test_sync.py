# -*- coding: utf-8 -*-
"""
多端同步自测：模拟两台电脑各自记账，然后互相合并。

这是最容易出错的功能，所以测得很细：
  - 各记各的 → 合并后两边都有
  - 同一条被两边改 → 后改的赢
  - 软删除要能传播，不能复活
  - 材料在 A 端叫「钢管」在 B 端也建了同名 → 不能撞车
  - 增量同步只带改动过的
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import Store, QUALITY_GOOD
from db import now_full as _now_full
import db as dd
from sync import SyncEngine

ok = 0
fail = []


def check(cond, msg):
    global ok
    if cond:
        ok += 1
    else:
        fail.append(msg)
        print("  FAIL", msg)


tmp = tempfile.mkdtemp()


def new_device(name):
    s = Store(os.path.join(tmp, name + ".db"))
    return s, SyncEngine(s)


# ============================================================ 场景1：各记各的
print("\n[场景1：两台各记各的，合并后都有]")
A, ea = new_device("A")
B, eb = new_device("B")

ma = A.add_material("不锈钢板", spec="304", unit="千克")
fa = A.add_factory("东莞A厂")
A.add_record("IN", ma, fa, 500, ts="2026-11-01 09:00", batch="A-PO1")

mb = B.add_material("铜线", spec="2mm", unit="米")
fb = B.add_factory("惠州B厂")
B.add_record("IN", mb, fb, 300, ts="2026-11-02 10:00", batch="B-PO1")

check(len(A.list_materials()) == 1 and len(B.list_materials()) == 1, "合并前各有 1 种材料")

# A 的包导入 B
pa = ea.export_payload()
r = eb.merge(pa)
check(len(B.list_materials()) == 2, "B 合并后有 2 种材料（自己的 + A 的）")
check(len(B.list_factories()) == 2, "B 合并后有 2 个厂")
check(len(B.list_records()) == 2, "B 合并后有 2 条流水")

# B 的包导入 A
pb = eb.export_payload()
r2 = ea.merge(pb)
check(len(A.list_materials()) == 2, "A 合并后也有 2 种材料")
check(len(A.list_records()) == 2, "A 合并后也有 2 条流水")

# 结存要对
tot_a = sum(x["total"] for x in A.stock_overview())
tot_b = sum(x["total"] for x in B.stock_overview())
check(abs(tot_a - 800) < 1e-6, "A 端结存合计 800（500+300）")
check(abs(tot_b - 800) < 1e-6, "B 端结存合计 800")

# ============================================================ 场景2：重复合并幂等
print("\n[场景2：同一份包合并两次，不该重复]")
before = len(A.list_records())
ea.merge(pb)
after = len(A.list_records())
check(before == after, "重复合并不会产生重复记录（%d -> %d）" % (before, after))

# ============================================================ 场景3：后改的赢
print("\n[场景3：同一条被两端修改，后改的赢]")
# 取 A 端第一条流水，改它的备注（模拟 A 端编辑）
rec_a = A.list_records()[0]
uid = rec_a["uid"]
A.conn.execute("UPDATE records SET note=?, updated_at=? WHERE uid=?",
               ("A端后改的", "2026-11-10 12:00:00", uid))
A.conn.commit()

# B 端那条还是老时间
B.conn.execute("UPDATE records SET note=?, updated_at=? WHERE uid=?",
               ("B端早改的", "2026-11-05 12:00:00", uid))
B.conn.commit()

# A 合并 B 的包：B 的版本更新 -> 用 B
# 等等，B 是 11-05，A 是 11-10，所以 A 更新，合并后应保留 A 的
ea.merge(eb.export_payload())
row = A.conn.execute("SELECT note FROM records WHERE uid=?", (uid,)).fetchone()
check(row["note"] == "A端后改的", "A 端时间更新，保留 A 的（实际: %s）" % row["note"])

# 反过来：B 合并 A 的包，应该拿到 A 的版本
eb.merge(ea.export_payload())
row = B.conn.execute("SELECT note FROM records WHERE uid=?", (uid,)).fetchone()
check(row["note"] == "A端后改的", "B 端合并后也变成 A 的版本（实际: %s）" % row["note"])

# ============================================================ 场景4：作废要传播
print("\n[场景4：作废标记要传播]")
rec = A.list_records()[0]
rid_uid = rec["uid"]
A.void_record(int(rec["id"]), True)
check(A.record_by_id(rec["id"])["voided"] == 1, "A 端已作废")

eb.merge(ea.export_payload())
row = B.conn.execute("SELECT voided FROM records WHERE uid=?", (rid_uid,)).fetchone()
check(row["voided"] == 1, "B 端也变成已作废")

# ============================================================ 场景5：同名材料不撞车
print("\n[场景5：两端各建了同名材料]")
C, ec = new_device("C")
D, ed = new_device("D")
C.add_material("钢管", spec="DN50", unit="根")
D.add_material("钢管", spec="DN50", unit="根")
# 两边 uid 不同，但 name+spec 相同
r = ed.merge(ec.export_payload())
check(len(D.list_materials()) == 1, "同名同规格不会变成两条，仍为 1 条")
check(r["materials"]["skip"] >= 1, "冲突的那条被记为 skip")

# ============================================================ 场景6：增量同步
print("\n[场景6：增量只带改动过的]")
E, ee = new_device("E")
me = E.add_material("料E", spec="x", unit="个")
fe = E.add_factory("厂E")
E.add_record("IN", me, fe, 10)
full = ee.export_payload(full=True)
check(len(full["tables"]["records"]) == 1, "全量导出 1 条流水")

# 记下第一条的时间戳，再新增一条
mark = dd.now_full()
import time
time.sleep(1.1)
E.add_record("IN", me, fe, 20, ts="2026-11-20 10:00")
inc = ee.export_payload(full=False, since=mark)
check(len(inc["tables"]["records"]) == 1,
      "增量只含新增的那 1 条（实际 %d）" % len(inc["tables"]["records"]))

# ============================================================ 场景7：预览不改数据
print("\n[场景7：preview 不写库]")
F, ef = new_device("F")
F.add_material("原有料", spec="a", unit="个")
before_n = len(F.list_materials())
pv = ef.preview(pa)
after_n = len(F.list_materials())
check(before_n == after_n, "preview 后数据条数没变")
check(pv["materials"]["add"] >= 1, "预览报告会新增材料")
txt = SyncEngine.summary_text(pv)
check("新增" in txt, "摘要文字可读：%s" % txt[:60])

# ============================================================ 场景8：坏包要被拒
print("\n[场景8：非法同步包]")
for bad in ({}, {"kind": "something-else"}, "not a dict"):
    try:
        ef.merge(bad)
        check(False, "非法包应报错: %r" % (bad,))
    except ValueError:
        check(True, "")

try:
    ef.merge({"kind": "inventory-sync", "version": 999, "tables": {}})
    check(False, "过高版本应报错")
except ValueError:
    check(True, "")

# ============================================================ 场景9：品质数据同步
print("\n[场景9：良品/不良品一起同步]")
G, eg = new_device("G")
mg = G.add_material("分品质料", spec="q", unit="个", quality_split=1)
fg = G.add_factory("厂G")
G.add_record("IN", mg, fg, 100, quality=QUALITY_GOOD)
G.add_record("IN", mg, fg, 20, quality="defect")

H, eh = new_device("H")
eh.merge(eg.export_payload())
mh = [m for m in H.list_materials() if m["name"] == "分品质料"]
check(len(mh) == 1, "H 端拿到该材料")
check(int(mh[0]["quality_split"]) == 1, "quality_split 一起同步过来了")
good = H.conn.execute("SELECT SUM(delta) FROM records WHERE quality='good' AND voided=0").fetchone()[0]
bad = H.conn.execute("SELECT SUM(delta) FROM records WHERE quality='defect' AND voided=0").fetchone()[0]
check(good == 100 and bad == 20, "H 端良品 100 不良品 20（实际 %s / %s）" % (good, bad))

# ============================================================ 场景10：uid 唯一性
print("\n[场景10：uid 不能重复]")
for dev, name in ((A, "A"), (B, "B")):
    dup = dev.conn.execute(
        "SELECT uid, COUNT(*) c FROM records GROUP BY uid HAVING c > 1").fetchall()
    check(len(dup) == 0, "%s 端没有重复 uid" % name)

for d in (A, B, C, D, E, F, G, H):
    d.close()

print("\n通过 %d 项，失败 %d 项" % (ok, len(fail)))
if fail:
    for x in fail:
        print("  ✗", x)
    sys.exit(1)
print("全部通过 ✓")
