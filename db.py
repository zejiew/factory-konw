# -*- coding: utf-8 -*-
"""
代工厂物料台账 —— 数据层

设计要点：
  * 所有数量变化都只记录在 records 表里（一条一条的流水），库存不单独存一份。
    这样永远不会出现「流水跟结存对不上」的情况，作废一条流水结存自动跟着变。
  * delta 是带符号的增量：入库 +、消耗 -、退回 -、盘点 ±（= 实际 - 账面）。
  * 数据全部保存在本机 data/inventory.db（SQLite 单文件），可以随时整包复制走。
"""

import os
import sys
import csv
import uuid
import shutil
import sqlite3
import datetime
import threading

# ---------------------------------------------------------------- 路径

def base_dir():
    """程序所在目录（打包成 exe 后就是 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = os.path.join(base_dir(), "data")
DB_PATH = os.path.join(DATA_DIR, "inventory.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

# ---------------------------------------------------------------- 常量

TYPE_LABELS = {"IN": "入库", "OUT": "消耗", "RETURN": "退回", "ADJUST": "盘点"}
LABEL_TYPES = {v: k for k, v in TYPE_LABELS.items()}

# 良品 / 不良品。空字符串 = 该材料不区分品质（默认行为）。
# 注意：这里只管「这一笔流水算良品还是不良品」，仓库总数永远是两者之和，
# 不额外存一个总数字段，避免出现「良品+不良品 ≠ 总数」的账实不符。
QUALITY_LABELS = {"": "", "good": "良品", "defect": "不良品"}
QUALITY_GOOD = "good"
QUALITY_DEFECT = "defect"

COMMON_UNITS = ["个", "件", "套", "千克", "克", "吨", "米", "平方米", "立方米", "升", "卷", "张", "箱", "包", "袋"]

EPS = 1e-9


def _locked(fn):
    """
    给 Store 的公开方法自动加锁。

    为什么要这个：云端备份在线程里读库生成 CSV，而 sqlite3 的连接不是线程安全的。
    这里用实例上的 self.lock 把所有公开方法串起来——重入锁，方法之间互相调用
    （比如 stock_overview 里会调 list_materials）不会死锁。
    __init__ / _create_tables / close 不加锁（那时锁还没建好，或已在收尾）。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        lock = getattr(self, "lock", None)
        if lock is None:
            return fn(self, *a, **kw)
        with lock:
            return fn(self, *a, **kw)

    return wrapper


def fmt_qty(v):
    """数量显示：去掉多余的小数零，整数不带 .0。"""
    if v is None:
        return "0"
    v = round(float(v), 4)
    if abs(v) < EPS:
        return "0"
    if abs(v - round(v)) < EPS:
        return str(int(round(v)))
    return ("%.4f" % v).rstrip("0").rstrip(".")


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def now_full():
    """
    带秒的时间戳，专供 updated_at 用。

    为什么不能复用 now_str：它只到分钟。同一分钟内连记两笔账，
    updated_at 会完全相同，增量同步用 ">" 比较时就会把旧的一起带上，
    多端合并时也判断不出「谁后改的」。
    """
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 同步用的 uid
#
# 两类数据用两种 uid 策略，这个区别很关键：
#
#   材料 / 代工厂 —— 用「名称+规格」算确定性 UUID5。
#     这样两台电脑各自建「不锈钢板 / 304」，算出来的 uid 相同，
#     合并时会认成同一条，而不是变成两条一模一样的材料。
#     代价：改名等于换身份（会被当成新材料）。这是可接受的取舍，
#     因为材料名本来就该稳定，改名时两端也通常都会改。
#
#   流水 —— 用随机 UUID4。
#     每笔账都是独立事件，哪怕时间和内容完全相同，也必须区分成两条。

UID_NS = uuid.UUID("6f1c1e2a-33d4-5a7b-9c8e-0b1d2a3f4e5d")


def material_uid(name, spec):
    """材料 uid：由名称+规格确定性生成，跨设备一致。"""
    key = "%s\x1f%s" % ((name or "").strip(), (spec or "").strip())
    return str(uuid.uuid5(UID_NS, "material:" + key))


def factory_uid(name):
    """代工厂 uid：由名称确定性生成，跨设备一致。"""
    return str(uuid.uuid5(UID_NS, "factory:" + (name or "").strip()))


