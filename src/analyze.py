"""开源仓库收藏 — 多维分析卡片生成器。

输入：GitHub 元数据(meta) + 用户备注(note)
输出：多维分析卡片 card
  {
    domain, purpose, tech_stack, why_needed,
    related_nodes, maturity, tags, recommendation, source
  }

设计原则：
- 零外部依赖，离线可跑；meta 缺失时降级出基础卡片。
- LLM 分支：复用灯笼项目的 llm 模块（chat 封装 + 同一份 key 配置），
  真实理解仓库并生成卡片；未配置 / 调用失败则回退规则卡片并标注 source="heuristic"。
- 卡片带 source 字段（llm / heuristic），前端据此区分「AI 分析」与「规则预览」，
  避免用假洞察污染行为认知镜。
"""

import os
import re
import json
import datetime

import config

# 灯笼项目根（复用其 llm 模块，不拷 key 到收藏箱）。
# 由 REPO_LLM_ROOT 覆盖；import 失败 / 未配置 key 即走规则分支。
LANTERN_ROOT = config.LLM_ROOT
LLM_MODE = {"module": None, "tried": False}


def _load_llm():
    """懒加载灯笼 llm 模块。只试一次，失败则永久标记不可用。"""
    if LLM_MODE["tried"]:
        return LLM_MODE["module"]
    LLM_MODE["tried"] = True
    try:
        if LANTERN_ROOT not in os.sys.path:
            os.sys.path.insert(0, LANTERN_ROOT)
        import llm as _m
        if getattr(_m, "AVAILABLE", False):
            LLM_MODE["module"] = _m
    except Exception:
        LLM_MODE["module"] = None
    return LLM_MODE["module"]


def llm_status():
    """给 /api/doctor 用：当前 LLM 是否可用、复用的模块在哪、上游熔断状态。

    只报告事实，不做降级决策 —— 降级是各调用点自己的事。
    """
    mod = _load_llm()
    st = {"root": LANTERN_ROOT, "module_loaded": bool(mod), "available": bool(mod)}
    if mod is not None and hasattr(mod, "breaker_state"):
        try:
            st["breaker"] = mod.breaker_state()
        except Exception:
            pass
    return st


def _llm_chat(system, user, timeout=None, retries=None):
    """全项目唯一的 llm.chat 调用点：懒加载 → 调用 → 解包 content。

    返回字符串，或 None（模块不可用 / 抛异常 / 空返回）。
    为什么集中：改造前 4 个调用点各写一遍 try/except + 元组解包，且都没传超时，
    一旦上游接口半死，worker 线程会一直挂着。超时预算现在统一从 config 来。
    """
    llm_mod = _load_llm()
    if llm_mod is None:
        return None
    kw = {}
    if timeout is not None:
        kw["timeout"] = timeout
    if retries is not None:
        kw["retries"] = retries
    try:
        resp = llm_mod.chat(system, user, **kw)
    except Exception:
        return None
    if isinstance(resp, (tuple, list)):
        content = resp[0] if resp else ""
    else:
        content = resp
    content = (content or "").strip()
    return content or None


def _llm_json(system, user, expect="dict", timeout=None, retries=None):
    """LLM → JSON 对象。容错剥掉 ```json 围栏，形状不符则返回 None。

    expect: "dict" | "list"。在唯一入口处校验结构，省掉各调用点重复的 isinstance。
    """
    content = _llm_chat(system, user, timeout=timeout, retries=retries)
    if not content:
        return None
    raw = content
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw[:4].lower() == "json":
            raw = raw[4:]
        raw = raw.strip()
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if expect == "list" and not isinstance(obj, list):
        return None
    if expect == "dict" and not isinstance(obj, dict):
        return None
    return obj


