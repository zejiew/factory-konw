# -*- coding: utf-8 -*-
"""
代工厂物料台账 —— 界面层（Tkinter 原生窗口）

六个页签：
    库存总览  —— 材料下面挂各厂结存，一眼看全
    出入库    —— 日常记账的地方，选材料选厂填数量就行
    流水明细  —— 全部记录、筛选、作废、导出
    材料字典  —— 材料名称/规格/单位/安全库存
    代工厂    —— 厂名/联系人/备注
    云端备份  —— 可选：把 CSV 快照传到自己的备份柜（不填就不联网）
"""

import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from db import (Store, TYPE_LABELS, COMMON_UNITS, fmt_qty, now_str, today_str,
                material_display, EPS,
                QUALITY_LABELS, QUALITY_GOOD, QUALITY_DEFECT)
import cloud as cloud_mod
from sync import SyncEngine

APP_TITLE = "代工厂物料台账"

C_BG = "#f4f6f9"
C_CARD = "#ffffff"
C_ACCENT = "#2f6feb"
C_TEXT = "#1f2933"
C_MUTED = "#6b7684"
C_IN = "#1e8e3e"
C_OUT = "#d93025"
C_LINE = "#d9dee5"


def icon_candidates():
    """
    找 app.ico 的候选路径。

    三种运行方式都要照顾到：
      1. python ui.py          → 源码目录
      2. PyInstaller 文件夹版   → _MEIPASS（_internal）里
      3. PyInstaller 单文件版   → 同样在 _MEIPASS 里，但 exe 旁边也可能放了一份
    """
    out = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        out.append(os.path.join(base, "app.ico"))
    out.append(os.path.join(base_dir(), "app.ico"))
    if getattr(sys, "frozen", False):
        out.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "app.ico"))
    return out


