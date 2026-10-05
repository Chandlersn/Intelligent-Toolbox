# 智能收藏箱（Intelligent Toolbox）

> 把你随手收下的仓库、视频、文章，攒成一面照见自己的镜子。
> 本地优先 · 单进程 · 开箱即用

---

## 为什么做这个

收藏正在变成遗忘的开端：刷到的东西「先收藏，回头再看」，可那个「回头」从不发生。更麻烦的是，你的数据碎在十几个 App 的围墙里、互不相通，也取不出来。**AI 很强，但它不认识你。**

智能收藏箱做的事很简单：把来源各异的收藏**聚到本地同一处、自动归类与关联、再反过来喂给 AI**——于是「你摄入的认知」和「你产出的作品」第一次碰面。

---

## 它能帮你做什么

**① 把「存了不看」变成「存了就有用」**
收进来即由后台自动分类、归位、建立关联——你只管收，整理交给它，收藏从终点变成一次认知网络的节点登记。

**② 把碎片聚成一张网，再归纳成主题**
不管从 GitHub、抖音还是文章里收，都落进同一张多轴图谱；「探索」页还进一步把零散收藏**归纳成主题**，并标出每个主题的**缺口**，让你看见认知盲点。

**③ 让数据回到你手里，也喂得起 AI**
数据只存本机 SQLite，默认不出本机；一旦结构化，就能被你自己的 LLM 调用，产出「只对你成立」的答案。

---

## 功能模块一览

| 模块 | 位置 | 职责 |
|------|------|------|
| 收集入口 | `web/collect.html` · `web/ball.js` · `web/host.html` · `web/embed.html` | H5 粘贴、悬浮球、书签、分享深链、纯文本投递 —— 统一打向 `/collect` |
| 后台采集 | `src/server.py` | 解析来源 → 转写 → 笔记整理 → 出卡 → 落库 → 同步轴表 |
| 卡片分析 | `src/analyze.py` | 领域/用途/技术栈/成熟度/标签/推荐、关联、画像；规则优先，LLM 可选 |
| 逐字稿转写 | `src/douyin_source.py` | 抖音分享页解析、下载音视频、ffmpeg 抽音频、whisper 转写 |
| 笔记结构化 | `src/analyze.py` | 口语逐字稿整理为 markdown 笔记（概述/要点/纪要），长文分块不截断 |
| 网页/文章 | `src/web_source.py` | 标准库抓取标题与正文片段（认知端来源） |
| 主题归纳 | `src/topics.py` | 集合级归纳：N 条 → M 个主题（成员/覆盖度/缺口），draft→confirmed 生命周期 |
| 探索页 | `web/explore.html` · `web/topics.html` · `web/map.html` | 主题与图谱同一页内分段切换；跨侧桥接、缺角、领域相邻 |
| 画像 / 推荐 | `web/profile.html` · `web/recommend.html` | 行为聚合；弱协同推荐 |
| 全文 / 对话检索 | `repo_fts` · `web/cards.html` | FTS5 trigram 中文子串检索；本地证据召回后由 LLM 回答并引用条目 |
| MCP 查询 | `src/mcp_server.py` | stdio 只读工具：收藏检索、条目读取、统计；不暴露设置密钥 |
| 导出 / 备份 | `/api/export` · `/api/backup` | Markdown / JSON 导出；SQLite Online Backup 一致性快照 |
| 回顾重访 | `/api/review` · `web/cards.html` | 最久未回看的已出卡优先；回顾时间本机持久化 |
| 存储 | `src/db.py` | SQLite（WAL）+ 版本化迁移 + FTS5 索引 |
| 配置 | `src/config.py` | 全部可变项集中，`REPO_*` 覆盖，带安全默认值 |

---

## 快速开始

需要 **Python 3.11+**，核心功能零 `pip install`。无虚拟环境时：

```bash
python run.py
```

Windows 上若项目已创建 `.venv`（例如安装了逐字稿依赖），请直接使用其解释器启动，避免重复切换进程：

```powershell
.venv/Scripts/python.exe run.py
```

打开 `http://127.0.0.1:8732` 即可。

