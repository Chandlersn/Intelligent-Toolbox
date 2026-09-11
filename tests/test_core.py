"""收藏箱核心回归测试（零依赖，纯标准库 unittest）。

怎么跑：
    python -m unittest discover -s tests -p "test_*.py" -v

设计原则：
- 每个用例用独立临时 DB，绝不动 repo_collector.db。
- LLM 一律用假模块注入，测试不联网、不依赖灯笼、不烧 token。
- 只测「契约」，不测实现细节 —— 最容易随重构失效的是前者被忽略、后者被写死。
"""

import json
import unittest

from common import Base, FakeLLM  # noqa: F401  (FakeLLM 供部分用例注入假模型)
from common import analyze, config, db, server  # noqa: F401


# ═══════════════════════════════════════════════════════════════════
#  1. 地址解析
# ═══════════════════════════════════════════════════════════════════
class TestUrlParsing(Base):
    def test_github_ok(self):
        self.assertEqual(server.valid_repo_url("https://github.com/pallets/flask"),
                         ("github", "/pallets/flask"))

    def test_deep_path_truncated_to_owner_repo(self):
        # 仓库内的子路径 / tree 页也应归一到 owner/repo
        self.assertEqual(server.valid_repo_url("https://github.com/a/b/tree/main/src"),
                         ("github", "/a/b"))

    def test_gitlab_ok(self):
        self.assertEqual(server.valid_repo_url("https://gitlab.com/a/b"), ("gitlab", "/a/b"))

    def test_rejects_non_repo_host(self):
        self.assertIsNone(server.valid_repo_url("https://example.com/a/b"))
        self.assertIsNone(server.valid_repo_url("https://bitbucket.org/a/b"))
        self.assertIsNone(server.valid_repo_url("ftp://github.com/a/b"))
        self.assertIsNone(server.valid_repo_url("https://github.com/onlyone"))

    def test_extract_from_text_strips_punctuation(self):
        txt = "推荐这个（好用）https://github.com/oven-sh/bun。值得看看"
        self.assertEqual(server.extract_repo_url(txt), "https://github.com/oven-sh/bun")

    def test_extract_stops_at_chinese_text_without_space(self):
        """中文写作里链接后常直接跟汉字，不能把汉字吞进 URL。"""
        self.assertEqual(server.extract_repo_url("推荐https://github.com/a/b很好用"),
                         "https://github.com/a/b")
        self.assertEqual(server.extract_repo_url("看这个https://github.com/a/b。"),
                         "https://github.com/a/b")

    def test_extract_keeps_legit_url_chars(self):
        u = "https://github.com/a/b"
        self.assertEqual(server.extract_repo_url(u + "?tab=readme#x"), u + "?tab=readme#x")

    def test_extract_skips_non_repo_links(self):
        txt = "先看 https://example.com/x 再看 https://github.com/oven-sh/bun"
        self.assertEqual(server.extract_repo_url(txt), "https://github.com/oven-sh/bun")

    def test_extract_empty(self):
        self.assertIsNone(server.extract_repo_url(""))
        self.assertIsNone(server.extract_repo_url("没有链接的一段话"))


