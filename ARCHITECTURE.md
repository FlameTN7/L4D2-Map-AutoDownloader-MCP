# L4D2 Workshop Mirror MCP - Architecture


## 1. 数据源架构

### 1.1 四个数据源对比矩阵

| 维度 | echocn:8090 | zhrradiant 镜像站 | Steam 官方 API | SWD 第三方 |
|---|---|---|---|---|
| **数据完整性** | 461 条人工策展 | 全工坊镜像 | 官方源头 | 全工坊 |
| **战役判定可靠性** | 极高（人工验证） | 低（标签污染） | 中（无 children） | 中 |
| **章节信息** | 含 map name 列表 | 无 | 无 | 无 |
| **中文人工标签** | 16 种 | 无 Steam 原生 | 无 | 无 |
| **依赖关系** | 无 | required_items + children | 无 children | 最全 |
| **file_url** | 无 | 有 | 无 | 有 |
| **协议** | HTTP 明文 | HTTPS | HTTPS | HTTPS (CF) |
| **维护方式** | 人工 | 自动 | 自动 | 自动 |
| **本 Skill 历史** | 完全没用 | 主用 | 备选 | 主用 |

### 1.2 各 API 端点的真实字段（实测 2026-07）

#### zhrradiant 镜像站（`https://zhrradiant.com/wp-json/l4d2-workshop/v1`）

| 端点 | 方法 | 关键参数 | 返回字段 |
|---|---|---|---|
| `/query` | GET | page, num, sort, search?, tags?, excluded_tags?, days? | items[]: id, title, preview, favorited, subscriptions, score, tags[], creator, creator_name |
| `/detail` | GET | id | 上述 + description, file_url, file_size_human, time_created, time_updated, subscriptions, favorited, creator, author_name, is_collection, children[], children_ids, children_total_count, required_items[], is_fallback |
| `/user-files` | GET | steamid, num, page, type | items[]: id, title, preview, creator, score (字段精简) |
| `/items` | POST | body: {ids: [...]} | 批量基本字段 |

**关键约束**：
- `/query` 不返回 file_size 和 file_url，必须二次调 `/detail`
- `/user-files` 是 Part 反向查询的黄金工具（一次拿到作者全部物品）
- `is_fallback=true` 表示数据回退（Steam 已删除/私密），应跳过

#### echocn 面板（`http://blog.echocn.cn:8090/`）

- **无 API**，纯 Flask/Jinja2 服务端渲染 HTML
- **不分页**，461 条一次性返回（HTML 全文约 1.2MB）
- 表格列：序号 / 中文名 / 原名 / 版本 / 链接 / 上传时间 / 上传者 / 文件大小(注释) / 标签 / 章节
- **链接来源分布**：Steam 工坊 174 / gamemaps 96 / 其他 147 / 无链接 44
- 16 种中文标签：高质量、高难度、常规、经典、机关、国产、长流程、移植、风景、推荐、黑暗、坑图、官改、优化差、隐藏彩蛋、解密
- 章节命名 10 种模式（见 4.1.3）

#### Steam Web API

- `POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/`
- 不返回 children / required_items
- 用于元数据交叉验证（不作为主路径）

#### SWD 第三方（`https://steamworkshopdownloader.io`）

- `POST /api/details/file?format=json` body: `[id1, id2, ...]`
- **依赖解析最全**：返回 children[]、num_children、file_type
- 需要 cloudscraper 绕 Cloudflare，偶发 500

### 2.3 数据流总图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        MCP Server (本实现)                          │
│                                                                      │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐ │
│  │ curated_store│   │ filter_store │   │ series_detector          │ │
│  │ (echocn缓存) │   │ (filter.json)│   │ dependency_resolver      │ │
│  └──────┬───────┘   └──────┬───────┘   └────────────┬─────────────┘ │
│         │                  │                        │               │
│         │     ┌────────────┴────────────────────────┘               │
│         │     │                                                     │
│         │     ▼                                                     │
│         │  ┌──────────────┐    ┌──────────────┐                     │
│         └──┤campaign_judge│◄───┤ api_client   │                     │
│            └──────┬───────┘    └──────┬───────┘                     │
│                   │                   │                              │
│                   │           ┌───────┴────────┐                    │
│                   │           │                │                    │
│                   │           ▼                ▼                    │
│                   │      ┌─────────┐    ┌────────────┐              │
│                   │      │镜像站API│    │ SWD / Steam│              │
│                   │      └─────────┘    └────────────┘              │
│                   ▼                                                   │
│            ┌──────────────┐                                          │
│            │  downloader  │─── 三级容错 ─── workshop_dir/            │
│            └──────────────┘                                          │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
              ▲
              │ stdio (MCP protocol)
              │