| 页面 | 地址 |
|------|------|
| 收集页 | `/`（或 `/collect.html`） |
| 卡片页 | `/cards.html` |
| 探索页（主题 + 图谱） | `/explore.html` |
| 推荐页 | `/recommend.html` |
| 画像页 | `/profile.html` |
| 自检 | `/api/doctor` |
| 全文搜索 API | `/api/search?q=关键词` |
| 对话检索 API | `POST /api/search/ask`（需配置 LLM） |
| Markdown / JSON 导出 | `/api/export?format=markdown` · `/api/export?format=json` |
| SQLite 完整备份 | `/api/backup` |
| MCP 只读查询 | `python src/mcp_server.py`（stdio） |

### 全文与对话检索

卡片页搜索框现在覆盖仓库元数据、备注、网页正文、抖音逐字稿与结构化笔记。SQLite FTS5 使用内置 `trigram` tokenizer，中文可按子串检索；1–2 个字符自动退回 LIKE。模型开启后可在「问问收藏箱」用自然语言检索，回答基于本地召回证据并附收藏条目引用。模型不可用时，全文搜索仍可用。

### 本地 MCP

MCP 服务只读打开本地 SQLite，不提供写入工具，也不会暴露 `app_settings` 中的 API Key。将以下 stdio server 条目合并到兼容客户端自己的配置（替换为实际绝对路径）；不要覆盖已有 server 配置：

```json
{
  "mcpServers": {
    "intelligent-toolbox": {
      "command": "D:/个人开发/智能工具箱/.venv/Scripts/python.exe",
      "args": ["D:/个人开发/智能工具箱/src/mcp_server.py"]
    }
  }
}
```

提供 `search_saved_items`、`get_saved_item`、`get_collection_stats` 三个只读工具。无需单独启动 HTTP 服务以外的守护进程；MCP 客户端会按需启动 stdio 子进程。

### 导出与备份

设置页「导出与备份」可下载 Markdown（便于 Obsidian）或 JSON（收藏、图谱轴、主题、更新事件），两种内容导出都不含模型/GitHub 密钥。SQLite 完整备份采用 Online Backup API，包含应用设置与密钥；备份文件需妥善保管，不要公开分享。

---

## 配置（全可选）

**接入 LLM（强烈建议但不强制）**：默认用内置规则出卡（离线、零配置）；也可在「设置 → 模型与能力」配置任意 OpenAI 兼容服务的 Base URL、模型名与 API Key。设置存于本机 SQLite 的 `app_settings`，不会写入代码或日志；未启用/不可用时自动回退规则。旧版外部 `llm.py` 接入仍可通过 `REPO_LLM_ROOT` 使用。打开 `/api/doctor` 看 `llm.available` 是否为 `true`。

**GitHub Token（可选）**：匿名搜索限速 10 次/分，配只读 token 解锁更高额度（`REPO_GITHUB_TOKEN=ghp_xxx`）。

**对外暴露**：局域网/公网使用时绑定 `0.0.0.0` 并设口令，所有接口即需校验：

```bash
REPO_HOST=0.0.0.0 REPO_TOKEN=my-secret python run.py
```

**逐字稿（可选）**：默认抖音只解析元数据；想真出逐字稿 + 结构化笔记，装两个本地依赖（不进仓库）：

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install imageio-ffmpeg faster-whisper
```

---

## 技术架构

单进程本地服务，路由很薄，重活交给后台线程；前端纯静态、无构建步骤。

```
  浏览器 / 悬浮球 / 分享链接
            │
            ▼
    HTTP 服务（薄路由）────────────▶ 后台采集线程
            │                          ├─ GitHub / GitLab API ──▶ 仓库卡片
            │                          └─ 抖音解析 ─▶ 抽音频 ─▶ whisper 逐字稿 ─▶ LLM 笔记
            ▼
     分析层（规则优先，可选 LLM）──▶ 主题归纳 / 图谱 / 画像 / 推荐
            │
            ▼
      SQLite（本地，WAL）
