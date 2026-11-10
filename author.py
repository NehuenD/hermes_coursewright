"""Authoring: turn a topic into a course spec through the host's own model.

Two stages, both through `ctx.llm` (host-owned credentials, no keys in the plugin):

    PLAN   -> one cheap call: title, thesis, outcomes, 6-8 section briefs, identity
    DRAFT  -> one call per section: three levels of prose, an exercise, an optional figure

The model never writes HTML. Everything it returns is validated by `spec.py` before
`render.py` draws it, and a section that fails to arrive is replaced by a thin one
carrying a warning rather than sinking the whole build.
"""

from __future__ import annotations

import copy
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
_SECTION_TOKENS = 6000
_REPAIR_TOKENS = 2600
_PLAN_TOKENS = 1400

BLOCK_MENU = """Available block kinds (use several different ones; never invent a kind):
- {"kind":"prose","md":"2-4 sentences"}
- {"kind":"analogy","term":"Think of it as","md":"a short comparison that maps cleanly onto the real thing"}
- {"kind":"definition","term":"the term","md":"a precise one-or-two-sentence definition"}
- {"kind":"callout","tone":"insight|honesty|warning","title":"short label","md":"one short paragraph"}
- {"kind":"steps","items":["first","second","third"]}
- {"kind":"comparison","left":"A","right":"B","items":["a1","b1","a2","b2"]}  (items alternate left,right)
- {"kind":"timeline","items":["1937 — something happened","1993 — something else"]}
- {"kind":"math","md":"formulas only, one step per line — `a = b` on its own line, `= c` on the next. Subscript `_x` or `_{...}`, superscript `^x` or `^{...}`. No LaTeX, no dollar signs, no HTML, and no sentences: commentary belongs in a prose block"}
- {"kind":"code","lang":"python","code":"short, runnable, commented"}
- {"kind":"aside","md":"one sentence of nuance"}
- {"kind":"checkpoint","md":"one sentence: what the reader should now be able to do"}
- {"kind":"figure","caption":"what it shows","spec":{FIGURES}}"""

FIGURE_MENU = """{"type":"flow","steps":["token id","embedding","attention head","feed-forward","logits"]}
       Best for any process, pipeline or causal chain: 3-6 short steps, two or three words
       each. No data needed. A step label may carry a symbol: W_Q multiplies, or write W_{Q}
       for a multi-character subscript.
       or {"type":"diagram","nodes":[{"id":"a","label":"prompt"},{"id":"b","label":"tokens"},{"id":"c","label":"logits","group":"focus"}],"edges":[{"from":"a","to":"b","label":"encode"},{"from":"b","to":"c","label":"predict"}]}
       Best when one thing points at another and the arrows carry meaning: architecture, data
       flow, a state machine, a dependency graph, a feedback loop. 2-8 nodes; ids are short
       words (a, b, fetch); labels two or three words; edge labels short and only where the
       arrow needs one. "group":"focus" marks the single node this section is about. Put a
       cycle in when the subject has one — it is drawn, not refused.
       or {"type":"sequence","actors":["user","gateway","model"],"messages":[{"from":"user","to":"gateway","label":"prompt"},{"from":"gateway","to":"model","label":"messages"},{"from":"model","to":"gateway","label":"tokens","reply":true}]}
       Best for anything that happens in time order between named parties: a protocol, a
       request and its answer, a handshake. 2-5 actors, up to 8 messages, read top to bottom;
       "reply":true marks the return leg of a call.
       or {"type":"bars","points":[{"label":"2019","value":12},{"label":"2024","value":31}],"xlabel":"year","ylabel":"count"}
       or {"type":"line","points":[{"label":"0","value":1},{"label":"1","value":4}]}
       A course should carry at least one figure when the subject has a natural structure or
       process. Prefer a diagram or a sequence over a flow when the thing you are showing has
       parts that connect rather than steps that follow. Data charts only for numbers you
       genuinely have — never invent data."""

