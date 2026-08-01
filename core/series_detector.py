"""Part 系列命名识别（纯函数）。

输入标题/描述 → 输出 {is_series, base_name, current, total}。

设计见 ARCHITECTURE.md §4.1.4。
"""
from __future__ import annotations

import re

# 注意：part 前缀用 (?<![a-z]) 而非 \b——"hehe23_part1"、"V1.3Part 1" 这类
# 数字/下划线紧贴 part 的常见命名在 \b 下没有词边界，会被漏判（实测复现）。
# (?<![a-z]) 在 re.IGNORECASE 下同时挡掉 "depart" 等单词内误匹配。
#
# 裸 "n/m"（bare_n_of_m）必须放在所有带括号/带 part 字样的模式之后，
# 且依赖 detect_series 里的数值校验（total<=10、current<=total、base 非空），
# 防止 "2026/07/31" 这类日期和空 base 的误判。
SERIES_PATTERNS = [
    (r"(?<![a-z])part\s*[_\-]?\s*(\d+)\s*(?:/|of|\s)\s*(\d+)\b", "part_n_of_m"),
    (r"(?<![a-z])part\s*[_\-]?\s*(\d+)(?!\d)(?!\s*(?:/|of))", "part_n"),
    (r"\[(\d+)\s*/\s*(\d+)\]", "bracket_n_of_m"),
    (r"\((\d+)\s*/\s*(\d+)\)", "paren_n_of_m"),
    (r"第\s*([一二三四五六七八九十\d]+)\s*[部分]", "cn_part"),
    (r"第\s*([一二三四五六七八九十\d]+)\s*章", "cn_chapter"),
    (r"(?<![a-z])chapter\s*(\d+)\b", "en_chapter"),
    (r"(?<![a-z])pt\.?\s*(\d+)(?!\d)", "pt_n"),
    (r"(?<![\d./])(\d+)\s*/\s*(\d+)(?!\d)", "bare_n_of_m"),
    (r"(?<![\d./])(\d+)\s+of\s+(\d+)\b", "bare_n_of_m"),
]

# 数值校验：part 总数上限（工坊系列实测最大 5，放宽到 10 防误判日期/比分）
MAX_TOTAL_PARTS = 10

CN_NUM_MAP = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_to_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s in CN_NUM_MAP:
        return CN_NUM_MAP[s]
    if len(s) == 2 and s[0] == "十":
        return 10 + CN_NUM_MAP.get(s[1], 0)
    if len(s) == 2 and s[1] == "十":
        return CN_NUM_MAP.get(s[0], 0) * 10
    if len(s) == 3 and s[0] != "十" and s[1] == "十":
        return CN_NUM_MAP.get(s[0], 0) * 10 + CN_NUM_MAP.get(s[2], 0)
    return 0


def _strip_part_suffix(title: str, matched_span: tuple[int, int]) -> str:
    base = title[: matched_span[0]].rstrip()
    base = re.sub(r"[\s\-_]+$", "", base)
    base = re.sub(r"[\(\[\{（【][^\)\]\}】]*$", "", base)
    return base or title


def _validate_n_of_m(current: int, total: int) -> bool:
    return (
        current >= 1
        and 2 <= total <= MAX_TOTAL_PARTS
        and current <= total
    )


VERSION_TOKEN_RE = re.compile(r"v?\d+(?:\.\d+)+", re.IGNORECASE)


def extract_version_token(text: str) -> str:
    """提取版本 token（如 "V0.7" -> "v0.7"），用于跨语言域系列配对。"""
    m = VERSION_TOKEN_RE.search(text or "")
    return m.group(0).lower() if m else ""


def share_version_token(a: str, b: str) -> bool:
    """双方都有版本号时必须一致；任一方无版本号则不构成排除依据。"""
    ta, tb = extract_version_token(a), extract_version_token(b)
    return (not ta) or (not tb) or ta == tb


def detect_series(title: str, description: str = "") -> dict:
    """识别标题中的 Part 系列。

    Returns:
        {
            "is_series": bool,
            "base_name": str,        # 去掉 Part 后缀的基础名
            "current": int | None,   # 当前是第几 Part
            "total": int | None,     # 总 Part 数（无法判断则 None）
            "pattern": str | None,   # 命中的模式名
        }
    """
    if not title:
        return _empty_result()

    for pattern, name in SERIES_PATTERNS:
        m = re.search(pattern, title, re.IGNORECASE)
        if not m:
            continue

        groups = m.groups()
        if name in ("part_n_of_m", "bracket_n_of_m", "paren_n_of_m", "bare_n_of_m"):
            current = int(groups[0])
            total = int(groups[1])
            if not _validate_n_of_m(current, total):
                continue
        elif name == "cn_part" or name == "cn_chapter":
            current = _cn_to_int(groups[0])
            total = None
        else:
            current = int(groups[0])
            total = None

        base_name = _strip_part_suffix(title, m.span())
        if m.start() == 0 or not base_name.strip():
            continue

        return {
            "is_series": True,
            "base_name": base_name,
            "current": current,
            "total": total,
            "pattern": name,
        }

    return _empty_result()


def titles_share_base(a: str, b: str, fuzzy: bool = True) -> bool:
    """两个标题是否属于同一系列（base_name 相同）。

    前缀模糊阈值为 0.75 且绝对长度 ≥6：实测 "hehe23"↔"hehe30"（共享 4 字符，
    比例 0.67）会误配成同一系列，必须排除；"Death Valley" 类前缀保留。
    """
    sa = detect_series(a)
    sb = detect_series(b)
    if not sa["is_series"] or not sb["is_series"]:
        return False
    if sa["base_name"] and sa["base_name"] == sb["base_name"]:
        return True
    if not fuzzy:
        return False
    ba = sa["base_name"].lower()
    bb = sb["base_name"].lower()
    if not ba or not bb:
        return False
    if ba in bb or bb in ba:
        return True
    prefix_len = 0
    for i in range(min(len(ba), len(bb))):
        if ba[i] == bb[i]:
            prefix_len += 1
        else:
            break
    return prefix_len >= max(6, max(len(ba), len(bb)) * 0.75)


def _empty_result() -> dict:
    return {
        "is_series": False,
        "base_name": "",
        "current": None,
        "total": None,
        "pattern": None,
    }
