"""HTTP 集成测试：真起一个服务，真发请求。

为什么单元测试不够：路由级回归只有把请求真打进去才看得见 ——
例如某个接口被挪进 /api/ 分支之后就再也到不了（/health 就这么丢过一次）。

这里只测本机逻辑，不碰 /api/recommend 与 /api/trending（那两个要联外网）。
"""

import json
import threading
import http.client
from http.server import ThreadingHTTPServer

from common import Base, config, db, server


class TestHttp(Base):
    def setUp(self):
        super().setUp()
        # 端口给 0，让系统分配空闲端口，避免与真实运行的 8732 打架
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self._th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._th.start()

    def tearDown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        super().tearDown()

    # ---- 请求工具 ----
    def req(self, method, path, body=None, headers=None, raw=None):
        h = {"Connection": "close"}
        h.update(headers or {})
        payload = raw
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=payload, headers=h)
            r = conn.getresponse()
            data = r.read()
            return r.status, data, dict(r.getheaders())
        finally:
            conn.close()

    def jreq(self, method, path, body=None, headers=None):
        code, data, _ = self.req(method, path, body, headers)
        try:
            return code, json.loads(data.decode("utf-8"))
        except Exception:
            return code, None

    # ---- 基础路由 ----
    def test_health(self):
        code, body = self.jreq("GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_root_serves_app_shell(self):
        """根路径返回 App 壳（shell.html），带壳标识 id="app-shell"。"""
        code, data, hdrs = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        self.assertIn("app-shell", data.decode("utf-8"))

    def test_static_css_js_are_served(self):
        code, _, hdrs = self.req("GET", "/theme.css")
        self.assertEqual(code, 200)
        self.assertIn("text/css", hdrs["Content-Type"])
        code, _, hdrs = self.req("GET", "/nav.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", hdrs["Content-Type"])

    def test_every_page_is_reachable(self):
        """页面清单是目录级 serve，新增页面不需要改后端 —— 这条锁住这一点。"""
        for page in ("collect.html", "cards.html", "map.html", "recommend.html",
                     "profile.html", "settings.html", "host.html", "embed.html",
                     "ball-demo.html", "ball.js"):
            code, _, _ = self.req("GET", "/" + page)
            self.assertEqual(code, 200, page + " 不可达")

    def test_unknown_page_404(self):
        code, _, _ = self.req("GET", "/nope.html")
        self.assertEqual(code, 404)

    def test_unknown_api_404(self):
        code, body = self.jreq("GET", "/api/nope")
        self.assertEqual(code, 404)

    def test_path_traversal_blocked(self):
        for bad in ("/../README.md", "/..%2f..%2fsrc%2fserver.py", "/%2e%2e/README.md"):
            code, _, _ = self.req("GET", bad)
            self.assertEqual(code, 404, bad + " 不应可达")

    # ---- 读接口 ----
    def test_cards_and_facets(self):
        rid = self.add_repo("https://github.com/a/b", meta={"name": "a/b"},
                            card={"domain": "安全", "tech_stack": ["Go"], "source": "heuristic"})
        conn = db.connect()
        server.sync_axes(conn, rid, {"domain": "安全", "tech_stack": ["Go"], "source": "heuristic"},
                         None, {}, use_llm=False)
        conn.close()
        code, body = self.jreq("GET", "/api/cards")
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 1)
        code, facets = self.jreq("GET", "/api/facets")
        self.assertEqual(code, 200)
        self.assertIn("domain", facets)

    def test_cards_filters_over_http(self):
        rid = self.add_repo("https://github.com/a/b", meta={"name": "a/b"},
                            card={"domain": "安全", "tech_stack": ["Go"], "source": "heuristic"})
        conn = db.connect()
        server.sync_axes(conn, rid, {"domain": "安全", "tech_stack": ["Go"], "source": "heuristic"},
                         None, {}, use_llm=False)
        conn.close()
        code, body = self.jreq("GET", "/api/cards?domain=%E5%AE%89%E5%85%A8")
        self.assertEqual(body["count"], 1)
        code, body = self.jreq("GET", "/api/cards?domain=%E5%AE%89%E5%85%A8&tech=Python")
        self.assertEqual(body["count"], 0)

    def test_graph_has_links_and_sample(self):
        code, g = self.jreq("GET", "/api/graph")
        self.assertEqual(code, 200)
        self.assertIn("domain_links", g)
        self.assertIn("sample", g)
        self.assertEqual(g["gap_source"], "seed")

    def test_profile_exposes_sources(self):
        code, p = self.jreq("GET", "/api/profile")
        self.assertEqual(code, 200)
        self.assertIn("sources", p)
        self.assertIn("techs_top", p)

    def test_doctor_ok_on_clean_db(self):
        code, rep = self.jreq("GET", "/api/doctor")
        self.assertEqual(code, 200)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["db"]["journal_mode"], "wal")

    def test_items_search(self):
        self.add_repo("https://github.com/oven-sh/bun", meta={"name": "oven-sh/bun"})
        code, body = self.jreq("GET", "/api/items?q=bun")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["items"]), 1)

    # ---- 写接口 ----
    def test_collect_and_duplicate(self):
        code, body = self.jreq("POST", "/collect",
                               {"url": "https://github.com/a/b", "source": "recommend"})
        self.assertEqual(code, 200)
        self.assertFalse(body.get("dup"))
        self.assertEqual(body["source"], "recommend")
        code, body2 = self.jreq("POST", "/collect", {"url": "https://github.com/a/b"})
        self.assertTrue(body2["dup"])

    def test_collect_normalizes_equivalent_urls(self):
        # 带跟踪参数 + 尾部斜杠 + fragment 的写法，规范化后应撞上同一仓库 → dup
        code, body = self.jreq("POST", "/collect",
                               {"url": "https://github.com/gh/x/?utm_source=news&sid=1#top"})
        self.assertEqual(code, 200)
        self.assertFalse(body.get("dup"))
        code, body2 = self.jreq("POST", "/collect", {"url": "https://github.com/gh/x#readme"})
        self.assertTrue(body2.get("dup"))

    def test_collect_near_dup_soft_warns(self):
        # 已收藏 owner/repo，再来一个不同大小写的"变体"链接：不硬阻断，仅 near_dup 软提示
        self.add_repo("https://github.com/owner/repo", meta={"name": "owner/repo"})
        code, body = self.jreq("POST", "/collect", {"url": "https://github.com/Owner/Repo"})
        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        self.assertFalse(body.get("dup"))
        self.assertEqual(server._near_dup.__name__, "_near_dup")  # 探针，确保符号存在
        nd = body.get("near_dup")
        self.assertIsNotNone(nd)
        self.assertEqual(nd["url"], "https://github.com/owner/repo")

    def test_collect_rejects_bad_url(self):
        # 非 http(s) 链接仍拒绝（网页/文章链接已是合法来源）
        code, body = self.jreq("POST", "/collect", {"url": "ftp://example.com/a/b"})
        self.assertEqual(code, 400)

    def test_collect_accepts_web_url(self):
        code, body = self.jreq("POST", "/collect", {"url": "https://example.com/a/b"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_collect_from_text_field(self):
        code, body = self.jreq("POST", "/collect",
                               {"text": "推荐这个https://github.com/a/b很好用"})
        self.assertEqual(code, 200)
        conn = db.connect()
        row = conn.execute("SELECT url FROM repos WHERE id=?", (body["id"],)).fetchone()
        conn.close()
        self.assertEqual(row[0], "https://github.com/a/b")

    def test_collect_body_too_large(self):
        backup = config.MAX_BODY
        try:
            config.MAX_BODY = 64
            code, body = self.jreq("POST", "/collect", {"url": "https://github.com/a/b" + "x" * 200})
            self.assertEqual(code, 413)
        finally:
            config.MAX_BODY = backup

    def test_bad_json_400(self):
        code, _, _ = self.req("POST", "/collect", raw=b"{not json",
                              headers={"Content-Type": "application/json"})
        self.assertEqual(code, 400)

    def test_regenerate_requires_id(self):
        code, body = self.jreq("POST", "/api/card/regenerate", {})
        self.assertEqual(code, 400)

    def test_reindex_repairs_drift_over_http(self):
        rid = self.add_repo("https://github.com/a/b",
                            meta={"name": "a/b", "language": "Python", "topics": ["llm"]},
                            card={"domain": "AI·LLM", "tech_stack": ["Python"], "source": "heuristic"})
        conn = db.connect()
        conn.execute("INSERT INTO repo_axis (repo_id,axis_key,value,source,confidence) VALUES (?,?,?,?,?)",
                     (rid, "domain", "错的值", "heuristic", 0.6))
        conn.commit()
        conn.close()
        code, rep = self.jreq("GET", "/api/doctor")
        self.assertFalse(rep["ok"])
        code, body = self.jreq("POST", "/api/reindex")
        self.assertEqual(code, 200)
        self.assertEqual(body["rebuild"], 1)
        code, rep = self.jreq("GET", "/api/doctor")
        self.assertTrue(rep["ok"], rep["axis_mismatch"])

    # ---- 安全守卫 ----
    def test_cross_origin_delete_is_blocked(self):
        rid = self.add_repo("https://github.com/a/b")
        code, body = self.jreq("DELETE", "/api/item/%d" % rid,
                               headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        conn = db.connect()
        still = conn.execute("SELECT id FROM repos WHERE id=?", (rid,)).fetchone()
        conn.close()
        self.assertIsNotNone(still, "跨源删除居然生效了")

    def test_same_origin_delete_works(self):
        rid = self.add_repo("https://github.com/a/b")
        code, body = self.jreq("DELETE", "/api/item/%d" % rid,
                               headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(code, 200)

    def test_cross_origin_regenerate_is_blocked(self):
        rid = self.add_repo("https://github.com/a/b")
        code, body = self.jreq("POST", "/api/card/regenerate", {"id": rid},
                               headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)

    def test_cross_origin_collect_is_allowed_by_design(self):
        """书签工具跑在 github.com 上、悬浮球被嵌进第三方页面 —— 收藏必须放开跨源。"""
        code, body = self.jreq("POST", "/collect", {"url": "https://github.com/a/b"},
                               headers={"Origin": "https://github.com"})
        self.assertEqual(code, 200)

    def test_token_required_when_configured(self):
        backup = config.TOKEN
        try:
            config.TOKEN = "s3cret"
            code, _ = self.jreq("GET", "/api/items")
            self.assertEqual(code, 401)
            code, _ = self.jreq("GET", "/api/items", headers={"X-Collector-Token": "s3cret"})
            self.assertEqual(code, 200)
            code, _ = self.jreq("POST", "/collect", {"url": "https://github.com/a/b"})
            self.assertEqual(code, 401)
            code, body = self.jreq("POST", "/collect", {"url": "https://github.com/a/b"},
                                   headers={"X-Collector-Token": "s3cret"})
            self.assertEqual(code, 200)
        finally:
            config.TOKEN = backup

    def test_preflight_allows_token_header(self):
        code, _, hdrs = self.req("OPTIONS", "/collect",
                                 headers={"Origin": "https://github.com",
                                          "Access-Control-Request-Method": "POST"})
        self.assertEqual(code, 204)
        self.assertIn("X-Collector-Token", hdrs["Access-Control-Allow-Headers"])

    # ---- 深链 ----
    def test_deep_link_collects_and_records_share_source(self):
        code, data, hdrs = self.req(
            "GET", "/collect?url=" + "https%3A%2F%2Fgithub.com%2Fa%2Fb")
        self.assertEqual(code, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        self.assertIn("已收藏", data.decode("utf-8"))
        conn = db.connect()
        row = conn.execute("SELECT url, source FROM repos").fetchone()
        conn.close()
        self.assertEqual(row[0], "https://github.com/a/b")
        self.assertEqual(row[1], "share")

    def test_deep_link_with_text_extracts_url(self):
        from urllib.parse import quote
        code, data, _ = self.req(
            "GET", "/collect?text=" + quote("快看这个https://github.com/a/b不错"))
        self.assertEqual(code, 200)
        conn = db.connect()
        row = conn.execute("SELECT url FROM repos").fetchone()
        conn.close()
        self.assertEqual(row[0], "https://github.com/a/b")

    def test_deep_link_bad_url_shows_error_page(self):
        # 非 http(s) 链接仍展示错误页（网页/文章链接已是合法来源）
        code, data, _ = self.req("GET", "/collect?url=" + "ftp%3A%2F%2Fexample.com%2Fa%2Fb")
        self.assertEqual(code, 400)
        self.assertIn("未能收藏", data.decode("utf-8"))

    def test_deep_link_web_url_collects(self):
        code, data, _ = self.req("GET", "/collect?url=" + "https%3A%2F%2Fexample.com%2Fa%2Fb")
        self.assertEqual(code, 200)
        self.assertIn("已收藏", data.decode("utf-8"))