def base_dir():
    """程序所在目录（打包后是 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1024.0 / 1024.0)


def pick_font():
    from tkinter import font as tkfont
    try:
        fams = set(tkfont.families())
    except Exception:
        fams = set()
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "PingFang SC", "Segoe UI"):
        if name in fams:
            return name
    return "TkDefaultFont"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.store = Store()
        self.font_name = pick_font()

        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.minsize(980, 620)
        self.configure(bg=C_BG)

        # 标题栏左上角那个图标：不设就是 Tk 默认的羽毛
        self._apply_icon()

        # 下拉框映射表：显示文字 -> 数据库 id（页面构建时就要用，先建好）
        self.mat_map = {}
        self.fac_map = {}
        self.store_mat_map = {"全部": None}
        self.store_fac_map = {"全部": None}

        self._init_style()
        self._build_header()
        self._build_notebook()
        self._build_statusbar()

        self.refresh_all()
        self.after(200, self._first_run_hint)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _apply_icon(self):
        """
        把窗口图标换成 app.ico。

        打包成 exe 后 app.ico 会被 PyInstaller 塞进 _MEIPASS，不在 exe 旁边，
        所以先找资源目录，再退回源码目录；两处都没有就安静跳过（不影响启动）。
        """
        for p in icon_candidates():
            try:
                if os.path.exists(p):
                    self.iconbitmap(default=p)
                    return
            except Exception:
                continue

    # ============================================================ 样式

    def _init_style(self):
        f = self.font_name
        self.opt = {
            "font": (f, 10),
            "font_bold": (f, 10, "bold"),
            "font_h1": (f, 16, "bold"),
            "font_h2": (f, 12, "bold"),
            "font_big": (f, 20, "bold"),
        }
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", font=(f, 10), background=C_BG, foreground=C_TEXT)
        st.configure("TFrame", background=C_BG)
        st.configure("Card.TFrame", background=C_CARD)
        st.configure("TLabel", background=C_BG, foreground=C_TEXT)
        st.configure("Card.TLabel", background=C_CARD, foreground=C_TEXT)
        st.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED, font=(f, 9))
        st.configure("CardMuted.TLabel", background=C_CARD, foreground=C_MUTED, font=(f, 9))
        st.configure("H1.TLabel", background=C_BG, foreground=C_TEXT, font=(f, 16, "bold"))
        st.configure("H2.TLabel", background=C_CARD, foreground=C_TEXT, font=(f, 12, "bold"))
        st.configure("Big.TLabel", background=C_CARD, foreground=C_ACCENT, font=(f, 19, "bold"))
        st.configure("TNotebook", background=C_BG, borderwidth=0)
        st.configure("TCheckbutton", font=(f, 10), background=C_CARD,
                     foreground=C_TEXT, indicatorcolor=C_CARD)
        st.map("TCheckbutton",
               background=[("active", C_CARD), ("!active", C_CARD)],
               indicatorcolor=[("selected", C_ACCENT), ("!selected", "#ffffff")],
               foreground=[("disabled", "#a3acb8"), ("!disabled", C_TEXT)])
        st.configure("TNotebook.Tab", padding=(18, 9), font=(f, 10, "bold"))
        st.map("TNotebook.Tab",
               background=[("selected", C_CARD), ("!selected", "#e6eaf0")],
               foreground=[("selected", C_ACCENT), ("!selected", C_MUTED)])
        st.configure("TButton", font=(f, 10), padding=(12, 5))
        st.configure("Accent.TButton", font=(f, 10, "bold"), padding=(14, 6),
                     background=C_ACCENT, foreground="#ffffff", borderwidth=0)
        st.map("Accent.TButton",
               background=[("active", "#255ccd"), ("pressed", "#1f4fb0")],
               foreground=[("disabled", "#cfd8e3")])
        st.configure("Danger.TButton", foreground=C_OUT)
        st.configure("TEntry", padding=4)
        st.configure("TCombobox", padding=4)
        st.configure("Treeview", font=(f, 10), rowheight=28, background=C_CARD,
                     fieldbackground=C_CARD, borderwidth=0)
        st.configure("Treeview.Heading", font=(f, 10, "bold"), background="#eef1f6",
                     foreground=C_TEXT, padding=(8, 6), relief="flat")
        st.map("Treeview", background=[("selected", "#dce8ff")],
               foreground=[("selected", C_TEXT)])
        st.configure("TLabelframe", background=C_BG, borderwidth=1, relief="solid")
        st.configure("TLabelframe.Label", background=C_BG, font=(f, 10, "bold"))

    # ============================================================ 顶部

    def _build_header(self):
        top = tk.Frame(self, bg=C_CARD, height=64)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        # 标题 + 署名 竖排，让 "by zejiew" 贴着标题右下角，不跟右侧按钮抢位置
        brand = tk.Frame(top, bg=C_CARD)
        brand.pack(side="left", padx=20)

        tk.Label(brand, text=APP_TITLE, font=self.opt["font_h1"],
                 bg=C_CARD, fg=C_TEXT).pack(anchor="w")

        sign = tk.Frame(brand, bg=C_CARD)
        sign.pack(anchor="e", pady=(0, 2))
        tk.Label(sign, text="by ", font=(self.font_name, 8),
                 bg=C_CARD, fg="#c3cbd6").pack(side="left")
        tk.Label(sign, text="zejiew", font=(self.font_name, 8),
                 bg=C_CARD, fg="#9aa4b1").pack(side="left")

        self.lbl_stats = tk.Label(top, text="", font=(self.font_name, 9),
                                  bg=C_CARD, fg=C_MUTED)
        self.lbl_stats.pack(side="left", padx=14)

        right = tk.Frame(top, bg=C_CARD)
        right.pack(side="right", padx=14)
        ttk.Button(right, text="备份数据", command=self.do_backup).pack(side="right", padx=4)
        ttk.Button(right, text="导出库存", command=self.export_stock).pack(side="right", padx=4)
        ttk.Button(right, text="刷新", command=self.refresh_all).pack(side="right", padx=4)

        tk.Frame(self, bg=C_LINE, height=1).pack(fill="x", side="top")

    # ============================================================ 页签

    def _build_notebook(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(10, 6))

        self.tab_stock = ttk.Frame(self.nb)
        self.tab_quick = ttk.Frame(self.nb)
        self.tab_records = ttk.Frame(self.nb)
        self.tab_materials = ttk.Frame(self.nb)
        self.tab_factories = ttk.Frame(self.nb)
        self.tab_cloud = ttk.Frame(self.nb)

        self.nb.add(self.tab_stock, text="  库存总览  ")
        self.nb.add(self.tab_quick, text="  出入库  ")
        self.nb.add(self.tab_records, text="  流水明细  ")
        self.nb.add(self.tab_materials, text="  材料字典  ")
        self.nb.add(self.tab_factories, text="  代工厂  ")
        self.nb.add(self.tab_cloud, text="  云端备份  ")

        self._build_stock_tab()
        self._build_quick_tab()
        self._build_records_tab()
        self._build_materials_tab()
        self._build_factories_tab()
        self._build_cloud_tab()

    def _build_statusbar(self):
        bar = tk.Frame(self, bg=C_BG, height=26)
        bar.pack(fill="x", side="bottom")
        self.lbl_status = tk.Label(bar, text="就绪", font=(self.font_name, 9),
                                   bg=C_BG, fg=C_MUTED, anchor="w")
        self.lbl_status.pack(side="left", padx=16)
        tk.Label(bar, text="数据文件：" + self.store.db_path,
                 font=(self.font_name, 8), bg=C_BG, fg="#9aa4b1").pack(side="right", padx=16)

    def set_status(self, msg, color=None):
        self.lbl_status.config(text=msg, fg=color or C_MUTED)

    # ============================================================ 库存总览

    def _build_stock_tab(self):
        root = self.tab_stock

        cards = tk.Frame(root, bg=C_BG)
        cards.pack(fill="x", pady=(10, 8))
        self.card_widgets = {}
        for key, title in (("materials", "在册材料"), ("factories", "代工厂"),
                           ("with_stock", "有结存材料"), ("records", "有效流水")):
            c = tk.Frame(cards, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
            c.pack(side="left", fill="both", expand=True, padx=(0, 10))
            tk.Label(c, text=title, font=(self.font_name, 9), bg=C_CARD, fg=C_MUTED).pack(anchor="w", padx=14, pady=(10, 0))
            v = tk.Label(c, text="0", font=self.opt["font_big"], bg=C_CARD, fg=C_ACCENT)
            v.pack(anchor="w", padx=14, pady=(0, 10))
            self.card_widgets[key] = v

        bar = tk.Frame(root, bg=C_BG)
        bar.pack(fill="x", pady=(0, 8))
        tk.Label(bar, text="搜索", bg=C_BG, font=self.opt["font"]).pack(side="left")
        self.v_stock_kw = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.v_stock_kw, width=26)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _e: self.refresh_stock())
        ttk.Button(bar, text="查询", command=self.refresh_stock).pack(side="left", padx=4)
        ttk.Button(bar, text="清空", command=self._clear_stock_kw).pack(side="left")

        tk.Label(bar, text="排序", bg=C_BG, font=self.opt["font"]).pack(side="left", padx=(20, 4))
        self.v_stock_sort = tk.StringVar(value="结存从多到少")
        cb = ttk.Combobox(bar, textvariable=self.v_stock_sort, state="readonly", width=14,
                          values=["结存从多到少", "材料名称", "最近变动"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_stock())

        self.lbl_low = tk.Label(bar, text="", bg=C_BG, fg=C_OUT, font=(self.font_name, 9, "bold"))
        self.lbl_low.pack(side="right")

        wrap = tk.Frame(root, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)

        cols = ("qty", "last", "note")
        self.tv_stock = ttk.Treeview(wrap, columns=cols, show="tree headings", height=18)
        self.tv_stock.heading("#0", text="材料 / 存放代工厂", anchor="w")
        self.tv_stock.heading("qty", text="结存", anchor="e")
        self.tv_stock.heading("last", text="最近变动", anchor="center")
        self.tv_stock.heading("note", text="说明", anchor="w")
        self.tv_stock.column("#0", width=420, minwidth=240, stretch=True)
        self.tv_stock.column("qty", width=130, anchor="e", stretch=False)
        self.tv_stock.column("last", width=150, anchor="center", stretch=False)
        self.tv_stock.column("note", width=280, anchor="w", stretch=True)

        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_stock.yview)
        self.tv_stock.configure(yscrollcommand=vs.set)
        self.tv_stock.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.tv_stock.tag_configure("odd", background="#fafbfd")
        self.tv_stock.tag_configure("parent", font=(self.font_name, 10, "bold"))
        self.tv_stock.tag_configure("low", foreground=C_OUT)
        self.tv_stock.tag_configure("zero", foreground="#a3acb8")
        self.tv_stock.bind("<Double-1>", self._on_stock_double)

    def _clear_stock_kw(self):
        self.v_stock_kw.set("")
        self.refresh_stock()

    def refresh_stock(self):
        tv = self.tv_stock
        tv.delete(*tv.get_children())
        kw = self.v_stock_kw.get().strip()
        data = self.store.stock_overview(keyword=kw)

        mode = self.v_stock_sort.get()
        if mode == "材料名称":
            data.sort(key=lambda x: x["material"]["name"])
        elif mode == "最近变动":
            data.sort(key=lambda x: x["last_ts"] or "", reverse=True)
        else:
            data.sort(key=lambda x: (-x["total"], x["material"]["name"]))

        lows = {a["material"]["id"] for a in self.store.low_stock_alerts()}

        idx = 0
        for item in data:
            m = item["material"]
            unit = m["unit"] or ""
            total = item["total"]
            split = bool(int(m.get("quality_split") or 0))
            tags = ["parent"]
            if m["id"] in lows:
                tags.append("low")
            label = "  " + m["name"]
            if m["spec"]:
                label += "   " + m["spec"]
            note = "共 %d 个厂" % len(item["children"])
            if split:
                note = "良品 %s ／ 不良品 %s　·　共 %d 个厂" % (
                    fmt_qty(item["good"]), fmt_qty(item["defect"]),
                    len(item["children"]))
            if m["id"] in lows:
                note += "　● 低于安全库存 %s" % fmt_qty(m["min_qty"])
            elif total <= EPS:
                tags.append("zero")
                note += "　（已用完）"
            pid = tv.insert("", "end", text=label,
                            values=(fmt_qty(total) + (" " + unit if unit else ""),
                                    item["last_ts"], note),
                            tags=tuple(tags), open=False)
            for c in item["children"]:
                if abs(c["qty"]) < EPS:
                    continue
                ctags = ("odd",) if idx % 2 else ()
                idx += 1
                qty_txt = fmt_qty(c["qty"]) + (" " + unit if unit else "")
                cnote = ""
                if split:
                    qty_txt = "总数 " + qty_txt
                    cnote = "良品 %s ／ 不良品 %s%s" % (
                        fmt_qty(c["good"]), fmt_qty(c["defect"]),
                        (" ／ 未分 %s" % fmt_qty(c["other"])) if abs(c["other"]) > EPS else "")
                tv.insert(pid, "end", text="     " + c["factory_name"],
                          values=(qty_txt, c["last_ts"], cnote), tags=ctags)

        st = self.store.stats()
        for k, w in self.card_widgets.items():
            w.config(text=str(st.get(k, 0)))

        alerts = self.store.low_stock_alerts()
        self.lbl_low.config(text=("⚠ %d 种材料低于安全库存" % len(alerts)) if alerts else "")

    def _on_stock_double(self, _e):
        sel = self.tv_stock.selection()
        if not sel:
            return
        # 双击材料行 → 跳到收支页并预选该材料
        iid = sel[0]
        text = self.tv_stock.item(iid, "text").strip()
        if self.tv_stock.parent(iid):
            return
        self.nb.select(self.tab_quick)
        name = text.split("   ")[0].strip()
        for disp, mid in self.mat_map.items():
            if disp.startswith(name):
                self.v_q_material.set(disp)
                self._on_quick_material_change()
                break

    # ============================================================ 出入库

    def _build_quick_tab(self):
        root = self.tab_quick

        form = tk.Frame(root, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        form.pack(fill="x", pady=(10, 8), ipady=4)

        head = tk.Frame(form, bg=C_CARD)
        head.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(head, text="记一笔", font=self.opt["font_h2"], bg=C_CARD).pack(side="left")
        self.lbl_balance = tk.Label(head, text="", font=(self.font_name, 11, "bold"),
                                    bg=C_CARD, fg=C_ACCENT)
        self.lbl_balance.pack(side="right")

        self.v_q_type = tk.StringVar(value="IN")
        rbf = tk.Frame(form, bg=C_CARD)
        rbf.pack(fill="x", padx=16, pady=(0, 10))
        for t in ("IN", "OUT", "RETURN", "ADJUST"):
            ttk.Radiobutton(rbf, text=TYPE_LABELS[t], value=t, variable=self.v_q_type,
                            command=self._on_quick_type_change).pack(side="left", padx=(0, 18))

        grid = tk.Frame(form, bg=C_CARD)
        grid.pack(fill="x", padx=16, pady=(0, 6))
        grid.columnconfigure(1, weight=1, minsize=260)
        grid.columnconfigure(3, weight=1, minsize=260)

        def lab(p, t, r, c):
            tk.Label(p, text=t, bg=C_CARD, font=self.opt["font"], fg=C_MUTED).grid(
                row=r, column=c, sticky="w", padx=(0, 8), pady=6)

        self.v_q_material = tk.StringVar()
        self.v_q_factory = tk.StringVar()
        self.v_q_qty = tk.StringVar()
        self.v_q_batch = tk.StringVar()
        self.v_q_operator = tk.StringVar()
        self.v_q_note = tk.StringVar()
        self.v_q_date = tk.StringVar(value=today_str())
        self.v_q_time = tk.StringVar(value=now_str()[11:])
        self.v_q_quality = tk.StringVar(value="良品")

        lab(grid, "材料", 0, 0)
        self.cb_q_material = ttk.Combobox(grid, textvariable=self.v_q_material, state="readonly")
        self.cb_q_material.grid(row=0, column=1, sticky="ew", pady=6, padx=(0, 20))
        self.cb_q_material.bind("<<ComboboxSelected>>", lambda _e: self._on_quick_material_change())

        lab(grid, "代工厂", 0, 2)
        self.cb_q_factory = ttk.Combobox(grid, textvariable=self.v_q_factory, state="readonly")
        self.cb_q_factory.grid(row=0, column=3, sticky="ew", pady=6)
        self.cb_q_factory.bind("<<ComboboxSelected>>", lambda _e: self._on_quick_material_change())

        lab(grid, "数量", 1, 0)
        qf = tk.Frame(grid, bg=C_CARD)
        qf.grid(row=1, column=1, sticky="ew", pady=6, padx=(0, 20))
        ttk.Entry(qf, textvariable=self.v_q_qty, width=14).pack(side="left")
        self.lbl_unit = tk.Label(qf, text="", bg=C_CARD, fg=C_MUTED, font=(self.font_name, 9))
        self.lbl_unit.pack(side="left", padx=8)

        lab(grid, "批次 / 单号", 1, 2)
        ttk.Entry(grid, textvariable=self.v_q_batch).grid(row=1, column=3, sticky="ew", pady=6)

        # 品质：只有勾了「区分良品/不良品」的材料才显示这一行
        self.quality_row = tk.Frame(grid, bg=C_CARD)
        self.quality_row.grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 0))
        tk.Label(self.quality_row, text="品质", bg=C_CARD, fg=C_MUTED,
                 font=self.opt["font"], width=8, anchor="e").pack(side="left", padx=(0, 10))
        self.rb_q_good = tk.Radiobutton(
            self.quality_row, text="良品", value="良品", variable=self.v_q_quality,
            bg=C_CARD, fg=C_IN, font=(self.font_name, 10, "bold"), activebackground=C_CARD,
            selectcolor="#ffffff", highlightthickness=0, bd=0, cursor="hand2",
            command=self._on_quality_change)
        self.rb_q_good.pack(side="left", padx=(0, 18))
        self.rb_q_defect = tk.Radiobutton(
            self.quality_row, text="不良品", value="不良品", variable=self.v_q_quality,
            bg=C_CARD, fg=C_OUT, font=(self.font_name, 10, "bold"), activebackground=C_CARD,
            selectcolor="#ffffff", highlightthickness=0, bd=0, cursor="hand2",
            command=self._on_quality_change)
        self.rb_q_defect.pack(side="left")
        self.lbl_quality_hint = tk.Label(
            self.quality_row, text="", bg=C_CARD, fg=C_MUTED,
            font=(self.font_name, 8))
        self.lbl_quality_hint.pack(side="left", padx=12)
        self.quality_row.grid_remove()

        lab(grid, "经办人", 2, 0)
        ttk.Entry(grid, textvariable=self.v_q_operator).grid(row=2, column=1, sticky="ew", pady=6, padx=(0, 20))

        lab(grid, "备注", 2, 2)
        ttk.Entry(grid, textvariable=self.v_q_note).grid(row=2, column=3, sticky="ew", pady=6)

        lab(grid, "时间", 3, 0)
        tf = tk.Frame(grid, bg=C_CARD)
        tf.grid(row=3, column=1, sticky="w", pady=6)
        ttk.Entry(tf, textvariable=self.v_q_date, width=12).pack(side="left")
        ttk.Entry(tf, textvariable=self.v_q_time, width=7).pack(side="left", padx=6)
        tk.Label(tf, text="（可改成补记的时间）", bg=C_CARD, fg=C_MUTED,
                 font=(self.font_name, 8)).pack(side="left", padx=6)

        btnf = tk.Frame(form, bg=C_CARD)
        btnf.pack(fill="x", padx=16, pady=(6, 4))
        ttk.Button(btnf, text="提交", style="Accent.TButton",
                   command=self.submit_quick).pack(side="left")
        ttk.Button(btnf, text="清空表单", command=self.clear_quick).pack(side="left", padx=8)
        tk.Label(btnf, text="提示：勾「盘点」时数量填实盘数，系统自动算差额",
                 bg=C_CARD, fg=C_MUTED, font=(self.font_name, 9)).pack(side="left", padx=14)

        # 今日流水
        box = tk.Frame(root, bg=C_BG)
        box.pack(fill="both", expand=True)
        hb = tk.Frame(box, bg=C_BG)
        hb.pack(fill="x", pady=(6, 4))
        tk.Label(hb, text="今天记的账", font=self.opt["font_h2"], bg=C_BG).pack(side="left")
        self.lbl_today = tk.Label(hb, text="", bg=C_BG, fg=C_MUTED, font=(self.font_name, 9))
        self.lbl_today.pack(side="left", padx=10)
        ttk.Button(hb, text="作废选中", command=self.void_selected_today).pack(side="right", padx=4)
        ttk.Button(hb, text="刷新", command=self.refresh_today).pack(side="right", padx=4)

        wrap = tk.Frame(box, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        cols = ("ts", "type", "mat", "fac", "qty", "batch", "op", "note")
        self.tv_today = ttk.Treeview(wrap, columns=cols, show="headings", height=9)
        for c, t, w, a in (("ts", "时间", 146, "center"), ("type", "类型", 60, "center"),
                           ("mat", "材料", 180, "w"), ("fac", "代工厂", 145, "w"),
                           ("qty", "数量", 90, "e"), ("batch", "批次/单号", 145, "w"),
                           ("op", "经办人", 80, "w"), ("note", "备注", 200, "w")):
            self.tv_today.heading(c, text=t, anchor=a)
            self.tv_today.column(c, width=w, anchor=a, stretch=(c in ("note", "mat")))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_today.yview)
        self.tv_today.configure(yscrollcommand=vs.set)
        self.tv_today.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_today.tag_configure("in", foreground=C_IN)
        self.tv_today.tag_configure("out", foreground=C_OUT)

        self.bind("<Return>", lambda _e: (
            self.submit_quick() if self.nb.select() == str(self.tab_quick) else None))

    def _on_quick_type_change(self):
        t = self.v_q_type.get()
        if t == "ADJUST":
            self.lbl_unit.config(text="（填实盘数量）")
        self._on_quick_material_change()

    def _on_quick_material_change(self):
        mid = self.mat_map.get(self.v_q_material.get())
        fid = self.fac_map.get(self.v_q_factory.get())
        m = self.store.material_by_id(mid) if mid else None
        self.lbl_unit.config(text=("单位：" + m["unit"]) if (m and m["unit"]) else "")

        # 材料勾了「区分良品/不良品」才显示品质选择
        split = bool(m and int(m.get("quality_split") or 0))
        if split:
            self.quality_row.grid()
        else:
            self.quality_row.grid_remove()
            self.v_q_quality.set("良品")

        if mid and fid:
            unit = ((m["unit"] if m else "") or "")
            if split:
                good, defect, total = self.store.balance_split(mid, fid)
                self.lbl_balance.config(
                    text="该厂结存：总数 %s %s　｜　良品 %s　不良品 %s"
                         % (fmt_qty(total), unit, fmt_qty(good), fmt_qty(defect)))
            else:
                bal = self.store.balance(mid, fid)
                self.lbl_balance.config(
                    text="该厂当前结存：%s %s" % (fmt_qty(bal), unit))
        else:
            self.lbl_balance.config(text="")
        self._on_quality_change()

    def _on_quality_change(self):
        """选了品质后，把该品质的结存也提示出来，避免超扣。"""
        mid = self.mat_map.get(self.v_q_material.get())
        fid = self.fac_map.get(self.v_q_factory.get())
        m = self.store.material_by_id(mid) if mid else None
        split = bool(m and int(m.get("quality_split") or 0))
        if not (split and mid and fid):
            self.lbl_quality_hint.config(text="")
            return
        q = QUALITY_GOOD if self.v_q_quality.get() == "良品" else QUALITY_DEFECT
        bal = self.store.balance(mid, fid, q)
        unit = (m["unit"] if m else "") or ""
        self.lbl_quality_hint.config(
            text="（该品质结存 %s %s）" % (fmt_qty(bal), unit))

    def submit_quick(self):
        t = self.v_q_type.get()
        mid = self.mat_map.get(self.v_q_material.get())
        fid = self.fac_map.get(self.v_q_factory.get())
        raw = self.v_q_qty.get().strip().replace("，", "").replace(",", "")
        if not mid:
            messagebox.showwarning(APP_TITLE, "请先选择材料。\n没有可选项的话，去「材料字典」先登记一个。", parent=self)
            return
        if not fid:
            messagebox.showwarning(APP_TITLE, "请先选择代工厂。\n没有可选项的话，去「代工厂」先登记一个。", parent=self)
            return
        try:
            qty = float(raw)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "数量要填数字，例如 120 或 12.5", parent=self)
            return

        ts = "%s %s" % (self.v_q_date.get().strip() or today_str(),
                        (self.v_q_time.get().strip() or now_str()[11:])[:5])

        # 品质：只有该材料开了区分才带值，否则一律空
        mat = self.store.material_by_id(mid)
        if mat and int(mat.get("quality_split") or 0):
            quality = QUALITY_GOOD if self.v_q_quality.get() == "良品" else QUALITY_DEFECT
        else:
            quality = ""
        q_label = QUALITY_LABELS.get(quality, "")

        if t == "OUT" and qty > 0:
            bal = self.store.balance(mid, fid, quality if quality else None)
            if qty > bal + EPS:
                what = ("「%s」的结存" % q_label) if q_label else "结存"
                if not messagebox.askyesno(
                        APP_TITLE,
                        "这次消耗比%s还多。\n当前%s %s，本次消耗 %s，记完会变成 %s。\n\n确定要记吗？"
                        % (what, what, fmt_qty(bal), fmt_qty(qty), fmt_qty(bal - qty)),
                        parent=self):
                    return

        try:
            if t == "ADJUST":
                self.store.add_record(t, mid, fid, 0, ts=ts, batch=self.v_q_batch.get(),
                                      operator=self.v_q_operator.get(), note=self.v_q_note.get(),
                                      target_qty=qty, quality=quality)
            else:
                self.store.add_record(t, mid, fid, qty, ts=ts, batch=self.v_q_batch.get(),
                                      operator=self.v_q_operator.get(), note=self.v_q_note.get(),
                                      quality=quality)
        except Exception as ex:
            messagebox.showerror(APP_TITLE, str(ex), parent=self)
            return

        self.v_q_qty.set("")
        self.v_q_batch.set("")
        self.v_q_note.set("")
        self.set_status("已记录：%s%s %s %s" % (
            TYPE_LABELS[t], ("（%s）" % q_label) if q_label else "",
            self.v_q_material.get(), fmt_qty(qty)), C_IN)
        self.refresh_all()
        self.auto_cloud_after_save()
        self._on_quick_material_change()

    def clear_quick(self):
        for v in (self.v_q_qty, self.v_q_batch, self.v_q_note, self.v_q_operator):
            v.set("")
        self.v_q_date.set(today_str())
        self.v_q_time.set(now_str()[11:])

    def refresh_today(self):
        tv = self.tv_today
        tv.delete(*tv.get_children())
        d = today_str()
        rows = self.store.list_records(date_from=d, date_to=d, include_voided=True, limit=500)
        for r in rows:
            unit = r["unit"] or ""
            sign = "+" if r["delta"] > 0 else ""
            tag = ("in",) if r["delta"] > 0 else ("out",)
            if r["voided"]:
                tag = ()
            qty = "%s%s%s" % (sign, fmt_qty(r["delta"]), (" " + unit) if unit else "")
            if r["voided"]:
                qty = "（已作废）" + qty
            q = r.get("quality") or ""
            qtag = QUALITY_LABELS.get(q, "")
            mat_txt = r["material_name"] + (" " + r["material_spec"] if r["material_spec"] else "")
            if qtag:
                mat_txt += "  〔%s〕" % qtag
            tv.insert("", "end", iid=str(r["id"]),
                      values=(r["ts"][11:], TYPE_LABELS.get(r["type"], r["type"]),
                              mat_txt,
                              r["factory_name"], qty, r["batch"], r["operator"], r["note"]),
                      tags=tag)
        self.lbl_today.config(text="共 %d 条" % len(rows))

    def void_selected_today(self):
        sel = self.tv_today.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先在下面列表里选中要作废的记录", parent=self)
            return
        if not messagebox.askyesno(APP_TITLE, "作废后结存会自动回退，确定作废 %d 条？" % len(sel), parent=self):
            return
        for iid in sel:
            self.store.void_record(int(iid), True)
        self.set_status("已作废 %d 条记录" % len(sel), C_OUT)
        self.refresh_all()
        self.auto_cloud_after_save()

    # ============================================================ 流水明细

    def _build_records_tab(self):
        root = self.tab_records

        bar = tk.Frame(root, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        bar.pack(fill="x", pady=(10, 8), ipady=6)

        r1 = tk.Frame(bar, bg=C_CARD)
        r1.pack(fill="x", padx=14, pady=4)
        tk.Label(r1, text="从", bg=C_CARD, font=self.opt["font"], fg=C_MUTED).pack(side="left")
        self.v_f_from = tk.StringVar()
        ttk.Entry(r1, textvariable=self.v_f_from, width=12).pack(side="left", padx=4)
        tk.Label(r1, text="到", bg=C_CARD, font=self.opt["font"], fg=C_MUTED).pack(side="left")
        self.v_f_to = tk.StringVar()
        ttk.Entry(r1, textvariable=self.v_f_to, width=12).pack(side="left", padx=4)

        ttk.Button(r1, text="今天", width=6,
                   command=lambda: self._set_range(0)).pack(side="left", padx=(8, 2))
        ttk.Button(r1, text="本月", width=6,
                   command=lambda: self._set_range(1)).pack(side="left", padx=2)
        ttk.Button(r1, text="全部", width=6,
                   command=lambda: self._set_range(2)).pack(side="left", padx=2)

        tk.Label(r1, text="类型", bg=C_CARD, font=self.opt["font"], fg=C_MUTED).pack(side="left", padx=(16, 4))
        self.v_f_type = tk.StringVar(value="全部")
        cb = ttk.Combobox(r1, textvariable=self.v_f_type, state="readonly", width=7,
                          values=["全部", "入库", "消耗", "退回", "盘点"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_records())

        self.v_f_voided = tk.BooleanVar(value=True)
        ttk.Checkbutton(r1, text="含已作废", variable=self.v_f_voided,
                        command=self.refresh_records).pack(side="left", padx=12)

        r2 = tk.Frame(bar, bg=C_CARD)
        r2.pack(fill="x", padx=14, pady=4)
        tk.Label(r2, text="材料", bg=C_CARD, font=self.opt["font"], fg=C_MUTED).pack(side="left")
        self.v_f_material = tk.StringVar(value="全部")
        self.cb_f_material = ttk.Combobox(r2, textvariable=self.v_f_material, state="readonly", width=24)
        self.cb_f_material.pack(side="left", padx=4)
        self.cb_f_material.bind("<<ComboboxSelected>>", lambda _e: self.refresh_records())

        tk.Label(r2, text="代工厂", bg=C_CARD, font=self.opt["font"], fg=C_MUTED).pack(side="left", padx=(12, 0))
        self.v_f_factory = tk.StringVar(value="全部")
        self.cb_f_factory = ttk.Combobox(r2, textvariable=self.v_f_factory, state="readonly", width=18)
        self.cb_f_factory.pack(side="left", padx=4)
        self.cb_f_factory.bind("<<ComboboxSelected>>", lambda _e: self.refresh_records())

        tk.Label(r2, text="关键词", bg=C_CARD, font=self.opt["font"], fg=C_MUTED).pack(side="left", padx=(12, 0))
        self.v_f_kw = tk.StringVar()
        e = ttk.Entry(r2, textvariable=self.v_f_kw, width=20)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self.refresh_records())
        ttk.Button(r2, text="查询", command=self.refresh_records).pack(side="left", padx=(10, 4))

        # 操作按钮单独一行 —— 筛选那行塞不下这么多按钮，会被挤没
        r3 = tk.Frame(bar, bg=C_CARD)
        r3.pack(fill="x", padx=14, pady=(6, 2))
        self.btn_rec_delete = ttk.Button(r3, text="删除选中", style="Danger.TButton",
                                         command=self.delete_selected_records)
        self.btn_rec_delete.pack(side="left")
        ttk.Button(r3, text="回收站", command=self.open_recycle_bin).pack(side="left", padx=(8, 0))
        tk.Label(r3, text="删除 = 从列表移除、可在回收站找回；作废 = 留在列表但不计入结存",
                 bg=C_CARD, fg="#9aa4b1", font=(self.font_name, 8)).pack(side="left", padx=14)

        ttk.Button(r3, text="导出 CSV", command=self.export_records).pack(side="right")
        ttk.Button(r3, text="恢复选中", command=lambda: self.void_selected_records(False)).pack(side="right", padx=4)
        ttk.Button(r3, text="作废选中", command=lambda: self.void_selected_records(True)).pack(side="right", padx=4)

        wrap = tk.Frame(root, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        cols = ("ts", "type", "mat", "spec", "quality", "fac", "qty", "unit", "batch", "op", "note", "state")
        self.tv_rec = ttk.Treeview(wrap, columns=cols, show="headings", height=16)
        for c, t, w, a, stretch in (
                ("ts", "时间", 158, "center", False), ("type", "类型", 52, "center", False),
                ("mat", "材料", 118, "w", True), ("spec", "规格", 110, "w", False),
                ("quality", "品质", 66, "center", False),
                ("fac", "代工厂", 118, "w", True), ("qty", "数量", 74, "e", False),
                ("unit", "单位", 56, "center", False), ("batch", "批次/单号", 130, "w", False),
                ("op", "经办人", 64, "w", False), ("note", "备注", 140, "w", True),
                ("state", "状态", 56, "center", False)):
            self.tv_rec.heading(c, text=t, anchor=a)
            self.tv_rec.column(c, width=w, anchor=a, stretch=stretch)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_rec.yview)
        self.tv_rec.configure(yscrollcommand=vs.set)
        self.tv_rec.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_rec.tag_configure("in", foreground=C_IN)
        self.tv_rec.tag_configure("out", foreground=C_OUT)
        self.tv_rec.tag_configure("voided", foreground="#b0b7c2")

        self.lbl_rec_sum = tk.Label(root, text="", bg=C_BG, fg=C_MUTED, font=(self.font_name, 9), anchor="w")
        self.lbl_rec_sum.pack(fill="x", pady=(4, 0))

        self._set_range(1)

    def _set_range(self, mode):
        import datetime
        t = datetime.date.today()
        if mode == 0:
            self.v_f_from.set(t.strftime("%Y-%m-%d"))
            self.v_f_to.set(t.strftime("%Y-%m-%d"))
        elif mode == 1:
            self.v_f_from.set(t.strftime("%Y-%m-01"))
            self.v_f_to.set(t.strftime("%Y-%m-%d"))
        else:
            self.v_f_from.set("")
            self.v_f_to.set("")
        self.refresh_records()

    def _record_filters(self):
        t = {"全部": ""}.get(self.v_f_type.get(), "")
        rtype = {v: k for k, v in TYPE_LABELS.items()}.get(self.v_f_type.get(), "")
        mid = self.store_mat_map.get(self.v_f_material.get())
        fid = self.store_fac_map.get(self.v_f_factory.get())
        return dict(date_from=self.v_f_from.get().strip(), date_to=self.v_f_to.get().strip(),
                    rtype=rtype, material_id=mid, factory_id=fid,
                    keyword=self.v_f_kw.get().strip(), include_voided=self.v_f_voided.get())

    def refresh_records(self):
        tv = self.tv_rec
        tv.delete(*tv.get_children())
        f = self._record_filters()
        rows = self.store.list_records(**f)
        tin = tout = 0.0
        for r in rows:
            unit = r["unit"] or ""
            sign = "+" if r["delta"] > 0 else ""
            tags = ("voided",) if r["voided"] else (("in",) if r["delta"] > 0 else ("out",))
            if not r["voided"]:
                if r["delta"] > 0:
                    tin += r["delta"]
                else:
                    tout += -r["delta"]
            q = r.get("quality") or ""
            tv.insert("", "end", iid=str(r["id"]),
                      values=(r["ts"], TYPE_LABELS.get(r["type"], r["type"]),
                              r["material_name"], r["material_spec"],
                              QUALITY_LABELS.get(q, ""),
                              r["factory_name"],
                              sign + fmt_qty(r["delta"]), unit, r["batch"], r["operator"],
                              r["note"], "已作废" if r["voided"] else ""),
                      tags=tags)
        self.lbl_rec_sum.config(
            text="共 %d 条　｜　入 %s　出 %s" % (len(rows), fmt_qty(tin), fmt_qty(tout)))

    def void_selected_records(self, voided):
        sel = self.tv_rec.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先选中要处理的记录", parent=self)
            return
        old = self.v_f_voided.get()
        self.v_f_voided.set(True)
        act = "作废" if voided else "恢复"
        if not messagebox.askyesno(APP_TITLE, "确定%s这 %d 条记录？" % (act, len(sel)), parent=self):
            self.v_f_voided.set(old)
            return
        for iid in sel:
            self.store.void_record(int(iid), voided)
        self.set_status("已%s %d 条" % (act, len(sel)), C_OUT if voided else C_IN)
        self.refresh_all()
        self.auto_cloud_after_save()

    def delete_selected_records(self):
        """
        删除选中的流水（软删除，可在回收站恢复）。

        与「作废」的区别要跟用户讲清楚，不然容易点错：
          作废 = 账还在、留个痕，只是不算进结存
          删除 = 从列表里拿走，去回收站找
        """
        sel = self.tv_rec.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先选中要删除的记录", parent=self)
            return

        rows = [self.store.record_by_id(int(i)) for i in sel]
        rows = [r for r in rows if r]
        sample = rows[:5]
        detail = "\n".join(
            "  %s  %s  %s%s" % (r["ts"], TYPE_LABELS.get(r["type"], r["type"]),
                                "+" if r["delta"] > 0 else "", fmt_qty(r["delta"]))
            for r in sample)
        if len(rows) > 5:
            detail += "\n  …还有 %d 条" % (len(rows) - 5)

        msg = ("确定删除这 %d 条记录？\n\n%s\n\n"
               "删除后这些记录不再计入结存，可以在「回收站」里恢复。\n\n"
               "（如果只是想让这笔账失效、但要留在列表里，请用「作废选中」）"
               % (len(sel), detail))

        if not messagebox.askyesno(APP_TITLE, msg, parent=self):
            return

        n = self.store.delete_records([int(i) for i in sel])
        self.set_status("已删除 %d 条记录（可在回收站恢复）" % n, C_OUT)
        self.refresh_all()
        self.auto_cloud_after_save()

    def open_recycle_bin(self):
        """打开回收站窗口。"""
        RecycleBinDialog(self, self.store, on_done=self.refresh_all)

    def export_records(self):
        rows = self.store.list_records(**self._record_filters())
        if not rows:
            messagebox.showinfo(APP_TITLE, "当前筛选条件没有记录可导出", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出流水", defaultextension=".csv",
            initialfile="流水_%s.csv" % today_str().replace("-", ""),
            filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            n = self.store.export_records_csv(path, **self._record_filters())
            self.set_status("已导出 %d 条到 %s" % (n, os.path.basename(path)), C_IN)
            if messagebox.askyesno(APP_TITLE, "已导出 %d 条：\n%s\n\n现在打开所在文件夹？" % (n, path), parent=self):
                os.startfile(os.path.dirname(path))
        except Exception as ex:
            messagebox.showerror(APP_TITLE, "导出失败：%s" % ex, parent=self)

    # ============================================================ 材料字典

    def _build_materials_tab(self):
        root = self.tab_materials
        bar = tk.Frame(root, bg=C_BG)
        bar.pack(fill="x", pady=(10, 8))
        ttk.Button(bar, text="新增材料", style="Accent.TButton",
                   command=lambda: self.material_dialog(None)).pack(side="left")
        ttk.Button(bar, text="修改", command=self.edit_material_sel).pack(side="left", padx=6)
        ttk.Button(bar, text="停用 / 启用", command=self.toggle_material_sel).pack(side="left", padx=6)
        tk.Label(bar, text="停用后不再出现在下拉框，历史记录和结存都保留",
                 bg=C_BG, fg=C_MUTED, font=(self.font_name, 9)).pack(side="left", padx=12)

        wrap = tk.Frame(root, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        cols = ("name", "spec", "unit", "cat", "minq", "qty", "quality", "note", "state")
        self.tv_mat = ttk.Treeview(wrap, columns=cols, show="headings", height=16)
        for c, t, w, a, stretch in (
                ("name", "材料名称", 170, "w", True), ("spec", "规格", 130, "w", True),
                ("unit", "单位", 60, "center", False), ("cat", "分类", 92, "w", False),
                ("minq", "安全库存", 86, "e", False), ("qty", "当前总结存", 104, "e", False),
                ("quality", "品质区分", 88, "center", False),
                ("note", "备注", 168, "w", True), ("state", "状态", 66, "center", False)):
            self.tv_mat.heading(c, text=t, anchor=a)
            self.tv_mat.column(c, width=w, anchor=a, stretch=stretch)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_mat.yview)
        self.tv_mat.configure(yscrollcommand=vs.set)
        self.tv_mat.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_mat.tag_configure("off", foreground="#a3acb8")
        self.tv_mat.bind("<Double-1>", lambda _e: self.edit_material_sel())

    def refresh_materials(self):
        tv = self.tv_mat
        tv.delete(*tv.get_children())
        rows = self.store.list_materials(active_only=False, with_stock=True)
        self.store_mat_map = {"全部": None}
        self.mat_map = {}
        for r in rows:
            if r["active"]:
                disp = material_display(r)
                self.mat_map[disp] = r["id"]
                self.store_mat_map[disp] = r["id"]
            tv.insert("", "end", iid=str(r["id"]),
                      values=(r["name"], r["spec"], r["unit"], r["category"],
                              fmt_qty(r["min_qty"]) if r["min_qty"] else "",
                              fmt_qty(r["qty"]),
                              "已启用" if int(r.get("quality_split") or 0) else "",
                              r["note"],
                              "在用" if r["active"] else "已停用"),
                      tags=() if r["active"] else ("off",))
        self._sync_combos()

    def _sync_combos(self):
        mats = list(self.mat_map.keys())
        facs = list(self.fac_map.keys())
        self.cb_q_material.config(values=mats)
        self.cb_q_factory.config(values=facs)
        self.cb_f_material.config(values=["全部"] + mats)
        self.cb_f_factory.config(values=["全部"] + facs)
        if self.v_q_material.get() not in mats:
            self.v_q_material.set(mats[0] if mats else "")
        if self.v_q_factory.get() not in facs:
            self.v_q_factory.set(facs[0] if facs else "")
        if self.v_f_material.get() not in ["全部"] + mats:
            self.v_f_material.set("全部")
        if self.v_f_factory.get() not in ["全部"] + facs:
            self.v_f_factory.set("全部")

    def edit_material_sel(self):
        sel = self.tv_mat.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先选一行材料", parent=self)
            return
        self.material_dialog(int(sel[0]))

    def toggle_material_sel(self):
        sel = self.tv_mat.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先选一行材料", parent=self)
            return
        mid = int(sel[0])
        m = self.store.material_by_id(mid)
        if m["active"] and self.store.material_record_count(mid) > 0:
            if not messagebox.askyesno(APP_TITLE,
                                       "「%s」有 %d 条历史记录，停用后记录仍会保留。\n确定停用？"
                                       % (m["name"], self.store.material_record_count(mid)), parent=self):
                return
        self.store.set_material_active(mid, not m["active"])
        self.refresh_all()

    def material_dialog(self, mid):
        MaterialDialog(self, self.store, mid, on_done=self.refresh_all)

    # ============================================================ 代工厂

    def _build_factories_tab(self):
        root = self.tab_factories
        bar = tk.Frame(root, bg=C_BG)
        bar.pack(fill="x", pady=(10, 8))
        ttk.Button(bar, text="新增代工厂", style="Accent.TButton",
                   command=lambda: self.factory_dialog(None)).pack(side="left")
        ttk.Button(bar, text="修改", command=self.edit_factory_sel).pack(side="left", padx=6)
        ttk.Button(bar, text="停用 / 启用", command=self.toggle_factory_sel).pack(side="left", padx=6)
        ttk.Button(bar, text="删除", style="Danger.TButton",
                   command=self.delete_factory_sel).pack(side="left", padx=(14, 6))
        ttk.Button(bar, text="恢复选中", command=self.restore_factory_sel).pack(side="left")

        self.v_fac_show_deleted = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="显示已删除", variable=self.v_fac_show_deleted,
                        command=self.refresh_factories).pack(side="right")
        tk.Label(bar, text="删除后不再出现；停用只是隐藏，随时能启用",
                 bg=C_BG, fg=C_MUTED, font=(self.font_name, 9)).pack(side="right", padx=12)

        wrap = tk.Frame(root, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        cols = ("name", "contact", "kinds", "qty", "note", "state")
        self.tv_fac = ttk.Treeview(wrap, columns=cols, show="headings", height=16)
        for c, t, w, a, stretch in (
                ("name", "代工厂名称", 230, "w", True), ("contact", "联系人", 180, "w", False),
                ("kinds", "材料种类", 100, "center", False), ("qty", "合计结存", 110, "e", False),
                ("note", "备注", 260, "w", True), ("state", "状态", 70, "center", False)):
            self.tv_fac.heading(c, text=t, anchor=a)
            self.tv_fac.column(c, width=w, anchor=a, stretch=stretch)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_fac.yview)
        self.tv_fac.configure(yscrollcommand=vs.set)
        self.tv_fac.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_fac.tag_configure("off", foreground="#a3a8b8")
        self.tv_fac.tag_configure("del", foreground="#c0c6d0")
        self.tv_fac.bind("<Double-1>", lambda _e: self.edit_factory_sel())

    def refresh_factories(self):
        tv = self.tv_fac
        tv.delete(*tv.get_children())
        show_del = bool(self.v_fac_show_deleted.get())
        rows = self.store.list_factories(active_only=False, include_deleted=show_del)
        self.fac_map = {}
        self.store_fac_map = {"全部": None}
        for r in rows:
            gone = bool(int(r.get("deleted") or 0))
            if r["active"] and not gone:
                self.fac_map[r["name"]] = r["id"]
                self.store_fac_map[r["name"]] = r["id"]
            kinds = self.conn_kinds(r["id"])
            state = "已删除" if gone else ("在用" if r["active"] else "已停用")
            tag = ("del",) if gone else (() if r["active"] else ("off",))
            tv.insert("", "end", iid=str(r["id"]),
                      values=(r["name"], r["contact"], kinds,
                              fmt_qty(self.store.factory_total(r["id"])),
                              r["note"], state),
                      tags=tag)
        self._sync_combos()

    def conn_kinds(self, fid):
        row = self.store.conn.execute(
            "SELECT COUNT(DISTINCT material_id) n FROM records WHERE factory_id=? AND voided=0"
            " AND deleted=0", (fid,)).fetchone()
        return row["n"] if row else 0

    def edit_factory_sel(self):
        sel = self.tv_fac.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先选一行代工厂", parent=self)
            return
        fid = int(sel[0])
        f = self.store.factory_by_id(fid)
        # 已删除的厂不该进编辑窗（改完也不会回到列表），直接引导去恢复
        if f and int(f.get("deleted") or 0):
            if messagebox.askyesno(APP_TITLE,
                                   "「%s」已经删除了，不能改。\n\n现在把它恢复回来吗？"
                                   % f["name"], parent=self):
                self.tv_fac.selection_set(str(fid))
                self.restore_factory_sel()
            return
        self.factory_dialog(fid)

    def toggle_factory_sel(self):
        sel = self.tv_fac.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先选一行代工厂", parent=self)
            return
        fid = int(sel[0])
        f = self.store.factory_by_id(fid)
        if int(f.get("deleted") or 0):
            messagebox.showinfo(APP_TITLE,
                                "「%s」已经删除了。\n要重新用它，点「恢复选中」。" % f["name"],
                                parent=self)
            return
        if f["active"] and self.store.factory_record_count(fid) > 0:
            if not messagebox.askyesno(APP_TITLE,
                                       "「%s」有 %d 条记录，停用后记录仍保留。确定停用？"
                                       % (f["name"], self.store.factory_record_count(fid)), parent=self):
                return
        self.store.set_factory_active(fid, not f["active"])
        self.refresh_all()

    def delete_factory_sel(self):
        """
        删除代工厂（软删除，可恢复）。

        和「停用」的区别要跟用户讲清楚：
          停用 = 从下拉框藏起来，列表里还在，随时能启用
          删除 = 从代工厂列表拿走，只能在「显示已删除」里看到

        名下还有有效流水的厂不允许删——那些账会变成「无所属」。
        """
        sel = self.tv_fac.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先选中要删除的代工厂", parent=self)
            return

        names, blocked = [], []
        for iid in sel:
            fid = int(iid)
            f = self.store.factory_by_id(fid)
            if not f:
                continue
            if int(f.get("deleted") or 0):
                continue
            live = self.store.factory_live_record_count(fid)
            if live > 0:
                blocked.append((f["name"], live, self.store.factory_total(fid)))
            else:
                names.append(f["name"])

        if blocked:
            detail = "\n".join("  %s —— 还有 %d 条有效记录（结存 %s）"
                               % (n, c, fmt_qty(q)) for n, c, q in blocked)
            msg = ("以下代工厂名下还有有效流水，不能直接删除：\n\n%s\n\n"
                   "删掉厂，这些账就无所属了。可以：\n"
                   "  · 在「流水明细」里把那些记录作废或删除，再来删厂\n"
                   "  · 或者用「停用 / 启用」——它只是从下拉框藏起来，账一分不少\n\n"
                   "仍然继续删除其余可以删的吗？" % detail)
            if not messagebox.askyesno(APP_TITLE, msg, parent=self):
                return

        if not names:
            if not blocked:
                messagebox.showinfo(APP_TITLE, "选中的都是已删除的代工厂，不用再删。", parent=self)
            return

        if not messagebox.askyesno(
                APP_TITLE,
                "确定删除以下代工厂？\n\n  %s\n\n"
                "删除后它们不再出现在下拉框和列表里，\n"
                "数据没有真删，勾上「显示已删除」可以点「恢复选中」找回来。"
                % "\n  ".join(names), parent=self):
            return

        n = 0
        skipped = []
        for iid in sel:
            fid = int(iid)
            f = self.store.factory_by_id(fid)
            if not f or int(f.get("deleted") or 0):
                continue
            try:
                n += self.store.delete_factory(fid)
            except ValueError as ex:
                # 数据层还有一道闸（判断之后、删除之前又记了账这种极端情况），
                # 不弹一个就把整个操作打断，收集起来最后统一说。
                skipped.append(str(ex).split("\n")[0])
        if skipped:
            messagebox.showwarning(APP_TITLE, "以下代工厂没能删除：\n\n  "
                                   + "\n  ".join(skipped), parent=self)
        self.set_status("已删除 %d 个代工厂（勾「显示已删除」可恢复）" % n, C_OUT)
        self.refresh_all()
        self.auto_cloud_after_save()

    def restore_factory_sel(self):
        """把「已删除」的代工厂恢复回来。"""
        sel = self.tv_fac.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先选中要恢复的代工厂", parent=self)
            return
        n = 0
        for iid in sel:
            n += self.store.restore_factory(int(iid))
        if not n:
            messagebox.showinfo(APP_TITLE,
                                "选中的代工厂不在「已删除」状态，不用恢复。\n"
                                "（停用的用「停用 / 启用」按钮）", parent=self)
            return
        self.set_status("已恢复 %d 个代工厂" % n, C_IN)
        self.refresh_all()
        self.auto_cloud_after_save()

    def factory_dialog(self, fid):
        FactoryDialog(self, self.store, fid, on_done=self.refresh_all)

    # ============================================================ 刷新 / 导出 / 备份

    def refresh_all(self):
        self.refresh_stock()
        self.refresh_factories()
        self.refresh_materials()
        self.refresh_today()
        self.refresh_records()
        self._on_quick_material_change()
        self.refresh_cloud_state()
        st = self.store.stats()
        self.lbl_stats.config(
            text="%d 种材料 · %d 个代工厂 · %d 条流水" %
                 (st["materials"], st["factories"], st["records"]))

    # ============================================================ 云端备份

    def _build_cloud_tab(self):
        root = self.tab_cloud

        # ---- 顶部说明
        note = tk.Frame(root, bg="#eef4ff", highlightbackground="#cfe0ff", highlightthickness=1)
        note.pack(fill="x", pady=(10, 8))
        tk.Label(note, text="这个页面是可选的，不用完全没关系——本地记账一直都能用。",
                 bg="#eef4ff", fg="#24518f", font=(self.font_name, 9, "bold"),
                 anchor="w").pack(fill="x", padx=14, pady=(9, 2))
        tk.Label(note,
                 text="打开后，每次记账保存都会在后台把这个账本的 CSV 快照传一份到你的备份柜；"
                      "传失败只影响备份，不影响本地数据。",
                 bg="#eef4ff", fg="#3d6aa8", font=(self.font_name, 9), anchor="w",
                 wraplength=940, justify="left").pack(fill="x", padx=14, pady=(0, 9))

        body = tk.Frame(root, bg=C_BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        hd = tk.Frame(left, bg=C_CARD)
        hd.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(hd, text="连接设置", font=self.opt["font_h2"], bg=C_CARD).pack(side="left")

        self.v_cloud_enabled = tk.BooleanVar(value=False)
        self.v_cloud_auto = tk.BooleanVar(value=True)
        self.v_cloud_url = tk.StringVar()
        self.v_cloud_pwd = tk.StringVar()
        self.v_cloud_showpwd = tk.BooleanVar(value=False)
        self.v_cloud_mode = tk.StringVar(value="全部数据（库存 + 流水）")

        grid = tk.Frame(left, bg=C_CARD)
        grid.pack(fill="x", padx=16, pady=(6, 4))
        grid.columnconfigure(1, weight=1)

        def lab(r, t):
            tk.Label(grid, text=t, bg=C_CARD, font=self.opt["font"], fg=C_MUTED,
                     anchor="e", width=10).grid(row=r, column=0, sticky="e", pady=7, padx=(0, 10))

        chk = tk.Frame(grid, bg=C_CARD)
        chk.grid(row=0, column=1, columnspan=2, sticky="w", pady=(2, 2))
        cb_style = dict(bg=C_CARD, fg=C_TEXT, font=(self.font_name, 10),
                        activebackground=C_CARD, activeforeground=C_TEXT,
                        selectcolor="#ffffff", highlightthickness=0, bd=0,
                        anchor="w", cursor="hand2")
        tk.Checkbutton(chk, text="启用云端备份", variable=self.v_cloud_enabled,
                       command=self.on_cloud_toggle, **cb_style).pack(side="left")
        tk.Checkbutton(chk, text="记账后自动上传", variable=self.v_cloud_auto,
                       command=self.on_cloud_toggle, **cb_style).pack(side="left", padx=(24, 0))

        lab(1, "备份地址")
        ttk.Entry(grid, textvariable=self.v_cloud_url).grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=7)

        lab(2, "访问密码")
        pf = tk.Frame(grid, bg=C_CARD)
        pf.grid(row=2, column=1, columnspan=2, sticky="ew", pady=7)
        self.e_cloud_pwd = ttk.Entry(pf, textvariable=self.v_cloud_pwd, show="*")
        self.e_cloud_pwd.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(pf, text="显示", variable=self.v_cloud_showpwd,
                        command=self._toggle_pwd_show).pack(side="left", padx=8)

        lab(3, "上传内容")
        ttk.Combobox(grid, textvariable=self.v_cloud_mode, state="readonly",
                     values=["全部数据（库存 + 流水）", "只要当前库存"]).grid(
            row=3, column=1, columnspan=2, sticky="w", pady=7)

        btnf = tk.Frame(left, bg=C_CARD)
        btnf.pack(fill="x", padx=16, pady=(10, 4))
        ttk.Button(btnf, text="保存设置", style="Accent.TButton",
                   command=self.save_cloud_settings).pack(side="left")
        self.btn_cloud_test = ttk.Button(btnf, text="测试连接",
                                         command=self.test_cloud)
        self.btn_cloud_test.pack(side="left", padx=8)
        self.btn_cloud_now = ttk.Button(btnf, text="立即备份一次",
                                        command=self.upload_cloud_now)
        self.btn_cloud_now.pack(side="left")

        self.lbl_cloud_hint = tk.Label(left, text="", bg=C_CARD, fg=C_MUTED,
                                       font=(self.font_name, 9), anchor="w",
                                       wraplength=560, justify="left")
        self.lbl_cloud_hint.pack(fill="x", padx=16, pady=(4, 12))

        tk.Frame(left, bg=C_LINE, height=1).pack(fill="x", padx=16)

        # ---------------- 多端同步 ----------------
        sync_box = tk.Frame(left, bg=C_CARD)
        sync_box.pack(fill="x", padx=16, pady=(10, 6))
        tk.Label(sync_box, text="多端同步", font=self.opt["font_h2"],
                 bg=C_CARD).pack(anchor="w")
        tk.Label(sync_box,
                 text="先把本机的账上传，再在另一台点「从云端同步」，两边的账会合并到一起，"
                      "不会互相覆盖。",
                 bg=C_CARD, fg=C_MUTED, font=(self.font_name, 9), anchor="w",
                 wraplength=560, justify="left").pack(anchor="w", pady=(2, 8))

        sbtn = tk.Frame(sync_box, bg=C_CARD)
        sbtn.pack(fill="x")
        self.btn_sync_push = ttk.Button(sbtn, text="上传本机数据到云端",
                                        command=self.sync_push)
        self.btn_sync_push.pack(side="left")
        self.btn_sync_pull = ttk.Button(sbtn, text="从云端同步",
                                        command=self.sync_pull)
        self.btn_sync_pull.pack(side="left", padx=8)

        self.lbl_sync_state = tk.Label(sync_box, text="", bg=C_CARD, fg=C_MUTED,
                                       font=(self.font_name, 9), anchor="w",
                                       wraplength=560, justify="left")
        self.lbl_sync_state.pack(anchor="w", pady=(8, 0))

        tk.Frame(left, bg=C_LINE, height=1).pack(fill="x", padx=16)

        stf = tk.Frame(left, bg=C_CARD)
        stf.pack(fill="x", padx=16, pady=(10, 14))
        tk.Label(stf, text="上次结果", font=self.opt["font_bold"], bg=C_CARD).grid(
            row=0, column=0, sticky="w")
        self.lbl_cloud_last = tk.Label(stf, text="还没有上传过", bg=C_CARD, fg=C_MUTED,
                                       font=(self.font_name, 9), anchor="w",
                                       wraplength=560, justify="left")
        self.lbl_cloud_last.grid(row=0, column=1, sticky="w", padx=10)
        self.lbl_cloud_dirty = tk.Label(stf, text="", bg=C_CARD, fg=C_MUTED,
                                        font=(self.font_name, 9), anchor="w")
        self.lbl_cloud_dirty.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ---- 右侧：云端文件
        right = tk.Frame(body, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        right.pack(side="left", fill="both", padx=(8, 0))
        right.configure(width=430)
        right.pack_propagate(False)

        rh = tk.Frame(right, bg=C_CARD)
        rh.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(rh, text="云端已有文件", font=self.opt["font_h2"], bg=C_CARD).pack(side="left")
        ttk.Button(rh, text="刷新", command=self.load_cloud_files).pack(side="right")

        wrap = tk.Frame(right, bg=C_CARD)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        cols = ("name", "size", "upd")
        self.tv_cloud = ttk.Treeview(wrap, columns=cols, show="headings", height=14)
        for c, t, w, a in (("name", "文件名", 250, "w"), ("size", "大小", 70, "e"),
                           ("upd", "更新时间", 92, "center")):
            self.tv_cloud.heading(c, text=t, anchor=a)
            self.tv_cloud.column(c, width=w, anchor=a, stretch=(c == "name"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_cloud.yview)
        self.tv_cloud.configure(yscrollcommand=vs.set)
        self.tv_cloud.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.lbl_cloud_list = tk.Label(right, text="", bg=C_CARD, fg=C_MUTED,
                                       font=(self.font_name, 8), anchor="w")
        self.lbl_cloud_list.pack(fill="x", padx=14, pady=(0, 10))

        self.cloud = cloud_mod.CloudBackup(self.store)
        self.sync_engine = SyncEngine(self.store)
        self._cloud_busy = False
        self._load_cloud_config()
        self.refresh_cloud_state()

    def _toggle_pwd_show(self):
        self.e_cloud_pwd.config(show="" if self.v_cloud_showpwd.get() else "*")

    def _load_cloud_config(self):
        c = self.cloud.config()
        self.v_cloud_url.set(c["url"])
        self.v_cloud_pwd.set(c["password"])
        self.v_cloud_enabled.set(c["enabled"])
        self.v_cloud_auto.set(c["auto"])

    def on_cloud_toggle(self):
        """勾选框一变就落盘，避免用户忘了点保存。"""
        self.cloud.save_config(enabled=self.v_cloud_enabled.get(),
                               auto=self.v_cloud_auto.get())
        self.refresh_cloud_state()

    def save_cloud_settings(self):
        url = self.v_cloud_url.get().strip()
        pwd = self.v_cloud_pwd.get()
        if self.v_cloud_enabled.get() and not url:
            messagebox.showwarning(APP_TITLE, "要启用云端备份，得先填备份地址。", parent=self)
            return
        if self.v_cloud_enabled.get() and not pwd:
            messagebox.showwarning(APP_TITLE, "要启用云端备份，得先填访问密码。", parent=self)
            return
        self.cloud.save_config(url=url, password=pwd,
                               enabled=self.v_cloud_enabled.get(),
                               auto=self.v_cloud_auto.get())
        self.refresh_cloud_state()
        self.set_status("云端备份设置已保存", C_IN)
        messagebox.showinfo(APP_TITLE, "已保存。\n\n建议点一下「测试连接」确认能通。", parent=self)

    def refresh_cloud_state(self):
        if not hasattr(self, "lbl_cloud_last"):
            return
        c = self.cloud.config()
        ready = self.cloud.is_ready()
        last = self.cloud.last_state()
        dirty = self.store.dirty_count()

        if not c["url"]:
            hint = "还没配置。填上地址和密码，勾「启用云端备份」才会开始传。"
            hint_color = C_MUTED
        elif not c["password"]:
            hint = "填了地址但没填密码。"
            hint_color = C_OUT
        elif not c["enabled"]:
            hint = "已填写，但开关是关的——记账不会自动传；想传一次可以点「立即备份一次」。"
            hint_color = C_MUTED
        elif not c["auto"]:
            hint = "已启用，但只在你点「立即备份一次」时上传。"
            hint_color = C_ACCENT
        else:
            hint = "已启用：每次记账保存后会自动在后台传一份。"
            hint_color = C_IN
        self.lbl_cloud_hint.config(text=hint, fg=hint_color)

        if last["ts"]:
            ok = last["result"].startswith("OK")
            body = last["result"][3:] if last["result"][:3] in ("OK ", "FAI") else last["result"]
            self.lbl_cloud_last.config(
                text="%s\n%s" % (last["ts"], body),
                fg=(C_IN if ok else C_OUT))
        else:
            self.lbl_cloud_last.config(text="还没有上传过", fg=C_MUTED)

        if not ready:
            dirty_text = "还没开启，改动只在本地"
            dirty_color = C_MUTED
        elif dirty:
            dirty_text = "有 %d 条改动正在等下一次上传" % dirty
            dirty_color = C_ACCENT
        else:
            dirty_text = "本地改动都已同步"
            dirty_color = C_MUTED
        self.lbl_cloud_dirty.config(text=dirty_text, fg=dirty_color)

        # 手动备份不要求开关是开的 —— 用户当场点就是想传一次
        can_manual = bool(c["url"] and c["password"]) and not self._cloud_busy
        self.btn_cloud_test.config(state=("disabled" if (self._cloud_busy or not c["url"]) else "normal"))
        if hasattr(self, "btn_cloud_now"):
            self.btn_cloud_now.config(state=("normal" if can_manual else "disabled"))
        # 同步按钮跟「填了地址和密码」绑定，不要求开总开关——
        # 手动同步是明确的用户动作，不该被自动备份的开关挡住
        if hasattr(self, "btn_sync_push"):
            st = "normal" if can_manual else "disabled"
            self.btn_sync_push.config(state=st)
            self.btn_sync_pull.config(state=st)

    def _cloud_mode_key(self):
        return "stock" if "库存" in self.v_cloud_mode.get() and "全部" not in self.v_cloud_mode.get() else "all"

    def test_cloud(self):
        if self._cloud_busy:
            return
        self.cloud.save_config(url=self.v_cloud_url.get().strip(),
                               password=self.v_cloud_pwd.get())
        self._cloud_busy = True
        self.refresh_cloud_state()
        self.btn_cloud_test.config(text="连接中…")

        q = queue.Queue()

        def worker():
            q.put(self.cloud.test_connection())

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                res = q.get_nowait()
            except queue.Empty:
                self.after(200, poll)
                return
            self._cloud_busy = False
            self.btn_cloud_test.config(text="测试连接")
            self.refresh_cloud_state()
            if res.ok:
                messagebox.showinfo(APP_TITLE, res.message, parent=self)
            else:
                messagebox.showerror(APP_TITLE, "连接失败：\n\n%s" % res.message, parent=self)

        self.after(200, poll)

    def upload_cloud_now(self):
        if self._cloud_busy:
            return
        self.cloud.save_config(url=self.v_cloud_url.get().strip(),
                               password=self.v_cloud_pwd.get(),
                               auto=self.v_cloud_auto.get())
        c = self.cloud.config()
        if not c["url"] or not c["password"]:
            messagebox.showwarning(APP_TITLE,
                                   "还差东西：需要填写备份地址和访问密码。", parent=self)
            return
        if not c["enabled"]:
            if not messagebox.askyesno(
                    APP_TITLE,
                    "云端备份的开关是关的。\n\n要临时传这一次吗？（开关保持不变，"
                    "之后记账仍然不会自动上传）", parent=self):
                return
        self._start_cloud_upload(manual=True)

    def _start_cloud_upload(self, manual=False):
        """后台传一次，用轮询回主线程更新界面（Tk 不能跨线程碰控件）。"""
        self._cloud_busy = True
        self.refresh_cloud_state()
        self.btn_cloud_now.config(text="上传中…")
        mode = self._cloud_mode_key()
        q = queue.Queue()
        dirty_before = self.store.dirty_count()

        def worker():
            try:
                res = self.cloud.upload(mode=mode, force=True)
            except Exception as ex:
                res = cloud_mod.UploadResult(False, str(ex))
            q.put(res)

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                res = q.get_nowait()
            except queue.Empty:
                self.after(300, poll)
                return
            self._cloud_busy = False
            self.btn_cloud_now.config(text="立即备份一次")
            if res.ok and dirty_before:
                self.store.clear_dirty()
            self.refresh_cloud_state()
            self.set_status(("备份成功：" if res.ok else "备份失败：") + res.message,
                            C_IN if res.ok else C_OUT)
            if manual:
                if res.ok:
                    messagebox.showinfo(APP_TITLE, res.message, parent=self)
                else:
                    messagebox.showerror(APP_TITLE, "上传失败：\n\n%s" % res.message, parent=self)
            if res.ok:
                self.after(400, self.load_cloud_files)

        self.after(300, poll)

    # ---------------------------------------------------------------- 多端同步

    def _sync_ready(self):
        c = self.cloud.config()
        if not c["url"] or not c["password"]:
            messagebox.showwarning(
                APP_TITLE,
                "同步要用云端备份的地址和密码。\n\n先在上面填好备份地址和访问密码并保存。",
                parent=self)
            return False
        return True

    def sync_push(self):
        """把本机全量数据做成同步包传上去（覆盖云端那份）。"""
        if self._cloud_busy or not self._sync_ready():
            return
        if not messagebox.askyesno(
                APP_TITLE,
                "这会把本机当前的全部数据上传成云端同步包，\n覆盖云端原来那份。\n\n"
                "如果另一台电脑上有云端还没有的记录，\n建议先在那台点「上传」，再回到这台点「从云端同步」。\n\n继续吗？",
                parent=self):
            return

        self._cloud_busy = True
        self.refresh_cloud_state()
        self.btn_sync_push.config(text="上传中…")
        q = queue.Queue()

        def worker():
            try:
                payload = self.sync_engine.export_payload(full=True)
                text = self.sync_engine.payload_to_json(payload)
                res = self.cloud.push_sync(text)
                q.put((res, payload))
            except Exception as ex:
                q.put((cloud_mod.UploadResult(False, str(ex)), None))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                res, payload = q.get_nowait()
            except queue.Empty:
                self.after(300, poll)
                return
            self._cloud_busy = False
            self.btn_sync_push.config(text="上传本机数据到云端")
            self.refresh_cloud_state()
            if res.ok and payload:
                n = (len(payload["tables"]["materials"]), len(payload["tables"]["factories"]),
                     len(payload["tables"]["records"]))
                msg = "已上传：材料 %d、代工厂 %d、流水 %d 条\n\n%s" % (n[0], n[1], n[2], res.message)
                self.lbl_sync_state.config(
                    text="上次上传：%s\n%s" % (now_str(), msg.split("\n")[0]), fg=C_IN)
                messagebox.showinfo(APP_TITLE, msg, parent=self)
            else:
                self.lbl_sync_state.config(text="上传失败：%s" % res.message, fg=C_OUT)
                messagebox.showerror(APP_TITLE, "上传失败：\n\n%s" % res.message, parent=self)

        self.after(300, poll)

    def sync_pull(self):
        """从云端下载同步包，预览后合并进本地。"""
        if self._cloud_busy or not self._sync_ready():
            return
        self._cloud_busy = True
        self.refresh_cloud_state()
        self.btn_sync_pull.config(text="下载中…")
        q = queue.Queue()

        def worker():
            try:
                okk, payload, msg = self.cloud.pull_sync()
                q.put((okk, payload, msg))
            except Exception as ex:
                q.put((False, None, str(ex)))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                okk, payload, msg = q.get_nowait()
            except queue.Empty:
                self.after(300, poll)
                return
            self._cloud_busy = False
            self.btn_sync_pull.config(text="从云端同步")
            self.refresh_cloud_state()

            if not okk or not payload:
                self.lbl_sync_state.config(text="同步失败：%s" % msg, fg=C_OUT)
                messagebox.showerror(APP_TITLE, "下载失败：\n\n%s" % msg, parent=self)
                return

            # 预览：算出会带来什么变化，让用户确认
            try:
                pv = self.sync_engine.preview(payload)
                text = SyncEngine.summary_text(pv)
            except Exception as ex:
                messagebox.showerror(APP_TITLE, "同步包无法识别：\n\n%s" % ex, parent=self)
                return

            detail = []
            names = {"materials": "材料", "factories": "代工厂", "records": "流水"}
            for t in ("materials", "factories", "records"):
                s2 = pv.get(t) or {}
                detail.append("  %s：新增 %d，更新 %d，相同 %d，本地较新 %d" % (
                    names[t], s2.get("add", 0), s2.get("update", 0),
                    s2.get("same", 0), s2.get("local_newer", 0)))

            body = text + "\n\n" + "\n".join(detail) + "\n\n要导入吗？"
            if not messagebox.askyesno(APP_TITLE, body, parent=self):
                self.lbl_sync_state.config(text="已取消，本地数据未改动", fg=C_MUTED)
                return

            # 执行合并
            try:
                merged = self.sync_engine.merge(payload)
            except Exception as ex:
                messagebox.showerror(APP_TITLE, "合并失败：\n\n%s" % ex, parent=self)
                return

            self.refresh_all()
            st = self.store.stats()
            self.lbl_sync_state.config(
                text="上次同步：%s\n材料 %d、代工厂 %d、流水 %d 条" %
                     (now_str(), st["materials"], st["factories"], st["records"]),
                fg=C_IN)
            messagebox.showinfo(
                APP_TITLE,
                "同步完成。\n\n本地现在是：材料 %d、代工厂 %d、流水 %d 条。"
                % (st["materials"], st["factories"], st["records"]),
                parent=self)
            self.auto_cloud_after_save()

        self.after(300, poll)

    def load_cloud_files(self):
        if not self.cloud.config()["url"] or not self.cloud.config()["password"]:
            return
        for iid in self.tv_cloud.get_children():
            self.tv_cloud.delete(iid)
        self.lbl_cloud_list.config(text="读取中…")

        q = queue.Queue()

        def worker():
            q.put(self.cloud.list_remote())

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                files = q.get_nowait()
            except queue.Empty:
                self.after(250, poll)
                return
            for f in files[:200]:
                import datetime
                ts = f.get("updated") or 0
                when = datetime.datetime.fromtimestamp(ts / 1000.0).strftime("%m-%d %H:%M") if ts else ""
                size = f.get("size") or 0
                self.tv_cloud.insert("", "end", values=(
                    f.get("name", ""), _human_size(size), when))
            self.lbl_cloud_list.config(
                text=("云端共 %d 个文件" % len(files)) if files else "云端还没有文件（或读取失败）")

        self.after(250, poll)

    def auto_cloud_after_save(self):
        """记账后调用：只在「启用 + 自动」时才动，且不打扰用户。"""
        if self._cloud_busy:
            return
        c = self.cloud.config()
        if not (c["enabled"] and c["auto"] and c["url"] and c["password"]):
            return
        self._start_cloud_upload(manual=False)

    def export_stock(self):
        path = filedialog.asksaveasfilename(
            parent=self, title="导出库存", defaultextension=".csv",
            initialfile="库存_%s.csv" % today_str().replace("-", ""),
            filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            n = self.store.export_stock_csv(path)
            self.set_status("库存已导出（%d 种材料）" % n, C_IN)
            if messagebox.askyesno(APP_TITLE, "已导出：\n%s\n\n打开所在文件夹？" % path, parent=self):
                os.startfile(os.path.dirname(path))
        except Exception as ex:
            messagebox.showerror(APP_TITLE, "导出失败：%s" % ex, parent=self)

    def do_backup(self):
        try:
            dst = self.store.backup()
            self.set_status("已备份到 %s" % os.path.basename(dst), C_IN)
            messagebox.showinfo(APP_TITLE, "备份完成：\n%s\n\n（自动保留最近 30 份）" % dst, parent=self)
        except Exception as ex:
            messagebox.showerror(APP_TITLE, "备份失败：%s" % ex, parent=self)

    def _first_run_hint(self):
        if not self.store.list_materials(active_only=False):
            if messagebox.askyesno(
                    APP_TITLE,
                    "看起来是第一次使用。\n\n先登记「材料」和「代工厂」，之后在「出入库」页记账。\n"
                    "现在就去登记第一批材料吗？", parent=self):
                self.nb.select(self.tab_materials)
                self.material_dialog(None)

    def on_close(self):
        try:
            self.store.backup(keep=30)
        except Exception:
            pass
        self.store.close()
        self.destroy()


# ==================================================================== 材料编辑弹窗

class MaterialDialog(tk.Toplevel):
    def __init__(self, master, store, mid, on_done=None):
        super().__init__(master)
        self.store = store
        self.mid = mid
        self.on_done = on_done
        self.f = master.font_name
        self.title("修改材料" if mid else "新增材料")
        self.configure(bg=C_BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        m = store.material_by_id(mid) if mid else None
        pad = 16
        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=pad, pady=pad)

        self.v_name = tk.StringVar(value=(m["name"] if m else ""))
        self.v_spec = tk.StringVar(value=(m["spec"] if m else ""))
        self.v_unit = tk.StringVar(value=(m["unit"] if m else ""))
        self.v_cat = tk.StringVar(value=(m["category"] if m else ""))
        self.v_min = tk.StringVar(value=(fmt_qty(m["min_qty"]) if m and m["min_qty"] else ""))
        self.v_note = tk.StringVar(value=(m["note"] if m else ""))
        self.v_quality = tk.BooleanVar(
            value=bool(m and int(m.get("quality_split") or 0)))

        def row(r, label, widget, hint=""):
            tk.Label(body, text=label, bg=C_BG, font=(self.f, 10), fg=C_MUTED, width=8,
                     anchor="e").grid(row=r, column=0, sticky="e", pady=7, padx=(0, 10))
            widget.grid(row=r, column=1, sticky="w", pady=7)
            if hint:
                tk.Label(body, text=hint, bg=C_BG, font=(self.f, 8), fg="#9aa4b1",
                         anchor="w").grid(row=r, column=2, sticky="w", padx=8)

        e_name = ttk.Entry(body, textvariable=self.v_name, width=34)
        row(0, "材料名称", e_name, "必填，如：不锈钢板")
        row(1, "规格型号", ttk.Entry(body, textvariable=self.v_spec, width=34), "如：304 / 2mm（可空）")
        cb_unit = ttk.Combobox(body, textvariable=self.v_unit, width=31,
                               values=[""] + COMMON_UNITS)
        row(2, "单位", cb_unit, "如：千克、件")
        row(3, "分类", ttk.Entry(body, textvariable=self.v_cat, width=34), "如：原材料 / 包材（可空）")
        row(4, "安全库存", ttk.Entry(body, textvariable=self.v_min, width=34),
            "低于此值在总览里标红；留空表示不设")
        row(5, "备注", ttk.Entry(body, textvariable=self.v_note, width=34))

        # 良品 / 不良品：非必选，勾了才在出入库时出现品质下拉
        tk.Label(body, text="品质", bg=C_BG, font=(self.f, 10), fg=C_MUTED, width=8,
                 anchor="e").grid(row=6, column=0, sticky="e", pady=(10, 0), padx=(0, 10))
        qf = tk.Frame(body, bg=C_BG)
        qf.grid(row=6, column=1, columnspan=2, sticky="w", pady=(10, 0))
        tk.Checkbutton(
            qf, text="区分良品 / 不良品", variable=self.v_quality,
            bg=C_BG, fg=C_TEXT, font=(self.f, 10), activebackground=C_BG,
            selectcolor="#ffffff", highlightthickness=0, bd=0, anchor="w",
            cursor="hand2").pack(side="left")
        tk.Label(body, text="勾了之后，记这笔账时可以选良品/不良品；仓库总数 = 良品 + 不良品",
                 bg=C_BG, font=(self.f, 8), fg="#9aa4b1", anchor="w",
                 justify="left").grid(row=7, column=1, columnspan=2, sticky="w",
                                      padx=(0, 8), pady=(0, 4))

        btns = tk.Frame(self, bg=C_BG)
        btns.pack(fill="x", padx=pad, pady=(0, pad))
        ttk.Button(btns, text="保存", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=8)

        self.bind("<Return>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.destroy())
        e_name.focus_set()
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        px, py = master.winfo_rootx(), master.winfo_rooty()
        pw, ph = master.winfo_width(), master.winfo_height()
        self.geometry("+%d+%d" % (px + (pw - w) // 2, py + (ph - h) // 3))

    def save(self):
        name = self.v_name.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "材料名称不能为空", parent=self)
            return
        try:
            minq = float(self.v_min.get().strip() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "安全库存要填数字（或留空）", parent=self)
            return

        qs = 1 if self.v_quality.get() else 0

        # 从「区分」改回「不区分」时，要确认——已记的品质数据不会消失，
        # 但之后不能再按品质记，容易让人困惑，所以先问一句。
        if self.mid and not qs:
            old = self.store.material_by_id(self.mid)
            if old and int(old.get("quality_split") or 0):
                has_q = self.store.conn.execute(
                    "SELECT COUNT(*) n FROM records WHERE material_id=? AND quality<>''",
                    (self.mid,)).fetchone()["n"]
                if has_q:
                    if not messagebox.askyesno(
                            APP_TITLE,
                            "这个材料已经有 %d 条带品质标记的记录。\n\n"
                            "取消勾选后，新的记账不再分良品/不良品；\n"
                            "已有的记录和数量统计都保留。\n\n确定要关掉吗？" % has_q,
                            parent=self):
                        return

        try:
            if self.mid:
                self.store.update_material(
                    self.mid, name=name, spec=self.v_spec.get().strip(),
                    unit=self.v_unit.get().strip(), category=self.v_cat.get().strip(),
                    min_qty=minq, note=self.v_note.get().strip(),
                    quality_split=qs)
            else:
                self.store.add_material(
                    name, spec=self.v_spec.get().strip(), unit=self.v_unit.get().strip(),
                    category=self.v_cat.get().strip(), min_qty=minq,
                    note=self.v_note.get().strip(), quality_split=qs)
        except Exception as ex:
            messagebox.showerror(APP_TITLE, str(ex), parent=self)
            return
        if self.on_done:
            self.on_done()
        self.destroy()


# ==================================================================== 代工厂编辑弹窗

class FactoryDialog(tk.Toplevel):
    def __init__(self, master, store, fid, on_done=None):
        super().__init__(master)
        self.store = store
        self.fid = fid
        self.on_done = on_done
        self.f = master.font_name
        self.title("修改代工厂" if fid else "新增代工厂")
        self.configure(bg=C_BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        f0 = store.factory_by_id(fid) if fid else None
        pad = 16
        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=pad, pady=pad)

        self.v_name = tk.StringVar(value=(f0["name"] if f0 else ""))
        self.v_contact = tk.StringVar(value=(f0["contact"] if f0 else ""))
        self.v_note = tk.StringVar(value=(f0["note"] if f0 else ""))

        def row(r, label, widget):
            tk.Label(body, text=label, bg=C_BG, font=(self.f, 10), fg=C_MUTED, width=8,
                     anchor="e").grid(row=r, column=0, sticky="e", pady=7, padx=(0, 10))
            widget.grid(row=r, column=1, sticky="w", pady=7)

        e_name = ttk.Entry(body, textvariable=self.v_name, width=34)
        row(0, "工厂名称", e_name)
        row(1, "联系人", ttk.Entry(body, textvariable=self.v_contact, width=34))
        row(2, "备注", ttk.Entry(body, textvariable=self.v_note, width=34))

        btns = tk.Frame(self, bg=C_BG)
        btns.pack(fill="x", padx=pad, pady=(0, pad))
        ttk.Button(btns, text="保存", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=8)

        self.bind("<Return>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.destroy())
        e_name.focus_set()
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        px, py = master.winfo_rootx(), master.winfo_rooty()
        pw, ph = master.winfo_width(), master.winfo_height()
        self.geometry("+%d+%d" % (px + (pw - w) // 2, py + (ph - h) // 3))

    def save(self):
        name = self.v_name.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "工厂名称不能为空", parent=self)
            return
        try:
            if self.fid:
                self.store.update_factory(self.fid, name=name,
                                          contact=self.v_contact.get().strip(),
                                          note=self.v_note.get().strip())
            else:
                self.store.add_factory(name, contact=self.v_contact.get().strip(),
                                       note=self.v_note.get().strip())
        except Exception as ex:
            messagebox.showerror(APP_TITLE, str(ex), parent=self)
            return
        if self.on_done:
            self.on_done()
        self.destroy()


# ==================================================================== 回收站

class RecycleBinDialog(tk.Toplevel):
    """
    已删除的流水放在这里。可以「恢复」回列表，或「彻底删除」。

    彻底删除不可逆，而且不会同步到其他设备 ——
    本机删干净了，别的设备还留着，下次同步可能又带回来，所以界面上要讲清楚。
    """

    def __init__(self, master, store, on_done=None):
        super().__init__(master)
        self.store = store
        self.on_done = on_done
        self.f = master.font_name
        self.title("回收站 —— 已删除的流水")
        self.configure(bg=C_BG)
        self.geometry("980x560")
        self.minsize(760, 400)
        self.transient(master)

        top = tk.Frame(self, bg=C_CARD)
        top.pack(fill="x")
        tk.Label(top, text="回收站", font=(self.f, 13, "bold"), bg=C_CARD,
                 fg=C_TEXT).pack(side="left", padx=16, pady=10)
        self.lbl_count = tk.Label(top, text="", bg=C_CARD, fg=C_MUTED, font=(self.f, 9))
        self.lbl_count.pack(side="left", padx=6)

        tk.Frame(self, bg=C_LINE, height=1).pack(fill="x")

        bar = tk.Frame(self, bg=C_BG)
        bar.pack(fill="x", padx=12, pady=8)
        ttk.Button(bar, text="恢复选中", command=self.do_restore).pack(side="left")
        ttk.Button(bar, text="彻底删除选中", command=self.do_purge).pack(side="left", padx=8)
        ttk.Button(bar, text="清空回收站", command=self.do_purge_all).pack(side="left")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="right")
        tk.Label(bar, text="彻底删除后无法恢复；本机删掉不会影响其他设备",
                 bg=C_BG, fg=C_MUTED, font=(self.f, 8)).pack(side="right", padx=10)

        wrap = tk.Frame(self, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        cols = ("ts", "type", "mat", "quality", "fac", "qty", "unit", "batch", "note")
        self.tv = ttk.Treeview(wrap, columns=cols, show="headings", height=16)
        for c, t, w, a, stretch in (
                ("ts", "时间", 150, "center", False), ("type", "类型", 56, "center", False),
                ("mat", "材料", 160, "w", True), ("quality", "品质", 60, "center", False),
                ("fac", "代工厂", 140, "w", True), ("qty", "数量", 84, "e", False),
                ("unit", "单位", 52, "center", False), ("batch", "批次/单号", 120, "w", False),
                ("note", "备注", 180, "w", True)):
            self.tv.heading(c, text=t, anchor=a)
            self.tv.column(c, width=w, anchor=a, stretch=stretch)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=vs.set)
        self.tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv.tag_configure("odd", background="#fafbfd")

        self.bind("<Escape>", lambda _e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.reload()

    def reload(self):
        tv = self.tv
        tv.delete(*tv.get_children())
        rows = self.store.list_records(only_deleted=True, include_voided=True, limit=5000)
        for i, r in enumerate(rows):
            unit = r["unit"] or ""
            sign = "+" if r["delta"] > 0 else ""
            tv.insert("", "end", iid=str(r["id"]),
                      values=(r["ts"], TYPE_LABELS.get(r["type"], r["type"]),
                              r["material_name"] + (" " + r["material_spec"]
                                                    if r["material_spec"] else ""),
                              QUALITY_LABELS.get(r.get("quality") or "", ""),
                              r["factory_name"],
                              sign + fmt_qty(r["delta"]), unit, r["batch"], r["note"]),
                      tags=("odd",) if i % 2 else ())
        self.lbl_count.config(text="共 %d 条" % len(rows))

    def _sel_ids(self):
        return [int(i) for i in self.tv.selection()]

    def _done(self, msg, n):
        self.reload()
        if self.on_done:
            self.on_done()
        if n:
            self.lbl_count.config(text="%s —— 现有 %d 条"
                                  % (msg, len(self.tv.get_children())))

    def do_restore(self):
        ids = self._sel_ids()
        if not ids:
            messagebox.showinfo(APP_TITLE, "先选中要恢复的记录", parent=self)
            return
        n = self.store.restore_records(ids)
        self._done("已恢复 %d 条" % n, n)

    def do_purge(self):
        ids = self._sel_ids()
        if not ids:
            messagebox.showinfo(APP_TITLE, "先选中要彻底删除的记录", parent=self)
            return
        if not messagebox.askyesno(
                APP_TITLE,
                "彻底删除这 %d 条？\n\n删掉之后无法恢复。\n\n"
                "另外：如果开了多端同步，其他设备上的这些记录仍在，"
                "下次同步可能会把它们再带回来。" % len(ids), parent=self):
            return
        n = self.store.purge_records(ids)
        self._done("已彻底删除 %d 条" % n, n)

    def do_purge_all(self):
        ids = [int(i) for i in self.tv.get_children()]
        if not ids:
            messagebox.showinfo(APP_TITLE, "回收站是空的", parent=self)
            return
        if not messagebox.askyesno(
                APP_TITLE,
                "清空回收站？共 %d 条，删除后无法恢复。\n\n"
                "注意：其他设备上如果还有这些记录，同步时可能会带回来。" % len(ids),
                parent=self):
            return
        n = self.store.purge_records(ids)
        self._done("已清空 %d 条" % n, n)

    def close(self):
        self.destroy()


def run():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run()
