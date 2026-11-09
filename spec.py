"""Course-spec validation.

The model fills this structure; nothing here trusts it. Validation never raises
on content problems — it drops what it cannot render, records a warning, and
substitutes a default, so a build always produces a page.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

LEVELS = ("novice", "intermediate", "advanced")
SPEC_VERSION = 1

# The closed block set. An unknown kind is dropped with a warning — never
# rendered raw, so a model emitting HTML cannot reach the page through content.
BLOCK_KINDS = {
    "prose", "figure", "aside", "quote", "steps", "definition", "analogy",
    "comparison", "code", "math", "callout", "timeline", "checkpoint",
}

EXERCISE_KINDS = {"mcq", "recall"}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Section ids land in `id="..."`, href="#..." and the page's own state keys.
_SID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def _s(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _list(value: Any) -> list:
    """A list, or [] — never a slice of a dict/str.

    `(raw.get("sources") or [])[:40]` raised KeyError on a dict, which broke this module's
    one promise: validation never raises on bad content.
    """
    return value if isinstance(value, list) else []


def _items(value: Any, limit: int) -> list:
    return _list(value)[:limit]


def _safe_url(value: Any, limit: int = 800) -> str:
    """Only http(s) survives: these become `href` attributes in the rendered page, and a
    model-supplied `javascript:` URL is an executable attribute, not a citation."""
    url = _s(value, limit).strip()
    return url if re.match(r"^https?://", url, re.I) else ""


_FIGURE_TYPES = {"flow", "bars", "line", "trajectory", "diagram", "sequence"}


def _figure_spec(spec: Dict[str, Any], where: str, warnings: List[str]) -> Dict[str, Any]:
    """Normalise a figure so the renderer can index it without checking anything.

    `{"type": "flow", "steps": 3}` used to reach `render._figure` and raise TypeError after a
    full build had already been paid for, so the shapes are settled here.
    """
    kind = _s(spec.get("type"), 20).strip().lower()
    if kind not in _FIGURE_TYPES:
        if kind:
            warnings.append(f"{where}: dropped figure of unknown type {kind!r}")
        return {}
    if kind == "flow":
        steps = [_s(s, 60).strip() for s in _list(spec.get("steps"))][:7]
        steps = [s for s in steps if s]
        if len(steps) < 2:
            warnings.append(f"{where}: dropped flow figure with fewer than two steps")
            return {}
        return {"type": "flow", "steps": steps, "caption": _s(spec.get("caption"), 300)}
    if kind == "trajectory":
        n = spec.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) else 27
        return {"type": "trajectory", "n": max(3, min(n, 97)),
                "caption": _s(spec.get("caption"), 300)}
    if kind == "diagram":
        nodes: List[Dict[str, str]] = []
        seen: set = set()
        for raw_node in _list(spec.get("nodes"))[:8]:
            if not isinstance(raw_node, dict):
                continue
            label = _s(raw_node.get("label"), 26).strip()
            if not label:
                continue
            nid = re.sub(r"[^A-Za-z0-9_-]", "", _s(raw_node.get("id"), 20).strip())[:20]
            if not nid or nid in seen:
                nid = f"n{len(nodes) + 1}"
            seen.add(nid)
            nodes.append({"id": nid, "label": label, "group": _s(raw_node.get("group"), 12).strip()})
        if len(nodes) < 2:
            warnings.append(f"{where}: dropped diagram with fewer than two labelled nodes")
            return {}
        known = {node["id"] for node in nodes}
        edges: List[Dict[str, str]] = []
        for raw_edge in _list(spec.get("edges"))[:12]:
            if not isinstance(raw_edge, dict):
                continue
            src = _s(raw_edge.get("from"), 20).strip()
            dst = _s(raw_edge.get("to"), 20).strip()
            if src not in known or dst not in known:
                continue      # a dangling edge is one typo, not a reason to lose the figure
            edges.append({"from": src, "to": dst, "label": _s(raw_edge.get("label"), 26).strip()})
        return {"type": "diagram", "nodes": nodes, "edges": edges,
                "caption": _s(spec.get("caption"), 300)}
    if kind == "sequence":
        actors: List[str] = []
        for raw_actor in _list(spec.get("actors"))[:5]:
            name = _s(raw_actor, 22).strip()
            if name and name not in actors:
                actors.append(name)
        if len(actors) < 2:
            warnings.append(f"{where}: dropped sequence figure with fewer than two actors")
            return {}
        messages: List[Dict[str, Any]] = []
        for raw_message in _list(spec.get("messages"))[:8]:
            if not isinstance(raw_message, dict):
                continue
            src = _s(raw_message.get("from"), 22).strip()
            dst = _s(raw_message.get("to"), 22).strip()
            if src not in actors or dst not in actors:
                continue
            messages.append({"from": src, "to": dst,
                             "label": _s(raw_message.get("label"), 32).strip(),
                             "reply": bool(raw_message.get("reply"))})
        if not messages:
            warnings.append(f"{where}: dropped sequence figure with no message between its actors")
            return {}
        return {"type": "sequence", "actors": actors, "messages": messages,
                "caption": _s(spec.get("caption"), 300)}
    points = []
    for raw in _list(spec.get("points"))[:24]:
        if not isinstance(raw, dict):
            continue
        value = raw.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        points.append({"label": _s(raw.get("label"), 18), "value": float(value)})
    if len(points) < 2:
        warnings.append(f"{where}: dropped {kind} figure with fewer than two numeric points")
        return {}
    return {"type": kind, "points": points, "caption": _s(spec.get("caption"), 300),
            "xlabel": _s(spec.get("xlabel"), 28), "ylabel": _s(spec.get("ylabel"), 28)}


def _blocks(raw: Any, where: str, warnings: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = _s(item.get("kind"), 40).strip().lower()
        if kind not in BLOCK_KINDS:
            warnings.append(f"{where}: dropped unknown block kind {kind!r}")
            continue
        block: Dict[str, Any] = {"kind": kind}
        if kind in {"prose", "aside", "quote", "analogy", "callout", "definition", "math", "figure"}:
            block["md"] = _s(item.get("md") or item.get("text") or item.get("tex"))
        if kind == "figure":
            block["caption"] = _s(item.get("caption"), 400)
            raw_figure = item.get("spec")
            figure = _figure_spec(raw_figure if isinstance(raw_figure, dict) else {}, where, warnings)
            if not figure:
                continue          # a figure with nothing to draw is not a block
            block["spec"] = figure
        if kind in {"definition", "analogy"}:
            block["term"] = _s(item.get("term"), 200)
        if kind == "quote":
            block["cite"] = _s(item.get("cite"), 200)
            block["href"] = _safe_url(item.get("href"), 500)
        if kind == "callout":
            tone = _s(item.get("tone") or item.get("kind_hint"), 20).strip().lower()
            block["tone"] = tone if tone in {"honesty", "warning", "insight"} else "insight"
            block["title"] = _s(item.get("title"), 200)
        if kind in {"steps", "comparison", "timeline"}:
            items = item.get("items")
            block["items"] = [ _s(x, 600) for x in items ] if isinstance(items, list) else []
            if kind == "comparison":
                block["left"] = _s(item.get("left"), 120)
                block["right"] = _s(item.get("right"), 120)
        if kind == "code":
            block["code"] = _s(item.get("code"), 8000)
            block["lang"] = _s(item.get("lang"), 20)
        if kind == "checkpoint":
            block["md"] = _s(item.get("md"))
        out.append(block)
    return out


def _exercise(raw: Any, where: str, warnings: List[str]) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = _s(raw.get("kind"), 20).strip().lower()
    if kind not in EXERCISE_KINDS:
        warnings.append(f"{where}: dropped exercise of unknown kind {kind!r}")
        return None
    ex: Dict[str, Any] = {"kind": kind, "prompt": _s(raw.get("prompt"), 1200)}
    if not ex["prompt"]:
        warnings.append(f"{where}: dropped exercise with no prompt")
        return None
    if kind == "mcq":
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) < 2:
            warnings.append(f"{where}: mcq needs >= 2 choices; dropped")
            return None
        ex["choices"] = [_s(c, 400) for c in choices[:6]]
        try:
            answer = int(raw.get("answer", -1))
        except (TypeError, ValueError):
            answer = -1
        if not 0 <= answer < len(ex["choices"]):
            warnings.append(f"{where}: mcq answer index out of range; dropped")
            return None
        ex["answer"] = answer
        ex["why"] = _s(raw.get("why"), 800)
    else:
        ex["rubric"] = [_s(k, 120) for k in _items(raw.get("rubric"), 8) if _s(k).strip()]
        ex["answer"] = _s(raw.get("answer"), 2000)
    anchor = raw.get("anchor")
    if isinstance(anchor, dict) and anchor.get("source"):
        ex["anchor"] = {"source": _s(anchor.get("source"), 200),
                        "page": anchor.get("page"), "time": anchor.get("time")}
    return ex


def _resources(raw: Any) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {"videos": [], "reading": []}
    if not isinstance(raw, dict):
        return out
    for video in _items(raw.get("videos"), 12):
        if not isinstance(video, dict) or not _s(video.get("id")).strip():
            continue
        out["videos"].append({
            "id": _s(video.get("id"), 40).strip(),
            "title": _s(video.get("title"), 300),
            "channel": _s(video.get("channel"), 120),
            "duration": video.get("duration") if isinstance(video.get("duration"), int) else None,
            "why": _s(video.get("why"), 300),
            "transcript": bool(video.get("transcript")),
            "thumb": _s(video.get("thumb"), 20) or "none",
            "thumb_data": _s(video.get("thumb_data"), 90000),
            "level": _s(video.get("level"), 20) if _s(video.get("level")) in LEVELS else "",
        })
    for item in _items(raw.get("reading"), 16):
        if not isinstance(item, dict) or not (_s(item.get("href")).strip() or _s(item.get("title")).strip()):
            continue
        out["reading"].append({
            "title": _s(item.get("title"), 300),
            "href": _safe_url(item.get("href")),
            "kind": _s(item.get("kind"), 40),
            "venue": _s(item.get("venue"), 200),
            "year": item.get("year") if isinstance(item.get("year"), int) else None,
            "why": _s(item.get("why"), 300),
            "level": _s(item.get("level"), 20) if _s(item.get("level")) in LEVELS else "",
        })
    return out


def validate(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Return (clean_spec, warnings). Never raises on bad content."""
    warnings: List[str] = []
    if not isinstance(raw, dict):
        return {"version": SPEC_VERSION, "sections": [], "resources": {"videos": [], "reading": []}}, \
            ["spec is not an object"]

    slug = _s(raw.get("slug"), 64).strip().lower()
    if not _SLUG_RE.match(slug):
        slug = re.sub(r"[^a-z0-9]+", "-", slug or "course").strip("-")[:64] or "course"
        warnings.append(f"slug normalised to {slug!r}")

    language = _s(raw.get("language"), 12).strip() or "en"
    default_level = _s(raw.get("default_level"), 20).strip().lower()
    if default_level not in LEVELS:
        default_level = "intermediate"

    provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    sources = []
    for src in _items(provenance.get("sources"), 40):
        if isinstance(src, dict) and _safe_url(src.get("url")):
            sources.append({"url": _safe_url(src.get("url")), "title": _s(src.get("title"), 300),
                            "used_in": [_s(u, 40) for u in _items(src.get("used_in"), 20)]})

    sections: List[Dict[str, Any]] = []
    used_ids: set = set()
    for index, raw_section in enumerate(_list(raw.get("sections"))):
        if not isinstance(raw_section, dict):
            continue
        sid = _s(raw_section.get("id"), 40).strip().lower()
        if not _SID_RE.match(sid):        # attribute-safe, or the anchor markup breaks
            sid = f"s{index + 1}"
        if sid in used_ids:               # duplicate ids break both anchors and page state
            base, suffix = sid, 2
            while f"{base}-{suffix}" in used_ids:
                suffix += 1
            sid = f"{base}-{suffix}"
        used_ids.add(sid)
        where = f"section {sid}"
        levels_raw = raw_section.get("levels") if isinstance(raw_section.get("levels"), dict) else {}
        levels: Dict[str, Any] = {}
        for level in LEVELS:
            body = levels_raw.get(level)
            blocks_raw = body.get("blocks") if isinstance(body, dict) else body
            blocks = _blocks(blocks_raw, f"{where}/{level}", warnings)
            levels[level] = {"blocks": blocks}
        if not any(levels[l]["blocks"] for l in LEVELS):
            warnings.append(f"{where}: no renderable blocks at any level; dropped")
            continue
        filled = [l for l in LEVELS if levels[l]["blocks"]]
        for level in LEVELS:
            if not levels[level]["blocks"]:
                warnings.append(f"{where}: level {level} empty, filled from {filled[0]}")
                levels[level] = {"blocks": list(levels[filled[0]]["blocks"])}
        raw_visual = raw_section.get("visual")
        section = {
            "id": sid,
            "title": _s(raw_section.get("title"), 300) or f"Section {index + 1}",
            "hook": _s(raw_section.get("hook"), 300),
            "eyebrow": _s(raw_section.get("eyebrow"), 80),
            "levels": levels,
            "exercise": _exercise(raw_section.get("exercise"), where, warnings),
            # the shared figure slot goes through the same coercion as a figure block: it
            # used to reach the renderer raw, so a malformed one drew nothing and said nothing
            "visual": (_figure_spec(raw_visual if isinstance(raw_visual, dict) else {},
                                    f"{where} figure", warnings) or None),
        }
        sections.append(section)

    spec = {
        "version": SPEC_VERSION,
        "slug": slug,
        "language": language,
        "title": _s(raw.get("title"), 200) or slug.replace("-", " ").title(),
        "subtitle": _s(raw.get("subtitle"), 300),
        "thesis": _s(raw.get("thesis"), 600),
        "outcomes": [_s(o, 200) for o in _items(raw.get("outcomes"), 6)],
        "default_level": default_level,
        "identity": raw.get("identity") if isinstance(raw.get("identity"), dict) else {},
        "provenance": {
            "researched": bool(provenance.get("researched")),
            "finders": [_s(x, 80) for x in _items(provenance.get("finders"), 6)],
            "readers": [_s(x, 80) for x in _items(provenance.get("readers"), 6)],
            "sources": sources,
            "failures": [_s(x, 300) for x in _items(provenance.get("failures"), 10)],
            "warnings": [_s(x, 300) for x in _items(provenance.get("warnings"), 10)],
            "generated": _s(provenance.get("generated"), 40),
        },
        "sections": sections,
        "resources": _resources(raw.get("resources")),
    }
    if not sections:
        warnings.append("course has no sections")
    return spec, warnings

