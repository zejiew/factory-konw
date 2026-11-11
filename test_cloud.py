# -*- coding: utf-8 -*-
"""
云端备份自测：跑一遍「没配置就不联网」「缺项就不传」「CSV 内容正确」
「上传成功/失败的状态记录」这些策略，不依赖网络。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import Store
import cloud as cloud_mod

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
store = Store(os.path.join(tmp, "c.db"))
cb = cloud_mod.CloudBackup(store)

# ---------------- 空配置
print("\n[空配置]")
c = cb.config()
check(c["url"] == "", "初始地址为空")
check(c["enabled"] is False, "初始未启用")
check(cb.is_ready() is False, "没配置时 is_ready 为 False")

r = cb.upload()
check(r.ok is False, "没配置时上传直接失败，不尝试联网")
check("地址" in r.message or "密码" in r.message, "失败原因说明缺什么")

# ---------------- 只填地址
print("\n[只填地址]")
cb.save_config(url="https://example.com")
check(cb.config()["url"] == "https://example.com", "地址已保存")
r = cb.upload()
check(r.ok is False and "密码" in r.message, "缺密码时指出缺密码")
check(cb.is_ready() is False, "缺密码不算 ready")

# ---------------- 启用但缺项
print("\n[启用但缺项]")
cb.save_config(enabled=True)
check(cb.config()["enabled"] is True, "开关已存")
check(cb.is_ready() is False, "缺密码时即使开关开也不能用")

# ---------------- 配全了但连不通（用不存在的域名）
print("\n[配全但连不通]")
cb.save_config(password="dummy", url="https://this-host-should-not-exist.invalid")
check(cb.is_ready() is True, "三项齐了才算 ready")
r = cb.upload(timeout=8)
check(r.ok is False, "连不通时返回失败而不是抛异常")
check(("连不上" in r.message) or ("HTTP" in r.message) or ("服务器" in r.message),
      "失败信息可读：%s" % r.message)

# 失败也要留下状态记录
st = cb.last_state()
check(st["ts"] != "", "失败也记录了时间")
check(st["result"].startswith("FAIL"), "状态标记为 FAIL")

# ---------------- CSV 内容
print("\n[CSV 生成]")
m1 = store.add_material("不锈钢板", spec="304/2mm", unit="千克", category="原材料", min_qty=100)
m2 = store.add_material("纸箱", spec="60x40", unit="个", category="包材")
f1 = store.add_factory("东莞A厂", contact="张三")
store.add_record("IN", m1, f1, 500, ts="2026-11-01 09:00", batch="PO001", operator="张三")
store.add_record("OUT", m1, f1, 120, ts="2026-11-02 10:00", note="第一批")
store.add_record("IN", m2, f1, 80, ts="2026-11-03 11:00")
rid = store.add_record("IN", m1, f1, 10, ts="2026-11-04 12:00")
store.void_record(rid, True)

full = cb.build_csv("all")
for kw in ("当前库存", "材料清单", "代工厂", "全部流水", "不锈钢板", "东莞A厂",
           "PO001", "已作废", "EOF"):
    check(kw in full, "全量 CSV 含「%s」" % kw)
check("380" in full, "结存 380（500-120）出现在 CSV 里")

stock_only = cb.build_csv("stock")
check("当前库存" in stock_only, "库存模式有库存段")
check("全部流水" not in stock_only, "库存模式不含流水段")
check(len(stock_only) < len(full), "库存 CSV 比全量短")

# 表头与转义
check('"不锈钢板"' in full or "不锈钢板" in full, "中文正常写入")

# ---------------- 文件名
print("\n[文件名]")
fn = cb.default_filename()
check(fn.startswith("代工厂物料台账_") and fn.endswith(".csv"), "默认文件名格式正确: %s" % fn)

# ---------------- 待备份队列
print("\n[改动队列]")
store.clear_dirty()
check(store.dirty_count() == 0, "清空后为 0")
store.add_record("IN", m2, f1, 5)
check(store.dirty_count() == 1, "记一笔产生 1 条改动")
store.add_record("OUT", m2, f1, 2)
store.void_record(rid, False)
check(store.dirty_count() == 3, "三笔操作共 3 条改动")
sm = store.dirty_summary()
check(len(sm) == 3 and sm[0]["action"] == "void_record", "最新的在最上面")

# 材料/工厂操作也进队列
store.clear_dirty()
store.add_material("新料", spec="X")
store.add_factory("新厂")
check(store.dirty_count() == 2, "增材料/增工厂也进队列")

# ---------------- 假服务器：验证成功路径
print("\n[模拟成功上传]")


class FakeResp(object):
    def __init__(self, payload):
        self._p = payload.encode("utf-8")

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener(object):
    def __init__(self, recorder):
        self.rec = recorder

    def open(self, req, timeout=None):
        url = req.full_url
        self.rec.append(url)
        if "/api/login" in url:
            return FakeResp('{"ok":true}')
        if "/api/files" in url:
            return FakeResp('{"ok":true,"results":[{"action":"created",'
                            '"file":{"id":"abc123"}}]}')
        return FakeResp('{"ok":true,"fileCount":3,"totalBytes":2048,"quotaBytes":1073741824}')


recorder = []
fake = FakeOpener(recorder)

orig_opener = cb._opener
cb._opener = lambda jar: fake
cb.save_config(url="https://fake.example", password="pw", enabled=True, auto=True)

r = cb.upload("all", filename="测试备份.csv")
check(r.ok is True, "模拟上传返回成功")
check(r.remote_id == "abc123", "拿到了远端 id")
check(len(recorder) == 2 and "/api/login" in recorder[0], "先登录再上传")
check("name=" in recorder[1], "上传请求带上了文件名参数")
from urllib.parse import unquote
check("测试备份.csv" in unquote(recorder[1]), "URL 解码后文件名正确")
st = cb.last_state()
check(st["result"].startswith("OK"), "状态记为 OK")
check(st["name"] == "测试备份.csv", "状态里记了文件名")

# 文件名里的路径分隔符会被清掉
recorder.clear()
r = cb.upload("all", filename="a/b\\c:d.csv")
check(r.ok is True, "含非法字符的文件名仍能上传")
check("a_b_c_d.csv" in recorder[1], "非法字符被替换成下划线")

# ---------------- 连接测试
recorder.clear()
res = cb.test_connection()
check(res.ok is True, "测试连接成功")
check("3 个文件" in res.message, "报告了云端文件数：%s" % res.message)

# ---------------- 关掉之后完全不联网
print("\n[关掉开关]")
cb.save_config(enabled=False)
check(cb.is_ready() is False, "关掉后 is_ready 为 False")
recorder.clear()
r = cb.upload()
check(len(recorder) == 0, "关掉后不会发出任何网络请求")
check(r.ok is False, "关掉后返回失败而不是假装成功")

# ---------------- 凭据不出现在 CSV 里
print("\n[凭据隔离]")
cb.save_config(url="https://secret.example", password="mypassword123", enabled=True)
payload = cb.build_csv("all")
check("mypassword123" not in payload, "CSV 里不含密码")
check("secret.example" not in payload, "CSV 里不含备份地址")

# ---------------- 设置项独立于业务数据
check(store.get_setting(cloud_mod.K_PASSWORD) == "mypassword123", "密码存在 settings 表")
# 导出业务 CSV 不应带出设置
p = os.path.join(tmp, "x.csv")
store.export_stock_csv(p)
content = open(p, encoding="utf-8-sig").read()
check("mypassword123" not in content, "导出的库存 CSV 不含密码")

store.close()

print("\n通过 %d 项，失败 %d 项" % (ok, len(fail)))
if fail:
    for f in fail:
        print("  ✗", f)
    sys.exit(1)
print("全部通过 ✓")
