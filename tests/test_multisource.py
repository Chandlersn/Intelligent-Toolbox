"""多源收藏测试：生产端（GitHub/GitLab）与认知端（抖音）的来源判别、元数据解析、
逐字稿转写、卡片生成、worker 落库与图谱透出。

全部离线、确定、快：抖音联网部分用 monkeypatch 替换，转写用假引擎。
"""

import json
import os
import shutil
import tempfile
import unittest

from common import Base  # noqa: E402  — 先把 src/ 加进 sys.path，下面才能 import 业务模块

import analyze  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import douyin_source  # noqa: E402
import server  # noqa: E402


# ═══════════════════════════════════════════════════════════════════
#  来源判别
# ═══════════════════════════════════════════════════════════════════
class TestClassifySource(unittest.TestCase):
    def test_github(self):
        self.assertEqual(server.classify_source("https://github.com/pallets/flask"),
                         ("github", "/pallets/flask"))

    def test_gitlab(self):
        self.assertEqual(server.classify_source("https://gitlab.com/a/b"),
                         ("gitlab", "/a/b"))

    def test_douyin_short(self):
        self.assertEqual(server.classify_source("https://v.douyin.com/abc/"),
                         ("douyin", None))

    def test_douyin_full(self):
        self.assertEqual(server.classify_source("https://www.douyin.com/video/7300000000"),
                         ("douyin", None))

    def test_unknown(self):
        self.assertIsNone(server.classify_source("https://example.com/a/b"))
        self.assertIsNone(server.classify_source("ftp://github.com/a/b"))

    def test_extract_douyin_from_text(self):
        # 从分享文字里抽抖音链接（与 GitHub 同一套抽取逻辑）
        self.assertEqual(
            server.extract_repo_url("推荐 https://v.douyin.com/abcd/ 很好用"),
            "https://v.douyin.com/abcd/")
        self.assertEqual(
            server.extract_repo_url("看看 https://github.com/pallets/flask 这个"),
            "https://github.com/pallets/flask")


# ═══════════════════════════════════════════════════════════════════
#  抖音元数据解析 / 转写
# ═══════════════════════════════════════════════════════════════════
def _router_html():
    """用 json.dumps 拼出合法内嵌 JSON，避免手写括号错位。"""
    data = {
        "loaderData": {
            "video_(id)/page": {
                "videoInfoRes": {
                    "aweme_id": "730000000000000",
                    "item_list": [{
                        "author": {"nickname": "测试作者"},
                        "desc": "这是一条测试视频简介",
                        "video": {
                            "vid": "vid123",
                            "play_addr": {
                                "url_list": ["https://aweme.snssdk.com/playwm/x.mp4"]},
                            "cover": {
                                "url_list": ["https://cover.example.com/c.jpg"]},
                        },
                    }],
                }
            }
        }
    }
    return ('<html><head></head><body><script>window._ROUTER_DATA = '
            + json.dumps(data) + '</script></body></html>')


_ROUTER_HTML = _router_html()


