# PaperGrep

PaperGrep 是一个用于自动抓取、翻译并追踪论文社区动态的脚本。当前针对 **alphaXiv (https://www.alphaxiv.org)** 的 Trending Papers。脚本会将论文元数据（标题、摘要、AI Overview、作者、日期、点赞、浏览、评论）持久化到本地 SQLite，使用 DashScope / OpenAI 兼容接口将内容翻译为中文，并在每次运行时生成一份报告，对比上一次运行，告诉你哪些是新论文、哪些评论是新的、点赞排行发生了什么变化。

---

## 功能特性

### 数据抓取
- **WebArchive 优先**：支持读取 macOS Safari 保存的 `.webarchive` 文件（默认优先扫描 `cache/alphaXiv.webarchive`）
- **直连网站回退**：若找不到 webarchive，自动请求 `https://www.alphaxiv.org/` 首页
- **JSON-LD 结构化提取**：不是脆弱的 CSS 选择器，而是解析 `<script type="application/ld+json">` 中 `schema.org / ItemList → ListItem → Article` 的结构化元数据（20 篇 Trending 全部字段齐全）
- **DOM 兜底解析**：JSON-LD 缺字段时再用 `extract_dom_papers` 解析一遍 HTML（`extract_all_papers` 会合并两路结果去重）
- **详情页增强**（可关闭）：并发（`ThreadPoolExecutor(max_workers=6)`）拉取每篇论文详情页，补充：
  - **AI Overview / AI Summary / TL;DR**（智能摘要）
  - **完整摘要**（解决列表页 "View More" 截断问题：详情页返回的是展开后的长文本 / meta description / og:description / JSON-LD description 中最长的那个）
  - **评论列表**（优先抽详情页内嵌 `"comments":[…]` JSON，失败回退 DOM）

### 数据库 & 增量检测
- 三张表：`papers` / `comments` / `runs`（详见下文 [数据库 Schema](#数据库-schema) 与 [数据库机制详解](#数据库机制详解)）
- **SHA1 内容哈希变更检测**：`title_hash` / `abstract_hash` / `ai_overview_hash` 字段，仅当 hash 变化才视为内容更新
- **`translated_fields` JSON 位标记**：每篇论文记录哪些字段已翻译，避免重复调用大模型
- **评论去重**：优先 `external_id` 其次 `content_hash`；内容变了自动清空旧译文等待重译
- **点赞排行历史**：每次运行前后都保留 Top 50 点赞排行快照到 `runs` 表
- **历史补全**：每次运行会扫描整个 `papers` 表（限 500 篇）与所有评论，把历史遗漏的未译字段一并补译，保证 DB 翻译状态最终一致
- **用户标记**：`is_read / is_favorite / is_disliked / is_shared` 四个 0/1 标记列，由 SuperDBViewr Web UI 写入，PaperGrep 本身只读不写

### 翻译（LLM）
- **双路由调用**：
  - 默认模型：`qwen-plus` → 使用 DashScope 原生 SDK（`dashscope.Generation.call(prompt=..., max_tokens=32000, temperature=0.7)`）
  - `deepseek-v4-pro` / `qwen3.6-plus` / 其他模型 → 使用 OpenAI 兼容接口（`base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"`，`messages=[system+user]`）
  - 统一读取环境变量 `DASHSCOPE_API_KEY`
- **双协议可选**：
  - `--protocol json`（默认）：要求模型输出顶层 JSON `{"0":…, "1":…}`，配合多级容错解析（strict → strict=False → 转义消毒 → 正则单键救援）
  - `--protocol xml`：用 `<item_0>…</item_0>` 标签分隔每条结果，适合译文里出现大量引号/反斜杠导致 JSON 频繁解析失败的情况
- **批量翻译**：把 title / abstract / overview / 待译评论一次性打包（带索引 + per-item mode）要求模型输出，**单次 HTTP** 返回 N 条译文，最小化 API 开销；条目过多时按 `max_items_per_chunk=50` 或 `max_chars_per_chunk=150000` 自动分块
- **AI Overview 特殊处理**：不再做逐字翻译（公式/伪代码翻译会失真），而是要求模型生成 **约 300 字中文总结**（覆盖研究背景/核心方法/关键创新/实验结论），结果同时写入 `ai_overview_zh` 与 `ai_overview_summary_zh` 两个字段以保持兼容
- **跳过重复翻译**：hash 未变化 + `translated_fields` 已标记 → 不进翻译队列
- **技术术语保留**：ML/AI/CS 专有名词按英文保留

### 报告输出（每次运行一份，Markdown 格式）
- `work/papergrep_report_YYYYMMDD_HHMMSS.md`：Markdown 全中文报告，含 emoji 图标、表格、章节：
  1. **📌 运行概览**（参数表格 + 本次统计，含翻译补充条目数）
  2. **🆕 新增论文**：每篇独立小节，**英文标题、中文标题、AI Overview（中文约 300 字总结，不截断）、论文摘要（中英双语完整不截断）**、点赞/浏览、作者、原文链接
  3. **🔄 更新论文**：按字段表格展示 Before → After
  4. **🌐 翻译补充**：本次新补充/重译的标题、摘要、Overview 总结、评论
  5. **💬 新评论 & 更新评论**：按论文分组，原文+中文翻译完整不截断
  6. **🏆 点赞数排行 Top 20**：排位变动以 🆕⬆️⬇️➖ 直观标识
  7. **📊 本次运行总结**
- `trash/papergrep_report_YYYYMMDD_HHMMSS.json`：机器可读 JSON（**注意路径在 `trash/` 而非 `work/`**），含 `args 快照 / new_papers_full / updated / translation_updates / new_comments / ranking_top20 / summary`
- 每次运行详情也写入 `runs` 表以便回溯

### 数据库浏览器
项目附带两个独立的 DB 查看工具，均不依赖 PaperGrep 主流程，可直接对 `db/papergrep.db` 做只读/标记操作：

- **[DBViewer.py](./DBViewer.py)**：纯命令行交互式浏览器（TUI）。主菜单提供：浏览论文列表（分页）、搜索论文（标题/摘要）、浏览评论、浏览运行记录、显示 Schema 五项功能。
  ```bash
  python DBViewer.py                     # 默认连 db/papergrep.db
  python DBViewer.py --db /path/to.db
  ```
- **[SuperDBViewr.py](./SuperDBViewr.py)**：基于 `http.server` 的本地 Web UI（单文件自带 HTML）。启动后自动打开浏览器，提供论文分页列表、多字段排序（点赞/入库/更新/浏览/评论/发布时间）、搜索、用户标记（已读/收藏/踩/分享）、评论浏览、运行记录查看等。所有 API 走 JSON，前端 SPA 在 `INDEX_HTML` 常量里。
  ```bash
  python SuperDBViewr.py                # 默认 127.0.0.1:8888，自动开浏览器
  python SuperDBViewr.py --port 9000 --no-browser
  python SuperDBViewr.py --db /path/to.db --host 0.0.0.0
  ```
  两个查看器共用同一个 SQLite 文件，标记列（`is_read`/`is_favorite`/`is_disliked`/`is_shared`）由 SuperDBViewr 的 `POST /api/paper/<id>/mark` 写入，DBViewer 仅展示。

---

## 命令行参数

```
positional:  files...             显式指定一个或多个 .webarchive 文件（可选；给出后忽略 --dir）

--after      "YYYY-MM-DD"
             "YYYY-MM-DD HH:MM"   只保留发布时间 ≥ 该时刻的论文
--before     "YYYY-MM-DD"
             "YYYY-MM-DD HH:MM"   只保留发布时间 ≤ 该时刻的论文（仅日期时用当天 23:59:59 补齐）
--model      qwen-plus | deepseek-v4-pro | qwen3.6-plus | ...
                                  指定翻译模型（默认 qwen-plus，亦可通过 $PAPERGREP_MODEL 设置）
--protocol   json | xml           LLM 输出协议（默认 json，亦可通过 $PAPERGREP_PROTOCOL 设置）
                                  xml 模式用 <item_N>…</item_N> 标签分隔，对译文含大量引号/反斜杠的场景更稳
--llm-verbose                      打开 LLM DEBUG 日志（prompt 长度、原始响应、分块解析过程）
                                  亦可通过 $PAPERGREP_LLM_VERBOSE=1 设置
--dir        /path/to/dir          扫描目录下所有 *.webarchive；不传则用 cache/
--no-details                       跳过详情页抓取（仅列表页 JSON-LD 数据；不拿 AI Overview、评论、完整摘要）
--db         /path/to/papergrep.db 自定义 SQLite 路径（默认 db/papergrep.db）
```

---

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `DASHSCOPE_API_KEY` | DashScope / OpenAI 兼容接口的 API Key（必填，否则跳过翻译） | — |
| `PAPERGREP_MODEL` | 默认翻译模型名，被 `--model` 覆盖 | `qwen-plus` |
| `PAPERGREP_PROTOCOL` | 默认 LLM 输出协议，被 `--protocol` 覆盖 | `json` |
| `PAPERGREP_LLM_VERBOSE` | 设为 `1/true/yes/on` 打开 LLM DEBUG 日志 | 关闭 |

---

## 目录结构

```
PaperGrep/
├── PaperGrep.py               # 主脚本（抓取 / 同步 / 翻译 / 报告）
├── DBViewer.py                # 命令行交互式 DB 浏览器
├── SuperDBViewr.py            # Web UI DB 浏览器（本地 HTTP 服务）
├── cache/
│   └── alphaXiv.webarchive    # 默认抓取源（Safari 导出的首页快照）
├── db/                        # 数据库目录（首次运行自动创建）
│   └── papergrep.db           # SQLite（WAL 模式）
├── work/                      # Markdown 报告输出目录（首次运行自动创建）
│   └── papergrep_report_YYYYMMDD_HHMMSS.md
└── trash/                     # JSON 报告与中间产物目录（首次运行自动创建）
    └── papergrep_report_YYYYMMDD_HHMMSS.json
```

---

## 快速开始

### 1. 环境准备
```bash
# 推荐 Python 3.10+ 环境
conda activate base

# 依赖（若尚未安装）：
pip install requests beautifulsoup4 urllib3 dashscope openai
```

### 2. 设置 API Key
```bash
# DashScope（阿里云百炼，支持 qwen-plus / deepseek-v4-pro / qwen3.6-plus）
export DASHSCOPE_API_KEY="sk-..."
```

### 3. 运行主脚本
```bash
# 最简单：用默认 cache/alphaXiv.webarchive + 默认 qwen-plus + 抓详情页
python PaperGrep.py

# 只看 8 月 12 日后新增论文，不抓详情（快很多）
python PaperGrep.py --after "2026-08-12" --no-details

# 指定 webarchive 目录 + 切换到 deepseek 模型
python PaperGrep.py --dir ./cache --model deepseek-v4-pro

# 译文里引号/反斜杠多导致 JSON 解析失败时，切 XML 协议
python PaperGrep.py --protocol xml

# 自定义时间窗
python PaperGrep.py --after "2026-08-10 00:00" --before "2026-08-14"

# 显式指定某个 webarchive 文件
python PaperGrep.py ./cache/alphaXiv.webarchive --model qwen3.6-plus
```

### 4. 查看结果
- 打开 `work/papergrep_report_*.md` 看本次 Markdown 报告（浏览器或 Markdown 预览器打开更佳）
- 查数据库：`sqlite3 db/papergrep.db "SELECT paper_id, likes, title_zh FROM papers ORDER BY likes DESC LIMIT 10;"`
- 用 Web UI 浏览：`python SuperDBViewr.py`
- 所有 `*_zh` 字段一旦翻译完成写入 DB，下次除非对应英文 hash 变化，否则不会重译

---

## 数据库 Schema

连接配置（`get_db`）：`PRAGMA journal_mode=WAL` + `PRAGMA foreign_keys=ON`，`row_factory = sqlite3.Row`。

### papers
| 列 | 类型 | 说明 |
|---|---|---|
| paper_id | TEXT PK | arXiv ID（如 `2608.09867`，带版本号 v1/v2…） |
| url | TEXT NOT NULL | `https://www.alphaxiv.org/abs/…` |
| title_en / title_zh | TEXT | 英文 / 中文标题 |
| abstract_en / abstract_zh | TEXT | 完整摘要（View More 展开后） |
| ai_overview_en | TEXT | 源站 AI Overview 原文 |
| ai_overview_zh | TEXT | AI Overview 的中文处理结果（当前实现为约 300 字总结） |
| ai_overview_summary_zh | TEXT | 与 `ai_overview_zh` 同值，保留字段以兼容历史"短总结/完整翻译"分离设计 |
| authors_json | JSON | `["Alice", "Bob", ...]` |
| published_date | TEXT | ISO 8601 |
| modified_date | TEXT | ISO 8601 |
| likes | INTEGER | 点赞数 |
| views | INTEGER | 浏览数 |
| comment_count | INTEGER | 评论数 |
| title_hash / abstract_hash / ai_overview_hash | TEXT | SHA1，变更检测 |
| translated_fields | JSON | `{"title_zh":true, "abstract_zh":true, "ai_overview_zh":true, "ai_overview_summary_zh":true}` |
| first_seen / last_updated | TEXT ISO 8601 | 首次入库 / 最近 touch 时刻 |
| is_read | INTEGER 0/1 | 用户标记：已读（由 SuperDBViewr 写入；打开详情页自动置 1） |
| is_favorite | INTEGER 0/1 | 用户标记：收藏 |
| is_disliked | INTEGER 0/1 | 用户标记：踩（与 is_favorite 互斥） |
| is_shared | INTEGER 0/1 | 用户标记：已分享 |

### comments
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| paper_id | TEXT FK | `FOREIGN KEY ... REFERENCES papers(paper_id) ON DELETE CASCADE` |
| external_id | TEXT | 源站评论 ID（如果有） |
| author_name | TEXT | |
| content_en / content_zh | TEXT | |
| published_at | TEXT | ISO 8601 |
| content_hash | TEXT | SHA1；变了则清空 content_zh 触发重译 |
| is_updated | INTEGER 0/1 | 新内容覆盖旧评论时标记 |
| | | `UNIQUE(paper_id, external_id)` + `INDEX idx_comments_paper(paper_id)` |

### runs
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| run_time | TEXT | 运行时刻 |
| new_papers_json | JSON | 本次新增 paper_id 列表 |
| updated_papers_json | JSON | 每篇变更字段详情 |
| new_comments_json | JSON | 本次新评论 / 更新评论 |
| ranking_before_json | JSON | 运行前 Top 50 点赞排行 |
| ranking_after_json | JSON | 运行后 Top 50 点赞排行 |
| summary | TEXT | 一句话摘要，如 "New papers: 11; Updated papers: 0; New/updated comments: 0" |

---

## 数据库机制详解

本节是 PaperGrep 的核心设计。整个 DB 层由 `init_db` / `get_db` / `sync_papers` / `_sync_comments_for` 四个函数驱动，所有状态都落在 SQLite 里，重启进程不丢任何东西。

### 1. 三表关系与生命周期

```
┌──────────────┐ 1        N ┌──────────────┐
│   papers     │────────────│   comments   │
│ (paper_id PK)│            │ (id PK, FK)  │
└──────┬───────┘            └──────────────┘
       │
       │  每次 sync_papers 调用前后各拍一次快照
       │  （Top 50 by likes DESC, first_seen ASC）
       ▼
┌──────────────┐
│    runs      │  每次 main() 结束写一行
│ (id PK)      │  new_papers_json / updated_papers_json
└──────────────┘  new_comments_json / ranking_before/after_json / summary
```

- `papers` 是唯一的事实表，`paper_id`（arXiv ID）作主键，跨运行幂等
- `comments` 通过 `paper_id` 外键挂到论文上，`ON DELETE CASCADE` 保证删论文时评论自动清理
- `runs` 是不可变历史日志，每行对应一次 `python PaperGrep.py` 执行；报告文件 `work/*.md` 与 `trash/*.json` 是它的"打印版"，DB 里的 `runs` 行才是"源数据"

### 2. 同步流程（`sync_papers`）的 7 个阶段

主流程 `main()` 把工作划成 7 步，DB 同步占 [4/7]–[5/7]：

```
[1/7] 收集 webarchive / 网页源
[2/7] 解析 JSON-LD + DOM，提取论文元数据
[3/7] 时间过滤（--after / --before）
[4/7] sync_papers：写元数据 + 评论 + 拍 ranking_before 快照
        ├── 4a. 并发抓详情页（除非 --no-details）
        ├── 4b. 逐篇 INSERT 或 UPDATE（见下文"哈希增量"）
        ├── 4c. _sync_comments_for：评论去重写入
        └── 4d. 拍 ranking_after 快照
[5/7] 收集翻译任务 → 批量调 LLM → 回写 *_zh 与 translated_fields
[6/7] build_and_save_report：生成 work/*.md + trash/*.json + 写 runs 表
[7/7] 打印结束横幅
```

### 3. 哈希增量检测（避免无谓更新与重译）

每篇论文入库时，对三个英文字段各算一次 SHA1：

```python
new_title_hash     = content_hash(title)
new_abstract_hash  = content_hash(abstract_full_incoming)
new_ai_overview_hash = content_hash(ai_overview_incoming)
```

`content_hash` 是 `hashlib.sha1(str(s).encode('utf-8')).hexdigest()`，`None` 直接返回 `None`。

**新论文**（DB 里无该 `paper_id`）：直接 `INSERT`，`translated_fields='{}'`，三个 hash 字段写入新值，`first_seen = last_updated = now`。

**已存在论文**（UPDATE 路径）按字段逐个判断：

| 字段 | 更新条件 | 副作用 |
|---|---|---|
| `likes` / `views` / `comment_count` | 数值变了就 always 刷新 | 仅记到 `changes` 列表，不影响翻译 |
| `title_en` | `prev.title_hash != new_title_hash` 且新值非空 | `translated.pop('title_zh')` → 标记需重译 |
| `abstract_en` | hash 不同**或**旧值为空而新值非空 | `translated.pop('abstract_zh')` |
| `ai_overview_en` | hash 不同**或**旧值为空而新值非空 | `translated.pop('ai_overview_zh')` + `pop('ai_overview_summary_zh')` |
| `modified_date` | 字符串不同 | 仅记到 `changes` |

关键设计：**只有 hash 变了的字段才会覆盖英文原文，并从 `translated_fields` 里删除对应 `*_zh` 标记**。hash 没变的字段保留 DB 里的旧英文值（不覆盖），中文译文也保留。这样即便抓取结果因换行/空白略有抖动，只要 SHA1 不变就不会触发无谓重译。

最终 `UPDATE papers SET ..., translated_fields=?, last_updated=? WHERE paper_id=?`，把新的 `translated_fields` JSON 一次性写回。

### 4. `translated_fields` JSON 位标记

`translated_fields` 是一个 JSON 对象，键是字段名，值是 `true`：

```json
{"title_zh": true, "abstract_zh": true, "ai_overview_zh": true, "ai_overview_summary_zh": true}
```

它的语义是"**这个字段已经有可信的中文译文了**"。判断一篇论文是否需要翻译某字段：

```
need_translate(field) = (英文非空) AND (translated_fields[field] 不为 true)
```

- 翻译成功后：`translated[field] = True`，连同 `*_zh` 文本一起 `UPDATE` 写回
- 英文 hash 变化时：`translated.pop(field, None)`，下次扫描就会重新进翻译队列
- 全部字段都已标记 + hash 都没变 → 该论文本次零 LLM 调用

### 5. 评论去重与重译（`_sync_comments_for`）

对每篇论文抓回来的评论列表逐条处理：

1. 计算 `content_hash = SHA1(content_en)`
2. 查重：有 `external_id` 用 `(paper_id, external_id)` 查（表上有 `UNIQUE` 约束）；否则用 `(paper_id, content_hash)` 查
3. **新评论**：`INSERT` 并追加到 `new_comments_tracker`，标记 `translated=False`
4. **已存在但内容变了**（`row.content_hash != chash` 且新内容更长）：`UPDATE` 覆盖 `content_en/content_hash/published_at/author_name`，**`content_zh=NULL`**（清空译文等下次重译），`is_updated=1`，并追加到 tracker 标记 `updated=True`
5. 任一评论有变动 → `UPDATE papers SET last_updated=now WHERE paper_id=?`

评论翻译在 [5/7] 阶段统一补：扫全表 `WHERE content_zh IS NULL OR content_zh = ''`，不限条数，确保 DB 翻译状态最终一致。

### 6. 历史补全（backfill）

`sync_papers` 在收集翻译任务时不是只看本次 fetched 的论文，而是并集三部分：

```
recent_set    = 本次新增 ∪ 本次更新
fetched_ids   = 本次抓到的所有 paper_id
history_ids   = SELECT paper_id FROM papers
                WHERE title/abstract/overview 任一字段未翻译
                ORDER BY first_seen DESC LIMIT 500
```

合并去重后逐篇检查 `translated_fields`，缺哪个补哪个。这意味着：

- 第一次跑只翻译了部分字段（比如 LLM 超时丢了一块），第二次跑会自动补上
- 历史评论缺译也会被扫到（无 LIMIT）
- 上限 500 是为了防一次性任务过大；超过的部分会在后续运行里分批补完

### 7. 点赞排行快照

`sync_papers` 在写元数据前后各执行一次：

```sql
SELECT paper_id, title_en, title_zh, likes
FROM papers ORDER BY likes DESC, first_seen ASC LIMIT 50
```

两次结果分别存到 `runs.ranking_before_json` / `runs.ranking_after_json`。报告里的 Top 20 排位变动（🆕⬆️⬇️➖）就是对比这两个快照算出来的：`delta = before_pos - after_pos`，`before_pos IS NULL` 表示新进榜。

### 8. 用户标记列与迁移

`is_read / is_favorite / is_disliked / is_shared` 是给 SuperDBViewr Web UI 用的，PaperGrep 主脚本只读不写（`SELECT *` 时会带出来，但 `INSERT/UPDATE` 不触碰这四列）。互斥规则由 Web API `api_mark_paper` 保证：收藏与踩互斥，置一时把对方清零。打开论文详情页时 `api_get_paper` 会自动 `UPDATE papers SET is_read=1`。

**迁移机制**：`init_db` 在 `CREATE TABLE IF NOT EXISTS` 之后，对历史 DB 用 `ALTER TABLE papers ADD COLUMN ...` 逐列尝试添加，`except Exception: pass` 吞掉"列已存在"错误。SuperDBViewr 里有独立的 `_migrate_db` 做同样的事，保证两个入口都能升级旧库。所以老用户直接 `git pull` 后运行不会丢数据。

### 9. 事务与并发

- 每个进程一个 `sqlite3.connect`，WAL 模式允许读写并发
- `sync_papers` 在 [4/7] 写完所有元数据后 `conn.commit()` 一次，[5/7] 翻译回写后再 `commit()` 一次——中途 LLM 失败不会丢元数据
- `runs` 行在 [6/7] `build_and_save_report` 里写入，与报告文件原子性绑定（写完 .md 再写 .json 再写 DB）
- 外部进程（SuperDBViewr）可以同时打开同一 DB 做只读查询或写标记列，WAL 保证不阻塞主脚本

---

## LLM 路由策略

| 模型名 | 调用方式 | API Key |
|---|---|---|
| `qwen-plus`（默认） | DashScope 原生 SDK：`Generation.call(model, prompt, max_tokens=32000, temperature=0.7)` | `DASHSCOPE_API_KEY` |
| 以 `deepseek` 开头 | OpenAI 兼容：`base_url=https://dashscope.aliyuncs.com/compatible-mode/v1`，`messages` 带 system+user | `DASHSCOPE_API_KEY` |
| 以 `qwen3.6` 开头 | OpenAI 兼容，同上 | `DASHSCOPE_API_KEY` |
| 其他自定义模型名 | OpenAI 兼容，同上 | `DASHSCOPE_API_KEY` |

调用失败按 `max_retries=2` 重试，退避 `10 * attempt` 秒。`qwen-plus` 走原生 SDK 时会把 system + user 合并成单个 prompt（DashScope 原生接口不接受 messages）。

### 批量翻译请求格式

单次翻译请求是"批量 + 索引 + per-item mode"模式。每条任务的 key 前缀决定翻译方式：

| key 前缀 | mode | 期望输出 |
|---|---|---|
| `TITLE::paper_id` | translate | 纯中文字符串（完整翻译） |
| `ABSTRACT::paper_id` | translate | 纯中文字符串（完整翻译） |
| `COMMENT::comment_id` | translate | 纯中文字符串（完整翻译） |
| `OVERVIEW::paper_id` | summary | 纯中文字符串，约 300 字总结（非逐字翻译） |

JSON 协议示例 prompt：
```
You are a professional translator and research paper summarizer. Process the following texts from English into Chinese.
Preserve technical terms (ML/AI/CS terminology) as-is when appropriate, ensure accuracy, keep Markdown/formatting intact.

For each input i, output JSON value at position i:
  - For TITLE / ABSTRACT / COMMENT inputs: output a single STRING (the full Chinese translation).
  - For AI OVERVIEW inputs: output a single STRING — a Chinese summary of about 300 characters, covering: 研究问题背景, 核心方法, 关键创新点, 主要实验结果与结论。

Wrap all outputs in a single top-level JSON object in the format: {"0": value0, "1": value1, ...}.
Output JSON ONLY, no prose, no markdown fences.

INPUTS with per-item mode:
0. [MODE: TITLE → 返回字符串（中文完整翻译）] Stealing Reasoning Traces from Proprietary LLM APIs
1. [MODE: OVERVIEW → 返回字符串（中文约 300 字总结，非逐字翻译）] Large language models (LLMs) ...
...
```

### JSON 容错解析（`_try_parse_json`）

模型返回的 JSON 经常带非法转义（`\x..`、`\$`、未闭合引号等）。解析走 5 级降级：

1. `json.loads(text)` 严格解析
2. `json.loads(text, strict=False)` 容忍控制字符
3. `sanitize_escapes`：逐字符清洗非法转义，保留 `\" \\ \/ \b \f \n \r \t \uXXXX`，非法序列改成 `\\X` 让 JSON 把它当字面反斜杠+字符
4. 对清洗后的文本再 `strict=False`
5. `_rescue_extract_json_values`：顶层 JSON 彻底坏掉时，用正则按 `"N":` 锚点逐键切片，每片单独尝试解析（含字符串去引号、对象内 `summary/full` 子键扫描）

XML 协议则用 `<item_N>...</item_N>` 标签正则配对抽取，对译文里的引号/反斜杠完全免疫，仅在标签被模型写错时回退到 JSON 解析。

---

## 设计要点与取舍

1. **为什么用 JSON-LD 而不是 CSS selector？**
   alphaXiv 首页直接把 20 篇 Trending 完整元数据写进 `<script type="application/ld+json">`，字段齐全（likes/views/date/authors…），比写一堆容易碎的 class selector 稳得多，解析一次 `json.loads` 全部到手。

2. **"View More" 完整摘要怎么处理？**
   不去模拟 JS 点击（webarchive 里也没法点），而是**拉详情页**，详情页本身返回的就是文章完整页，`meta description` / `og:description` / JSON-LD `Article.description` 三者取最长，再跟列表页短摘要对比取 max。这样最稳，且不依赖浏览器。

3. **为什么翻译要走 batch + 索引 JSON？**
   如果 20 篇论文 × 3 字段 = 60 次 API 调用会巨慢巨贵；一次请求把 60 段文本带索引发过去，模型输出 `{"0":…, "1":…, …}`，单次 HTTP 返回全部译文，成本和延迟都降一个数量级。条目超 50 或字符超 150k 时自动分块，单块失败不影响其它块。

4. **怎么避免重复翻译？**
   三条防线：
   - `title_hash/abstract_hash/ai_overview_hash`：英文没变 → 中文不失效
   - `translated_fields` JSON：标记哪些字段**已经译过**
   - 评论：`content_hash` 变了才清空 `content_zh` 进翻译队列

   第二次同数据再跑，"New papers 0 + Updated 0 + 0 条待译任务" → 零 LLM 调用，秒出报告。

5. **为什么 AI Overview 改成 300 字总结而非完整翻译？**
   AI Overview 里常有公式、伪代码、数值结果，逐字翻译反而失真。改成要求模型产出约 300 字中文总结（覆盖研究背景/核心方法/关键创新/实验结论），同时写入 `ai_overview_zh` 和 `ai_overview_summary_zh` 保持字段兼容。报告里展示这份总结并标注字数。

6. **为什么提供 XML 协议？**
   某些模型的中文译文里频繁出现引号、反斜杠、控制字符，导致 JSON 解析失败率上升。XML 标签模式 `<item_N>…</item_N>` 对这些字符完全免疫，是 JSON 模式频繁失败时的稳健兜底。

7. **为什么 JSON 报告放 `trash/`？**
   `work/` 只放给人看的 Markdown 报告，`trash/` 放机器可读的 JSON 快照（含完整 args、new_papers_full、ranking 等）。JSON 主要供程序回溯/对比用，不希望污染人类浏览的 `work/` 目录。

---

## 将来扩展多站点

脚本入口和数据流已经按多站点设计。目前只有 alphaXiv 一个 adapter，后续加新站点（如 Hugging Face Papers、arXiv Sanity Preserver、Reddit r/MachineLearning 讨论等）只需：

1. 新增一个 `extract_XXX_papers(...)` 函数，输出统一结构：
   ```python
   {"paper_id": str, "url": str, "title_en": str, "abstract_en": str,
    "authors": [str,...], "published_date": str, "modified_date": str,
    "likes": int, "views": int, "comment_count": int, "source": "xxx"}
   ```
2. 在 `main()` 的数据源汇集处（webarchive → HTML → extract_jsonld_papers）加一个分支即可
3. DB / 翻译 / 报告 / 增量检测全部复用，不用再写一遍