┌─────────────┴──────────────┐
│  AstrBot 主 LLM            │
│  - 调用 8 个 MCP 工具      │
│  - 决策"是否下载/上传"     │
└────────────────────────────┘
```

---

## 2. 工具集设计

### 2.1 工具总览

10 个工具，按使用频率排序。每个工具的输出都走**受限 schema**：字段名短、数值预格式化、长文本默认不返回。

| # | 工具 | 一句话职责 | 复杂度 |
|---|---|---|---|
| 1 | `install_campaign` | 黄金路径，一站式下载（支持 id/链接/标题，可选异步） | 高 |
| 2 | `search_campaigns` | 搜索工坊 + phase2 预筛 | 中 |
| 3 | `resolve_map` | 标题解析为候选（不下载），群聊"有没有/id 是多少"入口 | 中 |
| 4 | `get_map_detail` | 单图详情 + 战役判定 | 低 |
| 5 | `check_eligibility` | 黑名单/去重查询 | 低 |
| 6 | `find_series_parts` | Part 系列识别 | 高 |
| 7 | `list_new_campaigns` | 增量发现 | 中 |
| 8 | `query_curated_list` | echocn 人工名单查询（模糊/繁简） | 低 |
| 9 | `refresh_curated_list` | 手动刷新 echocn 缓存 | 低 |
| 10 | `get_download_job` | 异步下载任务轮询 | 低 |

另有内部工具 `sync_idle_folder`（废弃决定已撤销，保留：install_campaign 完成后自动同步闲置目录）。

### 2.2 `install_campaign` 黄金路径详解

绝大多数用户请求"下这张图"用一次调用完成。内部串联：

```
install_campaign(workshop_id, force=False, auto_parts=True)
    │
    ├─① check_eligibility(workshop_id, force)
    │    ├─ blacklisted → 直接返回拒绝
    │    ├─ dedup_active 且 force=False → 返回拒绝
    │    └─ allowed → 继续
    │
    ├─② get_detail(workshop_id)
    │    → 拿到 title, file_url, file_size, required_items, author
    │
    ├─③ if auto_parts:
    │    find_series_parts(workshop_id, aggressive=True)
    │    → 拿到 parts[] (可能含其他 Part 的 workshop_id)
    │
    ├─④ resolve_dependencies(workshop_id ∪ parts[])
    │    → 拿到 dependencies[]（递归 required_items / children，从入口与全部 Part 并集出发，
    │      保证"只有 part1 标了依赖"时从 part2/n 出发也能全捕获）
    │
    ├─⑤ 合并下载清单：
    │    下载清单 = [main] + parts[] + dependencies[]
    │    (去重 workshop_id，跳过已下载)
    │
    ├─⑥ 三级容错下载每个文件
    │    → 落盘 workshop_dir/{sanitize(title)}.vpk
    │
    ├─⑦ 写入 dedup_75days (source="downloaded")
    │
    └─⑧ 输出（精简 schema）：
       {
         eligibility: "allowed" | "blacklisted" | "dedup_active",
         main: {id, title, size_mb, saved_path, status},
         parts: [{n, id, title, status, source}],
         dependencies: [{id, title, status}],
         downloaded_total: int,
         filter_updated: bool,
         missing: [{id, reason}]
       }
```

### 2.3 各工具输入/输出 Schema

#### Tool 1: `install_campaign`
```yaml
描述: |
  一次性完成"识别→解析依赖→下载→落盘→更新去重名单"全流程。
  适用：用户给出 workshop_id、steam 链接或具体地图名要下载时。
  标题输入走内部解析链（curated 模糊 → 镜像搜索 → phase2），
  唯一高可信匹配自动下载，多候选返回 candidates[] 待确认。
输入:
  workshop_id: str = ""  # 纯数字 id 或 steamcommunity 链接（自动提取）
  title: str = ""        # 中文/英文标题（workshop_id 为空时使用）
  force: bool = False    # 忽略 dedup 限制（黑名单永远不可绕过）
  auto_parts: bool = True
  strict: bool = True
  async_mode: bool = False  # True 时后台下载，返回 job_id，用 get_download_job 轮询
