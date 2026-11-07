"""Renderer: validated course spec -> one self-contained HTML page.

No CDNs, no web fonts, no build step, no network. The model never writes HTML or
CSS; it fills the spec, and this module draws it from the design-token space in
`tokens.py`.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Dict, List

try:  # loaded as a package by the plugin loader
    from . import tokens
except ImportError:  # direct import during development
    import tokens  # type: ignore


# --------------------------------------------------------------------------
# inline markup: a deliberately small, escaped-first subset
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITAL_RE = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
# a bare [3] (not a markdown link) is a citation marker for the source list in the footer
_CITE_RE = re.compile(r"\[(\d{1,2})\](?!\()")


_CODE_SENTINEL = "\x00code{}\x00"


def _scripts(text: str, *, aggressive: bool = False) -> str:
    """Escaped text -> sub/sup.

    `_{...}` / `^{...}` always; bare `_x` / `^x` only in math (aggressive), so an
    identifier like `web_search` in prose is never mangled into a subscript.
    Written as an explicit scan rather than a regex: backslash handling in
    character pairs is exactly where this used to break.
    """
    out: list = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char in "_^" and index + 1 < length:
            tag = "sub" if char == "_" else "sup"
            if text[index + 1] == "{":
                close = text.find("}", index + 2)
                inner = text[index + 2:close] if close != -1 else ""
                if close != -1 and 0 < len(inner) <= 12:
                    out.append(f"<{tag}>{inner}</{tag}>")
                    index = close + 1
                    continue
            elif aggressive and text[index + 1].isalnum():
                out.append(f"<{tag}>{text[index + 1]}</{tag}>")
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _inline(text: str) -> str:
    """Escape everything, then apply the tiny markup subset. Never emits raw HTML."""
    raw = html.escape(_TAG_RE.sub("", text or ""), quote=False)
    # code spans are literal: hold them aside so no other rule rewrites their bytes
    held: list = []

    def _hold(match: re.Match) -> str:
        held.append(match.group(1))
        return _CODE_SENTINEL.format(len(held) - 1)

    raw = _CODE_RE.sub(_hold, raw)
    out = _LINK_RE.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}" rel="noopener noreferrer" '
                  f'target="_blank">{m.group(1)}</a>', raw)
    out = _CITE_RE.sub(r'<sup class="cite"><a href="#src-\1">\1</a></sup>', out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITAL_RE.sub(r"<em>\1</em>", out)
    out = _scripts(out, aggressive=False)
    for index, code in enumerate(held):
        out = out.replace(_CODE_SENTINEL.format(index), f"<code>{code}</code>")
    return out


_MATH_FUNCS = re.compile(r"\b(log|ln|exp|max|min|sup|inf|lim|E|Pr|gcd|mod)\b")


def _math(text: str) -> str:
    """Escape, then upright function names and sub/sup (function names first, so a
    generated <sup> tag is never mistaken for the word "sup")."""
    out = html.escape(_TAG_RE.sub("", text or ""), quote=False)
    out = _MATH_FUNCS.sub(r'<span class="fn">\1</span>', out)
    out = _scripts(out, aggressive=True)
    return f'<span class="m">{out}</span>'


_IDX_RE = re.compile(r"^\s*(?:\d+[.)]|[-\*•—–])\s*")


def _strip_index(text: str) -> str:
    """Drop a leading list marker the model wrote itself ('3. ', '- ') — the renderer numbers."""
    return _IDX_RE.sub("", str(text or "")).strip()


def _paras(text: str) -> str:
    chunks = [c for c in re.split(r"\n{2,}", (text or "").strip()) if c.strip()]
    return "".join(f"<p>{_inline(c.strip().replace(chr(10), ' '))}</p>" for c in chunks)


def _fill(template: str, mapping: Dict[str, str]) -> str:
    for key, value in mapping.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def _domain_of(url: str) -> str:
    try:
        return re.sub(r"^www\.", "", url.split("//", 1)[-1].split("/", 1)[0])
    except Exception:
        return ""


def _fmt_duration(seconds: Any) -> str:
    if not isinstance(seconds, int) or seconds <= 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------
def _block(block: Dict[str, Any]) -> str:
    kind = block["kind"]
    if kind in {"prose", "checkpoint"}:
        body = _paras(block.get("md", ""))
        if kind == "checkpoint":
            return f'<div class="checkpoint"><span class="cp-label">Before you move on</span>{body}</div>'
        return body
    if kind == "math":
        return f'<div class="mathblock">{_math(block.get("md", ""))}</div>'
    if kind == "aside":
        return f'<aside class="aside">{_paras(block.get("md", ""))}</aside>'
    if kind == "analogy":
        term = _inline(block.get("term", "")) or "Think of it this way"
        return (f'<div class="analogy"><span class="an-label">{term}</span>'
                f'<div class="an-body">{_paras(block.get("md", ""))}</div></div>')
    if kind == "definition":
        term = _inline(block.get("term", ""))
        head = f'<dt>{term}</dt>' if term else ""
        return f'<dl class="def">{head}<dd>{_paras(block.get("md", ""))}</dd></dl>'
    if kind == "quote":
        cite = block.get("cite", "")
        href = block.get("href", "")
        att = (f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer" target="_blank">'
               f'{_inline(cite)}</a>' if href else _inline(cite))
        return (f'<figure class="quote"><blockquote>{_paras(block.get("md", ""))}</blockquote>'
                f'{f"<figcaption>— {att}</figcaption>" if cite else ""}</figure>')
    if kind == "callout":
        tone = block.get("tone", "insight")
        title = _inline(block.get("title", ""))
        head = f'<span class="co-title">{title}</span>' if title else ""
        return f'<div class="callout co-{tone}">{head}{_paras(block.get("md", ""))}</div>'
    if kind == "steps":
        items = "".join(f"<li>{_inline(_strip_index(i))}</li>" for i in block.get("items", []))
        return f'<ol class="steps">{items}</ol>'
    if kind == "comparison":
        left, right = _inline(block.get("left", "A")), _inline(block.get("right", "B"))
        items = block.get("items", [])
        rows = ""
        half = max(1, len(items) // 2)
        for a, b in zip(items[:half], items[half:]):
            rows += f"<tr><td>{_inline(a)}</td><td>{_inline(b)}</td></tr>"
        return (f'<table class="cmp"><thead><tr><th>{left}</th><th>{right}</th></tr></thead>'
                f"<tbody>{rows}</tbody></table>")
    if kind == "timeline":
        items = "".join(f"<li>{_inline(i)}</li>" for i in block.get("items", []))
        return f'<ul class="timeline">{items}</ul>'
    if kind == "code":
        lang = block.get("lang", "")
        label = f'<span class="code-lang">{html.escape(lang)}</span>' if lang else ""
        return f'<div class="codeblock">{label}<pre><code>{html.escape(block.get("code", ""))}</code></pre></div>'
    if kind == "figure":
        spec = block.get("spec") or {}
        fig = _figure(spec)
        caption = _inline(block.get("caption", ""))
        return f'<figure class="fig">{fig}{f"<figcaption>{caption}</figcaption>" if caption else ""}</figure>'
    return ""


def _figure(spec: Dict[str, Any]) -> str:
    kind = str(spec.get("type") or "").strip().lower()
    if kind == "flow":
        steps = [str(s).strip() for s in (spec.get("steps") or [])][:7]
        steps = [s for s in steps if s]
        if len(steps) < 2:
            return ""
        parts = "".join(
            f'<span class="flow-step"><span class="flow-n">{i + 1}</span>'
            f'<span class="flow-t">{_inline(step)}</span></span>'
            for i, step in enumerate(steps))
        cols = len(steps) if len(steps) <= 4 else 3
        spoken = html.escape(" then ".join(steps), quote=True)
        return (f'<div class="flow" style="--cols:{cols}" role="img" '
                f'aria-label="{spoken}">{parts}</div>')
    if kind == "trajectory":
        n = spec.get("n") if isinstance(spec.get("n"), int) else 27
        return (f'<canvas class="plot" data-plot="trajectory" data-n="{max(3, min(int(n), 97))}"'
                f' role="img" aria-label="Trajectory of {n}"></canvas>')
    if kind in {"bars", "line"}:
        points = []
        for raw in (spec.get("points") or [])[:24]:
            if not isinstance(raw, dict):
                continue
            value = raw.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            points.append({"label": str(raw.get("label") or "")[:18], "value": float(value)})
        if len(points) < 2:
            return ""
        payload = html.escape(json.dumps(points, ensure_ascii=False), quote=True)
        xlabel = html.escape(str(spec.get("xlabel") or "")[:28], quote=True)
        ylabel = html.escape(str(spec.get("ylabel") or "")[:28], quote=True)
        label = html.escape(f"{kind} chart: {ylabel or 'values'} by {xlabel or 'item'}", quote=True)
        return (f'<canvas class="plot" data-plot="{kind}" data-points="{payload}" '
                f'data-x="{xlabel}" data-y="{ylabel}" role="img" aria-label="{label}"></canvas>')
    return ""


# --------------------------------------------------------------------------
# exercise
# --------------------------------------------------------------------------
def _exercise(ex: Dict[str, Any], index: int) -> str:
    if not ex:
        return ""
    kind = ex["kind"]
    slug_attr = f'data-kind="{kind}"'
    if kind == "mcq":
        choices = "".join(
            f'<button class="choice" data-i="{i}" type="button">'
            f'<span class="choice-key">{chr(65 + i)}</span><span>{_inline(c)}</span></button>'
            for i, c in enumerate(ex["choices"]))
        body = (f'<div class="choices" data-answer="{ex["answer"]}">{choices}</div>'
                f'<div class="feedback" aria-live="polite"></div>'
                + (f'<div class="ex-why" hidden>{_paras(ex.get("why", ""))}</div>' if ex.get("why") else ""))
    else:
        rubric = ex.get("rubric") or []
        rubric_attr = html.escape("|".join(rubric), quote=True)
        body = (
            '<textarea class="recall" rows="3" placeholder="Answer in your own words — short is fine."></textarea>'
            '<div class="recall-actions">'
            '<button class="btn primary" data-act="check" type="button">Check my answer</button>'
            '<button class="btn" data-act="reveal" type="button">Show the model answer</button>'
            '<button class="btn" data-act="ask" type="button">Ask Arden to grade it</button>'
            "</div>"
            f'<div class="feedback" aria-live="polite" data-rubric="{rubric_attr}"></div>'
            f'<div class="ex-answer" hidden>{_paras(ex.get("answer", ""))}</div>'
        )
    return (f'<section class="ex" {slug_attr} data-ex="{index}">'
            f'<div class="ex-head"><span class="ex-label">Exercise {index:02d}</span>'
            f'<span class="ex-kind">{"multiple choice" if kind == "mcq" else "recall"}</span></div>'
            f'<p class="ex-prompt">{_inline(ex["prompt"])}</p>{body}</section>')


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------
def _section(section: Dict[str, Any], index: int) -> str:
    levels = "".join(
        f'<div class="lv lv-{level}" data-lab="{level}">'
        f'{ "".join(_block(b) for b in section["levels"][level]["blocks"]) }</div>'
        for level in ("novice", "intermediate", "advanced"))
    visual = ""
    if section.get("visual"):
        visual = _figure(section["visual"])
        if visual:
            visual = f'<figure class="fig fig-lead">{visual}</figure>'
    hook = f'<p class="hook">{_inline(section["hook"])}</p>' if section.get("hook") else ""
    eyebrow = (f'<span class="sec-eyebrow"><span class="sec-num">{index:02d}</span>'
               f'{" · " + _inline(section["eyebrow"]) if section.get("eyebrow") else ""}</span>')
    ask = (f'<div class="sec-ask"><button class="ask-link" type="button"'
           f' data-sec="{section["id"]}"'
           f' data-stitle="{html.escape(str(section["title"]), quote=True)}">'
           f'Ask about this section</button></div>')
    return (f'<section class="sec" id="{section["id"]}" data-idx="{index}">'
            f'<header class="sec-head">{eyebrow}'
            f'<h2>{_inline(section["title"])}</h2>{hook}</header>'
            f"{visual}{levels}{_exercise(section.get('exercise'), index)}{ask}</section>")


# --------------------------------------------------------------------------
# page furniture
# --------------------------------------------------------------------------
def _notice(spec: Dict[str, Any]) -> str:
    """The provenance warning, as a quiet notice under the CTA — visible, not shouting."""
    prov = spec["provenance"]
    n = len(prov["sources"])
    if not prov["researched"]:
        return ('<p class="notice"><span>⚠</span><span><b>Unresearched.</b> Nothing here was checked '
                'against an outside source — it comes from the model\'s own knowledge. Where the '
                'advanced level says something is proved, treat it as a claim to verify.</span></p>')
    if n < 3:
        return (f'<p class="notice"><span>⚠</span><span><b>Thin sources:</b> only {n} source(s) survived '
                'the research pass, so the advanced level is only partly grounded.</span></p>')
    return ""


_TOOL_NAMES = {"web_search": "web search", "yt-dlp": "video search", "web_extract": "page extraction",
               "firecrawl": "Firecrawl"}


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _dated(stamp: str) -> str:
    """'2026-11-06T21:44:54Z' -> '6 Nov 2026'; anything unparseable is dropped, not shown raw."""
    try:
        import datetime as _dt

        parsed = _dt.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
        return f"{parsed.day} {parsed.strftime('%b %Y')}"
    except Exception:
        return ""


def _provenance_footer(spec: Dict[str, Any], identity: Dict[str, str]) -> str:
    prov = spec["provenance"]
    parts = []
    if prov["finders"]:
        parts.append("found with " + ", ".join(_TOOL_NAMES.get(f, f) for f in prov["finders"]))
    if prov["readers"]:
        parts.append("read with " + ", ".join(_TOOL_NAMES.get(r, r) for r in prov["readers"]))
    parts.append(_plural(len(prov["sources"]), "source") + " read")
    recommended = len(spec["resources"]["reading"])
    if recommended:
        parts.append(f"{recommended} recommended")
    vids = len(spec["resources"]["videos"])
    if vids:
        parts.append(_plural(vids, "video"))
    if prov["failures"]:
        parts.append(f'{len(prov["failures"])} dead end(s)')
    line = " · ".join(parts) if parts else "no research pass ran"
    when = _dated(prov.get("generated") or "")
    if when:
        line = f"{line} · made {when}"
    sources = ""
    if prov["sources"]:
        items = "".join(
            f'<li id="src-{i + 1}"><a href="{html.escape(s["url"], quote=True)}" '
            f'rel="noopener noreferrer" target="_blank">{_inline(s["title"] or s["url"])}</a> '
            f'<span class="prov-dom">{_inline(_domain_of(s["url"]))}</span></li>'
            # the validator keeps up to 40 sources and a marker may cite any of them, so the
            # footer has to list all of them or [n] dangles
            for i, s in enumerate(prov["sources"][:40]))
        sources = f'<div class="prov-sources"><span class="prov-label">Sources</span><ol>{items}</ol></div>'
    warnings = ""
    if prov["warnings"]:
        warnings = ('<div class="prov-warnings"><span class="prov-label">Build notes</span><ul>'
                    + "".join(f"<li>{_inline(w)}</li>" for w in prov["warnings"][:6]) + "</ul></div>")
    return ('<footer class="prov"><div class="wrap">'
            '<span class="prov-label">How this course was made</span>'
            f'<div class="prov-line">{_inline(line)}</div>'
            f'<div class="prov-design">{function_text(identity)}</div>'
            f"{sources}{warnings}</div></footer>")


def _resources(spec: Dict[str, Any]) -> str:
    res = spec["resources"]
    if not res["videos"] and not res["reading"]:
        return ""
    kind_label = {"paper": "Paper", "book": "Book", "article": "Article",
                  "docs": "Docs", "video": "Video", "talk": "Talk"}
    videos = ""
    for v in res["videos"]:
        dur = _fmt_duration(v["duration"])
        why = f'<p class="vc-why">{_inline(v["why"])}</p>' if v["why"] else ""
        meta = " · ".join(x for x in [html.escape(v["channel"]), dur] if x)
        thumb = v.get("thumb_data") or ""
        image = (f'<img class="vthumb" src="{html.escape(thumb, quote=True)}" alt="" '
                 f'loading="lazy" decoding="async">' if thumb.startswith("data:image") else "")
        videos += (
            f'<button class="vcard" type="button" data-yt="{html.escape(v["id"], quote=True)}"'
            f' data-level="{v["level"]}">'
            f'<span class="vposter" aria-hidden="true">{image}'
            f'<span class="vplay"></span>'
            f'<span class="vdur">{dur}</span></span>'
            f'<span class="vtitle">{_inline(v["title"] or v["id"])}</span>'
            f'<span class="vmeta">{html.escape(v["channel"])}</span>{why}</button>')
    reading = ""
    for r in res["reading"]:
        tag = kind_label.get(r["kind"], (r["kind"] or "Read").title())
        bits = " · ".join(str(x) for x in [tag, r["venue"], r["year"]] if x)
        why = f'<span class="rd-why">{_inline(r["why"])}</span>' if r["why"] else ""
        label = _inline(r["title"] or r["href"])
        title_html = (f'<a href="{html.escape(r["href"], quote=True)}" rel="noopener noreferrer" '
                      f'target="_blank">{label}</a>' if r["href"] else f"<span>{label}</span>")
        reading += (f'<li data-level="{r["level"]}">{title_html}'
                    f'<span class="rd-meta">{bits}</span>{why}</li>')
    return ('<section class="res-block" id="resources"><header class="sec-head">'
            '<span class="sec-eyebrow">Go further</span><h2>Watch and read next</h2>'
            '<p class="hook">Chosen for where you are: the friendliest explainer, or the paper and its critics.</p>'
            "</header>"
            + (f'<div class="videos">{videos}</div>' if videos else "")
            + (f'<ul class="reading">{reading}</ul>' if reading else "")
            + "</section>")


# --------------------------------------------------------------------------
# css
# --------------------------------------------------------------------------
_CSS = """
:root{
  --bg:{{bg}}; --surface:{{surface}}; --ink:{{ink}}; --muted:{{muted}};
  --accent:{{accent}}; --accent2:{{accent2}}; --line:{{line}}; --ok:{{ok}}; --warn:{{warn}};
  --display:{{display}}; --text:{{text}}; --mono:{{mono}};
  --fs:{{text_size}}; --lh:{{leading}}; --track:{{display_tracking}}; --dw:{{display_weight}};
  --motion:{{motion}}; --maxw:38rem;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:{{bg_d}}; --surface:{{surface_d}}; --ink:{{ink_d}}; --muted:{{muted_d}};
    --accent:{{accent_d}}; --accent2:{{accent2_d}}; --line:{{line_d}}; --ok:{{ok_d}}; --warn:{{warn_d}};
  }
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--text);font-size:var(--fs);
  line-height:var(--lh);font-kerning:normal;font-variant-ligatures:common-ligatures;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:color-mix(in srgb,var(--accent) 24%,transparent)}
