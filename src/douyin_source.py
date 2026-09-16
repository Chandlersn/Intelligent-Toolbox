"""抖音（认知端）来源模块 —— 多源收藏的第二类入口。

设计取舍（与项目铁律对齐）：
- 元数据解析借用 yzfly/douyin-mcp-server 的「分享页内嵌 window._ROUTER_DATA」思路，
  但用标准库 urllib 重写，核心保持零依赖：只取 标题 / 作者 / 文案 / 无水印播放地址。
  这部分不需要逆向 a_bogus 签名、不需要付费 API、不需要第三方库。
- 重要现实（2026-09 起）：抖音分享页对未执行 JS 的客户端返回 __ac_signature 反爬挑战壳子，
  标准库 urllib 无法绕过 —— 因此本模块用 Playwright + 本机 Edge 无头渲染过签（_fetch_with_browser，
  可选依赖，系统装了 Edge+playwright 才生效，默认零依赖不受影响）。但注意：浏览器过签只解决了
  「挑战页」这第一道关；视频详情走的是另一个要签名的 iteminfo 接口（签名由反爬 SDK 实时算，
  纯抓取拿不到），该接口现对无签名请求返回空。所以真实视频的 标题/作者/封面/播放地址 已结构性
  不可得，解析到占位壳时如实报「反爬签名拦截」，不再误诊为『视频删除/私密』。链接仍会作为
  『待补充』存根入收藏箱（见 server.py worker），由用户用备注文案补全。
- 「语音转逐字稿」才是重活：用户已批准引入 ffmpeg + whisper。
  下载音视频用 urllib，抽音频用 ffmpeg 子进程，转写用 faster-whisper（懒加载、可选）。
  转写引擎做成可注入，便于测试用假转录器，也便于以后换云端 ASR（硅基流动 / OpenAI 兼容）。

免责：仅用于你本人创作或有权处理的内容的本地归档，遵守平台条款。
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import config

# 抖音各域名（含短链 host）。v.douyin.com 是分享短链，会 302 到真实页面。
DOUYIN_HOSTS = {
    "v.douyin.com", "www.douyin.com", "douyin.com",
    "www.iesdouyin.com", "iesdouyin.com", "share.douyin.com",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://www.douyin.com/",
}

_ROUTER_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)
_VIDEO_PAGE_KEY = "video_(id)/page"
_NOTE_PAGE_KEY = "note_(id)/page"

# 抖音反爬挑战页的信号：未执行 JS 的客户端（urllib）会被挡在壳子外，
# 真实 videoInfoRes 数据根本不下发，只丢一个带 __ac_signature 的占位页。
_CHALLENGE_MARKERS = ("__ac_signature", "acrawler", "verify-slider", "captcha")
_PLACEHOLDER_TITLE = "在抖音记录美好生活"


def _looks_like_challenge(html):
    """判断拿回来的页面是不是反爬挑战/拦截壳子（而非真实内容页）。"""
    if not html:
        return False
    low = html.lower()
    for mk in _CHALLENGE_MARKERS:
        if mk in low:
            return True
    # 占位标题是抖音拦截页的通用 <title>
    mt = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if mt and _PLACEHOLDER_TITLE in mt.group(1):
        return True
    return False


def _meta_content(html, key):
    """取 <meta property=key> 或 <meta name=key> 的 content，两种属性顺序都兼容。"""
    if not html:
        return ""
    esc = re.escape(key)
    m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % esc, html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % esc, html, re.I)
    return m.group(1).strip() if m else ""


def _tag_title(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return m.group(1).strip() if m else ""


def _parse_meta_fallback(html, allow_challenge=False):
    """挑战/拦截页拿不到 videoInfoRes 时，尽力从 meta/og 标签抠标题/描述/封面。
    占位标题说明仍是拦截页，直接放弃。返回『部分 meta』或 None。
    allow_challenge=True 时跳过挑战页检测（浏览器渲染后的页面可能残留 __ac_signature 标记，
    但 og:meta 是真实的）——此时仍靠占位标题兜底拒绝。"""
    if not html:
        return None
    if not allow_challenge and _looks_like_challenge(html):
        return None
    title = _meta_content(html, "og:title") or _tag_title(html)
    if not title or _PLACEHOLDER_TITLE in title:
        return None
    desc = _meta_content(html, "og:description") or _meta_content(html, "description")
    cover = _meta_content(html, "og:image")
    return {
        "video_id": "",
        "title": title,
        "author": "",
        "desc": desc,
        "play_url": "",
        "cover": cover,
        "_partial": True,
        "parse_status": "partial",
    }


def _fetch_with_browser(url, timeout=25):
    """用 Playwright + 本机 Edge/Chromium 无头渲染页面，返回 JS 执行后的完整 HTML。

    抖音的 __ac_signature 反爬挑战需要真实浏览器执行 acrawler.js 才能过签；标准库 urllib 做不到。
    本函数作为兜底：系统装有 Edge/Chromium 且 pip 装了 playwright 时生效，否则返回 None（主流程不受影响）。
    channel='msedge' 复用系统已装 Edge，省去下载 Chromium；--disable-blink-features 降低被识别为自动化的概率。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    html = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel="msedge", headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(
                user_agent=_HEADERS["User-Agent"], locale="zh-CN")
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            # 等 acrawler 算完签名、真实 _ROUTER_DATA（含 videoInfoRes）注入；
            # 视频不存在时抖音会写入错误状态，等到超时再返回当前内容（避免拿到空壳）。
            try:
                page.wait_for_function(
                    "() => { const r = window._ROUTER_DATA; if (!r) return false;"
                    " const s = JSON.stringify(r);"
                    " return /videoInfoRes/.test(s) || s.includes('status_code') || s.includes('statusCode'); }",
                    timeout=timeout * 1000)
            except Exception:
                pass
            html = page.content()
            browser.close()
    except Exception:
        return None
    return html