def _llm_card(meta, note, item_id, conn, timeout=None, retries=None, kind="production"):
    """用灯笼 llm.chat 生成卡片。返回 (card_dict, True) 或 (None, False)。

    kind='production' → 仓库视角（GitHub 元数据）；kind='cognition' → 内容视角
    （抖音/文章，标题/简介/逐字稿）。两者共用同一份领域白名单与卡片结构。
    """
    llm_mod = _load_llm()
    if llm_mod is None:
        return None, False
    # 已有收藏的轻量上下文：用 domain 轴让分析带一点个性化（只发领域名，不出行为明细）
    ctx = ""
    if conn:
        try:
            cc = conn.cursor()
            cc.execute(
                "SELECT value, COUNT(*) AS n FROM repo_axis WHERE axis_key='domain' "
                "GROUP BY value ORDER BY n DESC LIMIT 5"
            )
            rows = cc.fetchall()
            if rows:
                ctx = "你已收藏的领域分布（仅领域名，用于语境，不泄露其它数据）：" + \
                      "；".join("%s×%d" % (r[0], r[1]) for r in rows)
        except Exception:
            ctx = ""

    if kind == "cognition":
        title = meta.get("title") or meta.get("name") or ""
        author = meta.get("author") or ""
        desc = meta.get("description") or ""
        transcript = meta.get("transcript") or ""
        system = (
            "你是内容分析助手。用户收藏了一段短视频/文章类认知内容（如抖音、教程），"
            "需要给出结构化、克制、不说废话的多维卡片。严格只输出 JSON，不要任何解释或 Markdown。"
            "字段：domain(领域，从[AI·LLM, Web 框架, 前端/UI, DevOps/云, 数据库, CLI/工具, "
            "移动端, 安全, 数据/可视化, 知识/笔记, 未分类]选一), "
            "purpose(一句话说清这段内容讲什么/解决什么认知问题), "
            "tech_stack(涉及的技术/工具数组，最多6，没有则[]), "
            "why_needed(为什么用户可能需要它，结合备注，90字内), "
            "related_hint(基于已知领域，给1句关联方向建议), "
            "maturity(顶级/成熟/活跃/新兴/未知), "
            "tags(标签数组，最多8), recommendation(0-5整数，内容质量与相关性自评)。"
        )
        user = "标题：%s\n作者：%s\n简介：%s\n逐字稿：%s\n用户备注：%s\n%s" % (
            title, author, desc,
            (transcript[:4000] if transcript else "（无逐字稿）"),
            note or "（无）", ctx)
        fallback_domain_text = (title + " " + desc + " " + transcript)
        fallback_tech = []
        fallback_tags = None
        fallback_maturity = "未知"
        fallback_rec = 0
    else:
        name = meta.get("name") or meta.get("full_name") or ""
        desc = meta.get("description") or "（无描述）"
        lang = meta.get("language") or "未知"
        stars = meta.get("stars") or 0
        topics = ", ".join(str(t) for t in (meta.get("topics") or [])) or "无"
        system = (
            "你是仓库分析助手。用户收藏开源仓库，你需要给出结构化、克制、不说废话的多维卡片。"
            "严格只输出 JSON，不要任何解释或 Markdown 代码块标记。字段："
            "domain(领域，从[AI·LLM, Web 框架, 前端/UI, DevOps/云, 数据库, CLI/工具, 移动端, 安全, 数据/可视化, 知识/笔记, 未分类]选一), "
            "purpose(一句话说清这仓库解决什么问题), "
            "tech_stack(技术栈数组，最多6), "
            "why_needed(为什么用户可能需要它，结合备注，90字内), "
            "related_hint(基于已知领域，给1句关联方向建议), "
            "maturity(顶级/成熟/活跃/新兴/未知 + 可选' · 近一年有更新'), "
            "tags(标签数组，最多8), "
            "recommendation(0-5整数，综合热度与时效)。"
        )
        user = (
            "仓库：%s\n描述：%s\n主语言：%s\nStars：%s\nTopics：%s\n用户备注：%s\n%s"
            % (name, desc, lang, stars, topics, note or "（无）", ctx)
        )
        fallback_domain_text = _text_blob(meta)
        fallback_tech = detect_tech_stack(meta)
        fallback_tags = detect_tags(meta)
        fallback_maturity = detect_maturity(meta)
        fallback_rec = detect_recommendation(meta)

    obj = _llm_json(
        system, user, expect="dict",
        timeout=timeout if timeout is not None else config.LLM_TIMEOUT,
        retries=retries if retries is not None else config.LLM_RETRIES,
    )
    if not obj:
        return None, False
    # 规整成标准卡片结构（两类共用）
    domain = obj.get("domain") or detect_domain_text(fallback_domain_text)
    card = {
        "domain": domain,
        "purpose": obj.get("purpose") or desc,
        "tech_stack": obj.get("tech_stack") or fallback_tech,
        "why_needed": obj.get("why_needed") or (note or "（未填写备注）"),
        "related_nodes": [],  # 关联仍由本地 detect_related 计算，保证基于真实收藏
        "maturity": obj.get("maturity") or fallback_maturity,
        "tags": obj.get("tags") if obj.get("tags") is not None else (fallback_tags or [domain]),
        "recommendation": obj.get("recommendation", fallback_rec),
        "source": "llm",
    }
    try:
        card["recommendation"] = int(card["recommendation"])
    except Exception:
        card["recommendation"] = fallback_rec
    return card, True


