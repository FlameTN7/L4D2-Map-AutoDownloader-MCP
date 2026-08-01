"""L4D2 Workshop Mirror MCP Server 入口。

8 个工具的注册和编排。设计见 ARCHITECTURE.md §3。
"""
from __future__ import annotations

import os
for _proxy_var in ("ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_var, None)

import json
import logging
import os
import re
import signal
import sys
import threading
from datetime import datetime
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from config import Config, get_config, set_config
from core.api_client import ApiClient
from core.curated_store import CuratedStore
from core.dependency_resolver import (
    find_series_parts as _resolve_series_parts,
    resolve_dependencies_union,
)
from core.downloader import Downloader, sanitize_filename
from core.filter_store import FilterStore
from core.campaign_judge import judge_campaign
from core.dead_store import DeadStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("l4d2-mcp")


_cfg: Optional[Config] = None
_api: Optional[ApiClient] = None
_filter: Optional[FilterStore] = None
_curated: Optional[CuratedStore] = None
_downloader: Optional[Downloader] = None
_dead: Optional[DeadStore] = None

# 异步下载任务（async_mode）：job_id -> {status, result, error, started_at, finished_at}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_job_executor: Optional[ThreadPoolExecutor] = None

# steam 链接/纯数字 id 提取
_STEAM_ID_RE = re.compile(r"id=(\d{6,})")
_RAW_ID_RE = re.compile(r"^\d{6,}$")


def _ensure_init() -> tuple[Config, ApiClient, FilterStore, CuratedStore, Downloader, DeadStore]:
    global _cfg, _api, _filter, _curated, _downloader, _dead
    if _cfg is None:
        cfg = Config.load()
        set_config(cfg)
        _cfg = cfg
        _api = ApiClient(cfg)
        _filter = FilterStore(cfg.filter_json_path, cfg.dedup_days)
        _curated = CuratedStore(cfg)
        _downloader = Downloader(cfg)
        _dead = DeadStore(cfg)
        try:
            _curated.start()
        except Exception as e:
            logger.warning(f"curated start failed (degraded mode): {e}")
    return _cfg, _api, _filter, _curated, _downloader, _dead


def _confirm_dead(
    api: ApiClient,
    dead: DeadStore,
    workshop_id: str,
    mirror_detail: Optional[dict] = None,
) -> Optional[dict]:
    """死图检测：缓存命中直接返回；镜像站空壳/is_fallback 时用 Steam 确认后标记。

    空壳信号：is_fallback=true，或 title 为空/"Unknown"（镜像站对已删物品的
    残缺返回）。必须带 id 字段才标记——打错的 id 镜像站不认识（无 id），
    不应污染死图缓存。无 Steam key 时按信号保守标记（reason 注明）。
    返回死图记录或 None。
    """
    rec = dead.is_dead(workshop_id)
    if rec:
        return rec
    if mirror_detail is None:
        return None
    title = (mirror_detail.get("title") or "").strip()
    is_shell = (
        mirror_detail.get("is_fallback")
        or not title
        or title == "Unknown"
    )
    if not is_shell or not mirror_detail.get("id"):
        return None
    if api.steam.cfg.steam_api_key:
        try:
            steam_item = api.steam.get_file_details(workshop_id)
        except Exception as e:
            logger.warning(f"dead confirm {workshop_id} failed: {e}")
            return None
        # get_file_details 已在源头过滤空壳（result=1 但 title 空 → None），
        # 故非 None 即 Steam 确认有效 → 不是死图
        if steam_item:
            return None
        reason = "Steam 已删除或私密"
    else:
        reason = "镜像站 Unknown/is_fallback（物品不存在或已删除）"
    dead.mark_dead(workshop_id, reason)
    return {"detected_at": datetime.now().isoformat(timespec="seconds"), "reason": reason}


mcp = FastMCP("l4d2-workshop-mirror")


@mcp.tool
def check_eligibility(workshop_id: str = "", title: str = "") -> dict:
    """检查一张图是否能下载。零网络调用，纯本地查询（filter.json + 死图缓存）。

    适用场景：下载前预检、批量过滤。

    Args:
        workshop_id: 工坊物品 ID（与 title 至少传一个）
        title: 物品标题（workshop_id 没传或不命中时使用）

    Returns:
        {
            "state": "allowed"|"blacklisted"|"dedup_active"|"dedup_expired"
                    |"dead"|"not_found",
            "days_remaining": int (仅 dedup_active 时有),
            "source": str
        }
    """
    _, _, flt, _, _, dead = _ensure_init()
    if not workshop_id and not title:
        return {"state": "not_found", "days_remaining": None, "source": "no_input"}
    # 死图预检（仅 workshop_id 维度——死图缓存无标题索引，且标题可能对应同名新图）
    if workshop_id:
        dead_rec = dead.is_dead(workshop_id)
        if dead_rec:
            return {
                "state": "dead",
                "days_remaining": None,
                "source": (
                    f"dead_map: {dead_rec.get('reason', '')} "
                    f"(检测于 {dead_rec.get('detected_at', '')})"
                ),
            }
    return flt.check_eligibility(
        workshop_id=workshop_id or None, title=title or None
    )


@mcp.tool
def get_map_detail(
    workshop_id: str,
    include_description: bool = False,
) -> dict:
    """查询单张地图的完整详情，包含 phase2 战役判定结果。

    默认不返回 description（省 token），需要时设 include_description=True（截断到 500 字符）。

    Args:
        workshop_id: 工坊物品 ID
        include_description: 是否返回 description（截断到 500 字符）

    Returns:
        {
            "id", "title", "size_mb", "subscriptions", "favorited",
            "is_campaign": bool,
            "score": int,
            "reasons": [str],
            "ambiguous": bool,
            "definitely_mod": bool,
            "confidence": "high"|"medium"|"low",
            "series_info": {...}|null,
            "dependency_count": int,
            "eligibility": str,
            "in_curated_list": bool,
            "curated_tags": [str],
            "description": str (仅 include_description=True 时返回)
        }
    """
    _, api, flt, curated, _, dead = _ensure_init()

    # B5：死图缓存标注（镜像站 is_fallback 的新死图在 search/install 路径检测）
    dead_rec = dead.is_dead(workshop_id)

    try:
        detail = api.get_detail_with_fallback(workshop_id)
    except Exception as e:
        return {"error": f"detail fetch failed: {e}", "id": workshop_id}

    if not detail:
        if dead_rec:
            return {
                "id": workshop_id,
                "dead": True,
                "dead_reason": dead_rec.get("reason", ""),
                "error": "not_found",
            }
        # 新死图检测：mirror 返回 is_fallback 时确认并标记
        try:
            m_detail = api.mirror.detail(workshop_id)
        except Exception:
            m_detail = None
        dead_rec = _confirm_dead(api, dead, workshop_id, m_detail)
        if dead_rec:
            return {
                "id": workshop_id,
                "dead": True,
                "dead_reason": dead_rec.get("reason", ""),
                "error": "not_found",
            }
        return {"error": "not_found", "id": workshop_id}

    curated_entry = curated.find_by_workshop_id(workshop_id)
    in_curated = curated_entry is not None
    curated_tags = curated_entry.tags if curated_entry else []
    curated_chapters = curated_entry.chapters if curated_entry else []

    judgment = judge_campaign(
        detail,
        in_curated_list=in_curated,
        curated_chapters=curated_chapters,
    )

    eligibility = flt.check_eligibility(
        workshop_id=workshop_id, title=detail.get("title", "")
    )

    series_info = None
    try:
        series = _resolve_series_parts(api, workshop_id, aggressive=False)
        if series["is_series"]:
            series_info = {
                "base_name": series["base_name"],
                "total_parts": series["total_parts"],
                "current": series.get("current_part"),
                "confidence": series["confidence"],
            }
    except Exception as e:
        logger.debug(f"series detect failed: {e}")

    dep_count = (
        len(detail.get("required_items") or [])
        + len(detail.get("children_ids") or [])
    )

    size_human = detail.get("file_size_human", "")
    size_mb = _parse_size_mb(size_human)

    result: dict = {
        "id": str(detail.get("id") or workshop_id),
        "title": detail.get("title", ""),
        "title_zh": (curated_entry.title_zh if curated_entry else ""),
        "size_mb": size_mb,
        "size_human": size_human,
        "subscriptions": int(detail.get("subscriptions") or 0),
        "favorited": int(detail.get("favorited") or 0),
        "is_campaign": judgment["is_campaign"],
        "score": judgment["score"],
        "reasons": judgment["reasons"],
        "ambiguous": judgment["ambiguous"],
        "definitely_mod": judgment["definitely_mod"],
        "confidence": judgment["confidence"],
        "series_info": series_info,
        "dependency_count": dep_count,
        "eligibility": eligibility["state"],
        "in_curated_list": in_curated,
        "curated_tags": curated_tags,
        "dead": dead_rec is not None,
        "dead_reason": (dead_rec.get("reason", "") if dead_rec else ""),
    }
    if include_description:
        desc = detail.get("description") or ""
        result["description"] = desc[:500] + ("..." if len(desc) > 500 else "")
    return result


@mcp.tool
def search_campaigns(
    keyword: str = "",
    days: int = -1,
    sort: str = "trend",
    limit: int = 10,
    include_ambiguous: bool = False,
) -> dict:
    """在工坊中搜索战役地图，用 phase2 多因子规则预筛，
    自动过滤黑名单和 active 去重。

    默认只返回 score≥3 的高可信结果。模棱两可的（score 1-2，可能是 mod）
    默认不返回；如需查看，设 include_ambiguous=True。

    Args:
        keyword: 搜索关键词（空字符串表示不限定）
        days: 时间范围 1/7/30/90/180/365/-1（默认 -1 = 所有时间）
        sort: 排序 trend/new/relevance
        limit: 返回条数上限（默认 10，最大 30）
        include_ambiguous: 是否返回 score=1-2 的模糊判定结果（默认 False）。
            开启后每条会带 likely_mod 提示，便于 LLM/用户决策。

    Returns:
        {"items": [{workshop_id, title, size_mb, subscriptions,
                    is_campaign_score, eligibility, in_curated_list,
                    ambiguous: bool, likely_mod: bool}],
         "summary": {"high_confidence": N, "ambiguous": N, "filtered_out": N}}
    """
    limit = max(1, min(30, limit))
    _, api, flt, curated, _, dead = _ensure_init()

    try:
        data = api.mirror.query(
            page=1,
            num=min(limit * 2, 25),
            sort=sort,
            search=keyword or None,
            tags=["Campaigns"] if not keyword else None,
            days=days if days != -1 else None,
        )
    except Exception as e:
        return {"error": f"search failed: {e}", "items": [], "summary": {}}

    items = data.get("items") or []
    out: list[dict] = []
    stats = {"high_confidence": 0, "ambiguous": 0, "filtered_out": 0, "dead": 0}

    from core.campaign_judge import MOD_TAGS, MOD_TITLE_PATTERNS
    import re as _re

    def _quick_filter(item: dict) -> bool:
        raw_tags = item.get("tags") or []
        tag_names = {
            (t.get("tag") if isinstance(t, dict) else str(t))
            for t in raw_tags
            if t
        }
        if {"Models", "Textures"}.issubset(tag_names):
            return False
        if "Textures" in tag_names and "Co-op" not in tag_names:
            return False
        if "Scripts" in tag_names and "Co-op" not in tag_names:
            return False
        if len(tag_names & MOD_TAGS) >= 3:
            return False
        title_l = (item.get("title") or "").lower()
        for pattern in MOD_TITLE_PATTERNS:
            if _re.search(pattern, title_l, _re.IGNORECASE):
                return False
        return True

    # F：并行二次 detail + phase2 判定（N+1 串行 → 并发）
    def _judge_item(item: dict) -> Optional[dict]:
        wid = str(item.get("id") or "")
        if not wid:
            return None
        if not _quick_filter(item):
            stats["filtered_out"] += 1
            return None

        title = item.get("title") or ""
        elig = flt.check_eligibility(workshop_id=wid, title=title)
        if elig["state"] in ("blacklisted", "dedup_active"):
            stats["filtered_out"] += 1
            return None

        in_curated = curated.find_by_workshop_id(wid) is not None
        c_entry = curated.find_by_workshop_id(wid)

        try:
            detail = api.mirror.detail(wid)
            # B5：死图检测（mirror is_fallback + Steam 确认 → 入缓存并跳过）
            dead_rec = _confirm_dead(api, dead, wid, detail)
            if dead_rec:
                stats["dead"] += 1
                return None
            judgment = judge_campaign(
                detail,
                in_curated_list=in_curated,
                curated_chapters=c_entry.chapters if c_entry else [],
            )
            score = judgment["score"]
            size_mb = _parse_size_mb(detail.get("file_size_human", ""))
        except Exception:
            score = -5
            size_mb = None
            judgment = {"ambiguous": False, "definitely_mod": True}

        if judgment["definitely_mod"]:
            stats["filtered_out"] += 1
            return None

        if score < 1:
            stats["filtered_out"] += 1
            return None

        is_amb = judgment["ambiguous"]
        if is_amb:
            stats["ambiguous"] += 1
        else:
            stats["high_confidence"] += 1

        return {
            "workshop_id": wid,
            "title": title,
            "title_zh": (c_entry.title_zh if c_entry else ""),
            "size_mb": size_mb,
            "subscriptions": int(item.get("subscriptions") or 0),
            "is_campaign_score": score,
            "eligibility": elig["state"],
            "in_curated_list": in_curated,
            "ambiguous": is_amb,
            "likely_mod": is_amb and not in_curated,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        judged = list(ex.map(_judge_item, items))

    for entry in judged:
        if entry is None:
            continue
        if entry["ambiguous"] and not include_ambiguous:
            continue
        if len(out) >= limit:
            break
        out.append(entry)
    return {"items": out, "summary": stats}


@mcp.tool
def find_series_parts(
    workshop_id: str,
    aggressive: bool = True,
) -> dict:
    """识别一张图所属的完整系列（所有 Part）。

    融合三路信号：①正向依赖(required_items+children+description)
                ②反向查询作者全部物品(/user-files)
                ③命名模式识别。

    Args:
        workshop_id: 工坊物品 ID
        aggressive: 命名像系列但 API 查不到关系时是否按 base_name 自动归类（默认 True）

    Returns:
        {
            "is_series": bool,
            "base_name": str,
            "total_parts": int|None,
            "parts": [{"n": int, "workshop_id": str, "title": str, "source": str}],
            "confidence": "high"|"medium"|"low"
        }
    """
    _, api, _, _, _, dead = _ensure_init()

    # B6：死图返回明确提示，避免误判为"识别失败"
    dead_rec = dead.is_dead(workshop_id)
    if dead_rec:
        return {
            "is_series": False,
            "status": "unavailable",
            "base_name": "",
            "total_parts": None,
            "parts": [],
            "confidence": "low",
            "current_part": None,
            "reason": (
                f"该物品在 Steam 已不可用（{dead_rec.get('reason', '')}，"
                f"检测于 {dead_rec.get('detected_at', '')}）"
            ),
        }
    return _resolve_series_parts(api, workshop_id, aggressive=aggressive)


@mcp.tool
def install_campaign(
    workshop_id: str = "",
    title: str = "",
    force: bool = False,
    auto_parts: bool = True,
    strict: bool = True,
    async_mode: bool = False,
) -> dict:
    """一站式下载：识别 → 解析依赖 → 下载 → 落盘 → 更新去重名单。

    适用：用户给出 workshop_id、steam 链接或具体地图名要下载时。
    workshop_id 与 title 至少传一个：
      - workshop_id 可为纯数字 id、steamcommunity 链接（自动提取 id）
      - title 为中文/英文标题时走内部解析链（curated 模糊 → 镜像搜索 → phase2 判定），
        唯一高可信匹配自动下载，多候选返回 candidates[] 待确认

    Args:
        workshop_id: 工坊物品 ID 或 steam 链接
        title: 地图标题（workshop_id 为空时使用）
        force: 忽略 dedup_active 限制（黑名单永远不可绕过）
        auto_parts: 自动识别并下载同系列 Part
        strict: 严格模式（默认 True）。definitely_mod 物品直接拒绝下载；
                ambiguous 物品返回 preview 不下载，需用户/LLM 确认后传 strict=False 重试。
        async_mode: True 时后台异步下载，立即返回 job_id，用 get_download_job 查询进度

    Returns:
        {
            "eligibility": str,
            "main": {id, title, title_zh, size_mb, saved_path, status},
            "parts": [{n, id, title, status, source}],
            "dependencies": [{id, title, status}],
            "downloaded_total": int,
            "filter_updated": bool,
            "missing": [{id, reason}],
            "judgment": {...}|null,  # phase2 判定结果
            "suggested_action": "auto_download"|"ask_user_confirm"|"user_explicit_request",
            "preview_only": bool,  # strict 拦截/标题多候选时为 True，未实际下载
            "candidates": [...]|null,  # 标题解析出多候选时返回
            "job_id": str|null  # async_mode 时返回
        }
    """
    cfg, api, flt, curated, downloader, dead = _ensure_init()

    wid, title_text = _normalize_input(workshop_id, title)
    candidates = None

    if not wid:
        if not title_text:
            return _failed("", "workshop_id 与 title 至少传一个")
        resolution = _resolve_map(title_text, limit=5)
        items = resolution.get("items") or []
        if not items:
            return {
                "eligibility": "not_found",
                "main": {"id": "", "title": title_text, "status": "skipped"},
                "parts": [],
                "dependencies": [],
                "downloaded_total": 0,
                "filter_updated": False,
                "missing": [{"id": "", "reason": "未找到匹配的地图，建议提供工坊链接"}],
                "judgment": None,
                "suggested_action": "ask_user_confirm",
                "preview_only": True,
                "candidates": [],
                "reason": "未找到匹配的地图，建议提供工坊链接",
            }
        best = items[0]
        gap = (
            (best["is_campaign_score"] - items[1]["is_campaign_score"])
            if len(items) > 1
            else 999
        )
        # 自动下载三重门槛：分数 ≥3、与第二名差距 ≥2、标题相似度 ≥0.8。
        # 相似度门槛防止"死亡小镇"误配到相似度 0.5 的"死亡之地"（实测事故）
        best_ratio = best.get("match_ratio") or 0
        if (
            best["is_campaign_score"] >= 3
            and gap >= 2
            and best_ratio >= 0.8
        ):
            wid = best["workshop_id"]
            logger.info(
                f"title 解析唯一高可信命中: {best['title']} ({wid}) score={best['is_campaign_score']}"
            )
        else:
            return {
                "eligibility": "needs_confirmation",
                "main": None,
                "parts": [],
                "dependencies": [],
                "downloaded_total": 0,
                "filter_updated": False,
                "missing": [],
                "judgment": None,
                "suggested_action": "ask_user_confirm",
                "preview_only": True,
                "candidates": items[:5],
                "reason": "标题解析出多个候选（或判定不够高可信），请用户选择后传入 workshop_id 重试",
            }

    elig = flt.check_eligibility(workshop_id=wid, title=title_text or None)

    # B5：死图缓存命中直接拒绝（不必等 detail/下载）
    dead_rec = dead.is_dead(wid)
    if dead_rec:
        return {
            "eligibility": "dead",
            "main": None,
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": None,
            "suggested_action": "reject",
            "preview_only": True,
            "candidates": candidates,
            "reason": (
                f"该物品在 Steam 已不可用（{dead_rec.get('reason', '')}，"
                f"检测于 {dead_rec.get('detected_at', '')}），无法下载"
            ),
        }

    if elig["state"] == "blacklisted":
        return {
            "eligibility": "blacklisted",
            "main": None,
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": None,
            "suggested_action": "reject",
            "preview_only": True,
            "candidates": candidates,
            "reason": f"在黑名单中: {elig.get('source', '')}",
        }
    if elig["state"] == "dedup_active" and not force:
        return {
            "eligibility": "dedup_active",
            "main": None,
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": None,
            "suggested_action": "ask_user_confirm",
            "preview_only": True,
            "candidates": candidates,
            "reason": (
                f"在 {flt.dedup_days} 天去重名单中，剩余 {elig.get('days_remaining')} 天；"
                f"如需强制下载，传 force=True"
            ),
        }

    if async_mode:
        job_id = _submit_install_job(wid, force, auto_parts, strict)
        return {
            "eligibility": elig["state"],
            "job_id": job_id,
            "status": "queued",
            "queued": True,
            "main": None,
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": None,
            "suggested_action": "download_in_progress",
            "note": "任务已排队并在后台执行，用 get_download_job(job_id=...) 查询进度；不需要二次确认",
        }

    return _install_impl(wid, force, auto_parts, strict)


def _install_impl(
    workshop_id: str,
    force: bool = False,
    auto_parts: bool = True,
    strict: bool = True,
) -> dict:
    cfg, api, flt, curated, downloader, dead = _ensure_init()

    # B5：异步/并发路径的二次死图检查（排队期间可能已标记）
    dead_rec = dead.is_dead(workshop_id)
    if dead_rec:
        return {
            "eligibility": "dead",
            "main": {
                "id": workshop_id,
                "title": "",
                "status": "rejected",
            },
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": None,
            "suggested_action": "reject",
            "preview_only": True,
            "reason": (
                f"该物品在 Steam 已不可用（{dead_rec.get('reason', '')}，"
                f"检测于 {dead_rec.get('detected_at', '')}），无法下载"
            ),
        }

    try:
        detail = api.get_detail_with_fallback(workshop_id)
    except Exception as e:
        return _failed(workshop_id, f"detail fetch failed: {e}")
    if not detail:
        # 新死图检测：mirror is_fallback + Steam 确认（或保守标记）→ dead
        try:
            m_detail = api.mirror.detail(workshop_id)
        except Exception:
            m_detail = None
        dead_rec = _confirm_dead(api, dead, workshop_id, m_detail)
        if dead_rec:
            return {
                "eligibility": "dead",
                "main": {
                    "id": workshop_id,
                    "title": "",
                    "status": "rejected",
                },
                "parts": [],
                "dependencies": [],
                "downloaded_total": 0,
                "filter_updated": False,
                "missing": [],
                "judgment": None,
                "suggested_action": "reject",
                "preview_only": True,
                "reason": (
                    f"该物品在 Steam 已不可用（{dead_rec.get('reason', '')}，"
                    f"检测于 {dead_rec.get('detected_at', '')}），无法下载"
                ),
            }
        return _failed(workshop_id, "detail not found")

    main_title = detail.get("title", "") or f"workshop_{workshop_id}"

    # BUG A 修复：拿到真实标题后必须带 title 重新查资格（黑名单按标题匹配）
    elig = flt.check_eligibility(workshop_id=workshop_id, title=main_title)
    if elig["state"] == "blacklisted":
        return {
            "eligibility": "blacklisted",
            "main": {
                "id": workshop_id,
                "title": main_title,
                "status": "rejected",
            },
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": None,
            "suggested_action": "reject",
            "preview_only": True,
            "reason": f"标题命中黑名单: {elig.get('source', '')}",
        }
    if elig["state"] == "dedup_active" and not force:
        return {
            "eligibility": "dedup_active",
            "main": {
                "id": workshop_id,
                "title": main_title,
                "status": "rejected",
            },
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": None,
            "suggested_action": "ask_user_confirm",
            "preview_only": True,
            "reason": (
                f"标题命中去重名单，剩余 {elig.get('days_remaining')} 天；"
                f"如需强制下载，传 force=True"
            ),
        }

    curated_entry = curated.find_by_workshop_id(workshop_id)
    in_curated = curated_entry is not None
    judgment = judge_campaign(
        detail,
        in_curated_list=in_curated,
        curated_chapters=curated_entry.chapters if curated_entry else [],
    )

    if judgment["definitely_mod"] and strict:
        return {
            "eligibility": "rejected_as_mod",
            "main": {
                "id": workshop_id,
                "title": main_title,
                "size_mb": _parse_size_mb(detail.get("file_size_human", "")),
                "status": "rejected",
            },
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": judgment,
            "suggested_action": "ask_user_confirm",
            "preview_only": True,
            "reason": (
                "phase2 判定该物品是 mod 而非战役（含强负向信号）。"
                "如用户明确给出工坊链接/精确标题并确认要下，传 strict=False 重试。"
            ),
        }

    if strict and judgment["ambiguous"]:
        return {
            "eligibility": "needs_confirmation",
            "main": {
                "id": workshop_id,
                "title": main_title,
                "size_mb": _parse_size_mb(detail.get("file_size_human", "")),
                "status": "preview",
            },
            "parts": [],
            "dependencies": [],
            "downloaded_total": 0,
            "filter_updated": False,
            "missing": [],
            "judgment": judgment,
            "suggested_action": "ask_user_confirm",
            "preview_only": True,
            "reason": (
                "phase2 判定为模糊（score 在 1-2 之间），未自动下载。"
                "请向用户展示 judgment.reasons 并询问是否确认下载；"
                "用户确认后传 strict=False 重试。"
            ),
        }

    main_url = detail.get("file_url", "")
    workshop_dir = Path(cfg.workshop_dir)
    workshop_dir.mkdir(parents=True, exist_ok=True)

    parts: list[dict] = []
    dependencies: list[dict] = []
    downloaded: list[str] = []
    missing: list[dict] = []
    filter_updated = False
    part_ids: set[str] = {workshop_id}

    if auto_parts:
        try:
            series = _resolve_series_parts(api, workshop_id, aggressive=True)
            if series["is_series"] and len(series["parts"]) > 1:
                for p in series["parts"]:
                    pid = p["workshop_id"]
                    part_ids.add(pid)
                    if pid == workshop_id:
                        parts.append(
                            {
                                "n": p.get("n") or 1,
                                "id": pid,
                                "title": main_title,
                                "status": "pending",
                                "source": p["source"],
                            }
                        )
                        continue
                    p_status = _download_one_item(
                        api, downloader, pid, workshop_dir, flt
                    )
                    parts.append(
                        {
                            "n": p.get("n") or (len(parts) + 1),
                            "id": pid,
                            "title": p_status["title"],
                            "status": p_status["status"],
                            "source": p["source"],
                            "saved_path": p_status.get("saved_path"),
                            "size_mb": p_status.get("size_mb"),
                        }
                    )
                    if p_status["status"] == "downloaded":
                        downloaded.append(p_status["saved_path"])
                        flt.add_downloaded(
                            p_status["title"], pid, source="downloaded_part"
                        )
                        filter_updated = True
                    elif p_status["status"] == "failed":
                        missing.append(
                            {"id": pid, "reason": p_status.get("error", "")}
                        )
        except Exception as e:
            logger.warning(f"auto_parts failed: {e}")

    # E-1: 依赖解析从 {入口 id} ∪ {全部 part id} 并集出发——
    # 只有 part1 标了 required_items 时，从 part2/n 出发也必须能捕获其依赖
    try:
        deps_map = resolve_dependencies_union(api, sorted(part_ids), max_depth=2)
        for dep_id, dep_info in deps_map.items():
            # E-3: 已在 parts 清单里的 id 不再重复下载/报告
            if dep_id == workshop_id or dep_id in part_ids:
                continue
            if dep_info.get("depth", 0) > 2:
                continue
            dep_title = dep_info.get("title", "") or f"dep_{dep_id}"
            dep_url = dep_info.get("file_url", "")
            if not dep_url:
                dependencies.append(
                    {
                        "id": dep_id,
                        "title": dep_title,
                        "status": "skipped",
                        "reason": "no file_url",
                    }
                )
                continue
            save_name = sanitize_filename(dep_title)
            save_path = workshop_dir / save_name
            if save_path.exists():
                dependencies.append(
                    {
                        "id": dep_id,
                        "title": dep_title,
                        "status": "skipped",
                        "saved_path": str(save_path),
                        "reason": "already_exists",
                    }
                )
                continue
            r = downloader.download(dep_url, save_path)
            dep_entry: dict = {
                "id": dep_id,
                "title": dep_title,
                "status": "downloaded" if r["success"] else "failed",
            }
            if r["success"]:
                dep_entry.update(
                    {
                        "saved_path": r["saved_path"],
                        "size_mb": r["size_mb"],
                    }
                )
                downloaded.append(r["saved_path"])
                flt.add_downloaded(dep_title, dep_id, source="downloaded_dep")
                filter_updated = True
            else:
                dep_entry["error"] = r["attempts"][-1].get("msg", "") if r["attempts"] else "unknown"
                missing.append({"id": dep_id, "reason": dep_entry["error"]})
            dependencies.append(dep_entry)
    except Exception as e:
        logger.warning(f"resolve_dependencies failed: {e}")

    main_status = "pending"
    main_saved = None
    main_size = None
    if main_url:
        save_name = sanitize_filename(main_title)
        save_path = workshop_dir / save_name
        if save_path.exists():
            main_status = "skipped"
            main_saved = str(save_path)
        else:
            r = downloader.download(main_url, save_path)
            if r["success"]:
                main_status = "downloaded"
                main_saved = r["saved_path"]
                main_size = r["size_mb"]
                downloaded.append(r["saved_path"])
                flt.add_downloaded(main_title, workshop_id, source="downloaded")
                filter_updated = True
            else:
                main_status = "failed"
                last_msg = (
                    r["attempts"][-1].get("msg", "")
                    if r["attempts"]
                    else "download failed"
                )
                missing.append(
                    {"id": workshop_id, "reason": last_msg}
                )
                # 第四项：下载成功但 VPK 校验失败 = 源站给了假文件（网络问题
                # 走 raise_for_status，到不了校验这步）。回查镜像站原始返回，
                # 若是空壳形态 → 实锤死图，回写缓存避免反复下载（30 天 TTL）
                if "validation failed" in last_msg:
                    try:
                        m_detail = api.mirror.detail(workshop_id)
                    except Exception:
                        m_detail = None
                    confirm = _confirm_dead(api, dead, workshop_id, m_detail)
                    if confirm:
                        logger.info(
                            f"下载校验失败且镜像站为空壳，标记死图 {workshop_id}: "
                            f"{confirm.get('reason', '')}"
                        )
    else:
        main_status = "skipped"
        missing.append({"id": workshop_id, "reason": "no file_url"})

    # A3：主图下载结果回填 parts 里的主图项，报告自洽
    for p in parts:
        if p["id"] == workshop_id:
            p["status"] = main_status
            if main_saved:
                p["saved_path"] = main_saved
            if main_size:
                p["size_mb"] = main_size
            break

    idle_sync_result = None
    if filter_updated and cfg.idle_dir:
        try:
            idle_sync_result = _sync_idle_internal(cfg, flt)
        except Exception as e:
            logger.warning(f"idle sync failed: {e}")

    return {
        "eligibility": "allowed",
        "main": {
            "id": workshop_id,
            "title": main_title,
            "title_zh": (curated_entry.title_zh if curated_entry else ""),
            "size_mb": main_size,
            "saved_path": main_saved,
            "status": main_status,
        },
        "parts": parts,
        "dependencies": dependencies,
        "downloaded_total": len(downloaded),
        "filter_updated": filter_updated,
        "missing": missing,
        "judgment": judgment,
        "suggested_action": "auto_download" if judgment["is_campaign"] else "ask_user_confirm",
        "preview_only": False,
        "idle_sync": idle_sync_result,
    }


def _download_one_item(
    api: ApiClient,
    downloader: Downloader,
    workshop_id: str,
    workshop_dir: Path,
    flt: FilterStore,
) -> dict:
    try:
        detail = api.get_detail_with_fallback(workshop_id)
    except Exception as e:
        return {"title": "", "status": "failed", "error": str(e)}
    if not detail:
        return {"title": "", "status": "failed", "error": "not_found"}
    title = detail.get("title", "") or f"workshop_{workshop_id}"

    # BUG A：Part 下载前同样查黑名单（按标题）
    elig = flt.check_eligibility(workshop_id=workshop_id, title=title)
    if elig["state"] == "blacklisted":
        return {
            "title": title,
            "status": "skipped",
            "reason": f"blacklisted: {elig.get('source', '')}",
        }

    url = detail.get("file_url", "")
    if not url:
        return {"title": title, "status": "skipped", "reason": "no file_url"}
    save_path = workshop_dir / sanitize_filename(title)
    if save_path.exists():
        return {
            "title": title,
            "status": "skipped",
            "saved_path": str(save_path),
            "reason": "already_exists",
        }
    r = downloader.download(url, save_path)
    if r["success"]:
        flt.add_downloaded(title, workshop_id, source="downloaded_part")
        return {
            "title": title,
            "status": "downloaded",
            "saved_path": r["saved_path"],
            "size_mb": r["size_mb"],
        }
    return {
        "title": title,
        "status": "failed",
        "error": r["attempts"][-1].get("msg", "") if r["attempts"] else "unknown",
    }


def _failed(workshop_id: str, reason: str) -> dict:
    return {
        "eligibility": "failed",
        "main": {"id": workshop_id, "title": "", "status": "failed"},
        "parts": [],
        "dependencies": [],
        "downloaded_total": 0,
        "filter_updated": False,
        "missing": [{"id": workshop_id, "reason": reason}],
        "judgment": None,
        "suggested_action": "reject",
        "preview_only": True,
    }


def _normalize_input(workshop_id: str, title: str) -> tuple[str, str]:
    """归一化用户输入：steam 链接/纯数字 → id；其余按标题处理。"""
    raw = (workshop_id or "").strip()
    if raw:
        m = _STEAM_ID_RE.search(raw)
        if m:
            return m.group(1), (title or "").strip()
        if _RAW_ID_RE.match(raw):
            return raw, (title or "").strip()
    return "", (title or raw).strip()


def _title_match_ratio(query: str, title: str) -> float:
    """查询词与标题的相似度：双向子串 → 1.0，否则 SequenceMatcher。"""
    from difflib import SequenceMatcher

    q = query.strip().lower()
    t = (title or "").strip().lower()
    if not q or not t:
        return 0.0
    if q in t or t in q:
        return 1.0
    return round(SequenceMatcher(None, q, t).ratio(), 3)


def _resolve_map(query: str, limit: int = 5) -> dict:
    """标题解析链：curated 模糊 → 镜像站搜索 → phase2 判定 → 排序。

    返回 {"query", "source", "items": [{workshop_id, title, title_zh, size_mb,
           subscriptions, is_campaign_score, match_ratio, reasons, in_curated_list}]}
    """
    cfg, api, flt, curated, _, _ = _ensure_init()
    query = (query or "").strip()
    candidates: dict[str, dict] = {}

    # ① curated 人工策展名单（质量最高，优先）
    for entry, score in curated.fuzzy_search(query, limit=10):
        wid = entry.workshop_id
        if not wid:
            continue
        candidates[str(wid)] = {
            "workshop_id": str(wid),
            "title": entry.title_original or entry.title_zh,
            "title_zh": entry.title_zh,
            "in_curated_list": True,
            "curated_bonus": 3,
            "match_ratio": score,
            "source": "curated",
        }

    # ② 镜像站搜索
    try:
        data = api.mirror.query(
            page=1, num=max(limit + 5, 8), sort="trend", search=query
        )
        for it in (data.get("items") or []):
            wid = str(it.get("id") or "")
            if not wid or wid in candidates:
                continue
            c_entry = curated.find_by_workshop_id(wid)
            candidates[wid] = {
                "workshop_id": wid,
                "title": it.get("title") or "",
                "title_zh": (c_entry.title_zh if c_entry else ""),
                "in_curated_list": c_entry is not None,
                "curated_bonus": 3 if c_entry else 0,
                "match_ratio": _title_match_ratio(query, it.get("title") or ""),
                "source": "mirror",
            }
    except Exception as e:
        logger.warning(f"resolve mirror search failed: {e}")

    if not candidates:
        return {"query": query, "source": "none", "items": []}

    # ③ 并行拉 detail + phase2 判定
    from core.campaign_judge import judge_campaign

    def _judge(cand: dict) -> dict:
        out = dict(cand)
        try:
            detail = api.get_detail_with_fallback(cand["workshop_id"])
        except Exception:
            return out
        if not detail:
            return out
        j = judge_campaign(
            detail,
            in_curated_list=cand.get("in_curated_list", False),
        )
        out["size_mb"] = _parse_size_mb(detail.get("file_size_human", ""))
        out["subscriptions"] = int(detail.get("subscriptions") or 0)
        out["is_campaign_score"] = j["score"] + cand.get("curated_bonus", 0)
        out["reasons"] = j["reasons"]
        return out

    items = list(candidates.values())
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(items)))) as ex:
        judged = list(ex.map(_judge, items))

    scored = [
        c
        for c in judged
        if c.get("is_campaign_score") is not None
    ]
    if not scored:
        scored = judged
    scored.sort(
        key=lambda c: (
            c.get("is_campaign_score") or -99,
            c.get("match_ratio") or 0,
            c.get("subscriptions") or 0,
        ),
        reverse=True,
    )
    # 显示层过滤：策展命中 或 高可信 或 标题相似度 ≥0.5 才展示。
    # 过滤"塔科夫死亡音效"这类镜像搜索沾边但无关的 mod 噪音。
    good = [
        c
        for c in scored
        if c.get("in_curated_list")
        or (c.get("is_campaign_score") or 0) >= 3
        or (c.get("match_ratio") or 0) >= 0.5
    ]
    if not good:
        # 兜底：至少不是 mod（score>=1），如"死亡小镇"→"死亡中心"这种沾边候选
        good = [
            c for c in scored if (c.get("is_campaign_score") or 0) >= 1
        ]
    return {
        "query": query,
        "source": "curated+mirror",
        "items": good[:limit],
    }


