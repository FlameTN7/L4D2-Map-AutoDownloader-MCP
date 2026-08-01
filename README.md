# L4D2 Workshop Mirror MCP

## 由LLM驱动的一站式群服求生之路2地图下载器

L4D2 工坊战役地图的 **MCP 服务器**：包含搜索、战役判定、依赖解析、一键下载、死图拦截。
所有脏活（网络请求、判定、去重、文件校验）在 MCP 内部完成，接上支持 MCP 的机器人（如 AstrBot）后，
用户一句"下xx地图"即可完成从找图到落盘游戏服务器全流程，并可引导LLM将地图文件传至群聊，达成全程懒人的目的。

由于借助了`镜像站`，该工具还可以`拯救国内服务器的无代理网络环境`问题！

其他的说明都是AI写的，嘻嘻。
## 特性

- **一站式下载**：支持 workshop id / steam 链接 / 中文标题三种输入，自动解析依赖、自动补齐系列 Part
- **战役判定**：phase2 多因子打分，过滤 mod/皮肤等噪音，策展名单交叉验证
- **Part 系列识别**：兼容 `part1` / `PART-1/2` / `XXX 2/5` / `[1/5]` / `第一章` / `Chapter N` 等 10 种命名
- **死图拦截**：Steam 已删除/私密物品自动识别并缓存（30 天 TTL），不再反复推荐和下载
- **异步下载**：大图后台下载 + 进度轮询，群聊不阻塞
- **去重与黑名单**：75 天去重窗口、黑名单永不可绕过
- **数据源融合**：echocn（尼姑服的人工维护三方图列表，注意是明文传输）+ zhrradiant 创意工坊镜像站 + SteamWorkshopDownloader.io（依赖解析） + Steam 官方 API 四级数据源

## 快速开始（3 步）

```bash
# 1. 安装
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. 配置（模板见 config.example.json，字段说明见 DEPLOYMENT.md §3.3）
cp config.example.json config.json
# 编辑 config.json：workshop_dir / filter_json_path / http_proxy 必填

# 3. 接入 AstrBot（MCP 配置模板见 DEPLOYMENT.md §3.5）
```

启动验证：`python3 server.py`（stdio 模式，日志出现 `L4D2 Workshop Mirror MCP starting` 即正常）。

## 文档导航

| 文档 | 读者 | 内容 |
|---|---|---|
| **README.md**（本文档） | 所有人 | 项目简介、快速开始、文档导航 |
| **DEPLOYMENT.md** | 部署/运维者 | 安装、配置格式约定、AstrBot 接入、升级、排障 |
| **ARCHITECTURE.md** | 开发者 | 架构设计、数据源、判定规则、决策记录 |
| **skills/l4d2-workshop-mirror/SKILL.md** | LLM（可选但不建议，如果你依赖LLM自动部署可考虑） | LLM 使用说明书：工具指令、决策规则、回复纪律。需要 AstrBot skill 形态时可整体拷贝到 `astrbot/data/skills/` |
| **examples/** | 部署者 | 各 JSON 配置/数据文件的格式模板 |

## 工具总览（11 个）

| 工具 | 用途 |
|---|---|
| `install_campaign` | 一站式下载（id / 链接 / 标题，可选异步） |
| `resolve_map` | 标题解析为候选（不下载） |
| `get_download_job` | 异步下载进度轮询 |
| `search_campaigns` | 搜索战役（自动过滤 mod/黑名单/去重/死图） |
| `get_map_detail` | 单图详情 + 战役判定 |
| `check_eligibility` | 下载前预检（黑名单/去重/死图，零网络） |
| `find_series_parts` | 系列 Part 识别 |
| `list_new_campaigns` | 最近 N 天新战役 |
| `query_curated_list` | 查 三方列表名单（模糊/繁简） |
| `refresh_curated_list` | 手动同步最新三方列表缓存 |
| `sync_idle_folder` | 闲置目录同步到去重名单 |

## 环境要求

- Python ≥ 3.10（3.14 测试通过）
- 网络：元数据查询走 HTTP 代理；vpk 下载走内置加速线路
- 可选：Steam Web API key（死图确认、三级回退兜底，无 key 也能运行）