a{color:var(--accent);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:.18em}
a:hover{text-decoration-thickness:2px}
.skip{position:absolute;left:-9999px}
.skip:focus{left:1rem;top:1rem;z-index:99;background:var(--surface);padding:.6rem 1rem;border-radius:8px}

/* progress hairline */
.bar{position:fixed;inset:0 0 auto 0;height:2px;background:transparent;z-index:60}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .18s linear}

/* one grid: every band shares .wrap so nothing lands on a different left edge */
.wrap{max-width:var(--maxw);margin:0 auto;padding:0 1.5rem}

/* cover */
.cover{padding:clamp(3.5rem,9vw,7rem) 0 clamp(2rem,5vw,3.5rem)}
.kicker{font-family:var(--mono);font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;
  color:var(--muted);margin:0 0 1.4rem;display:flex;gap:.7rem;align-items:center;flex-wrap:wrap}
.badge{font-family:var(--mono);font-size:.66rem;letter-spacing:.08em;text-transform:uppercase;
  border:1px solid var(--line);border-radius:999px;padding:.28em .7em;color:var(--muted)}
.badge-warn{border-color:color-mix(in srgb,var(--warn) 55%,var(--line));color:var(--warn)}
h1{font-family:var(--display);font-weight:var(--dw);font-size:clamp(2.3rem,6vw,3.7rem);
  line-height:1.02;letter-spacing:var(--track);margin:0 0 1rem;text-wrap:balance}