@mcp.tool
def resolve_map(query: str, limit: int = 5) -> dict:
    """解析地图标题为工坊候选（不下载）。

    适用：用户说"XXX 有没有 / XXX 的 id 是什么"，或 install_campaign 传标题
    前先看看候选。融合 echocn 人工策展名单与镜像站搜索，按 phase2 判定排序。

    Args:
        query: 中文/英文标题关键词
        limit: 返回候选条数上限（默认 5）

    Returns:
        {"query", "source", "items": [{workshop_id, title, title_zh, size_mb,
          subscriptions, is_campaign_score, in_curated_list, reasons}]}
    """
    _ensure_init()
    return _resolve_map(query, limit=limit)


@mcp.tool
def get_download_job(job_id: str) -> dict:
    """查询异步下载任务（install_campaign async_mode=True 时返回的 job_id）进度。

    Returns:
        {job_id, status: "queued"|"running"|"done"|"error",
         elapsed_s, result: install 结果|null, error: str|null}
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found", "result": None}
    elapsed = time.time() - job.get("started_at", time.time())
    return {
        "job_id": job_id,
        "status": job["status"],
        "elapsed_s": round(elapsed, 1),
        "result": job.get("result"),
        "error": job.get("error"),
    }


def _submit_install_job(
    workshop_id: str,
    force: bool,
    auto_parts: bool,
    strict: bool,
) -> str:
    global _job_executor
    with _jobs_lock:
        if _job_executor is None:
            _job_executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="l4d2-dl"
            )
    job_id = uuid.uuid4().hex[:8]
    job = {"status": "queued", "result": None, "error": None}
    with _jobs_lock:
        _jobs[job_id] = job

    def _run() -> None:
        with _jobs_lock:
            job["status"] = "running"
            job["started_at"] = time.time()
        try:
            result = _install_impl(workshop_id, force, auto_parts, strict)
            with _jobs_lock:
                job["status"] = "done"
                job["result"] = result
                job["finished_at"] = time.time()
        except Exception as e:
            logger.exception(f"install job {job_id} failed")
            with _jobs_lock:
                job["status"] = "error"
                job["error"] = str(e)
                job["finished_at"] = time.time()

    _job_executor.submit(_run)
    return job_id


def _sync_idle_internal(cfg: Config, flt: FilterStore) -> dict:
    """扫描闲置目录，把未在 dedup 名单的 vpk 加入。"""
    idle_path = Path(cfg.idle_dir)
    if not idle_path.exists():
        return {"synced": 0, "skipped_duplicates": 0, "reason": "idle_dir not exist"}
    synced = 0
    skipped = 0
    for vpk_file in idle_path.glob("*.vpk"):
        title = vpk_file.stem
        existing = flt.check_eligibility(title=title)
        if existing["state"] in ("dedup_active", "dedup_expired", "blacklisted"):
            skipped += 1
            continue
        flt.add_downloaded(title, "", source="idle_folder")
        synced += 1
    return {"synced": synced, "skipped_duplicates": skipped}


@mcp.tool
def list_new_campaigns(days: int = 7, limit: int = 20) -> dict:
    """列出最近 N 天新增的战役地图（增量发现）。

    自动跳过黑名单和 active 去重，融合 echocn 策展名单。

    Args:
        days: 时间范围（默认 7）
        limit: 返回条数上限（默认 20）

    Returns:
        {"items": [{workshop_id, title, size_mb, uploaded_at,
                    in_curated_list, is_campaign_score}]}
    """
    return search_campaigns(
        keyword="", days=days, sort="new", limit=min(limit, 30)
    )


@mcp.tool
def query_curated_list(
    keyword: str = "",
    tag: str = "",
    limit: int = 20,
) -> dict:
    """查询 echocn 人工策展的 461 条高质量战役地图名单。

    含 16 种中文标签、章节信息、人工标注。
    这是质量最高的数据源，应优先用于判定和推荐。

    Args:
        keyword: 中英文标题模糊搜索（空字符串不限定）
        tag: 中文标签名（如 "高质量"/"高难度"/"推荐"）
        limit: 返回条数上限（默认 20）

    Returns:
        {"items": [{title_zh, title_original, workshop_id, tags, chapters,
                    uploaded_at, file_size_mb}],
         "cached_at": str}
    """
    _, _, _, curated, _, _ = _ensure_init()
    items, cached_at = curated.query(
        keyword=keyword, tag=tag, limit=limit
    )
    return {"items": items, "cached_at": cached_at}


@mcp.tool
def refresh_curated_list() -> dict:
    """手动刷新 echocn 缓存。

    MCP 启动时已自动拉取，每日后台也会自动刷新。
    此工具用于：①首次部署验证 ②怀疑缓存过期 ③排障。

    Returns:
        {success, total, sha256, cached_at, error?}
    """
    _, _, _, curated, _, _ = _ensure_init()
    meta = curated.refresh_blocking()
    return {
        "success": "error" not in meta,
        "total": meta.get("total", 0),
        "sha256": meta.get("sha256", ""),
        "cached_at": meta.get("last_fetch", ""),
        "error": meta.get("error"),
    }


@mcp.tool
def sync_idle_folder() -> dict:
    """扫描闲置地图目录（已玩过的图），将未在 dedup 名单的 .vpk 加入去重列表。

    适用场景：手动整理闲置目录后触发同步；install_campaign 完成后会自动调用一次。
    已在 dedup 名单的会跳过，不会重复添加。

    Returns:
        {
            "synced": int,            # 新加入条数
            "skipped_duplicates": int, # 已在名单跳过的条数
            "scanned_total": int,     # 扫描到的 .vpk 总数
            "idle_dir": str
        }
    """
    cfg, _, flt, _, _, _ = _ensure_init()
    if not cfg.idle_dir:
        return {
            "synced": 0,
            "skipped_duplicates": 0,
            "scanned_total": 0,
            "idle_dir": "",
            "error": "idle_dir not configured",
        }
    idle_path = Path(cfg.idle_dir)
    if not idle_path.exists():
        return {
            "synced": 0,
            "skipped_duplicates": 0,
            "scanned_total": 0,
            "idle_dir": cfg.idle_dir,
            "error": "idle_dir does not exist",
        }
    scanned = 0
    synced = 0
    skipped = 0
    for vpk_file in idle_path.glob("*.vpk"):
        scanned += 1
        title = vpk_file.stem
        existing = flt.check_eligibility(title=title)
        if existing["state"] in ("dedup_active", "dedup_expired", "blacklisted"):
            skipped += 1
            continue
        flt.add_downloaded(title, "", source="idle_folder")
        synced += 1
    return {
        "synced": synced,
        "skipped_duplicates": skipped,
        "scanned_total": scanned,
        "idle_dir": cfg.idle_dir,
    }


def _parse_size_mb(size_human: str) -> Optional[float]:
    if not size_human:
        return None
    s = size_human.strip().lower().replace(",", "")
    try:
        if "mb" in s:
            return round(float(s.replace("mb", "").strip()), 2)
        if "gb" in s:
            return round(float(s.replace("gb", "").strip()) * 1024, 2)
        if "kb" in s:
            return round(float(s.replace("kb", "").strip()) / 1024, 2)
        return round(float(s), 2)
    except (ValueError, TypeError):
        return None


def _self_start_ticks() -> float:
    """当前进程的 /proc 启动 tick（用于锁身份校验）。非 Linux 返回 0。"""
    try:
        with open("/proc/self/stat", "rb") as f:
            fields = f.read().split()
        if len(fields) > 21:
            return float(fields[21])
    except Exception:
        pass
    return 0.0


def _proc_start_ticks(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            fields = f.read().split()
        if len(fields) > 21:
            return float(fields[21])
    except Exception:
        pass
    return 0.0


def _proc_is_server(pid: int) -> bool:
    """校验 /proc/<pid>/cmdline 确为 l4d2 MCP 进程（防 pid 复用误杀无辜进程）。"""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ")
        return b"server.py" in cmd or b"l4d2" in cmd.lower()
    except Exception:
        return True  # /proc 不可读时不做 cmdline 限制，由 kill 结果兜底


def _pid_alive(pid: int) -> bool:
    # zombie 进程（state=Z）pid 仍存在、os.kill(pid,0) 仍成功，但进程已死——
    # 父进程未 reap 时（AstrBot 场景常见）会无限等待，必须显式识别
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            fields = f.read().split()
        if len(fields) > 2 and fields[2] == b"Z":
            return False
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_lock(lock_path: Path) -> dict:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        return {
            "pid": int(data.get("pid") or 0),
            "start_ticks": float(data.get("start_ticks") or 0),
        }
    except Exception:
        return {"pid": 0, "start_ticks": 0}


def _write_lock(lock_path: Path) -> None:
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "start_ticks": _self_start_ticks(),
                "started_at": time.time(),
            }
        ),
        encoding="utf-8",
    )


def _acquire_single_instance_lock(
    cfg: Config,
    same_process: Optional[callable] = None,
    terminate: Optional[callable] = None,
) -> bool:
    """单实例锁（data/mcp.lock）——最后启动者胜。

    防止 AstrBot 双进程并存导致 stdio 混流、job 状态路由错乱（生产反馈 2026-08）。
    新实例启动时若发现旧实例存活且确认为同一 l4d2 MCP 进程（pid + /proc 启动
    tick + cmdline 三重校验，防 pid 复用误杀），自动 SIGTERM 接管——保证
    AstrBot enable 后连接永远指向最新代码（生产现场：旧 v0.2 进程残留导致
    enable 后调用仍路由到旧 bug 代码）。
    kill 失败时回退为拒绝启动。锁残留（pid 已死/被复用）直接覆盖。

    same_process/terminate 参数供单测注入。
    """
    same_process = same_process or (
        lambda pid, ticks: (
            ticks > 0
            and _proc_start_ticks(pid) == ticks
            and _pid_alive(pid)
            and _proc_is_server(pid)
        )
    )
    terminate = terminate or _terminate_process

    lock_path = Path(cfg.data_dir) / "mcp.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = _read_lock(lock_path)
        if lock["pid"]:
            if same_process(lock["pid"], lock["start_ticks"]):
                if not terminate(lock["pid"]):
                    logger.error(
                        f"无法终止旧实例 pid={lock['pid']}（{lock_path}），拒绝启动"
                    )
                    return False
                logger.warning(
                    f"已接管并终止旧实例 (pid={lock['pid']}, {lock_path})"
                )
            else:
                logger.warning(
                    f"锁残留（pid={lock['pid']} 非本服务进程或已死），直接接管"
                )
        _write_lock(lock_path)
        return True
    except Exception as e:
        logger.warning(f"single instance lock failed (放行): {e}")
        return True


def _terminate_process(pid: int) -> bool:
    """SIGTERM 等待 3 秒，仍存活则 SIGKILL。返回是否成功清理。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    for _ in range(15):
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        return False
    time.sleep(0.2)
    return not _pid_alive(pid)


def main() -> None:
    cfg, *_ = _ensure_init()
    if not _acquire_single_instance_lock(cfg):
        sys.exit(1)
    logger.info("L4D2 Workshop Mirror MCP starting (stdio)")
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