# ═══════════════════════════════════════════════════════════════════
#  2. 启发式分析器
# ═══════════════════════════════════════════════════════════════════
class TestHeuristics(Base):
    def test_domain_by_topics_and_description(self):
        meta = {"language": "Python", "topics": ["llm", "rag"],
                "description": "A framework for LLM agents"}
        self.assertEqual(analyze.detect_domain(meta), "AI·LLM")

    def test_domain_unknown_when_no_signal(self):
        self.assertEqual(analyze.detect_domain({}), "未分类")

    def test_tech_stack_filters_soft_topics(self):
        meta = {"language": "Python", "topics": ["awesome", "tutorial", "llm"]}
        stack = analyze.detect_tech_stack(meta)
        self.assertIn("Python", stack)
        self.assertIn("llm", stack)
        self.assertNotIn("awesome", stack)
        self.assertNotIn("tutorial", stack)

    def test_tech_stack_dedupes_language_case_insensitively(self):
        meta = {"language": "Python", "topics": ["python"]}
        stack = [s.lower() for s in analyze.detect_tech_stack(meta)]
        self.assertEqual(stack.count("python"), 1)

    def test_maturity_buckets(self):
        self.assertTrue(analyze.detect_maturity({"stars": 50000}).startswith("顶级"))
        self.assertTrue(analyze.detect_maturity({"stars": 8000}).startswith("成熟"))
        self.assertTrue(analyze.detect_maturity({"stars": 600}).startswith("活跃"))
        self.assertTrue(analyze.detect_maturity({"stars": 10}).startswith("新兴"))
        self.assertTrue(analyze.detect_maturity({}).startswith("未知"))

    def test_recommendation_follows_stars(self):
        self.assertEqual(analyze.detect_recommendation({"stars": 50000}), 5)
        self.assertEqual(analyze.detect_recommendation({"stars": 0}), 0)

    def test_build_card_marks_source_heuristic(self):
        card = analyze.build_card({"description": "x", "topics": ["cli"]}, None, 0, None)
        self.assertEqual(card["source"], "heuristic")
        self.assertIn("domain", card)


# ═══════════════════════════════════════════════════════════════════
#  3. LLM 胶水（唯一入口 _llm_json 的行为）
# ═══════════════════════════════════════════════════════════════════
class TestLlmGlue(Base):
    def test_parses_fenced_json(self):
        analyze.LLM_MODE = {"module": FakeLLM('```json\n{"domain": "AI·LLM"}\n```'), "tried": True}
        self.assertEqual(analyze._llm_json("s", "u", expect="dict"), {"domain": "AI·LLM"})

    def test_parses_bare_json(self):
        analyze.LLM_MODE = {"module": FakeLLM('[{"domain": "安全"}]'), "tried": True}
        self.assertEqual(analyze._llm_json("s", "u", expect="list"), [{"domain": "安全"}])

    def test_shape_mismatch_returns_none(self):
        analyze.LLM_MODE = {"module": FakeLLM('{"a": 1}'), "tried": True}
        self.assertIsNone(analyze._llm_json("s", "u", expect="list"))

    def test_broken_json_returns_none(self):
        analyze.LLM_MODE = {"module": FakeLLM("模型今天不想输出 JSON"), "tried": True}
        self.assertIsNone(analyze._llm_json("s", "u", expect="dict"))

    def test_exception_returns_none(self):
        analyze.LLM_MODE = {"module": FakeLLM(RuntimeError("熔断中")), "tried": True}
        self.assertIsNone(analyze._llm_json("s", "u", expect="dict"))

    def test_unavailable_module_returns_none(self):
        analyze.LLM_MODE = {"module": None, "tried": True}
        self.assertIsNone(analyze._llm_json("s", "u", expect="dict"))

    def test_timeout_budget_is_passed_through(self):
        fake = FakeLLM('{"ok": true}')
        analyze.LLM_MODE = {"module": fake, "tried": True}
        analyze._llm_json("s", "u", expect="dict", timeout=7, retries=2)
        self.assertEqual(fake.calls[0][2], {"timeout": 7, "retries": 2})


