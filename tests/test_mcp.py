"""MCP stdio/只读收藏工具的离线测试。"""

import sqlite3

from common import Base, db
import mcp_server


class TestMcp(Base):
    def test_initialize_and_tools_list(self):
        current = mcp_server.dispatch({
            "jsonrpc": "2.0", "id": 10, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        })
        self.assertEqual(current["result"]["protocolVersion"], "2025-11-25")
        init = mcp_server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        })
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", init["result"]["capabilities"])
        listed = mcp_server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({t["name"] for t in listed["result"]["tools"]}, {
            "search_saved_items", "get_saved_item", "get_collection_stats"})

    def test_search_and_read_tools_are_readonly(self):
        rid = self.add_repo("https://example.com/local-first", note="本地优先知识库")
        conn = db.connect()
        conn.execute("UPDATE repos SET raw='本地优先软件支持 CRDT 同步' WHERE id=?", (rid,))
        conn.commit(); conn.close()
        result = mcp_server.call_tool("search_saved_items", {"query": "CRDT"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["id"], rid)
        item = mcp_server.call_tool("get_saved_item", {"id": rid})
        self.assertIn("CRDT", item["raw"])
        ro = mcp_server._readonly_connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("DELETE FROM repos WHERE id=?", (rid,))
        finally:
            ro.close()

    def test_tool_errors_are_reported_without_jsonrpc_crash(self):
        response = mcp_server.dispatch({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_saved_item", "arguments": {"id": 99999}},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertIn("找不到", response["result"]["structuredContent"]["error"])

    def test_stats_excludes_settings(self):
        self.add_repo("https://github.com/a/b")
        stats = mcp_server.call_tool("get_collection_stats", {})
        self.assertEqual(stats["total"], 1)
        self.assertNotIn("settings", stats)
