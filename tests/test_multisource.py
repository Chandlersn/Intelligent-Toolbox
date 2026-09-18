"""多源收藏测试：生产端（GitHub/GitLab）与认知端（抖音）的来源判别、元数据解析、
逐字稿转写、卡片生成、worker 落库与图谱透出。

全部离线、确定、快：抖音联网部分用 monkeypatch 替换，转写用假引擎。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from common import Base  # noqa: E402  — 先把 src/ 加进 sys.path，下面才能 import 业务模块

import analyze  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import douyin_source  # noqa: E402
import web_source  # noqa: E402
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

    def test_web(self):
        # 任意 http(s) 链接（非 github/gitlab/douyin）按网页 / 文章（认知端）收录
        self.assertEqual(server.classify_source("https://example.com/a/b"),
                         ("web", "https://example.com/a/b"))
        self.assertEqual(server.classify_source("https://blog.x.com/p/123"),
                         ("web", "https://blog.x.com/p/123"))

    def test_unknown(self):
        # 非 http(s) 或无法解析的协议仍拒绝
        self.assertIsNone(server.classify_source("ftp://github.com/a/b"))
        self.assertIsNone(server.classify_source("not a url"))

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

    def test_parse_router_data_empty_raises(self):
        # 抖音对无效/已删视频仍返回空壳 videoInfoRes（_ROUTER_DATA 在，但内容全空）。
        # 不能生成占位标题的垃圾卡，必须如实报错。
        data = {"loaderData": {"video_(id)/page": {"videoInfoRes": {
            "aweme_id": "", "item_list": [{}]}}}}
        html = ('<html><body><script>window._ROUTER_DATA = '
                + json.dumps(data) + '</script></body></html>')
        with self.assertRaises(ValueError) as ctx:
            douyin_source._parse_router_data(html)
        self.assertIn("信息为空", str(ctx.exception))

    def test_parse_router_data_placeholder_shell_honest(self):
        # 真实抖音链接经浏览器渲染后：_ROUTER_DATA 在，但 videoInfoRes 为空、页面标题是占位符
        # 『在抖音记录美好生活』——这是视频详情接口（iteminfo）被反爬签名拦截，纯抓取拿不到数据。
        # 必须如实报『反爬签名拦截』，绝不能误诊成『已删除/私密』。
        data = {"loaderData": {"video_(id)/page": {"ua": "x", "query": {}}}}
        html = ('<html><head><title>在抖音记录美好生活20260916 - 抖音</title></head>'
                '<body><script>window._ROUTER_DATA = '
                + json.dumps(data) + '</script></body></html>')
        with self.assertRaises(ValueError) as ctx:
            douyin_source._parse_router_data(html)
        msg = str(ctx.exception)
        self.assertIn("反爬签名拦截", msg)
        self.assertNotIn("已删除", msg)
        self.assertNotIn("设为私密", msg)

    # ---- 反爬挑战页：必须说真话，不能误诊为『删除/私密』 ----
    def test_challenge_page_detection(self):
        challenge = ('<html><head><title>在抖音记录美好生活</title></head>'
                     '<body>__ac_signature</body></html>')
        self.assertTrue(douyin_source._looks_like_challenge(challenge))
        # 真实内容页不应被误判为挑战页
        self.assertFalse(douyin_source._looks_like_challenge(_ROUTER_HTML))

    def test_challenge_page_honest_error(self):
        challenge = ('<html><head><title>在抖音记录美好生活</title></head>'
                     '<body>__ac_signature placeholder</body></html>')
        with self.assertRaises(ValueError) as ctx:
            douyin_source._parse_router_data(challenge)
        msg = str(ctx.exception)
        self.assertIn("反爬挑战", msg)
        # 关键：挑战页绝不能误诊成『删除/私密』
        self.assertNotIn("已删除", msg)
        self.assertNotIn("设为私密", msg)

    def test_rendered_page_with_ac_signature_not_misjudged(self):
        # 浏览器渲染后的真实页会残留 __ac_signature 字符串，但 _ROUTER_DATA 已注入。
        # 绝不能把这种页误判为挑战页 —— 必须先认 _ROUTER_DATA。
        html = _ROUTER_HTML.replace(
            "<html>", '<html><script>var __ac_signature="abc";</script>')
        info = douyin_source._parse_router_data(html)
        self.assertEqual(info["video_id"], "730000000000000")
        self.assertEqual(info["author"], "测试作者")

    def test_meta_fallback_only_on_real_page(self):
        # 挑战页 → 退化兜底返回 None（仍是拦截壳子，无可用信息）
        challenge = ('<html><head><title>在抖音记录美好生活</title></head>'
                     '<body>__ac_signature</body></html>')
        self.assertIsNone(douyin_source._parse_meta_fallback(challenge))
        # 有 og 标签的真实页（无 videoInfoRes）→ 返回『部分 meta』
        real = ('<html><head><title>正常标题</title>'
                '<meta property="og:title" content="一条真实视频">'
                '<meta property="og:description" content="真实简介">'
                '<meta property="og:image" content="https://img/x.jpg">'
                '</head><body></body></html>')
        fb = douyin_source._parse_meta_fallback(real)
        self.assertIsNotNone(fb)
        self.assertTrue(fb["_partial"])
        self.assertEqual(fb["title"], "一条真实视频")
        self.assertEqual(fb["desc"], "真实简介")
        self.assertEqual(fb["cover"], "https://img/x.jpg")

    def test_resolve_douyin_challenge_raises_honest(self):
        # monkeypatch 网络层：短链 302 出带 id 的真实页、分享页返回挑战壳子，
        # 并把 _fetch_with_browser 置为不可用，验证 resolve_douyin 透传诚实的『反爬挑战』错误而非误诊。
        challenge = ('<html><head><title>在抖音记录美好生活</title></head>'
                     '<body>__ac_signature</body></html>')

        class _Resp:
            def geturl(self):
                return "https://www.douyin.com/video/7301234567890123456"

        with mock.patch.object(
                douyin_source.urllib.request, "urlopen", lambda *a, **k: _Resp()), \
                mock.patch.object(
                douyin_source, "_get", lambda u, timeout=8: challenge), \
                mock.patch.object(
                douyin_source, "_fetch_with_browser", lambda u, timeout=25: None):
            with self.assertRaises(ValueError) as ctx:
                douyin_source.resolve_douyin("https://v.douyin.com/abc/")
            self.assertIn("反爬挑战", str(ctx.exception))

    def test_resolve_douyin_uses_browser_fallback(self):
        # 集成验证：标准库拿到挑战壳子 → _fetch_with_browser 返回带 _ROUTER_DATA 的渲染页 →
        # 解析出完整卡片（证明浏览器兜底链路打通，不再误诊）。
        challenge = ('<html><head><title>在抖音记录美好生活</title></head>'
                     '<body>__ac_signature</body></html>')

        class _Resp:
            def geturl(self):
                return "https://www.douyin.com/video/7301234567890123456"

        with mock.patch.object(
                douyin_source.urllib.request, "urlopen", lambda *a, **k: _Resp()), \
                mock.patch.object(
                douyin_source, "_get", lambda u, timeout=8: challenge), \
                mock.patch.object(
                douyin_source, "_fetch_with_browser", lambda u, timeout=25: _ROUTER_HTML):
            info = douyin_source.resolve_douyin("https://v.douyin.com/abc/")
        self.assertEqual(info["video_id"], "730000000000000")
        self.assertEqual(info["author"], "测试作者")
        self.assertIn("play", info["play_url"])

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

    def test_collect_douyin_transcribe_empty_is_visible(self):
        """转写返回空（纯音乐/无旁白/模型未就绪）绝不能静默成 None —— 必须落到 transcript_error，
        否则用户会以为『这功能没有逐字稿』。这是真实踩过的坑。"""
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
                transcribe_fn=lambda p: "", media_dir=None)
        finally:
            douyin_source.resolve_douyin = orig_resolve
            douyin_source.download_video = orig_dl
            douyin_source.extract_audio = orig_ax
        self.assertIsNone(raw)
        self.assertIn("transcript_error", meta)
        self.assertTrue(meta["transcript_error"])

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

    def test_transcript_md_heuristic_fallback(self):
        """LLM 不可用时，逐字稿仍要有可用的结构化 markdown（按句分段），不能返回 None。"""
        run_on = "第一句话讲投资窗口。" * 4 + "第二句话讲大模型演进。" * 4
        md = analyze.build_transcript_md(run_on, {"title": "AI 投资"})
        self.assertTrue(md)
        self.assertTrue(md.startswith("## "))          # 有 markdown 标题
        self.assertIn("\n\n", md)                      # 已段落化
        self.assertIn("投资窗口", md)                  # 内容未丢

    def test_cognition_card_passes_through_transcript_md(self):
        """结构化笔记由 worker 生成、build_card 只透传 —— 认知端卡片要把它带到 card 上。"""
        note_md = "# AI 投资机会\n\n## 概述\n讲了算力与存储。\n\n## 核心要点\n- 估值可能翻十倍"
        meta = {"platform": "douyin", "title": "AI 浪潮下的投资机会",
                "transcript": "指数从两千六涨到四千。" * 3, "transcript_md": note_md}
        card = analyze.build_card(meta, "", 1, None, kind="cognition")
        self.assertEqual(card["transcript_md"], note_md)

    def test_split_transcript_never_cuts_mid_sentence(self):
        """超长逐字稿分块必须在句末切，且不丢字符（保证篇末内容不丢）。"""
        text = "这是第一句话。这是第二句话！这是第三句话？这是第四句话。" * 40
        chunks = analyze._split_transcript(text, 60)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)                     # 一字不丢
        for c in chunks[:-1]:
            self.assertTrue(c.endswith(("。", "！", "？", "\n")))    # 不在句子中间断

    def test_production_card_has_no_transcript_md(self):
        """生产端（GitHub 仓库）没有逐字稿，不应生成该字段（避免空 markdown 区块）。"""
        meta = {"name": "foo/bar", "description": "a lib"}
        card = analyze.build_card(meta, "", 1, None, kind="production")
        self.assertFalse(card.get("transcript_md"))

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

    def test_semaphore_released_after_worker(self):
        # 后台并发控制：真实 worker 跑完后信号量计数必须复原（防 acquire 泄漏，
        # 否则后续收藏会永久排队）。
        orig = server.douyin_source.collect_douyin
        server.douyin_source.collect_douyin = self._fake_collect
        v0 = server._BOOST_SEM._value
        try:
            conn = db.connect()
            c = conn.cursor()
            c.execute(
                "INSERT INTO repos (url,note,status,created_at,source,kind) "
                "VALUES (?,?,?,?,?,?)",
                ("https://v.douyin.com/sem/", "", "queued", server.now(), "manual", "cognition"))
            rid = c.lastrowid
            conn.commit()
            conn.close()
            server.worker(rid, "https://v.douyin.com/sem/", "", "douyin", None, "manual")
            self.assertEqual(server._BOOST_SEM._value, v0)
        finally:
            server.douyin_source.collect_douyin = orig


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


# ═════════════════════════════════════════════════════════════════
#  网页 / 文章来源：抓取与解析（零依赖，联网用 fetch_fn 注入）
# ═════════════════════════════════════════════════════════════════
class TestWebSource(unittest.TestCase):
    def _html(self):
        return (
            '<html><head>'
            '<meta property="og:title" content="我的文章标题">'
            '<meta name="description" content="这是简介">'
            '<title>兜底标题</title></head><body>'
            '<script>var x=1;</script>'
            '<p>今天讲 FastAPI 怎么搭后端接口，适合做本地知识库。</p>'
            '</body></html>')

    def test_parse_meta(self):
        meta, raw = web_source.collect_web(
            "https://example.com/a", fetch_fn=lambda u: self._html())
        self.assertEqual(meta["platform"], "web")
        self.assertEqual(meta["title"], "我的文章标题")   # og:title 优先于 <title>
        self.assertEqual(meta["description"], "这是简介")
        self.assertIn("FastAPI", raw)                     # 正文已抽取
        self.assertNotIn("var x=1", raw)                  # 脚本被剥离
        self.assertIn("FastAPI", meta["transcript"])

    def test_fetch_failure_degrades(self):
        def boom(u):
            raise Exception("offline")
        meta, raw = web_source.collect_web("https://example.com/a", fetch_fn=boom)
        self.assertEqual(meta["platform"], "web")
        self.assertIsNone(raw)
        self.assertTrue(meta["title"])                    # 降级标题非空，仍能落基础卡


# ═════════════════════════════════════════════════════════════════
#  worker：网页分支落库（kind / raw / card / 轴表）
# ═════════════════════════════════════════════════════════════════
class TestWorkerWeb(Base):
    def setUp(self):
        super().setUp()
        server.worker = self._worker_backup  # 恢复真实 worker（本测试要真的落库）

    def _fake_collect(self, url, fetch_fn=None):
        meta = {"platform": "web", "name": "用 FastAPI 搭后端", "title": "用 FastAPI 搭后端",
                "description": "FastAPI 做后端接口", "url": url,
                "transcript": "今天讲 FastAPI 怎么搭后端接口，适合做本地知识库。"}
        return meta, "今天讲 FastAPI 怎么搭后端接口，适合做本地知识库。"

    def test_worker_web_creates_cognition_card(self):
        orig = server.web_source.collect_web
        server.web_source.collect_web = self._fake_collect
        try:
            conn = db.connect()
            c = conn.cursor()
            c.execute(
                "INSERT INTO repos (url,note,status,created_at,source,kind) "
                "VALUES (?,?,?,?,?,?)",
                ("https://example.com/a/b", "", "queued", server.now(), "manual", "cognition"))
            rid = c.lastrowid
            conn.commit()
            conn.close()
            server.worker(rid, "https://example.com/a/b", "", "web", None, "manual")
        finally:
            server.web_source.collect_web = orig

        conn = db.connect()
        row = conn.execute(
            "SELECT kind,raw,meta,card,status FROM repos WHERE id=?", (rid,)).fetchone()
        ax = conn.execute(
            "SELECT value FROM repo_axis WHERE repo_id=? AND axis_key='domain'",
            (rid,)).fetchone()
        conn.close()

        self.assertEqual(row[0], "cognition")
        self.assertIn("FastAPI", row[1])              # 正文双保留（raw）
        meta = json.loads(row[2])
        self.assertEqual(meta["platform"], "web")
        card = json.loads(row[3])
        self.assertEqual(card["domain"], "Web 框架")
        self.assertEqual(row[4], "carded")
        self.assertIsNotNone(ax)                      # 轴表已同步


class TestRelatedContract(Base):
    """方案 B · 关联计算去重：_related_for 规则回退应保持既有关联契约。"""

    def test_related_fallback_links_same_domain(self):
        self.add_repo("https://github.com/a/alpha",
                      card={"domain": "AI·LLM", "tags": ["llm", "工具"]})
        self.add_repo("https://github.com/b/beta",
                      card={"domain": "AI·LLM", "tags": ["llm"]})
        self.add_repo("https://github.com/c/gamma",
                      card={"domain": "数据库", "tags": ["sql"]})
        new_meta = {"name": "delta", "description": "llm 推理库"}
        conn = db.connect()
        try:
            nodes = analyze._related_for(new_meta, "想试试", 0, conn, domain="AI·LLM")
        finally:
            conn.close()
        self.assertTrue(nodes)
        reasons = {n["url"]: n["reason"] for n in nodes}
        self.assertIn("https://github.com/a/alpha", reasons)
        self.assertEqual(reasons["https://github.com/a/alpha"], "同属「AI·LLM」")
        self.assertIn("https://github.com/b/beta", reasons)
        # 不同领域、不共享标签的不相关项不该混进来
        self.assertNotIn("https://github.com/c/gamma", reasons)


class TestVersionedCache(Base):
    """方案 C · 热点接口 TTL 缓存：同库复用（同一对象），切库即失效（隔离正确）。"""

    def test_graph_cache_reuses_same_object_and_invalidates_on_db_switch(self):
        g1 = server.get_graph()
        self.assertIs(server.get_graph(), g1)          # TTL 窗内同库 → 命中同对象
        # 切到另一个临时库：key 含库路径 → 立即失效重建（保证测试/换库隔离）
        other = os.path.join(self.tmp, "other.db")
        config.DB = other
        server.init_db()
        g2 = server.get_graph()
        self.assertIsNot(g2, g1)

    def test_profile_cache_reuses_same_object_and_invalidates_on_db_switch(self):
        p1 = server.get_profile()
        self.assertIs(server.get_profile(), p1)
        other = os.path.join(self.tmp, "other2.db")
        config.DB = other
        server.init_db()
        p2 = server.get_profile()
        self.assertIsNot(p2, p1)


if __name__ == "__main__":
    unittest.main()