def new_record_uid():
    """流水 uid：随机，每笔账独立。"""
    return str(uuid.uuid4())


def today_str():
    return datetime.date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 数据库

class Store(object):
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        # check_same_thread=False：云端备份要在线程里读库生成 CSV。
        # 用 self.lock 把所有读写串起来，保证同一时刻只有一个线程碰连接。
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    # ---------------- 建表

    def _create_tables(self):
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS materials (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                spec       TEXT    NOT NULL DEFAULT '',
                unit       TEXT    NOT NULL DEFAULT '',
                category   TEXT    NOT NULL DEFAULT '',
                min_qty    REAL    NOT NULL DEFAULT 0,
                note       TEXT    NOT NULL DEFAULT '',
                active     INTEGER NOT NULL DEFAULT 1,
                quality_split INTEGER NOT NULL DEFAULT 0,
                uid        TEXT    NOT NULL DEFAULT '',
                updated_at TEXT    NOT NULL DEFAULT '',
                deleted    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material ON materials(name, spec);

            CREATE TABLE IF NOT EXISTS factories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL UNIQUE,
                contact    TEXT    NOT NULL DEFAULT '',
                note       TEXT    NOT NULL DEFAULT '',
                active     INTEGER NOT NULL DEFAULT 1,
                uid        TEXT    NOT NULL DEFAULT '',
                updated_at TEXT    NOT NULL DEFAULT '',
                deleted    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                type        TEXT    NOT NULL,
                material_id INTEGER NOT NULL,
                factory_id  INTEGER NOT NULL,
                delta       REAL    NOT NULL,
                batch       TEXT    NOT NULL DEFAULT '',
                operator    TEXT    NOT NULL DEFAULT '',
                note        TEXT    NOT NULL DEFAULT '',
                quality     TEXT    NOT NULL DEFAULT '',
                voided      INTEGER NOT NULL DEFAULT 0,
                uid         TEXT    NOT NULL DEFAULT '',
                updated_at  TEXT    NOT NULL DEFAULT '',
                deleted     INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_rec_mat ON records(material_id);
            CREATE INDEX IF NOT EXISTS ix_rec_fac ON records(factory_id);
            CREATE INDEX IF NOT EXISTS ix_rec_ts  ON records(ts);

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            -- 云端备份的改动流水：本地每记一笔就往这里塞一条，
            -- 备份线程攒够一批再传，避免每敲一次就发一次网络请求。
            CREATE TABLE IF NOT EXISTS cloud_dirty (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                action     TEXT NOT NULL,
                detail     TEXT NOT NULL DEFAULT ''
            );
            """
        )
        c.commit()
        self._migrate()

    def _migrate(self):
        """
        老的库缺列时补上。全部用 ADD COLUMN，不动已有数据。

        加过的列：
          materials.quality_split  该材料是否启用良品/不良品区分
          records.quality          这一笔流水算良品还是不良品

        多端同步相关（uid / updated_at / deleted）：
          三张业务表都要，见 _migrate_sync()
        """
        c = self.conn

        def cols(table):
            return {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}

        if "quality_split" not in cols("materials"):
            c.execute("ALTER TABLE materials ADD COLUMN quality_split INTEGER NOT NULL DEFAULT 0")
            c.commit()

        if "quality" not in cols("records"):
            c.execute("ALTER TABLE records ADD COLUMN quality TEXT NOT NULL DEFAULT ''")
            c.commit()

        self._migrate_sync()

    def _migrate_sync(self):
        """
        多端同步需要的三样东西，加到 materials / factories / records：

          uid         全局唯一 id（UUID）。本地 id 在不同电脑上会重复，
                      合并时必须靠它认「这是不是同一条」。
          updated_at  最后一次修改时间。两端同时改一条时，用它判断谁更新。
          deleted     软删除标记。真删掉的话，另一端同步时没法知道「这条被删了」，
                      会把它当成新数据又导回来。所以只打标记不真删。

        老数据补 uid：用 表名+id 生成确定性的 uuid，保证同一行每次算出来一样。
        """
        import uuid as _uuid

        c = self.conn

        def cols(table):
            return {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}

        for table in ("materials", "factories", "records"):
            have = cols(table)
            changed = False
            if "uid" not in have:
                c.execute("ALTER TABLE %s ADD COLUMN uid TEXT NOT NULL DEFAULT ''" % table)
                changed = True
            if "updated_at" not in have:
                c.execute("ALTER TABLE %s ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''" % table)
                changed = True
            if "deleted" not in have:
                c.execute("ALTER TABLE %s ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0" % table)
                changed = True
            if changed:
                c.commit()

            # 关键顺序：先把空 uid 全部补上，再建唯一索引。
            # 反过来会直接失败——所有老数据 uid 都是 ''，加唯一索引时互相冲突。
            #
            # 补 uid 的规则跟新建时保持一致：
            #   材料 / 工厂 → 按名称(+规格)确定性生成，这样另一端建的同名项能对上
            #   流水        → 老流水没有更合适的种子，用确定性 UUID5 保证同一条稳定
            rows = c.execute("SELECT * FROM %s WHERE uid=''" % table).fetchall()
            for r in rows:
                if table == "materials":
                    u = material_uid(r["name"], r["spec"])
                elif table == "factories":
                    u = factory_uid(r["name"])
                else:
                    u = str(uuid.uuid5(UID_NS, "legacy-record:%s:%s" % (table, r["id"])))
                c.execute("UPDATE %s SET uid=?, updated_at=COALESCE(NULLIF(updated_at,''), ?)"
                          " WHERE id=?" % table, (u, now_full(), r["id"]))
            if rows:
                c.commit()

            # 兜底：updated_at 还空着的补上
            c.execute("UPDATE %s SET updated_at=? WHERE updated_at=''" % table, (now_full(),))
            c.commit()

            # 内容补齐之后才建唯一索引
            try:
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_%s_uid ON %s(uid)"
                          % (table, table))
                c.commit()
            except sqlite3.IntegrityError:
                # 万一历史数据里真有重复 uid（手工改过库），退化成普通索引，
                # 同步仍能工作（按 uid 查第一条），只是不再强制唯一。
                c.rollback()
                c.execute("CREATE INDEX IF NOT EXISTS ix_%s_uid ON %s(uid)" % (table, table))
                c.commit()

        # ---------------- 把材料/工厂的 uid 统一成确定性值 ----------------
        #
        # 早期版本给材料/工厂用的是随机 uuid4，导致两台电脑各自建的同名材料
        # 得到不同 uid，合并时会被当成两条不同材料。
        # 这里把所有材料/工厂的 uid 重算成「名称+规格」派生的 UUID5，
        # 让跨设备同名项能自动对上。
        #
        # 只改 materials / factories，不动 records：
        # 流水是独立事件，uid 必须保持稳定，重算会让已同步的数据对不上。
        self._normalize_entity_uids()

    def _normalize_entity_uids(self):
        """把材料/工厂 uid 收敛到确定性值。已经正确的不动，避免无谓写入。"""
        c = self.conn
        changed = 0

        for r in c.execute("SELECT id, name, spec, uid FROM materials").fetchall():
            want = material_uid(r["name"], r["spec"])
            if r["uid"] != want:
                # 目标 uid 可能已被另一条占着（改名后撞车），那就跳过这条
                taken = c.execute("SELECT id FROM materials WHERE uid=? AND id<>?",
                                  (want, r["id"])).fetchone()
                if taken:
                    continue
                c.execute("UPDATE materials SET uid=? WHERE id=?", (want, r["id"]))
                changed += 1

        for r in c.execute("SELECT id, name, uid FROM factories").fetchall():
            want = factory_uid(r["name"])
            if r["uid"] != want:
                taken = c.execute("SELECT id FROM factories WHERE uid=? AND id<>?",
                                  (want, r["id"])).fetchone()
                if taken:
                    continue
                c.execute("UPDATE factories SET uid=? WHERE id=?", (want, r["id"]))
                changed += 1

        if changed:
            c.commit()

    # ---------------- 设置项

    @_locked
    def get_setting(self, key, default=""):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    @_locked
    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ---------------- 材料

    @_locked
    def list_materials(self, keyword="", active_only=True, with_stock=False):
        sql = "SELECT * FROM materials WHERE 1=1"
        args = []
        if active_only:
            sql += " AND active=1"
        if keyword:
            sql += " AND (name LIKE ? OR spec LIKE ? OR category LIKE ?)"
            k = "%%%s%%" % keyword
            args += [k, k, k]
        sql += " ORDER BY name COLLATE NOCASE, spec"
        rows = [dict(r) for r in self.conn.execute(sql, args).fetchall()]
        if with_stock:
            for r in rows:
                r["qty"] = self.material_total(r["id"])
        return rows

    @_locked
    def material_options(self):
        return [(r["id"], material_display(r)) for r in self.list_materials(active_only=False)]

    @_locked
    def material_by_id(self, mid):
        row = self.conn.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
        return dict(row) if row else None

    @_locked
    def material_total(self, material_id):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(delta),0) q FROM records WHERE material_id=? AND voided=0",
            (material_id,),
        ).fetchone()
        return float(row["q"]) if row else 0.0

    @_locked
    def find_material(self, name, spec=""):
        return self.conn.execute(
            "SELECT * FROM materials WHERE name=? AND spec=?", (name, spec)
        ).fetchone()

    @_locked
    def add_material(self, name, spec="", unit="", category="", min_qty=0, note="", quality_split=0):
        name = (name or "").strip()
        spec = (spec or "").strip()
        if not name:
            raise ValueError("材料名称不能为空")
        exist = self.find_material(name, spec)
        if exist:
            if exist["active"]:
                raise ValueError("材料「%s」已存在" % material_display(dict(exist)))
            self.conn.execute("UPDATE materials SET active=1, updated_at=? WHERE id=?",
                              (now_full(), exist["id"]))
            self.conn.commit()
            self.mark_dirty("material_restore", exist["id"])
            return exist["id"]
        cur = self.conn.execute(
            "INSERT INTO materials(name, spec, unit, category, min_qty, note, active,"
            " quality_split, uid, updated_at, created_at) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
            (name, spec, unit or "", category or "", float(min_qty or 0), note or "",
             1 if quality_split else 0, material_uid(name, spec), now_full(), now_str()),
        )
        self.conn.commit()
        self.mark_dirty("material_add", name)
        return cur.lastrowid

    @_locked
    def update_material(self, mid, **kw):
        fields = ["name", "spec", "unit", "category", "min_qty", "note", "quality_split"]
        sets, args = [], []
        for f in fields:
            if f in kw:
                sets.append("%s=?" % f)
                args.append(kw[f])
        if not sets:
            return
        sets.append("updated_at=?")
        args.append(now_full())
        args.append(mid)
        try:
            self.conn.execute("UPDATE materials SET %s WHERE id=?" % ", ".join(sets), args)
            self.conn.commit()
            self.mark_dirty("material_update", mid)
        except sqlite3.IntegrityError:
            raise ValueError("已存在同名称同规格的材料")

    @_locked
    def set_material_active(self, mid, active):
        """停用/启用。停用后不出现在下拉框里，但历史流水和结存照样保留。"""
        self.conn.execute("UPDATE materials SET active=?, updated_at=? WHERE id=?",
                          (1 if active else 0, now_full(), mid))
        self.conn.commit()
        self.mark_dirty("material_active", "%s -> %s" % (mid, active))

    @_locked
    def material_record_count(self, mid):
        return self.conn.execute(
            "SELECT COUNT(*) n FROM records WHERE material_id=?", (mid,)
        ).fetchone()["n"]

    # ---------------- 代工厂

    @_locked
    def list_factories(self, active_only=True):
        sql = "SELECT * FROM factories WHERE 1=1"
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY name COLLATE NOCASE"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    @_locked
    def factory_options(self):
        return [(r["id"], r["name"]) for r in self.list_factories(active_only=False)]

    @_locked
    def factory_by_id(self, fid):
        row = self.conn.execute("SELECT * FROM factories WHERE id=?", (fid,)).fetchone()
        return dict(row) if row else None

    @_locked
    def factory_total(self, fid):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(delta),0) q FROM records WHERE factory_id=? AND voided=0", (fid,)
        ).fetchone()
        return float(row["q"]) if row else 0.0

    @_locked
    def add_factory(self, name, contact="", note=""):
        name = (name or "").strip()
        if not name:
            raise ValueError("代工厂名称不能为空")
        row = self.conn.execute("SELECT * FROM factories WHERE name=?", (name,)).fetchone()
        if row:
            if row["active"]:
                raise ValueError("代工厂「%s」已存在" % name)
            self.conn.execute("UPDATE factories SET active=1, updated_at=? WHERE id=?",
                              (now_full(), row["id"]))
            self.conn.commit()
            self.mark_dirty("factory_restore", row["id"])
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO factories(name, contact, note, active, uid, updated_at, created_at)"
            " VALUES(?,?,?,1,?,?,?)",
            (name, contact or "", note or "", factory_uid(name), now_full(), now_str()),
        )
        self.conn.commit()
        self.mark_dirty("factory_add", name)
        return cur.lastrowid

    @_locked
    def update_factory(self, fid, **kw):
        fields = ["name", "contact", "note"]
        sets, args = [], []
        for f in fields:
            if f in kw:
                sets.append("%s=?" % f)
                args.append(kw[f])
        if not sets:
            return
        sets.append("updated_at=?")
        args.append(now_full())
        args.append(fid)
        try:
            self.conn.execute("UPDATE factories SET %s WHERE id=?" % ", ".join(sets), args)
            self.conn.commit()
            self.mark_dirty("factory_update", fid)
        except sqlite3.IntegrityError:
            raise ValueError("已有同名代工厂")

    @_locked
    def set_factory_active(self, fid, active):
        self.conn.execute("UPDATE factories SET active=?, updated_at=? WHERE id=?",
                          (1 if active else 0, now_full(), fid))
        self.conn.commit()
        self.mark_dirty("factory_active", "%s -> %s" % (fid, active))

    @_locked
    def factory_record_count(self, fid):
        return self.conn.execute(
            "SELECT COUNT(*) n FROM records WHERE factory_id=?", (fid,)
        ).fetchone()["n"]

    # ---------------- 流水

    @_locked
    def balance(self, material_id, factory_id, quality=None):
        """
        结存。quality=None 表示不分品质、返回总数（= 良品 + 不良品）；
        传 "good" / "defect" 只算该品质的结存。
        """
        sql = ("SELECT COALESCE(SUM(delta),0) q FROM records"
               " WHERE material_id=? AND factory_id=? AND voided=0")
        args = [material_id, factory_id]
        if quality is not None:
            sql += " AND quality=?"
            args.append(quality)
        row = self.conn.execute(sql, args).fetchone()
        return float(row["q"]) if row else 0.0

    @_locked
    def balance_split(self, material_id, factory_id):
        """返回 (良品, 不良品, 总数)。没开品质选项的材料，全部算良品口径之外的总数。"""
        rows = self.conn.execute(
            "SELECT quality, COALESCE(SUM(delta),0) q FROM records"
            " WHERE material_id=? AND factory_id=? AND voided=0 GROUP BY quality",
            (material_id, factory_id),
        ).fetchall()
        good = defect = other = 0.0
        for r in rows:
            if r["quality"] == QUALITY_GOOD:
                good += float(r["q"] or 0)
            elif r["quality"] == QUALITY_DEFECT:
                defect += float(r["q"] or 0)
            else:
                other += float(r["q"] or 0)
        return good, defect, good + defect + other

    @_locked
    def add_record(self, rtype, material_id, factory_id, qty, ts=None, batch="", operator="", note="",
                   target_qty=None, quality=""):
        """
        rtype: IN 入库 / OUT 消耗 / RETURN 退回 / ADJUST 盘点
        入库数量一律传正数；盘点传 target_qty（实盘数量），内部换算成差额。
        quality: "good" 良品 / "defect" 不良品 / "" 不区分（该材料没开品质选项时）
        """
        rtype = rtype.upper()
        if rtype not in TYPE_LABELS:
            raise ValueError("未知的流水类型：%s" % rtype)
        if not material_id:
            raise ValueError("请选择材料")
        if not factory_id:
            raise ValueError("请选择代工厂")

        quality = (quality or "").strip()
        if quality not in QUALITY_LABELS:
            raise ValueError("未知的品质标记：%s" % quality)

        # 材料没开「良品/不良品」时，一律记成不区分，避免界面传了值污染数据
        mat = self.material_by_id(material_id)
        if mat is not None and not int(mat.get("quality_split") or 0):
            quality = ""

        qty = round(float(qty or 0), 4)

        if rtype == "ADJUST":
            if target_qty is None:
                raise ValueError("盘点需要填写实盘数量")
            # 盘点按「同品质」的结存算差额，否则良品盘成不良品会被算成大额差异
            cur = self.balance(material_id, factory_id, quality)
            delta = round(float(target_qty) - cur, 4)
        else:
            if qty <= 0:
                raise ValueError("数量必须大于 0")
            delta = qty if rtype == "IN" else -qty

        if abs(delta) < EPS:
            raise ValueError("数量没有变化（差额为 0），无需记录")

        ts = ts or now_str()
        self.conn.execute(
            "INSERT INTO records(ts, type, material_id, factory_id, delta, quality,"
            " batch, operator, note, voided, uid, updated_at, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (ts, rtype, material_id, factory_id, delta, quality,
             batch or "", operator or "", note or "",
             new_record_uid(), now_full(), now_str()),
        )
        self.conn.commit()
        rid = self.conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        self.mark_dirty("add_record", "记录 #%d" % rid)
        return rid

    @_locked
    def void_record(self, rid, voided=True):
        self.conn.execute("UPDATE records SET voided=?, updated_at=? WHERE id=?",
                          (1 if voided else 0, now_full(), rid))
        self.conn.commit()
        self.mark_dirty("void_record", "记录 #%d %s" % (rid, "作废" if voided else "恢复"))

    @_locked
    def list_records(self, date_from="", date_to="", rtype="", material_id=None,
                     factory_id=None, keyword="", include_voided=True, limit=5000):
        sql = """
            SELECT r.*, m.name AS material_name, m.spec AS material_spec, m.unit AS unit,
                   f.name AS factory_name
            FROM records r
            JOIN materials m ON m.id = r.material_id
            JOIN factories f ON f.id = r.factory_id
            WHERE 1=1
        """
        args = []
        if date_from:
            sql += " AND r.ts >= ?"
            args.append(date_from + " 00:00")
        if date_to:
            sql += " AND r.ts <= ?"
            args.append(date_to + " 23:59")
        if rtype:
            sql += " AND r.type = ?"
            args.append(rtype)
        if material_id:
            sql += " AND r.material_id = ?"
            args.append(material_id)
        if factory_id:
            sql += " AND r.factory_id = ?"
            args.append(factory_id)
        if not include_voided:
            sql += " AND r.voided = 0"
        if keyword:
            k = "%%%s%%" % keyword
            sql += " AND (m.name LIKE ? OR f.name LIKE ? OR r.batch LIKE ? OR r.note LIKE ? OR r.operator LIKE ?)"
            args += [k, k, k, k, k]
        sql += " ORDER BY r.ts DESC, r.id DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    @_locked
    def record_by_id(self, rid):
        row = self.conn.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

    # ---------------- 库存总览（按材料分组，下面挂各厂结存）

    @_locked
    def stock_overview(self, keyword=""):
        result = []
        for m in self.list_materials(keyword=keyword, active_only=True):
            rows = self.conn.execute(
                "SELECT factory_id, quality, SUM(delta) qty, MAX(ts) last_ts"
                " FROM records WHERE material_id=? AND voided=0"
                " GROUP BY factory_id, quality",
                (m["id"],),
            ).fetchall()
            facs = {f["id"]: f["name"] for f in self.list_factories(active_only=False)}

            # 按厂聚合：每个厂名下再分良品 / 不良品
            per_fac = {}
            for r in rows:
                fid = r["factory_id"]
                d = per_fac.setdefault(fid, {
                    "factory_id": fid,
                    "factory_name": facs.get(fid, "(已删除)"),
                    "good": 0.0, "defect": 0.0, "other": 0.0,
                    "last_ts": "",
                })
                q = float(r["qty"] or 0)
                if r["quality"] == QUALITY_GOOD:
                    d["good"] += q
                elif r["quality"] == QUALITY_DEFECT:
                    d["defect"] += q
                else:
                    d["other"] += q
                if (r["last_ts"] or "") > d["last_ts"]:
                    d["last_ts"] = r["last_ts"] or ""

            children = []
            for d in per_fac.values():
                d["qty"] = d["good"] + d["defect"] + d["other"]
                children.append(d)
            children.sort(key=lambda x: (-abs(x["qty"]), x["factory_name"]))

            total = sum(c["qty"] for c in children)
            total_good = sum(c["good"] for c in children)
            total_defect = sum(c["defect"] for c in children)
            last = max([c["last_ts"] for c in children] or [""])
            result.append({
                "material": m, "total": total, "children": children, "last_ts": last,
                "good": total_good, "defect": total_defect,
            })
        return result

    @_locked
    def stats(self):
        mats = self.conn.execute("SELECT COUNT(*) n FROM materials WHERE active=1").fetchone()["n"]
        facs = self.conn.execute("SELECT COUNT(*) n FROM factories WHERE active=1").fetchone()["n"]
        recs = self.conn.execute("SELECT COUNT(*) n FROM records WHERE voided=0").fetchone()["n"]
        kinds = self.conn.execute(
            "SELECT COUNT(*) n FROM (SELECT material_id FROM records WHERE voided=0"
            " AND delta>0 GROUP BY material_id)"
        ).fetchone()["n"]
        return {"materials": mats, "factories": facs, "records": recs, "with_stock": kinds}

    @_locked
    def low_stock_alerts(self):
        """低于安全库存（min_qty）的材料。"""
        out = []
        for m in self.list_materials(active_only=True):
            if float(m["min_qty"] or 0) <= 0:
                continue
            total = self.material_total(m["id"])
            if total < float(m["min_qty"]) - EPS:
                out.append({"material": m, "qty": total, "min_qty": float(m["min_qty"])})
        return out

    # ---------------- 导出 / 备份

    @_locked
    def export_records_csv(self, path, **filters):
        rows = self.list_records(**filters)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["时间", "类型", "材料", "规格", "品质", "单位", "代工厂", "数量",
                        "批次/单号", "经办人", "备注", "状态"])
            for r in rows:
                w.writerow([
                    r["ts"], TYPE_LABELS.get(r["type"], r["type"]),
                    r["material_name"], r["material_spec"],
                    QUALITY_LABELS.get(r.get("quality") or "", ""),
                    r["unit"], r["factory_name"],
                    fmt_qty(r["delta"]), r["batch"], r["operator"], r["note"],
                    "已作废" if r["voided"] else "有效",
                ])
        return len(rows)

    @_locked
    def export_stock_csv(self, path):
        data = self.stock_overview()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["材料", "规格", "单位", "代工厂", "结存数量",
                        "良品", "不良品", "未分品质", "最近变动"])
            for item in data:
                m = item["material"]
                split = bool(int(m.get("quality_split") or 0))
                if not item["children"]:
                    w.writerow([m["name"], m["spec"], m["unit"], "（无记录）", "0",
                                "0", "0", "0", ""])
                    continue
                for c in item["children"]:
                    w.writerow([m["name"], m["spec"], m["unit"], c["factory_name"],
                                fmt_qty(c["qty"]),
                                fmt_qty(c["good"]) if split else "",
                                fmt_qty(c["defect"]) if split else "",
                                fmt_qty(c["other"]) if split else "",
                                c["last_ts"]])
        return len(data)

    # ---------------- 待备份改动队列（供云端备份使用）

    @_locked
    def mark_dirty(self, action, detail=""):
        """记一笔改动。调用方是 add_record / void_record 等写操作。"""
        try:
            self.conn.execute(
                "INSERT INTO cloud_dirty(ts, action, detail) VALUES(?,?,?)",
                (now_str(), action, detail or ""),
            )
            self.conn.commit()
        except Exception:
            pass   # 备份是可选功能，队列写失败绝不能影响记账本身

    @_locked
    def dirty_count(self):
        try:
            row = self.conn.execute("SELECT COUNT(*) n FROM cloud_dirty").fetchone()
            return row["n"] if row else 0
        except Exception:
            return 0

    @_locked
    def dirty_summary(self, limit=20):
        try:
            rows = self.conn.execute(
                "SELECT * FROM cloud_dirty ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    @_locked
    def clear_dirty(self):
        try:
            self.conn.execute("DELETE FROM cloud_dirty")
            self.conn.commit()
        except Exception:
            pass

    @_locked
    def backup(self, keep=30):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(BACKUP_DIR, "inventory_%s.db" % stamp)
        self.conn.commit()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(self.db_path, dst)
        # 只保留最近 N 份
        files = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.startswith("inventory_") and f.endswith(".db")]
        )
        for old in files[:-keep] if keep > 0 else []:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except OSError:
                pass
        return dst

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass


def material_display(m):
    """下拉框里显示的文字：名称 + 规格（+ 单位）。"""
    s = m["name"]
    if m.get("spec"):
        s += " / " + m["spec"]
    if m.get("unit"):
        s += "（%s）" % m["unit"]
    return s