.sub{font-size:clamp(1.05rem,2.2vw,1.28rem);color:var(--muted);margin:0 0 1.6rem;text-wrap:pretty}
.thesis{font-size:clamp(1.1rem,2.1vw,1.32rem);line-height:1.48;margin:0 0 2rem;max-width:46ch;
  padding-left:1.15rem;border-left:2px solid var(--accent);text-wrap:pretty;color:var(--ink)}
.notice{display:flex;gap:.55rem;align-items:flex-start;margin:1.5rem 0 0;padding:.7rem .9rem;
  border:1px solid var(--line);border-left:2px solid var(--warn);border-radius:8px;
  font-size:.84rem;line-height:1.5;color:var(--muted);max-width:52ch}
.notice b{color:var(--ink);font-weight:600}
.outcomes{list-style:none;margin:0 0 2.2rem;padding:0;display:grid;gap:.45rem;max-width:52ch}
.outcomes li{position:relative;padding-left:1.4rem;color:var(--muted)}
.outcomes li::before{content:"→";position:absolute;left:0;color:var(--accent)}
.meta{display:flex;gap:1.4rem;flex-wrap:wrap;font-family:var(--mono);font-size:.72rem;
  letter-spacing:.06em;color:var(--muted);text-transform:uppercase}

/* level control */
.levels{position:sticky;top:0;z-index:50;background:color-mix(in srgb,var(--bg) 86%,transparent);
  backdrop-filter:blur(14px) saturate(1.3);border-bottom:1px solid var(--line)}
.levels .wrap{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;
  padding-top:.55rem;padding-bottom:.55rem}
.levels .lab{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;
  color:color-mix(in srgb,var(--ink) 70%,var(--bg));margin-right:.3rem}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:2px;gap:2px}
.seg button{appearance:none;border:0;background:transparent;font:inherit;
  font-size:.82rem;padding:.32em .95em;border-radius:999px;cursor:pointer;
  color:color-mix(in srgb,var(--ink) 78%,var(--bg));transition:color .15s,background .15s}
.seg button:hover{color:var(--ink);background:color-mix(in srgb,var(--ink) 7%,transparent)}
/* the selected depth is a state, not the loudest thing on the page: the CTA owns the accent */
.seg button[aria-pressed="true"]{background:color-mix(in srgb,var(--accent) 14%,var(--bg));
  color:var(--ink);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--accent) 55%,transparent)}
.seg button:focus-visible{outline:2px solid var(--accent2);outline-offset:2px}
.levels .spacer{flex:1}
.levels .pct{font-family:var(--mono);font-size:.7rem;
  color:color-mix(in srgb,var(--ink) 66%,var(--bg));letter-spacing:.08em}

/* shell + rail */
.shell{padding-bottom:6rem}
.rail{display:none}
@media (min-width:1180px){
  .rail{display:block;position:fixed;top:9rem;left:max(1rem,calc(50% - 33ch - 13.5rem));width:12rem}
  .rail ol{list-style:none;margin:0;padding:0;display:grid;gap:.7rem}
  .rail a{display:grid;grid-template-columns:1.6rem 1fr;gap:.1rem;text-decoration:none;
    color:color-mix(in srgb,var(--ink) 62%,var(--bg));
    font-size:.84rem;line-height:1.38;padding-left:.65rem;text-wrap:pretty;
    background:linear-gradient(var(--line),var(--line)) no-repeat left top / 1px 1.35em;
    transition:color .15s}
  .rail a .n{font-family:var(--mono);font-size:.68rem;opacity:.75}
  .rail a .t{text-wrap:pretty}
  .rail a:hover{color:var(--ink);background-image:linear-gradient(var(--muted),var(--muted))}
  .rail a[aria-current="true"]{color:var(--ink);
    background-image:linear-gradient(var(--accent),var(--accent))}
  .rail a.seen .n{color:var(--ok);opacity:1}
}