输出:
  eligibility: str       # allowed/blacklisted/dedup_active/needs_confirmation/rejected_as_mod/not_found/failed
  main: {id, title, title_zh, size_mb, saved_path, status}
  parts: [{n, id, title, status, source}]
  dependencies: [{id, title, status}]
  downloaded_total: int
  filter_updated: bool
  missing: [{id, reason}]
  candidates: [...]|null  # 标题解析多候选
  job_id: str|null        # async_mode 时
  preview_only: bool
错误:
  黑名单 → eligibility="blacklisted"，其他字段为空
  dedup_active 且 force=False → eligibility="dedup_active"
  标题无匹配 → eligibility="not_found"
```

#### Tool 3: `resolve_map`
```yaml
描述: |
  解析地图标题为工坊候选（不下载）。融合 echocn 策展名单 + 镜像站搜索，
  并行拉 detail 做 phase2 判定后按分数排序。
  适用："XXX 有没有"、"XXX 的 id 是多少"、install_campaign 标题输入的共享内部实现。
输入:
  query: str (必填)
  limit: int = 5
输出:
  items: [{
    workshop_id, title, title_zh, size_mb, subscriptions,
    is_campaign_score, in_curated_list, reasons
  }]
  source: "curated+mirror"|"none"
```

#### Tool 10: `get_download_job`
```yaml
描述: |
  查询异步下载任务进度（install_campaign async_mode=True 时返回的 job_id）。
  群聊场景：LLM 先回复"开始下载"，再轮询本工具汇报结果。
输入:
  job_id: str (必填)
输出:
  job_id, status: "queued"|"running"|"done"|"error"|"not_found"
  elapsed_s: float
  result: install 完整结果|null
  error: str|null
```

#### Tool 2: `search_campaigns`
```yaml
描述: |
  在工坊中搜索战役地图，自动用 phase2 多因子规则预筛，
  自动过滤黑名单和 active 去重。不返回 description。
输入:
  keyword: str = ""
  days: int = -1  # 1/7/30/90/180/365/-1
  sort: str = "trend"  # trend/new/relevance
  limit: int = 10  # 上限 30
输出:
  items: [{
    workshop_id, title, size_mb, subscriptions,
    is_campaign_score, eligibility,
    in_curated_list: bool
  }]
```

#### Tool 3: `get_map_detail`
```yaml
描述: |
  查询单张地图的完整详情，包含 phase2 战役判定结果。
  默认不返回 description（省 token），需要时显式开启。
输入:
  workshop_id: str
  include_description: bool = False  # True 时截断到 500 字符
输出:
  id, title, size_mb, subscriptions, favorited
  is_campaign: bool
  score: int
  reasons: [str]
  ambiguous: bool  # score 在 1-2 之间
  definitely_mod: bool  # score < -1
  series_info: {base_name, total_parts, current} | null
  dependency_count: int
  eligibility: str
  in_curated_list: bool
  description: str (仅 include_description=True)
```

#### Tool 4: `check_eligibility`
```yaml
描述: |
  检查一张图是否能下载。零网络调用，纯本地 filter.json 查询。
  适用：下载前预检、批量过滤。
输入:
  workshop_id: str = ""
  title: str = ""
  (至少传一个)
输出:
  state: "allowed"|"blacklisted"|"dedup_active"|"dedup_expired"|"not_found"
  days_remaining: int (仅 dedup_active)
  source: str  # 来自哪条 dedup 记录
```

#### Tool 5: `find_series_parts`
```yaml
描述: |
  识别一张图所属的完整系列（所有 Part）。
  融合三路信号：①正向依赖 ②反向查询作者全部物品 ③命名模式识别。
  适用：用户给的图可能是系列的一部分；install_campaign 内部调用。
输入:
  workshop_id: str
  aggressive: bool = True  # 命名像系列但 API 查不到关系时是否按 base_name 自动归类
输出:
  is_series: bool
  base_name: str
  total_parts: int
  parts: [{
    n: int,  # 在系列中的序号（从命名识别，无则按上传时间）
    workshop_id: str,
    title: str,
    source: "forward"|"reverse"|"description"|"inferred"
  }]
  confidence: "high"|"medium"|"low"
