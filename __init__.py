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

import asyncio
import concurrent.futures
import contextlib
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
_MAX_CONCURRENCY = 4

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
            "plan_path": {"type": "string", "description": "A plan JSON written by coursewright_plan, inside the plugin data directory (plans/<slug>.json)."},
            "spec_path": {"type": "string", "description": "A complete course spec JSON to render as-is, inside the plugin data directory (courses/<slug>/spec.json)."},
            "level": {"type": "string", "enum": ["novice", "intermediate", "advanced"]},
            "language": {"type": "string"},
            "max_sections": {"type": "integer",
                             "description": f"How many sections to draft (cap {_MAX_SECTIONS_BUILT})."},
            "refresh": {"type": "boolean",
                        "description": "Ignore the cached research for this topic and search again."},
            "background": {"type": "boolean",
                           "description": ("Start the build and return at once; the finished course is "
                                           "posted to this chat when it lands. Use this for a full "
                                           "course: a deep build takes longer than a tool call may run.")},
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


def _confined_json(raw: str, what: str):
    """Read a plan/spec only from inside the plugin's own data directory.

    These values are tool arguments, so they can be steered by injected text toward a secrets
    file. ``resolve()`` also collapses ``..`` and lands a symlink on its real target; the
    refusal happens before any file is opened. Relative paths are interpreted *inside* the
    store, and a relative spec/plan written by the plugin keeps working. Returns ``(doc, error)``.
    """
    root = _store().resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if path != root and root not in path.parents:
        return None, f"{what} must be a JSON file inside {root}; refusing {raw}"
    if not path.is_file() or path.suffix.lower() != ".json":
        return None, f"{what} is not an existing JSON file in {root}: {raw}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"{what} is not readable JSON: {exc}"
    if not isinstance(doc, dict):
        return None, f"{what} must contain a JSON object"
    return doc, ""


