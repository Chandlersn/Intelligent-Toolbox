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

# 每次新增列/表都要 +1，并在 migrate() 里写一步幂等迁移。
SCHEMA_VERSION = 2


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