```

#### Tool 6: `list_new_campaigns`
```yaml
描述: |
  列出最近 N 天新增的战役地图（增量发现）。
  自动跳过黑名单和 active 去重，融合 echocn 策展名单。
输入:
  days: int = 7
  limit: int = 20
输出:
  items: [{
    workshop_id, title, size_mb, uploaded_at,
    in_curated_list, is_campaign_score
  }]
```

#### Tool 7: `query_curated_list`
```yaml
描述: |
  查询 echocn 人工策展的 461 条高质量战役地图名单。
  含 16 种中文标签、章节信息、人工标注。
  这是质量最高的数据源，应优先用于判定和推荐。
输入:
  keyword: str = ""
  tag: str = ""  # 如 "高质量"/"高难度"/"推荐"
  limit: int = 20
输出:
  items: [{
    title_zh, title_original,
    workshop_id: str | null,  # gamemaps 链接的为 null
    tags: [str],
    chapters: [str],  # 章节 map name 列表
    uploaded_at, file_size_mb
  }]
  cached_at: str  # 缓存时间（ISO）
```

#### Tool 8: `refresh_curated_list`
```yaml
描述: |
  手动刷新 echocn 缓存。MCP 启动时已自动拉取，每日后台也会自动刷新。
  此工具用于：①首次部署验证 ②怀疑缓存过期 ③排障。
输入: 无
输出:
  success: bool
  total: int  # 收录条数
  sha256: str  # 当前内容哈希（用于人工对比）
  cached_at: str
  error: str (失败时)
```

### 3.4 工具间调用关系

```
LLM ──┬─► install_campaign ──┬─► check_eligibility
      │                       ├─► get_map_detail
      │                       ├─► find_series_parts ──► /user-files
      │                       ├─► resolve_dependencies (内部)
      │                       └─► downloader
      │
      ├─► search_campaigns ──► curated_store (查 echocn 名单)
      ├─► get_map_detail
      ├─► check_eligibility (零网络)
      ├─► find_series_parts
      ├─► list_new_campaigns ──► search_campaigns (内部)
      ├─► query_curated_list (零网络，纯本地缓存)
      └─► refresh_curated_list ──► echocn HTTP
```

---

## 3. 关键决策点

### 3.1 Part 系列识别

#### 3.1.1 4 种数据形态

```
场景 A：合集型（Collection）
  作者建了一个 file_type=1 的 Collection 物品
       ┌──────────────────┐
       │ Collection (主)   │
       │ children_ids:    │
       │ [P1, P2, P3, P4] │
       └──────────────────┘
  解析：children_ids 一查就有 ✅

场景 B：主 Part 标了依赖（最常见的坑）
       ┌──────────────────┐
       │ Part 1 (主)      │
       │ required_items:  │
       │  [P2, P3, P4]    │
       └──────────────────┘
  从 Part 1 出发能解析全系列 ✅
  从 Part 2/3/4 出发：它们 required_items 为空 ❌（当前 bug）

场景 C：依赖只在 description 里
  正则扫 description 能解析（正向）
  反向查询失效 ❌

场景 D：什么都没标（最坑）
  作者直接发了 N 个独立 vpk，标题像 "Xxx Part 1/5"
  但不建 Collection、不标 required_items、描述也不写
  完全无法通过 API 自动发现 ❌
  只能靠命名模式 + 同作者 + 时间相近 启发式推断
```

#### 3.1.2 三路融合策略（`find_series_parts`）

```
输入 workshop_id
   │
   ├── 路径 1：正向依赖（解决 A/B/C）
   │   ├─ detail.required_items        ← 必须有（2026-08 修复：此前漏用，
   │   │                                   场景 B"Part1 标了 [P2,P3,P4]"会失效）
   │   ├─ detail.children_ids
   │   └─ 正则扫 detail.description 找 steamcommunity 链接
   │
   ├── 路径 2：反向查询（解决 B/C 的反向）
   │   ├─ 从 detail.title 提取 base_name（去掉 Part N/M 后缀）
   │   ├─ 调镜像站 /user-files?steamid=detail.creator
   │   ├─ 候选预筛：① 候选本身命中 part 模式 ② 版本 token 一致（V0.7 等）
   │   ├─ 二次调 /detail 取同语言域标题再严格比较
   │   │   ← 2026-08 修复：/user-files 返回英文标题（ContraZ），/detail 返回
   │   │     中文（魂斗罗Z），直接比会漏（实测复现）
   │   └─ 在作者物品中按 base_name + Part 模式匹配
   │
   ├── 路径 3：命名识别（解决 D 兜底）
   │   └─ 对当前物品跑 series_detector 正则
   │       命中：标记 is_series=true，current/total 已知
   │
   └── 合并去重 → parts[]
       每个 part 标 source 字段：
         - "forward"     来自 required_items/children
         - "reverse"     来自作者物品反向查询
         - "description" 来自描述里提取的链接
         - "inferred"    仅靠命名识别推断（最低置信度）
