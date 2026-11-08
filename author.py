"""Authoring: turn a topic into a course spec through the host's own model.

Two stages, both through `ctx.llm` (host-owned credentials, no keys in the plugin):

    PLAN   -> one cheap call: title, thesis, outcomes, 6-8 section briefs, identity
    DRAFT  -> one call per section: three levels of prose, an exercise, an optional figure

The model never writes HTML. Everything it returns is validated by `spec.py` before
`render.py` draws it, and a section that fails to arrive is replaced by a thin one
carrying a warning rather than sinking the whole build.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from . import tokens
except ImportError:  # direct import during development
    import tokens  # type: ignore

logger = logging.getLogger(__name__)

MAX_SECTIONS = 8
_SECTION_TOKENS = 2200
_PLAN_TOKENS = 1400

BLOCK_MENU = """Available block kinds (use several different ones; never invent a kind):
- {"kind":"prose","md":"2-4 sentences"}
- {"kind":"analogy","term":"Think of it as","md":"a short comparison that maps cleanly onto the real thing"}
- {"kind":"definition","term":"the term","md":"a precise one-or-two-sentence definition"}
- {"kind":"callout","tone":"insight|honesty|warning","title":"short label","md":"one short paragraph"}
- {"kind":"steps","items":["first","second","third"]}
- {"kind":"comparison","left":"A","right":"B","items":["a1","b1","a2","b2"]}  (items alternate left,right)
- {"kind":"timeline","items":["1937 — something happened","1993 — something else"]}
- {"kind":"math","md":"plain text math: _x or _{...} for a subscript, ^x or ^{...} for a superscript. No LaTeX, no dollar signs, no HTML"}
- {"kind":"code","lang":"python","code":"short, runnable, commented"}
- {"kind":"aside","md":"one sentence of nuance"}
- {"kind":"checkpoint","md":"one sentence: what the reader should now be able to do"}
- {"kind":"figure","caption":"what it shows","spec":{FIGURES}}"""

FIGURE_MENU = """{"type":"flow","steps":["token id","embedding","attention head","feed-forward","logits"]}
       Best for any process, pipeline, architecture or causal chain: 3-6 short steps, two or
       three words each. No data needed. A step label may carry a symbol: W_Q multiplies, or
       write W_{Q} for a multi-character subscript.
       or {"type":"bars","points":[{"label":"2019","value":12},{"label":"2024","value":31}],"xlabel":"year","ylabel":"count"}
       or {"type":"line","points":[{"label":"0","value":1},{"label":"1","value":4}]}
       A course should carry at least one figure when the subject has a natural structure or
       process. Data charts only for numbers you genuinely have — never invent data."""

LEVEL_RULES = """LEVEL RULES — this is the whole point of the course. The three versions must NOT be
the same text at different lengths.

novice:
- No jargon at all. If a technical word is unavoidable, explain it in the same sentence.
- Lead with a concrete image or analogy, not a definition.
- Short sentences. Short paragraphs. 2-3 blocks.
- A reader with no background must be able to follow it out loud.

intermediate:
- The real terms, each introduced the moment the reader needs it (definition block).
- The mechanism: why it works, not just what it does. 2-4 blocks.
- One honest limit or edge case.

advanced:
- Proper terminology used without apology; precise statements.
- Name the standard results and who owns them when you are sure. Say plainly what is
  contested or open (a callout with tone "honesty" is a good home for that).
- 2-4 blocks. This is where math/code/comparison belong."""

HARD_RULES = """HARD RULES:
- Never invent a citation, URL, DOI, page number, ISBN, or direct quotation. Naming a
  well-known result and its author is fine; a fake reference is not.
- Math is plain text, in every block and in figure labels: a subscript is an underscore after
  the base (W_Q), a superscript a caret (K^T), and a multi-character script is wrapped in curly
  braces. Never LaTeX, never dollar signs, never HTML — anything else prints literally.