```

| 层 | 位置 | 职责 |
|----|------|------|
| 接入层 | `web/` | 收集 / 卡片 / 探索（主题+图谱）/ 推荐 / 画像；跨平台悬浮球 |
| 服务层 | `src/server.py` | 路由、静态分发、业务函数、后台采集线程 |
| 分析层 | `src/analyze.py` | 卡片、笔记结构化、画像、推荐、关联；规则优先，LLM 可选 |
| 归纳层 | `src/topics.py` | 集合级主题归纳（成员/覆盖度/缺口），draft→confirmed 生命周期 |
| 认知端 | `src/douyin_source.py` · `src/web_source.py` | 分享页解析、音视频转写；网页正文抽取 |
| 存储层 | `src/db.py` | SQLite 统一连接（WAL）+ 版本化迁移 |
| 配置层 | `src/config.py` | 全部可变项集中，`REPO_*` 覆盖 |

**技术选型**：后端主服务只用 Python 标准库（HTTP/SQLite/正则/urllib），核心零第三方依赖；前端原生 JS + CSS，无框架。LLM 调用兼容 OpenAI API；FTS5 使用 SQLite 内置扩展；转写依赖是可选安装项，缺失时自动降级。MCP stdio server 同样只用标准库。

**鉴权与安全**：默认只绑回环地址；写操作有同源守卫，只认本机回环来源。对外暴露时开 `REPO_TOKEN` 校验口令。LLM/GitHub 凭据存本机 SQLite 设置表，不进代码或日志；JSON/Markdown 内容导出会排除凭据。完整数据库备份包含凭据，需妥善保管。

**存储**：收藏主表 `repos` 存地址、备注、多维卡片（含结构化笔记）、认知端原文；多轴数据（domain/tech/scene/gap/motive/timeline）为可从卡片重建的派生物，重算即同步。主题归纳产出自成 `topics` / `topic_members` 表，重算只替换 `draft`，已确认主题不受影响。

---

## 适合谁 / 不适合谁

**适合**
- 收藏了很多、却从没回看过，想把「收藏」变成「用得上」的人
- 想用本地数据喂自己的 AI，而不是把数据交给平台的人
- 做内容 / 做项目、想看见「学」与「做」之间关联的人

**不适合**
- 要云端多端实时同步、团队协作的——这是**单机本地优先**工具，没做云端同步
- 要现成 SaaS 体验、不愿碰命令行启动的——它需要本地 `python run.py`
- 收藏量极小（少于 4 条）时，主题归纳等集合级功能暂不触发

---

## 隐私

数据只存本机 SQLite，默认只绑 `127.0.0.1`；音视频与转写全在本地。启用远程 LLM 时，检索/分析所需文本会发往你配置的模型服务；API Key 保存在本地 SQLite 设置表、不进代码或日志。内容导出排除密钥，数据库备份包含密钥。

---

## 版本迭代

| 版本 | 日期 | 主题 | 内容 |
|------|------|------|------|
| v1.0 | 2026-09-12 | 核心链路 | 收集 → 自动出卡 → 多轴图谱的主链路；仓库更新检测（本地比对 GitHub 元数据 → 横幅提示） |
| v1.1 | 2026-09-13 | 多源与碰撞 | 新增抖音认知端来源，认知/生产双类型分轨；跨类碰撞桥接（看的视频 ↔ 收的仓库）；全站界面收敛统一 |
| v1.2 | 2026-09-16 | 认知端深化 | 新增网页/文章来源（自动抓标题正文）；抖音逐字稿转写 + 口语稿结构化为笔记；长文阅读优化 |
| v1.3 | 2026-09-18 | 体验大版本 | 桌面闭环（系统分享直达 + 悬浮球）；设置页（明暗/主题色/字体/字号）；模型能力应用内可配；AI 缺角识别；卡片滑动切换、长文目录/进度条；桌面壳导航（页面常驻切换不刷新）+ 骨架屏 |
| v1.4 | 2026-09-19 | 主题归纳 | 集合级主题归纳（成员/覆盖度/缺口）；主题与图谱合并为「探索」页 |
| v2.0 | 2026-10-02 | 原生桌面入口 | Tauri 悬浮球：透明置顶、满屏拖放、单击收剪贴板、双击开主页、Alt+Q 全局一键收、右键菜单；GitHub Token 应用内配置（5000 次/时）；批量导入冷启动（浏览器书签/粘贴链接）；更新提示一次已读全局消失；弹窗全面主题化 |

---

## 许可

个人自用工具，暂未声明开源许可；如需引用请先联系作者。