```

**依赖捕获边界（2026-08 实测确认）**：`install_campaign` 的依赖解析从
`{入口 id} ∪ {全部发现的 part id}` **并集**出发（`resolve_dependencies_union`），
保证最坏场景（只有 part1 标了 required_items，用户从 part2/n 出发）也能捕获全部依赖。

#### 3.1.3 章节命名模式（10 种，echocn 实测）

| 模式 | 示例 |
|---|---|
| `cNm1_xxx`（官方式） | `c1m1_outerbayous_a`、`c4m1_mountains` |
| `mN_xxx`（单 m） | `m1_beach`、`m1_nanfang` |
| `l4d2_xxx` / `l4d_xxx` | `l4d2_lastorange1`、`l4d_deathaboard01_prison` |
| `缩写_mN` | `cf_m1_town`、`dkr_m1_motel` |
| `数字前缀_xxx` | `2_earhcrash_l4d2`、`3_earthcrash_l4d2` |
| `字母数字_N` | `haha7_1`、`fk2_1` |
| `campaign_portal_xxx` | `campaign_portal_acfir_final` |
| `_time` 后缀 | `home_time`、`finish_time` |
| `xxxfinale` 结尾 | `egypt4finale`、`l4d_auburn_finale` |
| 中文 `第N章` | `第六章巴比伦的囚徒重制版` |

#### 4.1.4 Part 标题命名识别正则

实测真实样本（2026-08 复测）：`hehe23_part1`、`魂斗罗Z V0.7(part1)`、`九死一生V3.5 - part2`、
`广西-南宁 V1.15.1 PART-1/2`、`Dear Esther (Part 1/2)`、`连接 (part 1/2)`、`可畏语音包…part1`、
裸 `XXX 2/5`、全角 `XXX（2/5）`、`XXX 2 of 5`、中文 `死亡小镇 第一章`、英文 `AeroShore Chapter 1`。

```python
SERIES_PATTERNS = [
    r"(?<![a-z])part\s*[_\-]?\s*(\d+)\s*(?:/|of|\s)\s*(\d+)\b",   # Part 1/5, Part 2 of 5
    r"(?<![a-z])part\s*[_\-]?\s*(\d+)(?!\d)(?!\s*(?:/|of))",       # Part 1 / xxx_part1 / V1.3Part 1
    r"\[(\d+)\s*/\s*(\d+)\]",                                      # [1/2]
    r"\((\d+)\s*/\s*(\d+)\)",                                      # (1/2)
    r"第\s*([一二三四五六七八九十\d]+)\s*[部分]",                    # 第一部分
    r"第\s*([一二三四五六七八九十\d]+)\s*章",                        # 第一章
    r"(?<![a-z])chapter\s*(\d+)\b",                                # AeroShore Chapter 1
    r"(?<![a-z])pt\.?\s*(\d+)(?!\d)",                              # pt2
    r"(?<![\d./])(\d+)\s*/\s*(\d+)(?!\d)",                         # 裸 2/5（防版本号/日期）
    r"(?<![\d./])(\d+)\s+of\s+(\d+)\b",                             # 裸 2 of 5
]
```

**要点**：
- `part` 前缀用 `(?<![a-z])` 而非 `\b`：`hehe23_part1`、`V1.3Part 1` 这类数字/下划线紧贴
  命名在 `\b` 下没有词边界会漏判（2026-07 实测 bug，已修）
- 裸 `n/m` 模式必须带数值校验（total ∈ [2,10]、current ≤ total、base 非空），
  且排除前导数字/点（防 `2026/07/31` 日期和 `V1.15.1/2` 版本号）
- **不加** `第X期/第X弹/第X集` 等泛化中文模式——实测 `MMD替换背景…第13期`、
  `KFR喷漆第1期` 全是 mod，加了必误判

#### 3.1.5 场景 D 的默认行为

按用户决策：**自动下载全部发现的 Part**。

`aggressive=True` 时：
- 命名识别命中 → 即使 API 查不到关系，也按 base_name 在作者物品中搜
- 找到候选且命名匹配 → 加入下载清单
- 仍找不到 → `missing[]` 里报告 `reason="likely_series_but_not_found"`

### 3.2 phase2 战役判定规则（重写版）

#### 3.2.1 设计动机（实测发现）

实测两个物品都标了 Campaigns 标签：

| 字段 | 地球崩溃（真战役） | Friendly soldiers（mod） |
|---|---|---|
| tags | `Campaigns, Co-op` (2 个) | `Campaigns, Misc, Survivors, Sounds, Common Infected, SP, Models, Textures` (8 个) |
| file_size | 836 MB | 132 MB |
| subscriptions | 3068 | 7403（更高！） |
| description | 中英对照、含 gamemaps 链接 | "this mod adds NPC soldiers..." |
| 旧规则打分 | 标签+1, VPK+1 = **2 ✓** | 标签+1, VPK+1 = **2 ❌误判** |

旧规则把 mod 误判为战役。改进要点：
- 真战役 tags 通常 1-3 个；mod 通常 ≥ 4 个
- tags 同时含 Models + Textures ≈ 必是 mod
- subscriptions 高反而是 mod 特征（不能作正信号）
- echocn 名单是黄金正信号（人工策展）

#### 3.2.2 重写后的打分规则

```python
# === 正向信号 ===
SIGNALS_POSITIVE = [
    # (条件, 分值, 描述)
    ("in_curated_list",                +3, "echocn 人工策展名单命中（黄金信号）"),
    ("chapter_name_pattern",           +2, "描述/标题含章节命名模式 (cNm1_/mN_/l4d2_xxx)"),
    ("tags_count_le_3_and_campaigns",  +2, "tags 数量 ≤ 3 且含 Campaigns（真战役典型）"),
    ("tags_has_coop_no_mod_tags",      +1, "tags 含 Co-op 且不含 Mod 类标签"),
    ("file_size_gt_50mb",              +1, "文件 > 50MB"),
    ("title_campaign_keyword",         +1, "标题含 campaign/战役/章节/chapter/map pack"),
]