- No block may exceed 70 words. A section may not exceed 240 words per level.
- Write in {language}.
- Do not mention that you are an AI, do not address the reader's level, do not write
  "in this section we will" — just teach."""

_EXERCISE_RULES = """Exercise: exactly one per section. Choose ONE kind:
- {"kind":"mcq","prompt":"...","choices":["A","B","C","D"],"answer":0,"why":"why the right one is right, one or two sentences"}
  (exactly 4 choices, answer is the 0-based index of the correct one, the distractors must
  be plausible mistakes, not nonsense)
- {"kind":"recall","prompt":"...","rubric":["keyword1","keyword2","keyword3"],"answer":"a model answer in 2-3 sentences"}
  (rubric = 3-6 single words a good answer would contain; keep them lowercase)"""

_IDENTITY_MENU = """Choose the page identity. Allowed values only:
palette: {palettes}
type_pairing: {types}
layout: {layouts}
motion: {motifs}"""


def _catalog() -> Dict[str, str]:
    return {
        "palettes": ", ".join(tokens.PALETTES),
        "types": ", ".join(tokens.TYPE_PAIRINGS),
        "layouts": ", ".join(tokens.LAYOUTS),
        "motifs": ", ".join(tokens.MOTIONS),
    }


def _dump(index: int, label: str, payload: Any) -> None:
    """Debug aid: COURSEWRIGHT_DUMP=<dir> writes each section's raw model reply."""
    try:
        root = Path(os.environ["COURSEWRIGHT_DUMP"]).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{index + 1:02d}-{label}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _extract_json(text: str) -> Optional[Any]:
    """Pull the first complete JSON object out of a model reply (fences tolerated)."""
    if not text:
        return None
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start = raw.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(raw[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except ValueError:
                    return None
    return None


def _ask(ctx, *, instructions: str, payload: str, purpose: str, max_tokens: int,
         json_mode: bool = True) -> Optional[Any]:
    """One structured call through the host model; None on any failure (never raises)."""
    try:
        result = ctx.llm.complete_structured(
            instructions=instructions,
            input=[{"type": "text", "text": payload}],
            json_mode=json_mode,
            purpose=purpose,
            temperature=0.4,
            max_tokens=max_tokens,
            timeout=180,
        )
    except Exception as exc:  # trust gate, provider error, timeout
        logger.warning("coursewright: %s call failed: %s", purpose, exc)
        return None
    parsed = getattr(result, "parsed", None)
    if parsed is not None:
        return parsed
    return _extract_json(getattr(result, "text", "") or "")


# --------------------------------------------------------------------------
# stage 1: plan
# --------------------------------------------------------------------------
_PLAN_PROMPT = """You are designing a short, beautiful course on one subject. You are not
writing the course yet — you are writing its skeleton, so someone can approve the angle
before the expensive part.

Return ONE JSON object:
{
  "slug": "kebab-case-id",
  "title": "max 8 words, concrete, no dash clause",
  "subtitle": "one line that makes someone want to read (the intriguing half goes here, not in the title)",
  "thesis": "the single sentence the whole course argues or reveals, max 30 words",
  "outcomes": ["verb-first, 4 max", "..."],
  "sections": [
    {"title": "...", "eyebrow": "2-3 words", "hook": "one line: why this section matters",
     "teaches": "one sentence: the one thing this section establishes"}
  ],
  "identity": {"palette": "...", "type_pairing": "...", "layout": "...", "motion": "...",
               "rationale": "half a sentence: why this visual mood fits the subject"}
}

Rules:
- 6 to 8 sections. They must build: each one uses what the previous established.
- Section 1 must assume zero background. The last section must open a door (what is
  still unknown, or where to go next) rather than summarise.
- Titles are concrete images or questions ("A game with two moves"), never "Overview",
  "Introduction", "Conclusion".
- Order by understanding, not by history: no timeline section unless the history IS the
  mechanism.
- No URLs, no citations, no page numbers, no invented statistics.

{identity_menu}

{hard_rules}"""


def plan_course(ctx, *, topic: str, level: str = "intermediate", language: str = "en",
                avoid_identity: Optional[Dict[str, str]] = None,
                sections: Optional[int] = None,
                forced_identity: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Stage 1. Returns a validated plan dict (never raises)."""
    requested = sections if isinstance(sections, int) and 2 <= sections <= MAX_SECTIONS else None
    identity_extra = ""
    if forced_identity:
        identity_extra += ("\nUse exactly this identity; it was assigned to keep the course set "
                           f"visually varied: palette={forced_identity.get('palette')}, "
                           f"type_pairing={forced_identity.get('type_pairing')}, "
                           f"layout={forced_identity.get('layout')}, "
                           f"motion={forced_identity.get('motion')}.\n")
    if avoid_identity:
        identity_extra += (f"\nDo not reuse this identity: palette={avoid_identity.get('palette')}, "
                          f"type_pairing={avoid_identity.get('type_pairing')}, "
                          f"layout={avoid_identity.get('layout')}. Pick a different combination.\n")
    instructions = (_PLAN_PROMPT
                    .replace("{identity_menu}", _IDENTITY_MENU.format(**_catalog()))
                    .replace("{hard_rules}", HARD_RULES.format(language=language))
                    + identity_extra)
    payload = f"Topic: {topic}\nOpens at depth: {level}\nLanguage: {language}\n"
    if requested:
        payload += f"Plan exactly {requested} sections.\n"

    plan = _ask(ctx, instructions=instructions, payload=payload, purpose="coursewright.plan",
                max_tokens=_PLAN_TOKENS)
    if not isinstance(plan, dict) or not isinstance(plan.get("sections"), list):
        return _fallback_plan(topic, language)

    planned = []
    for item in plan["sections"][:MAX_SECTIONS]:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        planned.append({
            "title": str(item.get("title"))[:120],
            "eyebrow": str(item.get("eyebrow") or "")[:40],
            "hook": str(item.get("hook") or "")[:200],
            "teaches": str(item.get("teaches") or "")[:300],
        })
    if len(planned) < min(3, requested or 3):
        return _fallback_plan(topic, language)

    identity = tokens.resolve(forced_identity or plan.get("identity"))
    title = str(plan.get("title") or topic)[:200]
    subtitle = str(plan.get("subtitle") or "")[:300]
    if not subtitle:  # models like to smuggle the hook into the title behind a dash
        for sep in (" — ", " – ", " - ", ": "):
            head, found, tail = title.partition(sep)
            if found and 1 <= len(head.split()) <= 9 and len(tail.split()) >= 3:
                title, subtitle = head.strip(), tail.strip()[:300]
                break
    return {
        "slug": str(plan.get("slug") or topic).strip().lower(),
        "title": title,
        "subtitle": subtitle,
        "thesis": str(plan.get("thesis") or "")[:600],
        "outcomes": [str(o)[:200] for o in (plan.get("outcomes") or [])][:5],
        "sections": planned,
        "identity": identity,
        "language": language,
    }


def _fallback_plan(topic: str, language: str) -> Dict[str, Any]:
    """A plan that still renders when the model call fails: honest, minimal, flagged."""
    return {
        "slug": re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:64] or "course",
        "title": topic,
        "subtitle": "",
        "thesis": "",
        "outcomes": [],
        "sections": [{"title": topic, "eyebrow": "", "hook": "",
                      "teaches": f"an introduction to {topic}"}],
        "identity": tokens.resolve({}),
        "language": language,
        "degraded": "The planning call failed; this plan is a placeholder.",
    }


# --------------------------------------------------------------------------
# stage 2: one section at a time
# --------------------------------------------------------------------------
def _section_prompt(plan: Dict[str, Any], index: int, total: int, language: str,
                    sources: str = "") -> tuple[str, str]:
    section = plan["sections"][index]
    guide = "\n".join(f"{i + 1}. {s['title']} — {s['teaches']}" for i, s in enumerate(plan["sections"]))
    sources_block = ""
    if sources:
        sources_block = f"""
REAL SOURCES (numbered; each excerpt is actual text from that page):
{sources}

CITATION RULES:
- When a claim comes from one of these sources, put its number in square brackets at the end
  of the sentence: attention is quadratic in sequence length [2].
- Cite only what the source actually supports. Never invent a number not in the list.
- The advanced level should cite; the novice level should teach, not argue, so it usually
  carries no citations at all.
- If a source contradicts what you were going to write, follow the source.
"""
    # A plain template + explicit tokens, not an f-string: prompts are full of JSON
    # examples, and brace escaping differs enough across interpreters to be a trap.
    template = """You are writing section <<N>> of <<TOTAL>> of a course called
"<<TITLE>>". The course's argument: <<THESIS>>

The whole course, in order (do not write the other sections):
<<GUIDE>>
<<SOURCES>>
Write THIS section: "<<SECTION>>" — its job is: <<TEACHES>>

Return ONE JSON object:
{
  "id": "s<<N>>",
  "title": "<<SECTION>>",
  "eyebrow": "keep or improve the 2-3 word label",
  "hook": "one line that earns the next two minutes",
  "visual": null or a figure (see below),
  "levels": {
    "novice": {"blocks": [ ... ]},
    "intermediate": {"blocks": [ ... ]},
    "advanced": {"blocks": [ ... ]}
  },
  "exercise": { ... }
}

REQUIRED STRUCTURE FOR THIS SECTION:
- This section needs exactly one figure, in the top-level "visual" field:
  {"type":"flow","steps":["step","step","step"],"caption":"what it shows"}
  The figure is SHARED by all three depths, so put it here and not inside a level's blocks —
  a novice must be able to see it too.
  Choose the figure this section actually needs:
  * a process, pipeline, architecture or causal chain -> {"type":"flow","steps":["step","step","step"]}
  * real numbers you are sure of -> {"type":"bars","points":[{"label":"x","value":1},...]}
  * a conceptual distinction with no process and no numbers -> a flow of 3-5 concept labels
    (a flow of ideas is a valid figure; never invent data to fill space)

<<LEVEL_RULES>>

<<BLOCK_MENU>>

<<EXERCISE_RULES>>
This section's exercise MUST be kind "<<EXERCISE_KIND>>" — the mix across the course is fixed,
so give this one the kind it is told to have.

<<HARD_RULES>>

Use 3 or more different block kinds across the three levels of this section. Vary the figure
type between sections: do not give every section the same kind of figure."""
    instructions = (template
                    .replace("<<N>>", str(index + 1))
                    .replace("<<TOTAL>>", str(total))
                    .replace("<<TITLE>>", str(plan["title"]))
                    .replace("<<THESIS>>", str(plan["thesis"] or plan["title"]))
                    .replace("<<GUIDE>>", guide)
                    .replace("<<SOURCES>>", sources_block)
                    .replace("<<SECTION>>", str(section["title"]))
                    .replace("<<TEACHES>>", str(section["teaches"]))
                    .replace("<<LEVEL_RULES>>", LEVEL_RULES)
                    .replace("<<BLOCK_MENU>>", BLOCK_MENU.replace("{FIGURES}", FIGURE_MENU))
                    .replace("<<EXERCISE_RULES>>", _EXERCISE_RULES)
                    .replace("<<EXERCISE_KIND>>", _exercise_kind(index))
                    .replace("<<HARD_RULES>>", HARD_RULES.replace("{language}", language)))
    payload = f"Course: {plan['title']}\nSection {index + 1}/{total}: {section['title']}\n{section.get('hook', '')}\n"
    return instructions, payload


def _exercise_kind(index: int) -> str:
    """Alternate kinds by section: a course where every exercise is MCQ never uses the
    grading path at all, and a course of all free-recall has no instant feedback."""
    return "recall" if index % 2 else "mcq"


def draft_section(ctx, plan: Dict[str, Any], index: int, *, language: str = "en",
                  sources: str = "", retry_errors: Optional[List[str]] = None) -> Dict[str, Any]:
    """Stage 2 for one section. Always returns a section dict."""
    total = len(plan["sections"])
    instructions, payload = _section_prompt(plan, index, total, language, sources)
    if retry_errors:
        payload += ("\nYour previous answer had these problems; fix them and return the "
                    "whole JSON object again:\n- " + "\n- ".join(retry_errors[:6]))

    raw = _ask(ctx, instructions=instructions, payload=payload,
               purpose=f"coursewright.section.{index + 1}", max_tokens=_SECTION_TOKENS)
    if os.environ.get("COURSEWRIGHT_DUMP"):
        _dump(index, "raw", raw)
    section = _coerce_section(raw, plan, index)
    if os.environ.get("COURSEWRIGHT_DUMP"):
        _dump(index, "coerced", section)
    problems = _section_problems(section)
    required_kind = _exercise_kind(index)
    got_kind = (section.get("exercise") or {}).get("kind")
    if got_kind and got_kind != required_kind:
        problems.append(f"this section's exercise must be a {required_kind}, not a {got_kind}")
    if problems and not retry_errors:
        logger.info("coursewright: retrying section %s (%s)", index + 1, problems[:2])
        return draft_section(ctx, plan, index, language=language, sources=sources,
                             retry_errors=problems)
    if problems:
        section.setdefault("_warnings", []).extend(problems)
    return section


def _coerce_section(raw: Any, plan: Dict[str, Any], index: int) -> Dict[str, Any]:
    brief = plan["sections"][index]
    if not isinstance(raw, dict):
        return _thin_section(brief, index, "the drafting call returned nothing usable")
    section = {
        "id": f"s{index + 1}",
        "title": str(raw.get("title") or brief["title"])[:300],
        "eyebrow": str(raw.get("eyebrow") or brief.get("eyebrow") or "")[:80],
        "hook": str(raw.get("hook") or brief.get("hook") or "")[:300],
        "levels": raw.get("levels") if isinstance(raw.get("levels"), dict) else {},
        "exercise": raw.get("exercise"),
        "visual": raw.get("visual") if isinstance(raw.get("visual"), dict) else None,
    }
    filled = [lvl for lvl in ("novice", "intermediate", "advanced")
              if isinstance(section["levels"].get(lvl), dict)
              and (section["levels"][lvl].get("blocks") or isinstance(section["levels"][lvl], list))]
    if not filled:
        return _thin_section(brief, index, "no level bodies came back")
    for level in ("novice", "intermediate", "advanced"):
        if level not in filled:
            section["levels"][level] = dict(section["levels"][filled[0]])
            section.setdefault("_warnings", []).append(f"{level} was empty; copied from {filled[0]}")
    return section


def _thin_section(brief: Dict[str, Any], index: int, why: str) -> Dict[str, Any]:
    body = {"blocks": [{"kind": "prose", "md": brief.get("teaches") or brief["title"]}]}
    return {
        "id": f"s{index + 1}",
        "title": brief["title"],
        "eyebrow": brief.get("eyebrow", ""),
        "hook": brief.get("hook", ""),
        "levels": {"novice": dict(body), "intermediate": dict(body), "advanced": dict(body)},
        "_warnings": [f"section {index + 1} was rebuilt thin: {why}"],
    }


def _section_problems(section: Dict[str, Any]) -> List[str]:
    """Cheap structural checks worth one retry."""
    problems: List[str] = []
    for level in ("novice", "intermediate", "advanced"):
        body = section.get("levels", {}).get(level) or {}
        blocks = body.get("blocks") if isinstance(body, dict) else body
        if not isinstance(blocks, list) or not blocks:
            problems.append(f"{level}: no blocks")
            continue
        kinds = {str((b or {}).get("kind", "")).strip() for b in blocks if isinstance(b, dict)}
        if not kinds:
            problems.append(f"{level}: blocks have no kinds")
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("kind") == "comparison" and len(block.get("items") or []) % 2:
                problems.append(f"{level}: comparison items must pair up (left,right,left,right)")
    exercise = section.get("exercise")
    if not isinstance(exercise, dict):
        problems.append("missing exercise")
    elif str(exercise.get("kind")) == "mcq":
        if len(exercise.get("choices") or []) != 4:
            problems.append("mcq needs exactly 4 choices")
        if not isinstance(exercise.get("answer"), int):
            problems.append("mcq needs an integer answer index")
    elif str(exercise.get("kind")) == "recall" and not (exercise.get("rubric") or []):
        problems.append("recall needs a rubric list")
    has_figure = any(
        isinstance(b, dict) and b.get("kind") == "figure"
        for level in ("novice", "intermediate", "advanced")
        for b in ((section.get("levels", {}).get(level) or {}).get("blocks") or [])
    )
    if not has_figure and not section.get("visual"):
        problems.append('no figure — put exactly one in the top-level "visual" field, '
                        'e.g. {"type":"flow","steps":[...],"caption":"..."}')
    return problems


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
def _flow_from_steps(section: Dict[str, Any]) -> bool:
    """Give a section a figure drawn from its own content when the model skipped one.

    Only real material is used: a `steps` block the model already wrote becomes a flow
    figure (same words, structural rendering). No invented data, no invented steps.
    """
    levels = section.get("levels") or {}
    if section.get("visual"):
        return False
    if any(isinstance(b, dict) and b.get("kind") == "figure"
           for body in levels.values() for b in ((body or {}).get("blocks") or [])):
        return False
    for level in ("intermediate", "novice", "advanced"):
        blocks = (levels.get(level) or {}).get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("kind") != "steps":
                continue
            items = [re.sub(r"^[\s\-\*\d.)]+", "", str(item)).strip()[:26]
                     for item in (block.get("items") or [])]
            items = [i for i in items if i][:5]
            if len(items) < 2:
                continue
            # promote to the shared, level-independent figure slot so every depth sees it
            section["visual"] = {"type": "flow", "steps": items}
            return True
    return False


_RESOURCE_PROMPT = """You are choosing the "watch and read next" list for a finished course.
Pick only from the candidates given. Never invent an id, a URL, a venue or a year.

Reject anything about a different sense of the topic — if the course is about memory
management, a video about refuse collection is wrong no matter how well it matches the
words. Prefer two correct items over five doubtful ones; an empty list is acceptable.

Return ONE JSON object:
{
  "videos": [{"id": "...", "why": "one line: what this gives the reader", "level": "novice|intermediate|advanced"}],
  "reading": [{"url": "...", "kind": "paper|article|book|docs|talk", "venue": "", "year": null,
               "why": "one line", "level": "novice|intermediate|advanced"}]
}

Rules:
- at most 3 videos and at most 4 reading items, best first
- the friendliest explainer goes to novice, the primary source or the strongest critique to advanced
- "why" must say what the item adds to THIS course, never repeat its title
- an empty candidate list means an empty list in the answer"""

_PAPER_HINT = ("arxiv.org", "doi.org", "nature.com", "science.org", "ieee.org", "acm.org",
               "jstor.org", "springer", "wiley", "sciencedirect", ".pdf")


def _honest_kind(claimed: str, url: str) -> str:
    """A model's 'paper' claim on a blog is a data-quality bug, not a citation."""
    kind = (claimed or "").strip().lower()
    if kind == "paper" and not any(hint in (url or "") for hint in _PAPER_HINT):
        return _guess_kind(url)
    return kind if kind in {"paper", "article", "book", "docs", "talk"} else _guess_kind(url)


def _honest_year(claimed: Any, source: Dict[str, str]) -> Optional[int]:
    """Keep a year only when it is plausible AND literally present in the source text."""
    if not isinstance(claimed, int) or not (1000 < claimed <= time.gmtime().tm_year + 1):
        return None
    haystack = f"{source.get('title', '')} {source.get('excerpt', '')} {source.get('url', '')}"
    return claimed if str(claimed) in haystack else None


_KINDS = (("arxiv.org", "paper"), ("doi.org", "paper"), ("nature.com", "paper"), ("science.org", "paper"),
          ("ieee.org", "paper"), ("acm.org", "paper"), ("wikipedia.org", "article"),
          ("developer.mozilla.org", "docs"), ("docs.", "docs"), ("github.com", "docs"),
          (".gov", "docs"), ("youtube.com", "talk"))


def _guess_kind(url: str) -> str:
    for fragment, kind in _KINDS:
        if fragment in (url or ""):
            return kind
    return "article"


_VIDEO_QUERY_PROMPT = """You are choosing YouTube search queries for a course that is about to be written.

Give 3 short queries (4-9 words each) that would surface good explainer videos for THIS
specific topic, at the technical level of THIS course. Disambiguate aggressively: if the
topic's common reading is wrong for the course (a "garbage collector" course is about memory,
not bin lorries), add the words that force the right sense.

Return ONE JSON object: {"queries": ["...", "...", "..."]}"""


def video_queries(ctx, topic: str, plan: Optional[Dict[str, Any]] = None) -> List[str]:
    """Model-chosen YouTube queries, disambiguated by the course's own outline.

    Falls back to plain topic queries if the model call fails — never blocks research.
    """
    sections = (plan or {}).get("sections") or []
    context = "\n".join(f"- {s.get('title', '')}: {s.get('teaches', '')}" for s in sections[:8])
    payload = (f"Topic: {topic}\n"
               f"Course thesis: {(plan or {}).get('thesis') or ''}\n"
               f"Sections:\n{context}\n")
    raw = _ask(ctx, instructions=_VIDEO_QUERY_PROMPT, payload=payload,
               purpose="coursewright.video_queries", max_tokens=400)
    queries = []
    if isinstance(raw, dict):
        for item in (raw.get("queries") or [])[:3]:
            text = str(item).strip()
            if 8 <= len(text) <= 120:
                queries.append(text)
    return queries or [f"{topic} explained"]


def pick_resources(ctx, plan: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    """One small call: which videos and readings, tagged by level. Falls back to order."""
    videos = (record or {}).get("videos") or []
    sources = (record or {}).get("sources") or []
    if not videos and not sources:
        return {"videos": [], "reading": []}

    video_lines = "\n".join(
        f"- id={v['id']} | {v['title']} | {v['channel']} | ~{round((v.get('duration') or 0) / 60)} min"
        for v in videos) or "(none)"
    reading_lines = "\n".join(
        f"- url={s['url']} | {s['title']} | {s.get('domain', '')}" for s in sources) or "(none)"
    payload = (f"Course: {plan.get('title')}\nThesis: {plan.get('thesis')}\n"
               f"Sections: " + "; ".join(s["title"] for s in plan["sections"]) +
               f"\n\nVIDEO CANDIDATES:\n{video_lines}\n\nREADING CANDIDATES:\n{reading_lines}\n")
    raw = _ask(ctx, instructions=_RESOURCE_PROMPT, payload=payload,
               purpose="coursewright.resources", max_tokens=900)

    picked_videos: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        by_id = {v["id"]: v for v in videos}
        for item in (raw.get("videos") or [])[:3]:
            if not isinstance(item, dict):
                continue
            video = by_id.get(str(item.get("id") or "").strip())
            if not video:
                continue
            picked_videos.append({"id": video["id"], "why": str(item.get("why") or "")[:220],
                                  "level": str(item.get("level") or "")[:20]})
        by_url = {s["url"]: s for s in sources}
        picked_reading = []
        for item in (raw.get("reading") or [])[:4]:
            if not isinstance(item, dict):
                continue
            source = by_url.get(str(item.get("url") or "").strip())
            if not source:
                continue
            year = item.get("year")
            picked_reading.append({
                "url": source["url"],
                "title": str(item.get("title") or source["title"])[:220],
                "kind": _honest_kind(str(item.get("kind") or ""), source["url"]),
                "venue": source.get("domain") or "",
                "year": _honest_year(item.get("year"), source),
                "why": str(item.get("why") or "")[:220],
                "level": str(item.get("level") or "")[:20],
            })
        if picked_videos or picked_reading:
            return {"videos": picked_videos, "reading": picked_reading}

    # deterministic fallback: candidates in the order research ranked them
    levels = ("novice", "intermediate", "advanced")
    return {
        "videos": [{"id": v["id"], "why": "", "level": levels[min(i, 2)]}
                   for i, v in enumerate(videos[:3])],
        "reading": [{"url": s["url"], "title": s["title"], "kind": _guess_kind(s["url"]),
                     "venue": s.get("domain", ""), "year": None, "why": "",
                     "level": levels[min(i, 2)]}
                    for i, s in enumerate(sources[:4])],
    }


def assemble(plan: Dict[str, Any], sections: List[Dict[str, Any]], *, topic: str,
             research: Optional[Dict[str, Any]] = None,
             picks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    warnings: List[str] = []
    if plan.get("degraded"):
        warnings.append(plan["degraded"])
    clean_sections = []
    seen_titles: set = set()
    for index, section in enumerate(sections):
        for note in section.get("_warnings") or []:
            warnings.append(note)
        clean_section = {k: v for k, v in section.items() if not k.startswith("_")}
        # a section titled the same as another (or left blank) is a drafting failure:
        # the planned title is always better than "Roots" four times over.
        planned = plan["sections"][index]["title"] if index < len(plan["sections"]) else ""
        title = str(clean_section.get("title") or "").strip()
        if not title or title.lower() in seen_titles:
            clean_section["title"] = str(planned or f"Part {index + 1}")
            warnings.append(f"section {index + 1} reused a title; the planned title was restored")
        seen_titles.add(str(clean_section["title"]).lower())
        if _flow_from_steps(clean_section):
            logger.info("coursewright: drew a flow figure for %s from its steps block",
                        clean_section.get("id"))
            # the missing-figure complaint is resolved by the figure we just drew
            warnings = [w for w in warnings if "no figure" not in w]
        clean_sections.append(clean_section)

    record = research or {}
    by_id = {v["id"]: v for v in (record.get("videos") or [])}
    videos: List[Dict[str, Any]] = []
    seen_why: set = set()
    for pick in (picks or {}).get("videos", [])[:6]:
        video = by_id.get(pick.get("id"))
        if not video:
            continue
        why = (pick.get("why") or "").strip()
        if why and why.lower() in seen_why:  # repeated blurbs read as placeholder copy
            why = ""
        if why:
            seen_why.add(why.lower())
        videos.append({
            "id": video["id"], "title": video.get("title", ""), "channel": video.get("channel", ""),
            "duration": video.get("duration"), "transcript": bool(video.get("transcript")),
            "thumb": "inline" if video.get("thumb") else "none",
            "thumb_data": video.get("thumb", ""),
            "why": why, "level": pick.get("level", ""),
        })

    reading = []
    for item in (picks or {}).get("reading", [])[:8]:
        if not item.get("url"):
            continue
        why = (item.get("why") or "").strip()
        if why and why.lower() in seen_why:
            why = ""
        if why:
            seen_why.add(why.lower())
        reading.append({
            "title": item.get("title", ""), "href": item.get("url", ""), "kind": item.get("kind", ""),
            "venue": item.get("venue", ""), "year": item.get("year"), "why": why,
            "level": item.get("level", ""),
        })

    return {
        "version": 1,
        "slug": plan.get("slug") or topic,
        "language": plan.get("language", "en"),
        "title": plan.get("title") or topic,
        "subtitle": plan.get("subtitle", ""),
        "thesis": plan.get("thesis", ""),
        "outcomes": plan.get("outcomes") or [],
        "default_level": plan.get("default_level", "intermediate"),
        "identity": plan.get("identity") or {},
        "provenance": {
            "researched": bool(record.get("researched")),
            "finders": record.get("finders") or [],
            "readers": record.get("readers") or [],
            "sources": [{"url": s["url"], "title": s.get("title", ""),
                         "used_in": []} for s in (record.get("sources") or [])],
            "failures": (record.get("failures") or [])[:6],
            "warnings": warnings + (record.get("warnings") or []),
            "generated": record.get("fetched_at") or "",
        },
        "sections": clean_sections,
        "resources": {"videos": videos, "reading": reading},
    }
