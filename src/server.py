"""收藏箱 · HTTP 服务层。

结构约定（改造后）：
  路由/传输  ── Handler 只做「解析请求 → 调业务函数 → 写响应」
  业务逻辑  ── 模块级函数（collect_payload / regenerate_card / get_cards / ...）
  存储      ── 统一走 db.connect()，不再各函数自己 sqlite3.connect
  配置      ── 统一走 config，端口/路径/口令不再写死在源码里

为什么把业务从 Handler 里抽出来：这些逻辑要能被测试直接调用。
特别是 regenerate_card —— 「重算之后轴表必须与卡片一致」是一条核心回归，
埋在处理器的 do_POST 里就永远测不到（P0-1 就是这么漏过去的）。

一致性契约（本项目最重要的一条）：
  repos.card 是唯一真源；repo_axis / tags 是可随时重建的派生物。
  任何改写 card 的路径都必须紧跟一次 sync_axes()，否则四类视图会各说各话。
"""

import datetime
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote, parse_qs, unquote

import analyze
import config
import db
import douyin_source
import web_source

# 兼容旧引用（migrate/外部脚本曾 import server.DB / server.PORT）
ROOT = config.ROOT
WEB_DIR = config.WEB_DIR
DB = config.DB
PORT = config.PORT

# 收藏来源（谁把它放进来的）。推荐页必须带上，画像才知道「有多少是别人喂的」。
SOURCES = {"manual", "recommend", "trend", "share", "bookmark", "ball", "import"}

# 「后来怎么样了」：用过 / 弃了 / 还想用。
# 为什么不复用卡片的成熟度：那是按 stars 分桶，讲的是项目的江湖地位，不是你用得多不多。
# 这是整个画像里唯一不靠推测、纯靠你动手反馈的一层。
FEEDBACK = {"used": "用过", "dropped": "弃了", "want": "还想用"}

CTYPES = {
    ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
    ".json": "application/json", ".svg": "image/svg+xml", ".txt": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".ico": "image/x-icon", ".webmanifest": "application/manifest+json",
}

# charset 只对文本类类型有意义。给图片/字体/二进制挂 charset 虽无害但不规范，
# 所以这里显式区分：文本类 + 结构化文本 + SVG(XML) 才附加。
_CHARSET_CTYPES = {
    "application/javascript", "application/json",
    "application/manifest+json", "image/svg+xml",
}


def with_charset(ctype):
    """文本类补 charset=utf-8；二进制/图片原样返回。"""
    if ctype.startswith("text/") or ctype in _CHARSET_CTYPES:
        return ctype + "; charset=utf-8"
    return ctype


