"""Production audit: past misclassifications turned into rules.

Every rule names the incident it comes from. The audit runs after discovery
and agent writes, on every asset page and from `prod audit`; it never changes
data by itself. Each finding carries the exact fix (a preview move, an option
mark) or can be acknowledged once as intended, after which it stays quiet.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import production

RULES: dict[str, dict[str, str]] = {
    "state-as-revision": {
        "title": "状态或截图被当成了版本",
        "severity": "warn",
        "incident": "TASK-82.2 界面状态（收起、更多控制、成员菜单、改前改后对比）曾被编号为 v1…v7",
    },
    "parallel-as-revision": {
        "title": "同时存在的配置被当成了版本",
        "severity": "warn",
        "incident": "钢琴键盘布局与数字圆形键位曾被编号为同一视图的 v2 / v3",
    },
    "options-as-revisions": {
        "title": "版本号没有依据：是前后迭代，还是互相竞争的备选方案？",
        "severity": "warn",
        "incident": "4 款吉他 donor、3 种强调色、13 张概念方向图曾被编号为 v1…vN",
    },
    "duplicate-content": {
        "title": "同一内容出现了不止一次",
        "severity": "info",
        "incident": "同一文件的副本被当作新版本时，比较和定稿都会误导",
    },
    "unsorted-backlog": {
        "title": "有待归类的素材",
        "severity": "info",
        "incident": "没有视图/变体或版本号的文件曾被自动递增成新版本",
    },
    "lock-unverified": {
        "title": "定稿里的文件丢失或已被改动",
        "severity": "error",
        "incident": "定稿后原文件被覆盖，Agent 会拿到与批准时不同的字节",
    },
}

# Words that name an interaction state, a framing or a capture condition, not a new design.
STATE_TOKENS = {
    "receded", "collapsed", "expanded", "more", "less", "cinematic", "hidden", "hover", "pressed", "active", "disabled",
    "tooltip", "menu", "popup", "crop", "cropped", "compare", "comparison", "before", "after", "empty", "solo", "current",
    "replace", "create", "discover", "member", "sounding", "playing", "idle", "open", "closed", "selected", "focus",
    "a11y", "contrast", "large", "en", "zh", "hans", "hant", "ja", "ko", "locale", "1280", "1920", "2560", "4k", "720p", "1080p",
    "front", "side", "rear", "back", "top", "bottom", "left", "right", "closeup", "detail", "wide", "medium", "close",
    "wireframe", "clay", "jianpu", "solfege", "day", "dusk", "night", "morning", "shift", "ctrl",
    "收起", "展开", "悬停", "按下", "状态", "裁切", "对比", "改前", "改后", "白天", "黄昏", "夜晚", "简谱", "唱名",
}
# Pairs of words that name configurations which ship side by side.
PARALLEL_TOKENS = {
    "piano", "grid", "round", "keys", "keyboard", "pad", "light", "dark", "male", "female", "mobile", "desktop", "tablet",
    "portrait", "landscape", "summer", "winter", "red", "blue", "green", "gold", "silver", "lefthand", "righthand",
    "钢琴", "圆形", "数字", "浅色", "深色", "男", "女",
}
_TOKEN_SPLIT = re.compile(r"[\s_\-.()\[\]]+")


def _tokens(rel: str) -> set[str]:
    stem = Path(rel).stem.lower()
    return {token for token in _TOKEN_SPLIT.split(stem) if token and not re.fullmatch(r"v\d{1,3}", token)}


def _version_fact(rel: str) -> bool:
    return production._parse_version(rel) is not None or bool(re.search(r"(^|/)v\d{1,3}/", rel, re.IGNORECASE))


def audit(asset_key: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    detail = detail or production.asset_detail(asset_key)
    acked = production.acknowledgements(asset_key)
    findings: list[dict[str, Any]] = []

    def add(rule: str, subject: str, slot: str | None, message: str, fixes: list[dict[str, Any]], items: list[str] | None = None) -> None:
        if (rule, subject) in acked:
            return
        meta = RULES[rule]
        findings.append({"rule": rule, "severity": meta["severity"], "title": meta["title"], "incident": meta["incident"],
                         "subject": subject, "slot": slot, "message": message, "items": items or [], "fixes": fixes})

    seen_sha: dict[str, str] = {}
    for view in detail["views"]:
        live = [r for r in view["revisions"] if r["state"] != "archived"]
        for revision in live:
            label = f"{view['label']} {revision['label']}"
            for sha, where in ([(revision["sha256"], label)] + [(p.get("sha256"), f"{label} 预览") for p in revision.get("previews", [])]):
                if not sha:
                    continue
                if sha in seen_sha and seen_sha[sha] != where:
                    add("duplicate-content", sha, view["view"], f"{where} 与 {seen_sha[sha]} 是同一份内容", [], [where, seen_sha[sha]])
                seen_sha.setdefault(sha, where)
        iterations = [r for r in live if r["kind"] == "revision"]
        if len(iterations) < 2:
            continue
        unfounded = [r for r in iterations if not _version_fact(r["rel"]) and r["origin"] != "uploaded"]
        if len(unfounded) < 2:
            continue
        flagged: set[str] = set()
        primary = min(iterations, key=lambda r: r["number"])
        # Only words that tell files apart count; a word every file shares (often the slot itself) says nothing.
        common = set.intersection(*(_tokens(r["rel"]) for r in iterations))
        for revision in unfounded:
            if revision is primary:
                continue
            states = (_tokens(revision["rel"]) - common) & STATE_TOKENS
            if states and not (revision["selected"] or revision["locked"]):
                flagged.add(revision["revision_id"])
                add("state-as-revision", revision["revision_id"], view["view"],
                    f"「{view['label']}」{revision['label']} 的文件名表明是状态/取景（{'、'.join(sorted(states))}），更像 {primary['label']} 的一张预览",
                    [{"action": "preview", "revision_id": revision["revision_id"], "view": view["view"], "preview_of": primary["number"],
                      "label": f"转为 {primary['label']} 的预览",
                      "command": f"asset-viewer prod move {revision['revision_id']} --slot {view['view']} --preview-of {primary['number']}"}],
                    [revision["rel"]])
        for i, a in enumerate(unfounded):
            for b in unfounded[i + 1:]:
                diff = _tokens(a["rel"]) ^ _tokens(b["rel"])
                if diff and diff <= PARALLEL_TOKENS:
                    subject = "|".join(sorted((a["revision_id"], b["revision_id"])))
                    flagged.update((a["revision_id"], b["revision_id"]))
                    add("parallel-as-revision", subject, view["view"],
                        f"「{view['label']}」{a['label']} 与 {b['label']} 只差在 {'、'.join(sorted(diff))}：更像同时存在的两个配置变体",
                        [{"action": "ack", "label": "确认确实是迭代"},
                         {"action": "hint", "label": "改为配置变体",
                          "command": f"asset-viewer prod slot-add {asset_key} <variant> --variant  然后  asset-viewer prod move <revision> --slot <variant>"}],
                        [a["rel"], b["rel"]])
        rest = [r for r in unfounded if r["revision_id"] not in flagged]
        if len(rest) >= 2:
            add("options-as-revisions", view["view"], view["view"],
                f"「{view['label']}」有 {len(rest)} 个编号版本没有版本号依据（文件名或 vN 目录）。若它们互相竞争、只选其一，应标为备选方案",
                [{"action": "option", "revision_ids": [r["revision_id"] for r in rest], "label": "标为备选方案",
                  "command": f"asset-viewer prod mark {' '.join(r['revision_id'] for r in rest)} option"},
                 {"action": "ack", "label": "确认是前后迭代"}],
                [r["rel"] for r in rest])

    unsorted = detail.get("unsorted")
    if unsorted is None:
        unsorted = production.unsorted_files(asset_key)
    if unsorted:
        add("unsorted-backlog", f"{len(unsorted)}", None, f"{len(unsorted)} 个文件没有可确定的视图/变体或版本号",
            [{"action": "hint", "label": "在资产页归类", "command": f"asset-viewer prod unsorted {asset_key}"}], [u["rel"] for u in unsorted[:5]])
    if detail.get("lock"):
        lock = production.design_lock(asset_key)
        if lock.get("locked") and not lock.get("verified"):
            add("lock-unverified", lock["lock"]["name"], None, "；".join(lock["problems"]),
                [{"action": "hint", "label": "重新上传为新版本后再次定稿"}])
    order = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return {"asset": asset_key, "findings": findings,
            "counts": {level: sum(1 for f in findings if f["severity"] == level) for level in order}}


def audit_project(project: str | None = None) -> list[dict[str, Any]]:
    return [result for key in production.asset_keys(project) if (result := audit(key))["findings"]]
