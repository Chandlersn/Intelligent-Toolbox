"""主题归纳：把库里的 N 条条目**归纳**成 M 个主题（集合级结构）。

与 `card.domain`（领域标签）的区别 —— 这是本模块存在的理由：
  - `domain` 是**逐条独立判定**的预定义桶：同一条在不同时间可能落进不同桶，桶与桶之间
    没有关系，也不告诉你这个桶里"已经有什么、还缺什么"。
  - 主题是**互相参照归纳**出来的：它由条目之间的关系产生，带成员、覆盖度与缺口，
    粒度也比领域标签更具体（"理财/投资"下可以分出"AI 算力主线的投资逻辑"与
    "一级市场募资环境"两个主题）。

产出先落 `draft`，用户确认后才变 `confirmed` —— 沿用项目既定原则：模型产出只展示，
定稿才固化。重算只替换 `draft`，不碰已确认的主题。
"""

import datetime
import threading

import analyze
import config
import db

STATUS_DRAFT = "draft"
STATUS_CONFIRMED = "confirmed"

_SIG_KEY = "topics_signature"
_BUILT_KEY = "topics_built_at"

# 同一时刻只允许一次归纳在跑：归纳是整库 + 一次长 LLM 调用，重复触发既慢又浪费。
_BUILD_LOCK = threading.Lock()

# 覆盖率下限：本次归纳覆盖到的条目占比低于它会再调一次模型，取覆盖更多的那次。
# 0.6 是实测定的——模型偶尔只挑最显眼的一组就收工（8 条只覆盖 2 条）。
_COVER_ENOUGH = 0.6


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════
#  给模型的输入：把条目压成清单
# ═══════════════════════════════════════════════════════════════════

_SYSTEM = """你是知识结构分析师。用户会给你一份收藏条目清单，每行格式：
编号 | 类型 | 领域标签 | 关键词 | 标题 | 用途

你的任务：把这些条目**归纳**成若干主题，并指出每个主题的覆盖缺口。

硬性要求：
- 主题必须从这些条目**归纳**得出，不是套用现成的学科分类；主题名要具体到能看出在讲什么。
  禁止「其他」「杂项」「综合」「各类」这类没有信息量的名字。
- 一条条目只放进它真正所属的主题；宁缺勿滥——不属于任何主题的条目就不要放进去，
  也允许某个主题只有一条条目（如果它确实自成一类）。
- **先把全部条目过一遍再分组**：不同的事要分成不同主题，不要把明显不同类的内容
  硬塞进同一个主题，也不要只挑最显眼的一组就收工。
- 主题数量由内容决定，通常 3-8 个；条目少时就少给几个，不要硬凑。
- 每个主题给 1-3 条「缺口」：现有条目**没能覆盖**的关键问题或证据类型
  （例如「只有正面案例，没有失败复盘」「全是观点，没有数据来源」「没说明适用边界」）。
  缺口必须能从给定条目判断出来，不许凭空设想。
- 只输出 JSON，不要任何解释文字、不要 markdown 围栏。
- **紧凑输出**：不要缩进、不要换行美化，整个 JSON 写成一行或用最少空白 ——
  输出长度有限，空白会挤掉正文并导致结果被截断。
- **必须完整闭合**：整个 JSON 对象要写到最后一个 `}` 为止，不要中途收尾。

输出格式：
{"topics":[{"title":"主题名","summary":"这个主题在讲什么（一两句）","members":[条目编号...],"gaps":["缺口1","缺口2"]}]}"""


def _load(s):
    import json
    try:
        return json.loads(s) if s else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _clip(s, n):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _label(kind, meta):
    """给模型看的条目类型。分不清的不硬猜，统一叫「内容」。"""
    if kind == "cognition":
        return "视频" if (meta or {}).get("platform") == "douyin" else "文章"
    if kind == "production":
        return "仓库"
    return "内容"


