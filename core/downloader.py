"""vpk 下载（三级容错 + 文件校验）。

设计见 ARCHITECTURE.md §3.2 第⑥步 和 §5.2。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from config import Config

logger = logging.getLogger(__name__)


# per-path 锁：AstrBot 并发调用 install_campaign 时防同名文件双写
_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


VPK_MAGIC_V1 = b"\x34\x12\xaa\x55"
VPK_MAGIC_V2 = b"\x35\x12\xaa\x55"


def sanitize_filename(title: str) -> str:
    """净化文件名。规则：
    - 主文件名内的 . 替换为 _
    - 非法字符 <>:"/\\|?* 替换为空格
    - 合并多余空白
    - 末尾加 .vpk
    """
    if not title:
        return "unknown.vpk"
    name = title.replace(".", "_")
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, " ")
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(". ")
    return (name or "unknown") + ".vpk"


def validate_vpk(file_path: Path) -> tuple[bool, str]:
    """VPK 文件基础校验。"""
    try:
        size = file_path.stat().st_size
    except OSError:
        return False, "文件不存在"
    if size < 1_000_000:
        return False, f"文件过小 ({size} B)，疑似非真实 vpk"
    try:
        with open(file_path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        return False, f"读取失败: {e}"
    if magic not in (VPK_MAGIC_V1, VPK_MAGIC_V2):
        return False, f"VPK 魔数不匹配: {magic.hex()}"
    return True, "ok"


class Downloader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.timeout = cfg.download_timeout_seconds
        self.max_retries = cfg.download_max_retries

    def download(
        self,
        url: str,
        save_path: Path,
        referer: str = "https://zhrradiant.com/tools/l4d2-workshop",
    ) -> dict:
        """三级容错下载。

        返回 {success, saved_path, size_mb, line_used, attempts: [...]}
        """
        with _lock_for(save_path):
            return self._download_locked(url, save_path, referer)

    def _download_locked(
        self,
        url: str,
        save_path: Path,
        referer: str = "https://zhrradiant.com/tools/l4d2-workshop",
    ) -> dict:
        attempts: list[dict] = []
        save_path.parent.mkdir(parents=True, exist_ok=True)

        proxies = list(self.cfg.download_proxies)
        for proxy_base in proxies:
            attempt_info = {"line": f"proxy:{proxy_base}", "ok": False}
            try:
                proxy_url = f"{proxy_base}?url={quote(url, safe='')}"
                ok, msg, size = self._download_one(
                    proxy_url, save_path, referer=referer
                )
                attempt_info.update({"ok": ok, "msg": msg, "size_mb": size})
                if ok:
                    attempts.append(attempt_info)
                    return self._success(save_path, size, attempt_info["line"], attempts)
            except Exception as e:
                attempt_info["msg"] = str(e)
            attempts.append(attempt_info)
            if save_path.exists():
                try:
                    save_path.unlink()
                except OSError:
                    pass

        for attempt in range(self.max_retries):
            attempt_info = {
                "line": f"direct:retry{attempt + 1}",
                "ok": False,
            }
            try:
                ok, msg, size = self._download_one(url, save_path)
                attempt_info.update({"ok": ok, "msg": msg, "size_mb": size})
                if ok:
                    attempts.append(attempt_info)
                    return self._success(
                        save_path, size, attempt_info["line"], attempts
                    )
            except Exception as e:
                attempt_info["msg"] = str(e)
            attempts.append(attempt_info)
            if save_path.exists():
                try:
                    save_path.unlink()
                except OSError:
                    pass
            time.sleep((attempt + 1) * 2)

        return {
            "success": False,
            "saved_path": None,
            "size_mb": 0,
            "line_used": None,
            "attempts": attempts,
        }

    def _download_one(
        self,
        url: str,
        save_path: Path,
        referer: Optional[str] = None,
    ) -> tuple[bool, str, float]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            ),
        }
        if referer:
            headers["Referer"] = referer

        with requests.get(
            url, headers=headers, stream=True, timeout=self.timeout
        ) as r:
            r.raise_for_status()
            tmp = save_path.with_suffix(save_path.suffix + ".tmp")
            total = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
            os.replace(tmp, save_path)

        ok, msg = validate_vpk(save_path)
        if not ok:
            try:
                save_path.unlink()
            except OSError:
                pass
            return False, f"validation failed: {msg}", total / 1024 / 1024

        return True, "ok", total / 1024 / 1024

    def _success(
        self, path: Path, size_mb: float, line: str, attempts: list
    ) -> dict:
        return {
            "success": True,
            "saved_path": str(path),
            "size_mb": round(size_mb, 2),
            "line_used": line,
            "attempts": attempts,
        }
