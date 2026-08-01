"""phase2 战役判定（重写版）。

输入 detail 字典 → 输出 (score, is_campaign, reasons, ambiguous, definitely_mod)。

设计见 ARCHITECTURE.md §4.2。
"""
from __future__ import annotations

import re
from typing import Any

MOD_TAGS = {
    "Models", "Textures", "Sounds",
    "Survivors", "Common Infected", "Special Infected",
    "Scripts", "Weapons", "Items", "UI",
}

REPLACEMENT_TAG_PAIRS = [
    {"Sounds", "Survivors"},
    {"Sounds", "Common Infected"},
    {"Survivors", "Common Infected"},
    {"Models", "Survivors"},
]

CAMPAIGN_TITLE_KEYWORDS = [
    "campaign", "战役", "map pack", "地图包", "章节", "chapter",
]

MOD_DESC_KEYWORDS_STRONG = [
    "replaces the ", "replacement for", "skin mod", "skin replacer",
    "替换", "皮肤", "替换为", "model replace",
    "color correction", "colour correction", "skybox",
    "script pack", "ai function", " survivors ai ",
    "menu background", "main menu", "replacer pack", "skybox pack",
    "weapon replace", "weapon replacer", "texture pack",
    "replaces the default", "replaces all ",
]

MOD_DESC_KEYWORDS_WEAK = [
    "this mod ",
    "this mod adds npc",
    "this mod changes",
    "addon for the",
    "mutation",
]

MOD_TITLE_PATTERNS = [
    r"\[mutation\]",
    r"\(with sound\)",
    r"color\s*correction",
    r"skybox",
    r"replacer",
    r"^.*\bskin\b.*$",
    r"\bmenu\b",
    r"\bscript\b",
    r"\b survivors? ai\b",
]

CHAPTER_NAME_PATTERNS = [
    r"\bc\d+m\d+[_a-z]",
    r"\bm\d+_[a-z]",
    r"\bl4d2?_[a-z]",
    r"\bchapter\s*\d+",
    r"第[一二三四五六七八九十\d]+章",
    r"finale\b",
]


def _tag_set(detail: dict) -> set[str]:
    raw = detail.get("tags") or []
    if raw and isinstance(raw[0], dict):
        return {(t.get("tag") or "").strip() for t in raw if t.get("tag")}
    return {str(t).strip() for t in raw if t}


