"""series_detector 系列命名识别单测。"""
import pytest

from core.series_detector import (
    detect_series,
    extract_version_token,
    share_version_token,
    titles_share_base,
)

POSITIVE = [
    ("hehe23_part1", 1, None),
    ("hehe23_part2", 2, None),
    ("魂斗罗Z V0.7(part1)", 1, None),
    ("魂斗罗Z V0.7(part2)", 2, None),
    ("江门职院 V1.3Part 1", 1, None),
    ("江门职院 V1.3_Part 2", 2, None),
    ("九死一生之小女孩的梦境V3.5 - part2", 2, None),
    ("广西-南宁 V1.15.1 PART-1/2", 1, 2),
    ("广西-南宁 V1.15.1 PART-2/2", 2, 2),
    ("Dear Esther v1.2 (Part 1/2)", 1, 2),
    ("连接 Connection (part 1/2)", 1, 2),
    ("可畏语音包（替换nick）part1", 1, None),
    ("XXX 2/5", 2, 5),
    ("XXX 1/5", 1, 5),
    ("XXX（2/5）", 2, 5),
    ("XXX [2/5]", 2, 5),
    ("XXX (2/5)", 2, 5),
    ("XXX 2 of 5", 2, 5),
    ("XXX Part 2 of 5", 2, 5),
    ("死亡小镇 第一章", 1, None),
    ("死亡小镇 第二章", 2, None),
    ("XX城 第二部分", 2, None),
    ("AeroShore Chapter 1", 1, None),
    ("ContraZ V0.7(part2)", 2, None),
    ("hehe23_part1_v2", 1, None),
]

NEGATIVE = [
    "2026/07/31",
    "2026年7月8日",
    "MMD替换背景菜单(R18)第13期",
    "KFR喷漆第1期",
    "VTubers主菜单图标第2.5弹！",
    "party time",
    "particle-purple",
    "格洛克18全自动手枪（第三把手枪）",
    "结算音乐替换成碧蓝档案的结算音乐《party time》",
    "V1.15.1/2",
    "2/5",
    "Part",
    "chapter",
    "第13期",
]


@pytest.mark.parametrize("title,current,total", POSITIVE)
def test_positive(title, current, total):
    r = detect_series(title)
    assert r["is_series"] is True, f"{title!r} 应识别为系列: {r}"
    assert r["current"] == current
    assert r["total"] == total


@pytest.mark.parametrize("title", NEGATIVE)
def test_negative(title):
    r = detect_series(title)
    assert r["is_series"] is False, f"{title!r} 不应识别为系列: {r}"


def test_titles_share_base_same_series():
    assert titles_share_base("XXX 1/5", "XXX 2/5") is True
    assert titles_share_base("魂斗罗Z V0.7(part1)", "魂斗罗Z V0.7(part2)") is True
    assert titles_share_base("hehe23_part1", "hehe23_part2") is True
    assert titles_share_base("AeroShore Chapter 1", "AeroShore Chapter 2") is True
    assert titles_share_base("死亡小镇 第一章", "死亡小镇 第二章") is True


def test_titles_share_base_different_series():
    assert titles_share_base("AAA 1/5", "BBB 1/5") is False
    assert titles_share_base("hehe23_part1", "魂斗罗Z V0.7(part1)") is False
    # hehe23 ↔ hehe30 误配回归（共享 "hehe" 前缀但数字不同）
    assert titles_share_base("hehe23_part1", "hehe30-part1（3.0）") is False


def test_version_token():
    assert extract_version_token("魂斗罗Z V0.7(part1)") == "v0.7"
    assert extract_version_token("ContraZ V0.7(part2)") == "v0.7"
    assert extract_version_token("hehe23_part1") == ""
    assert share_version_token("魂斗罗Z V0.7(part1)", "ContraZ V0.7(part2)") is True
    assert share_version_token("A V1.0", "B V2.0") is False
    assert share_version_token("A V1.0", "B") is True
