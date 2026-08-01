"""curated_store 查询/模糊/死图单测（纯本地，不联网）。"""
import json
import tempfile
import types
from pathlib import Path

from core.curated_store import CuratedEntry, CuratedStore
from core.dead_store import DeadStore
from config import Config


def _make_entries():
    return [
        CuratedEntry(
            title_zh="死亡之地", title_original="Field of the Dead",
            workshop_id="2968601720", tags=["高质量"], chapters=["章节 1: a1"],
        ),
        CuratedEntry(
            title_zh="哥奇利森林（Kokiri Forest）", title_original="Korkiri Forest",
            workshop_id="3422832583", tags=["解密"], chapters=["章节 1: oot_forest"],
        ),
        CuratedEntry(
            title_zh="超越死亡", title_original="Beyond Dead",
            workshop_id="2279448617", tags=["高难度"],
        ),
        CuratedEntry(
            title_zh="Blood Proof", title_original="Blood Proof",
            workshop_id="999000111", tags=["推荐"],
        ),
    ]


def _make_store(tmp_path: Path) -> CuratedStore:
    cfg = Config(data_dir=str(tmp_path))
    store = CuratedStore(cfg)
    store._entries = _make_entries()
    store._meta = {"last_fetch": "2026-08-01T00:00:00", "total": 4, "sha256": ""}
    return store


def test_query_with_keyword_no_crash(tmp_path):
    """A2 回归：query() 带 keyword 时解包顺序必须正确（曾必崩）。"""
    store = _make_store(tmp_path)
    items, _ = store.query(keyword="Blood Proof", limit=5)
    assert items, "应命中 Blood Proof"
    assert items[0]["workshop_id"] == "999000111"
    assert isinstance(items[0], dict)
    assert "title_zh" in items[0]


def test_query_with_keyword_and_tag(tmp_path):
    store = _make_store(tmp_path)
    items, _ = store.query(keyword="森林", tag="解密", limit=5)
    assert len(items) == 1
    assert items[0]["workshop_id"] == "3422832583"


def test_fuzzy_typo(tmp_path):
    store = _make_store(tmp_path)
    items, _ = store.query(keyword="Korkiri", limit=5)
    assert items[0]["workshop_id"] == "3422832583"


def test_fuzzy_weak_ratio_excluded(tmp_path):
    """0.6 阈值：死亡小镇 ↔ 死亡之地（0.5）应排除。"""
    store = _make_store(tmp_path)
    items, _ = store.query(keyword="死亡小镇", limit=5)
    ids = [i["workshop_id"] for i in items]
    assert "2968601720" not in ids


def test_empty_chapters_do_not_match_everything(tmp_path):
    """回归：空 chapters 字段（"" 是任意字符串子串）不得让无关条目得 1.0。"""
    cfg = Config(data_dir=str(tmp_path))
    store = CuratedStore(cfg)
    store._entries = [
        CuratedEntry(
            title_zh="超越死亡", title_original="Beyond Dead",
            workshop_id="2279448617", tags=[], chapters=[],
        ),
    ]
    hits = store.fuzzy_search("blood proof")
    assert hits == []


def test_query_empty_keyword_returns_all(tmp_path):
    store = _make_store(tmp_path)
    items, _ = store.query(keyword="", limit=10)
    assert len(items) == 4


def test_dead_store_roundtrip(tmp_path):
    ds = DeadStore(Config(data_dir=str(tmp_path)))
    assert ds.is_dead("130928055") is None
    ds.mark_dead("130928055", "Steam 已删除或私密")
    rec = ds.is_dead("130928055")
    assert rec is not None
    assert "Steam" in rec["reason"]

    # 重新加载（持久化）
    ds2 = DeadStore(Config(data_dir=str(tmp_path)))
    assert ds2.is_dead("130928055") is not None


class _FakeSteamCfg:
    steam_api_key = None


class _FakeSteamWithKey:
    def __init__(self, result):
        self.cfg = _FakeSteamCfg()
        self.cfg.steam_api_key = "TEST_KEY"
        self._result = result

    def get_file_details(self, wid):
        return self._result


class _FakeSteamNoKey:
    cfg = _FakeSteamCfg()


def _fake_api(steam):
    api = types.SimpleNamespace(steam=steam)
    return api


def test_confirm_dead_no_key_shell_marks(tmp_path):
    """无 key：mirror 空壳（title 空）→ 保守标记死图。"""
    import server
    from core.dead_store import DeadStore

    ds = DeadStore(Config(data_dir=str(tmp_path)))
    api = _fake_api(_FakeSteamNoKey())
    rec = server._confirm_dead(
        api, ds, "130928055",
        {"id": "130928055", "title": "", "is_fallback": False},
    )
    assert rec is not None
    assert ds.is_dead("130928055") is not None


def test_confirm_dead_with_key_valid_not_marked(tmp_path):
    """有 key 且 Steam 返回有效数据 → 不标记。"""
    import server
    from core.dead_store import DeadStore

    ds = DeadStore(Config(data_dir=str(tmp_path)))
    api = _fake_api(_FakeSteamWithKey({"publishedfileid": "X", "title": "Real"}))
    rec = server._confirm_dead(
        api, ds, "X",
        {"id": "X", "title": "", "is_fallback": False},
    )
    assert rec is None
    assert ds.is_dead("X") is None


def test_confirm_dead_with_key_missing_marks(tmp_path):
    """有 key 且 Steam 确认不存在（result!=1 → None）→ 标记死图。"""
    import server
    from core.dead_store import DeadStore

    ds = DeadStore(Config(data_dir=str(tmp_path)))
    api = _fake_api(_FakeSteamWithKey(None))
    rec = server._confirm_dead(
        api, ds, "130928055",
        {"id": "130928055", "title": "", "is_fallback": False},
    )
    assert rec is not None
    assert "Steam 已删除" in rec["reason"]


def test_confirm_dead_normal_detail_noop(tmp_path):
    """镜像站返回有效数据（非空壳）→ 不触发检测。"""
    import server
    from core.dead_store import DeadStore

    ds = DeadStore(Config(data_dir=str(tmp_path)))
    api = _fake_api(_FakeSteamNoKey())
    rec = server._confirm_dead(
        api, ds, "3774952433",
        {"id": "3774952433", "title": "Red Square (Campaign)", "is_fallback": False},
    )
    assert rec is None
    assert ds.is_dead("3774952433") is None
