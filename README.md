# 智能收藏箱（Intelligent Toolbox）

> 一键收下你遇到的东西，累积成一面照见自己的镜子。
> 一个本地优先、单进程、源码零依赖的收藏工具箱。

---

## 一、它是什么

我们每天在做两件事：**消费**（看视频、读文章、听播客）和**生产**（写代码、造轮子、做东西）。

传统收藏工具把这两件事都存成一张孤立的书签列表——存完即弃，既不知道你为什么收，也不知道它和你已有的东西有什么关系。**收藏变成了终点，而不是一次认知网络的节点登记。**

这个工具换了个做法：

- **收集同步、分析异步**。点一下就结束，领域 / 用途 / 关联全部丢给后台。人越懒，数据越全。
- **行为信号 > 自述信号**。你实际收了什么（用行动投的票），比"你觉得自己该学什么"更真。
- **收藏反哺认知**。N 条记录是你轨迹的抽样，稀疏处就是"缺角"。

---

## 二、两个面：生产端 ↔ 认知端

这是当前版本的**主设计**。收藏被拆成两类，`kind` 字段区分：

| 面 | kind | 来源 | 存什么 |
|----|------|------|--------|
| **生产端** | `production` | GitHub / GitLab 仓库 | 仓库元数据 → 多轴卡片 |
| **认知端** | `cognition` | 抖音等短视频分享链接 | **原文逐字稿**（`raw`）+ **萃取卡片**（`card`），二者都保留 |

**为什么这样分**：单看任一面都只是一份列表。真正有意思的地方在于——把"你在消费什么"和"你在建造什么"放进**同一张多轴图谱**，它们会在共同的轴上相遇（比如同一个 `domain`），于是你能看见：

> 你摄入的认知，和你输出的作品，中间隔着什么、又在哪里接上了。

这就是**碰撞**。图谱里跨类型的边会被标成**桥接**（陶土虚线）+ 认知节点加外圈虚线环——一眼看出哪些是"生产↔认知"的接线。

> 认知端的逐字稿不是副产品：`raw` 存原始文本（可回溯、可二次萃取），`card` 存结构化理解。**来源与结论分层留存，不做有损压缩。**

---

## 三、架构

单进程、分层的本地服务。路由很薄，重活都在业务函数与后台线程里。

```
                  ┌────────────────────────────────────────────────┐
   GitHub/GitLab  │              接入层  web/（纯静态）              │
   ──────────────▶│   收集 · 卡片 · 图谱 · 推荐 · 画像 · 分享         │
   抖音分享链接     │   跨平台悬浮球 ball.js（任意页面 / webview）      │
                  └───────────────────────┬────────────────────────┘
                                          │  HTTP JSON（CORS *）
                  ┌───────────────────────▼────────────────────────┐
                  │         服务层  src/server.py（薄路由）          │
                  │   classify_source 分派 → 后台 worker 线程        │
                  └──────┬────────────────────────────┬────────────┘
          production 端  │                            │  cognition 端
      ┌──────────────────▼──────────┐   ┌─────────────▼──────────────────┐
      │ 拉仓库元数据（GitHub API）   │   │ douyin_source.py               │
      │ language / stars / topics    │   │ 分享页 _ROUTER_DATA → 元数据     │
      │                             │   │ ffmpeg 抽音 → whisper 转逐字稿   │
      └──────────────────┬──────────┘   └─────────────┬──────────────────┘
                         │                            │
                  ┌──────▼────────────────────────────▼──────────────┐
                  │      分析层  src/analyze.py（规则优先 + LLM 可选） │
                  │   多轴卡片 · 画像聚合 · 关联推断 · 跨类碰撞         │
                  └───────────────────────┬──────────────────────────┘
                                          │
                  ┌───────────────────────▼──────────────────────────┐
                  │   存储层  src/db.py · SQLite(WAL) · schema v5     │
                  │   repos(card = 真源, raw = 逐字稿, kind) + 多轴轴表 │
                  └──────────────────────────────────────────────────┘
```

### 分层职责

