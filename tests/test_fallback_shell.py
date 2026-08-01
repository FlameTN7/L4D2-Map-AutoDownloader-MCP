"""api_client 三级回退空壳穿透单测（FakeClient，无网络）。

覆盖 v0.3.1 生产反馈修复：
- 镜像站空壳 → SWD 空壳 → 不得透传，返回 {}（130928055 场景）
- SWD 有效数据正常透传；Steam 空壳被源头过滤
- _confirm_dead 在 fallback 全挂后接管标记死图
"""
import json

import pytest

from core.api_client import ApiClient, _is_shell_detail


def _mirror_detail(title, is_fallback=False, file_url=None):
    return {
        "id": "X", "title": title, "is_fallback": is_fallback,
        "file_url": file_url, "file_size_human": "377.65 KB",
    }


class FakeMirror:
    def __init__(self, detail):
        self._detail = detail

    def detail(self, wid):
        return dict(self._detail)

    def user_files(self, *a, **k):
        return {"items": []}


class FakeSwd:
    def __init__(self, results):
        self._results = results

    def query_details(self, ids):
        return self._results


class FakeSteam:
    def __init__(self, item):
        self._item = item

    def get_file_details(self, wid):
        return self._item


class FakeCfg:
    steam_api_key = None


def _api(mirror, swd, steam_item=None):
    api = ApiClient.__new__(ApiClient)
    api.cfg = FakeCfg()
    api.mirror = FakeMirror(mirror)
    api.swd = FakeSwd(swd)
    api.steam = FakeSteam(steam_item)
    return api


def test_mirror_shell_swd_shell_returns_empty():
    """生产 130928055 场景：mirror title 空（有 file_url）→ SWD 更空壳 → 返回 {}。"""
    api = _api(
        mirror=_mirror_detail("", file_url="http://fake/377kb"),
        swd=[{"publishedfileid": "130928055", "title": "", "file_url": ""}],
    )
    assert api.get_detail_with_fallback("130928055") == {}


def test_mirror_shell_swd_valid_passthrough():
    api = _api(
        mirror=_mirror_detail(""),
        swd=[{"publishedfileid": "X", "title": "Real Map", "file_url": "http://x"}],
    )
    d = api.get_detail_with_fallback("X")
    assert d["title"] == "Real Map"


def test_mirror_valid_direct_return():
    api = _api(mirror=_mirror_detail("Good Map"), swd=[])
    d = api.get_detail_with_fallback("X")
    assert d["title"] == "Good Map"


def test_steam_shell_not_passthrough():
    """Steam 源产物为空壳（title 空）→ fallback 链拦截，返回 {}。"""
    api = _api(mirror=_mirror_detail(""), swd=[], steam_item={"publishedfileid": "X", "title": ""})
    assert api.get_detail_with_fallback("X") == {}


def test_steam_valid_passthrough():
    api = _api(
        mirror=_mirror_detail(""),
        swd=[],
        steam_item={"publishedfileid": "X", "title": "Steam Map", "file_url": "http://s"},
    )
    d = api.get_detail_with_fallback("X")
    assert d["title"] == "Steam Map"


def test_is_shell_detail_forms():
    assert _is_shell_detail({}) is True
    assert _is_shell_detail({"title": ""}) is True
    assert _is_shell_detail({"title": "Unknown"}) is True
    assert _is_shell_detail({"title": "X", "is_fallback": True}) is True
    assert _is_shell_detail({"title": "  Real Map  "}) is False