LEVEL_RULES = """LEVEL RULES — this is the whole point of the course. The three versions must NOT be
the same text at different lengths, and none of them is a summary. Each level is a complete
explanation for its reader.

Every level runs the same arc, in this order:
(1) what problem this part solves, in one or two sentences, before any name appears;
(2) the mechanism, step by step: what goes in, what each step does to it, what comes out;
(3) the worked example (given below), carried through with real text, real numbers or
    runnable code, read out step by step — not gestured at;
(4) what it costs or where it fails, with the reason.

novice (350-500 words, 5-7 blocks):
- No jargon until the mechanism is on the table. When a term is unavoidable, define it in the
  same sentence in plain words: "the tokenizer — the rule that cuts text into pieces".
- The worked example is the spine. Start from a sentence the reader already knows, show it
  being cut into pieces, show the pieces becoming numbers. Real strings, real-looking numbers.
- Short sentences. Explain more things, not fewer.

intermediate (500-700 words, 6-8 blocks):
- The real terms, each defined the first time it appears, in a sentence that says what it
  does — not what it is called.
- The mechanism at the level of quantities: sizes, dimensions, what is multiplied by what,
  what order things happen in.
- The worked example developed past the novice version: intermediate values, and one case
  where the design choice visibly matters.
- One honest limit, with the reason it exists. 6-8 blocks.

advanced (550-800 words, 6-8 blocks):
- Precise statements; every symbol defined before it is used; math or code a reader could
  check or run.
- Why the design works and what it buys, naming the standard result and its author when sure.
- Where it breaks, what is contested, what is still open — with the specific reason, not the
  word "complex".
- One sentence that sets up the next section."""

HARD_RULES = """HARD RULES:
- Never invent a citation, URL, DOI, page number, ISBN, or direct quotation. Naming a
  well-known result and its author is fine; a fake reference is not.
- Math is plain text, in every block and in figure labels: a subscript is an underscore after
  the base (W_Q), a superscript a caret (K^T), and a multi-character script is wrapped in curly
  braces. Never LaTeX, never dollar signs, never HTML — anything else prints literally.
- Length: a block is at most 200 words; a level is 350-800 words as its rule above says. A
  level under 300 words is a failure — it means the mechanism was named, not explained.
- Define before use. The first time a technical term appears in a level, the same sentence
  must say what it does to the data. "The tokenizer" on its own is a label, not teaching.
- Never name a mechanism without showing it operate: what goes in, what changes, what comes
  out. If you cannot say what it does to the input, do not name it.
- Every level carries this section's worked example — real text, real numbers, or code — and
  reads it through. An analogy may support it; an analogy may not replace it.
- No filler. A checkpoint, aside or callout never stands in for the mechanism; at most one
  checkpoint, at the end of a level.
- A math block is formulas, not prose: one relation per line, `=` aligned by the renderer, a
  chain split across lines (`a = b` then `= c`). Any sentence that explains a formula goes in
  the prose around it.
- Delete any sentence that would still be true of a different subject ("this matters because
  it affects quality"). Prefer the number, the mechanism, or the failure.
- Write in {language}.
- Do not mention that you are an AI, do not address the reader's level, do not write
  "in this section we will" — just teach."""