/* sections */
.sec{padding:clamp(3rem,6vw,4.6rem) 0 0;scroll-margin-top:4.5rem}
.sec-head{margin-bottom:1.6rem}
.sec-eyebrow{display:block;font-family:var(--mono);font-size:.68rem;letter-spacing:.16em;
  text-transform:uppercase;color:var(--accent);margin-bottom:.7rem}
.sec-num{font-family:var(--mono);font-size:.72rem;color:var(--muted);letter-spacing:.1em}
h2{font-family:var(--display);font-weight:var(--dw);font-size:clamp(1.5rem,3.4vw,2.05rem);
  line-height:1.14;letter-spacing:var(--track);margin:.3rem 0 0;text-wrap:balance}
.hook{color:var(--muted);margin:.6rem 0 0;font-size:1.02rem;max-width:56ch}
p{margin:0 0 1.05rem;text-wrap:pretty}
strong{font-weight:600}
code{font-family:var(--mono);font-size:.88em;background:var(--surface);padding:.12em .38em;
  border-radius:5px;border:1px solid var(--line)}
.m{font-style:italic;letter-spacing:.01em;white-space:nowrap}
.m sub,.m sup{font-size:.68em;font-style:normal}
.m .fn{font-style:normal;letter-spacing:0}
.cite a{text-decoration:none;font-family:var(--mono);font-size:.62em;color:var(--accent2);
  padding:0 .08em;margin-left:.06em;vertical-align:super;line-height:1;
  border-bottom:1px solid color-mix(in srgb,var(--accent2) 45%,transparent)}
.cite a:hover{border-bottom-color:var(--accent2);background:none}
.prov-dom{font-family:var(--mono);font-size:.66rem;color:var(--muted);opacity:.8;margin-left:.35rem}
.prov li:target{background:color-mix(in srgb,var(--accent) 12%,transparent);border-radius:4px}
.cta{display:flex;gap:1.1rem;align-items:center;flex-wrap:wrap;margin:0 0 2.2rem}
.cta .btn{font-size:.9rem;padding:.55em 1.2em}
.cta .est{font-family:var(--mono);font-size:.74rem;letter-spacing:.06em;text-transform:uppercase;
  color:color-mix(in srgb,var(--ink) 74%,var(--bg))}
.resume{font-family:var(--mono);font-size:.72rem;letter-spacing:.04em;margin:0 0 0;
  color:color-mix(in srgb,var(--ink) 64%,var(--bg))}
.resume[hidden]{display:none}
.pctwrap{display:flex;align-items:center;gap:.55rem}
.track{display:block;width:72px;height:2px;border-radius:2px;background:var(--line);overflow:hidden}
.track i{display:block;height:100%;width:0;background:var(--accent);transition:width .18s linear}

/* level bodies */
.lv{display:none}
html[data-level="novice"] .lv-novice,
html[data-level="intermediate"] .lv-intermediate,
html[data-level="advanced"] .lv-advanced{display:block;animation:fo var(--motion) ease both}
@keyframes fo{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

/* blocks */
.mathblock{font-size:1.3rem;padding:1.1rem 0;text-align:center;color:var(--ink)}
.aside{font-size:.92rem;color:var(--muted);border-left:1px solid var(--line);padding-left:1rem;margin:1.4rem 0}
.analogy,.callout{background:var(--surface);border-radius:12px;padding:1.1rem 1.2rem;margin:1.5rem 0;
  border-left:3px solid var(--accent2)}
.an-label,.co-title{display:block;font-family:var(--mono);font-size:.7rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--accent2);margin-bottom:.5rem}
.analogy p:last-child,.callout p:last-child{margin-bottom:0}
.callout{border-left-color:var(--accent)}
.co-honesty{border-left-color:var(--warn)}
.co-honesty .co-title{color:var(--warn)}
.co-warning{border-left-color:var(--warn)}
.co-warning .co-title{color:var(--warn)}
dl.def{margin:1.5rem 0;padding:1rem 0 0;border-top:1px solid var(--line)}
dl.def dt{font-family:var(--mono);font-size:.74rem;letter-spacing:.1em;text-transform:uppercase;color:var(--accent)}
dl.def dd{margin:.4rem 0 0}
dl.def dd p:last-child{margin-bottom:0}
figure.quote{margin:1.8rem 0;padding:1.2rem 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
figure.quote blockquote{margin:0;font-size:1.16rem;line-height:1.5}
figure.quote blockquote p:last-child{margin:0}
figure.quote figcaption{margin-top:.7rem;font-family:var(--mono);font-size:.72rem;color:var(--muted)}
ol.steps{counter-reset:s;list-style:none;padding:0;margin:1.4rem 0;display:grid;gap:.8rem}
ol.steps li{counter-increment:s;position:relative;padding-left:2.4rem}
ol.steps li::before{content:counter(s);position:absolute;left:0;top:.05em;width:1.6rem;height:1.6rem;
  display:grid;place-items:center;border-radius:50%;border:1px solid var(--accent);color:var(--accent);
  font-family:var(--mono);font-size:.72rem}
table.cmp{width:100%;border-collapse:collapse;margin:1.5rem 0;font-size:.95rem}
table.cmp th{text-align:left;font-family:var(--mono);font-size:.7rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);padding:.5rem .8rem .5rem 0}
table.cmp td{padding:.6rem .8rem .6rem 0;border-bottom:1px solid var(--line);vertical-align:top}
ul.timeline{list-style:none;padding:0;margin:1.5rem 0;border-left:1px solid var(--line)}
ul.timeline li{position:relative;padding:.15rem 0 .9rem 1.4rem}
ul.timeline li::before{content:"";position:absolute;left:-4px;top:.55em;width:7px;height:7px;
  border-radius:50%;background:var(--accent)}
.codeblock{background:var(--surface);border:1px solid var(--line);border-radius:10px;margin:1.5rem 0;overflow:hidden}
.code-lang{display:block;font-family:var(--mono);font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);padding:.5rem .9rem;border-bottom:1px solid var(--line)}
pre{margin:0;padding:.9rem 1rem;overflow-x:auto}
pre code{background:none;border:0;padding:0;font-size:.85rem;line-height:1.6}
.checkpoint{border:1px dashed var(--line);border-radius:12px;padding:1rem 1.2rem;margin:1.6rem 0}
.cp-label{display:block;font-family:var(--mono);font-size:.68rem;letter-spacing:.12em;
  text-transform:uppercase;color:var(--accent);margin-bottom:.4rem}
.checkpoint p:last-child{margin:0}

/* figures */
figure.fig{margin:2rem 0}

/* ---- layouts: the identity's third axis has to change something real ---- */
html[data-layout="column"] .rail,
html[data-layout="bleed"] .rail{display:none!important}
html[data-layout="column"] .wrap{max-width:34rem}
html[data-layout="bleed"] .wrap{max-width:42rem}
@media (min-width:1180px){
  /* bleed: figures break out of the text column while prose stays readable */
  html[data-layout="bleed"] figure.fig{margin-inline:calc(-50vw + 50% + 3rem);max-width:none}
  html[data-layout="bleed"] figure.fig figcaption{margin-inline:auto;max-width:34rem}
}
.fig-lead{margin:0 0 2rem}
canvas.plot{width:100%;height:clamp(180px,26vw,260px);display:block;border-radius:10px;background:var(--surface)}
.flow{display:grid;grid-template-columns:repeat(var(--cols,3),minmax(0,1fr));gap:.5rem;
  padding:1rem;border-radius:12px;background:var(--surface);border:1px solid var(--line)}
