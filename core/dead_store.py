"""死图缓存：镜像站/Steam 确认不可用的工坊物品。

设计见生产反馈（2026-08）：镜像站索引陈旧，部分物品在 Steam 只剩空壳，
反复推荐/请求是浪费。缓存到 data/dead_maps.json，TTL 30 天防 Steam 恢复。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)

DEAD_TTL_DAYS = 30


class DeadStore:
    def __init__(self, cfg: Config):
        self.path = Path(cfg.data_dir) / "dead_maps.json"
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._cache = json.loads(
                    self.path.read_text(encoding="utf-8")
                )
        except Exception as e:
            logger.warning(f"dead cache load failed: {e}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.warning(f"dead cache save failed: {e}")

    def is_dead(self, workshop_id: str) -> Optional[dict]:
        """返回死图记录（未过期）或 None。"""
        if not workshop_id:
            return None
        with self._lock:
            rec = self._cache.get(str(workshop_id))
        if not rec:
            return None
        try:
            detected = datetime.fromisoformat(rec["detected_at"])
            if datetime.now() - detected > timedelta(days=DEAD_TTL_DAYS):
                return None
        except (ValueError, KeyError, TypeError):
            pass
        return dict(rec)

    def mark_dead(self, workshop_id: str, reason: str) -> None:
        with self._lock:
            self._cache[str(workshop_id)] = {
                "detected_at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
            }
            self._save()
