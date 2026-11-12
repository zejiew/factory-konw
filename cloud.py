# -*- coding: utf-8 -*-
"""
代工厂物料台账 —— 云端 CSV 备份（可选功能）

设计原则：
  * **纯可选**。没配置就不联网，配置了也只是「记账后顺手传一份」。
  * **不阻塞、不打扰**。上传放在后台线程，失败只记状态，绝不打断记账。
  * **凭据不进源码**。地址/密码存在本地 data/inventory.db 的 settings 表里，
    源码里一个字符都不写。用户自己填、自己开、自己关。
  * **上传失败不影响本地账**。本地永远是权威，云端只是副本。

接口（对端是一个 Cloudflare Pages 的「CSV 备份柜」）：
    POST /api/login        {"password": "..."}   → 拿到 cbk_session cookie
    POST /api/files?name=X ← 原始字节流，content-type: text/csv
    GET  /api/files                              → 文件列表
    GET  /api/stats                              → 配额

已知对端限制（实测）：DELETE /api/files/<id> 和 PATCH 会返回 500（服务端 bug），
所以本模块只做「上传 / 覆盖」，不提供云端删除，避免用户以为删掉了其实没删。
"""

import csv
import io
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
import datetime


# ------------------------------------------------------------------ 设置键名

K_URL = "cloud_url"
K_PASSWORD = "cloud_password"
K_ENABLED = "cloud_enabled"
K_AUTO = "cloud_auto"
K_LAST_TS = "cloud_last_ts"
K_LAST_RESULT = "cloud_last_result"
K_LAST_NAME = "cloud_last_name"

UA = "DSH-InventoryLedger/1.0"

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_OFF = "off"


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 结果对象

class UploadResult(object):
    def __init__(self, ok, message, filename="", remote_id="", action="", ts=None):
        self.ok = ok
        self.message = message
        self.filename = filename
        self.remote_id = remote_id
        self.action = action          # created / versioned / ""
        self.ts = ts or now_str()

    def to_dict(self):
        return {
            "ok": self.ok, "message": self.message, "filename": self.filename,
            "remote_id": self.remote_id, "action": self.action, "ts": self.ts,
        }

    def __repr__(self):
        return "<UploadResult ok=%s %s %s>" % (self.ok, self.filename, self.message)


# ------------------------------------------------------------------ 客户端