_EXERCISE_RULES = """Exercise: exactly one per section. Choose ONE kind:
- {"kind":"mcq","prompt":"...","choices":["A","B","C","D"],"answer":0,"why":"why the right one is right, one or two sentences"}
  (exactly 4 choices, answer is the 0-based index of the correct one. Test the mechanism,
  not the vocabulary: give this section's example a twist and ask what happens to it. Each
  distractor must be a mistake the mechanism predicts — what a reader who half-understood
  it would answer — never an obviously wrong option)
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


class ModelUnavailable(RuntimeError):
    """The host refused to run the model at all: a trust gate or no provider configured.

    That is not a generation that came back thin — there is nothing to retry, and pretending
    otherwise produced a one-section course reported as success.
    """


_LAST_ERROR: List[str] = [""]


def last_error() -> str:
    """The most recent model failure, for a build that has to explain itself."""
    return _LAST_ERROR[0]


def _ask(ctx, *, instructions: str, payload: str, purpose: str, max_tokens: int,
         json_mode: bool = True) -> Optional[Any]:
    """One structured call through the host model; None on a failed response."""
    try:
        result = ctx.llm.complete_structured(
            **_ask_kwargs(instructions=instructions, payload=payload, purpose=purpose,
                          max_tokens=max_tokens, json_mode=json_mode)
        )
    except Exception as exc:  # trust gate, provider error, timeout
        _call_failed(exc, purpose)
        return None
    return _parsed(result)


async def _ask_async(ctx, *, instructions: str, payload: str, purpose: str, max_tokens: int,
                     json_mode: bool = True) -> Optional[Any]:
    """The same call on the host's async sibling, so sections can be drafted at once.

    A host without `acomplete_structured` still answers: this falls back to the sync path,
    which is slower but never wrong.
    """
    call = getattr(getattr(ctx, "llm", None), "acomplete_structured", None)
    if call is None:
        return _ask(ctx, instructions=instructions, payload=payload, purpose=purpose,
                    max_tokens=max_tokens, json_mode=json_mode)
    try:
        result = await call(
            **_ask_kwargs(instructions=instructions, payload=payload, purpose=purpose,
                          max_tokens=max_tokens, json_mode=json_mode)
        )
    except Exception as exc:
        _call_failed(exc, purpose)
        return None
    return _parsed(result)


def _ask_kwargs(*, instructions: str, payload: str, purpose: str, max_tokens: int,
                json_mode: bool) -> Dict[str, Any]:
    return dict(
        instructions=instructions,
        input=[{"type": "text", "text": payload}],
        json_mode=json_mode,
        purpose=purpose,
        temperature=0.4,
        max_tokens=max_tokens,
        timeout=300,
    )


def _call_failed(exc: Exception, purpose: str) -> None:
    """Record a failed call; a denied trust gate is terminal and stops the build."""
    _LAST_ERROR[0] = f"{type(exc).__name__}: {exc}"
    # A denied trust gate is terminal: every later call fails the same way, so the build
    # should stop and say so rather than draft eight placeholder sections.
    if isinstance(exc, PermissionError) or type(exc).__name__ == "PluginLlmTrustError":
        logger.error("coursewright: the host refused model access: %s", exc)
        raise ModelUnavailable(str(exc)) from exc
    logger.warning("coursewright: %s call failed: %s", purpose, exc)


def _parsed(result: Any) -> Optional[Any]:
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
     "teaches": "2-3 sentences: the mechanism this section establishes, and the one thing a reader gets wrong about it",
     "example": "the concrete case this section will work through — the actual text, numbers or code, with its intermediate values"}
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
            # the section's spine: what it establishes and the case it works through
            "teaches": str(item.get("teaches") or "")[:700],
            "example": str(item.get("example") or "")[:300],
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
                      "teaches": f"an introduction to {topic}", "example": ""}],
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
    recap = ""
    if index:
        previous = plan["sections"][index - 1]
        recap = (f"\nThe section just before this one, \"{previous['title']}\", established: "
                 f"{previous['teaches'][:320]}\nBuild on it; assume the reader has read it.\n")
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
<<RECAP>>
The whole course, in order (do not write the other sections):
<<GUIDE>>
<<SOURCES>>
Write THIS section: "<<SECTION>>" — its job is: <<TEACHES>>
Its worked example: <<EXAMPLE>>
Carry that example through all three levels, adding detail at each depth. If it does not fit
what the section teaches, improve it — but the section still has one concrete case, walked
through with its actual values.

Return ONE JSON object:
{
  "id": "s<<N>>",
  "title": "<<SECTION>>",
  "eyebrow": "keep or improve the 2-3 word label",
  "hook": "one line that earns the next two minutes",
  "visual": null or a figure (see below),
  "exercise": { ... },
  "levels": {
    "novice": {"blocks": [ ... ]},
    "intermediate": {"blocks": [ ... ]},
    "advanced": {"blocks": [ ... ]}
  }
}
Write the exercise before the levels: a long answer that runs out of room drops whatever is
last, and the exercise is the part that goes missing.

REQUIRED STRUCTURE FOR THIS SECTION:
- This section needs exactly one figure, in the top-level "visual" field:
  {"type":"flow","steps":["step","step","step"],"caption":"what it shows"}
  The figure is SHARED by all three depths, so put it here and not inside a level's blocks —
  a novice must be able to see it too.
  Choose the figure this section actually needs:
  * a process, pipeline or causal chain -> {"type":"flow","steps":["step","step","step"]}
  * parts that connect, with meaning on the arrows (architecture, state machine, data flow,
    feedback loop) -> {"type":"diagram","nodes":[...],"edges":[...]}
  * parties exchanging things in time order (protocol, request/response) ->
    {"type":"sequence","actors":[...],"messages":[...]}
  * real numbers you are sure of -> {"type":"bars","points":[{"label":"x","value":1},...]}
  * a conceptual distinction with no process and no numbers -> a flow of 3-5 concept labels
    (a flow of ideas is a valid figure; never invent data to fill space)
  A diagram or a sequence is a better figure than a flow whenever the subject has parts that
  relate, or an exchange that happens in time. The renderer places every element; never give
  coordinates, sizes or colours.

<<LEVEL_RULES>>

<<BLOCK_MENU>>

<<EXERCISE_RULES>>
This section's exercise MUST be kind "<<EXERCISE_KIND>>" — the mix across the course is fixed,
so give this one the kind it is told to have.

<<HARD_RULES>>

Use 3 or more different block kinds across the three levels of this section. Vary the figure
type between sections: do not give every section the same kind of figure.

BEFORE YOU ANSWER, check every level against this list. Fix what fails; do not report it.
- Could a reader who has never seen this subject predict what happens to the example input
  after reading the level? If not, the mechanism is missing and the level is not finished.
- Is every technical term defined where it first appears, in words that do not assume it?
- Does the level walk the worked example through with real text, numbers or code — showing
  intermediate values, not only the input and the output?
- Is any sentence still true if you swap the subject for a different one? Replace it with the
  number, the mechanism, or the failure.
- Are the lengths inside the ranges (novice 350-500, intermediate 500-700, advanced 550-800
  words)? A level under 300 words is not finished."""
    instructions = (template
                    .replace("<<N>>", str(index + 1))
                    .replace("<<TOTAL>>", str(total))
                    .replace("<<TITLE>>", str(plan["title"]))
                    .replace("<<THESIS>>", str(plan["thesis"] or plan["title"]))
                    .replace("<<GUIDE>>", guide)
                    .replace("<<SOURCES>>", sources_block)
                    .replace("<<SECTION>>", str(section["title"]))
                    .replace("<<TEACHES>>", str(section["teaches"]))
                    .replace("<<RECAP>>", recap)
                    .replace("<<EXAMPLE>>", str(section.get("example") or "none given — choose one "
                                                        "concrete case and carry it through"))
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


def _section_problems_for(section: Dict[str, Any], index: int) -> List[str]:
    """Structural problems, plus whether the exercise is the kind this section was told to write."""
    problems = _section_problems(section)
    required_kind = _exercise_kind(index)
    got_kind = (section.get("exercise") or {}).get("kind")
    if got_kind and got_kind != required_kind:
        problems.append(f"this section's exercise must be a {required_kind}, not a {got_kind}")
    return problems


_LEVELS = ("novice", "intermediate", "advanced")


def _levels_named(problems: List[str]) -> List[str]:
    """The depths a problem list blames, or [] when any problem is about the whole section.

    A repair can only touch depths: a missing figure or exercise sits outside them, so those
    problems send the section back through a full draft instead of a repair.
    """
    named: List[str] = []
    for problem in problems:
        head, sep, _ = problem.partition(":")
        head = head.strip().lower()
        if not sep or head not in _LEVELS:
            return []
        if head not in named:
            named.append(head)
    return named


def _repair_prompt(plan: Dict[str, Any], index: int, section: Dict[str, Any], levels: List[str],
                   problems: List[str], language: str) -> tuple[str, str]:
    """Instructions and payload for rewriting only the depths that failed."""
    brief = plan["sections"][index]
    current = {level: (section.get("levels") or {}).get(level) for level in levels}
    template = """You are repairing <<LEVELS>> of one section of a course called "<<TITLE>>".
The rest of the section is finished and stays exactly as it is; you do not rewrite it.

The section is "<<SECTION>>" and its job is: <<TEACHES>>
Its worked example: <<EXAMPLE>>

The depth you are rewriting, as it stands now. Keep every claim that is right, keep the
example, keep the voice; fix what the problems name and change nothing else:
<<CURRENT>>

<<LEVEL_RULES>>

<<BLOCK_MENU>>

<<HARD_RULES>>

Return ONE JSON object and nothing else:
{"levels": {<<SHAPE>>}}
Write only <<LEVELS>>. Do not return the other depths, the exercise, the figure or the title.

What is wrong with what you are rewriting:
<<PROBLEMS>>"""
    shape = ", ".join(f'"{level}": {{"blocks": [{{"kind": "...", ...}}]}}' for level in levels)
    instructions = (template
                    .replace("<<LEVELS>>", " and ".join(levels))
                    .replace("<<TITLE>>", str(plan["title"]))
                    .replace("<<SECTION>>", str(brief["title"]))
                    .replace("<<TEACHES>>", str(brief.get("teaches") or brief["title"]))
                    .replace("<<EXAMPLE>>", str(brief.get("example")
                                                 or "the one the section already walks through"))
                    .replace("<<CURRENT>>", json.dumps(current, indent=1, ensure_ascii=False)[:9000])
                    .replace("<<LEVEL_RULES>>", LEVEL_RULES)
                    .replace("<<BLOCK_MENU>>", BLOCK_MENU.replace("{FIGURES}", FIGURE_MENU))
                    .replace("<<HARD_RULES>>", HARD_RULES.replace("{language}", language))
                    .replace("<<SHAPE>>", shape)
                    .replace("<<PROBLEMS>>", "\n".join(f"- {problem}" for problem in problems[:6])))
    payload = (f"Section {index + 1}/{len(plan['sections'])}: {brief['title']}\n"
               f"Depth to rewrite: {', '.join(levels)}")
    return instructions, payload


def repair_levels(ctx, plan: Dict[str, Any], index: int, section: Dict[str, Any],
                  levels: List[str], problems: List[str], *, language: str = "en"
                  ) -> Optional[Dict[str, Any]]:
    """Re-ask for the failing depths only; None when nothing usable came back."""
    instructions, payload = _repair_prompt(plan, index, section, levels, problems, language)
    raw = _ask(ctx, instructions=instructions, payload=payload,
               purpose=f"coursewright.repair.{index + 1}", max_tokens=_REPAIR_TOKENS)
    return merge_repair(section, raw, levels)


async def repair_levels_async(ctx, plan: Dict[str, Any], index: int, section: Dict[str, Any],
                              levels: List[str], problems: List[str], *, language: str = "en"
                              ) -> Optional[Dict[str, Any]]:
    """The async sibling of :func:`repair_levels`."""
    instructions, payload = _repair_prompt(plan, index, section, levels, problems, language)
    raw = await _ask_async(ctx, instructions=instructions, payload=payload,
                           purpose=f"coursewright.repair.{index + 1}", max_tokens=_REPAIR_TOKENS)
    return merge_repair(section, raw, levels)


def merge_repair(section: Dict[str, Any], raw: Any, levels: List[str]) -> Optional[Dict[str, Any]]:
    """Splice repaired depths into a copy of the section; None when nothing usable came back."""
    if not isinstance(raw, dict):
        return None
    bodies = raw.get("levels") if isinstance(raw.get("levels"), dict) else raw
    if not isinstance(bodies, dict):
        return None
    merged = copy.deepcopy(section)
    into = merged.get("levels")
    if not isinstance(into, dict):
        into = merged["levels"] = {}
    for level in levels:
        body = bodies.get(level)
        if isinstance(body, dict) and isinstance(body.get("blocks"), list) and body["blocks"]:
            into[level] = body
    return merged if any(isinstance(into.get(level), dict) and into[level].get("blocks")
                         for level in levels) else None


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
    problems = _section_problems_for(section, index)
    if problems and not retry_errors:
        levels = _levels_named(problems)
        if levels:
            repaired = repair_levels(ctx, plan, index, section, levels, problems, language=language)
            if repaired is not None:
                section, problems = repaired, _section_problems_for(repaired, index)
    if problems and not retry_errors:
        logger.info("coursewright: retrying section %s (%s)", index + 1, problems[:2])
        return draft_section(ctx, plan, index, language=language, sources=sources,
                             retry_errors=problems)
    if problems:
        # numbered, so the assembler can clear exactly the note its own repair resolved
        section.setdefault("_warnings", []).extend(
            f"section {index + 1}: {problem}" for problem in problems)
    return section


async def draft_section_async(ctx, plan: Dict[str, Any], index: int, *, language: str = "en",
                              sources: str = "",
                              retry_errors: Optional[List[str]] = None) -> Dict[str, Any]:
    """The async sibling of :func:`draft_section`, for drafting several at once."""
    total = len(plan["sections"])
    instructions, payload = _section_prompt(plan, index, total, language, sources)
    if retry_errors:
        payload += ("\nYour previous answer had these problems; fix them and return the "
                    "whole JSON object again:\n- " + "\n- ".join(retry_errors[:6]))

    raw = await _ask_async(ctx, instructions=instructions, payload=payload,
                           purpose=f"coursewright.section.{index + 1}", max_tokens=_SECTION_TOKENS)
    if os.environ.get("COURSEWRIGHT_DUMP"):
        _dump(index, "raw", raw)
    section = _coerce_section(raw, plan, index)
    if os.environ.get("COURSEWRIGHT_DUMP"):
        _dump(index, "coerced", section)
    problems = _section_problems_for(section, index)
    if problems and not retry_errors:
        levels = _levels_named(problems)
        if levels:
            repaired = await repair_levels_async(ctx, plan, index, section, levels, problems,
                                                 language=language)
            if repaired is not None:
                section, problems = repaired, _section_problems_for(repaired, index)
    if problems and not retry_errors:
        logger.info("coursewright: retrying section %s (%s)", index + 1, problems[:2])
        return await draft_section_async(ctx, plan, index, language=language, sources=sources,
                                         retry_errors=problems)
    if problems:
        section.setdefault("_warnings", []).extend(
            f"section {index + 1}: {problem}" for problem in problems)
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
        "_thin": True,
        "_warnings": [f"section {index + 1} was rebuilt thin: {why}"],
    }


