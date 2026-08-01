# Deployment Guide

> 部署 L4D2 Workshop Mirror MCP 到生产环境的完整指南（部署/运维者阅读）。

## 1. 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.10 | 在 3.14 上测试通过 |
| pip | ≥ 21 | |
| 网络 | HTTP 代理 | 工坊/SWD/echocn 最好需要代理；下载走专用加速器，不需要代理 |

依赖包：
- `fastmcp >= 3.4.0`
- `requests >= 2.31`
- `cloudscraper >= 1.2.71`（绕 SWD 的 Cloudflare）
- `beautifulsoup4 >= 4.12` + `lxml >= 5.0`（解析 echocn HTML）
- `zhconv >= 0.4.0`（繁简归一，群聊中文标题解析用）

---

## 2. 打包（开发机）

### 2.1 打成 tar.gz

```bash
cd /workspace
tar --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='data/' \
    -czf l4d2-workshop-mirror.tar.gz \
    l4d2-workshop-mirror/

ls -lh l4d2-workshop-mirror.tar.gz
# 大约 60-80KB
```

包含的文件：
```
l4d2-workshop-mirror/
├── README.md               ← 项目简介与文档导航
├── server.py
├── config.py
├── config.example.json     ← 主配置模板（必填字段见 §3.3）
├── pyproject.toml
├── ARCHITECTURE.md
├── DEPLOYMENT.md           ← 本文档
├── .gitignore
├── core/
│   ├── __init__.py
│   ├── api_client.py
│   ├── campaign_judge.py
│   ├── series_detector.py
│   ├── dependency_resolver.py
│   ├── curated_store.py
│   ├── dead_store.py
│   ├── downloader.py
│   └── filter_store.py
├── skills/l4d2-workshop-mirror/SKILL.md   ← LLM 说明书（可选接入）
├── examples/                              ← JSON 格式模板
│   ├── filter.example.json
│   ├── steam_api.example.json
│   └── dead_maps.example.json
└── tests/                    ← 单测（可选，不部署）
```

### 2.2 不打包的文件

- `data/`（运行时数据，含 curated.json 缓存，生产环境首次启动会自动生成）
- `config.json`（含敏感配置，应单独配置）
- `l4d2_map_filter.json`（生产环境已有，不要覆盖）

### 2.3 可选：用 wheels 加速

如果生产环境没外网或网络差，开发机可预下载 wheels：

```bash
pip download -d ./wheels \
    fastmcp requests cloudscraper beautifulsoup4 lxml
tar -czf l4d2-wheels.tar.gz wheels/
# 传输到生产机后：pip install --no-index --find-links=./wheels -e .
```

---

## 3. 部署（生产机）

### 3.1 解压

```bash
cd ~/server   # 或任意工作目录
tar -xzf l4d2-workshop-mirror.tar.gz
cd l4d2-workshop-mirror
```

### 3.2 创建虚拟环境（推荐）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

或不用虚拟环境直接装：
```bash
pip3 install -e .
```

### 3.3 配置 config.json

```bash
cp config.example.json config.json
```

编辑 `config.json`，根据生产机实际路径调整：

```jsonc
{
  "workshop_dir":     "/home/flametn7/server/game/l4d2/left4dead2/addons/workshop",
  "idle_dir":         "/home/flametn7/server/game/l4d2/left4dead2/addons/workshop/闲置地图放这里",
  "filter_json_path": "/home/flametn7/server/game/l4d2/l4d2_map_filter.json",
  "steam_api_key_path": "/home/flametn7/server/game/l4d2/l4d2_steam_api.json",
  "http_proxy":       "http://127.0.0.1:10808",  // 改成生产机的代理地址
  // ... 其他字段保留默认
}
```

**关键检查项**：
- [ ] `workshop_dir` 目录存在且 Python 进程有写权限
- [ ] `filter_json_path` 指向**已有的** dedup 名单（避免覆盖现状）
- [ ] `http_proxy` 是生产机的真实代理地址（不是开发机的 10808）
- [ ] `download_proxies` 数组里至少一个加速器地址可达

#### config.json 全部字段（含默认值）