def _digest(conn, limit=None):
    """把条目压成清单行。返回 (lines, ids, total)。

    取**最近更新的 N 条**（超出上限时），并在正文里显式说明截断了多少 —— 不静默丢弃。
    """
    limit = limit or config.TOPICS_MAX_ITEMS
    rows = conn.execute(
        "SELECT id, kind, meta, card FROM repos ORDER BY id DESC").fetchall()
    total = len(rows)
    rows = list(reversed(rows[:limit]))                      # 截最近的，再按 id 升序输出
    lines, ids = [], []
    for rid, kind, meta_s, card_s in rows:
        meta, card = _load(meta_s), _load(card_s)
        title = (meta.get("title") or meta.get("name") or "").strip()
        purpose = (card.get("purpose") or meta.get("description") or "").strip()
        tags = "·".join((card.get("tags") or [])[:6])
        lines.append("#%d | %s | %s | %s | %s | %s" % (
            rid, _label(kind, meta), (card.get("domain") or "未分类"),
            tags or "-", _clip(title, 60), _clip(purpose, 80)))
        ids.append(rid)
    return lines, ids, total


# ═══════════════════════════════════════════════════════════════════
#  校验：模型给的成员编号必须真的存在
# ═══════════════════════════════════════════════════════════════════

def _validate(obj, allowed):
    """把模型返回的主题清洗成可落库的形状。

    只保留**确实在给定清单里**的成员编号（模型可能编出不存在的编号）。
    没有有效成员的主题直接丢弃 —— 空主题没有意义。
    """
    out = []
    for t in (obj or {}).get("topics") or []:
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or "").strip()
        members = []
        for m in t.get("members") or []:
            try:
                mid = int(m)
            except (TypeError, ValueError):
                continue
            if mid in allowed and mid not in members:
                members.append(mid)
        if not title or not members:
            continue
        gaps = [str(g).strip() for g in (t.get("gaps") or []) if str(g).strip()]
        out.append({
            "title": _clip(title, 40),
            "summary": _clip(t.get("summary") or "", 200),
            "members": members,
            "gaps": gaps[:3],
        })
    return out


# ═══════════════════════════════════════════════════════════════════
#  指纹：判断现有主题是否「过期」
# ═══════════════════════════════════════════════════════════════════

