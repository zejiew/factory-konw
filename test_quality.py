# -*- coding: utf-8 -*-
"""
良品/不良品功能自测。

重点验证一条铁律：
    仓库总数 == 良品 + 不良品 (+ 未分品质)
不管怎么记，这个等式都不能破。
"""

import os
import sys
import sqlite3
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import (Store, QUALITY_GOOD, QUALITY_DEFECT, QUALITY_LABELS,
                fmt_qty, TYPE_LABELS)

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
s = Store(os.path.join(tmp, "q.db"))

# ---------------- 默认不区分
print("\n[默认行为：不区分品质]")
m0 = s.add_material("普通料", spec="A", unit="个")
check(s.material_by_id(m0)["quality_split"] == 0, "新材料默认不区分品质")
f = s.add_factory("测试厂")
s.add_record("IN", m0, f, 100)
check(s.balance(m0, f) == 100, "普通料结存 100")
check(s.balance(m0, f, QUALITY_GOOD) == 0, "普通料没有良品专项口径")
ov = [x for x in s.stock_overview() if x["material"]["id"] == m0][0]
check(ov["total"] == 100 and ov["good"] == 0 and ov["defect"] == 0,
      "普通料总览：总数 100，良品/不良品均为 0")

# ---------------- 启用区分
print("\n[启用良品/不良品]")
m1 = s.add_material("分品质料", spec="B", unit="个", quality_split=1)
check(s.material_by_id(m1)["quality_split"] == 1, "quality_split 保存成功")

s.add_record("IN", m1, f, 100, quality=QUALITY_GOOD)
s.add_record("IN", m1, f, 20, quality=QUALITY_DEFECT)
check(s.balance(m1, f, QUALITY_GOOD) == 100, "良品 100")
check(s.balance(m1, f, QUALITY_DEFECT) == 20, "不良品 20")
check(s.balance(m1, f) == 120, "总数 = 100 + 20 = 120")

# ---------------- 铁律：总数 = 良品 + 不良品
print("\n[核心约束：总数 = 良品 + 不良品]")
s.add_record("OUT", m1, f, 30, quality=QUALITY_GOOD)
s.add_record("OUT", m1, f, 5, quality=QUALITY_DEFECT)
g = s.balance(m1, f, QUALITY_GOOD)
d = s.balance(m1, f, QUALITY_DEFECT)
t = s.balance(m1, f)
check(g == 70, "消耗后良品 70")
check(d == 15, "消耗后不良品 15")
check(abs(t - (g + d)) < 1e-9, "总数 %s == 良品 %s + 不良品 %s" % (t, g, d))

g2, d2, t2 = s.balance_split(m1, f)
check((g2, d2, t2) == (70, 15, 85), "balance_split 返回 (70, 15, 85)")

# ---------------- 盘点按品质算差额
print("\n[盘点：按同品质算差额]")
before_g = s.balance(m1, f, QUALITY_GOOD)
rid = s.add_record("ADJUST", m1, f, 0, target_qty=60, quality=QUALITY_GOOD)
after_g = s.balance(m1, f, QUALITY_GOOD)
check(after_g == 60, "良品盘点后 60")
rec = s.record_by_id(rid)
check(abs(rec["delta"] - (60 - before_g)) < 1e-9,
      "盘点差额只针对良品：%s" % rec["delta"])
check(s.balance(m1, f, QUALITY_DEFECT) == 15, "不良品未受良品盘点影响")

# 盘不良品不应影响良品
s.add_record("ADJUST", m1, f, 0, target_qty=10, quality=QUALITY_DEFECT)
check(s.balance(m1, f, QUALITY_DEFECT) == 10, "不良品盘点后 10")
check(s.balance(m1, f, QUALITY_GOOD) == 60, "良品仍是 60")

# ---------------- 没开品质的材料传 quality 应被忽略
print("\n[未开品质的材料，quality 被忽略]")
before = s.balance(m0, f)
s.add_record("IN", m0, f, 50, quality=QUALITY_GOOD)
after = s.balance(m0, f)
check(after - before == 50, "普通料仍按总数记")
row = s.conn.execute(
    "SELECT quality FROM records WHERE material_id=? ORDER BY id DESC LIMIT 1",
    (m0,)).fetchone()
