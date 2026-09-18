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
import db
import llm_client

# 内置远程模型优先；灯笼模块作为回退（未在本应用内配置模型时复用）。
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


def _llm_ready():
    """是否至少有一种可用后端（内置远程模型 或 灯笼模块）。

    各 LLM 调用点在动手前用它判断要不要走规则分支。
    _llm_chat 才真正决定走哪条后端，这里只管「有没有得用」。
    """
    return llm_client.configured() or _load_llm() is not None


def llm_status():
    """给 /api/doctor 用：当前 LLM 是否可用、走哪条后端、复用的模块/上游熔断。

    只报告事实，不做降级决策 —— 降级是各调用点自己的事。
    """
    use_remote = llm_client.configured()
    mod = _load_llm() if not use_remote else None
    st = {
        "root": LANTERN_ROOT,
        "module_loaded": bool(mod),
        "available": bool(use_remote or mod),
        "backend": "builtin_remote" if use_remote else ("lantern" if mod else "off"),
    }
    if use_remote:
        conn = db.connect()
        st["base_url"] = db.get_settings(conn, "llm_base_url") or ""
        st["model"] = db.get_settings(conn, "llm_model") or ""
        st["has_key"] = bool(db.get_settings(conn, "llm_api_key"))
        conn.close()
    else:
        st["base_url"] = None
        st["model"] = None
        st["has_key"] = None
        if mod is not None and hasattr(mod, "breaker_state"):
            try:
                st["breaker"] = mod.breaker_state()
            except Exception:
                pass
    return st


def _llm_chat(system, user, timeout=None, retries=None, max_tokens=None, use_cache=None):
    """全项目唯一的 llm.chat 调用点：懒加载 → 调用 → 解包 content。

    返回字符串，或 None（模块不可用 / 抛异常 / 空返回）。
    为什么集中：改造前 4 个调用点各写一遍 try/except + 元组解包，且都没传超时，
    一旦上游接口半死，worker 线程会一直挂着。超时预算现在统一从 config 来。

    max_tokens/use_cache：llm.chat 默认 max_tokens=600，长输出（如逐字稿结构化笔记）
    会被从中间硬截断 —— 需要长输出的调用点必须显式调大。另外 llm.chat 的缓存 key
    不含 max_tokens，改大后必须 use_cache=False，否则会命中旧的截断结果。

    优先级：内置远程模型（本应用内配置）> 灯笼模块回退 > None。
    """
    # ① 配置了内置远程模型 → 用它。失败/空输出同样返回 None（降级到规则）。
    if llm_client.configured():
        return llm_client.chat(system, user, timeout=timeout, max_tokens=max_tokens)
    # ② 否则回退灯笼模块。
    llm_mod = _load_llm()
    if llm_mod is None:
        return None
    kw = {}
    if timeout is not None:
        kw["timeout"] = timeout
    if retries is not None:
        kw["retries"] = retries
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    if use_cache is not None:
        kw["use_cache"] = use_cache
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
    if not _llm_ready():
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
        _example_c = json.dumps(
            {"domain": "知识/笔记", "purpose": "用卡片法系统整理读书笔记，建立知识关联",
             "tech_stack": [], "why_needed": "想把零散的读书摘抄沉淀成可检索的知识网",
             "maturity": "活跃", "tags": ["读书", "笔记法", "学习方法"], "recommendation": 4},
            ensure_ascii=False)
        system = (
            "你是内容分析助手。用户收藏了一段短视频/文章类认知内容（如抖音、教程），"
            "需要给出结构化、克制、不说废话的多维卡片。严格只输出 JSON，不要任何解释或 Markdown。"
            "字段：domain(领域，从[%s]选最接近的一个；仅当完全无法归类时才用「未分类」，不要偷懒用它), "
            "purpose(一句话说清这段内容讲什么/解决什么认知问题), "
            "tech_stack(涉及的技术/工具数组，最多6，没有则[]), "
            "why_needed(为什么用户可能需要它；用户备注是其真实意图，必须优先参考，90字内), "
            "maturity(顶级/成熟/活跃/新兴/未知), "
            "tags(标签数组，最多8), recommendation(0-5整数，内容质量与相关性自评)。"
            "\n示例：一条讲「卡片笔记法」的抖音视频 → " + _example_c
        ) % "、".join(KNOWN_DOMAINS)
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
        _example = json.dumps(
            {"domain": "知识/笔记", "purpose": "本地优先的个人知识库，自动分类与关联收藏",
             "tech_stack": ["Python", "SQLite"],
             "why_needed": "想给自己的收藏做结构化沉淀，避免收藏即遗忘",
             "maturity": "新兴 · 近一年有更新",
             "tags": ["知识管理", "个人工具", "Python"], "recommendation": 3},
            ensure_ascii=False)
        system = (
            "你是仓库分析助手。用户收藏开源仓库，你需要给出结构化、克制、不说废话的多维卡片。"
            "严格只输出 JSON，不要任何解释或 Markdown 代码块标记。字段："
            "domain(领域，从[%s]选最接近的一个；仅当完全无法归类时才用「未分类」，不要偷懒用它), "
            "purpose(一句话说清这仓库解决什么问题), "
            "tech_stack(技术栈数组，最多6), "
            "why_needed(为什么用户可能需要它；用户备注是其真实意图，必须优先参考，90字内), "
            "maturity(顶级/成熟/活跃/新兴/未知 + 可选' · 近一年有更新'，可结合 Stars 与更新时间判断), "
            "tags(标签数组，最多8), "
            "recommendation(0-5整数，综合热度与时效)。"
            "\n示例：仓库「lantern-caliper」描述「个人双尺度知识库，自带引擎」→ " + _example
        ) % "、".join(KNOWN_DOMAINS)
        pushed = meta.get("pushed_at") or "未知"
        user = (
            "仓库：%s\n描述：%s\n主语言：%s\nStars：%s\nTopics：%s\n最近更新：%s\n用户备注：%s\n%s"
            % (name, desc, lang, stars, topics, pushed, note or "（无）", ctx)
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
    raw_domain = obj.get("domain")
    domain = raw_domain if raw_domain and raw_domain != "未分类" else detect_domain_text(fallback_domain_text)
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
    "CLI/工具", "移动端", "安全", "数据/可视化", "知识/笔记",
    "生活/兴趣", "理财/投资", "历史/人文", "未分类",
]