| 层 | 位置 | 职责 | 依赖 |
|----|------|------|------|
| 接入层 | `web/*.html` + `ball.js` | 收集 / 卡片 / 图谱 / 推荐 / 画像 / 分享；跨平台悬浮球 | 仅浏览器 |
| 服务层 | `src/server.py` | 薄路由、静态分发、业务函数、后台采集线程 | 标准库 |
| 分析层 | `src/analyze.py` | 多轴卡片、画像聚合、推荐、关联推断、跨类碰撞；规则优先，LLM 可选 | 标准库 + 灯笼 llm（可选） |
| 存储层 | `src/db.py` | 统一连接（WAL + busy_timeout）、`PRAGMA user_version` 版本化迁移 | sqlite3 |
| 配置层 | `src/config.py` | 全部可变项集中，`REPO_*` 环境变量覆盖 | os |
| 认知端 | `src/douyin_source.py` | 分享页解析、音频抽取、逐字稿转写；引擎懒加载、可注入 | 标准库 + ffmpeg/whisper（可选） |
| 原生壳 | `native-shell/` | Tauri / Android 工程，系统分享直达、常驻浮球（骨架） | Rust / Kotlin |

---

## 四、目录结构

```
智能收藏箱/
├── run.py                    # 一键启动（会优先切到项目 .venv，见 §5）
├── README.md
├── src/                      # 后端（Python 标准库）
│   ├── config.py             # 配置层：所有可变项走 REPO_* 环境变量
│   ├── db.py                 # 存储层：统一连接 + schema 版本化迁移（v5）
│   ├── server.py             # 服务层：薄路由 + 业务函数 + 后台采集
│   ├── analyze.py            # 分析层：卡片 · 画像 · 推荐 · 关联；规则 + LLM 唯一入口
│   ├── douyin_source.py      # 认知端：分享页解析 + 音频抽取 + 逐字稿转写
│   └── migrate.py            # 多轴数据维护 CLI：重建 / 自检
├── web/                      # 前端静态（暖纸白主题，无构建）
│   ├── collect.html          # 收集入口（移动优先，可加到主屏幕）
│   ├── cards.html            # 多维卡片 + 胶囊筛选 + 类型药丸 + 原文逐字稿块
│   ├── map.html              # 兴趣图谱 + 缺角 + 跨类桥接高亮
│   ├── recommend.html        # 弱协同推荐 + GitHub 本周涨星 Top10
│   ├── profile.html          # 行为画像（领域 · 技术栈 · 时段 · 来源 · 收藏类型）
│   ├── share.html / host.html / embed.html / ball-demo.html
│   ├── ball.js               # 跨平台悬浮球（拖拽 / 剪贴板 / 拖入收藏）
│   ├── nav.js                # 统一底部导航（含可选口令注入）
│   └── theme.css             # 设计系统（变量 + 组件：card / pill / note / g-tip）
├── tests/                    # 零依赖 unittest
│   ├── common.py             # 隔离基类：临时库 + 断网 + worker 空转
│   ├── test_core.py          # 单元：解析 / 启发式 / LLM 胶水 / 多轴 / 检索 / 守卫
│   ├── test_http.py          # 集成：真起服务真发请求（路由 / 静态 / 写守卫 / 深链）
│   └── test_multisource.py   # 多源：来源分派 / 抖音解析 / 认知卡片 / worker 分支 / 类型筛选
├── native-shell/             # 原生壳骨架（未接通系统分享）
│   ├── tauri/                # 桌面端 Tauri 工程
│   └── android/              # 移动端 Android 工程（SYSTEM_ALERT_WINDOW 浮球）
├── docs/                     # 文档
│   ├── 产品说明书.md
│   ├── 开发节点计划表.md
│   └── 项目评价报告.md / .html
└── repo_collector.db         # 本地数据（不进版本库，含 -wal/-shm 伴生文件）
```

> 路径约定：`config.py` 以项目根为 `ROOT`，静态从 `web/` 取，DB 在根。移动文件不影响运行。

---

## 五、快速开始

环境：Python 3.11+。**核心功能（仓库收藏 / 卡片 / 图谱 / 画像）只用标准库，无需任何安装。**

```bash
python run.py            # 一键启动（等价于 python src/server.py）
```

启动后访问 `http://127.0.0.1:8732/`：

| 页面 | 地址 |
|------|------|
| 收集页 | `/`（或 `/collect.html`） |
| 卡片页 | `/cards.html` |
| 图谱页 | `/map.html` |
| 推荐页 | `/recommend.html` |
| 画像页 | `/profile.html` |
| 分享页 | `/share.html` |
| 自检 | `/api/doctor`（人直接看也行） |

### 可选：抖音逐字稿（认知端转录）

