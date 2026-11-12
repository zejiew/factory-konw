# -*- coding: utf-8 -*-
"""
删除 / 回收站 自测。

重点：
  - 删除后不再计入结存，也不出现在默认列表
  - 删除可恢复，恢复后结存回到原样
  - 彻底删除只对回收站里的记录生效（防止误删活跃数据）
  - 作废和删除是两回事，互不干扰
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import Store

ok = 0
fail = []


def check(cond, msg):
    global ok
    if cond:
        ok += 1
        print("  ok   ", msg)
    else:
        fail.append(msg)
        print("  FAIL ", msg)


tmp = tempfile.mkdtemp()
s = Store(os.path.join(tmp, "d.db"))

m = s.add_material("测试料", spec="A", unit="个")
f = s.add_factory("测试厂")

r1 = s.add_record("IN", m, f, 100, batch="R1")
r2 = s.add_record("IN", m, f, 200, batch="R2")
r3 = s.add_record("OUT", m, f, 50, batch="R3")

# ---------------- 初始
print("\n[初始状态]")
check(s.balance(m, f) == 250, "结存 100+200-50 = 250")
check(len(s.list_records()) == 3, "列表 3 条")
check(s.deleted_record_count() == 0, "回收站为空")

# ---------------- 删除一条
print("\n[删除一条入库]")
n = s.delete_records([r2])
check(n == 1, "删除返回 1")
check(s.balance(m, f) == 50, "结存变成 100-50 = 50（这 200 不算了）")
check(len(s.list_records()) == 2, "默认列表只剩 2 条")
check(s.deleted_record_count() == 1, "回收站里有 1 条")
check(len(s.list_records(only_deleted=True)) == 1, "回收站查询只返回这 1 条")

# 已删除的不该出现在默认查询
rows = s.list_records()
check(all(r["id"] != r2 for r in rows), "默认列表里没有已删除的那条")

# 总览也不该算它
ov = [x for x in s.stock_overview() if x["material"]["id"] == m][0]
check(abs(ov["total"] - 50) < 1e-6, "总览结存 50")

# material_total 也要排除
check(s.material_total(m) == 50, "material_total 为 50")

# stats 的流水数
st = s.stats()
check(st["records"] == 2, "统计里有效流水 2 条")

# ---------------- 恢复
print("\n[从回收站恢复]")
n = s.restore_records([r2])
check(n == 1, "恢复返回 1")
check(s.balance(m, f) == 250, "结存回到 250")
check(len(s.list_records()) == 3, "列表回到 3 条")
check(s.deleted_record_count() == 0, "回收站空了")

# ---------------- 批量删除
print("\n[批量删除]")
n = s.delete_records([r1, r3])
check(n == 2, "一次删 2 条")
check(s.balance(m, f) == 200, "结存只剩 200")
check(s.deleted_record_count() == 2, "回收站 2 条")

# ---------------- 彻底删除
print("\n[彻底删除]")
n = s.purge_records([r1])
check(n == 1, "彻底删除 1 条")
check(s.deleted_record_count() == 1, "回收站剩 1 条")

# 彻底删除只对回收站里的生效
n = s.purge_records([r3])
check(n == 1, "回收站里的 r3 也能彻底删")
n = s.purge_records([r2])
check(n == 0, "活跃记录（r2）不能被彻底删除 —— 返回 0")

row = s.record_by_id(r1)
check(row is None, "彻底删除后记录真的没了")

# ---------------- 作废 vs 删除
print("\n[作废和删除互不干扰]")
check(s.record_by_id(r2)["voided"] == 0, "r2 未作废")
s.void_record(r2, True)
check(s.record_by_id(r2)["voided"] == 1, "r2 已作废")
check(s.record_by_id(r2)["deleted"] == 0, "但没被删除")
check(s.balance(m, f) == 0, "作废后结存 0")
# 作废的记录仍在列表里（默认 include_voided=True）
rows = s.list_records(include_voided=True)
check(any(r["id"] == r2 for r in rows), "作废的记录仍能在列表里看到")
# 删除作废的记录
s.delete_records([r2])
check(len(s.list_records(include_voided=True)) == 0, "删除后列表空了")

# 恢复删除后，它还是作废状态（两个标记独立）
s.restore_records([r2])
row = s.record_by_id(r2)
check(row["deleted"] == 0 and row["voided"] == 1, "恢复后仍是作废状态，两个标记独立")

# ---------------- 非法输入
print("\n[边界情况]")
check(s.delete_records([]) == 0, "空列表返回 0")
check(s.delete_records(["", None]) == 0, "空 id 被忽略")
check(s.delete_records([99999]) == 0, "不存在的 id 返回 0")
check(s.restore_records([r1]) == 0, "恢复一个不存在的 id 返回 0")

s.close()

# ---------------- 同步：删除要能传播
print("\n[多端：删除状态能同步]")
from sync import SyncEngine

A = Store(os.path.join(tmp, "A.db"))
B = Store(os.path.join(tmp, "B.db"))
ea = SyncEngine(A)
eb = SyncEngine(B)

ma = A.add_material("料", spec="x", unit="个")
fa = A.add_factory("厂")
ra = A.add_record("IN", ma, fa, 300, batch="SYNC-1")
A.add_record("IN", ma, fa, 100, batch="SYNC-2")

# A 上传 -> B 下载
eb.merge(ea.export_payload())
check(len(B.list_records()) == 2, "B 拿到 2 条")
check(B.balance(1, 1) == 400, "B 结存 400")

# A 删掉一条 -> B 同步
A.delete_records([ra])
eb.merge(ea.export_payload())
check(len(B.list_records()) == 1, "B 同步后只剩 1 条（删除传播过去了）")
check(B.balance(1, 1) == 100, "B 结存变成 100")

# B 端这条记录被标记为 deleted
row = B.conn.execute("SELECT deleted FROM records WHERE batch='SYNC-1'").fetchone()
check(row is not None and row["deleted"] == 1, "B 端那条被正确标记为已删除")

# 被删的不会从 B 反向复活到 A
ea.merge(eb.export_payload())
check(len(A.list_records()) == 1, "反向合并不会让已删的复活")
check(A.balance(1, 1) == 100, "A 结存仍是 100")

A.close()
B.close()

print("\n通过 %d 项，失败 %d 项" % (ok, len(fail)))
if fail:
    for x in fail:
        print("  ✗", x)
    sys.exit(1)
print("全部通过 ✓")