def is_douyin(url):
    """判断一个 url 是否抖音分享链接。"""
    try:
        host = (urllib.parse.urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:
        return False
    return host in DOUYIN_HOSTS


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


_AWEME_RE = re.compile(r"(\d{10,})")


def _extract_video_id(text):
    """从 URL 或文本里抠 10 位以上的数字 id（抖音 aweme_id 约 19 位）。

    短链 302 后通常是 /video/<id>；直接对末段抠长数字即可，避免把短码当 id 漏过去。
    """
    if not text:
        return ""
    tail = text.split("?")[0].rstrip("/").split("/")[-1] or text
    m = _AWEME_RE.search(tail)
    return m.group(1) if m else ""


def _extract_video_id_from_html(html):
    """URL 没带 id 时，从 _ROUTER_DATA / 页面里抠 aweme_id 兜底。"""
    if not html:
        return ""
    m = re.search(r'"aweme_id"\s*:\s*"?(\d{10,})', html)
    if m:
        return m.group(1)
    m = _AWEME_RE.search(html)
    return m.group(1) if m else ""


def _parse_router_data(html, video_id=None):
    """从分享页 HTML 抽 window._ROUTER_DATA 并取出视频信息。

    纯字符串 / 正则 / JSON，可单测。失败抛 ValueError（上层据此降级）。
    返回 {video_id, title, author, desc, play_url, cover}。
    video_id 仅用于兜底提示，解析失败信息更可读。

    反爬注意：抖音现在对未执行 JS 的客户端返回 __ac_signature 挑战壳子，
    此时 _ROUTER_DATA 缺失或为空 —— 这是结构性拦截，不是链接坏了，报错必须说真话。
    """
    # 1) 先认 _ROUTER_DATA：浏览器渲染后的页面仍会残留 __ac_signature 字符串，
    #    不能先按挑战页判定，否则会把真实链接误杀。有 _ROUTER_DATA 就优先解析。
    m = _ROUTER_RE.search(html)
    if not m:
        # 连 _ROUTER_DATA 都没有，才判定为反爬挑战壳子（urllib 这种非浏览器客户端拿不到）
        if _looks_like_challenge(html):
            raise ValueError(
                "抖音返回反爬挑战页（__ac_signature），标准库 urllib 无法执行 JS 通过验证；"
                "真实链接需在浏览器 / 无头渲染环境解析，或接受『仅页面元信息』的部分解析")
        raise ValueError("从抖音分享页未取到 _ROUTER_DATA（可能被反爬拦截或页面结构已变动）")
    try:
        data = json.loads(m.group(1).strip())
    except Exception:
        raise ValueError("抖音分享页内嵌 JSON 解析失败")
    loader = data.get("loaderData", {})
    info = None
    # 先按已知 key；再退化到「任意含 videoInfoRes 的 /page key」
    for key in (_VIDEO_PAGE_KEY, _NOTE_PAGE_KEY):
        if key in loader:
            info = loader[key].get("videoInfoRes")
            if info:
                break
    if not info:
        for k, v in loader.items():
            if isinstance(v, dict) and "videoInfoRes" in v:
                info = v["videoInfoRes"]
                break
    if not info:
        # _ROUTER_DATA 已解析，但 videoInfoRes 节点缺失。分两种情形：
        # (a) 占位壳：页面标题仍是『在抖音记录美好生活』——说明视频详情接口（iteminfo）
        #     被反爬签名拦截，纯抓取（含无头浏览器）拿不到签名，数据为空。这是抖音现行反爬，
        #     不是链接坏了，必须如实报，不能误诊成『已删除/私密』。
        # (b) 真实标题但无视频节点——才可能是真下架/私密。
        if _PLACEHOLDER_TITLE in _tag_title(html):
            raise ValueError(
                "抖音分享页已渲染，但视频详情接口（iteminfo）被反爬签名拦截："
                "_ROUTER_DATA 中 videoInfoRes 为空（占位壳『在抖音记录美好生活』）。"
                "纯抓取（含无头浏览器）无法获得标题/作者/封面——这是抖音现行反爬措施，"
                "需在 App 内打开，或把视频文案作为备注一起收藏以便补充")
        raise ValueError("已加载 _ROUTER_DATA 但未找到 videoInfoRes 视频节点（视频可能已下架/私密，或抖音页面结构已变动）")
    item = (info.get("item_list") or [{}])[0]
    video = item.get("video") or {}
    play_list = (video.get("play_addr") or {}).get("url_list") or []
    # 无水印：playwm → play（抖音水印地址带 wm，替换即去水印）
    play_url = (play_list[0].replace("playwm", "play") if play_list else "")
    author = (item.get("author") or {}).get("nickname") or ""
    desc = (item.get("desc") or "").strip()
    cover = ((video.get("cover") or {}).get("url_list") or [None])[0]
    vid = (info.get("aweme_id") or (video.get("vid") or ""))
    title = desc or ("douyin_%s" % vid)
    result = {
        "video_id": vid,
        "title": title,
        "author": author,
        "desc": desc,
        "play_url": play_url,
        "cover": cover,
    }
    # 解析成功但关键字段全空 ⇒ 抖音对无效/已删视频仍返回空壳 videoInfoRes，
    # 不能当成有效卡片（否则会生成一张只有占位标题的垃圾卡），如实报错。
    if not (vid or author or desc or play_url):
        raise ValueError("从抖音解析到的视频信息为空（链接可能无效，或视频已删除/设为私密）")
    return result


def resolve_douyin(url):
    """解析抖音分享链接 → 元数据。先让短链 302 出 video_id，再抓分享页取 _ROUTER_DATA。

    返回 {video_id, title, author, desc, play_url, cover, url}；失败抛 ValueError。
    """
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(url, headers=_HEADERS), timeout=8)
        final = resp.geturl()
    except urllib.error.URLError:
        final = url
    # 1) 先从最终 URL 抠长数字 id（短链 302 后通常是 /video/<id>）
    video_id = _extract_video_id(final)
    html = None
    # 2) 拿分享页：优先 iesdouyin，失败回退真实页
    if video_id:
        try:
            html = _get("https://www.iesdouyin.com/share/video/%s" % video_id)
        except Exception:
            html = None
    if html is None:
        try:
            html = _get(final if final != url else
                        ("https://www.douyin.com/video/%s" % video_id if video_id else url))
        except Exception:
            html = None
    if not html:
        raise ValueError("无法抓取抖音分享页（链接可能已失效，或需 App 内打开）")
    # 3) URL 没拿到 id 时，从页面里再抠一次 aweme_id
    if not video_id:
        video_id = _extract_video_id_from_html(html)
        if not video_id:
            raise ValueError("无法从抖音链接解析出视频 ID")
    try:
        info = _parse_router_data(html, video_id=video_id)
        info["url"] = url
        return info
    except ValueError as e:
        msg = str(e)
        # 反爬挑战页：标准库拿不到 → 尝试用本机无头浏览器（Edge/Chromium + playwright）渲染过签
        if "反爬挑战" in msg or "__ac_signature" in msg or _looks_like_challenge(html):
            # 优先用带 id 的真实视频页渲染（比分享页更稳），失败再退原始 url
            candidate = "https://www.douyin.com/video/%s" % video_id if video_id else url
            bhtml = _fetch_with_browser(candidate) or _fetch_with_browser(url)
            if bhtml:
                try:
                    info = _parse_router_data(bhtml, video_id=video_id)
                    info["url"] = url
                    return info
                except ValueError as e2:
                    es = str(e2)
                    # 浏览器已渲染拿到正常页，但内容为空/无视频节点（无效/已删/结构变）⇒ 如实报
                    if "信息为空" in es or "未找到 videoInfoRes" in es:
                        raise
                    # 否则再试 meta 兜底（页面真实、可能有 og:标题/封面），能出部分卡就不报错
                    fb = _parse_meta_fallback(bhtml, allow_challenge=True)
                    if fb:
                        fb["url"] = url
                        return fb
            # 浏览器也拿不到（未装 playwright / 仍被识别为自动化）→ 如实报错
            raise
        # 非挑战页的解析失败 → 尝试 meta 兜底
        fallback = _parse_meta_fallback(html)
        if fallback:
            fallback["url"] = url
            return fallback
        raise