抖音逐字稿依赖 **ffmpeg + faster-whisper**。它们**不在版本库里**（`.venv/`、`bin/`、`media/` 均已 gitignore），属于本机可选外挂：

- **不装**：功能不崩。抖音只存元数据，`raw` 留空，萃取走规则。
- **装了**：`raw` 落真实逐字稿，全链路可用。

落地方式（源码级仍是零依赖——import 是懒加载、缺失即降级）：

```bash
# ① 项目专属 venv（放仓库之外观察不到，但在项目目录内）
python -m venv .venv
.venv/Scripts/python.exe -m pip install faster-whisper

# ② ffmpeg 静态包解到 bin/（ffmpeg.exe / ffprobe.exe）
#    或走系统 PATH，由 config.FFMPEG_BIN 的解析顺序兜底
```

`run.py` 检测到项目 `.venv` 时会自动用它启动服务，这样 server 才 import 得到 faster-whisper。启动后打 `/api/doctor`，`transcribe.ready = true` 即表示认知端转录就绪。

> 首次真实抖音收藏会联网下载 whisper `base` 模型（约 140MB），落 HuggingFace 缓存，之后离线可用。

### 配置（全部走环境变量，都有默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `REPO_PORT` | `8732` | 端口 |
| `REPO_HOST` | `127.0.0.1` | 绑定地址；给手机用改 `0.0.0.0` |
| `REPO_DB` | `<根>/repo_collector.db` | 库路径 |
| `REPO_TOKEN` | 空（关闭） | 设置后所有接口都要带 `X-Collector-Token`（或 `?k=`） |
| `REPO_ALLOW_ORIGINS` | 空 | 额外放行哪些跨源主机能发写操作（局域网调试填本机 IP） |
| `REPO_MAX_BODY` | `262144` | 请求体上限（字节），超限 413 |
| `REPO_LLM_ROOT` | `D:\测试\lantern-caliper` | 复用的 LLM 模块位置 |
| `REPO_LLM_TIMEOUT` | `20` | 后台出卡的 LLM 超时（秒） |
| `REPO_LLM_TIMEOUT_FAST` | `9` | 交互链路（点「重算」）短预算，须小于前端 12s |
| `REPO_GITHUB_TOKEN` | 空 | 配了走认证调用，否则搜索限速 10 次/分 |
| `REPO_MEDIA_DIR` | `<根>/media` | 认知端音视频落盘目录（已 gitignore） |
| `REPO_WHISPER_MODEL` | `base` | 转写模型尺寸：`base` / `small` / `medium` |
| `REPO_FFMPEG_BIN` | 见下 | ffmpeg 路径；解析顺序 = 项目 `bin/ffmpeg.exe` → 本变量 → PATH |

```bash
# 例：换端口 + 开口令
REPO_PORT=9000 REPO_TOKEN=my-secret python run.py
```

开口令后，前端从 `localStorage.repo_token` 读取并自动带上（`nav.js` 里十行，没配就完全不介入）：

```js
localStorage.setItem('repo_token', 'my-secret');
```

### 测试

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

**122 条**，不联网、不依赖灯笼、不碰正式库（每个用例独立临时库，worker 空转）。

---

## 六、接口清单