def _fingerprint(conn):
    """条目集合指纹（条数 + 最大 id + id 之和）。

    三者一起能在新增、删除（含删除中间条目）时都发生变化 —— 用来告诉前端
    「库变了，主题是旧的」，而不是悄悄展示过期结构。
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(id), 0) FROM repos"
    ).fetchone()
    return "%d:%d:%d" % (row[0], row[1], row[2])


def _setting(conn, key, value=None):
    """读写 app_settings（复用既有 KV 表，不为两个元信息单开一张表）。"""
    if value is None:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    conn.execute("INSERT INTO app_settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return value


# ═══════════════════════════════════════════════════════════════════
#  归纳
# ═══════════════════════════════════════════════════════════════════

def _missing_closers(s):
    """返回补全 JSON 所需的闭合符，或 None（不该补 / 不能补）。

    实测模型会**随机漏掉最外层的 `}`**（同一输入两次，一次完整一次缺尾括号），
    而输出长度远未到上限 —— 这不是 token 截断，是模型输出习惯。补一个 `}` 就能救回
    整份结果，值得做。

    但边界必须严格：**字符串未闭合时拒绝补**。那说明内容真的被从中间截断了，
    补出来的 JSON 会把半句话当成正常内容静默混入 —— 宁可失败，不可假装成功。
    括号错配（如 `]}` 顺序颠倒）同样拒绝。
    """
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
    if in_str or not stack or len(stack) > 3:
        return None
    return "".join(reversed(stack))


def _parse(raw):
    """解析模型返回的主题 JSON。返回 (dict|None, repaired)。

    比通用 `analyze._llm_json` 多做两件事：
      1. 模型有时在 JSON 前后加一句说明（"好的，以下是归纳结果："）→ 抓首尾大括号再试；
      2. 漏了闭合括号时按 `_missing_closers` 补全（repaired=True 会被带回给调用方）。
    """
    import json
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()

    cands = [s]
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i and (i, j) != (0, len(s) - 1):
        cands.append(s[i: j + 1])

    for cand in cands:
        for text, repaired in ((cand, False), (None, True)):
            if repaired:
                fix = _missing_closers(cand)
                if not fix:
                    continue
                text = cand + fix
            try:
                obj = json.loads(text)
            except Exception:                                # noqa: BLE001
                continue
            if isinstance(obj, dict):
                return obj, repaired
    return None, False


def build_topics(timeout=None, conn=None):
    """跑一次归纳：全库条目 → 主题，落 draft。返回统计结果。

    条目少于 `TOPICS_MIN_ITEMS` 时不调用模型 —— 两三条素材归纳出来的「主题」
    只是领域标签的同义词，没有结构价值，还会给人「已分析过」的错觉。
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        lines, ids, total = _digest(conn)
        if len(ids) < config.TOPICS_MIN_ITEMS:
            return {"ok": True, "count": 0, "total_items": total, "built": False,
                    "note": "条目太少（%d 条），暂不归纳：至少 %d 条才看得出结构。"
                            % (len(ids), config.TOPICS_MIN_ITEMS)}
        if not _BUILD_LOCK.acquire(blocking=False):
            return {"ok": False, "count": 0, "built": False, "msg": "已有一次归纳在进行中"}
        try:
            user = "以下是 %d 条收藏条目（库中共 %d 条，按最近更新取 %d 条）：\n\n%s" % (
                len(ids), total, len(ids), "\n".join(lines))
            max_tokens = config.TOPICS_MAX_TOKENS
            # 模型输出质量不稳定：实测同一批 8 条，有一次只归纳出 1 个主题、覆盖 2 条
            # （只挑最显眼的一组就收工）。因此最多调用两次，取**覆盖条目更多**的那次，
            # 覆盖率够高就不再重试。归纳是显式动作，多花一次调用换可用结果是划算的。
            best = None                       # (topics, repaired, covered, raw_len)
            fail = None                       # ("empty"|"badjson", 长度, 结尾)
            for _attempt in (1, 2):
                raw = analyze._llm_chat(_SYSTEM, user,
                                        timeout=timeout or config.TOPICS_TIMEOUT,
                                        max_tokens=max_tokens, use_cache=False)
                if not raw:
                    fail = ("empty", 0, "")
                    continue
                obj, repaired = _parse(raw)
                if obj is None:
                    fail = ("badjson", len(raw), raw.strip()[-30:])
                    continue
                found = _validate(obj, set(ids))
                covered = len({m for t in found for m in t["members"]})
                if best is None or covered > best[2]:
                    best = (found, repaired, covered, len(raw))
                if found and covered >= _COVER_ENOUGH * len(ids):
                    break
            if best is None or not best[0]:
                if fail and fail[0] == "empty":
                    return {"ok": False, "count": 0, "built": False,
                            "msg": "模型没有返回内容（未配置 / 不可达 / 超时）"}
                if fail and fail[0] == "badjson":
                    # 解析失败几乎总是「输出被从中间截断」，把长度与结尾一并给出，
                    # 下次排障不用再翻原始返回。
                    return {"ok": False, "count": 0, "built": False,
                            "msg": "模型返回的不是合法 JSON（%d 字，结尾 %r）；"
                                   "常见原因是输出被 max_tokens=%d 截断，"
                                   "可调大 REPO_TOPICS_MAX_TOKENS 重试。"
                                   % (fail[1], fail[2], max_tokens)}
                if fail:
                    return {"ok": False, "count": 0, "built": False,
                            "msg": "模型返回的 JSON 里没有可用的主题"
                                   "（成员编号都不在清单内，或主题缺标题）。"}
                return {"ok": False, "count": 0, "built": False,
                        "msg": "模型没有产出任何主题。"}
            topics, repaired, covered, _ = best
            # 只替换 draft：用户已确认的主题不该被一次重算抹掉。
            conn.execute("DELETE FROM topics WHERE status=?", (STATUS_DRAFT,))
            conn.execute("DELETE FROM topic_members WHERE topic_id NOT IN "
                         "(SELECT id FROM topics)")
            ts = _now()
            for t in topics:
                cur = conn.execute(
                    "INSERT INTO topics(title,summary,gaps,status,origin,signature,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (t["title"], t["summary"], _dumps(t["gaps"]), STATUS_DRAFT,
                     "llm", None, ts, ts))
                tid = cur.lastrowid
                conn.executemany(
                    "INSERT OR IGNORE INTO topic_members(topic_id,repo_id) VALUES(?,?)",
                    [(tid, r) for r in t["members"]])
            _setting(conn, _SIG_KEY, _fingerprint(conn))
            _setting(conn, _BUILT_KEY, ts)
            conn.commit()
            return {"ok": True, "count": len(topics), "built": True,
                    "total_items": total, "analyzed_items": len(ids), "built_at": ts,
                    "covered": covered, "uncovered": len(ids) - covered,
                    "repaired": repaired}     # True=模型漏了闭合括号，由解析层补全
        finally:
            _BUILD_LOCK.release()
    finally:
        if own:
            conn.close()