# 已收录的领域白名单（与 GAP_COOCCUR / _llm_card 保持一致）。
KNOWN_DOMAINS = [
    "AI·LLM", "Web 框架", "前端/UI", "DevOps/云", "数据库",
    "CLI/工具", "移动端", "安全", "数据/可视化", "知识/笔记", "未分类",
]


def _llm_recommend_insight(profile_text):
    """用 LLM 基于用户画像推「还想要什么领域 + 为什么 + 置信度」。

    返回 [{domain, reason, confidence}, ...] 或 None（失败/不可用）。
    注意：LLM 只负责语义层的「推领域+理由+置信度」，真实仓库仍由 GitHub 搜索补全，
    避免模型编造不存在的仓库地址。confidence 标注样本稀疏时的可信度，防误导。
    """
    llm_mod = _load_llm()
    if llm_mod is None:
        return None
    if not profile_text or not profile_text.strip():
        return None
    system = (
        "你是开源工具收藏顾问。用户有一份真实的收藏行为画像，你需要推断"
        "「他还可能想要哪类仓库」，并标注推荐置信度。严格只输出 JSON 数组，"
        "不要任何解释或 Markdown 代码块。数组每项："
        "{domain(从已知领域选一), reason(为什么基于他的画像推这个类，60字内，口语、不说废话), "
        "confidence(高/中/低，样本稀疏时务必给低)}。"
        "已知领域：" + "、".join(KNOWN_DOMAINS) + "。"
        "约束：只推用户画像里没有出现、但与其收藏有逻辑关联的领域；"
        "若画像领域极少（≤2类），confidence 不得超过「中」；最多推 4 个。"
    )
    user = "用户收藏画像：\n" + profile_text.strip()
    obj = _llm_json(system, user, expect="list")
    if not obj:
        return None
    out = []
    for it in obj:
        if not isinstance(it, dict):
            continue
        dom = it.get("domain")
        if dom not in KNOWN_DOMAINS or dom == "未分类":
            continue
        conf = it.get("confidence")
        if conf not in ("高", "中", "低"):
            conf = "低"
        out.append({
            "domain": dom,
            "reason": (it.get("reason") or "").strip()[:80],
            "confidence": conf,
        })
    # 去重（按 domain）
    seen, uniq = set(), []
    for x in out:
        if x["domain"] in seen:
            continue
        seen.add(x["domain"])
        uniq.append(x)
    return uniq[:4] if uniq else None