# ═══════════════════════════════════════════════════════════════════
#  4. 多轴同步（本项目的核心契约）
# ═══════════════════════════════════════════════════════════════════
class TestAxesSync(Base):
    def test_sync_writes_domain_tech_timeline(self):
        rid = self.add_repo("https://github.com/a/b")
        card = {"domain": "AI·LLM", "tech_stack": ["Python", "pytorch"],
                "maturity": "成熟", "tags": ["llm"], "source": "heuristic"}
        conn = db.connect()
        server.sync_axes(conn, rid, card, None, {}, use_llm=False)
        conn.close()
        self.assertEqual(self.axis_value(rid, "domain"), "AI·LLM")
        self.assertEqual(self.axis_value(rid, "timeline"), "成熟")
        techs = [r[1] for r in self.axis_rows(rid) if r[0] == "tech"]
        # 轴上的技术栈经过归一化（小写），卡片上的 tech_stack 仍保留原始大小写
        self.assertEqual(sorted(techs), ["python", "pytorch"])

    def test_provenance_follows_card_source_llm(self):
        """P0-2 回归：LLM 分析的卡片，轴表不能标成 heuristic。"""
        rid = self.add_repo("https://github.com/a/b")
        card = {"domain": "AI·LLM", "tech_stack": ["Python"], "source": "llm"}
        conn = db.connect()
        server.sync_axes(conn, rid, card, None, {}, use_llm=False)
        conn.close()
        dom = [r for r in self.axis_rows(rid) if r[0] == "domain"][0]
        self.assertEqual(dom[2], "llm")
        self.assertGreaterEqual(dom[3], 0.8)

    def test_provenance_follows_card_source_heuristic(self):
        rid = self.add_repo("https://github.com/a/b")
        card = {"domain": "安全", "tech_stack": ["Go"], "source": "heuristic"}
        conn = db.connect()
        server.sync_axes(conn, rid, card, None, {}, use_llm=False)
        conn.close()
        dom = [r for r in self.axis_rows(rid) if r[0] == "domain"][0]
        self.assertEqual(dom[2], "heuristic")
        self.assertLess(dom[3], 0.8)

    def test_sync_is_idempotent(self):
        rid = self.add_repo("https://github.com/a/b")
        card = {"domain": "安全", "tech_stack": ["Go", "Rust"], "tags": ["t1"], "source": "heuristic"}
        conn = db.connect()
        server.sync_axes(conn, rid, card, None, {}, use_llm=False)
        first = sorted(map(tuple, self.axis_rows(rid)))
        server.sync_axes(conn, rid, card, None, {}, use_llm=False)
        second = sorted(map(tuple, self.axis_rows(rid)))
        conn.close()
        self.assertEqual(first, second)

    def test_note_without_llm_falls_back_to_keyword_motive(self):
        rid = self.add_repo("https://github.com/a/b")
        card = {"domain": "安全", "tech_stack": [], "source": "heuristic"}
        conn = db.connect()
        server.sync_axes(conn, rid, card, "想用它学习一下鉴权", {}, use_llm=False)
        conn.close()
        self.assertEqual(self.axis_value(rid, "motive"), "主动寻找")
        self.assertEqual(self.axis_value(rid, "scene"), "学习研究")

    def test_note_without_note_marks_passive_motive(self):
        rid = self.add_repo("https://github.com/a/b")
        conn = db.connect()
        server.sync_axes(conn, rid, {"domain": "安全", "source": "heuristic"}, None, {}, use_llm=False)
        conn.close()
        self.assertEqual(self.axis_value(rid, "motive"), "随手刷到")


