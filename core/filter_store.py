"""filter.json 状态库读写 + eligibility 判断。

设计见 ARCHITECTURE.md §3.3 (check_eligibility 工具) 和 §6.1。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def _sanitize_title(s: str) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[（）\(\)\[\]\【\】.,;:'\"!?\\/_\-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


class FilterStore:
    def __init__(self, filter_path: str, dedup_days: int = 75):
        self.path = Path(filter_path)
        self.dedup_days = dedup_days
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {
                "updated_at": datetime.now().isoformat(),
                "blacklist": [],
                "dedup_75days": [],
            }
            return self._cache
        self._cache = json.loads(
            self.path.read_text(encoding="utf-8")
        )
        return self._cache

    def _flush(self) -> None:
        assert self._cache is not None
        self._cache["updated_at"] = datetime.now().isoformat(
            timespec="seconds"
        )
        # filter.json 父目录可能不存在（无 filter 部署场景），必须创建
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def check_eligibility(
        self,
        workshop_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> dict:
        """检查单张图是否可下载。零网络调用。"""
        data = self._load()
        blacklist = data.get("blacklist") or []
        dedup = data.get("dedup_75days") or []

        if workshop_id:
            wid_str = str(workshop_id)
            for entry in dedup:
                if entry.get("workshop_id") == wid_str and wid_str:
                    return self._entry_to_result(entry)

        if title:
            title_clean = _sanitize_title(title)
            for bl in blacklist:
                if _sanitize_title(bl) == title_clean or title_clean in _sanitize_title(bl) or _sanitize_title(bl) in title_clean:
                    return {
                        "state": "blacklisted",
                        "days_remaining": None,
                        "source": f"blacklist: {bl}",
                    }
            for entry in dedup:
                if _sanitize_title(entry.get("map", "")) == title_clean:
                    return self._entry_to_result(entry)

        return {
            "state": "allowed",
            "days_remaining": None,
            "source": None,
        }

    def _entry_to_result(self, entry: dict) -> dict:
        status = entry.get("status", "active")
        if status == "expired":
            return {
                "state": "dedup_expired",
                "days_remaining": 0,
                "source": entry.get("source", ""),
            }
        added = entry.get("added")
        expires = entry.get("expires")
        days_remaining = None
        if expires:
            try:
                exp_date = datetime.fromisoformat(expires)
                days_remaining = max(
                    0, (exp_date - datetime.now()).days
                )
            except (ValueError, TypeError):
                pass
        return {
            "state": "dedup_active",
            "days_remaining": days_remaining,
            "source": entry.get("source", ""),
        }

    def add_downloaded(
        self,
        title: str,
        workshop_id: str,
        source: str = "downloaded",
    ) -> dict:
        """添加到 dedup 名单（已存在则更新）。"""
        with self._lock:
            data = self._load()
            dedup = data.setdefault("dedup_75days", [])
            now = datetime.now()
            expires = now + timedelta(days=self.dedup_days)
            wid_str = str(workshop_id) if workshop_id else ""

            for entry in dedup:
                existing_title = _sanitize_title(entry.get("map", ""))
                if (
                    (wid_str and entry.get("workshop_id") == wid_str)
                    or existing_title == _sanitize_title(title)
                ):
                    entry["map"] = title
                    if wid_str:
                        entry["workshop_id"] = wid_str
                    entry["added"] = now.date().isoformat()
                    entry["expires"] = expires.date().isoformat()
                    entry["status"] = "active"
                    entry["source"] = source
                    self._flush()
                    return entry

            new_entry = {
                "map": title,
                "workshop_id": wid_str,
                "added": now.date().isoformat(),
                "expires": expires.date().isoformat(),
                "status": "active",
                "source": source,
            }
            dedup.append(new_entry)
            self._flush()
            return new_entry

    def list_recent(self, limit: int = 10, source_filter: Optional[str] = None) -> list[dict]:
        data = self._load()
        dedup = list(data.get("dedup_75days") or [])
        if source_filter:
            dedup = [e for e in dedup if e.get("source") == source_filter]
        dedup.sort(
            key=lambda e: e.get("added", ""),
            reverse=True,
        )
        return dedup[:limit]

    def add_blacklist(self, title: str) -> None:
        with self._lock:
            data = self._load()
            bl = data.setdefault("blacklist", [])
            if title not in bl:
                bl.append(title)
                self._flush()
