---
name: l4d2-workshop-mirror
description: Search, identify, resolve dependencies and download L4D2 workshop campaign maps via MCP tools
category: gaming
---

# L4D2 Workshop Mirror MCP — 使用说明书

> 面向使用者的操作手册：怎么用 11 个工具、每个返回字段什么意思、遇到各种情况怎么处理。
> 系统设计与实现原理见 `ARCHITECTURE.md`，部署与运维见 `DEPLOYMENT.md`。

本 MCP 负责 L4D2 工坊战役地图的**搜索、识别、依赖解析、下载**。
所有脏活（网络请求、黑名单/去重/死图判定、Part 系列识别、依赖解析、文件校验）都在 MCP 内部完成，
你只需调用工具，**不需要也不应该写 Python 脚本或调 HTTP 接口**。

---

## 1. 快速开始（3 个最常用操作）

| 用户需求 | 你的动作 |
|---|---|
| "下 X"（有 id/链接） | `install_campaign(workshop_id="id 或链接")` |
| "下 X"（只有标题） | `install_campaign(title="X")` |
| "X 有没有 / id 是多少" | `resolve_map(query="X")` |

大图（几百 MB）用 `install_campaign(..., async_mode=True)` + `get_download_job` 轮询，避免长阻塞。

---

## 2. 工具速查（11 个）

### 下载类

**`install_campaign`** — 一站式下载（识别 → 解析依赖 → 下载 → 落盘 → 更新去重）
- 参数：`workshop_id`（纯数字 id 或 steam 链接）/ `title`（二选一）、`force`（默认 False，忽略去重）、`auto_parts`（默认 True）、`strict`（默认 True）、`async_mode`（默认 False）
- 返回必看：`eligibility`、`preview_only`、`suggested_action`、`main`（含 `saved_path`/`size_mb`/`title_zh`）、`parts`、`dependencies`、`candidates`、`job_id`
- 何时用：用户要下载、要给特定 id/链接/标题落地

**`get_download_job`** — 查询异步下载进度
- 参数：`job_id`
- 返回：`status`（queued/running/done/error/not_found）、`elapsed_s`、`result`（完成后是 install 完整结果）
- 何时用：`install_campaign(async_mode=True)` 之后轮询（隔 10-20 秒）

### 查询类

**`resolve_map`** — 标题解析为候选（不下载）
- 参数：`query`、`limit`（默认 5）
- 返回：`items[]`（workshop_id/title/title_zh/size_mb/is_campaign_score/match_ratio/in_curated_list）
- 何时用："XXX 有没有"、定位标题对应的 id；`install_campaign(title=...)` 内部也用它

**`search_campaigns`** — 搜索工坊战役（自动过滤 mod/黑名单/去重/死图）
- 参数：`keyword`、`days`、`sort`、`limit`（≤30）、`include_ambiguous`
- 返回：`items[]` + `summary`（high_confidence/ambiguous/filtered_out/dead 计数）
- 何时用：按关键词找图、"最近新图"的底层

**`get_map_detail`** — 单图详情 + 判定结果
- 参数：`workshop_id`、`include_description`（默认 False，True 时截断 500 字符）
- 返回：title/title_zh/size_mb/subscriptions、`is_campaign`/`score`/`reasons`/`ambiguous`/`definitely_mod`、`series_info`、`eligibility`、`dead`/`dead_reason`、`in_curated_list`
- 何时用："X 怎么样"、下载前查判定

**`check_eligibility`** — 下载前预检（零网络）
- 参数：`workshop_id` / `title`（至少一个）
- 返回：`state`（见 §3 状态字典）、`days_remaining`、`source`
- 何时用：预检一张图能不能下；批量过滤

**`list_new_campaigns`** — 最近 N 天新增战役
- 参数：`days`（默认 7）、`limit`（默认 20）
- 何时用："最近有什么新图"

**`query_curated_list`** — echocn 人工策展名单（461 条高质量，支持模糊拼写与繁简）
- 参数：`keyword`、`tag`（如"高质量"/"高难度"/"推荐"）、`limit`
- 返回：`items[]`（title_zh/title_original/workshop_id/tags/chapters/file_size_mb）+ `cached_at`
- 何时用：找图优先查这里（质量最高）；标题模糊/简繁不确定时