class TestDouyinSource(unittest.TestCase):
    def test_parse_router_data(self):
        info = douyin_source._parse_router_data(_ROUTER_HTML)
        self.assertEqual(info["video_id"], "730000000000000")
        self.assertEqual(info["author"], "测试作者")
        self.assertEqual(info["desc"], "这是一条测试视频简介")
        # playwm → play 去水印替换
        self.assertEqual(info["play_url"], "https://aweme.snssdk.com/play/x.mp4")
        self.assertEqual(info["cover"], "https://cover.example.com/c.jpg")

    def test_parse_router_data_failure(self):
        with self.assertRaises(ValueError):
            douyin_source._parse_router_data("<html>no router data here</html>")

    def test_is_douyin(self):
        self.assertTrue(douyin_source.is_douyin("https://v.douyin.com/abc/"))
        self.assertTrue(douyin_source.is_douyin("https://www.iesdouyin.com/share/video/1"))
        self.assertFalse(douyin_source.is_douyin("https://github.com/a/b"))

    def _fake_resolve(self, url):
        return {"video_id": "vid999", "title": "测试标题", "author": "作者X",
                "desc": "简介内容", "play_url": "https://x.com/playwm/a.mp4",
                "cover": "https://x.com/c.jpg", "url": url}

    def test_collect_douyin_meta_only(self):
        orig = douyin_source.resolve_douyin
        douyin_source.resolve_douyin = self._fake_resolve
        try:
            meta, raw = douyin_source.collect_douyin(
                "https://v.douyin.com/abc/", transcribe_fn=None)
        finally:
            douyin_source.resolve_douyin = orig
        self.assertEqual(meta["platform"], "douyin")
        self.assertEqual(meta["author"], "作者X")
        self.assertIsNone(raw)  # 没要求转写 → 不下载、不联网

    def test_collect_douyin_transcribe(self):
        orig_resolve = douyin_source.resolve_douyin
        orig_dl = douyin_source.download_video
        orig_ax = douyin_source.extract_audio

        def fake_dl(play_url, out_path, timeout=30):
            open(out_path, "wb").close()
            return out_path

        def fake_ax(video_path, audio_path, ffmpeg="ffmpeg"):
            open(audio_path, "wb").close()
            return audio_path

        douyin_source.resolve_douyin = self._fake_resolve
        douyin_source.download_video = fake_dl
        douyin_source.extract_audio = fake_ax
        try:
            meta, raw = douyin_source.collect_douyin(
                "https://v.douyin.com/abc/",
                transcribe_fn=lambda p: "逐字稿内容", media_dir=None)
        finally:
            douyin_source.resolve_douyin = orig_resolve
            douyin_source.download_video = orig_dl
            douyin_source.extract_audio = orig_ax
        self.assertEqual(raw, "逐字稿内容")
        self.assertEqual(meta["platform"], "douyin")


# ═══════════════════════════════════════════════════════════════════
#  认知端卡片生成（启发式回退）
# ═══════════════════════════════════════════════════════════════════
class TestCognitionCard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        config.DB = os.path.join(self.tmp, "t.db")
        server.init_db()
        self._llm = dict(analyze.LLM_MODE)
        analyze.LLM_MODE = {"module": None, "tried": True}

    def tearDown(self):
        analyze.LLM_MODE = self._llm
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_cognition_heuristic(self):
        meta = {"platform": "douyin", "title": "用 FastAPI 搭后端",
                "description": "FastAPI 做后端接口",
                "transcript": "今天讲 FastAPI 怎么搭后端接口做本地知识库"}
        card = analyze.build_card(meta, "", 1, None, kind="cognition")
        self.assertEqual(card["source"], "heuristic")
        # 中文逐字稿应落到 Web 框架（后端/接口/知识库）
        self.assertEqual(card["domain"], "Web 框架")
        self.assertEqual(card["tech_stack"], [])  # 认知端无 tech_stack

    def test_kind_inferred_from_meta(self):
        meta = {"platform": "douyin", "title": "t", "description": "d",
                "transcript": "讲前端组件设计"}
        self.assertEqual(analyze._kind_of_meta(meta), "cognition")
        prod = {"name": "a/b", "description": "x"}
        self.assertEqual(analyze._kind_of_meta(prod), "production")