check(row["quality"] == "", "普通料的 quality 字段被清空")

# ---------------- 作废不影响等式
print("\n[作废后等式仍成立]")
rid2 = s.add_record("IN", m1, f, 40, quality=QUALITY_DEFECT)
s.void_record(rid2, True)
g3, d3, t3 = s.balance_split(m1, f)
check(abs(t3 - (g3 + d3)) < 1e-9, "作废后总数 = 良品 + 不良品")

# ---------------- 总览结构
print("\n[总览汇总]")
ov = [x for x in s.stock_overview() if x["material"]["id"] == m1][0]
check(abs(ov["total"] - (ov["good"] + ov["defect"])) < 1e-9,
      "总览：total = good + defect")
child = ov["children"][0]
check("good" in child and "defect" in child and "other" in child,
      "每个厂下面都带 good/defect/other")

# ---------------- 关闭区分
print("\n[关掉品质区分]")
s.update_material(m1, quality_split=0)
check(s.material_by_id(m1)["quality_split"] == 0, "已关闭")
check(s.balance(m1, f) == s.material_total(m1), "历史数据保留，总数不变")
g4, d4, t4 = s.balance_split(m1, f)
check(abs(t4 - (g4 + d4)) < 1e-9, "关闭后等式依然成立")

# ---------------- 非法品质值
print("\n[非法输入]")
try:
    s.add_record("IN", m1, f, 1, quality="bad_value")
    check(False, "非法品质值应报错")
except ValueError:
    check(True, "")

# ---------------- CSV 导出
print("\n[CSV 导出带品质]")
p1 = os.path.join(tmp, "rec.csv")
s.export_records_csv(p1)
head = open(p1, encoding="utf-8-sig").readline().strip()
check("品质" in head, "流水 CSV 有「品质」列：%s" % head)

p2 = os.path.join(tmp, "stock.csv")
s.export_stock_csv(p2)
head2 = open(p2, encoding="utf-8-sig").readline().strip()
check("良品" in head2 and "不良品" in head2, "库存 CSV 有良品/不良品列：%s" % head2)

s.close()

# ---------------- 迁移：老库补列
print("\n[老库自动迁移]")
old = os.path.join(tmp, "old.db")
c = sqlite3.connect(old)
c.executescript("""
CREATE TABLE materials (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  spec TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
  min_qty REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE factories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  contact TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL,
  material_id INTEGER NOT NULL, factory_id INTEGER NOT NULL, delta REAL NOT NULL,
  batch TEXT NOT NULL DEFAULT '', operator TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', voided INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
""")
c.execute("INSERT INTO materials(name,spec,unit,category,min_qty,note,active,created_at)"
          " VALUES('老料','S','个','',0,'',1,'2026-01-01')")
c.execute("INSERT INTO factories(name,contact,note,active,created_at)"
          " VALUES('老厂','','',1,'2026-01-01')")
c.execute("INSERT INTO records(ts,type,material_id,factory_id,delta,batch,operator,note,voided,created_at)"
          " VALUES('2026-01-01','IN',1,1,77,'','','',0,'2026-01-01')")
c.commit()
c.close()

s2 = Store(old)
mcols = [r[1] for r in s2.conn.execute("PRAGMA table_info(materials)")]
rcols = [r[1] for r in s2.conn.execute("PRAGMA table_info(records)")]
check("quality_split" in mcols, "迁移后 materials 有 quality_split")
check("quality" in rcols, "迁移后 records 有 quality")
check(s2.material_total(1) == 77, "迁移后原有结存 77 保持不变")
check(s2.material_by_id(1)["quality_split"] == 0, "老材料默认不区分品质")
s2.close()

print("\n通过 %d 项，失败 %d 项" % (ok, len(fail)))
if fail:
    for x in fail:
        print("  ✗", x)
    sys.exit(1)
print("全部通过 ✓")