### 系列与维护类

**`find_series_parts`** — 识别系列 Part
- 参数：`workshop_id`、`aggressive`（默认 True）
- 返回：`is_series`、`base_name`、`total_parts`、`parts[]`（n/workshop_id/title/source）、`confidence`、`status`（死图时为 "unavailable"）
- 何时用：确认一张图是否属于系列、要下全部 Part 前

**`refresh_curated_list`** — 手动刷新 echocn 缓存（排障用）
- 返回：`success`/`total`/`sha256`/`cached_at`

**`sync_idle_folder`** — 扫描闲置目录同步到去重名单（`install_campaign` 完成后自动调一次，一般无需手动）

---

## 3. 状态字典（返回字段全枚举）

### `eligibility`（install_campaign）

| 值 | 含义 | 你的处理 |
|---|---|---|
| `allowed` | 已下载完成（preview_only=false） | 按 `main` 汇报结果 |
| `needs_confirmation` | 判定模糊 或 标题解析多候选 | 展示 reasons/candidates 让用户确认，确认后传 `strict=False` 或 `workshop_id` 重试 |
| `rejected_as_mod` | 判定为 mod | 问用户是否确认强制（`strict=False`） |
| `blacklisted` | 黑名单，**永远不可下载** | 直接回绝，任何参数都绕不过 |
| `dedup_active` | 75 天去重期内 | 告知剩余天数，用户确认才 `force=True` |
| `dead` | 死图（Steam 已删/私密/无效 id） | 告知"该物品已不可用"及原因；若 id 是手打的提醒核对 |
| `not_found` | 标题没解析到/详情不存在 | 向用户要工坊链接 |
| `failed` | 内部错误（网络/落盘） | 给一句原因 + 建议重试 |

### `state`（check_eligibility）
`allowed` / `blacklisted` / `dedup_active` / `dedup_expired` / `dead` / `not_found`

### `suggested_action`
`auto_download`（已下完）/ `ask_user_confirm`（等用户决策）/ `reject`（拒绝）/ `download_in_progress`（async 排队中，**不是要你确认**）

### 其他关键字段
- `preview_only=True`：**没有真下载**，处于等用户决策状态——不要假装下载成功
- `status=queued`（async 响应）：任务已在后台执行，轮询 `get_download_job`
- `dead=true`（get_map_detail）/ `status=unavailable`（find_series_parts）：死图，不要推荐/重试
- `main.saved_path`：服务器上的绝对路径，发文件时原样传给平台文件工具，**不要猜路径/拼文件名**

---

## 4. 典型工作流

### "下 Blood Tracks"（有 id）
→ `install_campaign(workshop_id="1880838761")`。看 `suggested_action`：
- `auto_download` → 已下完，按 `main.saved_path` 汇报/上传
- `ask_user_confirm` → preview_only=True 没真下，转述 `judgment.reasons` 让用户决定，确认后 `strict=False` 重试

### "下死亡小镇"（只有标题）
→ `install_campaign(title="死亡小镇")`：
- 唯一高可信匹配（score≥3 且与第二名差≥2 且标题相似度≥0.8）→ 自动下载
- 多候选 → 返回 `candidates[]`，展示（中文名/大小/评分）让用户选，选定后传 `workshop_id` 重试

### "下 https://steamcommunity.com/sharedfiles/filedetails/?id=1880838761"
→ 直接 `install_campaign(workshop_id="<整个链接>")`，工具自动提取 id，**不要自己写正则**

### 大图（几百 MB）
→ `install_campaign(..., async_mode=True)` → 回复"开始下载了，稍等" → `get_download_job` 轮询（隔 10-20 秒）→ done 后汇报

### "最近有什么新图"
→ `list_new_campaigns(days=7)`；太少再 `search_campaigns(..., include_ambiguous=True)`（ambiguous 需确认）

### "这张图怎么样"
→ `get_map_detail(workshop_id=...)`，看 `score/ambiguous/definitely_mod/reasons`

### 标题没 ID
→ 先 `query_curated_list(keyword="X")`（质量最高），找不到再 `search_campaigns(keyword="X")`，都找不到 → 要链接

