"""内置 HTTP LLM 客户端（OpenAI 兼容 /v1/chat/completions）。

为了让收藏箱成为独立 app，模型能力不应再依赖外部脚本/环境变量。
配置存在本机 app_settings 表（由设置页写入）：
  llm_enable   → "1"/"0" 启用开关
  llm_base_url → 如 https://api.openai.com/v1
  llm_api_key  → Bearer 鉴权；留空表示本地无鉴权服务也可调用
  llm_model    → 模型名

实现只依赖标准库 urllib + json，守零依赖铁律。任何一步失败都返回 None，
由调用方优雅降级（规则卡片），绝不抛冒烟。
"""

import json
import urllib.request

import db


def _cfg(conn):
    return {
        "enable": db.get_settings(conn, "llm_enable", "") == "1",
        "base_url": (db.get_settings(conn, "llm_base_url") or "").strip().rstrip("/"),
        "api_key": (db.get_settings(conn, "llm_api_key") or "").strip(),
        "model": (db.get_settings(conn, "llm_model") or "").strip(),
    }


def configured(conn=None):
    """是否已启用且填齐 base_url + model。api_key 可空（本地无鉴权服务）。"""
    own = conn is None
    if own:
        conn = db.connect()
    try:
        c = _cfg(conn)
        return bool(c["enable"] and c["base_url"] and c["model"])
    finally:
        if own:
            conn.close()


def chat(system, user, conn=None, timeout=None, max_tokens=None):
    """单次 chat 调用。返回 content 字符串，或 None（未配置/请求失败/空输出）。"""
    own = conn is None
    if own:
        conn = db.connect()
    try:
        c = _cfg(conn)
        if not (c["enable"] and c["base_url"] and c["model"]):
            return None
        url = c["base_url"] + (
            "" if c["base_url"].endswith("/chat/completions") else "/chat/completions")
        payload = {
            "model": c["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if c["api_key"]:
            req.add_header("Authorization", "Bearer " + c["api_key"])
        with urllib.request.urlopen(req, timeout=timeout or 30) as r:
            data = json.loads(r.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        content = (msg.get("content") or "").strip()
        return content or None
    except Exception:
        return None
    finally:
        if own:
            conn.close()