@media (max-width:720px){.flow{grid-template-columns:repeat(2,minmax(0,1fr))!important}}
.flow-step{display:grid;gap:.3rem;padding:.65rem .75rem;border-radius:9px;background:var(--bg);
  border:1px solid var(--line)}
.flow-n{font-family:var(--mono);font-size:.6rem;letter-spacing:.1em;color:var(--accent)}
.flow-t{font-size:.9rem;line-height:1.32;font-weight:500}
figure.fig figcaption{margin-top:.6rem;font-size:.82rem;color:var(--muted)}

/* exercise */
.ex{margin:2.4rem 0 0;border:1px solid var(--line);border-radius:14px;padding:1.2rem 1.3rem;background:var(--surface)}
.ex-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:.7rem}
.ex-label,.ex-kind{font-family:var(--mono);font-size:.66rem;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
.ex-label{color:var(--accent)}
.ex-prompt{font-weight:500;margin:0 0 1rem}
.choices{display:grid;gap:.5rem}
.choice{display:flex;gap:.7rem;align-items:baseline;text-align:left;font:inherit;font-size:.96rem;
  background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:10px;
  padding:.65rem .8rem;cursor:pointer;transition:border-color .15s,transform .1s}
.choice:hover{border-color:var(--muted)}
.choice:active{transform:translateY(1px)}
.choice-key{font-family:var(--mono);font-size:.72rem;color:var(--muted);border:1px solid var(--line);
  border-radius:5px;padding:.05em .35em}
.choice[data-state="right"]{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,var(--bg))}
.choice[data-state="wrong"]{border-color:var(--accent);color:var(--muted)}
.choice[data-state="wrong"] .choice-key{border-color:var(--accent)}
.recall{width:100%;font:inherit;font-size:.96rem;background:var(--bg);color:var(--ink);
  border:1px solid var(--line);border-radius:10px;padding:.7rem .8rem;resize:vertical}
.recall:focus-visible{outline:2px solid var(--accent2);outline-offset:1px}
.recall-actions{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.6rem}
.btn{font:inherit;font-size:.84rem;background:transparent;color:var(--ink);border:1px solid var(--line);
  border-radius:999px;padding:.4em .95em;cursor:pointer;transition:border-color .15s,background .15s}
.btn:hover{border-color:var(--muted)}
.btn.primary{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.btn.primary:hover{background:var(--accent);border-color:var(--accent);color:var(--bg)}
.feedback{font-size:.9rem;margin-top:.8rem;min-height:1.2em;color:var(--muted)}
.feedback.ok{color:var(--ok)}
.feedback.no{color:var(--accent)}
.ex-answer,.ex-why{margin-top:.8rem;padding-top:.8rem;border-top:1px solid var(--line);font-size:.94rem}
.ex-answer p:last-child,.ex-why p:last-child{margin:0}

.sec-ask{display:flex;margin:1.7rem 0 0}
.ask-link{background:none;border:0;font:inherit;font-size:.82rem;cursor:pointer;padding:0 0 .12rem;
  color:color-mix(in srgb,var(--ink) 68%,var(--bg));
  border-bottom:1px solid color-mix(in srgb,var(--accent2) 42%,transparent);
  transition:color .15s,border-color .15s}
.ask-link:hover{color:var(--ink);border-bottom-color:var(--accent2)}
.ask-link:focus-visible{outline:2px solid var(--accent2);outline-offset:3px}

/* resources */
.res-block{padding-top:clamp(3rem,6vw,4.6rem)}
.videos{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fill,minmax(min(100%,16rem),1fr))}
.vcard{display:grid;grid-template-rows:auto auto auto 1fr;align-content:start;gap:.45rem;text-align:left;font:inherit;background:transparent;border:0;padding:0;cursor:pointer;color:inherit}
.vposter{position:relative;display:block;aspect-ratio:16/9;border-radius:12px;
  background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 22%,var(--surface)),
  color-mix(in srgb,var(--accent2) 30%,var(--surface)));border:1px solid var(--line);overflow:hidden;
  transition:transform .18s ease,border-color .18s}