| 字段 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `workshop_dir` | ✅ | — | 游戏服务器 addons/workshop 目录（vpk 落盘位置） |
| `filter_json_path` | ✅ | — | 去重/黑名单文件路径（`l4d2_map_filter.json`） |
| `http_proxy` | ✅ | — | 元数据查询代理（镜像站/SWD/echocn/Steam） |
| `idle_dir` | | "" | 闲置地图目录（install 后自动同步去重；留空跳过） |
| `steam_api_key_path` | | "" | Steam API key 文件路径（见 §3.7；留空降级为镜像站信号） |
| `mirror_base` | | zhrradiant 镜像站 | 镜像站 API 地址 |
| `swd_base` | | steamworkshopdownloader.io | SWD 依赖解析服务 |
| `steam_api_base` | | api.steampowered.com | Steam Web API |
| `echocn_url` | | blog.echocn.cn:8090 | 策展名单来源 |
| `download_proxies` | | accelerator.zhrradiantcos.top 等 | vpk 下载加速线路（按序尝试） |
| `curated_refresh_interval_hours` | | 24 | 策展缓存刷新间隔 |
| `request_timeout_seconds` | | 30 | 元数据请求超时 |
| `download_timeout_seconds` | | 600 | 单次下载超时 |
| `download_max_retries` | | 3 | 直连重试次数 |
| `dedup_days` | | 75 | 去重窗口天数 |
| `data_dir` | | 项目目录/data | 运行时数据目录（curated/dead 缓存、锁文件） |

环境变量 `L4D2_CONFIG` / `L4D2_WORKSHOP_DIR` / `L4D2_FILTER_PATH` / `L4D2_DATA_DIR` 可覆盖对应字段。

### 3.4 验证启动

```bash
# 验证模块加载
python3 -c "import server; print('OK')"

# 启动 stdio MCP server（前台测试）
python3 server.py
# 应输出：INFO l4d2-mcp: L4D2 Workshop Mirror MCP starting (stdio)
# 然后 curated_store 会自动拉取 echocn（首次约 5-10 秒）
```

如果看到 `curated cache loaded: 461 entries` 或 `Curated list refreshed: 461 entries`，说明 echocn 拉取正常。

### 3.5 接入 AstrBot

在 AstrBot 的 MCP 配置里加：

```json
{
  "mcpServers": {
    "l4d2-workshop": {
      "command": "python3",
      "args": ["/home/flametn7/server/l4d2-workshop-mirror/server.py"],
      "env": {
        "L4D2_CONFIG": "/home/flametn7/server/l4d2-workshop-mirror/config.json"
      }
    }
  }
}
```

或用 `pyproject.toml` 注册的入口：
```json
{
  "mcpServers": {
    "l4d2-workshop": {
      "command": "l4d2-mcp",
      "env": {
        "L4D2_CONFIG": "/home/flametn7/server/l4d2-workshop-mirror/config.json"
      }
    }
  }
}
```

接入后 AstrBot 会自动发现 11 个工具。如需 skill 形态的 LLM 使用说明书，
把 `skills/l4d2-workshop-mirror/` 整个目录拷贝到 `astrbot/data/skills/` 即可。

**群聊使用要点**：
- 用户给标题（如"下死亡小镇"）直接传 `install_campaign(title=...)`，内部自动解析，无需 LLM 手动两步查询
- 大图下载建议用 `install_campaign(..., async_mode=True)` + `get_download_job` 轮询，
  避免 LLM 工具调用阻塞数十秒（AstrBot 侧可能超时）
- 默认 `strict=True` 拦截 mod/模糊判定；群聊安全红线见 skills/SKILL.md"安全红线"

### 3.6 运行时 JSON 文件格式约定

除 `config.json` 外，系统还读写三个 JSON 文件，模板见 `examples/`：

**`filter_json_path` 指向的文件（去重/黑名单）** —— 模板 `examples/filter.example.json`

| 字段 | 说明 |
|---|---|
| `updated_at` | 最近写入时间（自动维护） |
| `blacklist[]` | 黑名单标题数组（按标题匹配，永远不可下载） |
| `dedup_75days[]` | 去重条目，每项：`map`（标题）、`workshop_id`（可空）、`added`（添加日期）、`expires`（过期日期）、`status`（`active`/`expired`）、`source`（`downloaded`/`downloaded_part`/`downloaded_dep`/`idle_folder`） |

下载完成后系统自动追加/更新条目，**不要手动编辑**。文件不存在时自动创建。

**`steam_api_key_path` 指向的文件（Steam key）** —— 模板 `examples/steam_api.example.json`

```json
{ "steam_api_key": "你的key" }
```

字段名兼容 `key` / `api_key` / `steam_api_key` 三种。未配置时：死图确认降级为镜像站信号、
回退链缺 Steam 位（三级变两级），其余功能不受影响。

