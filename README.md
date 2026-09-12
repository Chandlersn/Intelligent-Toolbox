# 智能收藏箱（Intelligent Toolbox）

> 把你随手收下的东西，攒成一面照见自己的镜子。
>
> 本地优先 · 单进程 · 开箱即用

一个跑在自己电脑上的收藏工具。你把遇到的**仓库、视频、文章**随手丢进来，它替你完成分类、归位与关联，最后攒成一张兴趣图谱和一份行为画像。

---

## 它解决什么问题

我们每天都在做两件事：**消费**（刷视频、读文章）和**生产**（写代码、造东西）。但两边的收藏都躺在各自的书签列表里，存完即弃——没有"为什么收"，也不知道它和别的收藏有什么关系。

这个工具换了个做法：**收藏不是终点，而是一次认知网络的节点登记。**

你点一下就结束，领域、用途、关联全部交给后台。人越懒，数据越全。

> 一条底层判断：**行为信号比自述信号更真**。你实际收了什么，比你"觉得自己该学什么"更接近事实。

---

## 两个面，一张图

收藏被分成两侧：

| | 来源 | 留下什么 |
|---|------|---------|
| **生产端** | GitHub / GitLab 仓库 | 仓库卡片：领域、用途、技术栈、成熟度…… |
| **认知端** | 抖音等短视频链接 | **原文逐字稿** + 萃取卡片，两份都留 |

单看任一面，都只是一份列表。有意思的是把两面放进**同一张多轴图谱**——它们会在共同的主题上相遇。于是你能看见：

> 你摄入的认知，和你产出的作品，中间隔着什么、又在哪里接上了。

这类跨侧的连线会在图谱里被标成**桥接**，一眼可辨。

---

## 功能

### 收集：多种入口，一次点击

所有入口最终都打向同一个 `/collect`，所以收什么、从哪收都行：

| 入口 | 怎么用 | 适合 |
|------|--------|------|
| **手机 H5**（`/`） | 粘贴链接，可"添加到主屏幕"当 App 用 | 移动端随手收 |
| **跨平台悬浮球**（`ball.js`） | 一个常驻小球，拖文字进去 / 读剪贴板 | 任意页面、任意 webview |
| **书签工具** | 一行 JS，点一下把当前页收进来 | 桌面浏览器 |
| **分享深链**（`/share.html`） | 把链接拼成 URL 发出去，打开即收藏 | 聊天、分享场景 |
| **纯文本投递** | 一整段话丢进来，自动抽出其中的仓库 / 视频链接 | 从对话、笔记里捞 |

收藏**同步落库**（毫秒级返回），分析在后台线程跑，不阻塞你。

### 卡片：自动成卡，可重算、可反馈

每条收藏都会生成一张多维卡片，无需你手动打标签：

| 字段 | 含义 |
|------|------|
| `domain` | 领域（AI·LLM / Web 框架 / DevOps·云 / 数据库……） |
| `purpose` | 一句话用途 |
| `tech_stack` | 技术栈 |
| `why_needed` | 你为什么可能需要它（结合你的备注） |
| `related_nodes` | 与已有收藏的关联 |
| `maturity` | 成熟度（stars 分桶 + 最近提交） |
| `tags` | 多面标签 |
| `recommendation` | 推荐指数 0–5 |
| `source` | `llm` 还是 `heuristic` |

- **来源可分辨**：卡片页会标出这张卡是"AI 分析"还是"规则预览"，不会拿规则结果冒充深度分析。
- **可重算**：内容变了、或你后来配了 LLM，可以对单张卡片一键重算。
- **可反馈**：标"用过 / 弃了 / 还想用"，画像与推荐会参考你的反馈。
- **认知端额外带原文**：抖音类收藏会同时展示逐字稿，方便回看原始内容。

### 图谱：兴趣结构 + 缺角 + 跨侧桥接

- 收藏按多轴投影成节点，共同轴上相邻的连成边。
- **缺角**：图谱上的稀疏区域会被标出来，提示你可能想补哪一块。
- **领域相邻**：基于你真实收藏的共现算出，而非预判。
- **跨侧桥接**：生产端与认知端之间的连线单独标色，是"学"与"做"的接线。

### 推荐

- **弱协同推荐**：先由 LLM 依据你的画像推断"你可能还想要什么领域 + 为什么"，再用 GitHub 搜索补上**真实存在**的仓库——只推能落地的，不推空气。
- **本周涨星榜**：GitHub 近一周涨星 Top10，帮你发现当下热度。

### 行为画像

把收藏聚合成一面镜子：领域密度、技术栈分布、活跃时段、来源分布、收藏类型（生产端 ↔ 认知端）比例。

### 更新提醒

对已收藏的仓库做定期比对，星标数、描述、语言有变化时生成**未读事件**，顶部横幅提示、卡片上打角标，不会漏掉你关注的项目在长大。