# ═══════════════════════════════════════════════════════════════════
#  4b. 技术栈归一化（轴用于聚合，取粗；卡片用于展示，取细）
# ═══════════════════════════════════════════════════════════════════
class TestTechNormalization(Base):
    def test_case_and_separators(self):
        self.assertEqual(analyze.normalize_tech("TypeScript"), "typescript")
        self.assertEqual(analyze.normalize_tech("node_js"), "nodejs")
        self.assertEqual(analyze.normalize_tech("Node JS"), "nodejs")
        self.assertEqual(analyze.normalize_tech("  Go  "), "go")

    def test_aliases(self):
        self.assertEqual(analyze.normalize_tech("JS"), "javascript")
        self.assertEqual(analyze.normalize_tech("golang"), "go")
        self.assertEqual(analyze.normalize_tech("k8s"), "kubernetes")

    def test_collapses_nested_variants_within_one_repo(self):
        """同一项目打的层层嵌套 topic 应合并到最短那个，否则胶囊云会被塞满。"""
        out = analyze.collapse_tech_variants(
            ["dsh", "dsh-plugin", "dsh-plugin-market", "TypeScript"])
        self.assertEqual(sorted(out), ["dsh", "typescript"])

    def test_does_not_merge_across_unrelated(self):
        out = analyze.collapse_tech_variants(["Python", "Go", "Rust"])
        self.assertEqual(sorted(out), ["go", "python", "rust"])

    def test_axis_stores_collapsed_value(self):
        rid = self.add_repo("https://github.com/a/b")
        card = {"domain": "前端/UI", "source": "heuristic",
                "tech_stack": ["dsh", "dsh-plugin", "dsh-plugin-market", "TypeScript"]}
        conn = db.connect()
        server.sync_axes(conn, rid, card, None, {}, use_llm=False)
        conn.close()
        techs = sorted(r[1] for r in self.axis_rows(rid) if r[0] == "tech")
        self.assertEqual(techs, ["dsh", "typescript"])

    def test_filter_tolerates_original_casing(self):
        """用户手敲原始大小写也要能筛到（轴里存的是归一化后的值）。"""
        rid = self.add_repo("https://github.com/a/b")
        card = {"domain": "AI·LLM", "tech_stack": ["Python"], "source": "heuristic"}
        conn = db.connect()
        server.sync_axes(conn, rid, card, None, {}, use_llm=False)
        conn.close()
        self.assertEqual(server.query_axes([("tech", "Python")]), {rid})
        self.assertEqual(server.query_axes([("tech", "python")]), {rid})
        self.assertEqual(
            [i["id"] for i in server.get_cards(
                {"domain": [], "tech": ["Python"], "motive": [], "use": []})], [rid])