def _write(path: Path, text: str) -> None:
    """Write through a temp file: a crash mid-write must not leave a half course page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _store() -> Path:
    from plugins.plugin_storage import plugin_data_dir  # public plugin surface

    return plugin_data_dir("coursewright")


@contextlib.contextmanager
def _build_slot(slug: str):
    """A cross-process guard for one course directory.

    `_RUNNING` only sees this process: a CLI build and a gateway build of the same topic both
    thought they owned the directory and overwrote each other's page and spec. The lock file
    is advisory and non-blocking — the second build is told to wait, not queued behind a
    twenty-minute one.
    """
    handle = None
    try:
        import fcntl

        lock_dir = _store() / "state"
        lock_dir.mkdir(parents=True, exist_ok=True)
        handle = open(lock_dir / f"{slug}.lock", "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    except ImportError:                      # not POSIX: the in-process guard is all there is
        yield True
    finally:
        if handle is not None:
            handle.close()


def _secret(name: str) -> str:
    """A credential, read the way Hermes requires: through the profile-aware secret scope.

    A bare `os.environ` read is what the catalog's credential rule forbids — under
    multiplexing it can hand back another profile's key. The import is guarded because this
    module also runs under the test harness, where `agent` is not on the path.
    """
    try:
        from agent.secret_scope import get_secret
    except Exception:                       # no host scope (tests, standalone import)
        return ""
    try:
        return str(get_secret(name, "") or "").strip()
    except Exception as exc:                # an unscoped read fails closed by design
        logger.warning("coursewright: %s unavailable: %s", name, exc)
        return ""


def _caps(ctx, *, want_research: bool = True, want_videos: bool = True) -> dict:
    """Research settings, including which scraper to use, as research.gather expects them."""
    def _setting(key, default):
        try:
            value = ctx.get_config(key, default=default)
        except Exception:
            return default
        return default if value is None else value

    key = str(_setting("firecrawl_key", "") or "").strip() or _secret("FIRECRAWL_API_KEY")
    timeout = _as_int(_setting("research_timeout", 150)) or 150
    return {
        "max_sources": _as_int(_setting("max_sources", 12)) if want_research else 0,
        "max_videos": _as_int(_setting("max_videos", 6)) if want_videos else 0,
        "timeout": max(30, min(timeout, 900)),
        "firecrawl_url": str(_setting("firecrawl_url", "") or ""),
        "firecrawl_key": key.strip(),
    }


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
# identity rotation
# --------------------------------------------------------------------------
# Six palettes and five type pairings only make a varied set if each build knows what the
# others spent. Two builds planning at the same moment read the same inventory and pick the
# same combination, so the choice is claimed on disk under a lock before the model is asked:
# a concurrent plan then sees the claim and takes the next free combination.
_IDENTITY_CLAIM_TTL = 45 * 60.0


def _identity_claim_dir() -> Path:
    return _store() / "state" / "identity-claims"


@contextlib.contextmanager
def _identity_slot():
    """One writer at a time. Timing out is better than refusing to plan at all."""
    import fcntl

    handle = None
    held = False
    try:
        folder = _store() / "state"
        folder.mkdir(parents=True, exist_ok=True)
        handle = open(folder / "identity.lock", "a+")
    except OSError:
        yield
        return
    try:
        deadline = time.time() + 60.0
        while time.time() < deadline:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                time.sleep(0.2)
        if not held:
            logger.warning("coursewright: identity lock busy; assigning without it")
        yield
    finally:
        try:
            if held:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _identity_history() -> List[dict]:
    """Every identity already spent: built courses, saved plans, and live claims.

    Plans count because a plan is written before its course exists; claims count because
    nothing is on disk yet while the model writes that plan.
    """
    inventory = _inventory()
    rows = [row["identity"] for row in inventory
            if isinstance(row.get("identity"), dict) and row["identity"].get("palette")]
    taken = {row.get("slug") for row in inventory}
    try:
        plans = sorted((_store() / "plans").glob("*.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        plans = []
    for path in plans:
        if path.stem in taken:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        identity = document.get("identity")
        if isinstance(identity, dict) and identity.get("palette"):
            rows.append(identity)
    try:
        claims = sorted(_identity_claim_dir().glob("*.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        claims = []
    for path in claims:
        try:
            if time.time() - path.stat().st_mtime > _IDENTITY_CLAIM_TTL:
                path.unlink(missing_ok=True)      # a crashed plan should not colour later ones
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        identity = document.get("identity")
        if isinstance(identity, dict) and identity.get("palette"):
            rows.append(identity)
    return rows


def _claim_identity() -> dict:
    """Pick the least-used combination and write the claim before the long model call."""
    with _identity_slot():
        identity = tokenmod.assign(_identity_history())
        try:
            folder = _identity_claim_dir()
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{os.getpid()}-{int(time.time() * 1000)}-{threading.get_ident()}.json"
            _write(folder / name, json.dumps({"identity": identity, "at": _now()}))
        except OSError as exc:
            logger.warning("coursewright: could not record the identity claim: %s", exc)
    return identity


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

    try:
        plan = author.plan_course(ctx, topic=topic, level=level, language=language,
                                  # The identity is assigned here to keep the course set visually
                                  # varied; asking the model to both use it and avoid the last one
                                  # put two contradictory instructions in the same prompt.
                                  avoid_identity=None,
                                  sections=_as_int(args.get("max_sections")),
                                  forced_identity=_claim_identity())
    except author.ModelUnavailable as exc:
        return {"ok": False,
                "error": (f"the host would not run the model ({exc}). A course needs model access: "
                          f"check that this plugin is trusted for ctx.llm and that a model is "
                          f"configured.")}
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
    total = min(max_sections, len(plan["sections"]))
    concurrency = _draft_concurrency(ctx)
    if concurrency > 1 and hasattr(getattr(ctx, "llm", None), "acomplete_structured"):
        return _draft_parallel(ctx, plan, language, total, sources, concurrency)
    sections = []
    for index in range(total):
        logger.info("coursewright: drafting %s/%s — %s", index + 1, total,
                    plan["sections"][index]["title"])
        sections.append(author.draft_section(ctx, plan, index, language=language, sources=sources))
    return sections


def _draft_concurrency(ctx) -> int:
    """How many sections may be written at once; 1 is the one-at-a-time path."""
    try:
        wanted = _as_int(ctx.get_config("draft_concurrency", default=3))
    except Exception:
        return 1
    return max(1, min(_MAX_CONCURRENCY, wanted or 1))


def _draft_parallel(ctx, plan: dict, language: str, total: int, sources: str,
                    concurrency: int) -> list:
    """Draft `total` sections with at most `concurrency` model calls in flight.

    Sections are written independently: each one gets its brief and the previous brief's
    summary from the plan, never the previous *drafted* section, so the order of completion
    cannot change what any section says. The results are put back in plan order by index.
    """
    async def run() -> list:
        gate = asyncio.Semaphore(concurrency)

        async def one(index: int):
            async with gate:
                logger.info("coursewright: drafting %s/%s — %s", index + 1, total,
                            plan["sections"][index]["title"])
                return await author.draft_section_async(ctx, plan, index, language=language,
                                                        sources=sources)

        results = await asyncio.gather(*(one(index) for index in range(total)),
                                       return_exceptions=True)
        # A refused model has to stop the build the way it does one section at a time, and
        # the other calls have to be finished (or failed) rather than left running.
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return list(results)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    # A build can be started from inside the gateway's event loop; the fan-out gets its own
    # loop on a helper thread rather than trying to nest one.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(run())).result()


def _hand_to_agent(ctx, message: str, session_key: str, preview_path: str, topic: str) -> bool:
    """Put the finished course into the conversation as a turn; a notification cannot.

    `post_message` on a local desktop session publishes a sticky notification and writes no
    message, which is why a build that ended there was reported and never seen. The agent's own
    reply is an assistant message, and an assistant message is where `::preview` renders the
    page, so the result is handed over as a turn and the notification stays as the fallback.
    """
    if _surface(ctx) not in {"desktop", "cli", "tui", "acp", ""}:
        return False  # a messaging chat gets a real message from post_message; don't inject
    instruction = ("Reply with one short line: the course title, how many sections it has, and "
                   "where the file is.")
    if preview_path:
        instruction += (" Then put the preview directive on a line of its own, in the form"
                        f' ::preview{{file="{preview_path}"}} and outside any code span, so the'
                        " page opens beside this chat.")
    else:
        instruction += " Say what failed and stop there."
    text = ("[/course] A background build finished. This is the plugin handing you the result,\n"
            "not a message from the user.\n\n"
            f"{message.strip()}\n\n"
            f"{instruction}\n\n"
            "Call no tools, and do not repeat the rest of this message.")
    try:
        return bool(ctx.inject_message(text, role="user", session_key=session_key))
    except Exception as exc:  # a refusal lands on the notification below
        logger.warning("coursewright: could not hand %r to the agent: %s", topic, exc)
        return False


def _spawn_build(ctx, args: dict, guard: str, topic: str, session_key: str = "") -> dict:
    """Run the build on a worker thread and post the result when it lands.

    A deep course (eight sections, three depths each) runs for longer than a tool call is
    allowed to block, so the caller gets an immediate answer and the chat gets the finished
    course as a message. The caller must already own *guard*; this releases it.
    """
    key = _topic_key(topic) or guard

    def _worker() -> None:
        preview_path = ""
        try:
            result = _build_course(ctx, dict(args))
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "the build refused"))
            preview_path = str(result.get("html") or "")
            message = (f"Your course is ready: \"{result['title']}\", "
                       f"{result['sections']} sections, {result['bytes'] // 1024} KB, "
                       f"opens at {result['level']}.\n"
                       f"{result['mirror'] or result['html']}"
                       + (f"\n\n{result['preview']}" if result.get("preview") else "")
                       + (f"\n\nOpen it with: {result['open']}" if result.get("open") else ""))
        except Exception as exc:  # a background failure must still reach the user
            logger.warning("coursewright: background build failed: %s", exc, exc_info=True)
            _record_build({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                           "topic": topic, "args": dict(args)})
            message = f"The course on \"{topic}\" failed: {type(exc).__name__}: {exc}"
        finally:
            with _BUILD_LOCK:
                _ACTIVE.pop(key, None)
            _release_build(guard)
        # A local desktop session has no transport of its own: `post_message` publishes a
        # sticky notification and writes no message, which is why a finished course arrived as
        # a toast and never in the chat. Only the agent's turn produces an assistant message,
        # and that message is where `::preview` renders the page beside the chat, so the
        # handoff is the first attempt and the notification is the fallback.
        if _hand_to_agent(ctx, message, session_key, preview_path, topic):
            return
        handle = None
        try:
            handle = ctx.post_message(message)
        except Exception as exc:
            logger.warning("coursewright: post unavailable (%s) for %r", exc, topic)
        if handle is not None and getattr(handle, "ok", False):
            logger.info("coursewright: posted build result for %r", topic)
            return
        if handle is not None:
            logger.warning("coursewright: post refused (%s) for %r",
                           getattr(handle, "error", "unknown"), topic)

    # post_message resolves the chat through a contextvar bound by the dispatcher;
    # a bare thread does not inherit it, so carry the context across.
    thread = threading.Thread(target=contextvars.copy_context().run, args=(_worker,),
                              name=f"coursewright-{key[:24]}", daemon=True)
    with _BUILD_LOCK:
        _ACTIVE[key] = thread
    _record_build({"ok": None, "running": True, "topic": topic, "args": dict(args)})
    try:
        thread.start()
    except Exception:  # a thread that cannot start must not hold the topic forever
        with _BUILD_LOCK:
            _ACTIVE.pop(key, None)
        _release_build(guard)
        raise
    return {"ok": True, "started": True, "background": True, "topic": topic,
            "slug": None, "sections": 0,
            "message": (f"Building \"{topic}\" in the background. It takes roughly one to two "
                        f"minutes per section at this depth; the finished course will be posted "
                        f"here, and `/courses` shows what is on disk meanwhile.")}


def build_course(ctx, args: dict) -> dict:
    """One build per topic at a time; a second caller is refused instead of racing the first."""
    topic = str(args.get("topic") or "").strip()
    guard = _topic_key(topic) or _slug(str(args.get("plan_path") or args.get("spec_path") or "course"))
    if not _claim_build(guard):
        return {"ok": False,
                "error": f"a build for {topic or guard!r} is already running here; wait for it to "
                         f"finish, or run /courses to see what is on disk"}
    if args.get("background"):
        return _spawn_build(ctx, args, guard, topic, _calling_session_key({}))
    try:
        return _build_course(ctx, args)
    finally:
        _release_build(guard)


def _surface(ctx) -> str:
    """`cli`/`tui`/`desktop`/`acp`, a messaging platform, or "" outside a session."""
    try:
        return str(ctx.session_surface() or "")
    except Exception:
        return ""


def _open_in_pane(ctx, path: Path, title: str) -> bool:
    """Show the finished page in the desktop's preview pane beside the chat.

    The pane belongs to one surface, so it is only asked for there. Dispatching it from a
    Telegram chat or a CLI session used to fail on every build; worse, the `::preview`
    directive that replaced it is desktop-only syntax that those surfaces print verbatim.
    """
    if _surface(ctx) not in {"desktop", ""}:
        return False
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


def _figure_scenes(spec: dict) -> list:
    """Every diagram/sequence figure in a spec, as (name, .excalidraw scene) pairs.

    The page draws these as SVG. The scenes are the editable hand-off: a reader who wants to
    move a box can open one at excalidraw.com. A figure block is normally repeated at all
    three depths, so scenes are de-duplicated by content, and named for the slot they came
    from.
    """
    scenes, seen = [], set()
    for section in spec.get("sections") or []:
        if not isinstance(section, dict):
            continue
        sid = str(section.get("id") or "section")
        slots = [(sid, section.get("visual"))]
        for level in ("novice", "intermediate", "advanced"):
            blocks = ((section.get("levels") or {}).get(level) or {}).get("blocks") or []
            for index, block in enumerate(blocks):
                if isinstance(block, dict) and block.get("kind") == "figure":
                    slots.append((f"{sid}-{level}-{index + 1}", block.get("spec")))
        for name, figure in slots:
            if not isinstance(figure, dict):
                continue
            if str(figure.get("type") or "").strip().lower() not in {"diagram", "sequence"}:
                continue
            key = json.dumps(figure, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            scene = renderer.scene_from_figure(figure, str(figure.get("caption") or ""))
            if scene:
                scenes.append((name, scene))
    return scenes


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
    sections: list = []

    if spec_path:                                     # 1. a finished spec
        raw, path_error = _confined_json(spec_path, "spec_path")
        if raw is None:
            return {"ok": False, "error": path_error or "spec_path is not a JSON object"}
        origin = spec_path
    elif not plan_path and (_SAMPLES / f"{_slug(topic)}.json").exists():
        raw = _read_json(_SAMPLES / f"{_slug(topic)}.json")   # 2. a bundled sample
        origin = f"sample:{_slug(topic)}"
    else:                                             # 3. plan -> research -> draft
        if plan_path:
            plan, path_error = _confined_json(plan_path, "plan_path")
            if plan is None:
                return {"ok": False, "error": path_error or "plan_path is not a JSON object"}
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
                caps=_caps(ctx, want_research=want_research, want_videos=want_videos),
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

    if origin == "model" and sections and all(section.get("_thin") for section in sections):
        # eight placeholder sections are worse than an error: nothing was learned, and the
        # earlier code called this "ok" and wrote the page anyway
        return {"ok": False,
                "error": (f"the model did not answer any section"
                          + (f" ({author.last_error()})" if author.last_error() else "")),
                "warnings": ["no section was drafted; nothing was written"]}

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
    scenes = _figure_scenes(clean)
    with _build_slot(clean["slug"]) as owned:
        if not owned:
            return {"ok": False,
                    "warnings": warnings,
                    "error": (f"another process is writing \"{clean['slug']}\" right now; "
                              f"wait for it to finish, or run /courses to see what is on disk")}
        _write(html_path, page)
        _write(store / "spec.json", json.dumps(clean, indent=2, ensure_ascii=False))
        for name, scene in scenes:
            _write(store / "figures" / f"{name}.excalidraw",
                   json.dumps(scene, indent=2, ensure_ascii=False))
        if origin == "model" and record is not None:
            _write(store / "research.json", json.dumps(record, indent=2, ensure_ascii=False))

    mirror = ""
    mirror_root = str(ctx.get_config("courses_dir", default="~/Courses") or "").strip()
    if mirror_root:
        try:
            target = Path(mirror_root).expanduser() / clean["slug"] / "index.html"
            _write(target, page)
            for name, scene in scenes:
                _write(target.parent / "figures" / f"{name}.excalidraw",
                       json.dumps(scene, indent=2, ensure_ascii=False))
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
    surface = _surface(ctx)
    # The pane tool resolves the window of the turn that asked, so a background build has no
    # window to show it in; there the result is handed to the agent as a turn instead, and that
    # message carries the `::preview` directive.
    pane_open = (False if args.get("background")
                 else _open_in_pane(ctx, Path(mirror) if mirror else html_path, clean["title"]))

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
        "diagrams": len(scenes),
        "warnings": warnings,
        # `::preview{...}` is desktop-only syntax: anywhere else it is a line of text the user
        # has to delete, so those surfaces get the path and a command instead
        "preview": (f'::preview{{file="{html_path}"}}'
                    if (not pane_open and surface in {"desktop", ""}) else ""),
        "pane": pane_open,
        "surface": surface,
        "open": (f"open \"{mirror or html_path}\"" if surface not in {"desktop", ""} else ""),
        # a re-render from a saved spec has no research record in hand, so the counts have to
        # come off the spec: reporting 0 sources for a page whose footer lists nine is a lie
        # the agent would repeat back to the user
        "readers": (record or {}).get("readers") or clean["provenance"]["readers"],
        "sources": len((record or {}).get("sources") or []) or len(clean["provenance"]["sources"]),
        "videos": len((record or {}).get("videos") or []) or len(clean["resources"]["videos"]),
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
    "description": ("Report the last coursewright build (ok or error, counts, timing), what this "
                    "host can actually do (search, readers, scraper, videos, preview surface) and "
                    "what is on disk: courses, research cache entries, saved plans. Pass "
                    "probe=true to also test the model with a tiny call."),
    "parameters": {"type": "object", "properties": {
        "probe": {"type": "boolean",
                  "description": "Also make one tiny model call to prove inference works."}}},
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
    with _BUILD_LOCK:
        running = sorted(name for name, thread in _ACTIVE.items() if thread.is_alive())
        claimed = sorted(key for key in _RUNNING if key not in _ACTIVE)
    environment = {}
    try:
        environment = research_mod.describe(ctx, _caps(ctx))
    except Exception as exc:                      # a probe must never break the report
        environment = {"error": f"{type(exc).__name__}: {exc}"}
    environment["surface"] = _surface(ctx) or "none"
    environment["store"] = str(_store())
    if args.get("probe"):
        try:
            ctx.llm.complete(messages=[{"role": "user", "content": "ping"}], max_tokens=4,
                             timeout=30, purpose="coursewright.probe")
            environment["model"] = {"ok": True, "detail": "answered"}
        except Exception as exc:
            environment["model"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    else:
        environment["model"] = {"ok": None, "detail": "not probed; pass probe=true to test it"}
    payload = {
        "ok": True,
        "environment": environment,
        "last_build": last,
        "running": running,
        "claimed": claimed,
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
        "provenance": {"researched": True, "finders": ["web_search"], "readers": ["web_extract"],
                       "sources": [{"url": "https://example.org/self", "title": "Self check"}],
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
That call returns as soon as the build starts (a full course takes longer than a tool call may
block); the plugin posts the finished course into this chat itself.

Reply with one short line: what the course will cover, and that it is building now. Do not call
the tool again and do not wait for the file. If it returns ok:false, say plainly why and stop;
do not retry with a different topic or a different tool."""


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
    call.append("background=true")
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

        # Fallback when no session can be woken (no gateway, no live chat): build on a worker
        # and post the result. Refused posts are injected, and a failed build is recorded so
        # coursewright_status can say what happened.
        logger.info("coursewright: no session to hand %r to; building in the background", topic)
        if not _claim_build(key):
            return (f"Already building **{topic}**. It will appear here when it lands, "
                    f"or run `/courses` in a few minutes.")
        return _spawn_build(ctx, dict(args), key, topic, session_key)["message"]

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
