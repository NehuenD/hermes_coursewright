"""coursewright — generate a beautiful, interactive HTML course on any subject.

M1: a topic becomes a course. PLAN (one cheap call) -> DRAFT (one call per section)
-> validate -> render -> write. The model fills a structured spec; `render.py` draws it.

Tools:
    coursewright_plan   topic -> the skeleton, for approval before the expensive part
    coursewright_build  topic | plan_path | spec_path -> the course file

Command:
    /course <topic> [--level=novice|intermediate|advanced] [--lang=es]
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

try:  # loaded as a package by the plugin loader
    from . import author, render as renderer, research as research_mod, spec as specmod
    from . import tokens as tokenmod
except ImportError:  # direct import during development
    import author  # type: ignore
    import render as renderer  # type: ignore
    import research as research_mod  # type: ignore
    import spec as specmod  # type: ignore
    import tokens as tokenmod  # type: ignore

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent
_SAMPLES = _PLUGIN_DIR / "samples"
# The planner is allowed eight sections, so the builder must allow eight too: capping the
# build at seven silently dropped the closing section every plan was told to write.
_MAX_SECTIONS_BUILT = 8

# Slash commands get 30 s; a build takes minutes. Builds run on worker threads, one per topic.
_BUILD_LOCK = threading.Lock()
_ACTIVE: dict = {}
# Topic keys with a build in flight in this process (whatever path started it: the tool in an
# agent turn, or a background fallback thread). Two concurrent builds of one topic would race
# for the same store directory, so the second is refused instead of half-overwriting the first.
_RUNNING: set = set()
# Topic keys handed to the agent by /course that have not reached the tool yet. Without this
# the guard has a gap: /course hands the work off in a few milliseconds, but nothing owns the
# topic until the agent's turn calls the tool, so two quick /course calls both dispatched.
_HANDOFFS: dict = {}
_HANDOFF_TTL = 15 * 60


def _claim_build(key: str) -> bool:
    """True when this caller now owns the build for *key*; False when one is already running."""
    with _BUILD_LOCK:
        if key in _RUNNING:
            return False
        _RUNNING.add(key)
        return True


def _release_build(key: str) -> None:
    with _BUILD_LOCK:
        _RUNNING.discard(key)
        _HANDOFFS.pop(key, None)


def _note_handoff(key: str) -> None:
    """Record that /course handed *key* to the agent, so a second /course is refused."""
    with _BUILD_LOCK:
        _HANDOFFS[key] = time.monotonic()


def _handoff_pending(key: str) -> bool:
    """True while a handed-off build for *key* has not reached the tool yet."""
    with _BUILD_LOCK:
        started = _HANDOFFS.get(key)
        if started is None:
            return False
        if time.monotonic() - started > _HANDOFF_TTL:  # the agent never ran it
            _HANDOFFS.pop(key, None)
            return False
        return True

PLAN_SCHEMA = {
    "name": "coursewright_plan",
    "description": (
        "Plan a course before building it: title, one-sentence thesis, learning outcomes, "
        "6-8 section briefs and the page identity. Cheap (one model call). Show the plan to "
        "the user; then call coursewright_build with plan_path to write the whole thing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "The subject to teach."},
            "level": {"type": "string", "enum": ["novice", "intermediate", "advanced"],
                      "description": "Which depth the course opens at."},
            "language": {"type": "string",
                         "description": "BCP-47-ish code (en, es). Defaults to the topic's language."},
            "max_sections": {"type": "integer", "minimum": 2, "maximum": 8,
                             "description": "How many sections to plan (2-8, default 6-8 by depth)."},
        },
        "required": ["topic"],
    },
}

BUILD_SCHEMA = {
    "name": "coursewright_build",
    "description": (
        "Build the course and write one self-contained interactive HTML page: three depths per "
        "section with an in-place toggle, figures, exercises, and an honest provenance footer. "
        "Give it a topic (plans and writes in one go), a plan_path from coursewright_plan, or a "
        "spec_path to render an existing spec. Opens the page in the desktop preview pane when "
        "there is one; otherwise returns a ready ::preview directive to paste."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Subject to build (when no plan_path/spec_path)."},
            "plan_path": {"type": "string", "description": "A plan JSON written by coursewright_plan."},
            "spec_path": {"type": "string", "description": "A complete course spec JSON to render as-is."},
            "level": {"type": "string", "enum": ["novice", "intermediate", "advanced"]},
            "language": {"type": "string"},
            "max_sections": {"type": "integer",
                             "description": f"How many sections to draft (cap {_MAX_SECTIONS_BUILT})."},
            "refresh": {"type": "boolean",
                        "description": "Ignore the cached research for this topic and search again."},
        },
        "required": [],
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:64] or "course"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _store() -> Path:
    from plugins.plugin_storage import plugin_data_dir  # public plugin surface

    return plugin_data_dir("coursewright")


def _guess_language(topic: str, explicit: str) -> str:
    """Follow the prompt's language: a Spanish-looking topic writes a Spanish course.

    One shared function word is not evidence — "de Broglie wavelength" is French, "la Niña"
    and "es Vedra" are English-language topics. Accents settle it; otherwise Spanish needs two
    markers and at least one that English rarely uses as a word.
    """
    if explicit:
        return explicit.strip()[:12]
    if re.search(r"[ñáéíóú¿¡]", topic, re.I):
        return "es"
    words = set(re.findall(r"[a-záéíóúñ]+", (topic or "").lower()))
    markers = {"el", "la", "los", "las", "un", "una", "de", "del", "que", "qué", "como",
               "cómo", "por", "para", "es", "son", "y", "en", "con"}
    strong = {"que", "qué", "como", "cómo", "para", "del", "los", "las", "es", "son", "y"}
    if len(words & markers) >= 2 and words & strong:
        return "es"
    return "en"


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def _topic_key(topic: str) -> str:
    """Stable identity for a requested topic, so a rebuild lands in the same course."""
    words = re.findall(r"[a-z0-9]+", (topic or "").lower())
    return "-".join(sorted(set(words)))[:120]


def _reuse_slug(slug: str, topic_key: str) -> str:
    """If a course already exists for this topic, keep its directory instead of adding one."""
    root = _store() / "courses"
    try:
        existing = sorted(root.iterdir(),
                          key=lambda p: (p / "spec.json").stat().st_mtime
                          if (p / "spec.json").is_file() else 0, reverse=True)
    except OSError:
        return slug
    for candidate in existing:
        spec_file = candidate / "spec.json"
        if not spec_file.is_file():
            continue
        try:
            stored = json.loads(spec_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _topic_key(stored.get("topic_key") or stored.get("title") or "") == topic_key:
            return candidate.name
    return slug


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def make_plan(ctx, args: dict) -> dict:
    topic = str(args.get("topic") or "").strip()
    if not topic:
        return {"ok": False, "error": "topic is required"}
    level = str(args.get("level") or ctx.get_config("default_level", default="intermediate"))
    language = _guess_language(topic, str(args.get("language") or ""))

    plan = author.plan_course(ctx, topic=topic, level=level, language=language,
                              # The identity is assigned here to keep the course set visually
                              # varied; asking the model to both use it and avoid the last one
                              # put two contradictory instructions in the same prompt.
                              avoid_identity=None,
                              sections=_as_int(args.get("max_sections")),
                              forced_identity=tokenmod.assign(
                                  [row["identity"] for row in _inventory() if row.get("identity")]))
    plan["default_level"] = level
    plan["requested_topic"] = topic
    plan["planned_at"] = _now()
    # the model is asked for exactly N sections and sometimes returns more; the plan a user
    # approves has to be the plan the build will follow
    wanted = _as_int(args.get("max_sections"))
    if wanted and isinstance(plan.get("sections"), list) and len(plan["sections"]) > wanted > 0:
        plan["sections"] = plan["sections"][:wanted]

    path = _store() / "plans" / f"{_slug(plan.get('slug') or topic)}.json"
    _write(path, json.dumps(plan, indent=2, ensure_ascii=False))
    return {
        "ok": True,
        "plan_path": str(path),
        "title": plan["title"],
        "subtitle": plan.get("subtitle", ""),
        "thesis": plan.get("thesis", ""),
        "outcomes": plan.get("outcomes", []),
        "sections": [{"title": s["title"], "hook": s.get("hook", "")} for s in plan["sections"]],
        "identity": plan["identity"],
        "language": language,
        "degraded": plan.get("degraded"),
    }


# --------------------------------------------------------------------------
# draft + build
# --------------------------------------------------------------------------
def _draft_all(ctx, plan: dict, language: str, max_sections: int, sources: str = "") -> list:
    sections = []
    total = min(max_sections, len(plan["sections"]))
    for index in range(total):
        logger.info("coursewright: drafting %s/%s — %s", index + 1, total,
                    plan["sections"][index]["title"])
        sections.append(author.draft_section(ctx, plan, index, language=language, sources=sources))
    return sections


def build_course(ctx, args: dict) -> dict:
    """One build per topic at a time; a second caller is refused instead of racing the first."""
    topic = str(args.get("topic") or "").strip()
    guard = _topic_key(topic) or _slug(str(args.get("plan_path") or args.get("spec_path") or "course"))
    if not _claim_build(guard):
        return {"ok": False,
                "error": f"a build for {topic or guard!r} is already running here; wait for it to "
                         f"finish, or run /courses to see what is on disk"}
    try:
        return _build_course(ctx, args)
    finally:
        _release_build(guard)


def _open_in_pane(ctx, path: Path, title: str) -> bool:
    """Show the finished page in the desktop's preview pane beside the chat.

    Best effort by design: the tool belongs to the desktop surface, so on any other
    surface (Telegram, plain CLI, a headless run) the dispatch answers with an error and
    the caller falls back to the inline `::preview` directive it returns instead.
    """
    try:
        raw = ctx.dispatch_tool("desktop_preview",
                                {"action": "open", "url": str(path), "label": str(title)[:40]})
    except Exception as exc:  # an older host without the tool, or no emitter wired
        logger.info("coursewright: could not open the preview pane: %s", exc)
        return False
    try:
        answer = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return False
    if isinstance(answer, dict) and answer.get("error"):
        logger.info("coursewright: preview pane refused the page: %s", answer.get("error"))
        return False
    return True


def _build_course(ctx, args: dict) -> dict:
    topic = str(args.get("topic") or "").strip()
    plan_path = str(args.get("plan_path") or "").strip()
    spec_path = str(args.get("spec_path") or "").strip()
    level = str(args.get("level") or ctx.get_config("default_level", default="intermediate"))
    language = _guess_language(topic, str(args.get("language") or ""))
    try:
        max_sections = int(args.get("max_sections") or _MAX_SECTIONS_BUILT)
    except (TypeError, ValueError):
        max_sections = _MAX_SECTIONS_BUILT
    max_sections = max(2, min(max_sections, _MAX_SECTIONS_BUILT))
    record = None

    if spec_path:                                     # 1. a finished spec
        raw = _read_json(Path(spec_path).expanduser())
        origin = spec_path
    elif not plan_path and (_SAMPLES / f"{_slug(topic)}.json").exists():
        raw = _read_json(_SAMPLES / f"{_slug(topic)}.json")   # 2. a bundled sample
        origin = f"sample:{_slug(topic)}"
    else:                                             # 3. plan -> research -> draft
        if plan_path:
            plan = _read_json(Path(plan_path).expanduser())
        else:
            # The same section count has to reach the planner, or the plan describes eight
            # sections and the course renders two of them.
            made = make_plan(ctx, {"topic": topic, "level": level, "language": language,
                                   "max_sections": _as_int(args.get("max_sections"))})
            if not made.get("ok"):
                return made
            plan = _read_json(Path(made["plan_path"]))
        # a saved plan remembers the depth it was approved for; only an explicit request
        # overrides it, the session config default must not
        plan["default_level"] = str(plan.get("default_level") or level)
        if args.get("level"):
            plan["default_level"] = level
        language = plan.get("language") or language
        # A plan that ignored the requested count would leave the course describing sections
        # it does not contain, so the requested count wins over the model's answer.
        if isinstance(plan.get("sections"), list) and len(plan["sections"]) > max_sections:
            logger.info("coursewright: plan had %d sections; keeping the %d requested",
                        len(plan["sections"]), max_sections)
            plan["sections"] = plan["sections"][:max_sections]

        record = None
        picks: dict = {"videos": [], "reading": []}
        # Research and videos are separate switches: a course with enable_research off but
        # enable_videos on still gets its watch list, so the two settings do not quietly
        # disable each other.
        want_research = bool(ctx.get_config("enable_research", default=True))
        want_videos = bool(ctx.get_config("enable_videos", default=True))
        if want_research or want_videos:
            queries = author.video_queries(ctx, topic or plan.get("title") or "course", plan) \
                if want_videos else None
            record = research_mod.gather(
                ctx,
                topic=topic or plan.get("title") or "course",
                language=language,
                caps={"max_sources": ctx.get_config("max_sources", default=12) if want_research else 0,
                      "max_videos": ctx.get_config("max_videos", default=6) if want_videos else 0,
                      "timeout": 150},
                cache_dir=_store() / "cache",
                force=bool(args.get("refresh")),
                want_videos=want_videos,
                video_queries=queries,
            )
            picks = author.pick_resources(ctx, plan, record)

        sections = _draft_all(ctx, plan, language, max_sections,
                              research_mod.source_block(record))
        raw = author.assemble(plan, sections, topic=topic or plan.get("title") or "course",
                              research=record, picks=picks)
        origin = "model"

    if args.get("level") in {"novice", "intermediate", "advanced"}:
        raw["default_level"] = args["level"]

    clean, warnings = specmod.validate(raw)
    if not clean.get("provenance", {}).get("generated"):
        # validate always emits the key, so setdefault never fired: a course built with
        # research off shipped without a date
        clean["provenance"]["generated"] = _now()
    if origin == "model":
        clean["provenance"]["generated_by"] = "coursewright M2"

    # A page with a cover and no sections is not a course. Refusing here keeps a broken spec
    # from being recorded as a successful build.
    if not clean.get("sections"):
        return {"ok": False,
                "warnings": warnings,
                "error": ("the spec produced no renderable sections"
                          + (f" ({warnings[0]})" if warnings else ""))}

    # One course per topic. The model names its own slug per build, so the same topic
    # used to land in a new directory every time; an existing course for this topic wins.
    # A spec re-render keeps the identity it was built with, or the next topic build would
    # not recognise it as the same course.
    if isinstance(raw, dict) and str(raw.get("topic_key") or "").strip():
        clean["topic_key"] = str(raw["topic_key"]).strip()[:120]
    else:
        clean.setdefault("topic_key", _topic_key(topic or clean["title"]))
    clean["slug"] = _reuse_slug(clean["slug"], clean["topic_key"])

    page = renderer.render(clean)

    store = _store() / "courses" / clean["slug"]
    html_path = store / "index.html"
    _write(html_path, page)
    _write(store / "spec.json", json.dumps(clean, indent=2, ensure_ascii=False))
    if origin == "model" and record is not None:
        _write(store / "research.json", json.dumps(record, indent=2, ensure_ascii=False))

    mirror = ""
    mirror_root = str(ctx.get_config("courses_dir", default="~/Courses") or "").strip()
    if mirror_root:
        try:
            target = Path(mirror_root).expanduser() / clean["slug"] / "index.html"
            _write(target, page)
            mirror = str(target)
        except OSError as exc:
            warnings.append(f"mirror copy failed: {exc}")

    identity = tokenmod.resolve(clean.get("identity"))
    try:
        ctx.state.set("last_identity", identity)
    except Exception:
        pass

    # The page is tuned for the pane's width, so the pane is where it belongs: open it there
    # and let the caller skip the inline directive. Falls back to the directive when the
    # surface has no pane. The visible mirror is the friendlier path for the tab (a stable
    # location outside plugin-data) and re-opening the same URL reuses the tab.
    pane_open = _open_in_pane(ctx, Path(mirror) if mirror else html_path, clean["title"])

    # how much explanation each depth actually carries: a thin build should be visible
    # without opening the page
    words = {level: sum(renderer._level_words(section, level) for section in clean["sections"])
             for level in ("novice", "intermediate", "advanced")}
    result = {
        "ok": True,
        "slug": clean["slug"],
        "title": clean["title"],
        "sections": len(clean["sections"]),
        "words": words,
        "level": clean["default_level"],
        "language": clean["language"],
        "identity": identity,
        "html": str(html_path),
        "spec": str(store / "spec.json"),
        "mirror": mirror,
        "source": origin,
        "researched": clean["provenance"]["researched"],
        "bytes": len(page.encode("utf-8")),
        "warnings": warnings,
        "preview": "" if pane_open else f'::preview{{file="{html_path}"}}',
        "pane": pane_open,
    }
    _record_build({k: result[k] for k in ("ok", "slug", "title", "sections", "source",
                                          "researched", "bytes", "words")})
    return result


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------
LIST_SCHEMA = {
    "name": "coursewright_list",
    "description": "List the courses coursewright has built, newest first, with counts and file paths.",
    "parameters": {"type": "object", "properties": {}},
}

STATUS_SCHEMA = {
    "name": "coursewright_status",
    "description": ("Report the last coursewright build (ok or error, counts, timing) and what is on "
                    "disk: courses, research cache entries, saved plans."),
    "parameters": {"type": "object", "properties": {}},
}


def _inventory() -> List[dict]:
    """Every built course on disk, newest first. Reads specs, never the renderer."""
    rows: List[dict] = []
    root = _store() / "courses"
    try:
        candidates = [p for p in root.iterdir() if (p / "spec.json").is_file()]
    except OSError:
        return rows
    for path in candidates:
        try:
            spec_doc = json.loads((path / "spec.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        prov = spec_doc.get("provenance") or {}
        resources = spec_doc.get("resources") or {}
        html = path / "index.html"
        rows.append({
            "slug": path.name,
            "title": spec_doc.get("title") or path.name,
            "sections": len(spec_doc.get("sections") or []),
            "sources": len(prov.get("sources") or []),
            "videos": len(resources.get("videos") or []),
            "reading": len(resources.get("reading") or []),
            "level": spec_doc.get("default_level") or "",
            "identity": spec_doc.get("identity") or {},
            "generated": prov.get("generated") or "",
            "researched": bool(prov.get("researched")),
            "bytes": html.stat().st_size if html.is_file() else 0,
            "html": str(html),
            "_mtime": (path / "spec.json").stat().st_mtime,
        })
    rows.sort(key=lambda row: row["_mtime"], reverse=True)
    for row in rows:
        row.pop("_mtime", None)
    return rows


def list_courses(ctx, args: dict) -> dict:
    rows = _inventory()
    return {"ok": True, "count": len(rows), "store": str(_store() / "courses"), "courses": rows}


def course_status(ctx, args: dict) -> dict:
    """The last build plus an inventory, so a failure is diagnosable without the gateway log."""
    last = None
    try:
        last = json.loads((_store() / "state" / "last-build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    def _count(pattern: str) -> int:
        try:
            return len(list((_store() / pattern.split("/")[0]).glob(pattern.split("/")[1])))
        except OSError:
            return 0
    rows = _inventory()
    payload = {
        "ok": True,
        "last_build": last,
        "courses": len(rows),
        "newest": rows[0]["slug"] if rows else None,
        "plans": _count("plans/*.json"),
        "research_cache_entries": _count("cache/*.json"),
        "store": str(_store()),
    }
    # ok is None while a build is handed off or in flight; only an explicit False is a failure
    if isinstance(last, dict) and last.get("ok") is False:
        payload["hint"] = ("The last build failed. Re-run with the same topic; a failure here is "
                           "recorded with its exception, not swallowed.")
    return payload


def _record_build(payload: dict) -> None:
    try:
        _write(_store() / "state" / "last-build.json",
               json.dumps(dict(payload, at=_now()), indent=2, ensure_ascii=False))
    except OSError:
        pass


def _selfcheck() -> None:
    """Exercise the two fragile paths at load time: prompt building and rendering.

    A prompt/template bug used to surface as a ValueError 30 s into a build; here it
    surfaces as a warning the moment the plugin is enabled.
    """
    plan = {"title": "t", "thesis": "t", "sections": [{"title": "t", "teaches": "t"}]}
    prompt, _payload = author._section_prompt(plan, 0, 1, "en", "[1] source")
    if '{"kind"' not in prompt:
        raise RuntimeError("section prompt lost its JSON example")
    probe = {
        "slug": "selfcheck", "title": "Self check", "subtitle": "", "thesis": "",
        "outcomes": ["say what a residual stream is"],
        "language": "en", "default_level": "intermediate",
        "identity": {"palette": "ink-copper", "type_pairing": "humanist-serif",
                     "layout": "single", "motion": "restrained", "rationale": ""},
        "provenance": {"researched": False, "finders": [], "readers": [], "sources": [],
                       "failures": [], "warnings": [], "generated": ""},
        "resources": {"videos": [], "reading": []},
        "sections": [{
            "id": "s1", "title": "Self check", "eyebrow": "check", "hook": "",
            "levels": {level: {"blocks": [{"kind": "prose", "md": "x_{1} and [1]."}]}
                       for level in ("novice", "intermediate", "advanced")},
            "exercise": {"kind": "mcq", "prompt": "q", "choices": ["a", "b", "c", "d"],
                         "answer": 0, "why": "w"},
        }],
    }
    page = renderer.render(probe)
    for needle in ("<!doctype html", 'class="seg"', "src-1"):
        if needle not in page.lower() and needle not in page:
            raise RuntimeError(f"renderer output missing {needle!r}")


_LEVELS = ("novice", "intermediate", "advanced")


def _parse_course_args(raw_args: str) -> dict:
    """`/course how a zipper works --level=novice --sections=6` -> the args build_course expects.

    A bare number is never a section count: `/course how 7 bridges of Königsberg work` used to
    lose its "7" from the topic and ask for seven sections.
    """
    args: dict = {}
    words: list = []
    for token in (raw_args or "").split():
        if token.startswith("--level="):
            level = token.split("=", 1)[1].strip().lower()
            if level in _LEVELS:
                args["level"] = level
        elif token.startswith("--lang="):
            args["language"] = token.split("=", 1)[1]
        elif token.startswith("--sections="):
            args["max_sections"] = _as_int(token.split("=", 1)[1])
        elif token in {"--refresh", "-r"}:
            args["refresh"] = True
        elif not token.startswith("-"):
            words.append(token)
    if words:  # the topic is the whole phrase, not its first word
        args["topic"] = " ".join(words)
    return args


def _level_of(args: dict) -> str:
    level = str(args.get("level") or "").strip().lower()
    return level if level in _LEVELS else "intermediate"


def _calling_session_key(kwargs: dict) -> str:
    """The calling chat's routing key: the dispatcher's keyword first, the contextvar second,
    the process environment only as a last resort (it can hold another session's id)."""
    key = str((kwargs or {}).get("session_key") or "").strip()
    if key:
        return key
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env("HERMES_SESSION_KEY")
                   or get_session_env("HERMES_SESSION_ID") or "").strip()
    except Exception:  # older layout: the env vars are all there is
        return str(os.environ.get("HERMES_SESSION_KEY")
                   or os.environ.get("HERMES_SESSION_ID") or "").strip()