# === 负向信号 ===
SIGNALS_NEGATIVE = [
    ("tags_models_and_textures",       -3, "tags 同时含 Models + Textures（mod 强特征）"),
    ("tags_two_more_replacement",      -2, "tags 含 Sounds+Survivors+Common Infected 任 2"),
    ("desc_mod_keywords",              -2, "描述含 this mod/replace/替换/skin"),
    ("small_size_with_campaigns_tag",  -1, "file_size < 5MB 且 tags 含 Campaigns"),
    ("high_subs_low_fav_ratio",        -1, "subscriptions > 5000 且 favorited/subs < 0.2"),
]

# === 阈值 ===
# score >= 3   → is_campaign=True,  confidence="high"
# score 1-2    → is_campaign=True,  confidence="medium", ambiguous=True
# score <= 0   → is_campaign=False
# score < -1   → is_campaign=False, definitely_mod=True
```

#### 3.2.3 "Mod 类标签"清单

```python
MOD_TAGS = {
    "Models", "Textures", "Sounds",
    "Survivors", "Common Infected", "Special Infected",
    "Scripts", "Weapons", "Items", "UI",
}
```

注意：`Campaigns`、`Co-op`、`Versus`、`Survival`、`Realism` 不算 Mod 类。
真战役可能同时有 Survival/Versus 标签（一张图支持多模式），不算负信号。

### 3.3 echocn 缓存与降级策略

#### 3.3.1 接入方式（方案 B）

```
启动时：
  ┌─────────────────────────────────────────┐
  │ 1. 检查 data/curated.json 是否存在      │
  │    ├─ 存在且 < 24h → 直接加载           │
  │    └─ 不存在或过期 → 拉取 echocn        │
  │ 2. 解析 HTML，结构化为 curated.json     │
  │ 3. 加载到内存                           │
  └─────────────────────────────────────────┘

