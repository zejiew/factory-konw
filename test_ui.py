# -*- coding: utf-8 -*-
"""界面冒烟测试：把窗口真正建起来，跑一遍主要刷新逻辑，确认不报错。"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import tkinter.messagebox as mb
import tkinter.filedialog as fd

# 静音所有弹窗，避免测试卡住
for name in ("showinfo", "showwarning", "showerror"):
    setattr(mb, name, lambda *a, **k: None)
mb.askyesno = lambda *a, **k: True
fd.asksaveasfilename = lambda *a, **k: ""

tmp = tempfile.mkdtemp()
db.DB_PATH = os.path.join(tmp, "smoke.db")
db.DATA_DIR = tmp
db.BACKUP_DIR = os.path.join(tmp, "backups")

import ui

fail = []


def step(msg, fn):
    try:
        fn()
        print("  ok   ", msg)
    except Exception as ex:
        import traceback
        print("  FAIL ", msg, "->", ex)
        traceback.print_exc()
        fail.append(msg)


app = ui.App()
app.update_idletasks()
app.update()
print("窗口构建完成: %sx%s" % (app.winfo_width(), app.winfo_height()))

# --- 造点数据走一遍界面路径
s = app.store
mid = s.add_material("不锈钢板", spec="304/2mm", unit="千克", min_qty=100)
mid_b = s.add_material("包装膜", spec="PE", unit="卷", min_qty=50)
f1 = s.add_factory("东莞A厂", contact="张三")
f2 = s.add_factory("惠州B厂")
s.add_record("IN", mid, f1, 500, batch="PO001", operator="张三")
s.add_record("IN", mid, f2, 120, batch="PO002")
s.add_record("OUT", mid, f1, 80, note="第一批生产")
s.add_record("IN", mid_b, f1, 30)
s.add_record("ADJUST", mid, f2, 0, target_qty=115)

def check_rows():
    n = len(app.tv_stock.get_children())
    print("       总览父行:", n)
    assert n == 2, "应有 2 种材料"


def check_today():
    n = len(app.tv_today.get_children())
    print("       今日记录:", n)
    assert n == 8, "今日应有 8 条（5 条造数 + 3 条提交）"


step("刷新全部", app.refresh_all)
step("库存总览行数", check_rows)
step("库存搜索", lambda: (app.v_stock_kw.set("不锈钢"), app.refresh_stock(),
                       print("       搜索后:", len(app.tv_stock.get_children()))))
step("库存排序切换", lambda: (app.v_stock_sort.set("材料名称"), app.refresh_stock(),
                          app.v_stock_sort.set("结存从多到少"), app.refresh_stock()))
step("低库存提示", lambda: print("       低库存标签:", repr(app.lbl_low.cget("text"))))

step("出入库-预选材料", lambda: (
    app.v_q_material.set(list(app.mat_map.keys())[0]),
    app.v_q_factory.set(list(app.fac_map.keys())[0]),
    app._on_quick_material_change(),
    print("       结存提示:", app.lbl_balance.cget("text"))))

step("出入库-提交入库", lambda: (
    app.v_q_type.set("IN"), app.v_q_qty.set("66"), app.submit_quick()))
step("出入库-提交消耗", lambda: (
    app.v_q_type.set("OUT"), app.v_q_qty.set("16"), app.submit_quick()))
step("出入库-盘点", lambda: (
    app.v_q_type.set("ADJUST"), app.v_q_qty.set("40"), app.submit_quick()))
step("今天列表", check_today)

step("流水-今天", lambda: (app._set_range(0), print("       今日流水:", len(app.tv_rec.get_children()))))
step("流水-本月", lambda: (app._set_range(1), print("       本月流水:", len(app.tv_rec.get_children()))))
step("流水-筛选消耗", lambda: (app.v_f_type.set("消耗"), app.refresh_records(),
                          print("       消耗记录:", len(app.tv_rec.get_children())),
                          app.v_f_type.set("全部"), app.refresh_records()))
step("流水-筛选代工厂", lambda: (
    app.v_f_factory.set("惠州B厂"), app.refresh_records(),
    print("       B厂记录:", len(app.tv_rec.get_children())),
    app.v_f_factory.set("全部"), app.refresh_records()))

step("材料字典", lambda: print("       材料行:", len(app.tv_mat.get_children())))
step("代工厂页", lambda: print("       工厂行:", len(app.tv_fac.get_children())))

step("停用材料", lambda: (s.set_material_active(mid_b, False), app.refresh_all(),
                       print("       下拉可选:", len(app.mat_map))))
step("恢复材料", lambda: (s.set_material_active(mid_b, True), app.refresh_all()))

step("作废一条流水", lambda: (
    app._set_range(2),
    app.tv_rec.selection_set(app.tv_rec.get_children()[0]),
    app.void_selected_records(True),
    print("       作废后记录数:", len(app.tv_rec.get_children()))))

step("导出库存 CSV", lambda: app.export_stock())
step("备份", lambda: app.do_backup())

app.update()
print("\n结果：失败 %d 项" % len(fail))
for f in fail:
    print("   ✗", f)

app.store.close()
app.destroy()
sys.exit(1 if fail else 0)
