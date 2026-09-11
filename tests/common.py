"""测试公共设施：路径引导、假 LLM、隔离用的 Base 用例基类。

单独成文件的理由：单元测试与 HTTP 集成测试都要用同一套「临时库 + 断网 + 不真跑 worker」
的隔离手段，复制两份必然会漂移（一边改了隔离方式另一边不知道）。
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
TESTS = os.path.dirname(os.path.abspath(__file__))
for p in (SRC, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import analyze  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


class FakeLLM:
    """按预设回应冒充灯笼 llm 模块。reply 可以是字符串，也可以是 Exception。

    reply 为 Exception 时 chat() 抛出它 —— 用来验证「上游挂了必须静默降级」。
    """

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat(self, system, user, **kw):
        self.calls.append((system, user, kw))
        if isinstance(self.reply, Exception):
            raise self.reply
        return (self.reply, "fake-model")


class Base(unittest.TestCase):
    """隔离基类：每个用例一个临时库；LLM 关闭；后台 worker 空转。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="repo_collector_test_")
        config.DB = os.path.join(self.tmp, "t.db")
        server.init_db()
        # 默认关掉 LLM：测试必须离线、确定、快
        self._llm_backup = dict(analyze.LLM_MODE)
        analyze.LLM_MODE = {"module": None, "tried": True}
        # 把后台 worker 换成空函数：collect 仍会起线程，但线程里不做任何事 ——
        # 否则测试会去拉 GitHub 元数据（联网、慢），还会在 tearDown 之后写库。
        self._worker_backup = server.worker
        server.worker = lambda *a, **kw: None

    def tearDown(self):
        server.worker = self._worker_backup
        analyze.LLM_MODE = self._llm_backup
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    # ---- 造数据的小工具 ----
    def add_repo(self, url, note=None, meta=None, card=None, source="manual"):
        conn = db.connect()
        c = conn.cursor()
        c.execute(
            "INSERT INTO repos (url,note,status,meta,card,created_at,source) VALUES (?,?,?,?,?,?,?)",
            (url, note, "carded" if card else "queued",
             json.dumps(meta or {}, ensure_ascii=False),
             json.dumps(card, ensure_ascii=False) if card else None,
             server.now(), source))
        rid = c.lastrowid
        conn.commit()
        conn.close()
        return rid

    def axis_rows(self, rid):
        conn = db.connect()
        rows = conn.execute(
            "SELECT axis_key, value, source, confidence FROM repo_axis WHERE repo_id=?",
            (rid,)).fetchall()
        conn.close()
        return rows

    def card_of(self, rid):
        conn = db.connect()
        row = conn.execute("SELECT card FROM repos WHERE id=?", (rid,)).fetchone()
        conn.close()
        return json.loads(row[0]) if row and row[0] else None

    def axis_value(self, rid, axis_key):
        vals = [r[1] for r in self.axis_rows(rid) if r[0] == axis_key]
        return vals[0] if vals else None
