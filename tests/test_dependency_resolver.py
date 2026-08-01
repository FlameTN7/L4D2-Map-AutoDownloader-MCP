"""dependency_resolver 依赖/系列解析单测（FakeApi，无网络）。

覆盖：
- E-2: find_series_parts 正向路径必须用 required_items（场景 B：Part1 标了 [P2,P3]）
- C:   反向查询语言域修正（/user-files 英文标题 → /detail 中文标题）
- E-1: resolve_dependencies_union 从多根并集（只有 part1 标依赖时也能捕获）
- 裸 "2/5" 命名（无 part 字样）也能反查配对
"""
import pytest

from core.dependency_resolver import (
    find_series_parts,
    resolve_dependencies_union,
)


def _detail(pid, title, required=None, desc="", creator="76561198000000001",
            is_collection=False, children_ids=None, file_url="http://cdn/x.vpk",
            file_size_human="100 MB"):
    return {
        "id": pid, "title": title, "description": desc,
        "required_items": [{"id": str(r)} for r in (required or [])],
        "children_ids": children_ids or [], "children": [],
        "is_collection": is_collection, "creator": creator,
        "file_url": file_url, "file_size_human": file_size_human,
        "tags": [{"tag": "Campaigns"}], "is_fallback": False,
    }


class FakeMirror:
    """/user-files 返回英文标题（模拟语言域差异）。"""

    def __init__(self, user_items):
        self.user_items = user_items

    def user_files(self, steamid, num=50, page=1):
        return {"items": self.user_items}


class FakeApi:
    def __init__(self, details, user_items):
        self.details = details
        self.mirror = FakeMirror(user_items)

    def get_detail_with_fallback(self, workshop_id):
        d = self.details.get(str(workshop_id))
        if not d:
            raise ValueError(f"not found: {workshop_id}")
        return d


def _series_setup():
    """魂斗罗Z 场景：detail 中文标题，user-files 英文标题，required_items 互标。"""
    details = {
        "1987326534": _detail("1987326534", "魂斗罗Z V0.7(part1)",
                              required=["2846314478"]),
        "2846314478": _detail("2846314478", "魂斗罗Z V0.7(part2)",
                              required=["1987326534"]),
    }
    user_items = [
        {"id": "2846314478", "title": "ContraZ V0.7(part2)"},
        {"id": "1987326534", "title": "ContraZ V0.7(part1)"},
        {"id": "1655417853", "title": "Land of oblivion V2.71"},
    ]
    return FakeApi(details, user_items)


def test_forward_path_uses_required_items():
    """E-2: 场景 B——part1 标了 required_items=[P2]，正向路径必须能发现 P2。"""
    details = {
        "P1": _detail("P1", "XX Campaign Part 1", required=["P2"]),
        "P2": _detail("P2", "XX Campaign Part 2", required=[]),
    }
    api = FakeApi(details, [])
    r = find_series_parts(api, "P1", aggressive=True)
    ids = {p["workshop_id"] for p in r["parts"]}
    assert "P2" in ids
    assert r["is_series"] is True


def test_reverse_lookup_localized_titles():
    """C: /user-files 英文标题时，经 /detail 同语言域比较仍能找到 part2。"""
    api = _series_setup()
    r = find_series_parts(api, "1987326534", aggressive=True)
    ids = {p["workshop_id"] for p in r["parts"]}
    assert ids == {"1987326534", "2846314478"}
    assert r["total_parts"] == 2


def test_reverse_lookup_bare_n_of_m():
    """裸 2/5 命名（无 part 字样）+ 无 required_items 也能反查配对。"""
    details = {
        "A1": _detail("A1", "Death Valley 1/5"),
        "A3": _detail("A3", "Death Valley 3/5"),
    }
    user_items = [
        {"id": "A1", "title": "Death Valley 1/5"},
        {"id": "A3", "title": "Death Valley 3/5"},
    ]
    api = FakeApi(details, user_items)
    r = find_series_parts(api, "A1", aggressive=True)
    ids = {p["workshop_id"] for p in r["parts"]}
    assert ids == {"A1", "A3"}


def test_union_captures_deps_declared_on_part1_only():
    """E-1: 只有 part1 标了依赖（P2 + 额外资源 D1），从 part2 出发必须全捕获。"""
    details = {
        "P1": _detail("P1", "Series Part 1", required=["P2", "D1"]),
        "P2": _detail("P2", "Series Part 2", required=[]),
        "D1": _detail("D1", "Required Model Pack", required=[]),
    }
    api = FakeApi(details, [])
    deps = resolve_dependencies_union(api, ["P2"], max_depth=2)
    ids = set(deps.keys())
    # P2 自身 + 经 P1 传递的 D1（P2 → P1 → D1 需要 P1 在 P2 的 required 里才可达）
    assert "P2" in ids
    # 仅从 P2 出发、P2 无声明时，D1 不可达——这正是场景 B 的剩余部分，
    # 由 find_series_parts 找到 P1 后把 P1 加入根集合才能捕获
    deps_from_all = resolve_dependencies_union(api, ["P2", "P1"], max_depth=2)
    ids_all = set(deps_from_all.keys())
    assert "D1" in ids_all


def test_union_skips_nonexistent_roots():
    api = _series_setup()
    deps = resolve_dependencies_union(api, ["1987326534", "9999999999"])
    assert "1987326534" in deps