---

## 快速开始

需要 **Python 3.11+**。核心功能不需要 `pip install`。

```bash
python run.py
```

打开 `http://127.0.0.1:8732` 即可。

| 页面 | 地址 |
|------|------|
| 收集页 | `/`（或 `/collect.html`） |
| 卡片页 | `/cards.html` |
| 图谱页 | `/map.html` |
| 推荐页 | `/recommend.html` |
| 画像页 | `/profile.html` |
| 分享页 | `/share.html` |
| 自检 | `/api/doctor` |

---

## 配置

全部走环境变量，**都有默认值**，不改也能跑。

### 服务

| 变量 | 默认 | 说明 |
|------|------|------|
| `REPO_PORT` | `8732` | 端口 |
| `REPO_HOST` | `127.0.0.1` | 绑定地址；给手机用改 `0.0.0.0` |
| `REPO_DB` | `<根>/repo_collector.db` | 库路径 |
| `REPO_TOKEN` | 空（关闭） | 设置后所有接口要带口令，对外暴露时用 |
| `REPO_ALLOW_ORIGINS` | 空 | 额外放行哪些主机能发写操作（局域网调试） |
| `REPO_MEDIA_DIR` | `<根>/media` | 认知端音视频落盘目录 |

### 接入 LLM（可选，但强烈建议）

**为什么需要**：默认不配 LLM 也能跑——卡片由内置规则生成（离线、零配置、开箱即用）。但规则只看得懂关键词，理解力有限。**接上 LLM，卡片质量会有质的差别**，推荐与关联也更准。

**怎么接**：本工具不内置任何模型厂商，而是复用一个外部的 `llm.py` 模块——这样**密钥不落在本项目里**。

1. 准备一个目录，放进 `llm.py`。它只需要暴露两样东西：`AVAILABLE` 和 `chat()`：

```python
# llm.py —— 放在 REPO_LLM_ROOT 指向的目录
import json, os, urllib.request

AVAILABLE = True
_BASE  = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")  # 任何兼容 OpenAI 的服务
_KEY   = os.environ.get("LLM_API_KEY", "")
_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

def chat(system, user, timeout=20, retries=1):
    body = json.dumps({
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()
    req = urllib.request.Request(
        _BASE.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + _KEY},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"]
```

> 用任何 SDK、任何厂商都行，只要 `chat(system, user, timeout=..., retries=...)` 返回字符串即可。密钥从环境变量读（或写在你自己那侧），**不会进本项目的库、也不会进日志**。

2. 启动时把这个目录指给工具：

```bash
REPO_LLM_ROOT=/path/to/your/llm-dir python run.py
```

3. 验证：打开 `/api/doctor`，看 `llm.available` 是否为 `true`。是则卡片会由 AI 生成；不是则自动回退规则，**功能照常**。

| 变量 | 默认 | 说明 |
|------|------|------|
| `REPO_LLM_ROOT` | `D:\测试\lantern-caliper` | 存放 `llm.py` 的目录 |
| `REPO_LLM_TIMEOUT` | `20` | 后台出卡的超时（秒） |
| `REPO_LLM_TIMEOUT_FAST` | `9` | 你在页面点「重算」时的短预算（秒） |

### GitHub Token（可选）

匿名调用 GitHub 的搜索接口限速 **10 次/分**，推荐与涨星榜容易撞限。配一个令牌即可解锁更高额度：

```bash
REPO_GITHUB_TOKEN=ghp_xxxxxxxx python run.py
```

（在 GitHub → Settings → Developer settings → Personal access tokens 生成，只读权限即可。）

### 抖音逐字稿（可选）

默认抖音链接只存元数据。想真的转出逐字稿，装上 **ffmpeg + faster-whisper** ——它们不进仓库，是本机的可选增强，**没装也不影响其他功能**：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install faster-whisper
# ffmpeg 放到 bin/ 或系统 PATH
```

装好后 `python run.py` 会自动使用这个环境。首次转写会联网下载模型（约 140MB），之后离线可用。

| 变量 | 默认 | 说明 |
|------|------|------|
| `REPO_WHISPER_MODEL` | `base` | 模型尺寸，可换 `small` / `medium` |
| `REPO_FFMPEG_BIN` | 自动 | ffmpeg 路径；默认先找项目 `bin/`，再找 PATH |

### 对外暴露

要在局域网或公网使用，两件事一起做：

```bash
REPO_HOST=0.0.0.0 REPO_TOKEN=my-secret python run.py
```

`REPO_TOKEN` 一开，所有接口都要带口令；前端把口令放到 `localStorage.repo_token` 即自动随请求带上：

```js
localStorage.setItem('repo_token', 'my-secret');
```

---

## 技术架构

单进程的本地服务，路由很薄，重活交给后台线程。纯静态前端，没有构建步骤。

```
   浏览器 / 悬浮球 / 分享链接
              │
              ▼
      HTTP 服务（薄路由）──────────▶ 后台采集线程
              │                          │
              │                  GitHub API ／ 抖音解析 + 转写
              ▼                          ▼
        分析层（规则优先，可选 LLM）
              │
              ▼
        SQLite（本地，WAL）
