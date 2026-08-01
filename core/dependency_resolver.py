"""依赖解析（正向 + 反向）。

设计见 ARCHITECTURE.md §4.1.2 三路融合策略。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from core.api_client import ApiClient
from core import series_detector

logger = logging.getLogger(__name__)

STEAM_LINK_RE = re.compile(
    r"steamcommunity\.com/sharedfiles/filedetails/\?id=(\d+)", re.IGNORECASE
)


def _extract_required_ids(detail: dict) -> list[str]:
    ids: list[str] = []
    for r in detail.get("required_items") or []:
        if isinstance(r, dict):
            rid = r.get("id") or r.get("publishedfileid")
            if rid:
                ids.append(str(rid))
        elif r:
            ids.append(str(r))
    return ids


def _extract_children_ids(detail: dict) -> list[str]:
    ids: list[str] = []
    for c in detail.get("children") or []:
        cid = c.get("publishedfileid") or c.get("id")
        if cid:
            ids.append(str(cid))
    for cid in detail.get("children_ids") or []:
        if cid:
            ids.append(str(cid))
    return ids


def _extract_description_links(detail: dict) -> list[str]:
    desc = detail.get("description") or ""
    return [m.group(1) for m in STEAM_LINK_RE.finditer(desc)]


def resolve_dependencies(
    api: ApiClient,
    workshop_id: str,
    max_depth: int = 2,
) -> dict[str, dict]:
    """正向递归解析依赖。

    Returns:
        {workshop_id: {"title": str, "file_url": str, "depth": int,
                       "is_dependency": bool, "source": str}}
    """
    visited: dict[str, dict] = {}

    def _walk(wid: str, depth: int) -> None:
        if depth > max_depth or wid in visited:
            return
        try:
            detail = api.get_detail_with_fallback(wid)
        except Exception as e:
            logger.warning(f"resolve dep {wid} failed: {e}")
            return
        if not detail:
            return
        visited[wid] = {
            "title": detail.get("title", ""),
            "file_url": detail.get("file_url", ""),
            "file_size_human": detail.get("file_size_human", ""),
            "depth": depth,
            "is_dependency": depth > 0,
            "source": "forward",
        }
        dep_ids = set(
            _extract_required_ids(detail)
            + _extract_children_ids(detail)
            + _extract_description_links(detail)
        )
        for dep_id in dep_ids:
            _walk(dep_id, depth + 1)

    _walk(workshop_id, 0)
    return visited


def resolve_dependencies_union(
    api: ApiClient,
    roots: list[str],
    max_depth: int = 2,
) -> dict[str, dict]:
    """从多个根 id 并集解析依赖（E-1：入口 id + 全部发现的 part id）。

    场景 B 的补齐：只有 part1 标了 required_items，用户从 part2/n 出发时，
    若只从入口解析会漏掉 part1 声明的依赖。每个根独立递归后合并。
    """
    merged: dict[str, dict] = {}
    for root in roots:
        try:
            merged.update(resolve_dependencies(api, root, max_depth=max_depth))
        except Exception as e:
            logger.warning(f"resolve_dependencies root {root} failed: {e}")
    return merged


def find_series_parts(
    api: ApiClient,
    workshop_id: str,
    aggressive: bool = True,
) -> dict[str, Any]:
    """识别系列 Part。三路融合。

    Returns:
        {
            "is_series": bool,
            "base_name": str,
            "total_parts": int | None,
            "parts": [{"n": int, "workshop_id": str, "title": str, "source": str}],
            "confidence": "high"|"medium"|"low",
            "current_part": int | None,
        }
    """
    try:
        detail = api.get_detail_with_fallback(workshop_id)
    except Exception as e:
        logger.warning(f"find_series detail {workshop_id} failed: {e}")
        return _empty_series(workshop_id)
    if not detail:
        return _empty_series(workshop_id)

    title = detail.get("title", "")
    series = series_detector.detect_series(title, detail.get("description") or "")

    parts_by_id: dict[str, dict] = {}

    if series["is_series"]:
        parts_by_id[workshop_id] = {
            "n": series.get("current") or 1,
            "workshop_id": workshop_id,
            "title": title,
            "source": "inferred",
        }

    forward_ids: list[str] = []
    if detail.get("is_collection"):
        forward_ids.extend(_extract_children_ids(detail))
    # 正向路径必须包含 required_items（场景 B：Part 1 标了依赖 [P2,P3,P4]）
    forward_ids.extend(_extract_required_ids(detail))
    desc_ids = _extract_description_links(detail)
    for did in desc_ids:
        if did not in forward_ids:
            forward_ids.append(did)

    for fid in forward_ids:
        if fid == workshop_id or fid in parts_by_id:
            continue
        try:
            sub = api.get_detail_with_fallback(fid)
        except Exception:
            continue
        if not sub:
            continue
        sub_title = sub.get("title", "")
        if not series_detector.titles_share_base(title, sub_title):
            continue
        sub_series = series_detector.detect_series(sub_title)
        parts_by_id[fid] = {
            "n": sub_series.get("current") or (len(parts_by_id) + 1),
            "workshop_id": fid,
            "title": sub_title,
            "source": "forward",
        }

    creator = detail.get("creator") or ""
    if series["is_series"] and creator:
        try:
            user_files = api.mirror.user_files(creator, num=50)
            items = user_files.get("items") or []
            for item in items:
                item_id = str(item.get("id") or "")
                item_title = item.get("title") or ""
                if not item_id or item_id == workshop_id:
                    continue
                if item_id in parts_by_id:
                    continue
                # 预筛：候选标题本身必须是 part 系列
                cand_series = series_detector.detect_series(item_title)
                if not cand_series["is_series"]:
                    continue
                # 预筛：版本 token 必须一致（"魂斗罗Z V0.7" vs "ContraZ V0.7"）
                if not series_detector.share_version_token(
                    series["base_name"], cand_series["base_name"]
                ):
                    continue
                # 语言域修正：/user-files 与 /detail 标题语言可能不同（实测 ContraZ/魂斗罗Z），
                # 必须二次调 /detail 拿到与主标题同语言域的标题后再严格比较
                try:
                    sub = api.get_detail_with_fallback(item_id)
                except Exception:
                    continue
                if not sub:
                    continue
                sub_title = sub.get("title", "")
                if not series_detector.titles_share_base(title, sub_title):
                    continue
                parts_by_id[item_id] = {
                    "n": cand_series.get("current") or (len(parts_by_id) + 1),
                    "workshop_id": item_id,
                    "title": sub_title,
                    "source": "reverse",
                }
        except Exception as e:
            logger.warning(f"user_files lookup failed: {e}")

    if not parts_by_id and series["is_series"] and not aggressive:
        return _empty_series(workshop_id)

    if not parts_by_id or not series["is_series"]:
        if not series["is_series"]:
            return _empty_series(workshop_id)

    parts_list = sorted(parts_by_id.values(), key=lambda p: p.get("n") or 999)

    total = series.get("total")
    if total is None:
        total = len(parts_list)

    inferred_count = sum(1 for p in parts_list if p["source"] == "inferred")
    forward_count = sum(1 for p in parts_list if p["source"] == "forward")
    reverse_count = sum(1 for p in parts_list if p["source"] == "reverse")

    if forward_count > 0 and series.get("total"):
        confidence = "high"
    elif reverse_count > 0 and inferred_count == 1:
        confidence = "high"
    elif forward_count > 0 or reverse_count > 0:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "is_series": series["is_series"] or len(parts_list) > 1,
        "base_name": series.get("base_name") or title,
        "total_parts": total,
        "parts": parts_list,
        "confidence": confidence,
        "current_part": series.get("current"),
    }


def _empty_series(workshop_id: str) -> dict[str, Any]:
    return {
        "is_series": False,
        "base_name": "",
        "total_parts": None,
        "parts": [],
        "confidence": "low",
        "current_part": None,
    }
