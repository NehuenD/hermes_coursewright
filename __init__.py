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

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

try:  # loaded as a package by the plugin loader
    from . import author, render as renderer, research as research_mod, spec as specmod
except ImportError:  # direct import during development
    import author  # type: ignore
    import render as renderer  # type: ignore
    import research as research_mod  # type: ignore
    import spec as specmod  # type: ignore

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent
_SAMPLES = _PLUGIN_DIR / "samples"
_MAX_SECTIONS_BUILT = 7

# Slash commands get 30 s; a build takes minutes. Builds run on worker threads, one per topic.
_BUILD_LOCK = threading.Lock()
_ACTIVE: dict = {}

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
        "spec_path to render an existing spec. Returns paths plus a ready ::preview directive."
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
    """Follow the prompt's language: a Spanish-looking topic writes a Spanish course."""
    if explicit:
        return explicit.strip()[:12]
    if re.search(r"[ñáéíóú¿¡]", topic, re.I):
        return "es"
    if re.search(r"\b(el|la|los|las|de|que|como|cómo|por|para|una?|y|es)\b", topic.lower()):
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

    last_identity = None
    try:
        last_identity = ctx.state.get("last_identity", default=None)
    except Exception:
        pass

    plan = author.plan_course(ctx, topic=topic, level=level, language=language,
                              avoid_identity=last_identity if isinstance(last_identity, dict) else None,
                              sections=_as_int(args.get("max_sections")),
                              forced_identity=tokens.assign(
                                  [row["identity"] for row in _inventory() if row.get("identity")]))
    plan["default_level"] = level
    plan["requested_topic"] = topic
    plan["planned_at"] = _now()

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
            made = make_plan(ctx, {"topic": topic, "level": level, "language": language})
            if not made.get("ok"):
                return made
            plan = _read_json(Path(made["plan_path"]))
        plan["default_level"] = level
        language = plan.get("language") or language

        record = None
        picks: dict = {"videos": [], "reading": []}
        if ctx.get_config("enable_research", default=True):
            want_videos = ctx.get_config("enable_videos", default=True)
            queries = author.video_queries(ctx, topic or plan.get("title") or "course", plan) \
                if want_videos else None
            record = research_mod.gather(
                ctx,
                topic=topic or plan.get("title") or "course",
                language=language,
                caps={"max_sources": ctx.get_config("max_sources", default=12),
                      "max_videos": ctx.get_config("max_videos", default=6) if want_videos else 0,
                      "timeout": 150},
                cache_dir=_store() / "cache",
                force=bool(args.get("refresh")),
                want_videos=bool(want_videos),
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
    clean["provenance"].setdefault("generated", _now())
    if origin == "model":
        clean["provenance"]["generated_by"] = "coursewright M2"

    # One course per topic. The model names its own slug per build, so the same topic
    # used to land in a new directory every time; an existing course for this topic wins.
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

    identity = renderer.tokens.resolve(clean.get("identity"))
    try:
        ctx.state.set("last_identity", identity)
    except Exception:
        pass

    result = {
        "ok": True,
        "slug": clean["slug"],
        "title": clean["title"],
        "sections": len(clean["sections"]),
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
        "preview": f'::preview{{file="{html_path}"}}',
    }
    _record_build({k: result[k] for k in ("ok", "slug", "title", "sections", "source",
                                          "researched", "bytes")})
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
    if isinstance(last, dict) and not last.get("ok"):
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
    page = render.render(probe)
    for needle in ("<!doctype html", 'class="seg"', "src-1"):
        if needle not in page.lower() and needle not in page:
            raise RuntimeError(f"renderer output missing {needle!r}")


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
        args: dict = {}
        words: list = []
        for token in (raw_args or "").split():
            if token.startswith("--level="):
                args["level"] = token.split("=", 1)[1]
            elif token.startswith("--lang="):
                args["language"] = token.split("=", 1)[1]
            elif token.startswith("--sections="):
                args["max_sections"] = _as_int(token.split("=", 1)[1])
            elif token.isdigit():
                args["max_sections"] = int(token)
            elif not token.startswith("-"):
                words.append(token)
        if words:  # the topic is the whole phrase, not its first word
            args["topic"] = " ".join(words)
        topic = str(args.get("topic") or "").strip()
        if not topic:
            return ("Usage: /course <topic> [--level=novice|intermediate|advanced] "
                    "[--lang=es] [--sections=6]")

        # A build takes minutes; slash-command dispatch gives up at 30 s. Start it in the
        # background and deliver the finished course as a message instead of blocking.
        key = _topic_key(topic)
        with _BUILD_LOCK:
            running = _ACTIVE.get(key)
            if running is not None and running.is_alive():
                return (f"Already building **{topic}**. It will appear here when it lands, "
                        f"or run `/courses` in a few minutes.")

            def _worker() -> None:
                try:
                    result = build_course(ctx, dict(args))
                    warn = (f" · {len(result['warnings'])} build note(s)"
                            if result.get("warnings") else "")
                    message = (f"**{result['title']}** is ready. {result['sections']} sections, "
                               f"opens at {result['level']}, {result['bytes'] // 1024} KB{warn}.\n"
                               f"`{result['html']}`\n\n{result['preview']}")
                except Exception as exc:  # a background failure must still reach the user
                    logger.warning("coursewright: background build failed: %s", exc, exc_info=True)
                    message = (f"Course build failed for \"{topic}\": "
                               f"{type(exc).__name__}: {exc}")
                finally:
                    with _BUILD_LOCK:
                        _ACTIVE.pop(key, None)
                try:
                    ctx.post_message(message)
                    logger.info("coursewright: posted build result for %r", topic)
                except Exception as exc:
                    logger.warning("coursewright: could not post build result: %s", exc)

            thread = threading.Thread(target=_worker, name=f"coursewright-{key[:24]}", daemon=True)
            _ACTIVE[key] = thread
        _record_build({"ok": None, "running": True, "topic": topic, "args": args})
        thread.start()
        level = args.get("level") or "intermediate"
        return (f"Building **{topic}** at {level} depth, about five minutes. "
                f"It will be posted here when it finishes; `/courses` lists what exists.")

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
            for row in rows:
                if row["slug"] == wanted:
                    return (f"**{row['title']}** — {row['sections']} sections, "
                            f"{row['sources']} sources, {row['videos']} videos, "
                            f"{row['bytes'] // 1024} KB, built {row['generated'] or 'unknown'}\n"
                            f"`{row['html']}`")
            return f"No course named {wanted!r}."
        lines = [f"**{len(rows)} course(s)**"]
        for row in rows:
            depth = f"{row['sections']} sections"
            src = f"{row['sources']} sources" if row["sources"] else "no sources"
            lines.append(f"- `{row['slug']}` — {row['title']} ({depth}, {src})")
        return "\n".join(head + lines)

    ctx.register_command("courses", _courses, description="List built courses",
                         args_hint="[slug]")