def _has_chapter_pattern(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(re.search(p, lower, re.IGNORECASE) for p in CHAPTER_NAME_PATTERNS)


def _match_replacement_pairs(tags: set[str]) -> bool:
    for pair in REPLACEMENT_TAG_PAIRS:
        if pair.issubset(tags):
            return True
    return False


def judge_campaign(
    detail: dict,
    in_curated_list: bool = False,
    curated_chapters: list[str] | None = None,
) -> dict[str, Any]:
    """对单个物品做 phase2 战役判定。

    Args:
        detail: 镜像站 /detail 接口返回的字典
        in_curated_list: 是否在 echocn 人工策展名单中
        curated_chapters: echocn 名单里的章节信息（如果有）

    Returns:
        {
            "score": int,
            "is_campaign": bool,
            "ambiguous": bool,
            "definitely_mod": bool,
            "confidence": "high"|"medium"|"low",
            "reasons": [str],
        }
    """
    score = 0
    reasons: list[str] = []

    title = detail.get("title") or ""
    desc = detail.get("description") or ""
    file_url = detail.get("file_url") or ""
    file_size_human = detail.get("file_size_human") or ""
    subscriptions = int(detail.get("subscriptions") or 0)
    favorited = int(detail.get("favorited") or 0)
    tags = _tag_set(detail)
    tag_count = len(tags)

    size_mb = _parse_size_mb(file_size_human)

    # === 正向信号 ===
    if in_curated_list:
        score += 3
        reasons.append("echocn 人工策展名单命中 (+3)")

    chapter_text = desc
    if curated_chapters:
        chapter_text = desc + " " + " ".join(curated_chapters)
    if _has_chapter_pattern(chapter_text):
        score += 2
        reasons.append("描述/标题含章节命名模式 (+2)")

    if tag_count <= 3 and "Campaigns" in tags:
        score += 2
        reasons.append(f"tags 数量 ≤ 3 且含 Campaigns (+2, 共 {tag_count} 个)")

    mod_tag_intersect = tags & MOD_TAGS
    if "Co-op" in tags and not mod_tag_intersect:
        score += 1
        reasons.append("tags 含 Co-op 且不含 Mod 类标签 (+1)")

    if size_mb is not None and size_mb > 50:
        score += 1
        reasons.append(f"文件 > 50MB (+1, {size_mb:.0f}MB)")

    title_lower = title.lower()
    if any(kw in title_lower for kw in CAMPAIGN_TITLE_KEYWORDS):
        score += 1
        reasons.append("标题含战役关键词 (+1)")

    # === 负向信号 ===
    if "Models" in tags and "Textures" in tags:
        score -= 3
        reasons.append("tags 同时含 Models + Textures (-3, mod 强特征)")

    if _match_replacement_pairs(tags):
        score -= 2
        reasons.append("tags 含 Sounds+Survivors+Common Infected 等 mod 替换组合 (-2)")

    desc_lower = desc.lower()
    strong_hits = [kw for kw in MOD_DESC_KEYWORDS_STRONG if kw in desc_lower]
    weak_hits = [kw for kw in MOD_DESC_KEYWORDS_WEAK if kw in desc_lower]

    if strong_hits:
        score -= 2
        reasons.append(f"描述含强 mod 关键词 (-2, {strong_hits[:2]})")
    elif weak_hits and not any(
        r.startswith(("描述/标题含章节", "echocn")) for r in reasons
    ):
        score -= 1
        reasons.append(f"描述含弱 mod 关键词且无强正信号 (-1, {weak_hits[:1]})")

    title_lower_for_mod = title.lower()
    mod_title_hits = [
        p for p in MOD_TITLE_PATTERNS
        if re.search(p, title_lower_for_mod, re.IGNORECASE)
    ]
    if mod_title_hits:
        score -= 2
        reasons.append(f"标题命中 mod 模式 (-2, {mod_title_hits[:2]})")

    if (
        size_mb is not None
        and size_mb < 10
        and "Campaigns" in tags
        and not in_curated_list
    ):
        score -= 2
        reasons.append(
            f"file_size < 10MB 且 tags 含 Campaigns 且非策展名单 (-2, {size_mb:.1f}MB)"
        )

    if (
        size_mb is not None
        and size_mb < 5
        and "Campaigns" in tags
        and not in_curated_list
    ):
        score -= 1
        reasons.append(f"file_size < 5MB 且 tags 含 Campaigns (-1, {size_mb:.1f}MB)")

    if subscriptions > 5000 and favorited > 0 and favorited / subscriptions < 0.2:
        score -= 1
        reasons.append(
            f"subscriptions 高但收藏率低 (-1, {subscriptions}/{favorited})"
        )

    if not file_url:
        score -= 1
        reasons.append("无 file_url (-1)")

    # === 阈值 ===
    is_campaign = score >= 1
    ambiguous = 1 <= score <= 2
    definitely_mod = score < -1

    if score >= 3:
        confidence = "high"
    elif score >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "score": score,
        "is_campaign": is_campaign,
        "ambiguous": ambiguous,
        "definitely_mod": definitely_mod,
        "confidence": confidence,
        "reasons": reasons,
    }


def _parse_size_mb(size_human: str) -> float | None:
    if not size_human:
        return None
    s = size_human.strip().lower().replace(",", "")
    try:
        if "mb" in s:
            return float(s.replace("mb", "").strip())
        if "gb" in s:
            return float(s.replace("gb", "").strip()) * 1024
        if "kb" in s:
            return float(s.replace("kb", "").strip()) / 1024
        return float(s)
    except (ValueError, TypeError):
        return None
