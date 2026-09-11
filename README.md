# 智能收藏箱（repo-collector）

一键收集开源仓库的工具箱。把"收藏"从终态变成认知网络的一次节点登记，累积后成为一面客观的"行为认知镜"。

> 独立工具，与灯笼知识库（lantern-caliper）解耦：仅复用其 LLM 模块做卡片分析，数据自管、不并入任何外部知识库。

---

## 一、架构

单进程、零外部依赖的本地服务。

```
                        ┌─────────────────────────────────────┐
   浏览器 / H5 / 浮球 ──▶│           HTTP 服务 (src/server.py)   │
   系统分享深链         │  路由(薄) · 静态分发 · 后台采集线程    │
                        └───────────────┬─────────────────────┘
                                        │ 调用
                        ┌───────────────┴─────────────────────┐
                        │        分析层 (src/analyze.py)        │
                        │  规则启发式 + LLM 扩展点(灯笼 llm)    │
                        └───────────────┬─────────────────────┘
                                        │ 读写
                        ┌───────────────┴─────────────────────┐
                        │      存储 (src/db.py · SQLite/WAL)    │
                        │  repos 表 + 多轴表(axes/repo_axis)    │
                        └─────────────────────────────────────┘

   配置 (src/config.py)：端口 / 绑定 / 库路径 / 口令 / LLM 路径 / GitHub Token 全走环境变量
   前端静态 (web/)：纯 HTML/CSS/JS，暖纸白 + 暖陶土主题，即时刷新，无构建
   原生壳 (native-shell/)：Tauri / Android 骨架，内嵌同一套 web + ball.js
   测试 (tests/)：零依赖 unittest，86 条，含 HTTP 集成与一致性契约回归
```

### 分层职责

| 层 | 位置 | 职责 | 依赖 |
|----|------|------|------|
| 接入层 | `web/*.html` + `ball.js` | 收集页 / 卡片 / 图谱 / 推荐 / 画像 / 分享；跨平台悬浮球 | 仅浏览器 |
| 服务层 | `src/server.py` | 路由（薄）、静态分发、业务函数、后台采集线程 | 标准库 |
| 分析层 | `src/analyze.py` | 多轴卡片生成、画像聚合、推荐、关联推断；规则优先，LLM 可选 | 标准库 + 灯笼 llm(可选) |
| 存储层 | `src/db.py` + `repo_collector.db` | 统一连接（WAL + busy_timeout）、版本化迁移 | sqlite3 |
| 配置层 | `src/config.py` | 全部可变项集中，环境变量覆盖 | os |
| 原生壳 | `native-shell/` | Tauri/Android 工程，系统分享直达、常驻浮球 | Rust/Kotlin |

---

## 二、目录结构

```
智能收藏箱/
├── run.py                 # 一键启动入口（python run.py）
├── README.md              # 本文件
├── .gitignore
├── src/                   # 后端（Python 标准库，零第三方依赖）
│   ├── config.py          # 配置层：所有可变项走环境变量（REPO_*）
│   ├── db.py              # 存储层：统一连接 + schema 版本化迁移
│   ├── server.py          # HTTP 服务：薄路由 + 业务函数 + 后台采集
│   ├── analyze.py         # 分析层：卡片·画像·推荐·关联；规则 + LLM 唯一入口
│   └── migrate.py         # 多轴数据维护 CLI：重建 / 自检
├── web/                   # 前端静态（暖纸白主题，无构建）
│   ├── collect.html       # 收集入口（移动优先 H5，可加到主屏幕）
│   ├── cards.html         # 多维分析卡片 + 胶囊云筛选 + 跨页钻取
│   ├── map.html           # 兴趣图谱 / 缺角视图 + 领域相邻（真实数据）
│   ├── recommend.html     # 弱协同推荐 + GitHub 本周涨星 Top10
│   ├── profile.html       # 行为画像 / 认知镜像（领域·技术栈·时段·来源·缺角）
│   ├── share.html + host.html + embed.html + ball-demo.html
│   ├── ball.js            # 跨平台悬浮球（拖拽 / 剪贴板 / 拖入收藏）
│   ├── nav.js             # 统一底部导航（含可选口令注入）
│   └── theme.css          # 设计系统（变量 + 组件：card/pill/note/g-tip）
├── tests/                 # 零依赖测试
│   ├── common.py          # 隔离基类：临时库 + 断网 + worker 空转
│   ├── test_core.py       # 单元：解析 / 启发式 / LLM 胶水 / 多轴 / 检索 / 守卫
│   └── test_http.py       # 集成：真起服务真发请求（路由 / 静态 / 写守卫 / 深链）
├── native-shell/          # 原生壳骨架（未接通系统分享）
│   ├── tauri/             # 桌面端 Tauri 工程
│   └── android/           # 移动端 Android 工程（SYSTEM_ALERT_WINDOW 浮球）
├── docs/                  # 文档
│   ├── 产品说明书.md       # 产品定位 / 场景 / 功能模块 / 数据模型
│   ├── 开发节点计划表.md   # M0–M8 里程碑
│   └── 项目评价报告.md/.html # 2026-09-11 全量评审（问题清单与方向）
└── repo_collector.db      # 本地 SQLite 数据（不进版本库，含 -wal/-shm）
```