# 领域关键词表：topic / 描述词 / 中文语义 → 领域。命中越多越优先。
# 后半段补的中文词，是为了让抖音/文章这类「认知端」中文逐字稿也能落到领域轴。
DOMAIN_KEYWORDS = {
    "AI·LLM": ["llm", "gpt", "transformer", "diffusion", "agent", "rag",
               "embedding", "machine-learning", "deep-learning", "nlp",
               "chatbot", "stable-diffusion", "pytorch", "tensorflow",
               "langchain", "prompt", "fine-tune", "inference",
               "人工智能", "大模型", "智能体", "机器学习", "深度学习",
               "自然语言", "语音识别", "神经网络", "gpt", "chatgpt", "ai"],
    "Web 框架": ["web", "framework", "flask", "django", "fastapi", "express",
                 "spring", "koa", "rest", "http-server", "backend",
                 "web-server", "middleware",
                 "后端", "服务端", "接口", "网站", "服务器"],
    "前端/UI": ["react", "vue", "svelte", "component", "ui", "css", "tailwind",
                "design-system", "component-library", "frontend", "spa",
                "前端", "界面", "组件", "设计", "交互", "网页", "app"],
    "DevOps/云": ["docker", "kubernetes", "k8s", "ci", "cd", "devops",
                  "terraform", "ansible", "helm", "cloud", "serverless",
                  "infrastructure", "monitoring", "observability",
                  "部署", "运维", "云", "自动化", "容器"],
    "数据库": ["database", "sql", "postgres", "mysql", "sqlite", "mongodb",
               "redis", "orm", "vector-db", "embedded-db", "oltp",
               "数据库", "存储", "缓存", "索引"],
    "CLI/工具": ["cli", "command-line", "terminal", "tool", "util", "utils",
                 "scaffold", "boilerplate", "dotfiles",
                 "命令行", "工具", "脚本", "插件"],
    "移动端": ["android", "ios", "react-native", "flutter", "mobile",
               "swift", "kotlin", "jetpack",
               "安卓", "手机", "app", "小程序"],
    "安全": ["security", "auth", "crypto", "encryption", "pentest",
             "vulnerability", "jwt", "oauth", "iam",
             "安全", "加密", "隐私", "权限", "防护"],
    "数据/可视化": ["data", "visualization", "chart", "dashboard", "analytics",
                   "etl", "pandas", "plot", "bi", "report",
                   "数据", "可视化", "图表", "分析", "报表", "看板"],
    "知识/笔记": ["knowledge", "note", "notebook", "wiki", "second-brain",
                  "pkm", "markdown", "docs", "documentation",
                  "笔记", "知识", "学习", "教程", "读书", "复盘", "方法论"],
}

# 非技术型、仅作修饰的 topic，不进 tech_stack 展示
SOFT_TOPICS = {"awesome", "tutorial", "example", "demo", "beginner",
               "learning", "resources", "list", "collection", "tool",
               "library", "api", "open-source", "opensource"}


def _text_blob(meta):
    parts = []
    if meta.get("language"):
        parts.append(meta["language"].lower())
    if meta.get("topics"):
        parts.extend([str(t).lower() for t in meta["topics"]])
    if meta.get("description"):
        parts.append(meta["description"].lower())
    return " ".join(parts)


def detect_domain_text(text):
    """对任意文本做领域关键词打分（中文逐字稿 / 英文描述都适用）。"""
    blob = (text or "").lower()
    if not blob:
        return "未分类"
    scores = {}
    for domain, kws in DOMAIN_KEYWORDS.items():
        hit = sum(1 for k in kws if k in blob)
        if hit:
            scores[domain] = hit
    if not scores:
        return "未分类"
    return max(scores, key=scores.get)


def detect_domain(meta):
    """从仓库 meta 的英文描述/语言/topics 推断领域（生产端）。"""
    return detect_domain_text(_text_blob(meta))


