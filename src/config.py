"""集中配置：所有可变项走环境变量，带安全默认值。

为什么需要它：改造前 PORT/绑定地址/DB 路径/LLM 路径/GitHub Token 全部写死在源码里
（端口一处、收集端点四处、LLM 路径一处），换台机器或换个端口就得改代码。
现在只改环境变量或直接读默认值，代码零改动。

约定：只用标准库 os.environ，不引入 .env 解析依赖（守住零依赖铁律）。
命名统一 REPO_ 前缀，避免与系统变量（如 GITHUB_TOKEN）混淆。
"""

import os

# 项目根 = src/ 的父目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")


def _env(key, default=""):
    v = os.environ.get(key)
    return default if v is None or v.strip() == "" else v.strip()


def _env_int(key, default):
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


# ---------- 服务 ----------
HOST = _env("REPO_HOST", "127.0.0.1")
PORT = _env_int("REPO_PORT", 8732)

# ---------- 存储 ----------
DB = _env("REPO_DB", os.path.join(ROOT, "repo_collector.db"))

# ---------- 安全 ----------
# 可选口令。设置后，写操作（收藏/删除/重算）必须带 X-Collector-Token 头（或 ?k=）。
# 默认留空=关闭：本机自用场景下由「只绑回环 + 同源守卫」兜底，不打扰使用。
TOKEN = _env("REPO_TOKEN", "")

# 允许发起写操作的跨源主机白名单。默认只认本机回环。
# 局域网调试（手机真机 / Android 壳）时用 REPO_ALLOW_ORIGINS 追加自己的机器 IP。
ALLOW_ORIGIN_HOSTS = {
    "127.0.0.1", "localhost", "::1", "[::1]", "tauri.localhost",
}
ALLOW_ORIGIN_HOSTS |= {
    h.strip().lower() for h in _env("REPO_ALLOW_ORIGINS", "").split(",") if h.strip()
}

# 请求体上限（字节）。超限直接 413，避免一个超大 body 把内存打满。
MAX_BODY = _env_int("REPO_MAX_BODY", 256 * 1024)

# ---------- LLM（复用灯笼模块，不可用自动回退规则） ----------
LLM_ROOT = _env("REPO_LLM_ROOT", r"D:\测试\lantern-caliper")
# 后台链路（采集出卡）的预算：可以慢，但不能无限等。
LLM_TIMEOUT = _env_int("REPO_LLM_TIMEOUT", 20)
LLM_RETRIES = _env_int("REPO_LLM_RETRIES", 1)
# 交互链路的短预算：前端重算有 12s 超时，后端必须比它先放弃，
# 否则用户等到的是「超时」而不是「保留原卡片」。
LLM_TIMEOUT_FAST = _env_int("REPO_LLM_TIMEOUT_FAST", 9)

# ---------- 外部 API ----------
# GitHub Token（可选）。不设则匿名调用，search 接口限速 10 次/分。
GITHUB_TOKEN = _env("REPO_GITHUB_TOKEN", _env("GITHUB_TOKEN", ""))

# ---------- 认知端（抖音等）转录 ----------
# 下载的视频/音频落盘目录。默认放在项目根 media/（不进版本库，已被 .gitignore 习惯排除）。
MEDIA_DIR = _env("REPO_MEDIA_DIR", os.path.join(ROOT, "media"))
# faster-whisper 模型尺寸：base 兼顾速度与质量；大 corpus 可换 small/medium。
WHISPER_MODEL = _env("REPO_WHISPER_MODEL", "base")
# ffmpeg 二进制解析顺序：① 项目自带 bin/ffmpeg.exe（进本项目方案）② REPO_FFMPEG_BIN ③ PATH 上的 ffmpeg。
# 这样「进本项目」时抖音转写开箱即用，没装时退回 PATH / 降级。
def _ffmpeg_bin():
    cand = os.path.join(ROOT, "bin", "ffmpeg.exe")
    if os.path.isfile(cand):
        return cand
    return _env("REPO_FFMPEG_BIN", "ffmpeg")
FFMPEG_BIN = _ffmpeg_bin()


def summary():
    """给 /api/doctor 与启动日志用：脱敏后的生效配置。"""
    return {
        "root": ROOT,
        "db": DB,
        "host": HOST,
        "port": PORT,
        "token_required": bool(TOKEN),
        "allow_origin_hosts": sorted(ALLOW_ORIGIN_HOSTS),
        "max_body": MAX_BODY,
        "llm_root": LLM_ROOT,
        "llm_timeout": LLM_TIMEOUT,
        "llm_timeout_fast": LLM_TIMEOUT_FAST,
        "github_token": bool(GITHUB_TOKEN),
        "media_dir": MEDIA_DIR,
        "whisper_model": WHISPER_MODEL,
        "ffmpeg_bin": FFMPEG_BIN,
    }