def download_video(play_url, out_path, timeout=30, retries=3):
    """下载无水印视频到本地文件（urllib 流式）。抖音 CDN 偶发断连，带重试。返回 out_path。"""
    import time
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(play_url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r, open(out_path, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            return out_path
        except Exception as e:
            last_err = e
            # 断连重试前稍等，避免对 CDN 猛打
            time.sleep(1.5 * attempt)
    raise RuntimeError("下载视频失败（已重试 %d 次）：%s" % (retries, last_err))


def _resolve_ffmpeg():
    """定位 ffmpeg 二进制：config.FFMPEG_BIN → PATH → imageio-ffmpeg 自带静态二进制。

    返回可执行路径；实在找不到返回 config.FFMPEG_BIN（让调用处报错时信息可读）。
    优先级最后放 imageio-ffmpeg，是因为它 pip 即装即用、免系统 PATH 依赖，
    正好解决本机没装 ffmpeg 的问题。
    """
    cand = getattr(config, "FFMPEG_BIN", "ffmpeg") or "ffmpeg"
    if cand and cand != "ffmpeg" and os.path.isfile(cand):
        return cand
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return cand


def extract_audio(video_path, audio_path, ffmpeg=None):
    """ffmpeg 抽 16k 单声道 wav（whisper 友好）。失败抛 RuntimeError。

    ffmpeg 解析顺序：显式传入 → _resolve_ffmpeg()（config.FFMPEG_BIN → PATH → imageio-ffmpeg 自带）。
    """
    ffmpeg = ffmpeg or _resolve_ffmpeg()
    cmd = [ffmpeg, "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
           "-f", "wav", audio_path]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120)
    except Exception as e:
        raise RuntimeError("ffmpeg 抽取音频失败：%s" % e)
    return audio_path


def transcribe(audio_path, engine="whisper", model=None, **kw):
    """可注入的转写引擎。

    engine='fake'      → 返回 kw['fake_text']（测试用）
    engine='whisper'   → faster-whisper 懒加载（需 pip install faster-whisper），
                         离线、本地优先；不可用返回 None
    其它               → 返回 None
    """
    if engine == "fake":
        return kw.get("fake_text")
    if engine == "whisper":
        # 国内直连 huggingface.co 常被网关 502，改用镜像；Xet 高速传输后端在镜像上会 401，必须禁用。
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        try:
            from faster_whisper import WhisperModel
        except Exception:
            return None
        # 优先用本地预置模型（models/faster-whisper-base），避免每次联网拉取、也绕开镜像 Xet 限制；
        # 本地不存在时才退回到 hub 自动下载。
        local_model = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "models", "faster-whisper-base")
        if model is None and os.path.isfile(os.path.join(local_model, "model.bin")):
            m = local_model
        else:
            m = model or getattr(config, "WHISPER_MODEL", "base")
        try:
            model_obj = WhisperModel(m, device="cpu")
            # vad_filter=False：抖音视频背景音乐重，默认 VAD 易把人声一并滤掉导致逐字稿为空；
            # 关掉 VAD 让整段音频都进识别，再靠语言模型纠错，更稳。
            segments, _ = model_obj.transcribe(
                audio_path, language="zh", vad_filter=False, beam_size=5)
            text = "\n".join(s.text for s in segments).strip()
            return text or None  # 空也视为失败，便于上层标记「未识别到语音」
        except Exception:
            return None
    return None