def detect_tags(meta):
    tags = []
    low = set()
    domain = detect_domain(meta)
    if domain != "未分类":
        tags.append(domain)
        low.add(domain.lower())
    lang = meta.get("language")
    if lang:
        tags.append(lang)
        low.add(lang.lower())
    for t in (meta.get("topics") or []):
        t = str(t)
        if t.lower() in SOFT_TOPICS:
            continue
        if t.lower() in low:        # 大小写去重，避免 Python / python 同轴重复
            continue
        tags.append(t)
        low.add(t.lower())
    # 限长，便于图谱分面
    return tags[:8]


# ---------- 技术栈取值归一化 ----------
# 分工：卡片上的 tech_stack 保留原始值（细，给人看）；写入轴的取值做归一化（粗，给聚合用）。
# 不归一化的话，同一个项目自己打的一串嵌套 topic（dsh / dsh-plugin / dsh-plugin-market）
# 会把筛选胶囊云塞满，看不出真实分布。
TECH_ALIASES = {
    "js": "javascript", "ts": "typescript", "py": "python",
    "node": "nodejs", "node-js": "nodejs", "golang": "go",
    "k8s": "kubernetes", "postgres": "postgresql", "postgre": "postgresql",
    "vuejs": "vue", "reactjs": "react", "nextjs": "next-js",
    "tf": "tensorflow", "sklearn": "scikit-learn", "scikit": "scikit-learn",
    "c++": "cpp", "c#": "csharp", "objective-c": "objc",
}

_TECH_SEP = re.compile(r"[\s_.]+")
_TECH_DASH = re.compile(r"-{2,}")


def normalize_tech(value):
    """统一成可比较的形态：小写 + 分隔符归一 + 已知别名。"""
    v = str(value or "").strip().lower()
    if not v:
        return ""
    v = _TECH_SEP.sub("-", v).strip("-")
    v = _TECH_DASH.sub("-", v)
    return TECH_ALIASES.get(v, v)


def collapse_tech_variants(values):
    """把同一次归一化后「层层嵌套的前缀变体」合并到最短的那个。

    例：dsh / dsh-plugin / dsh-plugin-market → dsh；deepseek / deepseek-harness → deepseek。
    **只在同一个仓库内部合并**：跨仓库合并等于把不同东西捏到一起，那是编数据。
    细粒度信息没有丢 —— 卡片上的 tech_stack 仍是原始值，轴只负责聚合。
    """
    norm, seen = [], set()
    for v in values:
        v = normalize_tech(v)
        if not v or v in seen:
            continue
        seen.add(v)
        norm.append(v)
    return [v for v in norm
            if not any(o != v and v.startswith(o + "-") for o in norm)]


def detect_tech_stack(meta):
    stack = []
    low = set()
    lang = meta.get("language")
    if lang:
        stack.append(lang)
        low.add(lang.lower())
    for t in (meta.get("topics") or []):
        t = str(t)
        if t.lower() in SOFT_TOPICS:
            continue
        if t.lower() in low:
            continue
        stack.append(t)
        low.add(t.lower())
    return stack[:6]


def detect_maturity(meta):
    stars = meta.get("stars")
    try:
        stars = int(stars) if stars is not None else 0
    except Exception:
        stars = 0
    if stars >= 20000:
        tier = "顶级"
    elif stars >= 5000:
        tier = "成熟"
    elif stars >= 500:
        tier = "活跃"
    elif stars > 0:
        tier = "新兴"
    else:
        tier = "未知"
    fresh = ""
    pushed = meta.get("pushed_at")
    if pushed:
        try:
            d = datetime.datetime.fromisoformat(pushed.replace("Z", ""))
            if (datetime.datetime.now() - d).days <= 365:
                fresh = "近一年有更新"
        except Exception:
            pass
    return tier + ((" · " + fresh) if fresh else "")


