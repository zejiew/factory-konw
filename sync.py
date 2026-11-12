# -*- coding: utf-8 -*-
"""
多端同步引擎

核心问题：两台电脑各自记的账，本地 id 都是 1、2、3，直接合并会撞车。
解法：每条数据都有全局唯一的 uid，合并靠 uid 认「是不是同一条」。

合并规则（三种情况）：
  1. 本地没有这条 uid          → 新增（从远端导入）
  2. 本地有，且远端 updated_at 更新 → 用远端覆盖本地
  3. 本地有，且本地 updated_at 更新  → 保留本地，不用远端

这叫「最后写入者胜出」（Last-Write-Wins）。它不能解决「两端同一秒改同一条」
这种极端情况，但日常记账不会撞上——记账本来就是各记各的流水。

删除用软删除（deleted=1），不能真删。真删的话另一端同步时不知道，
会把这条当成新数据又导回来，形成「删不掉的幽灵记录」。
"""

import datetime
import json


SCHEMA_VERSION = 1

# 同步包包含哪几张表，以及每张表的业务字段
TABLES = {
    "materials": ["name", "spec", "unit", "category", "min_qty", "note",
                  "active", "quality_split"],
    "factories": ["name", "contact", "note", "active"],
    "records": ["ts", "type", "material_id", "factory_id", "delta", "quality",
                "batch", "operator", "note", "voided"],
}

# 同步时需要额外带的元字段（不是业务数据，但要参与合并判断）
META_FIELDS = ["uid", "updated_at", "deleted"]


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts_key(s):
    """把时间字符串转成可比较的值。空值当作最旧。"""
    if not s:
        return ""
    return str(s)