# ═══════════════════════════════════════════════════════════════════
#  4c. 使用反馈（用过 / 弃了 / 还想用）
# ═══════════════════════════════════════════════════════════════════
class TestFeedback(Base):
    def _make(self, suffix="b"):
        rid = self.add_repo("https://github.com/a/" + suffix,
                            meta={"name": "a/b"},
                            card={"domain": "安全", "tech_stack": ["Go"],
                                  "source": "heuristic"})
        conn = db.connect()
        server.sync_axes(conn, rid, {"domain": "安全", "tech_stack": ["Go"],
                                     "source": "heuristic"}, None, {}, use_llm=False)
        conn.close()
        return rid

    def test_set_feedback_writes_column_and_axis(self):
        rid = self._make()
        code, body = server.set_feedback(rid, "used")
        self.assertEqual(code, 200)
        conn = db.connect()
        col = conn.execute("SELECT feedback FROM repos WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        self.assertEqual(col, "used")
        self.assertEqual(self.axis_value(rid, "use"), "用过")

    def test_feedback_axis_is_user_sourced(self):
        rid = self._make()
        server.set_feedback(rid, "want")
        row = [r for r in self.axis_rows(rid) if r[0] == "use"][0]
        self.assertEqual(row[2], "user")
        self.assertEqual(row[3], 1.0)

    def test_clicking_same_value_cancels(self):
        rid = self._make()
        server.set_feedback(rid, "used")
        code, body = server.set_feedback(rid, "used")
        self.assertEqual(code, 200)
        self.assertIsNone(body["feedback"])
        self.assertIsNone(self.axis_value(rid, "use"))

    def test_invalid_value_rejected(self):
        rid = self._make()
        code, body = server.set_feedback(rid, "maybe")
        self.assertEqual(code, 400)

    def test_missing_repo(self):
        code, body = server.set_feedback(99999, "used")
        self.assertEqual(code, 404)

    def test_reindex_preserves_feedback(self):
        rid = self._make()
        server.set_feedback(rid, "dropped")
        server.reindex_axes()
        self.assertEqual(self.axis_value(rid, "use"), "弃了")

    def test_profile_counts_feedback(self):
        a, b = self._make("x"), self._make("y")
        server.set_feedback(a, "used")
        server.set_feedback(b, "dropped")
        p = server.get_profile()
        by_key = {f["key"]: f["count"] for f in p["feedback"]}
        self.assertEqual(by_key.get("used"), 1)
        self.assertEqual(by_key.get("dropped"), 1)
        self.assertEqual(p["feedback_total"], 2)

    def test_cards_filter_by_use_axis(self):
        a, b = self._make("x"), self._make("y")
        server.set_feedback(a, "used")
        ids = [i["id"] for i in server.get_cards(
            {"domain": [], "tech": [], "motive": [], "use": ["用过"]})]
        self.assertEqual(ids, [a])


# ═══════════════════════════════════════════════════════════════════
#  5. 一致性契约：重算 / 重建 / 自检
# ═══════════════════════════════════════════════════════════════════
class TestConsistencyContract(Base):
    def _make_drifted(self):
        """造一条「卡片说 A、轴表说 B」的分叉数据，模拟 P0-1 的现场。"""
        rid = self.add_repo(
            "https://github.com/a/b",
            meta={"name": "a/b", "language": "Python", "topics": ["llm", "rag"],
                  "description": "LLM framework"},
            card={"domain": "前端/UI", "tech_stack": ["TypeScript"],
                  "maturity": "成熟", "source": "heuristic"})
        conn = db.connect()
        server.sync_axes(conn, rid, {"domain": "前端/UI", "tech_stack": ["TypeScript"],
                                     "source": "heuristic"}, None, {}, use_llm=False)
        conn.close()
        self.assertEqual(self.axis_value(rid, "domain"), "前端/UI")
        return rid

    def test_regenerate_keeps_axes_in_sync(self):
        """P0-1 回归：重算之后，轴表必须与卡片一致（而不是停留在旧领域）。"""
        rid = self._make_drifted()
        code, body = server.regenerate_card(rid)
        self.assertEqual(code, 200)
        card = self.card_of(rid)
        self.assertEqual(card["source"], "heuristic")
        self.assertEqual(self.axis_value(rid, "domain"), card["domain"])
        self.assertNotEqual(self.axis_value(rid, "domain"), "前端/UI",
                            "重算后轴表仍停留在旧领域，说明没同步")

    def test_regenerate_missing_id(self):
        code, body = server.regenerate_card(99999)
        self.assertEqual(code, 404)

    def test_reindex_repairs_drift(self):
        rid = self._make_drifted()
        # 手工把轴表改成错的，再重建
        conn = db.connect()
        conn.execute("UPDATE repo_axis SET value='错的值' WHERE repo_id=? AND axis_key='domain'", (rid,))
        conn.commit()
        conn.close()
        n = server.reindex_axes()
        self.assertGreaterEqual(n, 1)
        card = self.card_of(rid)
        self.assertEqual(self.axis_value(rid, "domain"), card["domain"])

    def test_doctor_detects_mismatch(self):
        rid = self._make_drifted()
        conn = db.connect()
        conn.execute("UPDATE repo_axis SET value='AI·LLM' WHERE repo_id=? AND axis_key='domain'", (rid,))
        conn.commit()
        conn.close()
        rep = server.doctor()
        self.assertFalse(rep["ok"])
        self.assertTrue(any(m["id"] == rid for m in rep["axis_mismatch"]))

    def test_doctor_clean_after_reindex(self):
        rid = self._make_drifted()
        server.reindex_axes()
        rep = server.doctor()
        self.assertTrue(rep["ok"], rep["axis_mismatch"])
        self.assertIn("journal_mode", rep["db"])


# ═══════════════════════════════════════════════════════════════════
#  6. 采集
# ═══════════════════════════════════════════════════════════════════
class TestCollect(Base):
    def test_rejects_non_repo_url(self):
        code, body = server.collect_payload("https://example.com/a/b", "")
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_accepts_and_records_source(self):
        code, body = server.collect_payload("https://github.com/a/b", "", "recommend")
        self.assertEqual(code, 200)
        self.assertEqual(body["source"], "recommend")
        conn = db.connect()
        row = conn.execute("SELECT source FROM repos WHERE id=?", (body["id"],)).fetchone()
        conn.close()
        self.assertEqual(row[0], "recommend")

    def test_unknown_source_falls_back_to_manual(self):
        code, body = server.collect_payload("https://github.com/a/b", "", "乱写的来源")
        self.assertEqual(body["source"], "manual")

    def test_duplicate_detected(self):
        server.collect_payload("https://github.com/a/b", "")
        code, body = server.collect_payload("https://github.com/a/b", "")
        self.assertTrue(body.get("dup"))

    def test_profile_counts_recommend_sources(self):
        """P0-3 回归：推荐来源必须能被统计出来，而不是恒为 0。"""
        server.collect_payload("https://github.com/a/b", "", "recommend")
        server.collect_payload("https://github.com/c/d", "", "manual")
        prof = server.get_profile()
        self.assertEqual(prof["from_recommend"], 1)
        self.assertEqual(prof["sources"].get("recommend"), 1)
        self.assertEqual(prof["sources"].get("manual"), 1)


# ═══════════════════════════════════════════════════════════════════
#  7. 检索（同层 OR / 跨层 AND）
# ═══════════════════════════════════════════════════════════════════
class TestSearch(Base):
    def setUp(self):
        super().setUp()
        self.r_ai = self.add_repo("https://github.com/a/ai",
                                  meta={"name": "a/ai", "description": "llm"},
                                  card={"domain": "AI·LLM", "tech_stack": ["Python"],
                                        "source": "heuristic", "tags": ["llm"]})
        self.r_sec = self.add_repo("https://github.com/a/sec",
                                   meta={"name": "a/sec", "description": "auth"},
                                   card={"domain": "安全", "tech_stack": ["Go"],
                                         "source": "heuristic", "tags": ["auth"]})
        conn = db.connect()
        for rid, card in ((self.r_ai, {"domain": "AI·LLM", "tech_stack": ["Python"], "source": "heuristic"}),
                          (self.r_sec, {"domain": "安全", "tech_stack": ["Go"], "source": "heuristic"})):
            server.sync_axes(conn, rid, card, "想学" if rid == self.r_ai else None, {}, use_llm=False)
        conn.close()

    def ids(self, **filters):
        filters.setdefault("domain", [])
        filters.setdefault("tech", [])
        filters.setdefault("motive", [])
        return sorted(i["id"] for i in server.get_cards(filters))

    def test_no_filter_returns_all(self):
        self.assertEqual(self.ids(), sorted([self.r_ai, self.r_sec]))

    def test_same_layer_is_or(self):
        self.assertEqual(self.ids(domain=["AI·LLM", "安全"]), sorted([self.r_ai, self.r_sec]))

    def test_cross_layer_is_and(self):
        # 领域是 AI·LLM 且 技术栈是 Go → 没有交集
        self.assertEqual(self.ids(domain=["AI·LLM"], tech=["Go"]), [])
        self.assertEqual(self.ids(domain=["AI·LLM"], tech=["Python"]), [self.r_ai])

    def test_motive_layer(self):
        self.assertEqual(self.ids(motive=["主动寻找"]), [self.r_ai])

    def test_fulltext_q(self):
        self.assertEqual(self.ids(q="auth"), [self.r_sec])

    def test_facets_expose_three_layers(self):
        conn = db.connect()
        rows = conn.execute("SELECT axis_key FROM repo_axis GROUP BY axis_key").fetchall()
        conn.close()
        keys = {r[0] for r in rows}
        self.assertIn("domain", keys)
        self.assertIn("tech", keys)
        self.assertIn("motive", keys)

    def test_query_axes_is_and(self):
        self.assertEqual(server.query_axes([("domain", "AI·LLM"), ("tech", "Python")]), {self.r_ai})
        self.assertEqual(server.query_axes([("domain", "AI·LLM"), ("tech", "Go")]), set())


# ═══════════════════════════════════════════════════════════════════
#  8. 删除
# ═══════════════════════════════════════════════════════════════════
class TestDelete(Base):
    def test_delete_removes_item_and_axes(self):
        rid = self.add_repo("https://github.com/a/b")
        conn = db.connect()
        server.sync_axes(conn, rid, {"domain": "安全", "tech_stack": ["Go"], "source": "heuristic"},
                         None, {}, use_llm=False)
        conn.close()
        code, body = server.delete_item(rid)
        self.assertEqual(code, 200)
        self.assertEqual(self.axis_rows(rid), [])
        self.assertIsNone(self.card_of(rid))

    def test_delete_missing(self):
        code, body = server.delete_item(4242)
        self.assertEqual(code, 404)


# ═══════════════════════════════════════════════════════════════════
#  9. 静态文件与守卫
# ═══════════════════════════════════════════════════════════════════
class _Headers:
    def __init__(self, d):
        self._d = d

    def get(self, k, default=None):
        return self._d.get(k, default)


def _fake_handler(origin=None, token=None, path="/"):
    h = object.__new__(server.Handler)
    d = {}
    if origin:
        d["Origin"] = origin
    if token:
        d["X-Collector-Token"] = token
    h.headers = _Headers(d)
    h.path = path
    return h


class TestStaticAndGuards(Base):
    def test_resolves_existing_page(self):
        self.assertTrue(server.resolve_static("cards.html"))
        self.assertTrue(server.resolve_static("/theme.css"))

    def test_rejects_missing(self):
        self.assertIsNone(server.resolve_static("nope.html"))

    def test_rejects_path_traversal(self):
        self.assertIsNone(server.resolve_static("../README.md"))
        self.assertIsNone(server.resolve_static("/../src/server.py"))
        self.assertIsNone(server.resolve_static("..%2f..%2fetc%2fpasswd"))

    def test_cross_origin_is_rejected(self):
        self.assertFalse(_fake_handler(origin="https://evil.example")._origin_local())
        self.assertTrue(_fake_handler(origin="http://127.0.0.1:8732")._origin_local())
        self.assertTrue(_fake_handler(origin="http://localhost:4321")._origin_local())
        self.assertTrue(_fake_handler()._origin_local())  # 无 Origin：本机工具

    def test_token_off_by_default(self):
        self.assertTrue(_fake_handler(origin="https://evil.example")._token_ok())

    def test_token_when_configured(self):
        backup = config.TOKEN
        try:
            config.TOKEN = "s3cret"
            self.assertTrue(_fake_handler(token="s3cret")._token_ok())
            self.assertTrue(_fake_handler(path="/collect?k=s3cret")._token_ok())
            self.assertFalse(_fake_handler(token="wrong")._token_ok())
            self.assertFalse(_fake_handler()._token_ok())
        finally:
            config.TOKEN = backup


# ═══════════════════════════════════════════════════════════════════
#  10. 缺角的诚实性
# ═══════════════════════════════════════════════════════════════════
class TestGapHonesty(Base):
    def test_gaps_are_labelled_as_seed(self):
        rid = self.add_repo("https://github.com/a/b")
        conn = db.connect()
        server.sync_axes(conn, rid, {"domain": "前端/UI", "tech_stack": ["TypeScript"],
                                     "source": "heuristic"}, None, {}, use_llm=False)
        conn.close()
        g = server.get_graph()
        self.assertEqual(g["gap_source"], "seed")
        for gap in g["gaps"]:
            self.assertEqual(gap["source"], "seed")
            # 样本稀疏时必须提示，不能当成结论
            self.assertIn("观察", gap["hint"])

    def test_domain_links_come_from_real_data(self):
        """领域邻接必须来自真实技术栈交集，而不是预判。"""
        a = self.add_repo("https://github.com/a/x")
        b = self.add_repo("https://github.com/b/y")
        conn = db.connect()
        server.sync_axes(conn, a, {"domain": "AI·LLM", "tech_stack": ["Python", "pytorch"],
                                   "source": "heuristic"}, None, {}, use_llm=False)
        server.sync_axes(conn, b, {"domain": "数据/可视化", "tech_stack": ["Python", "pandas"],
                                   "source": "heuristic"}, None, {}, use_llm=False)
        conn.close()
        links = server.get_graph()["domain_links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["shared"], 1)   # 只在 python 上交汇
        self.assertEqual({links[0]["from"], links[0]["to"]},
                         {"AI·LLM", "数据/可视化"})

    def test_no_links_when_nothing_shared(self):
        a = self.add_repo("https://github.com/a/x")
        b = self.add_repo("https://github.com/b/y")
        conn = db.connect()
        server.sync_axes(conn, a, {"domain": "AI·LLM", "tech_stack": ["Python"],
                                   "source": "heuristic"}, None, {}, use_llm=False)
        server.sync_axes(conn, b, {"domain": "安全", "tech_stack": ["Go"],
                                   "source": "heuristic"}, None, {}, use_llm=False)
        conn.close()
        self.assertEqual(server.get_graph()["domain_links"], [])


# ═══════════════════════════════════════════════════════════════════
#  仓库更新检测（不联网：用注入的假 fetch 验证 diff / 幂等 / 节流 / 已读）
# ═══════════════════════════════════════════════════════════════════
class TestUpdates(Base):
    def _seed(self, meta):
        return self.add_repo("https://github.com/o/r",
                             meta=meta, card={"domain": "AI·LLM", "source": "heuristic"})

    def _meta(self, rid):
        conn = db.connect()
        row = conn.execute("SELECT meta FROM repos WHERE id=?", (rid,)).fetchone()
        conn.close()
        return json.loads(row[0]) if row and row[0] else {}

    def test_detects_four_kinds(self):
        rid = self._seed({"name": "o/r", "stars": 10, "pushed_at": "2024-01-01",
                          "description": "old", "language": "Python"})
        def fake(path):
            return {"name": path, "stars": 20, "pushed_at": "2026-09-01",
                    "description": "new", "language": "Rust"}
        res = server.check_repo_updates(fetch_fn=fake, force=True)
        self.assertEqual(res["checked"], 1)
        self.assertEqual(len(res["events"]), 4)  # stars / push / desc / lang
        self.assertEqual(server.get_updates()["count"], 4)
        self.assertEqual(self._meta(rid)["stars"], 20)  # 基线已刷新

    def test_idempotent_on_recheck(self):
        rid = self._seed({"name": "o/r", "stars": 10, "pushed_at": "2024-01-01",
                          "description": "old", "language": "Python"})
        def fake(path):
            return {"name": path, "stars": 20, "pushed_at": "2026-09-01",
                    "description": "new", "language": "Rust"}
        server.check_repo_updates(fetch_fn=fake, force=True)
        server.check_repo_updates(fetch_fn=fake, force=True)  # meta 已是 fresh，无新变化
        self.assertEqual(server.get_updates()["count"], 4)

    def test_keeps_only_latest_unseen_per_kind(self):
        rid = self._seed({"name": "o/r", "stars": 10})
        server.check_repo_updates(fetch_fn=lambda p: {"name": p, "stars": 20}, force=True)
        server.check_repo_updates(fetch_fn=lambda p: {"name": p, "stars": 30}, force=True)
        conn = db.connect()
        rows = conn.execute(
            "SELECT old_val,new_val FROM repo_events WHERE repo_id=? AND kind='stars' AND seen=0",
            (rid,)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "30")

    def test_skips_non_github(self):
        self.add_repo("https://gitlab.com/o/r", meta={"name": "gitlab-o/r"},
                      card={"domain": "x", "source": "heuristic"})
        res = server.check_repo_updates(
            fetch_fn=lambda p: {"name": p, "stars": 99}, force=True)
        self.assertEqual(res["checked"], 0)

    def test_interval_throttle_skips_recent(self):
        rid = self._seed({"name": "o/r", "stars": 10})
        conn = db.connect()
        conn.execute("UPDATE repos SET last_checked_at=? WHERE id=?", (server.now(), rid))
        conn.commit()
        conn.close()
        res = server.check_repo_updates(
            fetch_fn=lambda p: {"name": p, "stars": 20}, force=False)
        self.assertEqual(res["checked"], 0)

    def test_mark_seen(self):
        rid = self._seed({"name": "o/r", "stars": 10})
        server.check_repo_updates(fetch_fn=lambda p: {"name": p, "stars": 20}, force=True)
        self.assertEqual(server.get_updates()["count"], 1)
        self.assertEqual(server.mark_updates_seen(), 1)
        self.assertEqual(server.get_updates()["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