class CloudBackup(object):
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()

    # ---------------- 配置读写

    def config(self):
        s = self.store
        return {
            "url": (s.get_setting(K_URL, "") or "").strip(),
            "password": s.get_setting(K_PASSWORD, "") or "",
            "enabled": s.get_setting(K_ENABLED, "0") == "1",
            "auto": s.get_setting(K_AUTO, "1") == "1",
        }

    def save_config(self, url=None, password=None, enabled=None, auto=None):
        s = self.store
        if url is not None:
            s.set_setting(K_URL, url.strip())
        if password is not None:
            s.set_setting(K_PASSWORD, password)
        if enabled is not None:
            s.set_setting(K_ENABLED, "1" if enabled else "0")
        if auto is not None:
            s.set_setting(K_AUTO, "1" if auto else "0")

    def is_ready(self):
        """既开了开关、又填全了地址和密码，才认为可用。"""
        c = self.config()
        return bool(c["enabled"] and c["url"] and c["password"])

    def last_state(self):
        s = self.store
        return {
            "ts": s.get_setting(K_LAST_TS, ""),
            "result": s.get_setting(K_LAST_RESULT, ""),
            "name": s.get_setting(K_LAST_NAME, ""),
        }

    def _record_status(self, result):
        s = self.store
        s.set_setting(K_LAST_TS, result.ts)
        s.set_setting(K_LAST_RESULT,
                      ("OK " if result.ok else "FAIL ") + result.message)
        s.set_setting(K_LAST_NAME, result.filename)

    # ---------------- 底层 HTTP

    def _base(self):
        url = self.config()["url"].strip().rstrip("/")
        if not url:
            raise ValueError("还没有填写备份地址")
        if not url.startswith("http"):
            url = "https://" + url
        return url

    def _opener(self, jar):
        # 用系统默认证书校验（实测该站点证书正常），不做降级
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def _login(self, op, base, password, timeout):
        body = json.dumps({"password": password}).encode("utf-8")
        req = urllib.request.Request(
            base + "/api/login", data=body, method="POST",
            headers={"content-type": "application/json", "User-Agent": UA})
        with op.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        if data.get("ok") is not True:
            raise RuntimeError(data.get("message") or "登录被拒绝（密码可能不对）")

    def _upload_bytes(self, op, base, filename, payload, timeout):
        """
        上传。默认 sameName=false：
          对端（Cloudflare Pages 版）在「同名已存在 → 写新版本」这条分支上有 500 bug，
          而备份每次都用带时间戳的新文件名，两者配合正好绕开。
          万一撞名（同一秒传两次），服务端会自动改名成 xxx (2).csv，不会覆盖。
        """
        q = urllib.parse.urlencode({"name": filename, "sameName": "false"})
        req = urllib.request.Request(
            base + "/api/files?" + q, data=payload, method="POST",
            headers={"content-type": "text/csv; charset=utf-8", "User-Agent": UA})
        with op.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except Exception:
            raise RuntimeError("服务器返回了看不懂的内容：%s" % raw[:120])
        if data.get("ok") is not True:
            raise RuntimeError(data.get("message") or "上传被拒绝")
        results = data.get("results") or []
        if results:
            first = results[0]
            f = first.get("file") or {}
            return f.get("id", ""), first.get("action", "")
        return "", ""

    # ---------------- 生成 CSV 内容

    def build_csv(self, mode="all"):
        """
        mode = all     完整快照（库存 + 全部流水），推荐
               stock   只要当前库存
        """
        st = self.store
        buf = io.StringIO()
        buf.write("# 代工厂物料台账 备份\n")
        buf.write("# 导出时间,%s\n" % now_str())
        buf.write("# 说明,本文件为程序自动生成，可直接用 Excel 打开\n")
        buf.write("\n")

        if mode == "stock":
            buf.write("=== 当前库存 ===\n")
            buf.write("材料,规格,单位,代工厂,结存数量,良品,不良品,未分品质,最近变动\n")
            for item in st.stock_overview():
                m = item["material"]
                split = bool(int(m.get("quality_split") or 0))
                if not item["children"]:
                    buf.write(_row([m["name"], m["spec"], m["unit"], "（无记录）", "0",
                                    "0", "0", "0", ""]))
                    continue
                for c in item["children"]:
                    buf.write(_row([m["name"], m["spec"], m["unit"], c["factory_name"],
                                    _q(c["qty"]),
                                    _q(c["good"]) if split else "",
                                    _q(c["defect"]) if split else "",
                                    _q(c["other"]) if split else "",
                                    c["last_ts"]]))
            return buf.getvalue()

        buf.write("=== 当前库存 ===\n")
        buf.write("材料,规格,单位,分类,代工厂,结存数量,良品,不良品,未分品质,安全库存,最近变动\n")
        for item in st.stock_overview():
            m = item["material"]
            split = bool(int(m.get("quality_split") or 0))
            if not item["children"]:
                buf.write(_row([m["name"], m["spec"], m["unit"], m["category"],
                                "（无记录）", "0", "0", "0", "0",
                                _q(m["min_qty"]), ""]))
                continue
            for c in item["children"]:
                buf.write(_row([m["name"], m["spec"], m["unit"], m["category"],
                                c["factory_name"], _q(c["qty"]),
                                _q(c["good"]) if split else "",
                                _q(c["defect"]) if split else "",
                                _q(c["other"]) if split else "",
                                _q(m["min_qty"]), c["last_ts"]]))
        buf.write("\n")

        buf.write("=== 材料清单 ===\n")
        buf.write("材料,规格,单位,分类,安全库存,品质区分,备注,状态\n")
        for m in st.list_materials(active_only=False):
            buf.write(_row([m["name"], m["spec"], m["unit"], m["category"],
                            _q(m["min_qty"]),
                            "良品/不良品" if int(m.get("quality_split") or 0) else "",
                            m["note"],
                            "在用" if m["active"] else "已停用"]))
        buf.write("\n")

        buf.write("=== 代工厂 ===\n")
        buf.write("代工厂,联系人,备注,状态\n")
        for f in st.list_factories(active_only=False):
            buf.write(_row([f["name"], f["contact"], f["note"],
                            "在用" if f["active"] else "已停用"]))
        buf.write("\n")

        from db import TYPE_LABELS, QUALITY_LABELS
        buf.write("=== 全部流水 ===\n")
        buf.write("时间,类型,材料,规格,品质,单位,代工厂,数量,批次/单号,经办人,备注,状态\n")
        rows = st.list_records(include_voided=True, limit=200000)
        for r in rows:
            buf.write(_row([r["ts"], TYPE_LABELS.get(r["type"], r["type"]),
                            r["material_name"], r["material_spec"],
                            QUALITY_LABELS.get(r.get("quality") or "", ""), r["unit"],
                            r["factory_name"], _q(r["delta"]), r["batch"],
                            r["operator"], r["note"],
                            "已作废" if r["voided"] else "有效"]))
        buf.write("\n=== EOF ===\n")
        return buf.getvalue()

    def default_filename(self):
        """
        每次都用带时间戳的新名字 —— 既避开对端「同名写新版本」的 500 bug，
        又天然形成按时间排列的历史备份，还能在网页上直接挑某个时刻的版本。
        """
        return "代工厂物料台账_%s.csv" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # ---------------- 上传（同步）

    def upload(self, mode="all", filename=None, timeout=45, force=False):
        """同步上传。失败不抛异常，返回 UploadResult。

        force=True 只给「用户当场点了立即备份」用；其余情况开关关着就不联网。
        """
        c = self.config()
        if not c["url"]:
            return UploadResult(False, "没填备份地址")
        if not c["password"]:
            return UploadResult(False, "没填访问密码")
        if not force and not c["enabled"]:
            return UploadResult(False, "云端备份未启用（开关是关的）")
        filename = filename or self.default_filename()
        if not filename.lower().endswith(".csv"):
            filename += ".csv"
        # 文件名里不能有路径分隔符之类
        filename = filename.replace("/", "_").replace("\\", "_").replace(":", "_")

        try:
            payload = self.build_csv(mode).encode("utf-8-sig")
        except Exception as ex:
            r = self._record_and_return(False, "生成 CSV 失败：%s" % ex, filename)
            return r

        base = None
        try:
            base = self._base()
            jar = http.cookiejar.CookieJar()
            op = self._opener(jar)
            self._login(op, base, c["password"], timeout)
            rid, action = self._upload_bytes(op, base, filename, payload, timeout)
            size_kb = max(1, len(payload) // 1024)
            msg = "已上传 %s（%d KB）" % (filename, size_kb)
            if action == "versioned":
                msg += "，同名文件自动留了旧版本"
            return self._record_and_return(True, msg, filename, rid, action)
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
            if ex.code == 401:
                msg = "密码不对或会话失效"
            elif ex.code == 403:
                msg = "没有权限"
            elif ex.code == 413:
                msg = "文件太大，服务器不收"
            else:
                msg = "服务器返回 HTTP %d" % ex.code
            if detail:
                msg += "（%s）" % detail
            return self._record_and_return(False, msg, filename)
        except urllib.error.URLError as ex:
            reason = getattr(ex, "reason", ex)
            return self._record_and_return(False, "连不上服务器：%s" % reason, filename)
        except Exception as ex:
            return self._record_and_return(False, str(ex), filename)

    def _record_and_return(self, ok, msg, filename, rid="", action=""):
        r = UploadResult(ok, msg, filename, rid, action)
        try:
            self._record_status(r)
        except Exception:
            pass
        return r

    # ---------------- 上传（后台线程，记账后调用）

    def upload_async(self, mode="all", on_done=None):
        """后台跑，立刻返回。on_done(result) 在 Tk 主线程不安全，
        所以 on_done 只在非 UI 场景用；UI 请轮询 last_state()。"""
        def worker():
            with self._lock:
                res = self.upload(mode)
            if on_done:
                try:
                    on_done(res)
                except Exception:
                    pass
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t

    # ---------------- 测试连接（只登录 + 读统计，不传数据）

    def test_connection(self, timeout=20):
        c = self.config()
        if not c["url"]:
            return UploadResult(False, "没填备份地址")
        if not c["password"]:
            return UploadResult(False, "没填访问密码")
        try:
            base = self._base()
            jar = http.cookiejar.CookieJar()
            op = self._opener(jar)
            self._login(op, base, c["password"], timeout)
            req = urllib.request.Request(base + "/api/stats",
                                         headers={"User-Agent": UA})
            with op.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            n = data.get("fileCount", 0)
            used = data.get("totalBytes", 0) or 0
            quota = data.get("quotaBytes", 0) or 0
            msg = "连接正常，云端已有 %d 个文件（已用 %.1f KB）" % (n, used / 1024.0)
            if quota:
                msg += "，配额 %.1f GB" % (quota / 1024.0 / 1024.0 / 1024.0)
            return UploadResult(True, msg)
        except urllib.error.HTTPError as ex:
            if ex.code == 401:
                return UploadResult(False, "密码不对")
            return UploadResult(False, "服务器返回 HTTP %d" % ex.code)
        except urllib.error.URLError as ex:
            return UploadResult(False, "连不上服务器：%s" % getattr(ex, "reason", ex))
        except Exception as ex:
            return UploadResult(False, str(ex))

    # ---------------- 远端文件列表（只读，用于展示）

    def list_remote(self, timeout=20):
        try:
            base = self._base()
            jar = http.cookiejar.CookieJar()
            op = self._opener(jar)
            self._login(op, base, self.config()["password"], timeout)
            req = urllib.request.Request(base + "/api/files", headers={"User-Agent": UA})
            with op.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data.get("files") or []
        except Exception:
            return []


# ------------------------------------------------------------------ 工具

def _row(cells):
    """写一行 CSV，自动加引号转义。"""
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerow(["" if c is None else str(c) for c in cells])
    return out.getvalue()


def _q(v):
    from db import fmt_qty
    return fmt_qty(v)