```

| 层 | 位置 | 职责 |
|----|------|------|
| 接入层 | `web/` | 收集 / 卡片 / 图谱 / 推荐 / 画像 / 分享；跨平台悬浮球 |
| 服务层 | `src/server.py` | 路由、静态分发、业务函数、后台采集线程 |
| 分析层 | `src/analyze.py` | 卡片、画像、推荐、关联；规则优先，LLM 可选 |
| 认知端 | `src/douyin_source.py` | 分享页解析、音频抽取、逐字稿转写 |
| 存储层 | `src/db.py` | SQLite 统一连接（WAL）+ 版本化迁移 |
| 配置层 | `src/config.py` | 全部可变项集中，`REPO_*` 覆盖 |
| 原生壳 | `native-shell/` | 桌面 / Android 工程（骨架） |

### 数据模型

```sql
repos (
  id, url, note, status, meta,      -- 地址、备注、状态、平台元数据
  card,                             -- 多维分析卡片（JSON）
  kind,                             -- production / cognition
  raw,                              -- 认知端原文逐字稿
  created_at, source
)
-- 多轴模型：以下都是「可从 card 重建的派生物」
axes      (key, name)               -- 轴定义：domain / tech / scene / gap / motive / timeline
repo_axis (repo_id, axis_key, value, weight, source, confidence)
tags      (repo_id, tag)
```

图谱、筛选、画像都建立在这些轴上；卡片一旦重算，轴表随之同步。

### 接口清单

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/collect` | 收藏，入参 `{url?, note?, text?, source?}` |
| GET | `/collect?url=&text=` | 分享深链，打开即收藏 |
| GET | `/api/items` | 收藏列表，支持 `?q=` 搜索 |
| GET | `/api/cards` | 卡片筛选 `?domain[]=&tech[]=&motive[]=&use[]=&kind[]=&q=&sort=` |
| GET | `/api/facets` | 筛选面（各轴取值与计数） |
| GET | `/api/graph` | 图谱节点 / 边 / 缺角 / 领域相邻 |
| GET | `/api/profile` | 行为画像聚合 |
| GET | `/api/recommend` | 弱协同推荐 |
| GET | `/api/trending` | GitHub 本周涨星 Top10 |
| GET | `/api/query` | 多轴交叉查询 |
| GET | `/api/updates` | 未读的仓库更新事件 |
| GET | `/api/doctor` | 自检：一致性、LLM 状态、库状态、转写就绪 |
| POST | `/api/card/regenerate` | 重算某张卡片 |
| POST | `/api/item/feedback` | 卡片反馈 |
| POST | `/api/reindex` | 从卡片重建全部多轴数据 |
| POST | `/api/updates/check` | 主动比对 GitHub，产出更新事件 |
| DELETE | `/api/item/<id>` | 删除收藏 |
| GET | `/health` | 健康检查 |

返回均为 JSON，跨源开放。你也可以用自己的脚本、书签工具、快捷指令往 `/collect` 里投：

```bash
curl -X POST http://127.0.0.1:8732/collect \
  -d '{"url":"https://github.com/xxx/yyy","note":"看着有用"}'
```

### 目录结构

```
智能收藏箱/
├── run.py          # 一键启动
├── src/            # 后端：配置 / 存储 / 路由 / 分析 / 抖音解析
├── web/            # 前端：收集 · 卡片 · 图谱 · 推荐 · 画像 · 分享
├── tests/          # 单元 + HTTP 集成测试
├── native-shell/   # 桌面 / Android 原生壳（骨架）
└── docs/           # 产品说明与开发计划
```

---

## 隐私

- 数据只存本机 SQLite，默认只绑 `127.0.0.1`，**不出本机**。
- **LLM 密钥不落在本项目**：它在你自己的 `llm.py` 里，不进库、不进日志。
- 只收集你主动投递的链接与备注，不抓取任何其他信息。

---

## 版本迭代

| 迭代 | 内容 |
|------|------|
| 起步 | 收集入口 + 本地落库 + 后台自动成卡 |
| 展开 | 多维卡片、兴趣图谱与缺角、行为画像、弱推荐 |
| 打通 | 分享深链、仓库更新提醒 |
| 当前 | **多源**：接入认知端（抖音逐字稿），生产端 ↔ 认知端 跨侧碰撞 |

下一步：原生 App——系统分享直达 + 常驻悬浮球，让"随手一收"真正零摩擦。

---

## 许可

个人自用工具，暂未声明开源许可；如需引用请先联系作者。
