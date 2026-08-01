"""check_eligibility 死图预检 + 无 filter 目录场景单测。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from core.filter_store import FilterStore  # noqa: E402


@pytest.fixture
def init_server(tmp_path, monkeypatch):
    """重置 server 全局并指向临时目录初始化（免网络：patch curated.start）。"""
    for attr in ("_cfg", "_api", "_filter", "_curated", "_downloader", "_dead"):
        monkeypatch.setattr(server, attr, None)
    monkeypatch.setenv("L4D2_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("L4D2_FILTER_PATH", str(tmp_path / "filter.json"))
    monkeypatch.setattr(server.CuratedStore, "start", lambda self: None)
    cfg, api, flt, curated, dl, dead = server._ensure_init()
    yield cfg, api, flt, curated, dl, dead


def test_eligibility_dead_state(init_server, tmp_path):
    """A: 死图缓存命中 → check_eligibility 返回 state=dead。"""
    cfg, api, flt, curated, dl, dead = init_server
    dead.mark_dead("130928055", "Steam 已删除或私密")
    r = server.check_eligibility(workshop_id="130928055")
    assert r["state"] == "dead"
    assert "Steam" in r["source"]


def test_eligibility_normal_allowed(init_server, tmp_path):
    cfg, api, flt, curated, dl, dead = init_server
    r = server.check_eligibility(workshop_id="3774952433")
    assert r["state"] == "allowed"


def test_eligibility_title_not_affected_by_dead(init_server, tmp_path):
    """死图缓存无标题索引：仅 title 查询不因死图缓存改变结果。"""
    cfg, api, flt, curated, dl, dead = init_server
    dead.mark_dead("130928055", "Steam 已删除或私密")
    r = server.check_eligibility(title="死亡前夜2")
    assert r["state"] == "allowed"


def test_eligibility_no_input(init_server, tmp_path):
    cfg, api, flt, curated, dl, dead = init_server
    assert server.check_eligibility()["state"] == "not_found"


def test_filter_flush_creates_parent_dir(tmp_path):
    """B: filter.json 父目录不存在时 add_downloaded 应创建目录并落盘。"""
    p = tmp_path / "nonexistent" / "sub" / "filter.json"
    flt = FilterStore(str(p), dedup_days=75)
    assert flt.check_eligibility(workshop_id="1")["state"] == "allowed"
    flt.add_downloaded("Death Town", "2006914715", source="downloaded")
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert len(data["dedup_75days"]) == 1
    assert flt.check_eligibility(workshop_id="2006914715")["state"] == "dedup_active"