def detect_recommendation(meta):
    stars = meta.get("stars")
    try:
        stars = int(stars) if stars is not None else 0
    except Exception:
        stars = 0
    score = 0
    if stars >= 20000:
        score = 5
    elif stars >= 5000:
        score = 4
    elif stars >= 1000:
        score = 3
    elif stars >= 100:
        score = 2
    elif stars > 0:
        score = 1
    pushed = meta.get("pushed_at")
    if pushed:
        try:
            d = datetime.datetime.fromisoformat(pushed.replace("Z", ""))
            if (datetime.datetime.now() - d).days > 730:
                score = max(0, score - 1)  # 久未更新略降
        except Exception:
            pass
    return score


def detect_related(meta, note, item_id, conn, domain=None):
    """弱协同关联：同领域或共享标签的已有收藏（排除自身）。

    domain 可由调用方显式传入（认知端用逐字稿算出的领域），否则从 meta 推断。
    显式传入是跨类型碰撞的关键 —— 抖音视频与 GitHub 仓库只要同处一个领域轴，
    就能在这里被关联起来。
    """
    if domain is None:
        domain = detect_domain(meta)
    my_tags = set(t.lower() for t in detect_tags(meta))
    c = conn.cursor()
    c.execute("SELECT id,url,meta,card FROM repos WHERE id<>? AND meta IS NOT NULL", (item_id,))
    rows = c.fetchall()
    related = []
    for rid, url, rmeta_s, rcard_s in rows:
        try:
            rmeta = json.loads(rmeta_s) if rmeta_s else {}
        except Exception:
            rmeta = {}
        # 优先用对方已算好的卡片 domain，否则即时算
        rdomain = "未分类"
        rtags = []
        if rcard_s:
            try:
                rc = json.loads(rcard_s)
                rdomain = rc.get("domain", "未分类")
                rtags = [t.lower() for t in rc.get("tags", [])]
            except Exception:
                pass
        if rdomain == "未分类":
            rdomain = detect_domain(rmeta)
            rtags = [t.lower() for t in detect_tags(rmeta)]
        reason = None
        if rdomain != "未分类" and rdomain == domain:
            reason = "同属「" + domain + "」"
        else:
            shared = my_tags & set(rtags)
            if shared:
                reason = "共享标签：" + "/".join(sorted(shared)[:2])
        if reason:
            name = rmeta.get("name") or url
            related.append({"id": rid, "url": url, "name": name, "reason": reason})
        if len(related) >= 5:
            break
    return related


def _llm_related(cur_name, cur_desc, cur_tags, cur_note, candidates,
                 timeout=None, retries=None):
    """用 LLM 基于语义（不限同领域）判断哪些已有收藏与当前仓库真正相关。

    返回 [{id, reason}, ...] 或 None（失败/不可用/候选不足）。
    比 detect_related 的硬标签匹配更智能：能发现跨领域的语义关联
    （如「插件市场型 CLI」与「自动化工作流」虽领域不同但语义相邻）。
    """
    llm_mod = _load_llm()
    if llm_mod is None or not candidates:
        return None
    cand_txt = []
    for c in candidates[:20]:  # 控制 prompt 体量
        cand_txt.append("- id=%s | %s | 领域:%s | 标签:%s | %s" % (
            c.get("id"), c.get("name"), c.get("domain", "未分类"),
            "、".join(c.get("tags", []) or []), (c.get("desc") or "")[:80]))
    cands = "\n".join(cand_txt)
    system = (
        "你是收藏关联分析器。用户刚收藏一个新仓库，需要从他已有的收藏里找出"
        "真正相关的（语义、用途、可组合、互补、同作者生态等，不限同一领域）。"
        "严格只输出 JSON 数组，不要解释或 Markdown。每项："
        "{id(已有收藏的 id), reason(为什么相关，口语、30字内、说清关联点)}。"
        "最多返回 5 个最相关的；没有明显相关的返回空数组 []。"
    )
    user = (
        "新收藏：%s\n描述：%s\n标签：%s\n用户备注：%s\n\n已有收藏：\n%s"
        % (cur_name, (cur_desc or "")[:160], "、".join(cur_tags or []), cur_note or "（无）", cands)
    )
    obj = _llm_json(system, user, expect="list",
                    timeout=timeout, retries=retries)
    if not obj:
        return None
    out = []
    seen = set()
    for it in obj:
        if not isinstance(it, dict):
            continue
        rid = it.get("id")
        if rid in seen or rid is None:
            continue
        reason = (it.get("reason") or "").strip()[:60]
        if not reason:
            continue
        out.append({"id": rid, "reason": reason})
        seen.add(rid)
    return out if out else None