def _llm_recommend_insight(profile_text):
    """用 LLM 基于用户画像推「还想要什么领域 + 为什么 + 置信度」。

    返回 [{domain, reason, confidence}, ...] 或 None（失败/不可用）。
    注意：LLM 只负责语义层的「推领域+理由+置信度」，真实仓库仍由 GitHub 搜索补全，
    避免模型编造不存在的仓库地址。confidence 标注样本稀疏时的可信度，防误导。
    """
    if not _llm_ready():
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
                  "笔记", "知识", "学习", "教程", "读书", "复盘", "方法论",
                  "收藏", "认知", "知识管理", "个人工具"],
    "生活/兴趣": ["生活", "日常", "美食", "旅行", "健康", "健身", "兴趣", "爱好",
                  "家居", "宠物", "lifestyle", "food", "travel", "health", "fitness"],
    "理财/投资": ["理财", "投资", "股票", "基金", "crypto", "区块链", "比特币",
                  "财富", "储蓄", "保险", "finance", "invest", "trading", "bitcoin"],
    "历史/人文": ["历史", "人文", "文化", "哲学", "社会", "心理", "传记",
                  "考古", "宗教", "history", "philosophy", "psychology", "culture"],
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


def _candidate_rows(conn, item_id):
    """已有收藏候选快照（排除自身），meta/card 各 json.loads 一次，容忍损坏。

    供 detect_related / _related_for 复用，避免两条路径各自重复全表读 +
    逐行 JSON 解析。不做跨调用的全局缓存：worker 并发收藏会串数据并引入竞争。
    """
    c = conn.cursor()
    c.execute("SELECT id,url,meta,card FROM repos WHERE id<>? AND meta IS NOT NULL", (item_id,))
    out = []
    for rid, url, rmeta_s, rcard_s in c.fetchall():
        try:
            rmeta = json.loads(rmeta_s) if rmeta_s else {}
        except Exception:
            rmeta = {}
        try:
            rcard = json.loads(rcard_s) if rcard_s else None
        except Exception:
            rcard = None
        out.append({"id": rid, "url": url, "meta": rmeta, "card": rcard})
    return out