> 路径约定：`config.py` 以项目根为 `ROOT`，静态从 `web/` 取，DB 在根。移动文件不影响运行。

---

## 三、快速开始

环境：Python 3.11+（标准库即可，无需 pip install）。

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

### 配置（全部走环境变量，都有默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `REPO_PORT` | `8732` | 端口 |
| `REPO_HOST` | `127.0.0.1` | 绑定地址；改局域网给手机用时改成 `0.0.0.0` |
| `REPO_DB` | `<根>/repo_collector.db` | 库路径 |
| `REPO_TOKEN` | 空（关闭） | 设置后所有接口都要带 `X-Collector-Token`（或 `?k=`） |
| `REPO_ALLOW_ORIGINS` | 空 | 额外放行哪些跨源主机能发写操作（局域网调试时填自己机器 IP） |
| `REPO_MAX_BODY` | `262144` | 请求体上限（字节），超限 413 |
| `REPO_LLM_ROOT` | `D:\测试\lantern-caliper` | 复用的 LLM 模块位置 |
| `REPO_LLM_TIMEOUT` | `20` | 后台出卡的 LLM 超时（秒） |
| `REPO_LLM_TIMEOUT_FAST` | `9` | 交互链路（点「重算」）的短预算，必须小于前端 12s |
| `REPO_GITHUB_TOKEN` | 空 | 配了就走认证调用，否则搜索限速 10 次/分 |

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

不联网、不依赖灯笼、不碰正式库（每个用例独立临时库）。

---

## 四、接口清单

| 方法 | 路径 | 说明 | 写守卫 |
|------|------|------|--------|
| POST | `/collect` | 入参 `{url?, note?, text?, source?}`；`text` 会抽取第一个合法仓库地址。同步落库 + 去重 + 后台出卡 | 口令（若配）；**不限制跨源**（书签/浮球需跨源投递） |
| GET | `/collect?url=&text=&note=&source=` | 分享深链，打开即收藏，返回移动友好结果页 | 口令（若配） |
| GET | `/api/items` | 收藏列表（含 meta/card），支持 `?q=` 全文搜索 | — |
| GET | `/api/cards` | 卡片筛选，`?domain[]=&tech[]=&motive[]=&q=&sort=`（同层 OR / 跨层 AND） | — |
| GET | `/api/facets` | 筛选面（domain/tech/motive 三层，含 count 与平均置信度） | — |
| GET | `/api/profile` | 行为画像聚合数据（含 `sources` 来源分布） | — |
| GET | `/api/graph` | 图谱节点 + 边 + 缺角（`gap_source=seed`）+ `domain_links` 领域相邻（真实数据） | — |
| GET | `/api/recommend` | 弱协同推荐（LLM 推领域 + GitHub 搜真实仓库） | — |
| GET | `/api/trending` | GitHub 本周涨星 Top10（缓存 1h） | — |
| GET | `/api/query` | 多轴交叉查询 `?axis=domain&value=AI·LLM`（可多组，AND） | — |
| GET | `/api/doctor` | 自检：卡片/轴表一致性、LLM 状态、库状态、生效配置 | — |
| POST | `/api/card/regenerate` | 重算某仓库卡片（`{id}`）。会同时更新卡片与多轴表 | 口令 + 同源 |
| POST | `/api/reindex` | 从卡片重建全部多轴数据（轴表是派生物，可随时重建） | 口令 + 同源 |
| DELETE | `/api/item/<id>` | 删除收藏及其多轴数据 | 口令 + 同源 |
| GET | `/health` | 健康检查 `{ok:true}` | — |

所有接口返回 JSON，`Access-Control-Allow-Origin: *`，平台无关。

### 写守卫的分工（为什么不是一刀切白名单）

- **`POST /collect` 不限制跨源**：它的设计前提就是「任意页面都能投递」—— 书签工具跑在 `github.com` 上，悬浮球会被嵌进第三方页面。全局同源白名单会直接打死这两个入口。
- **删除 / 重算 / 重建限制同源**：这三个是破坏性或非幂等的，只允许本机页面（或 `REPO_ALLOW_ORIGINS` 里放行的主机）调用，挡住「你浏览的任意网页偷偷清空你的收藏」。
- **口令（可选）**：要跑在局域网或公网时设 `REPO_TOKEN`，所有接口都要求带上。

---

## 五、数据模型

