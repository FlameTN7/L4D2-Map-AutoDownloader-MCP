"""配置加载：config.json 优先，环境变量覆盖。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    workshop_dir: str = ""
    idle_dir: str = ""
    filter_json_path: str = ""
    steam_api_key_path: str = ""
    http_proxy: Optional[str] = None
    mirror_base: str = "https://zhrradiant.com/wp-json/l4d2-workshop/v1"
    swd_base: str = "https://steamworkshopdownloader.io"
    steam_api_base: str = "https://api.steampowered.com"
    echocn_url: str = "http://blog.echocn.cn:8090/"
    download_proxies: list[str] = field(default_factory=list)
    curated_refresh_interval_hours: int = 24
    request_timeout_seconds: int = 30
    download_timeout_seconds: int = 600
    download_max_retries: int = 3
    dedup_days: int = 75
    data_dir: str = ""

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        path = Path(
            config_path
            or os.environ.get("L4D2_CONFIG")
            or Path(__file__).parent / "config.json"
        )
        data: dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))

        env = os.environ
        cfg = cls(
            workshop_dir=(
                env.get("L4D2_WORKSHOP_DIR") or data.get("workshop_dir", "")
            ),
            idle_dir=data.get("idle_dir", ""),
            filter_json_path=(
                env.get("L4D2_FILTER_PATH") or data.get("filter_json_path", "")
            ),
            steam_api_key_path=data.get("steam_api_key_path", ""),
            http_proxy=(
                env.get("HTTPS_PROXY")
                or env.get("HTTP_PROXY")
                or env.get("http_proxy")
                or data.get("http_proxy")
            ),
            mirror_base=data.get("mirror_base", cls.mirror_base),
            swd_base=data.get("swd_base", cls.swd_base),
            steam_api_base=data.get("steam_api_base", cls.steam_api_base),
            echocn_url=data.get("echocn_url", cls.echocn_url),
            download_proxies=data.get(
                "download_proxies", ["https://accelerator.zhrradiantcos.top"]
            ),
            curated_refresh_interval_hours=int(
                data.get("curated_refresh_interval_hours", 24)
            ),
            request_timeout_seconds=int(data.get("request_timeout_seconds", 30)),
            download_timeout_seconds=int(data.get("download_timeout_seconds", 600)),
            download_max_retries=int(data.get("download_max_retries", 3)),
            dedup_days=int(data.get("dedup_days", 75)),
            data_dir=(
                env.get("L4D2_DATA_DIR") or data.get("data_dir", "")
                or str(Path(__file__).parent / "data")
            ),
        )
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        return cfg

    @property
    def steam_api_key(self) -> Optional[str]:
        p = Path(self.steam_api_key_path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return (
                data.get("key")
                or data.get("api_key")
                or data.get("steam_api_key")  # 老配置文件字段名
            )
        except Exception:
            return None

    @property
    def proxies(self) -> Optional[dict]:
        if not self.http_proxy:
            return None
        return {"http": self.http_proxy, "https": self.http_proxy}

    def curated_cache_path(self) -> Path:
        return Path(self.data_dir) / "curated.json"

    def curated_meta_path(self) -> Path:
        return Path(self.data_dir) / "curated.meta.json"


_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def set_config(cfg: Config) -> None:
    global _config
    _config = cfg