| 方法 | 路径 | 说明 | 写守卫 |
|------|------|------|--------|
| POST | `/collect` | 入参 `{url?, note?, text?, source?}`；`text` 抽第一个合法链接（GitHub / GitLab / 抖音）。同步落库 + 去重 + 后台出卡 | 口令（若配）；**不限制跨源** |
| GET | `/collect?url=&text=&note=&source=` | 分享深链，打开即收藏，返回移动友好结果页 | 口令（若配） |
| GET | `/api/items` | 收藏列表（含 meta / card / kind），支持 `?q=` 全文搜索 | — |
| GET | `/api/cards` | 卡片筛选，`?domain[]=&tech[]=&motive[]=&use[]=&kind[]=&q=&sort=`（同层 OR / 跨层 AND） | — |
| GET | `/api/facets` | 筛选面（domain / tech / motive，含 count 与平均置信度） | — |
| GET | `/api/profile` | 行为画像聚合（含 `sources` 来源分布、`kinds` 类型分布） | — |
| GET | `/api/graph` | 图谱节点 + 边 + 缺角 + `domain_links` 领域相邻；节点带 `kind`，跨类边标 `bridge` | — |
| GET | `/api/recommend` | 弱协同推荐（LLM 推领域 + GitHub 搜真实仓库） | — |
| GET | `/api/trending` | GitHub 本周涨星 Top10（缓存 1h） | — |
| GET | `/api/query` | 多轴交叉查询 `?axis=domain&value=AI·LLM`（可多组，AND） | — |
| GET | `/api/updates` | 未读的仓库更新事件（本地比对 GitHub 元数据后产生） | — |
| GET | `/api/doctor` | 自检：卡片 / 轴表一致性、LLM 状态、库状态、生效配置、**转录就绪状态** | — |
| POST | `/api/card/regenerate` | 重算某卡片（同时同步多轴表） | 口令 + 同源 |
| POST | `/api/item/feedback` | 卡片反馈（用过 / 弃了 / 还想用） | 口令 + 同源 |
| POST | `/api/reindex` | 从卡片重建全部多轴数据（轴表是派生物，可随时重建） | 口令 + 同源 |
| POST | `/api/updates/check` | 强制比对 GitHub 元数据，产出更新事件 | 口令 + 同源 |
| POST | `/api/updates/seen` | 标记更新事件已读 | 口令 + 同源 |
| DELETE | `/api/item/<id>` | 删除收藏及其多轴数据 | 口令 + 同源 |
| GET | `/health` | 健康检查 `{ok:true}` | — |

所有接口返回 JSON，`Access-Control-Allow-Origin: *`，平台无关。

### 写守卫的分工（为什么不是一刀切白名单）

- **`POST /collect` 不限制跨源**：它的设计前提就是「任意页面都能投递」——书签工具跑在 `github.com` 上，悬浮球会被嵌进第三方页面。全局同源白名单会直接打死这两个入口。
- **删除 / 重算 / 重建 / 反馈 / 更新检查限制同源**：这些是破坏性或非幂等的，只允许本机页面（或 `REPO_ALLOW_ORIGINS` 里放行的主机）调用，挡住「你浏览的任意网页偷偷清空你的收藏」。
- **口令（可选）**：要跑在局域网或公网时设 `REPO_TOKEN`，所有接口都要求带上。

---

## 七、数据模型

```sql
repos (
  id         INTEGER PRIMARY KEY,
  url        TEXT UNIQUE,        -- 原始地址（仓库 / 视频分享链接）
  note       TEXT,               -- 用户可选的"为什么收"
  status     TEXT,               -- queued / carded / pending_meta
  meta       TEXT,               -- 平台元数据 JSON（后台拉取）
  card       TEXT,               -- 多维分析卡片 JSON ← 唯一真源
  kind       TEXT,               -- production（仓库）/ cognition（抖音等）
  raw        TEXT,               -- 认知端原文逐字稿（与 card 并存）
  created_at TEXT,
  source     TEXT                -- manual/recommend/trend/share/bookmark/ball/import
)
-- 多轴模型（多维同步存储与检索的根）：以下都是「可从 card 重建的派生物」
axes      (key, name)                  -- 维度轴定义（domain/tech/scene/gap/motive/timeline）
repo_axis (repo_id, axis_key, value,   -- 仓库在多轴上的同步关联
           weight, source, confidence) -- source: llm / heuristic / user
tags      (repo_id, tag)               -- 轻量标签轴
```

**多维分析卡片（card）字段**：`domain`(领域) / `purpose`(用途) / `tech_stack`(技术栈) / `why_needed`(为什么需要) / `related_nodes`(关联收藏) / `maturity`(成熟度) / `tags`(分面标签) / `recommendation`(推荐指数 0–5) / `source`(llm|heuristic)。

### 两层分类，别搞混

| 概念 | 取值 | 作用 |
|------|------|------|
| **platform** | `github` / `gitlab` / `douyin` | `worker` 按它分支去抓什么 |
| **kind** | `production` / `cognition` | DB 里的**类别**，决定它在图谱上是哪一端 |

`worker` 每次 UPDATE 都会重新断言 `kind` 列，杜绝两套概念漂移。

### 一致性契约（本项目最重要的一条）

**`repos.card` 是唯一真源；`repo_axis` / `tags` 是派生物，必须能随时重建。**