**`data_dir/dead_maps.json`（死图缓存）** —— 模板 `examples/dead_maps.example.json`

自动生成，键为 workshop_id，值为 `{detected_at, reason}`，TTL 30 天自动失效。
误判时删除该文件即可重置全部死图判定。

---

## 4. 运维

### 4.1 数据目录

`data/` 目录下文件：
- `curated.json`：echocn 缓存（约 100KB）
- `curated.meta.json`：缓存元信息（last_fetch, sha256）
- `dead_maps.json`：死图缓存（镜像站/Steam 确认不可用的物品，TTL 30 天自动失效；
  删除文件可立即重置全部死图判定）

删除 curated 相关文件会强制下次启动重新拉取 echocn。

### 4.2 echocn 不可用的降级

如果 `blog.echocn.cn:8090` 挂了：
- 启动时拉取会失败，日志输出 `curated start failed (degraded mode)`
- MCP 仍可用，但 `in_curated_list` 字段全部为 `false`，phase2 score 会偏低 3 分
- 拉取成功后下一次刷新会自动恢复

### 4.3 强制刷新缓存

通过 MCP 工具：
```
refresh_curated_list()
```

或删除文件：
```bash
rm data/curated.json data/curated.meta.json
# 重启 server
```

### 4.4 日志

日志输出到 stderr（FastMCP 标准做法）。AstrBot 会捕获并存储。如需独立日志：

```bash
# 用 systemd 时
[Service]
ExecStart=/path/to/python3 /path/to/server.py
StandardOutput=append:/var/log/l4d2-mcp.log
StandardError=append:/var/log/l4d2-mcp.log
```

### 4.5 排障速查

| 症状 | 原因 | 处理 |
|---|---|---|
| 启动卡 5 秒以上 | echocn 拉取慢或代理不通 | 检查 `http_proxy`；首次启动正常 |
| `curated start failed` | echocn 不可达或代理失败 | 降级模式继续运行；不阻塞使用 |
| 下载全部 `failed` | 加速器代理不通或被限流 | 改 `download_proxies` 顺序；或临时直连 |
| `find_series_parts` 漏 Part | 命名不规范或作者不可查 | 用户手工指定其他 Part 的 workshop_id |
| 标题解析"未找到" | 地图不在 echocn/镜像站 | 要用户提供工坊链接 |
| `get_download_job` 报 `error` | 后台任务异常（网络/磁盘） | 查看 error 字段，用同步模式重试 |
| 物品报 `eligibility=dead` | Steam 已删/私密/无效 id（死图缓存 30 天） | 属预期拦截；误判可删 `data/dead_maps.json` |
| `single instance lock` 接管日志 | 新实例自动终止旧实例（最后启动者胜） | 正常行为；若旧实例杀不掉会报"拒绝启动"，按 §6.1 手动清理 |
| phase2 误判 | echocn 名单未更新 / 标签异常 | 调 `get_map_detail(include_description=True)` 看 reasons 后人工决策 |

---

## 5. 数据迁移注意

### 5.1 现有 `l4d2_map_filter.json`

**不要覆盖**生产环境已有的 dedup 名单。MCP 启动后会自动加载并增量更新：
- 下载完成后追加新条目（source=`downloaded` / `downloaded_part` / `downloaded_dep`）
- 已存在条目遇到再次下载会更新 `added` 和 `expires` 时间戳
- 名单格式 100% 兼容旧版

### 5.2 闲置地图目录

`idle_dir` 字段填了之后：`install_campaign` 下载完成后会自动扫描一次闲置目录，
把未在 dedup 名单的 vpk 加入去重（下次遇到同名图会被拦截）；
也可手动调 `sync_idle_folder` 工具触发。闲置目录不存在时自动跳过，不影响主流程。

### 5.3 旧架构文件

旧 Skill 时代的残留文件（`子代理提示词.txt`、`templates/`、`references/`）已全部清理，
当前包内只包含 MCP 架构相关的文件。

---

## 6. 升级流程

```bash
# 1. 备份当前 config.json 和 data/
cp config.json config.json.bak
cp -r data data.bak

# 2. 解压新版本（覆盖代码）
tar -xzf l4d2-workshop-mirror-vX.Y.tar.gz

# 3. 恢复配置和数据
mv config.json.bak config.json
mv data.bak data

# 4. 重装依赖（如有变更）
pip install -e .

# 5. 重启
# AstrBot 会自动重启 MCP server
```