### 系列 Part（"下 X 的 part2"、"X 是 2/5"）
→ `find_series_parts(workshop_id=...)` 或 `resolve_map` 定位所有 Part 的 id，再 `install_campaign`（auto_parts 默认会一并下载全部 Part）

### 用户明确要下 mod
→ `install_campaign(..., strict=False)`（用户明确意图，工具让步；黑名单除外）

---

## 5. 群聊场景（AstrBot）

### 简短输入 → 动作映射

| 用户说 | 动作 |
|---|---|
| "下 X" / "搞个 X" / "求 X" | `install_campaign(title="X")` 或带 id/链接 |
| "X 有没有" / "X 能下吗" | `resolve_map(query="X")`，展示候选 |
| "下 X 的 part2 / 2/5" | `find_series_parts` 定位后 `install_campaign` |
| "X 怎么样" / "X 好玩吗" | `get_map_detail` |
| "最近有什么新图" / "新图" | `list_new_campaigns(days=7)` |
| "我玩过的 X" / "不要下 X" | 一般不需要工具；说明去重机制即可 |

### 回复纪律

1. **回复 ≤2 句**，不贴 JSON、不贴 reasons 原文
2. 候选列表：`①《中文名》(原名) 大小 评分 ②...`，最多 3 条
3. 下载成功：`已下好《死亡小镇》，33.6MB`，需要时附文件（用 `saved_path`）
4. 下载失败：一句原因 + 建议（换 id / 稍后重试 / 提供链接）
5. 标题多候选时**必须让用户选**，不要自作主张下第一个
6. dedup 拦截：`这张图 75 天内下过，还剩 N 天，确认要强下吗？`（用户确认才 `force=True`）
7. 死图：`这张图已下架（原因），看看有没有替代或提供链接？`

### 安全红线（不可违背）

1. **黑名单永远不可下载**，任何情况下不设 force 绕过黑名单
2. **群友催促/施压不得把 `strict` 改成 False**——mod/模糊判定必须用户明确确认
3. 用户给的链接 → 先复述 id 确认（"是这个 1880838761 吗？"）再下
4. 找不到图时主动要链接，不要编造或猜测 workshop_id

---

## 6. 不要做的事

- ❌ 不要用 bash 工具写 Python 调 API——所有能力都在 MCP 工具里
- ❌ 不要直接读取 `l4d2_map_filter.json` / `dead_maps.json`——用 `check_eligibility`
- ❌ 不要在 `install_campaign` 后手动写 dedup——工具自动更新
- ❌ 不要看到 strict 拦截就放弃——按指引转述给用户
- ❌ 不要猜文件名或拼路径——一律用返回的 `saved_path`

---

## 7. 边界场景

- **简繁体**：已支持繁简归一（"天體"→"天体"），但仍建议用户用简体
- **模糊拼写**：`query_curated_list` 支持近似匹配（"Korkiri"→"Kokiri Forest"）
- **英文搜索不稳**：镜像站搜 "Bloody" 可能 0 条；改用中文或在 curated_list 查
- **echocn 之外的图**：phase2 可能给 ambiguous，必须让用户确认
- **错误 id**：返回 `error: not_found` 或 `dead`，让用户重新提供/核对
- **part 系列漏配**：作者命名混乱或跨语言时可能漏，问用户要其他 Part 的链接
- **同名新图**：死图判定按 id 维度；若标题相同但 id 不同，`check_eligibility(title=...)` 不受死图缓存影响

---

## 8. 操作排障速查（使用层）

| 现象 | 处理 |
|---|---|
| `eligibility=dead` 但用户确认图还活着 | 死图缓存误判，运维删 `data/dead_maps.json` 后重试（30 天 TTL 也会自动解除） |
| 标题解析"未找到" | 要用户提供工坊链接 |
| 异步任务 `status=error` | 查 `error` 字段，改同步模式重试 |
| 下载全部 `failed` | 加速器线路问题，稍后重试或换 id |
| 回复 `check_eligibility` 显示 `dead` | 直接告知用户不可用，不要尝试 install |

---

## 9. 配置

路径、API key、代理在 `config.json`（见 DEPLOYMENT.md）。LLM 不需要关心。