运行中：
  - 后台线程每 24h 自动刷新一次
  - LLM 可调 refresh_curated_list 强制刷新
  - 刷新失败不阻塞主流程，沿用旧缓存

降级（echocn 不可用）：
  - curated_store 返回空列表
  - campaign_judge 自动跳过 echocn 信号
  - 其他工具不受影响
```

#### 3.3.2 安全考虑

echocn 是 HTTP 明文 + 人工维护，存在两类风险：

| 风险 | 缓解措施 |
|---|---|
| 中间人篡改数据，注入恶意 workshop_id | 1. 下载的 vpk 落盘前做基础校验（文件头魔数）<br>2. install_campaign 输出 `in_curated_list` 字段让 LLM/用户感知信任级别<br>3. phase2 规则中 echocn 命中只是 +3，单源不足以放行所有可疑物品 |
| 站点关停或滞后 | 自动降级到无 echocn 模式，campaign_judge 跳过该信号 |

#### 3.3.3 gamemaps 链接处理

echocn 96 条图的链接是 `gamemaps.com/details/数字`，没有 Steam workshop_id。按用户决策：

```
query_curated_list 返回时 workshop_id=null
LLM 决定要下载时：
  1. 用 title_original 在镜像站 /query 反查
  2. 命中且 phase2 score >= 3 → 用反查到的 workshop_id 走 install_campaign
  3. 命中但 score 低 → 标记可疑，建议用户复核
  4. 未命中 → 报告"仅 gamemaps 源，无法通过工坊下载"
```

---

## 4. 安全模型

### 4.1 信任级别

每个被处理过的 workshop 物品都有一个 trust_level 字段（在 `get_map_detail` 和 `install_campaign` 输出中体现）：

| 级别 | 条件 | 含义 |
|---|---|---|
| `high` | `in_curated_list=true` | echocn 人工策展过，可放心下载 |
| `medium` | phase2 score ≥ 3 且 `definitely_mod=false` | 多因子判定通过，可信 |
| `low` | phase2 score 1-2（ambiguous） | 边缘 case，建议 LLM 提示用户复核 |
| `reject` | `definitely_mod=true` 或在黑名单 | 不应下载 |

### 4.2 vpk 下载校验

`downloader` 模块在落盘前做基础校验：

```python
def validate_vpk(file_path: Path) -> tuple[bool, str]:
    """基础校验，防恶意文件伪装为 vpk"""
    # 1. 文件大小 > 1MB
    if file_path.stat().st_size < 1_000_000:
        return False, "文件过小，疑似非真实 vpk"
    # 2. VPK 文件头魔数校验
    # VPK v1: 0x55aa1234
    # VPK v2: 0x55aa1235
    with open(file_path, 'rb') as f:
        magic = f.read(4)
    if magic not in (b'\x34\x12\xaa\x55', b'\x35\x12\xaa\x55'):
        return False, f"VPK 魔数不匹配: {magic.hex()}"
    return True, "ok"