def _dumps(v):
    import json
    return json.dumps(v, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════
#  读取
# ═══════════════════════════════════════════════════════════════════

def list_topics(conn=None):
    """主题列表（含成员摘要与覆盖度）。一次查全再分组，不做 N+1 查询。"""
    own = conn is None
    conn = conn or db.connect()
    try:
        rows = conn.execute(
            "SELECT id,title,summary,gaps,status,created_at FROM topics "
            "ORDER BY CASE WHEN status='confirmed' THEN 0 ELSE 1 END, id"
        ).fetchall()                                          # 已确认的排在前面
        if not rows:
            total_items = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
            return {"ok": True, "topics": [], "total": 0,
                    "built_at": _setting(conn, _BUILT_KEY),
                    "stale": _fingerprint(conn) != _setting(conn, _SIG_KEY),
                    "covered": 0, "total_items": total_items}

        # 成员关系 + 条目摘要：两次查询解决全部主题，避免逐主题查库
        members = {}
        for tid, rid in conn.execute("SELECT topic_id, repo_id FROM topic_members"):
            members.setdefault(tid, []).append(rid)
        brief = {}
        for rid, kind, meta_s, card_s, created in conn.execute(
                "SELECT id, kind, meta, card, created_at FROM repos"):
            meta, card = _load(meta_s), _load(card_s)
            brief[rid] = {
                "id": rid,
                "title": _clip(meta.get("title") or meta.get("name") or "", 80)
                         or "(无标题)",
                "kind": kind or "",
                "label": _label(kind, meta),
                "domain": card.get("domain") or "未分类",
                "created_at": created or "",
            }

        topics = []
        covered_ids = set()
        for tid, title, summary, gaps, status, created in rows:
            items = [brief[r] for r in members.get(tid, []) if r in brief]
            if not items:
                continue                                          # 成员条目都被删了，不展示空壳
            covered_ids.update(it["id"] for it in items)
            kinds, domains = {}, []
            for it in items:
                kinds[it["label"]] = kinds.get(it["label"], 0) + 1
                if it["domain"] not in domains:
                    domains.append(it["domain"])
            topics.append({
                "id": tid, "title": title, "summary": summary,
                "gaps": _load(gaps) or [],
                "status": status, "created_at": created,
                "count": len(items),
                "kinds": kinds,
                "domains": domains,
                "latest_at": max((it["created_at"] for it in items), default=""),
                "items": items,
            })
        return {"ok": True, "topics": topics, "total": len(topics),
                "built_at": _setting(conn, _BUILT_KEY),
                "stale": _fingerprint(conn) != _setting(conn, _SIG_KEY),
                # 覆盖率：有多少条目真的被归进了主题。宁缺勿滥是刻意设计，但要让人
                # 看得见「这次归纳覆盖了多少」—— 数字低就说明该再归纳一次。
                "covered": len(covered_ids), "total_items": len(brief)}
    finally:
        if own:
            conn.close()


def set_status(topic_id, status, conn=None):
    """确认 / 撤回一个主题。确认后的主题不会被下次重算覆盖。"""
    if status not in (STATUS_DRAFT, STATUS_CONFIRMED):
        return {"ok": False, "msg": "状态只能是 draft 或 confirmed"}
    own = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.execute("UPDATE topics SET status=?, updated_at=? WHERE id=?",
                           (status, _now(), topic_id))
        conn.commit()
        if not cur.rowcount:
            return {"ok": False, "msg": "主题不存在"}
        return {"ok": True, "id": topic_id, "status": status}
    finally:
        if own:
            conn.close()


def status(conn=None):
    """给 doctor / 前端用的状态摘要。"""
    own = conn is None
    conn = conn or db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        rows = conn.execute("SELECT status, COUNT(*) FROM topics GROUP BY status").fetchall()
        return {"count": n, "by_status": dict(rows),
                "built_at": _setting(conn, _BUILT_KEY),
                "stale": _fingerprint(conn) != _setting(conn, _SIG_KEY),
                "min_items": config.TOPICS_MIN_ITEMS}
    finally:
        if own:
            conn.close()
