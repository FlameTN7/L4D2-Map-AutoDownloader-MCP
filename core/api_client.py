"""镜像站 + SWD + Steam 三套 API 客户端。

设计见 ARCHITECTURE.md §2.2。
所有 HTTP 调用都封装在此；不做判定、不做缓存。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import cloudscraper
import requests

from config import Config

logger = logging.getLogger(__name__)


class MirrorClient:
    """zhrradiant.com 镜像站客户端。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.mirror_base.rstrip("/")
        self.timeout = cfg.request_timeout_seconds
        self.session = requests.Session()
        if cfg.proxies:
            self.session.proxies.update(cfg.proxies)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36"
                ),
            }
        )

    def query(
        self,
        page: int = 1,
        num: int = 32,
        sort: str = "trend",
        search: Optional[str] = None,
        tags: Optional[list[str] | str] = None,
        excluded_tags: Optional[list[str] | str] = None,
        days: Optional[int] = None,
    ) -> dict:
        params: dict[str, Any] = {"page": page, "num": num, "sort": sort}
        if search:
            params["search"] = search
        if tags:
            params["tags"] = ",".join(tags) if isinstance(tags, list) else tags
        if excluded_tags:
            params["excluded_tags"] = (
                ",".join(excluded_tags)
                if isinstance(excluded_tags, list)
                else excluded_tags
            )
        if days is not None:
            params["days"] = days

        url = f"{self.base}/query"
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def detail(self, item_id: str | int) -> dict:
        url = f"{self.base}/detail"
        r = self.session.get(
            url, params={"id": str(item_id)}, timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()

    def batch_items(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        url = f"{self.base}/items"
        r = self.session.post(
            url,
            json={"ids": [str(i) for i in ids]},
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json() or []

    def user_files(
        self, steamid: str, num: int = 50, page: int = 1
    ) -> dict:
        """作者物品列表（Part 反向查询黄金工具）。"""
        url = f"{self.base}/user-files"
        r = self.session.get(
            url,
            params={"steamid": steamid, "num": num, "page": page},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()


class SwdClient:
    """steamworkshopdownloader.io 客户端（依赖解析最全）。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.swd_base.rstrip("/")
        self.timeout = cfg.request_timeout_seconds
        self._scraper: Optional[cloudscraper.CloudScraper] = None

    def _get_scraper(self) -> cloudscraper.CloudScraper:
        if self._scraper is None:
            self._scraper = cloudscraper.create_scraper(
                browser={
                    "browser": "chrome",
                    "platform": "windows",
                    "mobile": False,
                }
            )
            if self.cfg.proxies:
                self._scraper.proxies.update(self.cfg.proxies)
            try:
                self._scraper.get(f"{self.base}/", timeout=self.timeout)
            except Exception as e:
                logger.warning(f"SWD warmup failed: {e}")
        return self._scraper

    def query_details(self, workshop_ids: list[str]) -> list[dict]:
        """查询物品详情（含依赖）。单个查询更稳。"""
        results: list[dict] = []
        for wid in workshop_ids:
            try:
                scraper = self._get_scraper()
                r = scraper.post(
                    f"{self.base}/api/details/file?format=json",
                    json=[str(wid)],
                    timeout=self.timeout,
                    headers={
                        "Origin": self.base,
                        "Referer": f"{self.base}/",
                        "Accept": "application/json, */*",
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        results.extend(data)
                    elif isinstance(data, dict):
                        for v in data.values():
                            if isinstance(v, dict):
                                results.append(v)
                else:
                    logger.warning(
                        f"SWD query {wid} status {r.status_code}"
                    )
            except Exception as e:
                logger.warning(f"SWD query {wid} error: {e}")
            time.sleep(0.5)
        return results


class SteamApiClient:
    """Steam Web API 客户端（仅元数据，无 children）。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.steam_api_base.rstrip("/")
        self.timeout = cfg.request_timeout_seconds
        self.session = requests.Session()
        if cfg.proxies:
            self.session.proxies.update(cfg.proxies)

    def get_file_details(self, workshop_id: str | int) -> Optional[dict]:
        """返回有效详情；确认不可用时返回 None，请求/响应错误向调用方抛出。"""
        key = self.cfg.steam_api_key
        if not key:
            return None
        url = (
            f"{self.base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        )
        try:
            r = self.session.post(
                url,
                data={
                    "itemcount": 1,
                    "publishedfileids[0]": str(workshop_id),
                    "key": key,
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            resp = data.get("response", {})
            if resp.get("result") != 1:
                raise ValueError(f"Steam API request failed: result={resp.get('result')}")
            files = resp.get("publishedfiledetails")
            if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
                raise ValueError("Steam API response missing file details")
            item = files[0]
            result = item.get("result")
            if result in (9, 15):  # FileNotFound / AccessDenied
                return None
            if result != 1:
                raise ValueError(f"Steam file lookup failed: result={result}")
            # 防御：Steam 对私密/删除物品可能返回 result=1 的空壳
            if (item.get("title") or "").strip():
                return item
        except Exception as e:
            logger.warning(f"Steam API {workshop_id} error: {e}")
            raise  # _confirm_dead 必须区分未能确认和已确认不可用
        return None


class ApiClient:
    """聚合三个客户端，方便注入。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mirror = MirrorClient(cfg)
        self.swd = SwdClient(cfg)
        self.steam = SteamApiClient(cfg)

    def get_detail_with_fallback(self, workshop_id: str) -> dict:
        """三级回退：镜像站 → SWD → Steam。

        镜像站优先；SWD 依赖解析最全；Steam 作为兜底（镜像站索引陈旧/
        返回 is_fallback 时，Steam 还能拿到官方元数据）。
        三源产物统一过空壳校验（_is_shell_detail）——镜像站/ SWD/ Steam 对
        已删物品都可能返回 title 空/"Unknown" 的残缺数据（实测 130928055：
        镜像站 title="" 但 file_url 有值；SWD 返回 file_url 也空的更空壳），
        任何一源的壳数据都不得透传，全挂返回 {} 交给 _confirm_dead 处理。
        """
        try:
            d = self.mirror.detail(workshop_id)
            if d and not _is_shell_detail(d):
                return d
        except Exception as e:
            logger.warning(f"mirror detail {workshop_id} failed: {e}")
        try:
            results = self.swd.query_details([str(workshop_id)])
            if results:
                converted = self._swd_to_detail(results[0])
                if not _is_shell_detail(converted):
                    return converted
        except Exception as e:
            logger.warning(f"SWD detail {workshop_id} failed: {e}")
        try:
            steam_item = self.steam.get_file_details(workshop_id)
            if steam_item:
                converted = self._steam_to_detail(steam_item)
                if not _is_shell_detail(converted):
                    return converted
        except Exception as e:
            logger.warning(f"Steam detail {workshop_id} failed: {e}")
        return {}

    def _steam_to_detail(self, steam_item: dict) -> dict:
        """把 Steam GetPublishedFileDetails 返回伪装成镜像站 detail 结构。"""
        children = []
        for c in steam_item.get("children") or []:
            cid = c.get("publishedfileid") or c.get("id")
            if cid:
                children.append(
                    {
                        "publishedfileid": str(cid),
                        "file_type": c.get("file_type", 0),
                    }
                )

        children_ids = [c["publishedfileid"] for c in children]

        return {
            "id": str(
                steam_item.get("publishedfileid") or steam_item.get("id") or ""
            ),
            "title": steam_item.get("title", ""),
            "description": steam_item.get("description", ""),
            "preview": steam_item.get("preview_url", ""),
            "file_url": steam_item.get("file_url", ""),
            "file_size_human": _bytes_to_human(steam_item.get("file_size")),
            "subscriptions": steam_item.get("subscriptions", 0),
            "favorited": steam_item.get("favorited", 0),
            "tags": [{"tag": t.get("tag")} for t in (steam_item.get("tags") or [])],
            "creator": steam_item.get("creator", ""),
            "author_name": steam_item.get("creator", ""),
            "is_collection": (steam_item.get("file_type") == 1),
            "children": children,
            "children_ids": children_ids,
            "children_total_count": len(children),
            "required_items": [],
            "is_fallback": False,
            "steam_url": (
                f"https://steamcommunity.com/sharedfiles/filedetails/?id="
                f"{steam_item.get('publishedfileid') or steam_item.get('id')}"
            ),
        }

    def _swd_to_detail(self, swd_item: dict) -> dict:
        """把 SWD 返回结构伪装成镜像站 detail 结构。"""
        children = []
        for c in swd_item.get("children") or []:
            cid = c.get("publishedfileid") or c.get("id")
            if cid:
                children.append(
                    {
                        "publishedfileid": str(cid),
                        "file_type": c.get("file_type", 0),
                    }
                )

        children_ids = [c["publishedfileid"] for c in children]
        required_items = [
            {"id": str(r)} if isinstance(r, str) else r
            for r in (swd_item.get("required_items") or [])
            if r
        ]

        return {
            "id": str(swd_item.get("publishedfileid") or swd_item.get("id") or ""),
            "title": swd_item.get("title", ""),
            "description": swd_item.get("description", ""),
            "preview": swd_item.get("preview_url", ""),
            "file_url": swd_item.get("file_url", ""),
            "file_size_human": _bytes_to_human(swd_item.get("file_size")),
            "subscriptions": swd_item.get("subscriptions", 0),
            "favorited": swd_item.get("favorited", 0),
            "tags": [{"tag": t} for t in (swd_item.get("tags") or [])],
            "creator": swd_item.get("creator", ""),
            "author_name": swd_item.get("creator_name", ""),
            "is_collection": (swd_item.get("file_type") == 1),
            "children": children,
            "children_ids": children_ids,
            "children_total_count": len(children),
            "required_items": required_items,
            "is_fallback": False,
            "steam_url": (
                f"https://steamcommunity.com/sharedfiles/filedetails/?id="
                f"{swd_item.get('publishedfileid') or swd_item.get('id')}"
            ),
        }


def _is_shell_detail(detail: dict) -> bool:
    """空壳判定（三源通用）：镜像站/SWD/Steam 对已删物品的残缺返回形态。

    实测形态：title 为空字符串 或 "Unknown"（含 is_fallback 标记）。
    任何一源返回壳数据都不得透传。
    """
    if not detail:
        return True
    if detail.get("is_fallback"):
        return True
    title = (detail.get("title") or "").strip()
    return not title or title == "Unknown"


def _bytes_to_human(size_bytes) -> str:
    if not size_bytes:
        return ""
    try:
        n = float(size_bytes)
        if n >= 1024 * 1024 * 1024:
            return f"{n / 1024 / 1024 / 1024:.2f} GB"
        if n >= 1024 * 1024:
            return f"{n / 1024 / 1024:.2f} MB"
        if n >= 1024:
            return f"{n / 1024:.2f} KB"
        return f"{n} B"
    except (ValueError, TypeError):
        return ""
