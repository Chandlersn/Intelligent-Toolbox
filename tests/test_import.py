"""批量导入（冷启动）与 GitHub token 配置的回归测试。

覆盖两块新能力：
- extract_repo_urls / import_bookmarks：书签 HTML、粘贴文本 → 只抽 GitHub/GitLab
  仓库地址（普通网页不收）；导入幂等（重复导入全 dup）。
- settings 的 github 段：token 写入 → gh_token() 生效 → 掩码回显不泄明文；
  LLM 单独保存不得误清 github token（回归：set_settings 无条件覆盖的坑）。
"""

import json
import unittest

from common import Base, config, db, server


# ═══════════════════════════════════════════════════════════════════
#  1. 地址抽取
# ═══════════════════════════════════════════════════════════════════
class TestExtractRepoUrls(Base):
    def test_plain_lines(self):
        text = "https://github.com/a/b\nhttps://gitlab.com/c/d"
        self.assertEqual(server.extract_repo_urls(text),
                         ["https://github.com/a/b", "https://gitlab.com/c/d"])

    def test_bookmark_html(self):
        # Netscape 书签格式：href 带查询串/锚点，后随中文说明
        html = ('<DL><p><DT><A HREF="https://github.com/pallets/flask?tab=stars">flask</A>'
                '<DT><A HREF="https://github.com/pallets/click/tree/main/src">click</A>'
                '<DT><A HREF="https://www.baidu.com/s?wd=xx">百度</A></DL><p>')
        self.assertEqual(server.extract_repo_urls(html),
                         ["https://github.com/pallets/flask", "https://github.com/pallets/click"])

    def test_rejects_github_site_pages(self):
        # GitHub 站内非仓库页（两段路径但 owner 是保留字）不能混进来
        text = "https://github.com/topics/llm\nhttps://github.com/explore"
        self.assertEqual(server.extract_repo_urls(text), [])

    def test_rejects_non_repo_hosts(self):
        text = "https://example.com/a/b\nhttps://bitbucket.org/x/y"
        self.assertEqual(server.extract_repo_urls(text), [])

    def test_dedupe_and_git_suffix(self):
        text = ("https://github.com/a/b.git\nhttps://github.com/a/b/\n"
                "https://github.com/a/b#readme")
        self.assertEqual(server.extract_repo_urls(text), ["https://github.com/a/b"])

    def test_cap_limit(self):
        text = "\n".join("https://github.com/own%d/repo%d" % (i, i) for i in range(500))
        self.assertEqual(len(server.extract_repo_urls(text)), server.IMPORT_MAX)


# ═══════════════════════════════════════════════════════════════════
#  2. 导入契约：同步落库、幂等、失败不拖垮整批
# ═══════════════════════════════════════════════════════════════════
class TestImportBookmarks(Base):
    def test_import_inserts_and_reports(self):
        code, body = server.import_bookmarks(
            {"text": "https://github.com/a/b\nhttps://github.com/c/d"})
        self.assertEqual(code, 200)
        self.assertEqual((body["found"], body["new"], body["dup"], body["failed"]), (2, 2, 0, 0))
        # 落库即返回（分析异步）：状态应为 queued
        conn = db.connect()
        rows = conn.execute("SELECT url,status,source FROM repos ORDER BY id").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows], ["https://github.com/a/b", "https://github.com/c/d"])
        self.assertTrue(all(r[1] == "queued" for r in rows))
        self.assertTrue(all(r[2] == "import" for r in rows))

    def test_reimport_all_dup(self):
        text = {"text": "https://github.com/a/b"}
        server.import_bookmarks(dict(text))
        code, body = server.import_bookmarks(dict(text))
        self.assertEqual(code, 200)
        self.assertEqual((body["new"], body["dup"]), (0, 1))

    def test_empty_and_no_repo(self):
        self.assertEqual(server.import_bookmarks({"text": "  "})[0], 400)
        code, body = server.import_bookmarks({"text": "https://example.com/x/y"})
        self.assertEqual(code, 400)
        self.assertIn("未识别", body["error"])

    def test_normalizes_deep_paths(self):
        code, _ = server.import_bookmarks({"text": "https://github.com/a/b/tree/main/src"})
        self.assertEqual(code, 200)
        conn = db.connect()
        urls = [r[0] for r in conn.execute("SELECT url FROM repos").fetchall()]
        conn.close()
        self.assertEqual(urls, ["https://github.com/a/b"])


# ═══════════════════════════════════════════════════════════════════
#  3. GitHub token：应用内配置生效、掩码回显、不误清
# ═══════════════════════════════════════════════════════════════════
class TestGithubTokenSettings(Base):
    def test_save_and_effective(self):
        server.save_settings({"github": {"token": "ghp_" + "x" * 30}})
        self.assertTrue(server.gh_token().startswith("ghp_"))
        resp = server.get_settings_resp()
        g = resp["github"]
        self.assertTrue(g["has_token"])
        self.assertEqual(g["source"], "settings")
        self.assertNotIn("x" * 30, g["token"])  # 明文不下发
        self.assertIn("***", g["token"])

    def test_llm_save_must_not_clear_github_token(self):
        # 回归：保存模型设置（无 github 键）不得清掉已存 token
        server.save_settings({"github": {"token": "ghp_" + "y" * 30}})
        server.save_settings({"llm": {"enable": True, "base_url": "", "api_key": "", "model": "m"}})
        self.assertTrue(server.gh_token().startswith("ghp_"))

    def test_clear_falls_back_to_env(self):
        old = config.GITHUB_TOKEN
        config.GITHUB_TOKEN = "env_token_123"
        try:
            server.save_settings({"github": {"token": "ghp_" + "z" * 30}})
            server.save_settings({"github": {"token": ""}})  # 应用内清除
            self.assertEqual(server.gh_token(), "env_token_123")
        finally:
            config.GITHUB_TOKEN = old

    def test_masked_roundtrip_keeps_token(self):
        server.save_settings({"github": {"token": "ghp_" + "w" * 30}})
        masked = server.get_settings_resp()["github"]["token"]
        server.save_settings({"github": {"token": masked}})  # 前端原样回传掩码
        self.assertTrue(server.gh_token().startswith("ghp_"))  # 不得被掩码覆盖


if __name__ == "__main__":
    unittest.main()
