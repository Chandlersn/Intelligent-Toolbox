"""网页 / 文章（认知端）来源模块 —— 多源收藏的第四类入口。

设计取舍（与项目铁律对齐）：
- 零依赖：仅用标准库 urllib + html 解析抓取页面、抽取标题 / 简介 / 正文片段。
- 与抖音同属「认知端（cognition）」：抓来的网页 / 文章是用户摄入的内容，
  与 GitHub 仓库（生产端）在共同领域轴上碰撞 —— 这正是「学」与「做」的接线。
- 正文片段写入 meta.transcript，复用认知端卡片管线（中文 → 领域推断 / 萃取），
  不另起一套。
- 抓取失败优雅降级：标题取域名 / 路径，正文留空，仍落一张基础卡片，不抛异常。
"""

import re
import urllib.error
import urllib.parse
import urllib.request

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


def _decode_entities(s):
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'"))


def _meta_lookup(html):
    """抽出所有 <meta> 的 property/name → content 映射（属性顺序无关，比定点正则稳）。"""
    out = {}
    for tag in _META_RE.findall(html):
        attrs = dict(re.findall(r'(\w+)\s*=\s*["\'](.*?)["\']', tag, re.IGNORECASE))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs:
            out.setdefault(key, _decode_entities(attrs["content"]))
    return out


def _title_tag(html):
    m = _TITLE_RE.search(html)
    return _decode_entities(m.group(1).strip()) if m else ""


def _clean_text(html):
    html = _SCRIPT_RE.sub(" ", html)
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"&nbsp;", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:3000]


def collect_web(url, fetch_fn=None):
    """抓取网页 → (meta, raw_text)。

    meta：标准结构（name/title/description/url/platform + transcript 正文片段）
    raw_text：正文片段（与抖音逐字稿同位置，复用认知端分支出卡）；抓取失败为 None。
    fetch_fn 可注入（测试用假页面），否则走标准库 urllib。
    """
    fetch_fn = fetch_fn or _get
    try:
        html = fetch_fn(url)
    except Exception:
        # 抓取失败：降级为最小 meta，仍落基础卡片（不抛异常、不让后台线程挂掉）
        host = (urllib.parse.urlparse(url).netloc or "").lower()
        title = host or url
        return {
            "platform": "web", "name": title, "title": title,
            "description": "", "url": url, "transcript": "",
        }, None

    m = _meta_lookup(html)
    title = m.get("og:title") or _title_tag(html)
    description = m.get("og:description") or m.get("description") or ""
    transcript = _clean_text(html)

    if not title:
        host = (urllib.parse.urlparse(url).netloc or "").lower()
        title = host or url
    name = title[:80]

    meta = {
        "platform": "web",
        "name": name,
        "title": title,
        "description": description,
        "url": url,
        "transcript": transcript,
    }
    return meta, (transcript or None)