_MIN_LEVEL_WORDS = {"novice": 260, "intermediate": 360, "advanced": 360}
_CONCRETE_KINDS = {"steps", "code", "math", "timeline", "comparison"}


def _block_words(block: Dict[str, Any]) -> int:
    text = block.get("md") or " ".join(str(i) for i in (block.get("items") or [])) \
        or block.get("code") or ""
    return len(re.findall(r"[\w'’-]+", str(text)))


def _section_problems(section: Dict[str, Any]) -> List[str]:
    """Cheap structural checks worth one retry — including whether it actually taught."""
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
        # a level that names the mechanism without explaining it is the failure this course
        # exists to avoid, so length and a walked example are part of the contract
        words = sum(_block_words(b) for b in blocks if isinstance(b, dict))
        floor = _MIN_LEVEL_WORDS[level]
        if words < floor:
            problems.append(f"{level}: {words} words is a summary, not an explanation — "
                            f"at least {floor}, with the mechanism and the worked example")
        if not any(isinstance(b, dict) and b.get("kind") in _CONCRETE_KINDS for b in blocks):
            problems.append(f"{level}: no worked example — the example needs a steps, code, "
                            f"math or timeline block carrying its actual values")
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
            # the missing-figure complaint is resolved by the figure we just drew — and only
            # that section's complaint, not every section's
            prefix = f"section {len(clean_sections) + 1}: "
            warnings = [w for w in warnings
                        if not (w.startswith(prefix) and "no figure" in w)]
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