# ═══════════════════════════════════════════════════════════════════
#  worker：抖音分支落库（kind / raw / card / 轴表）
# ═══════════════════════════════════════════════════════════════════
class TestWorkerDouyin(Base):
    def setUp(self):
        super().setUp()
        server.worker = self._worker_backup  # 恢复真实 worker（本测试要真的落库）

    def _fake_collect(self, url, transcribe_fn=None, media_dir=None):
        meta = {"platform": "douyin", "name": "抖音 · 张三", "title": "用 FastAPI 搭后端",
                "description": "FastAPI 做后端接口", "author": "张三", "video_id": "v1"}
        raw = "今天讲 FastAPI 怎么搭后端接口，适合做本地知识库。"
        return meta, raw

    def test_worker_douyin_creates_cognition_card(self):
        orig = server.douyin_source.collect_douyin
        server.douyin_source.collect_douyin = self._fake_collect
        try:
            conn = db.connect()
            c = conn.cursor()
            c.execute(
                "INSERT INTO repos (url,note,status,created_at,source,kind) "
                "VALUES (?,?,?,?,?,?)",
                ("https://v.douyin.com/abc/", "", "queued", server.now(), "manual", "cognition"))
            rid = c.lastrowid
            conn.commit()
            conn.close()
            server.worker(rid, "https://v.douyin.com/abc/", "", "douyin", None, "manual")
        finally:
            server.douyin_source.collect_douyin = orig

        conn = db.connect()
        row = conn.execute(
            "SELECT kind,raw,meta,card,status FROM repos WHERE id=?", (rid,)).fetchone()
        ax = conn.execute(
            "SELECT value FROM repo_axis WHERE repo_id=? AND axis_key='domain'",
            (rid,)).fetchone()
        conn.close()

        self.assertEqual(row[0], "cognition")
        self.assertIn("FastAPI", row[1])          # 逐字稿双保留
        meta = json.loads(row[2])
        self.assertEqual(meta["platform"], "douyin")
        card = json.loads(row[3])
        self.assertEqual(card["domain"], "Web 框架")
        self.assertEqual(row[4], "carded")
        self.assertIsNotNone(ax)                   # 轴表已同步

    def test_worker_douyin_exposed_in_cards_and_graph(self):
        orig = server.douyin_source.collect_douyin
        server.douyin_source.collect_douyin = self._fake_collect
        try:
            conn = db.connect()
            c = conn.cursor()
            c.execute(
                "INSERT INTO repos (url,note,status,created_at,source,kind) "
                "VALUES (?,?,?,?,?,?)",
                ("https://v.douyin.com/abc/", "", "queued", server.now(), "manual", "cognition"))
            rid = c.lastrowid
            conn.commit()
            conn.close()
            server.worker(rid, "https://v.douyin.com/abc/", "", "douyin", None, "manual")
        finally:
            server.douyin_source.collect_douyin = orig

        cards = server.get_cards({})
        hit = next(it for it in cards if it["id"] == rid)
        self.assertEqual(hit["kind"], "cognition")
        self.assertIn("FastAPI", hit["raw"])

        g = server.get_graph()
        node = next(n for n in g["nodes"] if n["id"] == rid)
        self.assertEqual(node["kind"], "cognition")


# ═══════════════════════════════════════════════════════════════════
#  后端 kind 筛选
# ═════════════════════════════════════════════════════════════════
class TestCardsKindFilter(Base):
    def test_kind_filter(self):
        conn = db.connect()
        c = conn.cursor()
        c.execute(
            "INSERT INTO repos (url,note,status,meta,card,created_at,source,kind) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("https://github.com/a/b", "", "carded", "{}", "{}",
             server.now(), "manual", "production"))
        c.execute(
            "INSERT INTO repos (url,note,status,meta,card,created_at,source,kind) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("https://v.douyin.com/x/", "", "carded", '{"platform":"douyin"}', "{}",
             server.now(), "manual", "cognition"))
        conn.commit()
        conn.close()

        self.assertEqual(len(server.get_cards({})), 2)
        cog = server.get_cards({"kind": ["cognition"]})
        self.assertEqual(len(cog), 1)
        self.assertEqual(cog[0]["kind"], "cognition")
        prod = server.get_cards({"kind": ["production"]})
        self.assertEqual(len(prod), 1)
        self.assertEqual(prod[0]["kind"], "production")


if __name__ == "__main__":
    unittest.main()