def _llm_note_axes(note, meta):
    """用 LLM 从用户备注（及仓库上下文）提炼动机/使用场景轴。

    返回 {motive, scene} 或 None（失败/无备注）。
    motive：用户为什么想要它（一句话，如「主动寻找」「替代现有工具」「学习参考」）
    scene：典型使用场景（如「做项目」「学习研究」「日常效率」「调研选型」）
    比 sync_axes 里硬关键词映射更准——真正读懂用户的话。
    """
    llm_mod = _load_llm()
    if llm_mod is None or not note or not note.strip():
        return None
    name = (meta or {}).get("name") or (meta or {}).get("full_name") or ""
    desc = (meta or {}).get("description") or ""
    system = (
        "你是收藏意图理解器。用户给了一个仓库备注，需要提炼他的收藏动机与使用场景。"
        "严格只输出 JSON，不要解释或 Markdown。字段："
        "motive(用户为什么想要它，4-10字，如『主动寻找』『替代现有工具』『学习参考』『临时调研』), "
        "scene(典型使用场景，4-10字，如『做项目』『学习研究』『日常效率』『选型对比』)。"
        "只依据备注与仓库客观信息推断，不编造。"
    )
    user = "仓库：%s\n描述：%s\n用户备注：%s" % (name, desc[:120], note.strip())
    obj = _llm_json(system, user, expect="dict")
    if not obj:
        return None
    motive = (obj.get("motive") or "").strip()[:12]
    scene = (obj.get("scene") or "").strip()[:12]
    if not motive and not scene:
        return None
    return {"motive": motive or None, "scene": scene or None}


def _related_for(meta, note, item_id, conn, timeout=None, retries=None, domain=None):
    """关联节点：LLM 语义优先，规则回退。保证基于用户真实收藏。

    domain 显式传入时（认知端），规则回退会按该领域去匹配已有收藏，
    实现「抖音讲前端 → 你收藏的前端仓库」这类跨类型碰撞。
    """
    if not conn:
        return []
    # 取候选（已有收藏，排除自身）
    cc = conn.cursor()
    cc.execute("SELECT id,url,meta,card FROM repos WHERE id<>? AND meta IS NOT NULL", (item_id,))
    cands = []
    for rid, url, rmeta_s, rcard_s in cc.fetchall():
        try:
            rmeta = json.loads(rmeta_s) if rmeta_s else {}
        except Exception:
            rmeta = {}
        rdom = "未分类"; rtags = []
        if rcard_s:
            try:
                rc = json.loads(rcard_s)
                rdom = rc.get("domain", "未分类")
                rtags = rc.get("tags", [])
            except Exception:
                pass
        if rdom == "未分类":
            rdom = detect_domain(rmeta); rtags = detect_tags(rmeta)
        cands.append({
            "id": rid, "name": rmeta.get("name") or url, "url": url,
            "domain": rdom, "tags": rtags, "desc": rmeta.get("description") or "",
        })
    if not cands:
        return []
    # LLM 语义关联（失败回退规则）
    llm_rel = _llm_related(
        (meta or {}).get("name") or (meta or {}).get("full_name") or "",
        (meta or {}).get("description") or "",
        detect_tags(meta or {}), note, cands,
        timeout=timeout, retries=retries)
    if llm_rel:
        # 把 LLM 返回的 id+reason 映射成完整节点（带 url/name）。
        # 注意：LLM 可能把 id 输出为字符串，统一转 int 再查。
        by_id = {c["id"]: c for c in cands}
        nodes = []
        for r in llm_rel:
            try:
                rid = int(r["id"])
            except (TypeError, ValueError):
                continue
            c = by_id.get(rid)
            if not c:
                continue
            nodes.append({"id": rid, "url": c.get("url") or "",
                          "name": c["name"], "reason": r["reason"]})
        if nodes:
            return nodes[:5]
    return detect_related(meta, note, item_id, conn, domain=domain)


