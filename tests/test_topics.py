"""主题归纳（集合级结构）测试。

锁住四类容易被后续改动弄坏的行为：
  1. 条目太少时**不调用模型** —— 用两三条素材编出来的"主题"只是领域标签的同义词，
     还会给人「已分析过」的错觉；
  2. 模型可能**编出不存在的条目编号** —— 必须剔除，成员全非法的主题必须丢弃；
  3. 重算只替换 draft，**已确认的主题不被一次重算抹掉**；
  4. 库变化后必须标 `stale`，而不是继续展示过期结构。
"""

import unittest

from common import Base  # 先导入它：sys.path 引导在 common 里

import analyze  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import topics  # noqa: E402


class TestTopicInduction(Base):
    def setUp(self):
        super().setUp()
        # 归纳直接调 _llm_chat 拿原文（便于区分"截断"与"模型不可用"），所以冒充这一层。
        self._orig_chat = analyze._llm_chat
        self.calls = []

    def tearDown(self):
        analyze._llm_chat = self._orig_chat
        super().tearDown()

    # ---- 工具 ----
    def _seed(self, n):
        ids = []
        for i in range(n):
            ids.append(self.add_repo(
                "https://example.com/%d" % i,
                meta={"title": "条目 %d" % i, "description": "说明 %d" % i},
                card={"domain": "AI·LLM", "purpose": "用途 %d" % i, "tags": ["标签%d" % i]}))
        return ids

    def _fake(self, obj):
        """冒充模型返回。obj 传 dict 自动转 JSON，传字符串原样返回（用于测非法输出）。"""
        def _f(*a, **kw):
            self.calls.append(kw)
            if obj is None:
                return None
            if isinstance(obj, str):
                return obj
            import json
            return json.dumps(obj, ensure_ascii=False)
        analyze._llm_chat = _f

    def _rows(self, table):
        conn = db.connect()
        try:
            return conn.execute("SELECT * FROM %s" % table).fetchall()
        finally:
            conn.close()

    # ---- 1. 样本太小 ----
    def test_too_few_items_skips_model(self):
        self._seed(config.TOPICS_MIN_ITEMS - 1)
        self._fake({"topics": [{"title": "不该出现", "members": [1]}]})
        res = topics.build_topics()
        self.assertTrue(res["ok"])
        self.assertFalse(res["built"])
        self.assertEqual(res["count"], 0)
        self.assertIn("条目太少", res["note"])
        self.assertEqual(self.calls, [], "条目不足时不应调用模型")
        self.assertEqual(self._rows("topics"), [])

    # ---- 2. 正常归纳 ----
    def test_build_persists_topics_with_coverage(self):
        ids = self._seed(5)
        self._fake({"topics": [
            {"title": "投资主线", "summary": "讲 AI 投资逻辑",
             "members": ids[:3], "gaps": ["只有正面案例"]},
            {"title": "工具链", "summary": "讲工程工具",
             "members": ids[3:], "gaps": []},
        ]})
        res = topics.build_topics()
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)

        data = topics.list_topics()
        self.assertFalse(data["stale"])
        self.assertEqual(data["total"], 2)
        first = [t for t in data["topics"] if t["title"] == "投资主线"][0]
        self.assertEqual(first["status"], "draft")
        self.assertEqual(first["count"], 3)
        self.assertEqual(sorted(m["id"] for m in first["items"]), sorted(ids[:3]))
        self.assertEqual(first["gaps"], ["只有正面案例"])
        self.assertEqual(first["kinds"], {"仓库": 3})        # 未指定 kind 的条目按生产端归类
        self.assertEqual(first["domains"], ["AI·LLM"])
        self.assertEqual(len(self._rows("topic_members")), 5)

    def test_digest_labels_item_types(self):
        """给模型看的类型标签要分得清视频/文章/仓库 —— 主题归纳据此判断内容性质。"""
        rid_v = self.add_repo("https://v.douyin.com/a/",
                              meta={"title": "视频条", "platform": "douyin"})
        rid_a = self.add_repo("https://mp.weixin.qq.com/b",
                              meta={"title": "文章条", "platform": "web"})
        rid_r = self.add_repo("https://github.com/x/y", meta={"name": "repo条"})
        conn = db.connect()
        try:
            conn.execute("UPDATE repos SET kind='cognition' WHERE id IN (?,?)", (rid_v, rid_a))
            conn.execute("UPDATE repos SET kind='production' WHERE id=?", (rid_r,))
            conn.commit()
            lines, ids, _ = topics._digest(conn)
        finally:
            conn.close()
        blob = "\n".join(lines)
        self.assertIn("| 视频 |", blob)
        self.assertIn("| 文章 |", blob)
        self.assertIn("| 仓库 |", blob)
        self.assertEqual(sorted(ids), sorted([rid_v, rid_a, rid_r]))

    # ---- 3. 防幻觉：编造的编号 ----
    def test_hallucinated_members_are_dropped(self):
        ids = self._seed(4)
        self._fake({"topics": [
            {"title": "半真半假", "members": [ids[0], 99999, "abc"], "gaps": []},
            {"title": "全是假的", "members": [88888, 77777], "gaps": []},
            {"title": "", "members": [ids[1]], "gaps": []},        # 无标题
        ]})
        res = topics.build_topics()
        self.assertEqual(res["count"], 1, "成员全非法或无标题的主题必须丢弃")
        data = topics.list_topics()
        self.assertEqual(data["total"], 1)
        self.assertEqual([m["id"] for m in data["topics"][0]["items"]], [ids[0]])

    def test_model_unavailable_reports_failure(self):
        self._seed(4)
        self._fake(None)
        res = topics.build_topics()
        self.assertFalse(res["ok"])
        self.assertFalse(res["built"])
        self.assertIn("模型", res["msg"])
        self.assertEqual(self._rows("topics"), [], "失败不应留下半成品主题")

    def test_truncated_json_reports_real_cause(self):
        """输出被截断时必须说"不是合法 JSON + 可能被截断" —— 别把排障引向"模型不可用"。"""
        self._seed(4)
        self._fake('{"topics":[{"title":"被截断的主题","members":[1,2],"gap')
        res = topics.build_topics()
        self.assertFalse(res["ok"])
        self.assertIn("不是合法 JSON", res["msg"])
        self.assertIn("截断", res["msg"])
        self.assertIn("REPO_TOPICS_MAX_TOKENS", res["msg"], "要给出可操作的下一步")

    def test_parser_tolerates_surrounding_prose(self):
        """模型爱在 JSON 前后加一句说明 —— 抓首尾大括号也要能解出来。"""
        ids = self._seed(4)
        body = '{"topics":[{"title":"夹缝主题","members":[%d],"gaps":[]}]}' % ids[0]
        self._fake("好的，以下是归纳结果：\n" + body + "\n希望对你有帮助。")
        res = topics.build_topics()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["count"], 1)

    def test_missing_root_brace_is_repaired(self):
        """模型会随机漏掉最外层的 } —— 只差闭合符时应补全并如实标记 repaired。"""
        ids = self._seed(4)
        # 故意的：结尾只有一个 }（根对象未闭合）
        self._fake('{"topics":[{"title":"漏括号主题","members":[%d],"gaps":[]}]' % ids[0])
        res = topics.build_topics()
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["repaired"], "补全过就该标记，便于观察模型输出质量")
        data = topics.list_topics()
        self.assertEqual(data["topics"][0]["title"], "漏括号主题")

    def test_truncated_inside_string_is_rejected(self):
        """内容被从中间截断（字符串都没闭合）时必须拒绝 —— 补出来的会混入半句话。"""
        self._seed(4)
        self._fake('{"topics":[{"title":"半截主题","members":[1,2],"gaps":["这里被截断')
        res = topics.build_topics()
        self.assertFalse(res["ok"], "半句话不该被当成正常内容收下")
        self.assertIn("不是合法 JSON", res["msg"])
        self.assertEqual(self._rows("topics"), [])

    # ---- 4. 重算与确认 ----
    def test_rebuild_replaces_draft_but_keeps_confirmed(self):
        ids = self._seed(4)
        self._fake({"topics": [{"title": "旧主题", "members": ids, "gaps": []}]})
        topics.build_topics()
        old = topics.list_topics()["topics"][0]
        self.assertTrue(topics.set_status(old["id"], "confirmed")["ok"])

        self._fake({"topics": [{"title": "新主题", "members": ids[:2], "gaps": []}]})
        topics.build_topics()

        data = topics.list_topics()
        titles = [t["title"] for t in data["topics"]]
        self.assertIn("旧主题", titles, "已确认的主题不该被重算抹掉")
        self.assertIn("新主题", titles)
        confirmed = [t for t in data["topics"] if t["title"] == "旧主题"][0]
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(data["topics"][0]["title"], "旧主题", "已确认的排在最前")
        drafts = [t for t in data["topics"] if t["status"] == "draft"]
        self.assertEqual(len(drafts), 1, "旧的 draft 应被替换，不堆积")

    def test_set_status_rejects_unknown_value(self):
        ids = self._seed(4)
        self._fake({"topics": [{"title": "T", "members": ids, "gaps": []}]})
        topics.build_topics()
        tid = topics.list_topics()["topics"][0]["id"]
        self.assertFalse(topics.set_status(tid, "archived")["ok"])
        self.assertTrue(topics.set_status(tid, "draft")["ok"])

    # ---- 5. 过期与成员失效 ----
    def test_stale_after_library_changes(self):
        ids = self._seed(4)
        self._fake({"topics": [{"title": "T", "members": ids, "gaps": []}]})
        topics.build_topics()
        self.assertFalse(topics.list_topics()["stale"])
        self.add_repo("https://example.com/new", meta={"title": "新条目"},
                      card={"domain": "X"})
        self.assertTrue(topics.list_topics()["stale"], "库变了主题就该标过期")

    def test_topic_hidden_when_members_gone(self):
        ids = self._seed(4)
        self._fake({"topics": [{"title": "T", "members": ids, "gaps": []}]})
        topics.build_topics()
        conn = db.connect()
        conn.execute("DELETE FROM repos")
        conn.commit()
        conn.close()
        data = topics.list_topics()
        self.assertEqual(data["topics"], [], "成员条目都没了就不该展示空壳主题")

    # ---- 6. 输入清单 ----
    def test_digest_marks_truncation_and_carries_key_fields(self):
        old = config.TOPICS_MAX_ITEMS
        config.TOPICS_MAX_ITEMS = 3
        try:
            self._seed(5)
            conn = db.connect()
            try:
                lines, ids, total = topics._digest(conn)
            finally:
                conn.close()
            self.assertEqual(len(lines), 3)
            self.assertEqual(total, 5, "总量要如实带回，便于显式说明截断了多少")
            self.assertIn("#", lines[0])
            self.assertIn("AI·LLM", lines[0])
            self.assertIn("用途", lines[0])
        finally:
            config.TOPICS_MAX_ITEMS = old


if __name__ == "__main__":
    unittest.main()