def init_db():
    """建表 + 版本化迁移。幂等，每次启动都跑。"""
    conn = db.connect()
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            note TEXT,
            status TEXT DEFAULT 'queued',
            meta TEXT,
            created_at TEXT
        )"""
    )
    # 增量列：走 ensure_column（读 table_info 判断），不再用「ALTER 失败就 pass」猜
    db.ensure_column(conn, "repos", "card", "TEXT")
    db.ensure_column(conn, "repos", "source", "TEXT DEFAULT 'manual'")
    db.ensure_column(conn, "repos", "feedback", "TEXT")
    db.ensure_column(conn, "repos", "last_checked_at", "TEXT")
    # 多源：kind 区分生产端（GitHub/GitLab 仓库）与认知端（抖音/文章等）；
    # raw 存放原文（认知端的逐字稿），与 card（萃取）双保留。
    db.ensure_column(conn, "repos", "kind", "TEXT DEFAULT 'production'")
    db.ensure_column(conn, "repos", "raw", "TEXT")
    # 解析失败原因（抖音/网页解析异常时记录，便于前端直接展示，不再「永久卡 queued」）
    db.ensure_column(conn, "repos", "error", "TEXT")
    # 仓库更新事件：本地比对新旧 GitHub 元数据后落下的变化，供前端做「反馈消息」。
    # seen=0 表示用户还没看；反复检查不会重复刷屏（同仓同类型未读事件只保留最新一条）。
    c.execute(
        """CREATE TABLE IF NOT EXISTS repo_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            old_val TEXT,
            new_val TEXT,
            summary TEXT,
            created_at TEXT,
            seen INTEGER DEFAULT 0
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_repo_events_repo ON repo_events (repo_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_repo_events_seen ON repo_events (seen)")
    # 多轴数据模型（多维同步存储与检索的根）
    c.execute("CREATE TABLE IF NOT EXISTS axes (key TEXT PRIMARY KEY, name TEXT NOT NULL)")
    c.execute(
        """CREATE TABLE IF NOT EXISTS repo_axis (
            repo_id INTEGER NOT NULL,
            axis_key TEXT NOT NULL,
            value TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            source TEXT DEFAULT 'heuristic',
            confidence REAL DEFAULT 0.6,
            PRIMARY KEY (repo_id, axis_key, value)
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS tags (
            repo_id INTEGER NOT NULL, tag TEXT NOT NULL,
            PRIMARY KEY (repo_id, tag)
        )"""
    )
    # 应用内可配置项（模型/外观等），不靠环境变量，随库持久化。
    c.execute(
        """CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT
        )"""
    )
    # 多轴查询是核心路径，按轴取值建索引（否则每次筛选都全表扫）
    c.execute("CREATE INDEX IF NOT EXISTS idx_repo_axis_key_value ON repo_axis (axis_key, value)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_repo_axis_repo ON repo_axis (repo_id)")
    for k, n in (
        ("domain", "领域"), ("tech", "技术栈"), ("scene", "使用场景"),
        ("gap", "认知缺角"), ("motive", "收藏动机"), ("timeline", "时间线"),
        ("use", "使用结果"),
    ):
        c.execute("INSERT OR IGNORE INTO axes (key, name) VALUES (?, ?)", (k, n))
    db.set_schema_version(conn, db.SCHEMA_VERSION)
    conn.commit()
    conn.close()


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ═══════════════════════════════════════════════════════════════════
#  缺角推断：种子先验 + 真实行为
# ═══════════════════════════════════════════════════════════════════
# SEED_GAP_COOCCUR 只是「冷启动种子」——样本不足时的兜底先验，不是行为统计。
# 改造前它叫 GAP_COOCCUR，被当成结论输出，于是「镜子」照出来的其实是编者的预判。
# 现在它降级为 prior，并且在返回结果里带 source="seed"，如实标注。
SEED_GAP_COOCCUR = {
    "Web 框架": ["前端/UI", "DevOps/云"],
    "前端/UI": ["知识/笔记", "Web 框架"],
    "AI·LLM": ["数据库", "知识/笔记", "数据/可视化"],
    "数据库": ["DevOps/云", "AI·LLM"],
    "CLI/工具": ["DevOps/云", "知识/笔记"],
    "DevOps/云": ["数据库", "安全"],
    "移动端": ["前端/UI", "知识/笔记"],
    "安全": ["DevOps/云", "Web 框架"],
    "数据/可视化": ["AI·LLM", "知识/笔记"],
    "知识/笔记": ["前端/UI", "数据/可视化"],
}


def domain_adjacency(conn):
    """领域邻接：基于你真实收藏算出来的「领域之间靠什么连上」。

    做法：两个领域共享多少种技术栈取值（tech 轴）。这是能真正从行为数据里
    算出来的部分 —— 缺角指向「还没有的领域」，天生算不出来（没有数据），
    但「已有领域之间的相邻关系」可以，而且比写死的共现表诚实。

    返回 [{from, to, shared, samples}]，shared = 共享技术栈个数。
    """
    c = conn.cursor()
    c.execute("SELECT repo_id, axis_key, value FROM repo_axis WHERE axis_key IN ('domain','tech')")
    dom_of, techs_of = {}, {}
    for rid, ak, val in c.fetchall():
        if ak == "domain":
            dom_of[rid] = val
        else:
            techs_of.setdefault(rid, set()).add((val or "").lower())
    dom_tech = {}
    for rid, dom in dom_of.items():
        dom_tech.setdefault(dom, set()).update(techs_of.get(rid) or set())
    out = []
    doms = sorted(dom_tech)
    for i, a in enumerate(doms):
        for b in doms[i + 1:]:
            shared = dom_tech[a] & dom_tech[b]
            if not shared:
                continue
            out.append({
                "from": a, "to": b, "shared": len(shared),
                "samples": sorted(shared)[:3],
            })
    out.sort(key=lambda x: -x["shared"])
    return out


# ---------- 热点接口 TTL 缓存 ----------
# 之前计划用 SQLite 内建 PRAGMA data_version 作失效信号，但实测在本环境
# （内置 sqlite3）对 DML(INSERT/UPDATE/DELETE) commit 后并不递增，不可依赖。
# 退而用「库路径 key + 短 TTL」：切库即失效（保证测试/换库隔离），同库在
# TTL 内复用，写入后至多 stale ttl 秒。前端 8s 轮询 + 手动刷新下无感。
def _cached(slot, build_fn, ttl):
    """进程内 TTL 缓存。key 含库路径，切库重建；同库 TTL 窗口内复用。"""
    now = time.time()
    if (slot["key"] == config.DB and slot["data"] is not None
            and now - slot["ts"] < ttl):
        return slot["data"]
    data = build_fn()
    slot.update(key=config.DB, ts=now, data=data)
    return data


def _build_graph_uncached(sample_warn=True):
    """聚合多轴数据 → 图谱结构。优先读 repo_axis，轴缺失时回退 card。"""
    conn = db.connect()
    c = conn.cursor()
    c.execute("SELECT id,url,meta,card,kind FROM repos WHERE card IS NOT NULL")
    rows = c.fetchall()
    c.execute("SELECT repo_id, axis_key, value, confidence FROM repo_axis")
    axis_rows = c.fetchall()
    links = domain_adjacency(conn)
    conn.close()

    axes_by_repo = {}
    for rid, ak, val, conf in axis_rows:
        axes_by_repo.setdefault(rid, []).append(
            {"axis": ak, "value": val, "confidence": conf}
        )

    nodes, edges, domains = [], [], {}
    for rid, url, rmeta_s, rcard_s, rkind in rows:
        try:
            card = json.loads(rcard_s) if rcard_s else {}
            meta = json.loads(rmeta_s) if rmeta_s else {}
        except Exception:
            card, meta = {}, {}
        full = meta.get("name") or url
        name = full.split("/")[-1] if "/" in full else full
        my_axes = axes_by_repo.get(rid, [])
        domain_vals = [a for a in my_axes if a["axis"] == "domain"]
        if domain_vals:
            domain = max(domain_vals, key=lambda a: a["confidence"])["value"]
        else:
            domain = card.get("domain", "未分类")
        try:
            stars = int(meta.get("stars") or 0)
        except Exception:
            stars = 0
        nodes.append({
            "id": rid, "url": url, "name": name, "domain": domain,
            "kind": rkind or "production",
            "axes": my_axes, "tags": card.get("tags", []), "stars": stars,
            "maturity": card.get("maturity", ""),
            "recommendation": card.get("recommendation", 0),
            "purpose": (card.get("purpose") or "")[:140],
            "why_needed": card.get("why_needed", ""),
        })
        d = domains.setdefault(domain, {"name": domain, "count": 0, "repos": [], "stars": 0})
        d["count"] += 1
        d["repos"].append(rid)
        d["stars"] += stars
        for rel in card.get("related_nodes", []):
            edges.append({"source": rid, "target": rel["id"], "reason": rel.get("reason", "")})

    node_ids = {n["id"] for n in nodes}
    seen, uniq = set(), []
    for e in edges:
        if e["source"] not in node_ids or e["target"] not in node_ids:
            continue
        a, b = sorted((e["source"], e["target"]))
        if (a, b) in seen:
            continue
        seen.add((a, b))
        uniq.append(e)

    domain_list = sorted(domains.values(), key=lambda x: -x["count"])

    # 缺角 = 种子先验（明确标注来源，样本稀疏时提示别当真）
    covered = set(domains.keys())
    total = len(nodes)
    gaps = []
    for src_dom, targets in SEED_GAP_COOCCUR.items():
        if src_dom not in covered:
            continue
        for t in targets:
            if t in covered:
                continue
            hint = "你已收藏「%s」，同类收藏常也关注「%s」" % (src_dom, t)
            if sample_warn and total < 5:
                hint += "（当前样本仅 %d 条，先当观察，别当结论）" % total
            gaps.append({"from": src_dom, "to": t, "hint": hint, "source": "seed"})

    return {
        "nodes": nodes,
        "edges": uniq,
        "domains": domain_list,
        "gaps": gaps,
        "domain_links": links,
        "gap_source": "seed",
        "sample": total,
        "total": total,
    }


# 图谱/画像缓存 TTL：取 < 前端 8s 轮询，写入后至多 stale 这个窗口。
# 调用方对返回值只读，勿原地修改（注释约定）。
GRAPH_TTL = 3
_PROFILE_TTL = 3
_GRAPH_CACHE = {"key": None, "ts": 0.0, "data": None}
_PROFILE_CACHE = {"key": None, "ts": 0.0, "data": None}


def get_graph(sample_warn=True):
    """图谱（TTL 缓存包装）。同库短窗内复用结果，减少重复全量计算。"""
    return _cached(_GRAPH_CACHE, lambda: _build_graph_uncached(sample_warn), GRAPH_TTL)


# ═══════════════════════════════════════════════════════════════════
#  推荐：弱协同缺角 → 具体仓库
# ═══════════════════════════════════════════════════════════════════
GAP_SEARCH_KEYWORDS = {
    "前端/UI": "topic:frontend",
    "DevOps/云": "topic:devops",
    "知识/笔记": "topic:note-taking",
    "数据库": "topic:database",
    "安全": "topic:security",
    "数据/可视化": "topic:data-visualization",
    "移动端": "topic:android",
    "Web 框架": "topic:web-framework",
    "AI·LLM": "topic:llm",
    "CLI/工具": "topic:cli",
}

RECOMMEND_CACHE = {}
RECOMMEND_TTL = 3600
# GitHub 限流/失败状态：供 doctor 与前端引导（"限额用尽→去配 token"）。
_RATE_STATE = {"limited": False, "at": None, "remaining": None}


def _fail_cached(domain):
    """GitHub 搜索失败/限流时只把空结果短缓存 ~60s。

    若像成功路径那样缓存 3600s，一次限流会让该领域推荐哑火整整一小时；
    60s 后自动重试，既不每次失败都打接口，也不长期哑火。
    """
    RECOMMEND_CACHE[domain] = (time.time() - (RECOMMEND_TTL - 60), [])


def _github_headers():
    h = {"User-Agent": "repo-collector", "Accept": "application/vnd.github+json"}
    if config.GITHUB_TOKEN:
        h["Authorization"] = "Bearer " + config.GITHUB_TOKEN
    return h


def search_github(domain):
    """按领域关键词搜 GitHub 热门仓库（最多 4 个）。外网失败则降级为空列表。"""
    kw = GAP_SEARCH_KEYWORDS.get(domain)
    if not kw:
        return []
    q = kw + " stars:>500"
    url = "https://api.github.com/search/repositories?q=" + quote(q) + "&sort=stars&order=desc&per_page=6"
    try:
        req = urllib.request.Request(url, headers=_github_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            try:
                remaining = r.headers.get("X-RateLimit-Remaining")
            except Exception:
                remaining = None
            if remaining is not None:
                _RATE_STATE["remaining"] = remaining
            data = json.loads(r.read().decode("utf-8"))
        out = []
        for it in data.get("items", []):
            fn = it.get("full_name") or ""
            if not fn:
                continue
            out.append({
                "full_name": fn,
                "url": it.get("html_url"),
                "description": (it.get("description") or "")[:160],
                "language": it.get("language"),
                "stars": it.get("stargazers_count"),
                "topics": (it.get("topics") or [])[:5],
            })
            if len(out) >= 4:
                break
        _RATE_STATE["limited"] = False
        return out
    except urllib.error.HTTPError as e:
        _fail_cached(domain)
        # 403 + 配额确认为 0 → 真限流，标记状态供前端/doctor 引导去配 token
        if e.code == 403:
            rem = None
            try:
                rem = e.headers.get("X-RateLimit-Remaining")
            except Exception:
                rem = None
            if rem in (None, "0"):
                _RATE_STATE.update({"limited": True, "at": time.time(), "remaining": rem})
        return []
    except Exception:
        _fail_cached(domain)
        return []


def search_github_cached(domain, owned):
    """带 TTL 缓存的搜索；返回时按 owned 实时排除已收藏，避免已收的还被推荐。"""
    t = time.time()
    if domain in RECOMMEND_CACHE:
        ts, repos = RECOMMEND_CACHE[domain]
        if t - ts < RECOMMEND_TTL:
            return [x for x in repos if x["full_name"].split("/")[-1] not in owned]
    repos = search_github(domain)
    RECOMMEND_CACHE[domain] = (t, repos)
    return [x for x in repos if x["full_name"].split("/")[-1] not in owned]


# ===== GitHub 本周涨星 Top 10（独立板块，全局热门，不依赖用户画像）=====
# GitHub 无公开 trending API，解析 github.com/trending?since=weekly（纯标准库）。
# 注意：这是 HTML 抓取，GitHub 改版就会失效 —— 所以失败必须优雅降级为空列表，
# 而不是抛异常或返回半截数据。
TRENDING_CACHE = {"ts": 0, "data": None}
TRENDING_TTL = 3600


def get_trending_weekly():
    """抓 GitHub 本周趋势榜，返回前 10。失败降级为空 + ok=False。"""
    t = time.time()
    if TRENDING_CACHE["data"] is not None and t - TRENDING_CACHE["ts"] < TRENDING_TTL:
        return TRENDING_CACHE["data"]
    try:
        req = urllib.request.Request(
            "https://github.com/trending?since=weekly",
            headers={"User-Agent": "Mozilla/5.0 (repo-collector)"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8")
        arts = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.S)
        out = []
        for a in arts[:10]:
            sp = re.search(r'href="/([^"]+)/stargazers"', a)
            path = sp.group(1) if sp else None
            if not path:
                continue
            stars_m = re.search(r'/stargazers"[^>]*>.*?([\d,]+)\s*</a>', a, re.S)
            wk_m = re.search(r'([\d,]+)\s+stars this week', a)
            lang_m = re.search(r'<span itemprop="programmingLanguage">([^<]+)</span>', a)
            desc_m = re.search(r'<p[^>]*class="col-9[^"]*"[^>]*>(.*?)</p>', a, re.S)
            desc = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
            out.append({
                "full_name": path,
                "url": "https://github.com/" + path,
                "stars": int((stars_m.group(1) or "0").replace(",", "")) if stars_m else 0,
                "weekly_stars": int((wk_m.group(1) or "0").replace(",", "")) if wk_m else 0,
                "language": lang_m.group(1) if lang_m else None,
                "description": desc[:160],
            })
        data = {"ok": True, "source": "github-trending-weekly", "repos": out}
        TRENDING_CACHE["data"] = data
        TRENDING_CACHE["ts"] = t
        return data
    except Exception:
        return {"ok": False, "source": "github-trending-weekly", "repos": []}


def _build_profile_text():
    """拼一段给人/模型读的画像摘要（领域分布、技术栈、成熟度、动机）。不含行为明细泄露。"""
    conn = db.connect()
    c = conn.cursor()
    lines = []
    try:
        c.execute("SELECT value, COUNT(*) FROM repo_axis WHERE axis_key='domain' GROUP BY value ORDER BY COUNT(*) DESC")
        dom = c.fetchall()
        if dom:
            lines.append("已收藏领域：" + "；".join("%s×%d" % (r[0], r[1]) for r in dom))
        c.execute("SELECT DISTINCT value FROM repo_axis WHERE axis_key='tech' ORDER BY value LIMIT 12")
        tech = [r[0] for r in c.fetchall()]
        if tech:
            lines.append("常用技术栈：" + "、".join(tech))
        c.execute("SELECT DISTINCT value FROM repo_axis WHERE axis_key='motive' ORDER BY value LIMIT 8")
        motive = [r[0] for r in c.fetchall()]
        if motive:
            lines.append("收藏动机：" + "、".join(motive))
        c.execute("SELECT COUNT(*) FROM repos")
        lines.append("收藏总数：%d" % c.fetchone()[0])
        # 来源分布：主动搜到的 vs 别人推荐来的，是「按需」还是「被投喂」的硬信号
        c.execute("SELECT COALESCE(source,'manual'), COUNT(*) FROM repos GROUP BY 1 ORDER BY 2 DESC")
        src_rows = c.fetchall()
        if src_rows:
            lines.append("来源分布：" + "、".join("%s×%d" % (r[0], r[1]) for r in src_rows))
    except Exception:
        pass
    conn.close()
    return "\n".join(lines)


def get_recommendations():
    """弱协同缺角 → 具体仓库推荐。

    两层：
    1) 语义层（LLM）：基于用户画像推「还想要哪类 + 为什么 + 置信度」，
       替代写死的共现表；LLM 不可用时回退种子表（并明确标 rule 来源）。
    2) 真实仓库层：每个目标领域仍走 GitHub 搜索，保证跳转地址真实存在。
    每条带 based_on / hint / confidence（信用标注，防样本稀疏误导）。
    """
    g = get_graph()
    covered = {d["name"] for d in g["domains"]}
    owned = {n["name"] for n in g["nodes"]}

    insights = analyze._llm_recommend_insight(_build_profile_text())
    if insights:
        target, meta_map = [], {}
        for ins in insights:
            if ins["domain"] in covered:
                continue
            if ins["domain"] not in target:
                target.append(ins["domain"])
            meta_map[ins["domain"]] = ins
    else:
        # 回退：冷启动种子表。置信度统一标「低(种子)」—— 它本来就是编者先验，不是行为统计。
        target, meta_map = [], {}
        for src, targets in SEED_GAP_COOCCUR.items():
            if src not in covered:
                continue
            for t in targets:
                if t not in covered:
                    meta_map.setdefault(t, {"based_on": [], "reason": "", "confidence": "低(种子)"})
                    meta_map[t]["based_on"].append(src)
                    if t not in target:
                        target.append(t)
        for t in target:
            if not meta_map[t].get("reason"):
                bases = meta_map[t].get("based_on", [])
                meta_map[t]["reason"] = ("你已收藏「" + "、".join(bases) + "」，同类收藏常也关注「" + t + "」") if bases else "通用热门推荐"

    target = target[:4]  # 限速余量
    results = []
    for dom in target:
        repos = search_github_cached(dom, owned)
        if repos:
            m = meta_map.get(dom, {})
            results.append({
                "domain": dom,
                "based_on": m.get("based_on", []),
                "hint": m.get("reason") or "",
                "confidence": m.get("confidence", "低"),
                "source": "llm" if insights else "rule",
                "repos": repos,
            })
    return {"recommendations": results, "total": len(results)}


# ═══════════════════════════════════════════════════════════════════
#  行为画像（行为层，不并入知识库）
# ═══════════════════════════════════════════════════════════════════
def _build_profile_uncached():
    """聚合收藏行为 → 行为层画像。优先读 repo_axis（多轴+置信度），回退 card。"""
    conn = db.connect()
    c = conn.cursor()
    c.execute(
        "SELECT id,url,note,status,meta,card,created_at,COALESCE(source,'manual'),feedback,kind "
        "FROM repos ORDER BY id"
    )
    rows = c.fetchall()
    c.execute("SELECT repo_id, axis_key, value FROM repo_axis")
    axis_rows = c.fetchall()
    gaps = get_graph()["gaps"]
    conn.close()

    axis_dom, axis_tech, axis_motive = {}, {}, {}
    for rid, ak, val in axis_rows:
        if ak == "domain":
            axis_dom[rid] = val
        elif ak == "tech":
            axis_tech.setdefault(rid, []).append(val)
        elif ak == "motive":
            axis_motive[rid] = val

    total = len(rows)
    domains, techs, maturity = {}, {}, {}
    timeline_days, timeline_hours = {}, {}
    notes_with_src = 0
    from_recommend = 0
    sources = {}
    kinds = {}
    carded = 0
    motive_dist = {}
    feedback_dist = {}

    for rid, url, note, status, rmeta_s, rcard_s, created_at, src, fb, rkind in rows:
        try:
            card = json.loads(rcard_s) if rcard_s else None
        except Exception:
            card = None
        if card:
            carded += 1
        dom = axis_dom.get(rid) or (card.get("domain", "未分类") if card else "未分类")
        domains[dom] = domains.get(dom, 0) + 1
        ts = axis_tech.get(rid) or (card.get("tech_stack") or [] if card else [])
        for t in ts:
            techs[t] = techs.get(t, 0) + 1
        mat = (card.get("maturity", "未评估") if card else "未评估")
        maturity[mat] = maturity.get(mat, 0) + 1
        mv = axis_motive.get(rid)
        if mv:
            motive_dist[mv] = motive_dist.get(mv, 0) + 1
        if created_at:
            day = created_at[:10]
            timeline_days[day] = timeline_days.get(day, 0) + 1
            try:
                hh = int(created_at[11:13])
            except Exception:
                hh = -1
            if hh >= 0:
                timeline_hours[hh] = timeline_hours.get(hh, 0) + 1
        if note:
            notes_with_src += 1
        # 来源：读字段，不再靠 grep 备注里的中文（旧实现永远为 0）
        src = src or "manual"
        sources[src] = sources.get(src, 0) + 1
        if src == "recommend":
            from_recommend += 1
        k = rkind or "production"
        kinds[k] = kinds.get(k, 0) + 1
        if fb in FEEDBACK:
            feedback_dist[fb] = feedback_dist.get(fb, 0) + 1

    domain_list = sorted(
        [{"name": k, "count": v} for k, v in domains.items()], key=lambda x: -x["count"])
    # 注意：这里给前端的是完整列表，词数由 len 决定；截断只发生在展示层
    tech_list = sorted(
        [{"name": k, "count": v} for k, v in techs.items()], key=lambda x: -x["count"])
    maturity_list = [{"name": k, "count": v} for k, v in sorted(maturity.items(), key=lambda x: -x[1])]
    day_list = sorted(timeline_days.items())
    slots = {"凌晨 0-6": 0, "上午 6-12": 0, "下午 12-18": 0, "晚上 18-24": 0}
    for h, n in timeline_hours.items():
        if h < 6:
            slots["凌晨 0-6"] += n
        elif h < 12:
            slots["上午 6-12"] += n
        elif h < 18:
            slots["下午 12-18"] += n
        else:
            slots["晚上 18-24"] += n
    span = None
    if day_list:
        try:
            d0 = datetime.date.fromisoformat(day_list[0][0])
            d1 = datetime.date.fromisoformat(day_list[-1][0])
            span = {"first": day_list[0][0], "last": day_list[-1][0],
                    "days": max(1, (d1 - d0).days + 1)}
        except Exception:
            span = None

    return {
        "total": total,
        "carded": carded,
        "domains": domain_list,
        "techs": tech_list,
        "techs_top": tech_list[:12],
        "maturity": maturity_list,
        "motive_dist": motive_dist,
        "sources": sources,
        "feedback": [{"key": k, "name": FEEDBACK[k], "count": v}
                     for k, v in sorted(feedback_dist.items(), key=lambda x: -x[1])],
        "feedback_total": sum(feedback_dist.values()),
        "timeline_days": [{"date": d, "count": n} for d, n in day_list],
        "active_slots": slots,
        "notes_with_src": notes_with_src,
        "from_recommend": from_recommend,
        "kinds": kinds,
        "gaps": gaps,
        "span": span,
    }


def get_profile():
    """行为画像（TTL 缓存包装）。同库短窗内复用，避免每次全量聚合。"""
    return _cached(_PROFILE_CACHE, _build_profile_uncached, _PROFILE_TTL)


# ═══════════════════════════════════════════════════════════════════
#  检索
# ═══════════════════════════════════════════════════════════════════
def query_axes(filters, conn=None):
    """跨轴交叉检索：filters = [("axis_key","value"), ...]，全部 AND。"""
    own = conn is None
    conn = conn or db.connect()
    c = conn.cursor()
    if not filters:
        c.execute("SELECT id FROM repos")
        ids = {r[0] for r in c.fetchall()}
        if own:
            conn.close()
        return ids
    result = None
    for axis_key, value in filters:
        if axis_key == "tech":
            value = analyze.normalize_tech(value)
        c.execute("SELECT repo_id FROM repo_axis WHERE axis_key=? AND value=?", (axis_key, value))
        s = {r[0] for r in c.fetchall()}
        result = s if result is None else (result & s)
    if own:
        conn.close()
    return result if result is not None else set()


def get_cards(filters, conn=None):
    """多维卡片筛选。组合语义：同层 OR，跨层 AND；q 走全文模糊。"""
    own = conn is None
    conn = conn or db.connect()
    c = conn.cursor()
    c.execute("SELECT id,url,note,status,meta,card,created_at,feedback,kind,raw,error "
              "FROM repos ORDER BY id DESC")
    rows = c.fetchall()
    c.execute("SELECT repo_id, axis_key, value FROM repo_axis")
    axis_rows = c.fetchall()
    if own:
        conn.close()

    axes_by_repo = {}
    for rid, ak, val in axis_rows:
        axes_by_repo.setdefault(rid, {}).setdefault(ak, []).append(val)

    def axis_value_filter(axis_key, value):
        """tech 轴的取值写入前被归一化过（小写/分隔符/别名），筛选时必须走同一套，
        否则用户手敲 "Python" 匹配不到轴里的 "python"。其它轴原样比较 ——
        领域名含中文与「·」，不该被改写。"""
        return analyze.normalize_tech(value) if axis_key == "tech" else value

    def match_layer(rid, axis_key, wanted):
        if not wanted:
            return True
        have = axes_by_repo.get(rid, {}).get(axis_key, [])
        want = [axis_value_filter(axis_key, w) for w in wanted]
        return any(w in have for w in want)

    def match_q(it, q):
        q = q.lower()
        hay = [
            it["url"], it["note"] or "",
            (it["meta"] or {}).get("name") or "",
            (it["meta"] or {}).get("description") or "",
            (it["card"] or {}).get("domain") or "",
            (it["card"] or {}).get("why_needed") or "",
        ]
        if it["card"] and it["card"].get("tags"):
            hay += it["card"]["tags"]
        return q in " ".join(hay).lower()

    items = []
    for r in rows:
        try:
            meta = json.loads(r[4]) if r[4] else None
            card = json.loads(r[5]) if r[5] else None
        except Exception:
            meta, card = None, None
        it = {"id": r[0], "url": r[1], "note": r[2], "status": r[3],
              "meta": meta, "card": card, "created_at": r[6], "feedback": r[7],
              "kind": r[8] or "production", "raw": r[9], "error": r[10]}
        if not match_layer(it["id"], "domain", filters.get("domain", [])):
            continue
        if not match_layer(it["id"], "tech", filters.get("tech", [])):
            continue
        if not match_layer(it["id"], "motive", filters.get("motive", [])):
            continue
        if not match_layer(it["id"], "use", filters.get("use", [])):
            continue
        # 类型筛选：production 生产端（仓库）/ cognition 认知端（抖音等）
        kinds = filters.get("kind", [])
        if kinds and (it["kind"] or "production") not in kinds:
            continue
        if filters.get("q") and not match_q(it, filters["q"]):
            continue
        items.append(it)

    sort = filters.get("sort", "")
    if sort == "stars":
        def stars_of(it):
            try:
                return int((it["meta"] or {}).get("stars") or 0)
            except Exception:
                return 0
        items.sort(key=stars_of, reverse=True)
    elif sort == "rec":
        def rec_of(it):
            try:
                return int((it["card"] or {}).get("recommendation") or 0)
            except Exception:
                return 0
        items.sort(key=rec_of, reverse=True)
    return items


def get_axis_facets():
    """列出各轴的取值分布（供前端做分面筛选）。带 source / confidence 聚合。"""
    conn = db.connect()
    c = conn.cursor()
    c.execute(
        "SELECT axis_key, value, COUNT(*) AS n, AVG(confidence) AS conf "
        "FROM repo_axis GROUP BY axis_key, value ORDER BY axis_key, n DESC"
    )
    rows = c.fetchall()
    conn.close()
    facets = {}
    for axis_key, value, n, conf in rows:
        facets.setdefault(axis_key, []).append(
            {"value": value, "count": n, "confidence": round(conf or 0, 2)})
    return facets


# ═══════════════════════════════════════════════════════════════════
#  采集与写入
# ═══════════════════════════════════════════════════════════════════
def valid_repo_url(url):
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.netloc or "").lower().split(":")[0]
    if host == "github.com":
        segs = [s for s in p.path.split("/") if s]
        if len(segs) >= 2:
            return ("github", "/" + "/".join(segs[:2]))
    if host.endswith("gitlab.com"):
        segs = [s for s in p.path.split("/") if s]
        if len(segs) >= 2:
            return ("gitlab", "/" + "/".join(segs[:2]))
    return None


def classify_source(url):
    """统一来源分类：返回 (kind, path)。

    kind ∈ {"github", "gitlab", "douyin"}；path 仅生产端（GitHub owner/repo）有意义，
    认知端（抖音）为 None。无法识别返回 None。

    多源融合的入口判别：生产端（GitHub/GitLab 仓库）与认知端（抖音视频）走不同
    元数据/分析管线，但共享同一套「卡片-轴表」存储与图谱碰撞逻辑。
    """
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = (p.netloc or "").lower().split(":")[0]
    if host == "github.com":
        segs = [s for s in p.path.split("/") if s]
        if len(segs) >= 2:
            return ("github", "/" + "/".join(segs[:2]))
    if host.endswith("gitlab.com"):
        segs = [s for s in p.path.split("/") if s]
        if len(segs) >= 2:
            return ("gitlab", "/" + "/".join(segs[:2]))
    if douyin_source.is_douyin(url):
        return ("douyin", None)
    # 其它 http(s) 链接：当作网页 / 文章（认知端）收录，与生产端在共同领域轴碰撞
    if p.scheme in ("http", "https"):
        return ("web", url)
    return None


def esc_html(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


# URL 候选：只吃 ASCII 可见字符。
# 为什么不能写成 [^\s"'<>]+：中文写作里链接后面常常直接跟汉字（「推荐https://github.com/a/b很好用」），
# 非空白类会把汉字一并吞进 URL，存进去就是个坏地址 —— 而「从分享的文字里抽链接」
# 恰恰是本产品最主要的入口场景。限定 ASCII 后，汉字天然成为终止符。
REPO_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def extract_repo_url(text):
    """从一段文字里抽第一个合法收藏地址（GitHub/GitLab/Douyin，微信转发 / 系统分享场景）。"""
    if not text:
        return None
    for m in REPO_URL_RE.findall(text):
        u = m.rstrip(r""".,;:!?)'\]}>。，；：、""")
        if classify_source(u):
            return u
    return None


def fetch_github_meta(path):
    api = "https://api.github.com/repos/" + path.strip("/")
    try:
        req = urllib.request.Request(api, headers=_github_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {
            "name": data.get("full_name"),
            "description": data.get("description"),
            "language": data.get("language"),
            "stars": data.get("stargazers_count"),
            "topics": data.get("topics"),
            "homepage": data.get("homepage"),
            "pushed_at": data.get("pushed_at"),
        }
    except Exception as e:
        return {"error": str(e)}


def sync_axes(conn, rid, card, note, meta, use_llm=True, feedback=None):
    """把卡片 + 备注同步写入多轴表（repo_axis / tags）。多维存储的唯一落点。

    来源与置信度跟随 card["source"]：LLM 分析出的领域，不该在轴表里被记成
    「启发式 0.6」—— 那会让前端费力区分的「AI 分析 / 规则预览」在下游丢失，
    也让 confidence 字段彻底不可信。

    这是「全量重建」语义（先 DELETE 再写）。因此任何改写 card 的路径都必须
    紧跟一次本调用，否则卡片与轴表分叉（P0-1 的根因）。

    use_llm=False：批量重建时用，跳过 LLM 提炼，保证重建快且确定。
    """
    c = conn.cursor()
    is_llm = (card or {}).get("source") == "llm"
    src, conf = ("llm", 0.85) if is_llm else ("heuristic", 0.6)
    c.execute("DELETE FROM repo_axis WHERE repo_id=?", (rid,))
    c.execute("DELETE FROM tags WHERE repo_id=?", (rid,))

    def put(axis_key, value, weight, source, confidence):
        if not value:
            return
        c.execute(
            "INSERT OR REPLACE INTO repo_axis (repo_id,axis_key,value,weight,source,confidence) "
            "VALUES (?,?,?,?,?,?)",
            (rid, axis_key, value, weight, source, confidence),
        )

    domain = (card or {}).get("domain")
    if domain and domain != "未分类":
        put("domain", domain, 1.0, src, conf)
    # 技术栈轴：写入前归一化（大小写/分隔符/别名 + 同仓内嵌套变体合并）。
    # 轴是聚合用的，取粗；卡片上的 tech_stack 仍是原始值，取细。
    techs = analyze.collapse_tech_variants((card or {}).get("tech_stack", [])[:8])
    for i, t in enumerate(techs[:6]):
        put("tech", t, round(0.5 - i * 0.05, 2), src, max(0.3, round(conf - 0.1, 2)))
    maturity = (card or {}).get("maturity")
    if maturity:
        put("timeline", maturity, 1.0, src, max(0.3, round(conf - 0.1, 2)))

    if note:
        nl_axes = analyze._llm_note_axes(note, meta) if use_llm else None
        if nl_axes and (nl_axes.get("motive") or nl_axes.get("scene")):
            if nl_axes.get("motive"):
                put("motive", nl_axes["motive"], 1.0, "user", 1.0)
            if nl_axes.get("scene"):
                put("scene", nl_axes["scene"], 1.0, "user", 0.9)
        else:
            put("motive", "主动寻找", 1.0, "user", 1.0)
            nl = note.lower()
            if any(k in nl for k in ["学习", "入门", "教程", "了解"]):
                put("scene", "学习研究", 1.0, "user", 0.9)
            elif any(k in nl for k in ["做", "项目", "搭", "开发", "自己的"]):
                put("scene", "做项目", 1.0, "user", 0.9)
            elif any(k in nl for k in ["灵感", "参考", "想法", "思路"]):
                put("scene", "找灵感", 1.0, "user", 0.9)
            else:
                put("scene", "临时调研", 1.0, "user", 0.9)
    else:
        put("motive", "随手刷到", 1.0, "user", 0.9)
    # 使用结果轴：用户自己点的「用过/弃了/还想用」，来源与置信度都拉满 ——
    # 这是唯一不靠推测的一层，重建时由 reindex 从 repos.feedback 原样带回。
    if feedback in FEEDBACK:
        put("use", FEEDBACK[feedback], 1.0, "user", 1.0)
    for t in (card or {}).get("tags", []):
        c.execute("INSERT OR IGNORE INTO tags (repo_id, tag) VALUES (?, ?)", (rid, t))
    conn.commit()


def _transcribe_engine(audio_path):
    """默认转写引擎：本地 faster-whisper（懒加载）；不可用静默返回 None。

    独立成函数便于注入/测试。引擎的选择集中在 douyin_source.transcribe。
    """
    return douyin_source.transcribe(audio_path, engine="whisper")


def _enrich_transcript_md(meta):
    """给认知端内容补「结构化逐字稿笔记」。

    放在 worker（后台）里做：这段 LLM 调用输出长（要覆盖全文），需要比出卡更足的预算
    （config.TRANSCRIPT_MD_TIMEOUT / MAX_TOKENS）——交互端「重算」的短预算扛不住，
    所以只在这里生成，build_card 仅透传 meta.transcript_md。
    """
    tr = (meta or {}).get("transcript")
    if tr and not (meta or {}).get("transcript_md"):
        try:
            meta["transcript_md"] = analyze.build_transcript_md(
                tr, meta, timeout=config.TRANSCRIPT_MD_TIMEOUT)
        except Exception:
            meta["transcript_md"] = None
    return meta


# 后台采集并发控制：有界信号量，保证同时最多 config.REPO_MAX_BOOST_WORKERS
# 个 worker 在跑。批量收藏时 LLM 出卡限流/费用、whisper 转写抢 CPU 内存、
# SQLite 并发写，无界并发会互相挤兑。信号量随进程生命周期；进程退出时
# 排队线程被 daemon 丢弃，无需优雅回收。
# acquire 放 worker 体内（而非 collect_payload），才能让超出的线程真正排队——
# 前台 collect 依旧立即返回 queued。
_BOOST_SEM = threading.BoundedSemaphore(config.REPO_MAX_BOOST_WORKERS)


def worker(item_id, url, note, kind, path, source="manual", transcribe_fn=None):
    """后台异步：拉取元数据 → 生成多维分析卡片 → 落库 → 同步轴表。

    kind 决定走哪条元数据管线：
    - github/gitlab：拉取仓库元数据（生产端行为收藏）
    - douyin：解析分享页 + （可选）下载转写逐字稿（认知端内容收藏），
      逐字稿存 repos.raw，与萃取后的 card 双保留。

    transcribe_fn：可注入的转写引擎（测试用假引擎，也便于以后换云端 ASR）。
    不传则走默认本地 faster-whisper。
    """
    _BOOST_SEM.acquire()
    try:
        conn = db.connect()
        c = conn.cursor()
    except Exception:
        # 连接失败也释放信号量，避免泄漏让后续收藏永久排队
        _BOOST_SEM.release()
        raise
    try:
        if kind == "douyin":
            meta = None
            raw = None
            try:
                meta, raw = douyin_source.collect_douyin(
                    url, transcribe_fn=transcribe_fn or _transcribe_engine,
                    media_dir=config.MEDIA_DIR or None)
                if raw:
                    meta["transcript"] = raw
            except Exception as e:
                # 抖音解析被反爬签名拦截（或链接失效）：链接仍要存住——做成『待补充』存根卡，
                # 让用户在收藏箱里补全文案，而不是整个失败丢链接。错误信息如实保留。
                vid = douyin_source._extract_video_id(url)
                meta = {
                    "name": "抖音 · 待补充",
                    "title": "抖音视频（待补充）",
                    "description": (note or
                                    "抖音视频元数据被反爬签名拦截，纯抓取无法获取标题/作者/封面；"
                                    "可在备注中补充视频文案，或于 App 内打开后手动补全"),
                    "author": "", "cover": "", "play_url": "",
                    "video_id": vid, "platform": "douyin",
                    "parse_status": "blocked_antibot",
                    "error": str(e)[:300],
                    "url": url,
                }
            _enrich_transcript_md(meta)   # 生成结构化逐字稿笔记（后台预算充足）
            card = analyze.build_card(meta, note, item_id, conn, kind="cognition")
            # 存根卡：标注待补充，前端据此提示用户补全
            if meta.get("parse_status") == "blocked_antibot":
                card["_stub"] = True
                card["stub_reason"] = meta.get("error")
            status = "carded" if meta.get("title") else "pending_meta"
            c.execute("UPDATE repos SET meta=?, card=?, status=?, raw=?, kind='cognition' WHERE id=?",
                      (json.dumps(meta, ensure_ascii=False),
                       json.dumps(card, ensure_ascii=False), status, raw or None, item_id))
        elif kind == "web":
            meta, raw = web_source.collect_web(url)
            if raw:
                meta["transcript"] = raw
            _enrich_transcript_md(meta)   # 生成结构化逐字稿笔记（后台预算充足）
            card = analyze.build_card(meta, note, item_id, conn, kind="cognition")
            status = "carded" if meta.get("title") else "pending_meta"
            c.execute("UPDATE repos SET meta=?, card=?, status=?, raw=?, kind='cognition' WHERE id=?",
                      (json.dumps(meta, ensure_ascii=False),
                       json.dumps(card, ensure_ascii=False), status, raw or None, item_id))
        else:
            meta = fetch_github_meta(path) if kind == "github" else {"note": "gitlab meta 暂未拉取"}
            card = analyze.build_card(meta, note, item_id, conn)
            status = "carded" if "error" not in meta else "pending_meta"
            c.execute("UPDATE repos SET meta=?, card=?, status=?, kind='production' WHERE id=?",
                      (json.dumps(meta, ensure_ascii=False),
                       json.dumps(card, ensure_ascii=False), status, item_id))
        sync_axes(conn, item_id, card, note, meta)   # worker 与 regenerate 共用同一落点
        conn.commit()
    except Exception as e:
        # 健壮性：任一环节抛异常都不再让条目「永久卡在 queued」——
        # 改为记录 status='error' 与错误信息，前端能直接看到失败原因。
        err = (str(e) or e.__class__.__name__).strip()[:300]
        try:
            c.execute(
                "UPDATE repos SET status=?, error=?, meta=?, card=? WHERE id=?",
                ("error", err,
                 json.dumps({"url": url, "error": err}, ensure_ascii=False), None, item_id))
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
        _BOOST_SEM.release()


# GitHub/GitLab 的 query 从不标识仓库（owner/repo 在 path 里），
# 分享/导航参数一律去除后做规范化查重，避免同一仓库因参数不同被当成两条。


def normalize_repo_url(url):
    """把 URL 收敛成"同一链接同一种写法"：
    - 一律去掉 #fragment；
    - GitHub/GitLab 再去掉尾部斜杠与全部 query（这类宿主指向哪一文件由 path 决定，query 不承载定位）；
    - 抖音/网页分享链接的参数有语义，仅去 fragment，不擅动 query。
    返回规范化后的字符串（供查重与入库共享，保证等价链接互相命中 dup）。"""
    u = (url or "").strip().split("#", 1)[0]
    if not u:
        return u
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(u)
    except Exception:
        return u
    if not p.netloc:
        return u
    host = p.netloc.lower()
    is_repo_host = host.endswith(("github.com", "githubusercontent.com")) or "gitlab" in host
    if not is_repo_host:
        return u
    path = p.path.rstrip("/")
    try:
        return urlunparse((p.scheme, host, path or "/", p.params, "", ""))
    except Exception:
        return u


def _near_dup(cursor, platform, path):
    """同 owner/repo 的近重复（例如同名镜像/变体链接）→ 仅软提示，不阻断。
    返回 None 或 {id, url}。仅在 GitHub/GitLab 上做，抖音/网页太模糊不做。"""
    if platform not in ("github", "gitlab"):
        return None
    seg = [s for s in (path or "").split("/") if s]
    if len(seg) < 2:
        return None
    owner, repo = seg[0].lower(), seg[1].lower()
    if not owner or not repo:
        return None
    row = cursor.execute(
        "SELECT id,url FROM repos WHERE lower(url) LIKE ? LIMIT 1", ("%/" + owner + "/" + repo + "%",)
    ).fetchone()
    if row:
        return {"id": row[0], "url": row[1]}
    return None


def collect_payload(url, note, source="manual"):
    """校验 → 查重 → 同步落库 → 后台出卡。GET / POST 共用。返回 (http_code, body)。"""
    url = normalize_repo_url(url)
    note = (note or "").strip()
    source = source if source in SOURCES else "manual"
    kind_path = classify_source(url)
    if not kind_path:
        return (400, {"ok": False,
                      "error": "仅支持 GitHub / GitLab 仓库地址，或抖音视频分享链接"})
    platform, path = kind_path
    # DB 的 kind 列存「多源类别」（production 生产端 / cognition 认知端），
    # 与平台（github/gitlab/douyin/web）是两回事 —— 后者只用于 worker 分支。
    category = "cognition" if platform in ("douyin", "web") else "production"
    conn = db.connect()
    c = conn.cursor()
    c.execute("SELECT id,status FROM repos WHERE url=?", (url,))
    existing = c.fetchone()
    if existing:
        conn.close()
        return (200, {"ok": True, "id": existing[0], "status": existing[1], "dup": True})
    # 非硬性重复：同 owner/repo 的近重复仅软提示，不阻断入库
    near = _near_dup(c, platform, path)
    try:
        c.execute("INSERT INTO repos (url,note,status,created_at,source,kind) VALUES (?,?,?,?,?,?)",
                  (url, note, "queued", now(), source, category))
        item_id = c.lastrowid
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        # 并发下两条相同 URL 同时过了上面的查重：后到的撞 UNIQUE(url)。
        # 当作「已在途/已收藏」幂等返回，绝不重复入库、不重复起 worker。
        conn.rollback(); conn.close()
        row = db.connect().execute("SELECT id,status FROM repos WHERE url=?", (url,)).fetchone()
        if row:
            return (200, {"ok": True, "id": row[0], "status": row[1], "dup": True})
        raise
    threading.Thread(target=worker, args=(item_id, url, note, platform, path, source),
                     daemon=True).start()
    body = {"ok": True, "id": item_id, "status": "queued", "source": source, "kind": category}
    if near:
        body["near_dup"] = near
    return (200, body)


def regenerate_card(rid, timeout=None):
    """重算某仓库卡片。返回 (http_code, body)。

    抽成模块级函数（而非埋在 Handler 里）有两个理由：
    1. 可被测试直接调用 —— 这样「重算后轴表必须与卡片一致」才写得出回归测试；
    2. 更新卡片与同步轴表必须写在一起，分开放就一定有人漏（P0-1 就是这么来的）。
    """
    conn = db.connect()
    c = conn.cursor()
    c.execute("SELECT url,note,meta,card FROM repos WHERE id=?", (rid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return (404, {"ok": False, "error": "not found"})
    url, note, meta_s, card_s = row[0], row[1], row[2], row[3]
    try:
        meta = json.loads(meta_s) if meta_s else {}
    except Exception:
        meta = {}
    try:
        old_card = json.loads(card_s) if card_s else None
    except Exception:
        old_card = None

    budget = config.LLM_TIMEOUT_FAST if timeout is None else timeout
    kind = analyze._kind_of_meta(meta)
    card = analyze.build_card(meta, note, rid, conn, timeout=budget, kind=kind)
    # 重算保护：本次回退到启发式、而原卡是 LLM 分析，则不覆盖原卡
    fallback = (card.get("source") != "llm") and bool(old_card and old_card.get("source") == "llm")
    if fallback:
        conn.close()
        return (200, {"ok": True, "card": old_card, "unchanged": True,
                      "reason": "llm_unavailable"})
    c.execute("UPDATE repos SET card=?, status='carded' WHERE id=?",
              (json.dumps(card, ensure_ascii=False), rid))
    sync_axes(conn, rid, card, note, meta)   # ← P0-1 修复点：卡片与轴表同批更新
    conn.commit()
    conn.close()
    return (200, {"ok": True, "card": card})


def delete_item(rid):
    conn = db.connect()
    c = conn.cursor()
    c.execute("SELECT id FROM repos WHERE id=?", (rid,))
    if not c.fetchone():
        conn.close()
        return (404, {"ok": False, "error": "not found"})
    c.execute("DELETE FROM repo_axis WHERE repo_id=?", (rid,))
    c.execute("DELETE FROM tags WHERE repo_id=?", (rid,))
    c.execute("DELETE FROM repos WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return (200, {"ok": True, "id": rid})


def reindex_axes():
    """从卡片重建全部多轴数据，返回处理条数。

    这是「卡片是真源、轴表是派生物」这条契约的可执行版本：
    只要对一致性有任何怀疑，跑一次它就归位（不调 LLM，快且确定）。
    """
    conn = db.connect()
    c = conn.cursor()
    c.execute("SELECT id, note, meta, card, feedback FROM repos WHERE card IS NOT NULL")
    rows = c.fetchall()
    n = 0
    for rid, note, meta_s, card_s, feedback in rows:
        try:
            card = json.loads(card_s) if card_s else None
            meta = json.loads(meta_s) if meta_s else {}
        except Exception:
            continue
        if not card:
            continue
        sync_axes(conn, rid, card, note, meta, use_llm=False, feedback=feedback)
        n += 1
    conn.commit()
    conn.close()
    return n


def set_feedback(rid, feedback):
    """记录「后来怎么样了」。再点一次同一个值 = 取消。

    feedback 同时写进 repos 列与 use 轴：列是事实，轴是为了能和其他轴交叉筛选
    （比如「用过 且 AI·LLM」），这正是多轴模型该干的事。
    """
    if feedback not in FEEDBACK:
        return (400, {"ok": False, "error": "feedback 只能是 used / dropped / want"})
    conn = db.connect()
    c = conn.cursor()
    c.execute("SELECT note, meta, card, feedback FROM repos WHERE id=?", (rid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return (404, {"ok": False, "error": "not found"})
    note, meta_s, card_s, old = row
    try:
        meta = json.loads(meta_s) if meta_s else {}
        card = json.loads(card_s) if card_s else None
    except Exception:
        meta, card = {}, None
    if old == feedback:   # 再点一次同一个值表示取消
        feedback = None
    c.execute("UPDATE repos SET feedback=? WHERE id=?", (feedback, rid))
    sync_axes(conn, rid, card, note, meta, use_llm=False, feedback=feedback)
    conn.commit()
    conn.close()
    return (200, {"ok": True, "id": rid, "feedback": feedback})


# ═══════════════════════════════════════════════════════════════════
#  仓库更新检测：本地比对 GitHub 元数据，把变化落成「事件」做反馈消息
# ═══════════════════════════════════════════════════════════════════
# 这个能力最划算：meta 里本来就存了 stars / description / pushed_at / language，
# fetch_github_meta 也现成。重拉一遍做 diff，比「写死共现表」那类臆测诚实得多。
# 节流：非强制检查时，24h 内检查过的仓库跳过（GitHub 未登录限速 60 次/时）。
UPDATE_CHECK_INTERVAL_HOURS = 24


def _meta_path(url):
    """从收藏的原始 url 判断是不是 GitHub 仓库，是则返回 owner/repo。

    以 url 的 host 为准（而非 meta 内容），避免把 gitlab 等误判成 GitHub。
    """
    try:
        p = urlparse(url)
        if p.netloc.lower() == "github.com":
            parts = [x for x in p.path.split("/") if x]
            if len(parts) >= 2:
                return parts[0] + "/" + parts[1]
    except Exception:
        pass
    return None


def _diff_meta(old, fresh):
    """比对两份 GitHub meta，返回变化清单（每个变化带人类可读 summary）。"""
    diffs = []
    o_s, n_s = old.get("stars"), fresh.get("stars")
    if isinstance(o_s, int) and isinstance(n_s, int) and n_s > o_s:
        diffs.append({
            "kind": "stars", "old": str(o_s), "new": str(n_s),
            "summary": "星标 %d → %d（+%d）" % (o_s, n_s, n_s - o_s),
        })
    o_p, n_p = old.get("pushed_at"), fresh.get("pushed_at")
    if o_p and n_p and n_p > o_p:
        diffs.append({
            "kind": "push", "old": o_p, "new": n_p,
            "summary": "有新提交（%s）" % n_p[:10],
        })
    if (old.get("description") or "") != (fresh.get("description") or ""):
        diffs.append({
            "kind": "desc", "old": (old.get("description") or "")[:40],
            "new": (fresh.get("description") or "")[:40],
            "summary": "仓库简介已更新",
        })
    o_l, n_l = old.get("language"), fresh.get("language")
    if (o_l or n_l) and o_l != n_l:
        diffs.append({
            "kind": "lang", "old": o_l or "无", "new": n_l or "无",
            "summary": "主语言：%s → %s" % (o_l or "无", n_l or "无"),
        })
    return diffs


def check_repo_updates(conn=None, fetch_fn=None, force=False):
    """重拉每个仓库的 GitHub 元数据并做 diff，把变化写成 repo_events。

    - fetch_fn 可注入（测试用假数据，不联网）。
    - 节流：非强制且 24h 内已检查过则跳过该仓库。
    - 限速/离线（fetch 返回 error）不更新 last_checked_at，便于稍后重试。
    - 同仓同类型未读事件只保留最新一条，避免反复检查刷屏。
    返回 {"checked": 实际联网检查数, "events": 本次新写下的事件}。
    """
    own = conn is None
    if own:
        conn = db.connect()
    fetch_fn = fetch_fn or fetch_github_meta
    c = conn.cursor()
    c.execute("SELECT id, url, meta FROM repos WHERE card IS NOT NULL")
    rows = c.fetchall()
    events = []
    checked = 0
    ts = now()
    for rid, url, meta_s in rows:
        try:
            meta = json.loads(meta_s) if meta_s else {}
        except Exception:
            meta = {}
        path = _meta_path(url)
        if not path:
            continue
        if not force:
            last = c.execute(
                "SELECT last_checked_at FROM repos WHERE id=?", (rid,)).fetchone()[0]
            if last:
                try:
                    dt = datetime.datetime.fromisoformat(last)
                    if (datetime.datetime.now() - dt).total_seconds() < \
                            UPDATE_CHECK_INTERVAL_HOURS * 3600:
                        continue
                except Exception:
                    pass
        try:
            fresh = fetch_fn(path)
        except Exception:
            fresh = {"error": "fetch failed"}
        if not isinstance(fresh, dict) or fresh.get("error"):
            continue  # 限速/离线：不更新 last_checked，稍后重试
        checked += 1
        for d in _diff_meta(meta, fresh):
            # 同仓同类型未读事件只留最新一条
            c.execute("DELETE FROM repo_events WHERE repo_id=? AND kind=? AND seen=0",
                      (rid, d["kind"]))
            c.execute(
                "INSERT INTO repo_events (repo_id,kind,old_val,new_val,summary,created_at,seen) "
                "VALUES (?,?,?,?,?,?,0)",
                (rid, d["kind"], d["old"], d["new"], d["summary"], ts))
            events.append({"repo_id": rid, "kind": d["kind"], "summary": d["summary"]})
        c.execute("UPDATE repos SET meta=?, last_checked_at=? WHERE id=?",
                  (json.dumps(fresh, ensure_ascii=False), ts, rid))
    conn.commit()
    if own:
        conn.close()
    return {"checked": checked, "events": events}


def get_updates(conn=None):
    """返回未读事件（含仓库 url，便于前端跳转）。"""
    own = conn is None
    if own:
        conn = db.connect()
    c = conn.cursor()
    c.execute(
        "SELECT e.id, e.repo_id, e.kind, e.summary, e.created_at, r.url "
        "FROM repo_events e JOIN repos r ON r.id=e.repo_id "
        "WHERE e.seen=0 ORDER BY e.created_at DESC, e.id DESC")
    evs = [{"id": eid, "repo_id": rid, "kind": kind, "summary": summary,
            "created_at": created, "url": url}
           for eid, rid, kind, summary, created, url in c.fetchall()]
    last = (c.execute("SELECT MAX(last_checked_at) FROM repos").fetchone() or [None])[0]
    if own:
        conn.close()
    return {"count": len(evs), "events": evs, "last_checked_at": last}


def mark_updates_seen(conn=None):
    """把全部未读事件标记为已读。返回标记条数。"""
    own = conn is None
    if own:
        conn = db.connect()
    n = conn.execute("UPDATE repo_events SET seen=1 WHERE seen=0").rowcount
    conn.commit()
    if own:
        conn.close()
    return n


def doctor():
    """自检：这个项目也对自己照一次镜子。

    报告卡片/轴表不一致条数、LLM 可用性、样本量、DB 状态与生效配置（脱敏）。
    """
    conn = db.connect()
    c = conn.cursor()
    c.execute("SELECT id, url, card FROM repos")
    rows = c.fetchall()
    mismatches = []
    no_axis = []
    for rid, url, card_s in rows:
        try:
            card = json.loads(card_s) if card_s else None
        except Exception:
            card = None
        c.execute("SELECT value FROM repo_axis WHERE repo_id=? AND axis_key='domain'", (rid,))
        axis_dom = (c.fetchone() or [None])[0]
        if not card:
            continue
        if not axis_dom and card.get("domain"):
            no_axis.append({"id": rid, "url": url})
            continue
        if (card.get("domain") or "未分类") != (axis_dom or "未分类"):
            mismatches.append({"id": rid, "url": url, "card": card.get("domain"), "axis": axis_dom})
    try:
        journal = c.execute("PRAGMA journal_mode").fetchone()[0]
        freelist = c.execute("PRAGMA freelist_count").fetchone()[0]
        events_unseen = c.execute(
            "SELECT COUNT(*) FROM repo_events WHERE seen=0").fetchone()[0]
    except Exception:
        journal, freelist, events_unseen = "?", 0, 0
    conn.close()

    try:
        size = os.path.getsize(config.DB)
    except Exception:
        size = 0

    # 认知端转录能力（抖音逐字稿）：ffmpeg 是否就绪 + faster-whisper 是否可 import
    _fw_ok = False
    try:
        import faster_whisper  # noqa: F401
        _fw_ok = True
    except Exception:
        _fw_ok = False
    _ff_present = bool(config.FFMPEG_BIN) and (
        config.FFMPEG_BIN == "ffmpeg" or os.path.isfile(config.FFMPEG_BIN))
    # 缺什么 → 给普通用户可执行的中文引导，而不是只给布尔标志
    _hint = None
    if not _ff_present:
        _hint = "缺 ffmpeg：抽音频这步无法工作。请安装 ffmpeg，或通过环境变量 REPO_FFMPEG_BIN 指到可执行文件。"
    elif not _fw_ok:
        _hint = "缺 faster-whisper 依赖：运行 python -m pip install faster-whisper，装完重启服务。"
    transcribe = {
        "ffmpeg_bin": config.FFMPEG_BIN,
        "ffmpeg_present": _ff_present,
        "whisper_model": config.WHISPER_MODEL,
        "whisper_available": _fw_ok,
        "ready": _ff_present and _fw_ok,
        "hint": _hint,
    }

    return {
        "ok": not mismatches and not no_axis,
        "cards": len(rows),
        "axis_mismatch": mismatches,
        "axis_missing": no_axis,
        "events_unseen": events_unseen,
        "llm": analyze.llm_status(),
        "db": {
            "path": config.DB, "schema_version": db.SCHEMA_VERSION,
            "journal_mode": journal, "freelist": freelist, "size": size,
        },
        "transcribe": transcribe,
        "github": {
            "token": bool(config.GITHUB_TOKEN),
            "rate_limited": _RATE_STATE["limited"],
            "limit_at": _RATE_STATE["at"],
            "remaining": _RATE_STATE["remaining"],
        },
        "config": config.summary(),
    }


# ═══════════════════════════════════════════════════════════════════
#  静态文件
# ═══════════════════════════════════════════════════════════════════
def get_settings_resp():
    """读 app_settings 返回可编辑的模型配置。api_key 打码（sk-***ab）下发给前端。"""
    conn = db.connect()
    api_key = db.get_settings(conn, "llm_api_key") or ""
    resp = {
        "ok": True,
        "llm": {
            "enable": db.get_settings(conn, "llm_enable", "") == "1",
            "base_url": db.get_settings(conn, "llm_base_url") or "",
            "api_key": _mask_key(api_key),
            "model": db.get_settings(conn, "llm_model") or "",
            "has_key": bool(api_key),
        },
    }
    conn.close()
    return resp


def _mask_key(k):
    """打码 api_key：保留头 3 尾 2，中间补 ***。空返回空串。"""
    k = k or ""
    if len(k) <= 6:
        return "***" if k else ""
    return k[:3] + "***" + k[-2:]


def save_settings(payload):
    """写模型配置到 app_settings。前端回传的掩码 key 不得覆盖真实 key。"""
    llm = payload.get("llm") or {}
    conn = db.connect()
    db.set_settings(conn, "llm_enable", "1" if llm.get("enable") else "0")
    db.set_settings(conn, "llm_base_url", (llm.get("base_url") or "").strip())
    db.set_settings(conn, "llm_model", (llm.get("model") or "").strip())
    new_key = (llm.get("api_key") or "").strip()
    if new_key:
        if "***" not in new_key:
            db.set_settings(conn, "llm_api_key", new_key)
        # 掩码回传 = 未改 key，保留现有值（不覆盖）
    else:
        db.set_settings(conn, "llm_api_key", "")
    conn.commit()
    conn.close()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
def resolve_static(rel):
    """把 URL 路径解析成 web/ 下的真实文件；越界或不存在返回 None。

    改造前这里是一串硬编码文件名白名单，加一个页面就得改后端。
    改成目录级 serve，保留路径穿越校验（normpath 后必须仍在 web/ 内）。
    """
    rel = (rel or "").lstrip("/")
    if not rel:
        return None
    # 先解码再规范化：否则 /%2e%2e/ 这类编码过的穿越会绕过 startswith 检查
    full = os.path.normpath(os.path.join(WEB_DIR, unquote(rel)))
    if full != WEB_DIR and not full.startswith(WEB_DIR + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    return full


COLLECT_RESULT_TPL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#FBFAF7">
<title>收藏箱 · 收藏</title><style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
background:#FBFAF7;color:#23201B;max-width:420px;margin:0 auto;padding:56px 22px;text-align:center}
.t{font-size:22px;font-weight:600;margin-bottom:10px}
.m{font-size:14px;color:#6B655C;line-height:1.7}
.u{font-size:12px;color:#9A938A;word-break:break-all;margin:16px 0}
a{display:inline-block;margin:10px 6px;font-size:14px;color:#B5673E;
text-decoration:none;border:1px solid rgba(181,103,62,.34);border-radius:8px;padding:7px 18px}
</style></head><body>
<div class="t">%(title)s</div>
<div class="m">%(msg)s</div>
%(url)s
<div><a href="/">继续收藏</a><a href="/cards.html">看卡片</a></div>
</body></html>"""


def render_collect_result(code, body, url):
    """深链 / 移动端结果页。从 Handler 里独立出来，便于统一转义与复用。"""
    ok, dup = body.get("ok"), body.get("dup")
    if ok and dup:
        title, msg = "已在收藏", "这个仓库之前收过了，没有重复添加。"
    elif ok:
        title, msg = "已收藏", "仓库已加入收集队列，后台正在生成分析卡片。"
    else:
        title, msg = "未能收藏", (body.get("error") or "地址无效，请检查后重试。")
    return COLLECT_RESULT_TPL % {
        "title": esc_html(title),
        "msg": esc_html(msg),
        "url": ('<div class="u">' + esc_html(url) + "</div>") if url else "",
    }


# ═══════════════════════════════════════════════════════════════════
#  HTTP 层
# ═══════════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------- 基础设施 ----------
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, full, ctype):
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", with_charset(ctype))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        """读 JSON body。返回 (payload, err_code)。超限 413，坏 JSON 400。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > config.MAX_BODY:
            return None, 413
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return None, 400
        return (obj if isinstance(obj, dict) else {}), 200

    def _drain_body(self):
        """读掉并丢弃请求体（不解析）。

        为什么必须做：这些写接口（/api/reindex、/api/updates/check、/api/updates/seen）
        不关心 body，但浏览器 fetch 仍会发 `body:"{}"`。若不消费，残留字节会留在 keep-alive
        连接里，和下一个请求行黏成 `{}GET /api/updates` → 服务端报
        "Unsupported method ('{}GET')" 501，把紧随其后的 GET 请求打挂。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 0:
            try:
                self.rfile.read(length)
            except Exception:
                pass

    def _token_ok(self):
        """可选口令校验。未配置 TOKEN 时恒通过（本机自用默认）。"""
        if not config.TOKEN:
            return True
        sent = self.headers.get("X-Collector-Token") or ""
        if not sent:
            qs = parse_qs(urlparse(self.path).query)
            sent = qs.get("k", [""])[0] or ""
        return hmac.compare_digest(sent, config.TOKEN)

    def _origin_local(self):
        """跨源写守卫：只挡破坏性/非幂等操作（删除、重算）。

        为什么不挡 POST /collect：它的设计前提就是「任意页面都能投递」
        —— 书签工具跑在 github.com 上，悬浮球会被嵌进第三方页面。
        全局同源白名单会把这两个入口直接打死，所以只守删除与重算。
        """
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True          # 无 Origin：curl / 原生壳 / 系统分享，都是可信本机来源
        host = (urlparse(origin).hostname or "").lower()
        return host in config.ALLOW_ORIGIN_HOSTS

    def _write_guard(self, same_origin_required):
        """返回 None 表示放行，否则返回错误响应元组。"""
        if not self._token_ok():
            return (401, {"ok": False, "error": "缺少或错误的口令（X-Collector-Token）"})
        if same_origin_required and not self._origin_local():
            return (403, {"ok": False, "error": "拒绝跨源写操作"})
        return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS,DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Collector-Token")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._serve_file(os.path.join(WEB_DIR, "collect.html"), "text/html")
            return
        if path == "/collect":
            # 分享深链：从系统分享 / 二维码 / 短信打开即收藏（移动友好结果页）
            if not self._token_ok():
                self._send(401, {"ok": False, "error": "缺少或错误的口令"})
                return
            url = (qs.get("url", [""])[0] or "").strip()
            note = (qs.get("note", [""])[0] or "").strip()
            source = (qs.get("source", [""])[0] or "").strip() or "share"
            if not url:
                txt = (qs.get("text", [""])[0] or "").strip()
                if txt:
                    url = extract_repo_url(txt) or ""
                    if not note:
                        note = txt
            code, body = collect_payload(url, note, source)
            self._send(code, render_collect_result(code, body, url), "text/html")
            return
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if path.startswith("/api/"):
            self._api_get(path, qs)
            return
        # 静态：目录级 serve（web/ 下任何文件都可访问，越界与不存在均 404）
        full = resolve_static(path)
        if full:
            ext = os.path.splitext(full)[1].lower()
            self._serve_file(full, CTYPES.get(ext, "application/octet-stream"))
        else:
            self._send(404, {"error": "not found"})

    def _api_get(self, path, qs):
        if not self._token_ok():
            self._send(401, {"ok": False, "error": "缺少或错误的口令"})
            return
        if path == "/api/items":
            self._send(200, self._items(qs.get("q", [""])[0]))
        elif path == "/api/cards":
            items = get_cards({
                "domain": qs.get("domain", []), "tech": qs.get("tech", []),
                "motive": qs.get("motive", []), "use": qs.get("use", []),
                "kind": qs.get("kind", []),
                "q": (qs.get("q", [""])[0] or "").strip(),
                "sort": (qs.get("sort", [""])[0] or "").strip(),
            })
            self._send(200, {"count": len(items), "items": items})
        elif path == "/api/facets":
            f = get_axis_facets()
            # 只暴露有数据的层（domain/tech/motive/use），隐藏稀疏/内部轴
            visible = {k: f[k] for k in ("domain", "tech", "motive", "use")
                       if k in f and f[k]}
            self._send(200, visible)
        elif path == "/api/query":
            axes, values = qs.get("axis", []), qs.get("value", [])
            filters = [(axes[i], values[i]) for i in range(min(len(axes), len(values)))]
            ids = query_axes(filters)
            rows = []
            if ids:
                conn = db.connect()
                cc = conn.cursor()
                cc.execute("SELECT id,url,note,meta,card,kind FROM repos WHERE id IN (%s)"
                           % ",".join("?" * len(ids)), list(ids))
                rows = cc.fetchall()
                conn.close()
            items = []
            for r in rows:
                try:
                    meta = json.loads(r[3]) if r[3] else None
                    card = json.loads(r[4]) if r[4] else None
                except Exception:
                    meta, card = None, None
                items.append({"id": r[0], "url": r[1], "note": r[2], "meta": meta,
                              "card": card, "kind": r[5] or "production"})
            self._send(200, {"filters": filters, "count": len(items), "items": items})
        elif path == "/api/graph":
            self._send(200, get_graph())
        elif path == "/api/recommend":
            self._send(200, get_recommendations())
        elif path == "/api/trending":
            self._send(200, get_trending_weekly())
        elif path == "/api/profile":
            self._send(200, get_profile())
        elif path == "/api/doctor":
            self._send(200, doctor())
        elif path == "/api/settings":
            self._send(200, get_settings_resp())
        elif path == "/api/updates":
            self._send(200, get_updates())
        else:
            self._send(404, {"error": "not found"})

    @staticmethod
    def _items(q):
        conn = db.connect()
        c = conn.cursor()
        if q:
            like = "%" + q + "%"
            c.execute(
                """SELECT id,url,note,status,meta,card,created_at,feedback,kind,raw,error FROM repos
                   WHERE url LIKE ? OR note LIKE ? OR meta LIKE ? OR card LIKE ?
                   ORDER BY id DESC LIMIT 50""", (like, like, like, like))
        else:
            c.execute("SELECT id,url,note,status,meta,card,created_at,feedback,kind,raw,error FROM repos "
                      "ORDER BY id DESC LIMIT 50")
        rows = c.fetchall()
        conn.close()
        items = []
        for r in rows:
            try:
                meta = json.loads(r[4]) if r[4] else None
                card = json.loads(r[5]) if r[5] else None
            except Exception:
                meta, card = None, None
            items.append({"id": r[0], "url": r[1], "note": r[2], "status": r[3],
                          "meta": meta, "card": card, "created_at": r[6],
                          "feedback": r[7], "kind": r[8] or "production",
                          "raw": r[9], "error": r[10]})
        return {"items": items, "q": q}

    # ---------- POST ----------
    def do_POST(self):
        p = self.path.rstrip("/")
        if p == "/api/card/regenerate":
            guard = self._write_guard(same_origin_required=True)
            if guard:
                self._send(*guard)
                return
            payload, err = self._read_json()
            if err != 200:
                self._send(err, {"ok": False, "error": "bad json" if err == 400 else "body too large"})
                return
            rid = payload.get("id")
            if not rid:
                self._send(400, {"ok": False, "error": "need id"})
                return
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                self._send(400, {"ok": False, "error": "bad id"})
                return
            self._send(*regenerate_card(rid))
            return
        if p == "/api/reindex":
            guard = self._write_guard(same_origin_required=True)
            if guard:
                self._send(*guard)
                return
            self._drain_body()
            n = reindex_axes()
            self._send(200, {"ok": True, "rebuild": n})
            return
        if p == "/api/item/feedback":
            guard = self._write_guard(same_origin_required=True)
            if guard:
                self._send(*guard)
                return
            payload, err = self._read_json()
            if err != 200:
                self._send(err, {"ok": False,
                                 "error": "bad json" if err == 400 else "body too large"})
                return
            try:
                rid = int(payload.get("id"))
            except (TypeError, ValueError):
                self._send(400, {"ok": False, "error": "bad id"})
                return
            self._send(*set_feedback(rid, payload.get("feedback")))
            return
        if p == "/api/updates/check":
            guard = self._write_guard(same_origin_required=True)
            if guard:
                self._send(*guard)
                return
            self._drain_body()
            res = check_repo_updates(force=True)
            self._send(200, {"ok": True, "checked": res["checked"],
                             "new_events": len(res["events"])})
            return
        if p == "/api/updates/seen":
            guard = self._write_guard(same_origin_required=True)
            if guard:
                self._send(*guard)
                return
            self._drain_body()
            n = mark_updates_seen()
            self._send(200, {"ok": True, "seen": n})
            return
        if p == "/api/settings":
            guard = self._write_guard(same_origin_required=True)
            if guard:
                self._send(*guard)
                return
            payload, err = self._read_json()
            if err != 200:
                self._send(err, {"ok": False,
                                 "error": "bad json" if err == 400 else "body too large"})
                return
            self._send(200, save_settings(payload))
            return
        if p != "/collect":
            self._send(404, {"error": "not found"})
            return
        # 收藏入口：不设同源限制（书签 / 悬浮球 / 深链都从外部页面投递）
        if not self._token_ok():
            self._send(401, {"ok": False, "error": "缺少或错误的口令"})
            return
        payload, err = self._read_json()
        if err != 200:
            self._send(err, {"ok": False, "error": "bad json" if err == 400 else "body too large"})
            return
        url = (payload.get("url") or "").strip()
        note = (payload.get("note") or "").strip()
        source = (payload.get("source") or "").strip()
        text = (payload.get("text") or "").strip()
        if not url and text:
            url = extract_repo_url(text) or ""
            if not note:
                note = (text.replace(url, "").strip()[:200]) if url else text[:200]
        if not url:
            self._send(400, {"ok": False, "error": "请提供仓库地址，或在 text 中附上链接"})
            return
        self._send(*collect_payload(url, note, source))

    # ---------- PUT ----------
    # 写操作统一走 do_POST 的路由逻辑；这里的 PUT 只作别名（设置/更新类用 PUT 语义）。
    def do_PUT(self):
        self.do_POST()

    # ---------- DELETE ----------
    def do_DELETE(self):
        p = self.path.rstrip("/")
        if p.startswith("/api/item/"):
            guard = self._write_guard(same_origin_required=True)
            if guard:
                self._send(*guard)
                return
            try:
                rid = int(p.split("/")[-1])
            except Exception:
                self._send(400, {"ok": False, "error": "bad id"})
                return
            self._send(*delete_item(rid))
            return
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def main():
    init_db()
    print("repo collector on http://%s:%d  (db=%s)" % (config.HOST, config.PORT, config.DB))
    print("  页面：/  /cards.html  /map.html  /recommend.html  /profile.html  /share.html")
    print("  自检：/api/doctor")
    if config.TOKEN:
        print("  口令：已启用（写操作需 X-Collector-Token）")
    if not config.GITHUB_TOKEN:
        print("  提示：未配置 REPO_GITHUB_TOKEN，GitHub 搜索限速 10 次/分")
    ThreadingHTTPServer((config.HOST, config.PORT), Handler).serve_forever()


if __name__ == "__main__":
    # 进本项目方案：仓库自带 .venv 时优先用 venv python 启动（抖音转录依赖 faster-whisper）。
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _venv_py = os.path.join(_root, ".venv", "Scripts", "python.exe")
    if os.path.isfile(_venv_py) and os.path.abspath(sys.executable) != os.path.abspath(_venv_py):
        os.execv(_venv_py, [_venv_py, os.path.abspath(__file__)])
    main()