def _kind_of_meta(meta):
    """从 meta 推断收藏类型：抖音 / 网页文章等认知端内容为 cognition，其余为 production。"""
    return "cognition" if (meta or {}).get("platform") in ("douyin", "web") else "production"


def build_card(meta, note, item_id, conn, timeout=None, retries=None, kind=None):
    """生成多维分析卡片。优先 LLM（灯笼 llm 模块），失败回退启发式。

    kind='production'（GitHub 仓库）与 kind='cognition'（抖音/文章）共用同一套卡片结构；
    区别在于输入字段与领域推断的语料（认知端用中文逐字稿，生产端用英文 meta）。

    timeout/retries: 交互链路（用户点「重算」）传短预算（config.LLM_TIMEOUT_FAST），
    让后端比前端 12s 超时先放弃 —— 用户看到的是「已保留原卡片」而不是「超时」。
    后台 worker 不传，走 config 里的默认（可以慢，但不能无限等）。
    """
    meta = meta or {}
    kind = kind or _kind_of_meta(meta)
    # 1) 先试 LLM（复用灯笼 key 配置）
    llm_card, ok = _llm_card(meta, note, item_id, conn, timeout=timeout, retries=retries, kind=kind)
    if ok and llm_card:
        # 关联节点：LLM 语义优先（跨领域），规则回退（按本卡片领域做跨类型碰撞）
        llm_card["related_nodes"] = _related_for(
            meta, note, item_id, conn, timeout=timeout, retries=retries,
            domain=llm_card.get("domain"))
        return llm_card
    # 2) 回退：启发式规则卡片
    if kind == "cognition":
        title = meta.get("title") or meta.get("name") or ""
        desc = meta.get("description") or ""
        transcript = meta.get("transcript") or ""
        text = " ".join([title, desc, transcript])
        domain = detect_domain_text(text)
        purpose = desc or (transcript[:120] if transcript else "（暂无内容，可补充备注说明）")
        tech_stack = []
        tags = [domain] if domain != "未分类" else []
        maturity = "未知"
        recommendation = 0
        related = _related_for(meta, note, item_id, conn, timeout=timeout,
                               retries=retries, domain=domain)
        why = note or ("这段「" + domain + "」内容：" + purpose)
        return {
            "domain": domain, "purpose": purpose, "tech_stack": tech_stack,
            "why_needed": why, "related_nodes": related, "maturity": maturity,
            "tags": tags, "recommendation": recommendation, "source": "heuristic",
        }
    # 生产端启发式
    domain = detect_domain(meta)
    purpose = meta.get("description") or "（暂无描述，可补充备注说明用途）"
    tech_stack = detect_tech_stack(meta)
    tags = detect_tags(meta)
    maturity = detect_maturity(meta)
    recommendation = detect_recommendation(meta)
    related = _related_for(meta, note, item_id, conn, timeout=timeout, retries=retries, domain=domain)

    if note:
        why = note
    else:
        why = "你收藏了「" + domain + "」领域的 " + (meta.get("name") or "该仓库") + \
              "，可用于：" + purpose
    return {
        "domain": domain,
        "purpose": purpose,
        "tech_stack": tech_stack,
        "why_needed": why,
        "related_nodes": related,
        "maturity": maturity,
        "tags": tags,
        "recommendation": recommendation,
        "source": "heuristic",
    }
