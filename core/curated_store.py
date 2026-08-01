"""echocn 人工策展名单缓存（启动加载 + 每日刷新）。

设计见 ARCHITECTURE.md §4.3。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from config import Config

logger = logging.getLogger(__name__)

try:
    import zhconv

    def _norm_text(s: str) -> str:
        return zhconv.convert((s or "").lower(), "zh-cn").strip()
except ImportError:
    def _norm_text(s: str) -> str:
        return (s or "").lower().strip()


# 模糊匹配阈值：低于此相似度的标题不算命中。
# 0.6 实测校准：排除 "死亡小镇"↔"死亡之地"（0.5，会误下错图），
# 保留 "Korkiri"↔"Korkiri Forest"（0.67）这类拼写纠错。
FUZZY_SCORE_MIN = 0.6


WORKSHOP_ID_RE = re.compile(r"id=(\d+)")


class CuratedEntry:
    __slots__ = (
        "title_zh", "title_original", "version", "workshop_id",
        "link_type", "link_url", "uploaded_at", "uploader",
        "file_size_human", "tags", "chapters",
    )

    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k, ""))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


class CuratedStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._entries: list[CuratedEntry] = []
        self._meta: dict = {"last_fetch": "", "sha256": "", "total": 0}
        self._stop_event = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None

    def start(self, force_refresh: bool = False) -> None:
        if self._load_cache() and not force_refresh:
            self._schedule_refresh()
            return
        self.refresh_blocking()
        self._schedule_refresh()

    def stop(self) -> None:
        self._stop_event.set()

    def _load_cache(self) -> bool:
        cache_path = self.cfg.curated_cache_path()
        meta_path = self.cfg.curated_meta_path()
        if not cache_path.exists() or not meta_path.exists():
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            last = meta.get("last_fetch", "")
            if last:
                last_dt = datetime.fromisoformat(last)
                age_hours = (datetime.now() - last_dt).total_seconds() / 3600
                if age_hours > self.cfg.curated_refresh_interval_hours * 2:
                    logger.info(
                        f"Curated cache stale ({age_hours:.1f}h), will refresh"
                    )
                    return False
            self._entries = [
                CuratedEntry(**e)
                for e in json.loads(cache_path.read_text(encoding="utf-8"))
            ]
            self._meta = meta
            logger.info(
                f"Curated cache loaded: {len(self._entries)} entries"
            )
            return True
        except Exception as e:
            logger.warning(f"Curated cache load failed: {e}")
            return False

    def refresh_blocking(self) -> dict:
        try:
            html = self._fetch_html()
            entries = self._parse_html(html)
            sha = hashlib.sha256(
                json.dumps([e.to_dict() for e in entries],
                           ensure_ascii=False,
                           sort_keys=True).encode("utf-8")
            ).hexdigest()
            with self._lock:
                self._entries = entries
                self._meta = {
                    "last_fetch": datetime.now().isoformat(timespec="seconds"),
                    "sha256": sha,
                    "total": len(entries),
                }
            self._save_cache()
            logger.info(
                f"Curated list refreshed: {len(entries)} entries, sha={sha[:8]}"
            )
            return self._meta.copy()
        except Exception as e:
            logger.error(f"Curated refresh failed: {e}")
            return {
                "last_fetch": self._meta.get("last_fetch", ""),
                "sha256": self._meta.get("sha256", ""),
                "total": len(self._entries),
                "error": str(e),
            }

    def _fetch_html(self) -> str:
        session = requests.Session()
        if self.cfg.proxies:
            session.proxies.update(self.cfg.proxies)
        session.headers.update(
            {"User-Agent": "Mozilla/5.0 l4d2-mcp/0.1"}
        )
        r = session.get(
            self.cfg.echocn_url, timeout=self.cfg.request_timeout_seconds
        )
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text

    def _parse_html(self, html: str) -> list[CuratedEntry]:
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("table#mapTable tbody tr")
        entries: list[CuratedEntry] = []
        for row in rows:
            try:
                entry = self._parse_row(row)
                if entry:
                    entries.append(entry)
            except Exception as e:
                logger.debug(f"row parse skip: {e}")
        return entries

    def _parse_row(self, row) -> Optional[CuratedEntry]:
        cells = row.find_all("td")
        if len(cells) < 7:
            return None

        title_zh = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        title_original = ""
        if len(cells) > 2:
            name_cell = cells[2]
            title_original = name_cell.get_text(strip=True)

        link = ""
        link_type = "none"
        if len(cells) > 4:
            anchor = cells[4].find("a")
            if anchor:
                link = anchor.get("href", "")
                if "steamcommunity.com" in link:
                    link_type = "steam"
                elif "gamemaps.com" in link:
                    link_type = "gamemaps"
                else:
                    link_type = "other"

        workshop_id = ""
        if link_type == "steam":
            m = WORKSHOP_ID_RE.search(link)
            if m:
                workshop_id = m.group(1)

        uploaded_at = cells[5].get_text(strip=True) if len(cells) > 5 else ""
        uploader = cells[6].get_text(strip=True) if len(cells) > 6 else ""

        file_size_human = ""
        for comment in row.find_all(string=lambda t: isinstance(t, type(row.find(string=True)))):
            txt = str(comment).strip()
            m = re.search(r"(\d+(?:\.\d+)?\s*(?:Mb|Gb|Kb))", txt, re.IGNORECASE)
            if m:
                file_size_human = m.group(1)
                break

        tags: list[str] = []
        tag_idx = 7
        if len(cells) > tag_idx:
            for tag_span in cells[tag_idx].select("span.tag"):
                cls = " ".join(tag_span.get("class", []))
                m = re.search(r"tag-(\S+)", cls)
                if m:
                    tags.append(m.group(1))

        chapters: list[str] = []
        chapter_idx = 8
        if len(cells) > chapter_idx:
            chapter_container = cells[chapter_idx].find(
                class_="chapter-full"
            ) or cells[chapter_idx].find(class_="search-data")
            if chapter_container:
                for li in chapter_container.find_all("li"):
                    chapters.append(li.get_text(strip=True))

        return CuratedEntry(
            title_zh=title_zh,
            title_original=title_original,
            link_type=link_type,
            link_url=link,
            workshop_id=workshop_id,
            uploaded_at=uploaded_at,
            uploader=uploader,
            file_size_human=file_size_human,
            tags=tags,
            chapters=chapters,
        )

    def _save_cache(self) -> None:
        self.cfg.curated_cache_path().write_text(
            json.dumps(
                [e.to_dict() for e in self._entries],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.cfg.curated_meta_path().write_text(
            json.dumps(self._meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _schedule_refresh(self) -> None:
        interval = self.cfg.curated_refresh_interval_hours * 3600

        def _loop():
            while not self._stop_event.wait(interval):
                try:
                    self.refresh_blocking()
                except Exception as e:
                    logger.warning(f"scheduled refresh error: {e}")

        self._refresh_thread = threading.Thread(
            target=_loop, name="curated-refresh", daemon=True
        )
        self._refresh_thread.start()

    def fuzzy_search(self, keyword: str, limit: int = 20) -> list[tuple["CuratedEntry", float]]:
        """模糊搜索，返回 [(entry, score)] 按相似度降序。供标题解析链使用。"""
        kw = _norm_text(keyword)
        if not kw:
            return []
        with self._lock:
            entries = list(self._entries)

        def _fuzzy_score(e: CuratedEntry) -> float:
            fields = [
                _norm_text(e.title_zh),
                _norm_text(e.title_original),
                " ".join(_norm_text(c) for c in e.chapters),
            ]
            for f in fields:
                if not f:
                    continue  # 空字段 "" 是任意字符串的子串，必须跳过
                if kw in f or f in kw:
                    return 1.0
            return max(
                SequenceMatcher(None, kw, f).ratio() for f in fields if f
            ) if any(fields) else 0.0

        scored = []
        for e in entries:
            s = _fuzzy_score(e)
            if s >= FUZZY_SCORE_MIN:
                scored.append((e, s))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    def query(
        self,
        keyword: str = "",
        tag: str = "",
        limit: int = 20,
    ) -> tuple[list[dict], str]:
        """查询策展名单。keyword 为空时全部；非空时精确子串优先，模糊匹配兜底。"""
        with self._lock:
            meta_fetch = self._meta.get("last_fetch", "")

        tag_l = (tag or "").lower().strip()
        if keyword:
            entries = [
                e
                for e, _ in self.fuzzy_search(keyword, limit=limit)
                if (not tag_l) or any(t.lower() == tag_l for t in e.tags)
            ]
            return (
                [e.to_dict() for e in entries[:limit]],
                meta_fetch,
            )

        def _match(e: CuratedEntry) -> bool:
            if tag_l:
                return any(t.lower() == tag_l for t in e.tags)
            return True

        with self._lock:
            filtered = [e for e in self._entries if _match(e)]
        filtered.sort(
            key=lambda e: e.uploaded_at or "",
            reverse=True,
        )
        return (
            [e.to_dict() for e in filtered[:limit]],
            meta_fetch,
        )

    def find_by_workshop_id(self, workshop_id: str) -> Optional[CuratedEntry]:
        if not workshop_id:
            return None
        wid = str(workshop_id)
        with self._lock:
            for e in self._entries:
                if e.workshop_id == wid:
                    return e
        return None

    def find_by_title(self, title: str) -> Optional[CuratedEntry]:
        if not title:
            return None
        target = title.lower().strip()
        with self._lock:
            for e in self._entries:
                if (
                    e.title_zh.lower() == target
                    or e.title_original.lower() == target
                ):
                    return e
        with self._lock:
            for e in self._entries:
                if (
                    target in e.title_zh.lower()
                    or target in e.title_original.lower()
                ):
                    return e
        return None

    def stats(self) -> dict:
        with self._lock:
            return {
                "total": len(self._entries),
                "last_fetch": self._meta.get("last_fetch", ""),
                "sha256": self._meta.get("sha256", ""),
            }