```

校验失败 → 删除文件，在输出中标记 `status="failed_validation"`。

### 4.3 网络访问边界

MCP server 的所有网络出口：

| 目标 | 协议 | 用途 | 走代理？ |
|---|---|---|---|
| `zhrradiant.com` | HTTPS | 镜像站 API | 是 |
| `api.steampowered.com` | HTTPS | Steam Web API | 是 |
| `steamworkshopdownloader.io` | HTTPS (CF) | SWD 依赖解析 | 是 |
| `blog.echocn.cn:8090` | HTTP | curated 列表 | 是 |
| `cdn.steamusercontent.com` | HTTPS | vpk 直连下载 | 否（按用户决策） |
| `accelerator.zhrradiantcos.top` 等 | HTTPS | vpk 代理下载 | 否 |

下载默认走代理线路（CF/LA2），不走 HTTP_PROXY；元数据查询走 HTTP_PROXY。
配置项 `HTTP_PROXY` 控制，下载线路在 `downloader.py` 内部硬编码。

---

## 5. 项目结构

```
l4d2-workshop-mirror/
├── server.py                  # FastMCP 入口，注册 8 个工具
├── config.py                  # 配置加载（从 config.json + 环境变量）
├── config.example.json        # 配置模板
├── core/
│   ├── __init__.py
│   ├── api_client.py          # 镜像站 + SWD + Steam 三套客户端（三级回退 + 空壳识别）
│   ├── campaign_judge.py      # 重写版 phase2 多因子判定
│   ├── series_detector.py     # Part 命名识别（10 种正则 + 数值校验）
│   ├── dependency_resolver.py # 正向 + 反向依赖解析
│   ├── curated_store.py       # echocn 缓存读写 + 自动刷新 + 模糊/繁简
│   ├── dead_store.py          # 死图缓存（TTL 30 天）
│   ├── downloader.py          # 三级容错下载 + vpk 校验
│   └── filter_store.py        # filter.json 操作 + eligibility
├── data/                      # 运行时数据（git ignore）
│   ├── curated.json           # echocn 缓存
│   ├── curated.meta.json      # 缓存元信息（last_fetch, sha256）
│   └── dead_maps.json         # 死图缓存
├── README.md                 # 项目简介 + 文档导航（人读入口）
├── ARCHITECTURE.md            # 本文档（架构设计，纯设计）
├── DEPLOYMENT.md              # 部署手册（含配置文件格式约定）
├── skills/l4d2-workshop-mirror/SKILL.md  # LLM 使用说明书（可选接入 AstrBot skill）
├── examples/                  # JSON 格式模板（filter/steam_api/dead_maps）
├── pyproject.toml             # 依赖声明
├── .gitignore
└── tests/                     # 单测（series_detector / filter_store / resolver / curated / lock / fallback）
```

### 5.1 各 core 模块职责

| 模块 | 职责 | 不做的事 |
|---|---|---|
| `api_client.py` | 封装镜像站/SWD/Steam 的 HTTP 调用 | 不做判定、不做缓存 |
| `campaign_judge.py` | 纯函数：输入 detail → 输出 score+reasons | 不联网 |
| `series_detector.py` | 纯函数：标题/描述 → 系列信息 | 不联网 |
| `dependency_resolver.py` | 调用 api_client 做正向+反向解析 | 不下载 |
| `curated_store.py` | echocn HTML 解析 + 缓存 + 自动刷新 | 不参与判定 |
| `dead_store.py` | 死图缓存读写（TTL 30 天） | 不判定死图（判定在 server 层 _confirm_dead） |
| `downloader.py` | 三级容错下载 + 文件校验 | 不做依赖解析 |
| `filter_store.py` | filter.json CRUD + eligibility | 不联网 |

---

## 6. 配置

### 6.1 `config.example.json`

```json
{
  "workshop_dir": "/home/flametn7/server/game/l4d2/left4dead2/addons/workshop",
  "idle_dir": "/home/flametn7/server/game/l4d2/left4dead2/addons/workshop/闲置地图放这里",
  "filter_json_path": "/home/flametn7/server/game/l4d2/l4d2_map_filter.json",
  "steam_api_key_path": "/home/flametn7/server/game/l4d2/l4d2_steam_api.json",
  "http_proxy": "http://127.0.0.1:10808",
  "mirror_base": "https://zhrradiant.com/wp-json/l4d2-workshop/v1",
  "swd_base": "https://steamworkshopdownloader.io",
  "steam_api_base": "https://api.steampowered.com",
  "echocn_url": "http://blog.echocn.cn:8090/",
  "download_proxies": [
    "https://accelerator.zhrradiantcos.top",
    "https://la2-accelerator.zhrradiantcos.top"
  ],
  "curated_refresh_interval_hours": 24,
  "request_timeout_seconds": 30,
  "download_timeout_seconds": 600,
  "download_max_retries": 3
}
```

### 6.2 环境变量覆盖

配置分层设计：config.json 为基底，环境变量可覆盖（优先级：环境变量 > config.json），
用途是容器化/多环境部署不重复维护配置文件：

| 变量 | 覆盖项 |
|---|---|
| `L4D2_CONFIG` | 自定义 config.json 路径 |
| `HTTP_PROXY` / `HTTPS_PROXY` | http_proxy |
| `L4D2_WORKSHOP_DIR` | workshop_dir |
| `L4D2_FILTER_PATH` | filter_json_path |
| `L4D2_DATA_DIR` | data 目录（缓存/死图/锁文件位置） |

---

## 7. 部署/联调

部署、打包、AstrBot 接入、升级与运维见 `DEPLOYMENT.md`；本文件仅描述架构设计。
