"""收藏箱本地只读 MCP Server（stdio / JSON-RPC）。

运行：.venv/Scripts/python.exe src/mcp_server.py
仅提供收藏搜索、单条收藏读取和统计工具；不暴露 app_settings，也不写数据库。
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import config
import server as app_server

PROTOCOL_VERSION = "2025-11-25"
SERVER_INFO = {"name": "intelligent-toolbox", "version": "1.0.0"}

TOOLS = [
    {
        "name": "search_saved_items",
        "description": "在本地收藏箱内全文搜索仓库、网页正文、逐字稿、备注和结构化笔记。返回相关收藏及命中摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词，可中文或英文；至少给出有区分度的关键词。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_saved_item",
        "description": "按收藏条目 ID 读取本地收藏的完整详情，包括元数据、分析卡片、备注与原始正文/逐字稿。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "收藏条目 ID。"}},
            "required": ["id"],
        },
    },
    {
        "name": "get_collection_stats",
        "description": "获取收藏箱的条目总数、状态与来源类别分布，不包含任何密钥或应用设置。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _readonly_connect():
    """以 SQLite mode=ro 打开当前库；MCP 工具始终只读。"""
    uri = Path(config.DB).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _decode(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def _tool_search(args):
    query = str(args.get("query") or "").strip()[:200]
    if not query:
        raise ValueError("query 不能为空")
    try:
        limit = int(args.get("limit", 8))
    except (TypeError, ValueError):
        raise ValueError("limit 必须是整数")
    limit = max(1, min(limit, 20))
    conn = _readonly_connect()
    try:
        has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='repo_fts'").fetchone()
        app_server.SEARCH_FTS_ENABLED = bool(has_fts)
        items = app_server.search_repos(query, limit=limit, conn=conn)
        out = []
        for item in items:
            meta, card = item.get("meta") or {}, item.get("card") or {}
            out.append({
                "id": item["id"], "title": meta.get("name") or card.get("title") or item["url"],
                "url": item["url"], "created_at": item.get("created_at"),
                "note": item.get("note") or "", "excerpt": item.get("excerpt") or "",
                "status": item.get("status"),
            })
        return {"query": query, "count": len(out), "items": out}
    finally:
        conn.close()


def _tool_get_item(args):
    try:
        item_id = int(args.get("id"))
    except (TypeError, ValueError):
        raise ValueError("id 必须是整数")
    conn = _readonly_connect()
    try:
        row = conn.execute(
            "SELECT id,url,note,status,meta,card,created_at,kind,raw,error FROM repos WHERE id=?",
            (item_id,),
        ).fetchone()
        if not row:
            raise ValueError("找不到 ID=%d 的收藏" % item_id)
        result = {
            "id": row[0], "url": row[1], "note": row[2] or "", "status": row[3],
            "meta": _decode(row[4]), "card": _decode(row[5]), "created_at": row[6],
            "kind": row[7] or "production", "raw": (row[8] or "")[:30000],
            "error": row[9],
        }
        if row[8] and len(row[8]) > 30000:
            result["raw_truncated"] = True
        return result
    finally:
        conn.close()


def _tool_stats():
    conn = _readonly_connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        status = {str(k or "unknown"): n for k, n in conn.execute(
            "SELECT status,COUNT(*) FROM repos GROUP BY status")}
        kinds = {str(k or "unknown"): n for k, n in conn.execute(
            "SELECT kind,COUNT(*) FROM repos GROUP BY kind")}
        return {"total": total, "by_status": status, "by_kind": kinds}
    finally:
        conn.close()


def call_tool(name, arguments):
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "search_saved_items":
        return _tool_search(arguments)
    if name == "get_saved_item":
        return _tool_get_item(arguments)
    if name == "get_collection_stats":
        return _tool_stats()
    raise ValueError("未知工具：" + str(name))


def dispatch(message):
    """处理一条 MCP JSON-RPC 消息，返回响应对象或 None（通知）。"""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if method == "notifications/initialized" or (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "本地只读收藏库。先用 search_saved_items 检索，再按 ID 用 get_saved_item 读取详情。",
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            result = call_tool(params.get("name"), params.get("arguments"))
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result,
                "isError": False,
            }}
        except Exception as exc:
            result = {"error": str(exc)}
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result,
                "isError": True,
            }}
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": "Method not found: " + str(method)}}


def main():
    """MCP stdio transport：每行一条 JSON-RPC 消息，stdout 只输出协议响应。"""
    for line in sys.stdin:
        if len(line) > 1_000_000:
            continue
        try:
            message = json.loads(line)
            response = dispatch(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            print("MCP request failed: %s" % exc, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
