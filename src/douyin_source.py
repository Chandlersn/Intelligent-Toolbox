"""抖音（认知端）来源模块 —— 多源收藏的第二类入口。

设计取舍（与项目铁律对齐）：
- 元数据解析借用 yzfly/douyin-mcp-server 的「分享页内嵌 window._ROUTER_DATA」思路，
  但用标准库 urllib 重写，核心保持零依赖：只取 标题 / 作者 / 文案 / 无水印播放地址。
  这部分不需要逆向 a_bogus 签名、不需要付费 API、不需要第三方库。
- 「语音转逐字稿」才是重活：用户已批准引入 ffmpeg + whisper。
  下载音视频用 urllib，抽音频用 ffmpeg 子进程，转写用 faster-whisper（懒加载、可选）。
  转写引擎做成可注入，便于测试用假转录器，也便于以后换云端 ASR（硅基流动 / OpenAI 兼容）。

免责：仅用于你本人创作或有权处理的内容的本地归档，遵守平台条款。
"""

import json
import os
import re
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


def _parse_router_data(html):
    """从分享页 HTML 抽 window._ROUTER_DATA 并取出视频信息。

    纯字符串 / 正则 / JSON，可单测。失败抛 ValueError（上层据此降级）。
    返回 {video_id, title, author, desc, play_url, cover}。
    """
    m = _ROUTER_RE.search(html)
    if not m:
        raise ValueError("从抖音分享页解析视频信息失败（页面结构可能已变动）")
    try:
        data = json.loads(m.group(1).strip())
    except Exception:
        raise ValueError("抖音分享页内嵌 JSON 解析失败")
    loader = data.get("loaderData", {})
    info = None
    for key in (_VIDEO_PAGE_KEY, _NOTE_PAGE_KEY):
        if key in loader:
            info = loader[key].get("videoInfoRes")
            break
    if not info:
        raise ValueError("从 JSON 中解析视频信息失败")
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
    return {
        "video_id": vid,
        "title": title,
        "author": author,
        "desc": desc,
        "play_url": play_url,
        "cover": cover,
    }


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
    video_id = final.split("?")[0].rstrip("/").split("/")[-1]
    if not video_id or len(video_id) < 6:
        raise ValueError("无法从抖音链接解析出视频 ID")
    share_url = "https://www.iesdouyin.com/share/video/%s" % video_id
    try:
        html = _get(share_url)
    except Exception:
        # 回退：直接取真实页面（短链未 302 时）
        if final != url:
            html = _get(final)
        else:
            html = _get("https://www.douyin.com/video/%s" % video_id)
    info = _parse_router_data(html)
    info["url"] = url
    return info


def download_video(play_url, out_path, timeout=30):
    """下载无水印视频到本地文件（urllib 流式）。返回 out_path。"""
    req = urllib.request.Request(play_url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return out_path


def extract_audio(video_path, audio_path, ffmpeg=None):
    """ffmpeg 抽 16k 单声道 wav（whisper 友好）。失败抛 RuntimeError。

    ffmpeg 默认走 config.FFMPEG_BIN（项目 bin/ → REPO_FFMPEG_BIN → PATH），可注入便于测试。
    """
    ffmpeg = ffmpeg or config.FFMPEG_BIN
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
        try:
            from faster_whisper import WhisperModel
        except Exception:
            return None
        m = model or getattr(config, "WHISPER_MODEL", "base")
        try:
            model_obj = WhisperModel(m, device="cpu")
            segments, _ = model_obj.transcribe(audio_path, language="zh")
            return "\n".join(s.text for s in segments).strip()
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
    if transcribe_fn and info.get("play_url"):
        try:
            if media_dir:
                os.makedirs(media_dir, exist_ok=True)
                tv = os.path.join(media_dir, info["video_id"] + ".mp4")
                download_video(info["play_url"], tv)
                audio = tv + ".wav"
                extract_audio(tv, audio)
                raw = transcribe_fn(audio)
            else:
                with tempfile.TemporaryDirectory() as td:
                    tv = os.path.join(td, "v.mp4")
                    download_video(info["play_url"], tv)
                    audio = os.path.join(td, "a.wav")
                    extract_audio(tv, audio)
                    raw = transcribe_fn(audio)
        except Exception:
            raw = None
    return meta, raw
