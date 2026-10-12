"""filter_store 资格判断单测（黑名单/去重）。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.filter_store import FilterStore


@pytest.fixture
def clock(monkeypatch):
    class FrozenDatetime(datetime):
        value = datetime(2026, 7, 15, 12)

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr("core.filter_store.datetime", FrozenDatetime)
    return FrozenDatetime


def _make_filter(tmp_path: Path, blacklist=None, dedup=None) -> FilterStore:
    data = {
        "updated_at": "",
        "blacklist": blacklist or [],
        "dedup_75days": dedup or [],
    }
    p = tmp_path / "filter.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return FilterStore(str(p), dedup_days=75)


def test_blacklist_by_title(tmp_path):
    flt = _make_filter(tmp_path, blacklist=["Death Town", "Jade Helm - (backrooms)"])
    assert flt.check_eligibility(title="Death Town")["state"] == "blacklisted"
    # 子串双向匹配
    assert flt.check_eligibility(title="Death Town (fixed)")["state"] == "blacklisted"
    assert flt.check_eligibility(title="death town")["state"] == "blacklisted"


def test_blacklist_by_title_with_id(tmp_path):
    """BUG A 回归：install_campaign 必须先拿标题再查，只传 id 会漏掉标题黑名单。"""
    flt = _make_filter(tmp_path, blacklist=["Death Town"])
    r_id_only = flt.check_eligibility(workshop_id="2006914715")
    assert r_id_only["state"] == "allowed"
    r_full = flt.check_eligibility(workshop_id="2006914715", title="Death Town")
    assert r_full["state"] == "blacklisted"


def test_dedup_by_workshop_id(tmp_path, clock):
    flt = _make_filter(
        tmp_path,
        dedup=[{
            "map": "Old Map",
            "workshop_id": "1111111111",
            "added": "2026-07-01",
            "expires": "2026-09-14",
            "status": "active",
            "source": "downloaded",
        }]
    )
    r = flt.check_eligibility(workshop_id="1111111111")
    assert r["state"] == "dedup_active"
    assert r["days_remaining"] >= 0
    assert flt.check_eligibility(workshop_id="2222222222")["state"] == "allowed"


def test_dedup_expired(tmp_path):
    flt = _make_filter(
        tmp_path,
        dedup=[{
            "map": "Old",
            "workshop_id": "1111111111",
            "added": "2026-01-01",
            "expires": "2026-02-01",
            "status": "expired",
            "source": "downloaded",
        }]
    )
    assert flt.check_eligibility(workshop_id="1111111111")["state"] == "dedup_expired"


def test_add_downloaded_then_blocked(tmp_path):
    p = tmp_path / "filter.json"
    p.write_text(json.dumps({"blacklist": [], "dedup_75days": []}), encoding="utf-8")
    flt = FilterStore(str(p), dedup_days=75)
    flt.add_downloaded("Death Town", "2006914715", source="downloaded")
    assert flt.check_eligibility(title="Death Town")["state"] == "dedup_active"
    assert flt.check_eligibility(workshop_id="2006914715")["state"] == "dedup_active"
    # 幂等：重复添加不产生重复条目
    flt.add_downloaded("Death Town", "2006914715", source="downloaded")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert len(data["dedup_75days"]) == 1


def test_downloaded_entry_expires_without_status_rewrite(tmp_path, clock):
    flt = FilterStore(str(tmp_path / "filter.json"), dedup_days=75)
    flt.add_downloaded("Old Map", "1111111111")
    assert flt.check_eligibility(workshop_id="1111111111")["state"] == "dedup_active"

    clock.value += timedelta(days=76)
    for store in (flt, FilterStore(str(flt.path))):
        for query in ({"workshop_id": "1111111111"}, {"title": "Old Map"}):
            result = store.check_eligibility(**query)
            assert result["state"] == "dedup_expired"
            assert result["days_remaining"] == 0


@pytest.mark.parametrize("expires,state", [
    ("2026-07-15", "dedup_expired"),
    ("2026-07-15T12:00:00", "dedup_expired"),
    ("2026-07-15T12:00:01", "dedup_active"),
    ("2026-07-16", "dedup_active"),
    ("invalid-date", "dedup_active"),
    (None, "dedup_active"),
])
def test_expiry_boundary_and_unknown_date(tmp_path, clock, expires, state):
    flt = _make_filter(tmp_path, dedup=[{
        "map": "Old Map", "workshop_id": "1111111111",
        "expires": expires, "status": "active", "source": "downloaded",
    }])
    assert flt.check_eligibility(workshop_id="1111111111")["state"] == state
