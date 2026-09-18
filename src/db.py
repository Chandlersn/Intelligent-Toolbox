"""统一 SQLite 连接与 schema 迁移。

两件事：

1. 连接参数集中。改造前每个函数各自 `sqlite3.connect(DB)`，都是默认
   journal_mode=delete、无 busy_timeout。而本项目是「多线程 HTTP 请求读 +
   后台 worker 线程写」的结构，默认配置下偶发 `database is locked`。
   现在统一开 WAL（读写不互斥）+ busy_timeout（并发写短暂等待而非报错）。

2. schema 版本化。改造前用 `ALTER TABLE ... except OperationalError: pass`
   猜列是否存在，没有版本号、无法知道库处在哪一版。现在用 PRAGMA user_version
   记录，新增列走 ensure_column（读 table_info 判断，不靠异常兜）。
"""

import sqlite3

import config

# 每次新增列/表都要 +1，并在 init_db() 里写一步幂等迁移。
# v1 初始 · v2 加 repos.source + 索引 · v3 加 repos.feedback 与 use 轴
# v4 加 repo_events 表 + repos.last_checked_at（仓库更新检测）
# v5 加 repos.kind（production/cognition 多源分类）+ repos.raw（原文/逐字稿）
# v6 加 app_settings 表（应用内可配置项：内置远程模型）
SCHEMA_VERSION = 6


def connect(path=None):
    """统一连接工厂。所有模块共用，别绕过它直接 sqlite3.connect。"""
    conn = sqlite3.connect(path or config.DB, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def table_columns(conn, table):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def ensure_column(conn, table, column, ddl_type):
    """列不存在才 ALTER。返回 True 表示本次真的加了列。"""
    if column in table_columns(conn, table):
        return False
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl_type))
    return True


def schema_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def set_schema_version(conn, v):
    conn.execute("PRAGMA user_version=%d" % int(v))


def get_settings(conn, key, default=None):
    """读 app_settings 单键值。表由 init_db() 幂等创建。"""
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_settings(conn, key, value):
    """写 app_settings 单键值（upsert）。不 commit，由调用方统一提交。"""
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))
