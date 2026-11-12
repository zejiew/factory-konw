# -*- coding: utf-8 -*-
"""
删除代工厂 自测。

重点：
  - 删除后代工厂不再出现在列表和下拉框，但可在「显示已删除」里看到
  - 删除可恢复，恢复后回到在用状态
  - 名下还有有效流水的厂不允许删除（防止账变无所属）
  - 作废/删除掉名下流水之后就能删了
  - 「停用」和「删除」是两回事，互不干扰
  - 同名厂重新新增时是复活，不会撞唯一索引
  - 删除状态能同步到另一台电脑，且不会被反向复活
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

# ---------------- 初始
print("\n[初始状态]")
f1 = s.add_factory("东莞A厂", contact="张三")
f2 = s.add_factory("惠州B厂")
f3 = s.add_factory("无人的空厂")
check(len(s.list_factories(active_only=False)) == 3, "3 个厂都在列表里")
check(s.deleted_factory_count() == 0, "没有已删除的厂")

m = s.add_material("测试料", spec="A", unit="个")
s.add_record("IN", m, f1, 100, batch="R1")

# ---------------- 删除一个没流水的厂
print("\n[删除空厂]")
n = s.delete_factory(f3)
check(n == 1, "删除返回 1")
check(len(s.list_factories(active_only=False)) == 2, "默认列表只剩 2 个厂")
check(s.deleted_factory_count() == 1, "已删除计数为 1")
check(len(s.list_factories(active_only=False, include_deleted=True)) == 3,
      "勾上「显示已删除」能看到 3 个")
gone = [f for f in s.list_factories(active_only=False, include_deleted=True)
        if f["id"] == f3][0]
check(int(gone["deleted"]) == 1, "被删的厂 deleted=1")
check(int(gone["active"]) == 0, "被删的厂同时不是「在用」状态")
check(all(f["id"] != f3 for f in s.list_factories(active_only=True)),
      "在用的下拉框里没有它")
check(all(fid != f3 for fid, _ in s.factory_options()), "factory_options 里也没有它")

# ---------------- 恢复
print("\n[恢复]")
n = s.restore_factory(f3)
check(n == 1, "恢复返回 1")
check(s.deleted_factory_count() == 0, "已删除计数回到 0")
back = s.factory_by_id(f3)
check(int(back["deleted"]) == 0 and int(back["active"]) == 1, "恢复后是在用状态")
check(len(s.list_factories()) == 3, "下拉框里又能看到了")

# 恢复一个没删过的 → 返回 0
check(s.restore_factory(f1) == 0, "恢复一个没删过的厂返回 0")

# ---------------- 有有效流水的厂不许删
print("\n[有流水的厂不能删]")
try:
    s.delete_factory(f1)
    check(False, "有流水时应该拒绝删除")
except ValueError as ex:
    check("还有" in str(ex) and "条有效记录" in str(ex), "拒绝并提示还有几条有效记录")
check(len(s.list_factories()) == 3, "拒绝后厂还在")
check(s.factory_by_id(f1)["deleted"] == 0, "拒绝后没被打上删除标记")
check(s.balance(m, f1) == 100, "结存不受影响")

# ---------------- 作废流水后可以删
print("\n[作废流水后可以删]")
rows = s.list_records(factory_id=f1)
s.void_record(rows[0]["id"], True)
check(s.factory_live_record_count(f1) == 0, "作废后有效流水为 0")
check(s.factory_record_count(f1) == 1, "但历史流水那条还在（计入 record_count）")
n = s.delete_factory(f1)
check(n == 1, "作废流水后可以删厂了")
check(s.factory_by_id(f1)["deleted"] == 1, "厂已标记删除")
check(s.balance(m, f1) == 0, "作废后结存本来就是 0")
# 历史流水还在，删除流水后仍能被查到（联表要能找到厂名）
rows = s.list_records(factory_id=f1, include_voided=True)
check(len(rows) == 1 and rows[0]["factory_name"] == "东莞A厂",
      "已删厂的历史流水仍能查到厂名，不会变成空白")

# ---------------- 删除流水后仍然可以（历史记录保留）
print("\n[历史记录保留]")
s.delete_records([rows[0]["id"]])
check(len(s.list_records(factory_id=f1)) == 0, "流水删除后列表为空")
check(s.factory_record_count(f1) == 1, "厂名下仍记着 1 条历史流水（不真删）")

# ---------------- 重复删除
print("\n[边界情况]")
check(s.delete_factory(f1) == 0, "已经删过的厂再删返回 0")
check(s.delete_factory(99999) == 0, "不存在的厂返回 0")
check(s.restore_factory(99999) == 0, "恢复不存在的厂返回 0")

# 已删除的厂不能再用来记账（f1 此时是已删除状态）
try:
    s.add_record("IN", m, f1, 10)
    check(False, "已删除的厂应该拒绝记账")
except ValueError as ex:
    check("已经删除" in str(ex), "已删除的厂记账被拒绝")
check(s.balance(m, f1) == 0, "被拒后结存没变")

# ---------------- 停用 vs 删除
print("\n[停用和删除是两回事]")
s.set_factory_active(f2, False)
check(s.factory_by_id(f2)["active"] == 0, "停用后 active=0")
check(s.factory_by_id(f2)["deleted"] == 0, "但 deleted 还是 0")
# 此时：f1 已删除、f2 停用、f3 在用
names_stopped = [f["name"] for f in s.list_factories(active_only=False)]
check("惠州B厂" in names_stopped, "停用的厂仍在列表里（能看能恢复）")
names_active = [f["name"] for f in s.list_factories(active_only=True)]
check("惠州B厂" not in names_active, "但在用的下拉框里没有它")
check("东莞A厂" not in names_stopped, "而删除的厂连列表里都没有")
check(len(names_stopped) == 2, "列表里剩「停用的 B 厂 + 在用的空厂」2 个")
s.set_factory_active(f2, True)
check(s.factory_by_id(f2)["active"] == 1, "重新启用成功")

# ---------------- 同名厂重新新增 → 复活，不撞唯一索引
print("\n[同名厂重新新增]")
f1b = s.add_factory("东莞A厂", contact="李四")
check(f1b == f1, "同名厂拿到的是原来那条（复活，不是新建）")
row = s.factory_by_id(f1)
check(int(row["deleted"]) == 0 and int(row["active"]) == 1, "复活后是在用状态")
check(row["contact"] == "李四", "新增时填的联系人被写入")
check(len(s.list_factories(active_only=False, include_deleted=True)) == 3,
      "没有多出一条重复的厂")

# 已停用的同名厂新增 → 也是复活
s.set_factory_active(f2, False)
f2b = s.add_factory("惠州B厂")
check(f2b == f2 and s.factory_by_id(f2)["active"] == 1, "停用的同名厂新增时被重新启用")

# 在用的同名厂新增 → 仍然拦截
try:
    s.add_factory("惠州B厂")
    check(False, "在用的重名厂应该被拦截")
except ValueError:
    check(True, "在用的重名厂被拦截")

s.close()

# ---------------- 同步：删除要能传播，且不能被反向复活
print("\n[多端同步]")
from sync import SyncEngine

A = Store(os.path.join(tmp, "A.db"))
B = Store(os.path.join(tmp, "B.db"))
ea = SyncEngine(A)
eb = SyncEngine(B)

fa = A.add_factory("同步厂甲")
fb = A.add_factory("同步厂乙")
ma = A.add_material("料", spec="x", unit="个")
A.add_record("IN", ma, fa, 10, batch="S1")

eb.merge(ea.export_payload())
check(len(B.list_factories()) == 2, "B 拿到 2 个厂")

# A 删掉厂甲（先作废它的流水）→ 同步
for r in A.list_records(factory_id=fa):
    A.void_record(r["id"], True)
A.delete_factory(fa)
eb.merge(ea.export_payload())
check(len(B.list_factories()) == 1, "B 同步后只剩 1 个厂")
row = B.conn.execute("SELECT deleted FROM factories WHERE name='同步厂甲'").fetchone()
check(row is not None and row["deleted"] == 1, "B 端那个厂被标记为已删除")
check(all(f["name"] != "同步厂甲" for f in B.list_factories()), "B 的厂列表里没有它")

# 反向合并不会让它复活
ea.merge(eb.export_payload())
check(all(f["name"] != "同步厂甲" for f in A.list_factories()),
      "反向合并不会让已删的厂复活")
check(A.deleted_factory_count() == 1, "A 端仍记着它被删过")

A.close()
B.close()

print("\n通过 %d 项，失败 %d 项" % (ok, len(fail)))
if fail:
    for x in fail:
        print("  ✗", x)
    sys.exit(1)
print("全部通过 ✓")