def collect_douyin(url, transcribe_fn=None, media_dir=None):
    """端到端：解析 → （可选）下载 + 转写。返回 (meta, raw_text)。

    meta：标准结构（name/title/description/author/cover/play_url/video_id/platform）
    raw_text：逐字稿（转写成功）或 None（引擎不可用 / 用户稍后手动贴）
    transcribe_fn(audio_path) -> text|None 可注入，便于测试与换引擎。
    """
    info = resolve_douyin(url)
    meta = {
        "name": "抖音 · " + (info.get("author") or "未知作者"),
        "title": info.get("title"),
        "description": info.get("desc"),
        "author": info.get("author"),
        "cover": info.get("cover"),
        "play_url": info.get("play_url"),
        "video_id": info.get("video_id"),
        "platform": "douyin",
    }
    raw = None
    transcript_error = None
    if transcribe_fn and info.get("play_url"):
        # 始终下载到临时目录：转写只需要音频，留 108MB 的 mp4 在 media 目录毫无必要，
        # 反复收藏会撑爆磁盘。临时目录退出即清理。
        try:
            with tempfile.TemporaryDirectory() as td:
                tv = os.path.join(td, "v.mp4")
                download_video(info["play_url"], tv)
                audio = os.path.join(td, "a.wav")
                extract_audio(tv, audio)
                raw = transcribe_fn(audio) or None
        except Exception as e:
            # 不再静默吞掉：把失败原因落到 meta，卡片上能明示「转写失败：xxx」，
            # 而不是悄悄变成 None 让 boss 以为没这功能。
            transcript_error = (str(e) or e.__class__.__name__)[:300]
            raw = None
    if not raw and not transcript_error:
        # 下载/抽音频都成功，但 whisper 返回空（无语音或模型未就绪）：也记为可见原因
        transcript_error = "whisper 未识别出可转写语音（视频可能为纯音乐/无旁白，或模型权重未就绪）"
    if transcript_error:
        meta["transcript_error"] = transcript_error
    return meta, raw