class SyncEngine(object):
    """
    导出 / 合并。所有方法都在数据库上直接操作，不做网络。
    网络部分见 cloud.SyncTransport。
    """

    def __init__(self, store):
        self.store = store

    # ---------------------------------------------------------------- 导出

    def export_payload(self, full=True, since=None):
        """
        生成同步包。

        full=True  导出全部（包括已删除的，否则另一端无法知道删除）
        since      只导这个时间之后改动的（增量）。None = 全量。
        """
        st = self.store
        conn = st.conn
        out = {
            "kind": "inventory-sync",
            "version": SCHEMA_VERSION,
            "exported_at": now_str(),
            "tables": {},
        }

        # 材料 / 工厂的本地 id → uid 映射，流水里的 material_id 要换成本地 id，
        # 但为了让另一端能对应上，额外带一份 uid 映射表
        mat_id2uid = {}
        fac_id2uid = {}

        for table in ("materials", "factories", "records"):
            sql = "SELECT * FROM %s" % table
            args = []
            if since and not full:
                sql += " WHERE updated_at > ?"
                args.append(since)
            rows = conn.execute(sql, args).fetchall()
            items = []
            for r in rows:
                d = {}
                for k in ("id",) + tuple(TABLES[table]) + tuple(META_FIELDS):
                    if k in r.keys():
                        d[k] = r[k]
                items.append(d)
                if table == "materials":
                    mat_id2uid[r["id"]] = r["uid"]
                elif table == "factories":
                    fac_id2uid[r["id"]] = r["uid"]
            out["tables"][table] = items

        # 流水里的 material_id / factory_id 是本地 id，换成本地 uid 才能跨机器认人
        for rec in out["tables"]["records"]:
            rec["material_uid"] = mat_id2uid.get(rec.get("material_id"), "")
            rec["factory_uid"] = fac_id2uid.get(rec.get("factory_id"), "")

        out["id_map"] = {
            "materials": {str(k): v for k, v in mat_id2uid.items()},
            "factories": {str(k): v for k, v in fac_id2uid.items()},
        }
        return out

    def payload_to_json(self, payload):
        return json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)

    # ---------------------------------------------------------------- 预检

    def preview(self, payload):
        """
        只统计不写入，用来给用户看「这次同步会带来什么」。
        """
        if not isinstance(payload, dict) or payload.get("kind") != "inventory-sync":
            raise ValueError("这不是本程序生成的同步包")

        st = self.store
        conn = st.conn
        summary = {}
        for table in ("materials", "factories", "records"):
            incoming = payload.get("tables", {}).get(table) or []
            local = {}
            for r in conn.execute("SELECT uid, updated_at, deleted FROM %s" % table):
                local[r["uid"]] = r

            add = upd = same = newer_local = 0
            for it in incoming:
                uid = it.get("uid") or ""
                if not uid:
                    continue
                if uid not in local:
                    add += 1
                else:
                    lt = _ts_key(local[uid]["updated_at"])
                    rt = _ts_key(it.get("updated_at"))
                    if rt > lt:
                        upd += 1
                    elif rt == lt:
                        same += 1
                    else:
                        newer_local += 1
            summary[table] = {
                "incoming": len(incoming),
                "add": add,
                "update": upd,
                "same": same,
                "local_newer": newer_local,
            }
        return summary

    @staticmethod
    def summary_text(summary):
        names = {"materials": "材料", "factories": "代工厂", "records": "流水"}
        lines = []
        total_add = total_upd = 0
        for t in ("materials", "factories", "records"):
            s = summary.get(t) or {}
            a, u = s.get("add", 0), s.get("update", 0)
            total_add += a
            total_upd += u
            if a or u:
                lines.append("%s：新增 %d 条，更新 %d 条" % (names[t], a, u))
        if not lines:
            return "云端和本地一样，没有需要同步的内容。"
        return "；".join(lines) + "（共新增 %d、更新 %d）" % (total_add, total_upd)

    # ---------------------------------------------------------------- 合并

    def merge(self, payload, dry_run=False):
        """
        把远端同步包合并进本地。

        返回 dict：{table: {"add":n, "update":n, "skip":n}}
        dry_run=True 时只算不写。
        """
        if not isinstance(payload, dict) or payload.get("kind") != "inventory-sync":
            raise ValueError("这不是本程序生成的同步包")
        ver = payload.get("version")
        if ver and int(ver) > SCHEMA_VERSION:
            raise ValueError("同步包版本比本程序新（v%s），请先升级程序" % ver)

        st = self.store
        conn = st.conn
        result = {t: {"add": 0, "update": 0, "skip": 0} for t in TABLES}

        if dry_run:
            return self.preview(payload)

        # ---- 第一步：先导材料和代工厂，因为流水要引用它们的 id
        uid_to_matid = {}
        for r in conn.execute("SELECT id, uid FROM materials"):
            uid_to_matid[r["uid"]] = r["id"]

        for it in payload.get("tables", {}).get("materials") or []:
            self._merge_material(it, uid_to_matid, result["materials"])

        uid_to_facid = {}
        for r in conn.execute("SELECT id, uid FROM factories"):
            uid_to_facid[r["uid"]] = r["id"]

        for it in payload.get("tables", {}).get("factories") or []:
            self._merge_factory(it, uid_to_facid, result["factories"])

        # 刷新映射（合并过程中可能有新增）
        uid_to_matid = {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM materials")}
        uid_to_facid = {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM factories")}

        # ---- 第二步：流水
        for it in payload.get("tables", {}).get("records") or []:
            self._merge_record(it, uid_to_matid, uid_to_facid, result["records"])

        conn.commit()
        try:
            st.mark_dirty("sync_merge", "合并远端数据")
        except Exception:
            pass
        return result

    # ---------------------------------------------------------------- 单条合并

    def _local_by_uid(self, table, uid):
        return self.store.conn.execute(
            "SELECT * FROM %s WHERE uid=?" % table, (uid,)).fetchone()

    def _merge_material(self, it, uid_to_id, counter):
        conn = self.store.conn
        uid = (it.get("uid") or "").strip()
        if not uid:
            counter["skip"] += 1
            return

        cur = self._local_by_uid("materials", uid)
        remote_ts = _ts_key(it.get("updated_at"))

        if cur is None:
            # 新增。注意 (name, spec) 有唯一索引，可能和本地已存在的同名牌冲突
            name = it.get("name") or ""
            spec = it.get("spec") or ""
            if not name:
                counter["skip"] += 1
                return
            dup = conn.execute(
                "SELECT id FROM materials WHERE name=? AND spec=? AND uid<>?",
                (name, spec, uid)).fetchone()
            if dup:
                # 同名称同规格已存在（可能是两台机器各建了一个同名材料）
                # 不覆盖，把远端这条记成已删除状态，避免反复冲突
                counter["skip"] += 1
                uid_to_id[uid] = dup["id"]
                return
            cur2 = conn.execute(
                "INSERT INTO materials(name, spec, unit, category, min_qty, note, active,"
                " quality_split, uid, updated_at, deleted, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, spec, it.get("unit") or "", it.get("category") or "",
                 float(it.get("min_qty") or 0), it.get("note") or "",
                 int(it.get("active", 1)), int(it.get("quality_split") or 0),
                 uid, remote_ts or now_str(),
                 int(it.get("deleted") or 0), now_str()))
            uid_to_id[uid] = cur2.lastrowid
            counter["add"] += 1
            return

        lt = _ts_key(cur["updated_at"])
        if remote_ts <= lt:
            counter["skip"] += 1
            uid_to_id[uid] = cur["id"]
            return

        conn.execute(
            "UPDATE materials SET name=?, spec=?, unit=?, category=?, min_qty=?,"
            " note=?, active=?, quality_split=?, updated_at=?, deleted=? WHERE uid=?",
            (it.get("name") or cur["name"], it.get("spec") or "",
             it.get("unit") or "", it.get("category") or "",
             float(it.get("min_qty") or 0), it.get("note") or "",
             int(it.get("active", 1)), int(it.get("quality_split") or 0),
             remote_ts, int(it.get("deleted") or 0), uid))
        counter["update"] += 1
        uid_to_id[uid] = cur["id"]

    def _merge_factory(self, it, uid_to_id, counter):
        conn = self.store.conn
        uid = (it.get("uid") or "").strip()
        if not uid:
            counter["skip"] += 1
            return

        cur = self._local_by_uid("factories", uid)
        remote_ts = _ts_key(it.get("updated_at"))

        if cur is None:
            name = it.get("name") or ""
            if not name:
                counter["skip"] += 1
                return
            dup = conn.execute("SELECT id FROM factories WHERE name=? AND uid<>?",
                               (name, uid)).fetchone()
            if dup:
                counter["skip"] += 1
                uid_to_id[uid] = dup["id"]
                return
            cur2 = conn.execute(
                "INSERT INTO factories(name, contact, note, active, uid, updated_at,"
                " deleted, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (name, it.get("contact") or "", it.get("note") or "",
                 int(it.get("active", 1)), uid, remote_ts or now_str(),
                 int(it.get("deleted") or 0), now_str()))
            uid_to_id[uid] = cur2.lastrowid
            counter["add"] += 1
            return

        lt = _ts_key(cur["updated_at"])
        if remote_ts <= lt:
            counter["skip"] += 1
            uid_to_id[uid] = cur["id"]
            return

        conn.execute(
            "UPDATE factories SET name=?, contact=?, note=?, active=?, updated_at=?,"
            " deleted=? WHERE uid=?",
            (it.get("name") or cur["name"], it.get("contact") or "",
             it.get("note") or "", int(it.get("active", 1)), remote_ts,
             int(it.get("deleted") or 0), uid))
        counter["update"] += 1
        uid_to_id[uid] = cur["id"]

    def _merge_record(self, it, uid_to_matid, uid_to_facid, counter):
        conn = self.store.conn
        uid = (it.get("uid") or "").strip()
        if not uid:
            counter["skip"] += 1
            return

        # 靠 uid 找本地对应的材料/工厂；找不到就跳过（说明材料还没同步过来）
        mat_uid = it.get("material_uid") or ""
        fac_uid = it.get("factory_uid") or ""
        mid = uid_to_matid.get(mat_uid)
        fid = uid_to_facid.get(fac_uid)
        if mid is None or fid is None:
            counter["skip"] += 1
            return

        cur = self._local_by_uid("records", uid)
        remote_ts = _ts_key(it.get("updated_at"))

        if cur is None:
            conn.execute(
                "INSERT INTO records(ts, type, material_id, factory_id, delta, quality,"
                " batch, operator, note, voided, uid, updated_at, deleted, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (it.get("ts") or now_str(), it.get("type") or "IN", mid, fid,
                 float(it.get("delta") or 0), it.get("quality") or "",
                 it.get("batch") or "", it.get("operator") or "", it.get("note") or "",
                 int(it.get("voided") or 0), uid, remote_ts or now_str(),
                 int(it.get("deleted") or 0), now_str()))
            counter["add"] += 1
            return

        lt = _ts_key(cur["updated_at"])
        if remote_ts <= lt:
            counter["skip"] += 1
            return

        conn.execute(
            "UPDATE records SET ts=?, type=?, material_id=?, factory_id=?, delta=?,"
            " quality=?, batch=?, operator=?, note=?, voided=?, updated_at=?, deleted=?"
            " WHERE uid=?",
            (it.get("ts") or cur["ts"], it.get("type") or cur["type"], mid, fid,
             float(it.get("delta") or 0), it.get("quality") or "",
             it.get("batch") or "", it.get("operator") or "", it.get("note") or "",
             int(it.get("voided") or 0), remote_ts, int(it.get("deleted") or 0), uid))
        counter["update"] += 1