.vthumb{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.vcard:hover .vposter{transform:translateY(-2px);border-color:var(--muted)}
.vplay{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:2.6rem;height:2.6rem;
  border-radius:50%;background:color-mix(in srgb,var(--bg) 82%,transparent);display:grid;
  place-items:center;backdrop-filter:blur(4px);box-shadow:0 2px 10px rgba(0,0,0,.18)}
.vplay::after{content:"";border-left:.6rem solid var(--ink);border-top:.38rem solid transparent;
  border-bottom:.38rem solid transparent;margin-left:.18rem}
.vdur{position:absolute;right:.5rem;bottom:.5rem;font-family:var(--mono);font-size:.62rem;
  background:color-mix(in srgb,var(--bg) 86%,transparent);color:var(--ink);border-radius:4px;
  padding:.1rem .35rem}
.vtitle{font-family:var(--display);font-weight:600;font-size:1rem;line-height:1.28;text-wrap:pretty;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.vmeta{font-family:var(--mono);font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.vc-why{font-size:.86rem;color:var(--muted);margin:0}
.vframe{width:100%;aspect-ratio:16/9;border:1px solid var(--line);border-radius:12px;background:#000}
ul.reading{list-style:none;padding:0;margin:1.6rem 0 0;display:grid;gap:.9rem}
ul.reading li{display:grid;gap:.15rem;padding-bottom:.8rem;border-bottom:1px solid var(--line)}
ul.reading a{font-weight:500;text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--accent) 45%,transparent)}
.rd-meta{font-family:var(--mono);font-size:.68rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.rd-why{font-size:.85rem;color:var(--muted)}

/* provenance */
.prov{margin-top:5rem;border-top:1px solid var(--line);padding:1.6rem 0 3.5rem;
  font-size:.84rem;color:var(--muted)}
.prov .wrap{display:grid;gap:.2rem}
.prov-line{font-family:var(--mono);font-size:.72rem;letter-spacing:.04em}
.prov-design{margin-top:.7rem;font-family:var(--mono);font-size:.68rem;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted);opacity:.75}
.prov-label{display:block;font-family:var(--mono);font-size:.66rem;letter-spacing:.13em;
  text-transform:uppercase;color:var(--muted);margin:1.2rem 0 .4rem}
.prov ol,.prov ul{margin:0;padding-left:1.1rem;display:grid;gap:.25rem}
.prov a{color:var(--muted)}

/* ask */
.ask{margin-top:1.6rem;display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
.ask .hint{font-size:.8rem;color:var(--muted)}

/* print */
@media print{
  /* print is for reading on paper: drop the apparatus, keep the words and the figures */
  .levels,.rail,.bar,.sec-ask,.resume,.recall-actions,.vplay,.vthumb,.ask,.track,.cta,.vposter,.prov-warnings{display:none!important}
  body{background:#fff;color:#000;font-size:11pt;max-width:none}
  .wrap{max-width:none;padding:0 .5in}
  /* dark surfaces become outlined boxes: a filled card either eats ink or loses its text */
  .analogy,.callout,.notice,.checkpoint,.ex,pre,code,dl.def{
    background:#fff!important;color:#000!important;border:1px solid #999!important;
    box-shadow:none!important;border-radius:4px}
  .an-label,.co-insight,.sec-eyebrow,.ex-label,.ex-kind,.sub,.hook,.muted,.lv::before{color:#333!important}
  /* pale prose is the last thing that survives a grayscale printer badly */
  .thesis,.outcomes li,.lv,.lv p,.lv li,.vcard,.vc-why{color:#000!important}
  .an-label,[class*="co-"]{color:#333!important}
  .outcomes li::before{color:#333!important}
  a{color:#000;text-decoration:underline}
  a[href^="http"]::after{content:" (" attr(href) ")";font-size:8pt;color:#555;word-break:break-all}
  .lv{display:block!important;margin-bottom:1.3rem}
  .lv::before{content:attr(data-lab);display:block;font-weight:600;font-size:8.5pt;letter-spacing:.12em;
    text-transform:uppercase;color:#000!important;margin-bottom:.35rem;border-top:1px solid #999;padding-top:.4rem}
  .sec{page-break-inside:auto}
  .sec-head,.ex,figure.fig,.vcard,.checkpoint{page-break-inside:avoid}
  iframe,canvas{display:none!important}
  .thesis{border-left:2px solid #666}
  .prov{border-top:1px solid #ccc;color:#333}
  .cite a{border-bottom:0;color:#000}
}
"""


_JS = """
(function(){
  var root=document.documentElement;
  var store=(function(){try{localStorage.setItem('cw','1');localStorage.removeItem('cw');return localStorage}catch(e){return null}})();
  var KEY='coursewright:'+document.body.dataset.slug;
  var state={level:root.dataset.level,seen:{},answers:{}};
  try{var raw=store&&store.getItem(KEY);if(raw)state=Object.assign(state,JSON.parse(raw))}catch(e){}
  /* a stored level from another build (or a corrupted one) would set data-level to a name no
     rule matches, which hides every level body: fall back to the page's own default */
  var LEVELS={novice:1,intermediate:1,advanced:1};
  if(!LEVELS[state.level])state.level=root.dataset.level||'intermediate';
  function save(){try{store&&store.setItem(KEY,JSON.stringify(state))}catch(e){}}
  function applyLevel(level,persist){
    root.dataset.level=level;state.level=level;
    document.querySelectorAll('.seg button').forEach(function(b){
      b.setAttribute('aria-pressed',String(b.dataset.level===level));
    });
    if(persist)save();
  }
  document.querySelectorAll('.seg button').forEach(function(b){
    b.addEventListener('click',function(){applyLevel(b.dataset.level,true)});
  });
  applyLevel(state.level,false);

  /* keyboard: j/k or arrows jump between sections, 1/2/3 pick depth, g goes to the CTA */
  var DEPTH_KEYS={'1':'novice','2':'intermediate','3':'advanced'};
  function jumpSection(delta){
    if(!secs.length)return;
    var probe=Math.min(innerHeight*0.34,260),cur=0;
    secs.forEach(function(s,i){if(s.getBoundingClientRect().top<=probe)cur=i});
    var next=Math.min(secs.length-1,Math.max(0,cur+delta));
    if(next===cur&&delta<0){window.scrollTo({top:0,behavior:'smooth'});return}
    // one scroll call with an explicit offset: scrollIntoView + scrollBy cancels the animation
    var y=secs[next].getBoundingClientRect().top+scrollY-64;
    window.scrollTo({top:Math.max(0,y),behavior:'smooth'});
  }
  document.addEventListener('keydown',function(e){
    if(e.metaKey||e.ctrlKey||e.altKey)return;
    var tag=(e.target&&e.target.tagName)||'';
    if(/^(INPUT|TEXTAREA|SELECT)$/.test(tag))return;
    var k=(e.key||'').toLowerCase();
    if(DEPTH_KEYS[k]){applyLevel(DEPTH_KEYS[k],true);e.preventDefault();return}
    if(k==='j'||k==='arrowdown'){jumpSection(1);e.preventDefault();return}
    if(k==='k'||k==='arrowup'){jumpSection(-1);e.preventDefault();return}
    if(k==='g'||k==='home'){window.scrollTo({top:0,behavior:'smooth'});e.preventDefault()}
  });

  /* progress + rail */
  var bar=document.querySelector('.bar i');
  var secs=[].slice.call(document.querySelectorAll('.sec'));
  var links={};document.querySelectorAll('.rail a').forEach(function(a){links[a.dataset.target]=a});
  function pct(){
    var max=document.body.scrollHeight-innerHeight;
    var p=max>0?Math.min(1,Math.max(0,scrollY/max)):0;
    if(bar)bar.style.width=(p*100).toFixed(1)+'%';
    var tf=document.getElementById('trackfill');if(tf)tf.style.width=(p*100).toFixed(1)+'%';
    var lab=document.getElementById('pctlab');if(lab)lab.textContent=Math.round(p*100)+'% read';
    currentFromScroll();
  }
  function currentFromScroll(){
    if(!secs.length)return;
    var probe=Math.min(innerHeight*0.34,260),active=secs[0].id;
    secs.forEach(function(s){if(s.getBoundingClientRect().top<=probe)active=s.id});
    Object.keys(links).forEach(function(k){links[k].setAttribute('aria-current',String(k===active))});
  }
  addEventListener('scroll',pct,{passive:true});addEventListener('resize',pct);pct();

  if('IntersectionObserver' in window){
    var io=new IntersectionObserver(function(entries){
      entries.forEach(function(e){
        if(!e.isIntersecting)return;
        var id=e.target.id;
        if(e.intersectionRatio>0.25&&!state.seen[id]){state.seen[id]=1;save();
          if(links[id])links[id].classList.add('seen')}
      })
    },{threshold:[0.25,0.6]});
    secs.forEach(function(s){io.observe(s)});
    Object.keys(state.seen).forEach(function(id){if(links[id])links[id].classList.add('seen')});
  }

  /* primary action: land on the first section you have not read */
  var startBtn=document.getElementById('start');
  var resume=document.getElementById('resume');
  if(resume){
    var seenN=Object.keys(state.seen).length;
    if(seenN){
      resume.hidden=false;
      resume.textContent=seenN+' of '+secs.length+' sections read · depth '+state.level;
    }
  }
  if(startBtn){
    var unseen=secs.filter(function(s){return !state.seen[s.id]});
    var target=unseen[0]||secs[0];
    if(Object.keys(state.seen).length)startBtn.textContent='Continue';
    startBtn.addEventListener('click',function(){
      if(!target)return;
      target.scrollIntoView({behavior:'smooth',block:'start'});
      scrollBy(0,-64);
    });
  }

  /* exercises */
  document.querySelectorAll('.ex').forEach(function(ex){
    var kind=ex.dataset.kind,key=ex.dataset.ex;
    if(kind==='mcq'){
      var box=ex.querySelector('.choices'),ans=Number(box.dataset.answer);
      var fb=ex.querySelector('.feedback'),why=ex.querySelector('.ex-why');
      box.addEventListener('click',function(ev){
        var btn=ev.target.closest('.choice');if(!btn)return;
        var i=Number(btn.dataset.i),right=i===ans;
        box.querySelectorAll('.choice').forEach(function(c){
          c.disabled=true;c.dataset.state=Number(c.dataset.i)===ans?'right':'';
        });
        if(!right)btn.dataset.state='wrong';
        fb.className='feedback '+(right?'ok':'no');
        fb.textContent=right?'Correct.':'Not quite — the marked answer is the one that holds.';
        if(why)why.hidden=false;
        state.answers[key]={k:'mcq',v:i,ok:right?1:0};save();
      });
      var mcqSaved=state.answers[key];
      if(mcqSaved&&mcqSaved.k==='mcq'){
        var prev=box.querySelector('.choice[data-i="'+mcqSaved.v+'"]');
        if(prev)prev.click();
      }
    }else{
      var ta=ex.querySelector('.recall'),fb=ex.querySelector('.feedback'),
          ansBox=ex.querySelector('.ex-answer'),
          rubric=(fb.dataset.rubric||'').split('|').filter(Boolean);
      ex.querySelector('[data-act="reveal"]').addEventListener('click',function(){
        ansBox.hidden=!ansBox.hidden;
      });
      ex.querySelector('[data-act="check"]').addEventListener('click',function(){
        var text=(ta.value||'').toLowerCase();
        if(text.trim().length<8){fb.className='feedback no';fb.textContent='Write a sentence first.';return}
        // stem match: "verify" should be satisfied by "verifies", "sign" by "signature",
        // and "one-way" by either half. Exact substrings failed learners for using English.
        var tokens=(text.match(/[a-z0-9]+/g)||[]);
        function covers(item){
          return item.toLowerCase().split(/[^a-z0-9]+/).filter(function(w){return w.length>=3})
            .some(function(w){
              var stem=w.slice(0,Math.max(3,Math.min(w.length,5)));
              return tokens.some(function(t){return t.indexOf(stem)>=0});
            });
        }
        var hit=rubric.filter(covers);
        var miss=rubric.filter(function(i){return hit.indexOf(i)<0});
        fb.className='feedback '+(miss.length?'no':'ok');
        fb.innerHTML='You covered <strong>'+hit.length+' of '+rubric.length+'</strong> key ideas.'+
          (miss.length?' Missing: '+miss.map(function(m){
            var d=document.createElement('em');d.textContent=m;return d.outerHTML}).join(', ')+'.':'');
        state.answers[key]={k:'recall',v:(ta.value||'').slice(0,300),ok:miss.length?0:1};save();
      });
      var recallSaved=state.answers[key];
      if(recallSaved&&recallSaved.k==='recall'&&recallSaved.v){
        ta.value=recallSaved.v;
        ex.querySelector('[data-act="check"]').click();
      }
      ex.querySelector('[data-act="ask"]').addEventListener('click',function(){
        var text=(ta.value||'').trim();
        if(!text){fb.className='feedback no';fb.textContent='Write an answer first.';return}
        var prompt=askPrompt(ex,text);
        send(prompt,function(ok,err){
          fb.className='feedback '+(ok?'ok':'no');
          fb.textContent=ok?'Sent to the chat — Arden will grade it there.':'Could not send ('+err+').';
        });
      });
    }
  });

  /* the ONE channel out of the page */
  function askPrompt(ex,text){
    var sec=ex.closest('.sec');
    var slug=document.body.dataset.slug||'';
    var sid=sec?sec.id:'?';
    var h2=sec&&sec.querySelector('h2');
    var stitle=h2?h2.textContent.trim().slice(0,44):'';
    var lvl=document.documentElement.getAttribute('data-level')||'intermediate';
    var fb=ex.querySelector('.feedback');
    var rubric=((fb&&fb.getAttribute('data-rubric'))||'').replace(/[|]/g,', ');
    var q='Coursewright grade — course "'+slug+'", section '+sid+' ("'+stitle+'", level '+lvl+
      '). Question: '+(ex.querySelector('.ex-prompt').textContent||'').trim().replace(/[.?!]+$/,'').slice(0,110)+
      '. Rubric keywords: '+rubric.slice(0,80)+
      '. My answer: '+text.slice(0,140);
    return q.slice(0,490);
  }
  function send(prompt,cb){
    if(window.hermes&&typeof window.hermes.send==='function'){
      window.hermes.send(prompt).then(function(ack){cb(!!(ack&&ack.ok),ack&&ack.error||'error')});
    }else{
      try{navigator.clipboard&&navigator.clipboard.writeText(prompt)}catch(e){}
      cb(false,'no bridge — copied to clipboard instead');
    }
  }
  document.querySelectorAll('[data-hermes-send]').forEach(function(b){
    b.addEventListener('click',function(){send(b.getAttribute('data-hermes-send'),function(ok,err){
      if(!ok){b.textContent='Copied to clipboard';}
    })});
  });
  document.querySelectorAll('.ask-link').forEach(function(b){
    b.addEventListener('click',function(){
      var slug=document.body.dataset.slug||'';
      var lvl=document.documentElement.getAttribute('data-level')||'intermediate';
      var t=(b.getAttribute('data-stitle')||'').slice(0,60);
      var p=('Coursewright: course "'+slug+'", section '+b.getAttribute('data-sec')+' ("'+t+'"), '
        +'I am reading at '+lvl+' depth. Take my understanding one step further: give a concrete '
        +'example, name the one thing most people get wrong here, and say what I should be able to '
        +'do after this section. Keep it under 200 words.').slice(0,490);
      var label=b.textContent;
      send(p,function(ok){
        b.textContent=ok?'Asked — see the chat':'Copied to your clipboard';
        setTimeout(function(){b.textContent=label;},2600);
      });
    });
  });

  /* videos: poster -> nocookie embed on click */
  document.querySelectorAll('.vcard').forEach(function(card){
    card.addEventListener('click',function(){
      var id=card.dataset.yt;if(!id)return;
      var frame=document.createElement('iframe');
      frame.className='vframe';frame.src='https://www.youtube-nocookie.com/embed/'+id+'?autoplay=1&rel=0';
      frame.title=card.querySelector('.vtitle')?card.querySelector('.vtitle').textContent:'Video';
      frame.allow='accelerometer; autoplay; clipboard-write; encrypted-media; picture-in-picture';
      frame.allowFullscreen=true;frame.loading='lazy';
      card.replaceWith(frame);
    });
  });

  /* figures */
  var reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
  function css(name){return getComputedStyle(root).getPropertyValue(name).trim()}
  function draw(){
    document.querySelectorAll('canvas[data-plot]').forEach(function(cv){
      var dpr=Math.min(devicePixelRatio||1,2.5);
      var w=cv.clientWidth||600,h=cv.clientHeight||220;
      cv.width=w*dpr;cv.height=h*dpr;
      var g=cv.getContext('2d');g.scale(dpr,dpr);g.clearRect(0,0,w,h);
      var ink=css('--ink'),accent=css('--accent'),muted=css('--muted'),line=css('--line');
      var n=Number(cv.dataset.n)||27,pad={l:34,r:10,t:14,b:22};
      if(cv.dataset.plot==='trajectory'){
        var seq=[n],x=n;
        while(x!==1&&seq.length<1200){x=x%2?3*x+1:x/2;seq.push(x)}
        var max=Math.max.apply(null,seq),peak=seq.indexOf(max),steps=seq.length-1,min=1;
        var X=function(i){return pad.l+i*(w-pad.l-pad.r)/Math.max(1,seq.length-1)};
        var Y=function(v){return pad.t+(h-pad.t-pad.b)*(1-(Math.log(v)-Math.log(min))/(Math.log(max)-Math.log(min)))};
        g.strokeStyle=line;g.lineWidth=1;
        for(var k=0;k<4;k++){var yy=pad.t+(h-pad.t-pad.b)*k/3;g.beginPath();g.moveTo(pad.l,yy);g.lineTo(w-pad.r,yy);g.stroke()}
        g.strokeStyle=accent;g.lineWidth=2;g.lineJoin='round';g.beginPath();
        seq.forEach(function(v,i){i?g.lineTo(X(i),Y(v)):g.moveTo(X(i),Y(v))});g.stroke();
        g.fillStyle=accent;
        g.beginPath();g.arc(X(0),Y(n),3,0,7);g.fill();
        g.beginPath();g.arc(X(peak),Y(max),3.8,0,7);g.fill();
        g.beginPath();g.arc(X(seq.length-1),Y(1),3,0,7);g.fill();
        g.fillStyle=muted;g.font='11px ui-monospace,monospace';
        g.fillText('start '+n,4,pad.t+4);g.fillText('log scale',4,pad.t+18);
        var pk='peak '+max.toLocaleString();
        g.fillText(pk,Math.min(X(peak)+7,w-pad.r-g.measureText(pk).width-2),Math.max(pad.t+11,Y(max)-7));
        var st=steps.toLocaleString()+' steps';
        g.fillText(st,w-pad.r-g.measureText(st).width,h-6);
      }else if(cv.dataset.plot==='bars'||cv.dataset.plot==='line'){
        var pts=[];
        try{pts=JSON.parse(cv.dataset.points||'[]')}catch(e){pts=[]}
        if(pts.length<2)return;
        var vals=pts.map(function(p){return p.value});
        var hi=Math.max.apply(null,vals),lo=Math.min(0,Math.min.apply(null,vals));
        if(hi===lo)hi=lo+1;
        var X=function(i){return pad.l+i*(w-pad.l-pad.r)/Math.max(1,pts.length-1)};
        var Y=function(v){return pad.t+(h-pad.t-pad.b)*(1-(v-lo)/(hi-lo))};
        g.strokeStyle=line;g.lineWidth=1;
        for(var k=0;k<4;k++){var yy=pad.t+(h-pad.t-pad.b)*k/3;g.beginPath();g.moveTo(pad.l,yy);g.lineTo(w-pad.r,yy);g.stroke()}
        g.fillStyle=muted;g.font='11px ui-monospace,monospace';
        var yl=cv.dataset.y||'';if(yl)g.fillText(yl.slice(0,20),4,pad.t+4);
        var maxIdx=vals.indexOf(hi);
        if(cv.dataset.plot==='bars'){
          var bw=(w-pad.l-pad.r)/pts.length;
          pts.forEach(function(p,idx){
            var hh=Math.max(1,Y(lo)-Y(p.value));
            g.globalAlpha=p.value===hi?1:0.45;g.fillStyle=accent;
            g.fillRect(pad.l+idx*bw+bw*0.18,Y(p.value),Math.max(1,bw*0.64),hh);g.globalAlpha=1;
          });
        }else{
          g.strokeStyle=accent;g.lineWidth=2;g.lineJoin='round';g.beginPath();
          pts.forEach(function(p,i){i?g.lineTo(X(i),Y(p.value)):g.moveTo(X(i),Y(p.value))});
          g.stroke();
          g.fillStyle=accent;
          [maxIdx,0,pts.length-1].forEach(function(i){
            g.beginPath();g.arc(X(i),Y(pts[i].value),3,0,7);g.fill();
          });
        }
        g.fillStyle=muted;
        var first=pts[0].label||'',last=pts[pts.length-1].label||'';
        if(first)g.fillText(first.slice(0,14),pad.l,h-6);
        if(last){var lw=g.measureText(last.slice(0,14)).width;g.fillText(last.slice(0,14),w-pad.r-lw,h-6)}
        var mv=hi.toLocaleString();
        g.fillText(mv,Math.min(Math.max(X(maxIdx)-8,pad.l+2),w-pad.r-g.measureText(mv).width),Math.max(pad.t+16,Y(hi)-6));
      }
    });
  }
  draw();addEventListener('resize',draw);matchMedia('(prefers-color-scheme: dark)').addEventListener('change',draw);
})();
"""


def render(spec: Dict[str, Any], *, mode: str = "auto") -> str:
    """Build the full HTML document for a validated course spec."""
    identity = tokens.resolve(spec.get("identity"))
    palette = tokens.PALETTES[identity["palette"]]
    pairing = tokens.TYPE_PAIRINGS[identity["type_pairing"]]
    motion = tokens.MOTIONS[identity["motion"]]

    css = _fill(_CSS, {
        "bg": palette["light"]["bg"], "surface": palette["light"]["surface"], "ink": palette["light"]["ink"],
        "muted": palette["light"]["muted"], "accent": palette["light"]["accent"],
        "accent2": palette["light"]["accent2"], "line": palette["light"]["line"],
        "ok": palette["light"]["ok"], "warn": palette["light"]["warn"],
        "bg_d": palette["dark"]["bg"], "surface_d": palette["dark"]["surface"], "ink_d": palette["dark"]["ink"],
        "muted_d": palette["dark"]["muted"], "accent_d": palette["dark"]["accent"],
        "accent2_d": palette["dark"]["accent2"], "line_d": palette["dark"]["line"],
        "ok_d": palette["dark"]["ok"], "warn_d": palette["dark"]["warn"],
        "display": pairing["display"], "text": pairing["text"], "mono": pairing["mono"],
        "text_size": pairing["text_size"], "leading": pairing["leading"],
        "display_tracking": pairing["display_tracking"], "display_weight": pairing["display_weight"],
        "motion": f"{0.18 * max(motion, 0.0001):.2f}s" if motion else "0.001s",
    })

    sections = "".join(_section(s, i + 1) for i, s in enumerate(spec["sections"]))
    rail_items = "".join(
        f'<li><a href="#{s["id"]}" data-target="{s["id"]}"><span class="n">{i + 1:02d}</span>'
        f'<span class="t">{_inline(s["title"])}</span></a></li>'
        for i, s in enumerate(spec["sections"]))
    outcomes = "".join(f"<li>{_inline(o)}</li>" for o in spec["outcomes"])
    words = sum(len(_TAG_RE.sub("", "".join(b.get("md", "") for lv in s["levels"].values() for b in lv["blocks"])).split())
                for s in spec["sections"])
    minutes = max(4, round(words / 190))
    level_labels = (("novice", "Novice"), ("intermediate", "Intermediate"), ("advanced", "Advanced"))
    seg = "".join(f'<button type="button" data-level="{key}" aria-pressed="false">{label}</button>'
                  for key, label in level_labels)
    first_section = spec["sections"][0]["id"] if spec["sections"] else ""
    ask = (f'<div class="ask"><button class="btn" type="button" '
           f'data-hermes-send="Coursewright: I want to go deeper on this course — {html.escape(spec["title"], quote=True)}. '
           f'Give me the next thing to study.">Ask about this course</button>'
           f'<span class="hint">opens as a message in the chat</span></div>')

    return (
        "<!doctype html>\n"
        f'<html lang="{html.escape(spec["language"], quote=True)}" data-level="{spec["default_level"]}" '
        f'data-layout="{html.escape(identity["layout"], quote=True)}">\n<head>\n'
        '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>{html.escape(spec["title"])}</title>\n'
        f'<meta name="description" content="{html.escape(spec["thesis"] or spec["subtitle"], quote=True)}">\n'
        f'<meta name="coursewright:identity" content="{html.escape(identity["palette"] + "/" + identity["type_pairing"] + "/" + identity["layout"] + "/" + identity["motion"], quote=True)}">\n'
        f"<style>{css}</style>\n</head>\n"
        f'<body data-slug="{html.escape(spec["slug"], quote=True)}" data-title="{html.escape(spec["title"], quote=True)}">\n'
        '<div class="bar" aria-hidden="true"><i></i></div>\n'
        f'<a class="skip" href="#{first_section}">Skip to the course</a>\n'
        '<header class="cover"><div class="wrap">\n'
        f'<p class="kicker"><span>Course</span><span>·</span><span>{html.escape(spec["language"].upper())}</span></p>\n'
        f'<h1>{_inline(spec["title"])}</h1>\n'
        + (f'<p class="sub">{_inline(spec["subtitle"])}</p>\n' if spec["subtitle"] else "")
        + (f'<p class="thesis">{_inline(spec["thesis"])}</p>\n' if spec["thesis"] else "")
        + (f'<ul class="outcomes">{outcomes}</ul>\n' if outcomes else "")
        + f'<div class="cta"><button class="btn primary" type="button" id="start">Start reading</button>'
        + f'<span class="est">{len(spec["sections"])} sections · ~{minutes} min · starts at {spec["default_level"]}</span></div>\n'
        + '<div class="resume" id="resume" hidden></div>\n'
        + _notice(spec)
        + "</div></header>\n"
        + '<div class="levels"><div class="wrap"><span class="lab">Depth</span>'
        + f'<div class="seg" role="group" aria-label="Level of depth">{seg}</div>'
        + '<span class="spacer"></span>'
        + '<span class="pctwrap"><span class="track"><i id="trackfill"></i></span>'
        + '<span class="pct" id="pctlab">0% read</span></span></div></div>\n'
        + (f'<nav class="rail" aria-label="Sections"><ol>{rail_items}</ol></nav>\n' if rail_items else "")
        + f'<div class="shell"><div class="wrap"><main>{sections}{_resources(spec)}{ask}</main></div></div>\n'
        + _provenance_footer(spec, identity)
        + f"<script>{_JS}</script>\n</body>\n</html>\n"
    )


def function_text(identity: Dict[str, str]) -> str:
    """Small credit line: which identity the page was drawn with (auditable)."""
    bits = [identity["palette"], identity["type_pairing"].replace("-", " "),
            identity["layout"], identity["motion"]]
    return "<span>design · " + " / ".join(bits) + "</span>"