- 任何改写 `card` 的路径都必须紧跟一次 `sync_axes()`（`worker` 与 `regenerate_card` 共用同一落点）。
- 轴表的 `source` / `confidence` 跟随 `card["source"]`：LLM 分析出的标 `llm/0.85`，规则算的标 `heuristic/0.6`，用户备注提炼的标 `user/1.0`。
- 怀疑不一致就跑：`python src/migrate.py`（重建）或 `python src/migrate.py --doctor`（只看），也可以直接访问 `/api/doctor`。
- 回归测试锁住这条契约：`tests/test_core.py::TestConsistencyContract`。

---

## 八、设计约束（铁律）

1. **零外部依赖**：后端源码只用 Python 标准库，测试也用 unittest，不引第三方包。可选外挂（LLM 模块、抖音转录）一律懒加载 + 缺失即降级，不进提交物。
2. **复用设计系统**：前端跟随 `web/theme.css` 的变量与组件，不另起炉灶。主题是暖纸白 + 暖陶土 `--accent`。
3. **不编假洞察**：画像 / 卡片是"启发式预览，非深度分析"，产品内明示；样本稀疏时标注置信度。**推论：不展示无法产生非零值的指标**，也不把种子先验当行为统计（缺角标 `source=seed`）。
4. **收集同步、分析异步**：点击即落库返回，分析后台跑，绝不阻塞用户。
5. **本地优先**：数据存本机 SQLite，不出本机；LLM key 走灯笼侧，不入库、不进日志。
6. **入口一次点击**：收藏最终走到"系统分享直达 + 悬浮球常驻"（原生壳待接通）。
7. **卡片是真源，轴表是派生物**（见 §7 契约）。

---

## 九、路线图

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M0 | 收集入口 + 后端落库 + 后台采集 | 已完成 |
| M1 | 多维分析卡片 + 重算 | 已完成 |
| M2 | 兴趣图谱 / 缺角视图 | 已完成（缺角为种子先验） |
| M3 | 弱协同推荐 + GitHub 涨星 | 已完成 |
| M4 | 文本抽取 + 分享深链 | 已完成（微信机器人未接） |
| M5 | 原生壳 / 系统级悬浮球 | 骨架在，未接通（需 Rust / Android 工具链） |
| M6 | 独立行为画像 / 认知镜像 | 已完成 |
| M7 | 多轴数据模型实体化 | 已完成（一致性契约已锁） |
| M8 | 本周涨星榜 | 已完成（HTML 抓取，改版即失效，已做降级） |
| M9 | 工程加固 | 已完成（配置层 / 存储层 / 契约 / 写守卫 / 测试基线） |
| M10 | **多源收藏与碰撞** | 已完成（认知端 + 逐字稿 + 跨类桥接 + 类型筛选） |

---

## 十、已知短板

- **无鉴权（默认）**：本机自用靠「只绑回环 + 写操作同源守卫」兜底。要跑局域网/公网必须设 `REPO_TOKEN`。
- **缺角是种子先验**：`SEED_GAP_COOCCUR` 是编者预判的常见搭配，不是你的行为统计。样本够了应换成真共现（`domain_links` 已经是真算的那一半）。
- **跨轴检索未图谱化**：图谱仍主要投在 domain 一维上，多轴交叉可视化待做。
- **原生壳未接通**：系统分享直达与常驻浮球是「样本从 2 条变 200 条」的根因，不接通则画像/缺角永远建立在稀疏样本上。
- **涨星榜靠 HTML 抓取**：GitHub 改版即失效（已做降级为空列表 + 缓存）。
- **抖音解析依赖分享页结构**：`_ROUTER_DATA` 平台改版需复核；逐字稿依赖外部 ffmpeg/whisper（可降级）。
- **技术栈轴未归一化**：topic 的近义变体会各占一格（如 `dsh` / `dsh-plugin` / `dsh-plugin-market`）。
- **LLM 复用依赖本机路径**：默认 `D:\测试\lantern-caliper`，可用 `REPO_LLM_ROOT` 覆盖；不可用时自动回退规则（且不覆盖已有 AI 卡）。

---

## 十一、外部参照

- **OpenViking**（volcengine）：Self-evolving Context Database for AI Agents，统一 memory / RAG / skills，URI 命名空间多维。方向同构但它是 Agent 侧重引擎（Rust + AGPL-3.0），本工具不引入；可借鉴其 `recall / capture / commit` 三段式叙事与"技能即资源"思路。

---

## 许可

本项目为个人自用工具，暂未声明开源许可。如需引用请先联系作者。