```sql
repos (
  id         INTEGER PRIMARY KEY,
  url        TEXT UNIQUE,        -- 仓库地址
  note       TEXT,               -- 用户可选的"为什么收"
  status     TEXT,               -- queued / carded / pending_meta
  meta       TEXT,               -- GitHub 元数据 JSON（后台拉取）
  card       TEXT,               -- 多维分析卡片 JSON ← 唯一真源
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

### 一致性契约（本项目最重要的一条）

**`repos.card` 是唯一真源；`repo_axis` / `tags` 是派生物，必须能随时重建。**

- 任何改写 `card` 的路径都必须紧跟一次 `sync_axes()`（`worker` 与 `regenerate_card` 共用同一落点）。
- 轴表的 `source` / `confidence` 跟随 `card["source"]`：LLM 分析出的领域标 `llm/0.85`，规则算的标 `heuristic/0.6`，用户备注提炼的标 `user/1.0`。
- 怀疑不一致就跑：`python src/migrate.py`（重建）或 `python src/migrate.py --doctor`（只看），也可以直接访问 `/api/doctor`。
- 回归测试锁住这条契约：`tests/test_core.py::TestConsistencyContract`。

---

## 六、设计约束（铁律）

1. **零外部依赖**：后端只用 Python 标准库，测试也用 unittest，不引第三方包。
2. **复用设计系统**：前端跟随 `web/theme.css` 的变量与组件，不另起炉灶。主题是暖纸白 + 暖陶土 `--accent`（**不是**"宣纸墨韵"那套 `--paper/--cinnabar`）。
3. **不编假洞察**：画像/卡片是"启发式预览，非深度分析"，产品内明示；样本稀疏时标注置信度。**推论：不展示无法产生非零值的指标**，也不把种子先验当行为统计（缺角标 `source=seed`）。
4. **收集同步、分析异步**：点击即落库返回，分析后台跑，绝不阻塞用户。
5. **本地优先**：数据存本机 SQLite，不出本机；LLM key 走灯笼侧，不入库、不进日志。
6. **入口一次点击**：收藏最终走到"系统分享直达 + 悬浮球常驻"（原生壳待接通）。
7. **卡片是真源，轴表是派生物**（见上节契约）。

---

## 七、路线图

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M0 | 收集入口 + 后端落库 + 后台采集 | 已完成 |
| M1 | 多维分析卡片 + 重算 | 已完成 |
| M2 | 兴趣图谱 / 缺角视图 | 已完成（缺角为种子先验） |
| M3 | 弱协同推荐 + GitHub 涨星 | 已完成 |
| M4 | 文本抽取 + 分享深链 | 已完成（结果页 / 深链）；微信机器人未接 |
| M5 | 原生壳 / 系统级悬浮球 | 骨架在，未接通（需 Rust / Android 工具链编译） |
| M6 | 独立行为画像 / 认知镜像 | 已完成 |
| M7 | 多轴数据模型实体化 | 表与索引已建，一致性契约已锁；跨轴检索仍是列表筛选 |
| M8 | 本周涨星榜 | 已完成（HTML 抓取，改版即失效，已做优雅降级） |
| M9 | 补齐加固（2026-09-11） | 配置层 / 存储层 / 一致性契约 / 写守卫 / 测试基线 —— 详见 `docs/项目评价报告.md` |

---

## 八、已知短板

- **无鉴权（默认）**：本机自用靠「只绑回环 + 写操作同源守卫」兜底。要跑局域网/公网必须设 `REPO_TOKEN`。
- **缺角是种子先验**：`SEED_GAP_COOCCUR` 是编者预判的常见搭配，不是你的行为统计。样本够了应换成真共现（`domain_links` 已经是真算的那一半）。
- **跨轴检索未图谱化**：图谱只投在 domain 一维上，交叉可视化待做。
- **原生壳未接通**：系统分享直达与常驻浮球是「样本从 2 条变 200 条」的根因，不接通则画像/缺角永远建立在稀疏样本上。
- **涨星榜靠 HTML 抓取**：GitHub 改版即失效（已做降级为空列表 + 缓存）。
- **技术栈轴未归一化**：topic 的近义变体会各占一格（如 `dsh` / `dsh-plugin` / `dsh-plugin-market`）。
- **LLM 复用依赖本机路径**：默认 `D:\测试\lantern-caliper`，可用 `REPO_LLM_ROOT` 覆盖；不可用时自动回退规则（且不覆盖已有 AI 卡）。

---

## 九、外部参照

OpenViking（volcengine）：Self-evolving Context Database for AI Agents，统一 memory/RAG/skills，URI 命名空间多维。方向同构但它是 Agent 侧重引擎（Rust + AGPL-3.0），本工具不引入；可借鉴其 `recall/capture/commit` 三段式叙事与"技能即资源"思路。