_HANDOFF = """[/course] The /course command parsed this request from the chat.

Build the course on {topic} at {level} depth: call coursewright_build exactly once with
{call}
While it runs, say one line that it is building — the tool call is where the progress shows.

When it returns: two sentences on what the course actually covers, then the file path. If the
result carries a non-empty "preview" field, put that directive alone on its own line; if it is
empty the course is already open in the preview pane beside the chat, so do not paste a
directive of your own. If it returns ok:false, say plainly why and
stop; do not retry with a different topic or a different tool."""


def _handoff_text(topic: str, args: dict) -> str:
    """The instruction a handed-off `/course` turn runs: explicit tool, explicit arguments."""
    call = [f"topic={json.dumps(topic, ensure_ascii=False)}",
            f"level={json.dumps(_level_of(args))}"]
    sections = _as_int(args.get("max_sections"))
    if sections:
        call.append(f"max_sections={sections}")
    if str(args.get("language") or "").strip():
        call.append(f"language={json.dumps(str(args['language']).strip())}")
    if args.get("refresh"):
        call.append("refresh=true")
    return _HANDOFF.format(topic=json.dumps(topic, ensure_ascii=False), level=_level_of(args),
                           call=", ".join(call))


def register(ctx) -> None:
    try:
        _selfcheck()
    except Exception as exc:  # a plugin that cannot build its own prompt is not loadable
        logger.error("coursewright: self-check failed: %s", exc, exc_info=True)
        raise

    def _wrap(fn):
        def handler(args, **kwargs):
            try:
                return json.dumps(fn(ctx, args or {}), ensure_ascii=False)
            except Exception as exc:  # a tool handler must return JSON, never raise
                logger.warning("coursewright: %s failed: %s", getattr(fn, "__name__", "?"), exc,
                               exc_info=True)
                if getattr(fn, "__name__", "") == "build_course":
                    _record_build({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return handler

    ctx.register_tool(
        name="coursewright_plan",
        toolset="coursewright",
        schema=PLAN_SCHEMA,
        handler=_wrap(make_plan),
        description="Plan a course before building it",
        emoji="🗺️",
    )
    ctx.register_tool(
        name="coursewright_build",
        toolset="coursewright",
        schema=BUILD_SCHEMA,
        handler=_wrap(build_course),
        description="Build an interactive HTML course",
        emoji="📖",
    )

    def _course(raw_args: str, **kwargs) -> str:
        args = _parse_course_args(raw_args)
        topic = str(args.get("topic") or "").strip()
        if not topic:
            return ("Usage: /course <topic> [--level=novice|intermediate|advanced] "
                    "[--lang=es] [--sections=6]")

        # The dispatcher passes the calling chat's routing key; os.environ is process-wide and
        # may hold another session's id, so it is only the last resort.
        session_key = _calling_session_key(kwargs)
        key = _topic_key(topic)
        with _BUILD_LOCK:
            running = _ACTIVE.get(key)
            building = running is not None and running.is_alive()
            in_flight = key in _RUNNING
            pending_handoff = _HANDOFFS.get(key) is not None
        if pending_handoff:
            # a handed-off build may not have reached the tool yet; ask the honest question
            pending_handoff = _handoff_pending(key)
        if building or in_flight or pending_handoff:
            return (f"Already building **{topic}**. It will appear here when it lands, "
                    f"or run `/courses` in a few minutes.")

        # First choice: hand the job to the agent as a turn. A slash command that only returns
        # text ends the turn there: the desktop prints that text, the session goes idle with no
        # model reply, and a build finishing minutes later has no live conversation left to
        # report into. The turn is what makes the work — and its result — visible.
        handed_off = False
        if session_key:
            try:
                handed_off = bool(ctx.inject_message(_handoff_text(topic, args), role="user",
                                                     session_key=session_key))
            except Exception as exc:
                logger.warning("coursewright: could not hand %r to the agent: %s", topic, exc)
        if handed_off:
            logger.info("coursewright: /course handed %r to the agent", topic)
            _note_handoff(key)
            _record_build({"ok": None, "running": True, "handed_to_agent": True,
                           "topic": topic, "args": dict(args)})
            return (f"On it — building \"{topic}\" at {_level_of(args)} depth as the next turn, so "
                    f"the work and the finished course both show up here. `/courses` lists what "
                    f"already exists in the meantime.")

        # Fallback when no session can be woken (no gateway, no live chat): build in the
        # background and post the result. Refused posts are injected, and a failed build is
        # recorded so coursewright_status can say what happened.
        logger.info("coursewright: no session to hand %r to; building in the background", topic)

        def _worker() -> None:
            try:
                result = build_course(ctx, dict(args))
                if not result.get("ok"):
                    raise RuntimeError(str(result.get("error") or "the build refused"))
                message = (f"Your course is ready: \"{result['title']}\", "
                           f"{result['sections']} sections, {result['bytes'] // 1024} KB, "
                           f"opens at {result['level']}.\n"
                           f"{result['html']}"
                           + (f"\n\n{result['preview']}" if result.get("preview") else ""))
            except Exception as exc:  # a background failure must still reach the user
                logger.warning("coursewright: background build failed: %s", exc, exc_info=True)
                _record_build({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                               "topic": topic, "args": dict(args)})
                message = (f"The course on \"{topic}\" failed: {type(exc).__name__}: {exc}")
            finally:
                with _BUILD_LOCK:
                    _ACTIVE.pop(key, None)
            # Post first: it appends the message without pretending to be user input.
            handle = ctx.post_message(message)
            if getattr(handle, "ok", False):
                logger.info("coursewright: posted build result for %r", topic)
                return
            logger.warning("coursewright: post refused (%s) for %r; injecting instead",
                           getattr(handle, "error", "unknown"), topic)
            try:  # a refused post still has to reach the user somehow
                ctx.inject_message(message, role="user", session_key=session_key)
            except Exception as exc:
                logger.warning("coursewright: inject fallback failed: %s", exc)

        # post_message resolves the chat through a contextvar bound by the dispatcher;
        # a bare thread does not inherit it, so carry the context across.
        thread = threading.Thread(target=contextvars.copy_context().run, args=(_worker,),
                                  name=f"coursewright-{key[:24]}", daemon=True)
        with _BUILD_LOCK:
            _ACTIVE[key] = thread
        _record_build({"ok": None, "running": True, "topic": topic, "args": dict(args)})
        thread.start()
        return (f"Building \"{topic}\" at {_level_of(args)} depth. It takes about five minutes, "
                f"and the course will appear here as soon as it is done. /courses lists what "
                f"exists in the meantime.")

    ctx.register_command("course", _course, description="Build an interactive course on a topic",
                         args_hint="<topic> [--level=novice|intermediate|advanced]")

    ctx.register_tool(
        name="coursewright_list",
        toolset="coursewright",
        schema=LIST_SCHEMA,
        handler=_wrap(list_courses),
        description="List built courses",
        emoji="📚",
    )
    ctx.register_tool(
        name="coursewright_status",
        toolset="coursewright",
        schema=STATUS_SCHEMA,
        handler=_wrap(course_status),
        description="Report the last build and what is on disk",
        emoji="🩺",
    )

    def _courses(args):
        rows = _inventory()
        with _BUILD_LOCK:
            building = [t for t in _ACTIVE.values() if t.is_alive()]
        head = []
        if building:
            head.append(f"Building {len(building)} course(s) now. "
                        f"The result will be posted when it finishes.")
        if not rows:
            head.append("No courses yet. `/course <topic>` builds one.")
            return "\n".join(head)
        wanted = (args or "").strip()
        if wanted:
            # exact slug first, then a unique substring: `/courses zipper` should find
            # `zipper-moving-wedge` instead of claiming nothing is there
            matches = [row for row in rows if row["slug"] == wanted] or [
                row for row in rows if wanted.lower() in row["slug"].lower()]
            if len(matches) == 1:
                row = matches[0]
                return (f"**{row['title']}** — {row['sections']} sections, "
                        f"{row['sources']} sources, {row['videos']} videos, "
                        f"{row['bytes'] // 1024} KB, built {row['generated'] or 'unknown'}\n"
                        f"`{row['html']}`")
            if len(matches) > 1:
                return ("More than one course matches {0!r}: ".format(wanted)
                        + ", ".join(f"`{row['slug']}`" for row in matches))
            return f"No course named {wanted!r}."
        lines = [f"**{len(rows)} course(s)**"]
        for row in rows:
            depth = f"{row['sections']} sections"
            src = f"{row['sources']} sources" if row["sources"] else "no sources"
            lines.append(f"- `{row['slug']}` — {row['title']} ({depth}, {src})")
        return "\n".join(head + lines)

    ctx.register_command("courses", _courses, description="List built courses",
                         args_hint="[slug]")