def detect_related(meta, note, item_id, conn, domain=None):
    """弱协同关联：同领域或共享标签的已有收藏（排除自身）。

    domain 可由调用方显式传入（认知端用逐字稿算出的领域），否则从 meta 推断。
    显式传入是跨类型碰撞的关键 —— 抖音视频与 GitHub 仓库只要同处一个领域轴，
    就能在这里被关联起来。
    """
    if domain is None:
        domain = detect_domain(meta)
    my_tags = set(t.lower() for t in detect_tags(meta))
    related = []
    for it in _candidate_rows(conn, item_id):
        rid, url, rmeta, rcard = it["id"], it["url"], it["meta"], it["card"]
        # 优先用对方已算好的卡片 domain，否则即时算
        rdomain = "未分类"
        rtags = []
        if rcard:
            rdomain = rcard.get("domain", "未分类")
            rtags = [t.lower() for t in rcard.get("tags", [])]
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
    if not _llm_ready() or not candidates:
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
    if not _llm_ready() or not note or not note.strip():
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
    # 取候选（已有收藏，排除自身）——快照统一读取/解析，避免重复全表扫 + 逐行 JSON
    cands = []
    for it in _candidate_rows(conn, item_id):
        rmeta, rcard = it["meta"], it["card"]
        rdom = "未分类"; rtags = []
        if rcard:
            rdom = rcard.get("domain", "未分类")
            rtags = rcard.get("tags", [])
        if rdom == "未分类":
            rdom = detect_domain(rmeta); rtags = detect_tags(rmeta)
        cands.append({
            "id": it["id"], "name": rmeta.get("name") or it["url"], "url": it["url"],
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


# ═══════════════════════════════════════════════════════════════════
#  逐字稿 → 结构化 markdown 笔记
# ═══════════════════════════════════════════════════════════════════
TRANSCRIPT_MD_SYSTEM = """你是知识整理助手。把用户给的口语逐字稿整理成结构化 markdown 笔记。

硬性要求：
- 只依据逐字稿原文，绝不编造原文没有的信息、数字或结论。
- **必须覆盖逐字稿的全部内容，从开头到结尾**：宁可把后半段写得更凝练，也绝不能中途停笔、不能只整理前半段。
- 输出纯 markdown，不要用 ``` 代码围栏包裹。
- 结构：# <内容主题做标题>（不要用"逐字稿"当标题）→ ## 概述（1-3 句）→ ## 核心要点（3-8 条「-」列表）→ ## 内容纪要（按内容推进分 ### 小节，每节用要点或精炼短句复述，小节要能看出全文脉络与结论）。
- 保留关键人名/机构/数字/结论；删除口头禅、语气词与重复啰嗦，但不得丢失关键信息。
- 全文简体中文，篇幅以把内容讲全为准（通常 1200-3000 字）。"""

# 超长逐字稿分块整理时用：每块只要片段纪要，避免每块都重复标题/概述
TRANSCRIPT_MD_CHUNK_SYSTEM = """你是知识整理助手。用户会分块给出一份长逐字稿。
请只针对「本次给到的这一段」输出 markdown 纪要：
- 用 ### 小节 + 「-」要点，忠实复述本段内容，保留人名/机构/数字/结论。
- 不要写整体标题、不要写「概述」或「核心要点」，不要臆测其它段落的内容。
- 只依据本段原文，不编造；输出纯 markdown，不要 ``` 围栏。"""

# 分块纪要合并后，补写总览（概述 + 核心要点）
TRANSCRIPT_MD_HEAD_SYSTEM = """你是知识整理助手。下面是同一份长逐字稿的多个分段纪要。
请只依据这些分段纪要，输出两节 markdown：
## 概述（1-3 句，概括整份内容）
## 核心要点（3-8 条「-」列表，覆盖全文主线）
只输出这两节，不要重复各段纪要的细节，不要编造原文没有的信息。"""


def _strip_fence(s):
    """剥掉 LLM 可能残留的 ``` 围栏（含语言行），返回纯 markdown。"""
    s = (s or "").strip()
    if s.startswith("```"):
        s = s[3:]
        nl = s.find("\n")
        if nl != -1 and nl < 12:          # 第一行是语言标记（md/json/markdown）
            s = s[nl + 1:]
        s = s.rstrip("`").strip()
    return s


def _split_transcript(transcript, size):
    """按句末标点把逐字稿切成每块 <=size 的块，绝不从句子中间切断。"""
    text = (transcript or "").strip()
    if len(text) <= size:
        return [text]
    # 先按句末标点 / 换行切句，再贪心聚合成块
    sentences = [s for s in re.split(r"(?<=[。！？!?\n])", text) if s]
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > size:
            chunks.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        chunks.append(cur)
    return chunks


def _heuristic_transcript_md(transcript):
    """无 LLM 时的兜底：把流水句按句末标点切分，三句一段，套 markdown 标题。

    纯字符串处理、零依赖、确定性 —— 保证「结构化展示」在 LLM 不可用时仍有可用形态。
    """
    flat = re.sub(r"\s+", "", transcript or "")
    if not flat:
        return None
    sentences = [s for s in re.split(r"(?<=[。！？!?])", flat) if s]
    paras = ["".join(sentences[i:i + 3]) for i in range(0, len(sentences), 3)]
    return "## 逐字稿\n\n" + "\n\n".join(paras)


def build_transcript_md(transcript, meta=None, timeout=None, retries=None, max_tokens=None):
    """把口语逐字稿整理成结构化 markdown 笔记（完整、通顺，绝不做字数硬截断）。

    - 短/中篇（<= TRANSCRIPT_MD_SINGLE_MAX）：一次成稿。
    - 超长：按句切块 → 每块纪要 → 补总览 → 合并，保证篇末内容不丢。
    - LLM 不可用/失败：回退启发式分段（仍是全文，不截断）。

    max_tokens 必须显式调大：llm.chat 默认 600，会把笔记从中间硬截断；
    且其缓存 key 不含 max_tokens，所以一并 use_cache=False 避免命中旧的截断结果。
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return None
    title = ((meta or {}).get("title") or "").strip()
    mt = max_tokens or getattr(config, "TRANSCRIPT_MD_MAX_TOKENS", 4000)
    to = timeout if timeout is not None else getattr(config, "TRANSCRIPT_MD_TIMEOUT", 60)
    single_max = getattr(config, "TRANSCRIPT_MD_SINGLE_MAX", 30000)
    chunks = _split_transcript(transcript, single_max)
    # 1) 常规长度：一次成稿
    if len(chunks) == 1:
        user = ("标题：%s\n\n逐字稿：\n%s" % (title[:200], transcript)).strip()
        out = _strip_fence(_llm_chat(TRANSCRIPT_MD_SYSTEM, user, timeout=to, retries=retries,
                                     max_tokens=mt, use_cache=False))
        return out or _heuristic_transcript_md(transcript)
    # 2) 超长：分块纪要（每块都覆盖），任一块失败也不丢其余内容
    parts = []
    for i, ch in enumerate(chunks):
        seg = _strip_fence(_llm_chat(
            TRANSCRIPT_MD_CHUNK_SYSTEM,
            "（第 %d/%d 段）\n\n%s" % (i + 1, len(chunks), ch),
            timeout=to, retries=retries, max_tokens=mt, use_cache=False))
        if seg:
            parts.append(seg)
    if not parts:
        return _heuristic_transcript_md(transcript)
    # 补一节总览（概述 + 要点），让合并稿依然通顺；失败则省略该节
    head = _strip_fence(_llm_chat(TRANSCRIPT_MD_HEAD_SYSTEM, "\n\n".join(parts),
                                  timeout=to, retries=retries,
                                  max_tokens=min(mt, 1500), use_cache=False))
    body = "# %s\n\n" % (title or "逐字稿纪要")
    if head:
        body += head + "\n\n"
    body += "## 内容纪要\n\n" + "\n\n".join(parts)
    return body


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
    # 逐字稿结构化笔记由 worker 在后台生成（预算充足）后放进 meta.transcript_md，这里只做透传：
    # 交互端「重算」预算很短，不能在此重新生成，否则会把已生成的完整笔记降级成启发式版本。
    tmd = meta.get("transcript_md") if kind == "cognition" else None
    # 1) 先试 LLM（复用灯笼 key 配置）
    llm_card, ok = _llm_card(meta, note, item_id, conn, timeout=timeout, retries=retries, kind=kind)
    if ok and llm_card:
        # 关联节点：LLM 语义优先（跨领域），规则回退（按本卡片领域做跨类型碰撞）
        llm_card["related_nodes"] = _related_for(
            meta, note, item_id, conn, timeout=timeout, retries=retries,
            domain=llm_card.get("domain"))
        if tmd:
            llm_card["transcript_md"] = tmd
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
            "transcript_md": tmd,
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
