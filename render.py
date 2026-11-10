"""Renderer: validated course spec -> one self-contained HTML page.

No CDNs, no web fonts, no build step, no network. The model never writes HTML or
CSS; it fills the spec, and this module draws it from the design-token space in
`tokens.py`.
"""

from __future__ import annotations

import contextvars
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
# Only a known tag name is stripped. `<[^>]+>` also ate the middle of ordinary prose:
# "if n < 3 and m > 5" lost "3 and m", and "x <= y >= z" became "x = z".
_HTML_TAGS = (
    "a|abbr|b|big|blockquote|br|center|cite|code|dd|del|div|dl|dt|em|font|h[1-6]|hr|i|iframe|"
    "img|ins|kbd|li|mark|ol|p|pre|q|s|samp|script|small|span|strike|strong|style|sub|sup|"
    "table|tbody|td|th|thead|tr|u|ul|var|wbr"
)
_TAG_RE = re.compile(r"</?(?:" + _HTML_TAGS + r")(?:\s[^<>]*)?/?>", re.I)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITAL_RE = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
# a bare [3] (not a markdown link) is a citation marker for the source list in the footer
_CITE_RE = re.compile(r"\[(\d{1,2})\](?!\()")
# How many sources the footer will actually list, set by render() for the page being drawn.
# An unsourced course has no #src-N anchor, so a marker must not become a link to nothing;
# a contextvar rather than a module global because two subjects can render in two threads.
_CITE_LIMIT: contextvars.ContextVar = contextvars.ContextVar("coursewright_cite_limit",
                                                            default=None)


def _cite(match: re.Match) -> str:
    limit = _CITE_LIMIT.get()
    number = int(match.group(1))
    if limit is not None and not 1 <= number <= limit:
        return ""       # nothing to cite: the sentence stands, the dead marker does not
    return f'<sup class="cite"><a href="#src-{number}">{number}</a></sup>'



# ---- math tokens in ordinary prose and figure labels -------------------------------
# `W_Q` is a subscript; `web_search` is an identifier. The difference is the base: one
# letter (or a chemical-style `H_2O`) is math, a multi-letter base is a word with an
# underscore in it. These run in prose, steps, comparison cells and flow labels, where
# `_scripts(aggressive=True)` would mangle identifiers.
_BARE_SCRIPT_RE = re.compile(
    r"(?<![\w])([A-Za-z])_(\{[^}\n]{1,12}\}|[A-Za-z0-9]{1,3}|[a-z]{4,12})"
    r"(?:\^(\{[^}\n]{1,12}\}|[A-Za-z0-9]{1,3}))?(?![\w])")
_BARE_FORMULA_RE = re.compile(r"([A-Z])_([0-9]{1,2})(?![a-z])")   # H_2O, C_6H_12, H_12
_BARE_CARET_RE = re.compile(
    r"(?<![\w])([A-Za-z0-9])\^(\{[^}\n]{1,12}\}|[A-Za-z0-9]{1,3})(?![\w])")
# plain-text conventions the model already uses: R^(n×d) and sqrt(d_k)
_PAREN_SUPER_RE = re.compile(r"(?<![\w])([A-Za-z0-9])\^\(([^()\n]{1,28})\)")
_PLAIN_SQRT_RE = re.compile(r"(?<![\w])sqrt\s*\(([^()\n]{1,40})\)")


def _wrap_script(tag: str, match: re.Match) -> str:
    """The base stays outside the tag: `W_Q` is W with a small Q, not a small WQ."""
    return f"{match.group(1)}<{tag}>{match.group(2).strip('{}')}</{tag}>"


def _script_inner(text: str) -> str:
    """Render the inside of a {...} group before it is wrapped (no straddling tags)."""
    text = _BARE_CARET_RE.sub(_wrap_caret, text)
    return _BARE_SCRIPT_RE.sub(_wrap_sub_sup, text)


def _wrap_caret(match: re.Match) -> str:
    raw = match.group(2)
    if raw.startswith("{"):                      # `R^{n × d_v}`: nest the inner scripts first
        return f"{match.group(1)}<sup>{_script_inner(raw.strip('{}'))}</sup>"
    return f"{match.group(1)}<sup>{raw}</sup>"


def _wrap_sub_sup(match: re.Match) -> str:
    """`W_Q^T` -> W<sub>Q</sub><sup>T</sup> in one pass.

    Two passes could not do it: after the subscript pass the character before `^` is `>`,
    so the caret rule never fired and prose kept a literal "^T".
    """
    out = _wrap_script("sub", match)
    if match.group(3):
        out += f"<sup>{match.group(3).strip('{}')}</sup>"
    return out


def _bare_scripts(text: str) -> str:
    """`W_Q` -> W<sub>Q</sub>, `K^T` -> K<sup>T</sup>, `H_2O` -> H<sub>2</sub>O."""
    text = _PAREN_SUPER_RE.sub(lambda m: f"{m.group(1)}<sup>{m.group(2)}</sup>", text)
    text = _PLAIN_SQRT_RE.sub(lambda m: f"√({m.group(1)})", text)
    text = _BARE_FORMULA_RE.sub(lambda m: _wrap_script("sub", m), text)
    # braced sub/sup first and with balanced braces, so nesting works; what is left is the
    # bare `W_Q` / `K^T` / `d_model` shapes the rules below understand
    text = _scripts(text)
    text = _BARE_CARET_RE.sub(_wrap_caret, text)
    return _BARE_SCRIPT_RE.sub(_wrap_sub_sup, text)


def _brace_end(text: str, start: int) -> int:
    """Index of the `}` that closes the `{` at *start*, counting nesting; -1 if unbalanced."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


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
                # balanced braces: `R^{d_{ff} x d}` ends at its own closing brace, not at the
                # one belonging to the inner subscript
                close = _brace_end(text, index + 1)
                inner = text[index + 2:close] if close != -1 else ""
                if close != -1 and 0 < len(inner) <= 40:
                    out.append(f"<{tag}>{_scripts(inner, aggressive=aggressive)}</{tag}>")
                    index = close + 1
                    continue
            elif aggressive and text[index + 1].isalnum():
                # `d_model` is d with a subscript "model", not d_m + "odel": take the whole
                # letter run, but a digit run stays one digit so H_2O keeps its O
                j = index + 1
                if text[j].isalpha():
                    while j < length and text[j].isalpha():
                        j += 1
                else:
                    j = index + 2
                out.append(f"<{tag}>{text[index + 1:j]}</{tag}>")
                index = j
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _inline(text: str) -> str:
    """Escape everything, then apply the tiny markup subset. Never emits raw HTML."""
    staging: list = []

    def _stash(markup: str) -> str:
        staging.append(markup)
        return "\x00hold{}\x00".format(len(staging) - 1)

    raw = html.escape(_TAG_RE.sub("", text or ""), quote=False)
    # a star between digits is multiplication in a sentence ("1.095*0.954"); bold and italic
    # only ever pair stars around words, so this cannot eat markup
    raw = re.sub(r"(?<=\d)\s*\*\s*(?=\d)", " · ", raw)
    # code spans are literal: hold them aside so no other rule rewrites their bytes
    raw = _CODE_RE.sub(lambda m: _stash(f"<code>{m.group(1)}</code>"), raw)
    out = _CITE_RE.sub(_cite, raw)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITAL_RE.sub(r"<em>\1</em>", out)
    # links next, and the anchor is held like a code span: the prose math rules used to run
    # inside the href and turn `https://example.com/a_b` into `/a<sub>b</sub>`
    def _link(match: re.Match) -> str:
        label = _scripts(_bare_scripts(_detex(match.group(1))), aggressive=False)
        return _stash(f'<a href="{html.escape(match.group(2), quote=True)}" '
                      f'rel="noopener noreferrer" target="_blank">{label}</a>')

    out = _LINK_RE.sub(_link, out)
    # math that turns up outside a `math` block (figure labels, a prose sentence that
    # mentions W_Q) gets the same sub/sup treatment, plus LaTeX glyphs if it slipped in
    out = _bare_scripts(_detex(out))
    out = _scripts(out, aggressive=False)
    for index, markup in enumerate(staging):
        out = out.replace("\x00hold{}\x00".format(index), markup)
    return out


_MATH_FUNCS = re.compile(r"\b(log|ln|exp|max|min|sup|inf|lim|Pr|gcd|mod|sin|cos|tan|softmax)"
                         r"(?![A-Za-z0-9])")

# The model writes worked arithmetic, not LaTeX: `1.0*0.9`, `cat . dog`, `->`. Printed as-is
# in an italic run it reads like a sentence with asterisks in it, so the operators are set.
_MATH_OPERATORS = (
    (re.compile(r"(?<=[\d)\]])\s*-\s*(?=\d)"), " − "),     # 0.90 - 0.20, not a hyphen
    (re.compile(r"(?:(?<=[\s(\[{])|^)-(?=\s*\d)"), "−"),     # -0.4, - 0.20, 10^{-2}
    (re.compile(r"(?<=\d)\s*-\s*(?=\d)"), " − "),          # 10000-2k, 1.0-0.4
    (re.compile(r"\s*(?:\*|×)\s*"), " · "),
    (re.compile(r"(?<=\d)\s*\*\s*(?=\d)"), " · "),        # 1.095*0.954 inside a sentence
    (re.compile(r"\s+[.·]\s+"), " · "),          # `cat . dog`, but not the 1.0 decimal point
    (re.compile(r"\s*->\s*|\s*→\s*"), " → "),
    (re.compile(r"\s*<-\s*|\s*←\s*"), " ← "),
    (re.compile(r"\s*>=\s*"), " ≥ "),
    (re.compile(r"\s*<=\s*"), " ≤ "),
    (re.compile(r"\s*!=\s*"), " ≠ "),
)


def _math_operators(text: str) -> str:
    for pattern, replacement in _MATH_OPERATORS:
        text = pattern.sub(replacement, text)
    return text


def _italic_variables(html_text: str) -> str:
    """A lone letter is a variable and goes italic; a word (`cat`, `cos`) stays roman.

    Tags survive untouched, so `W<sub>Q</sub>` italicises the W and the Q and nothing else.
    """
    parts = re.split(r"(<[^>]+>)", html_text)
    for index, part in enumerate(parts):
        if part.startswith("<"):
            continue
        parts[index] = re.sub(r"(?<![A-Za-z0-9])([A-Za-z])(?![A-Za-z0-9])", r"<i>\1</i>", part)
    return "".join(parts)


# A model that slips into LaTeX used to print `\frac{a}{b}` and `$\alpha$` literally on the
# page. Converting the common commands is cheaper than a math typesetter and much better
# than a broken-looking equation.
_LATEX_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "leftrightarrow": "↔",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝",
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏", "int": "∫",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "emptyset": "∅", "ldots": "…", "dots": "…", "cdots": "⋯",
    "prime": "′", "degree": "°",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι",
    "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "quad": " ", "qquad": "  ", "text": "", "mathrm": "", "operatorname": "",
    "left": "", "right": "",
}
_LATEX_WRAP_RE = re.compile(r"\\(?:mathrm|text|operatorname|mathit|mathbf|textbf)\s*\{([^{}]*)\}")
_LEFT_RIGHT_RE = re.compile(r"\\(?:left|right)\s*([()\[\]|])")
_LATEX_CMD_RE = re.compile(r"\\\s*([A-Za-z]+)")
_LATEX_DELIM_RE = re.compile(r"\\[()\[\]]")
_DOLLAR_RE = re.compile(r"\${1,2}([^$]{1,300})\${1,2}")


def _read_group(text: str, index: int) -> tuple:
    """`text[index:]` starts with `{`: return (inner, index_after) or (None, index)."""
    if index >= len(text) or text[index] != "{":
        return None, index
    depth, start = 0, index
    while index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    return None, start


def _expand_latex(text: str, display: bool, depth: int = 0) -> str:
    """\\frac / \\sqrt with balanced-brace arguments, recursively.

    A regex cannot do this: `\\frac{\\exp(e_{ij})}{...}` has braces inside its own argument,
    and matching stopped at the first `}`, which printed `frac{...}` on the page.
    """
    if depth > 4:
        return text
    out: list = []
    index = 0
    while index < len(text):
        if text.startswith("\\frac", index):
            after = index + 5
            while after < len(text) and text[after].isspace():
                after += 1
            numerator, next_index = _read_group(text, after)
            if numerator is not None:
                while next_index < len(text) and text[next_index].isspace():
                    next_index += 1
                denominator, end_index = _read_group(text, next_index)
                if denominator is not None:
                    num = _expand_latex(numerator, display, depth + 1)
                    den = _expand_latex(denominator, display, depth + 1)
                    out.append(f'<span class="frac"><span class="num">{num}</span>'
                               f'<span class="den">{den}</span></span>' if display
                               else f"{num}/{den}")
                    index = end_index
                    continue
        if text.startswith("\\sqrt", index):
            after = index + 5
            while after < len(text) and text[after].isspace():
                after += 1
            radicand, next_index = _read_group(text, after)
            if radicand is not None:
                inner = _expand_latex(radicand, display, depth + 1)
                out.append(f'<span class="sqrt">{inner}</span>' if display else f"√({inner})")
                index = next_index
                continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _detex(text: str, *, display: bool = False) -> str:
    """LaTeX that slipped through -> glyphs. `display` builds stacked fractions."""
    tall = "\\frac" in text
    text = _expand_latex(text, display)
    # a delimiter that encloses a stacked fraction has to grow with it
    text = _LEFT_RIGHT_RE.sub(
        lambda m: f'<span class="dlg">{m.group(1)}</span>' if tall and display else m.group(1), text)
    text = _LATEX_WRAP_RE.sub(lambda m: m.group(1), text)
    text = _DOLLAR_RE.sub(lambda m: m.group(1), text)
    text = _LATEX_DELIM_RE.sub("", text)
    return _LATEX_CMD_RE.sub(lambda m: _LATEX_SYMBOLS.get(m.group(1), m.group(1)), text)


def _math(text: str) -> str:
    """Plain-text math -> an upright run with function names, sub/sup and italic variables."""
    raw = _math_operators(_TAG_RE.sub("", text or ""))
    out = _detex(html.escape(raw, quote=False), display=True)
    # hold function names aside so the variable pass cannot italicise `cos` or `softmax`
    held: list = []

    def _keep(match: re.Match) -> str:
        held.append(match.group(1))
        return f"\x01fn{len(held) - 1}\x01"

    out = _MATH_FUNCS.sub(_keep, out)
    out = _bare_scripts(out)
    out = _scripts(out, aggressive=True)
    out = _italic_variables(out)
    for index, name in enumerate(held):
        out = out.replace(f"\x01fn{index}\x01", f'<span class="fn">{name}</span>')
    return f'<span class="m">{out}</span>'

def _split_stages(text: str) -> list:
    """Split `a = b = c` into stages, but only on `=` outside brackets and never on prose.

    `(|A| = 256 for byte-level)` is a parenthetical, not a derivation; `Round 1 over {...}, c
    = 1 each:` is a sentence. Both stay on one row.
    """
    stages, current, depth = [], [], 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if depth == 0 and text.startswith(" = ", index):
            stages.append("".join(current).strip())
            current = []
            index += 3
            continue
        current.append(char)
        index += 1
    stages.append("".join(current).strip())
    stages = [stage for stage in stages if stage]
    if len(stages) < 2:
        return stages
    if len(text.split()) > 18:        # a paragraph that contains an equals sign is prose
        return [" = ".join(stages)]
    head = stages[0]
    plain = head.replace(" . ", " ").replace(" · ", " ")   # a dot product is not a sentence end
    if len(head) > 32 or "." in plain or len(plain.split()) > 5:
        return [" = ".join(stages)]        # a sentence that happens to contain an equals sign
    return stages


def _top_level_commas(text: str) -> list:
    """Split on commas outside brackets; `(x, y) = (1, 2)` stays one piece."""
    pieces, current, depth = [], [], 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if depth == 0 and char == ",":
            pieces.append("".join(current))
            current = []
            continue
        current.append(char)
    pieces.append("".join(current))
    return [piece.strip() for piece in pieces if piece.strip()]


def _math_line(text: str) -> str:
    """One source line -> one or more aligned rows.

    `cat . dog = 1.0·0.9 + … = 0.90 + 0.02 + 0.12 = 1.04` is a chain of stages; printed as one
    row it wraps mid-expression and the working is lost. Each stage gets its own row, the
    `=` signs share a column, and a reader can follow the arithmetic down the page.

    A line that defines several things — `q = h W_Q, k = h W_K, v = h W_V` — is not one chain:
    read as a chain it becomes the rows `q | = h W_Q, k` and `| = h W_K, v`, which says the
    opposite of what the model wrote. Split the definitions first, then each one into stages.
    """
    text = text.strip()
    pieces = _top_level_commas(text)
    groups = pieces if sum(1 for piece in pieces if len(_split_stages(piece)) > 1) > 1 else [text]
    out = []
    for group in groups:
        stages = _split_stages(group)
        if not stages:
            continue
        if len(stages) == 1:                   # a bare expression: one row, no sign
            out.append(f'<span class="m-lhs"></span><span class="m-eq"></span>'
                       f'<span class="m-rhs">{_math(stages[0])}</span>')
            continue
        rows = [(stages[0], stages[1])]
        rows.extend(("", stage) for stage in stages[2:4])   # at most four stages per source line
        if len(stages) > 4:                                 # keep the tail rather than drop it
            rows[-1] = ("", " = ".join(stages[3:]))
        out.append("".join(
            f'<span class="m-lhs">{_math(lhs)}</span>'
            f'<span class="m-eq">=</span>'
            f'<span class="m-rhs">{_math(rhs)}</span>'
            for lhs, rhs in rows))
    return "".join(out)


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


def _level_words(section: Dict[str, Any], level: str) -> int:
    """Prose words in one depth of a section, for the reading-time estimate."""
    blocks = ((section.get("levels") or {}).get(level) or {}).get("blocks") or []
    total = 0
    for block in blocks:
        text = (block.get("md") or " ".join(str(i) for i in (block.get("items") or []))
                or block.get("code") or "")
        total += len(re.sub(r"<[^>]+>", " ", str(text)).split())
    return total


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------
def _spoken_label(text: str) -> str:
    """How a figure step is read aloud: `W_Q` is "W sub Q", not "W underscore Q"."""
    out = re.sub(r"(?<![\w])sqrt\s*\(", "√(", str(text or ""))
    # the group excludes `_`/`^` so `W_Q^T` is read as two scripts, not "sub Q^T"
    out = re.sub(r"[_^]\{?([^}\s_^]{1,12})\}?",
                 lambda m: (" sup " if m.group(0)[0] == "^" else " sub ") + m.group(1), out)
    return re.sub(r"\s+", " ", out).strip()


def _block(block: Dict[str, Any]) -> str:
    kind = block["kind"]
    if kind in {"prose", "checkpoint"}:
        body = _paras(block.get("md", ""))
        if kind == "checkpoint":
            return f'<div class="checkpoint"><span class="cp-label">Before you move on</span>{body}</div>'
        return body
    if kind == "math":
        # a derivation is written one step per line; collapsing the newlines ran "Scores:",
        # "Weights:" and "Output:" into one wrapped paragraph
        lines = [line.strip() for line in str(block.get("md", "")).split("\n") if line.strip()]
        if not lines:
            return ""
        parts = []
        for line in lines:
            if len(line.split()) > 18:           # commentary, not a formula
                parts.append(f'<div class="math-note">{_inline(line)}</div>')
            else:
                parts.append(f'<div class="mathline">{_math_line(line)}</div>')
        return '<div class="mathblock">' + "".join(parts) + "</div>"
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
        fig, fig_w = _figure(spec)
        caption = _inline(block.get("caption", ""))
        body = fig + (f"<figcaption>{caption}</figcaption>" if caption else "")
        return _framed_figure(body, fig_w, "fig")
    return ""


# --------------------------------------------------------------------------
# figures drawn as inline SVG: a layered diagram and a sequence diagram
# --------------------------------------------------------------------------
# A figure has to belong to the page, not sit on it. These are SVG, not images: they inherit
# --ink, --surface and --line, so they follow the reader's light/dark setting, and they
# survive print, where the canvas plots do not (the print stylesheet hides canvas).
#
# Layout is computed here, never asked of the model: placement is the part models get wrong,
# and a deterministic layout means the same spec always draws the same picture.
_FIG_TEXT = 12.5            # node label size, px
_FIG_EDGE_TEXT = 10.5       # edge label size, px
_FIG_BOX_H = 38
_FIG_ROW_GAP = 22
_FIG_LABEL_CHARS = 24        # a label wraps at this many characters
_FIG_LINE_H = 15.0           # ...and each extra line adds this much box height           # between siblings inside one layer
_FIG_LAYER_GAP = 62         # between layers, before an edge label widens it


def _svg_text(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _wrap_label(label: str, limit: int = _FIG_LABEL_CHARS) -> List[str]:
    """Break a label into lines a box can hold, on spaces where there are any."""
    words = str(label).split()
    if not words:
        return [""]
    lines: List[str] = []
    line = ""
    for word in words:
        while len(word) > limit:              # one unbroken token: cut it, do not overflow
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:limit])
            word = word[limit:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= limit:
            line += " " + word
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _node_size(label: str) -> tuple:
    """A box that holds its own label: wrapped, so no clamp can leave text outside it.

    0.62em per glyph is the advance of the monospace face the labels are set in — 7.9px per
    character at 12.5px — and the wrap limit (24 characters) keeps the widest box near 207px,
    which is inside every column this renderer draws. Height, not width, absorbs long labels.
    """
    lines = _wrap_label(label)
    widest = max(len(line) for line in lines) or 1
    width = max(72.0, 7.9 * widest + 18.0)
    height = _FIG_BOX_H + (len(lines) - 1) * _FIG_LINE_H
    return width, height


def _layered(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Dict[str, int]:
    """Assign a layer to every node, breaking cycles at the edge that closes them.

    A cycle is legal in a state machine, so this cannot refuse one: depth-first, mark the
    edge that returns into the current stack as a back edge, then relax layers over what is
    left. Back edges are drawn underneath the figure instead of crossing it.
    """
    ids = [n["id"] for n in nodes]
    out: Dict[str, List[str]] = {i: [] for i in ids}
    back = set()
    for edge in edges:
        out[edge["from"]].append(edge["to"])
    state: Dict[str, int] = {}
    order: List[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        for nxt in out[node]:
            if state.get(nxt) == 1:
                back.add((node, nxt))
                continue
            if nxt not in state:
                visit(nxt)
        state[node] = 2
        order.append(node)

    for node in ids:
        if node not in state:
            visit(node)

    layer = {node: 0 for node in ids}
    for node in reversed(order):                     # a topological order of the acyclic part
        for nxt in out[node]:
            if (node, nxt) in back:
                continue
            layer[nxt] = max(layer[nxt], layer[node] + 1)
    return layer


def _arrow(x: float, y: float, dx: int, dy: int) -> str:
    """A filled head drawn as a polygon: no <defs> id to collide with the next figure."""
    back_x, back_y = x - 7 * dx, y - 7 * dy
    return (f'<polygon class="ah" points="{x},{y} {back_x + 4 * dy},{back_y + 4 * dx} '
            f'{back_x - 4 * dy},{back_y - 4 * dx}"/>')


def _edge_label(x: float, y: float, text: str, size: float = 0.0) -> str:
    if not text:
        return ""
    # the mask behind the label has to be as wide as the text it covers, at whatever size
    # that text is drawn
    width = 0.62 * (size or _FIG_EDGE_TEXT) * len(text) + 10
    style = f' style="font-size:{size:.1f}px"' if size else ""
    return (f'<rect class="elabel-bg" x="{x - width / 2:.1f}" y="{y - 12:.1f}" '
            f'width="{width:.1f}" height="14" rx="3"/>'
            f'<text class="elabel" x="{x:.1f}" y="{y - 1:.1f}" text-anchor="middle"{style}>'
            f'{_svg_text(text)}</text>')


def _diagram_geometry(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]],
                      direction: str = "") -> Dict[str, Any]:
    """Place the boxes and route the edges. The SVG and the .excalidraw file share this.

    `right` reads as a pipeline. `down` is the default once a graph has more than three
    layers: a five-column graph is wider than the reading measure, the browser shrinks it to
    fit, and 12.5px type then arrives at about 7px. Rotating the flow is the only fix that
    keeps type at the size it was measured for, and vertical space is free on a page.
    """
    layer = _layered(nodes, edges)
    sizes = {node["id"]: _node_size(node["label"]) for node in nodes}
    widths = {nid: dims[0] for nid, dims in sizes.items()}
    heights = {nid: dims[1] for nid, dims in sizes.items()}
    groups: Dict[int, List[str]] = {}
    for node in nodes:                       # input order inside a layer: the model's own
        groups.setdefault(layer[node["id"]], []).append(node["id"])
    depths = sorted(groups)
    label_w = max([0.62 * _FIG_EDGE_TEXT * len(str(edge.get("label") or "")) for edge in edges]
                  or [0.0])
    # the same label on parallel edges is one operation, printed once: a fan-out to three
    # projections reads "multiply" three times otherwise, and crowds its own diagram
    unique_labels = {(layer[edge["from"]], str(edge.get("label") or "")) for edge in edges
                     if edge.get("from") in layer and str(edge.get("label") or "")}
    bands: Dict[int, int] = {}
    for band_key, _ in unique_labels:
        bands[band_key] = bands.get(band_key, 0) + 1
    stack = max(bands.values() or [1])
    gap_y = 46.0 + 16.0 * (stack - 1)    # room for one label row per labelled edge in a band
    if direction not in ("right", "down"):
        across = sum(max(widths[i] for i in groups[L]) for L in depths) \
            + (len(depths) - 1) * max(_FIG_LAYER_GAP, label_w + 18)
        direction = "right" if len(depths) <= 3 and across <= 560 else "down"

    pad = 14.0
    boxes: Dict[str, tuple] = {}
    if direction == "right":
        gap = max(_FIG_LAYER_GAP, label_w + 18)
        columns: Dict[int, tuple] = {}
        x = pad
        for L in depths:
            columns[L] = (x, max(widths[i] for i in groups[L]))
            x += columns[L][1] + gap
        span = max(sum(heights[i] for i in groups[L]) + (len(groups[L]) - 1) * _FIG_ROW_GAP
                   for L in depths)
        for L in depths:
            height = sum(heights[i] for i in groups[L]) + (len(groups[L]) - 1) * _FIG_ROW_GAP
            top = pad + (span - height) / 2
            for nid in groups[L]:
                boxes[nid] = (columns[L][0], top, columns[L][1], heights[nid])
                top += heights[nid] + _FIG_ROW_GAP
    else:
        gap = gap_y
        rows: Dict[int, float] = {}
        row_h: Dict[int, float] = {}
        y = pad
        for L in depths:
            rows[L] = y
            row_h[L] = max(heights[i] for i in groups[L])
            y += row_h[L] + gap
        widest = max(sum(widths[i] for i in groups[L]) + (len(groups[L]) - 1) * _FIG_ROW_GAP
                     for L in depths)
        for L in depths:
            span = sum(widths[i] for i in groups[L]) + (len(groups[L]) - 1) * _FIG_ROW_GAP
            x = pad + (widest - span) / 2
            for nid in groups[L]:
                boxes[nid] = (x, rows[L] + (row_h[L] - heights[nid]) / 2,
                              widths[nid], heights[nid])
                x += widths[nid] + _FIG_ROW_GAP

    # Labels stack inside a band: three edges leaving one node all want the same midpoint,
    # and a fan-out with the same label on two of them printed one over the other.
    seen_in_band: Dict[int, int] = {}
    drawn_labels: set = set()

    routed: List[Dict[str, Any]] = []
    channel = 0
    for index, edge in enumerate(edges):
        src, dst = edge["from"], edge["to"]
        if src not in boxes or dst not in boxes:
            continue
        sx, sy, sw, sh = boxes[src]
        tx, ty, tw, th = boxes[dst]
        text = str(edge.get("label") or "")
        band = layer[src]
        if text and (band, text) in drawn_labels:
            text = ""                                  # already named on a parallel edge
        elif text:
            drawn_labels.add((band, text))
        text_w = 0.62 * _FIG_EDGE_TEXT * len(text) + 10 if text else 0.0
        ordinal = seen_in_band.get(band, 0)
        if text:
            seen_in_band[band] = ordinal + 1
        slot = ordinal if text else 0
        # outside-the-figure channels carry their own x, but two channels six pixels apart
        # still print on top of each other: stagger the label along the run as well
        spread = (slot - (bands.get(band, 1) - 1) / 2) * 16
        jump = abs(layer[dst] - layer[src])

        if src == dst:                                    # a state that stays put
            y0 = sy + sh * 0.3
            routed.append({"points": [(sx + sw, y0), (sx + sw + 22, y0),
                                      (sx + sw + 22, y0 + 18), (sx + sw, y0 + 18)],
                           "head": (sx + sw, y0 + 18, -1, 0),
                           "label": (sx + sw + text_w / 2 + 6, y0 + 9), "text": text})
            continue

        forward = layer[dst] > layer[src]
        if direction == "right" and forward and jump == 1:
            y1 = sy + sh / 2
            y2 = ty + th / 2
            # when the two boxes sit on one line the label lands ON the run between them, so
            # its mask has to stay inside the gap
            cx = sx + sw + max(_FIG_LAYER_GAP, label_w + 18) / 2 + ((index % 3) - 1) * 6
            cx = max(sx + sw + text_w / 2 + 3, min(cx, tx - text_w / 2 - 3))
            routed.append({"points": [(sx + sw, y1), (cx, y1), (cx, y2), (tx - 7, y2)],
                           "head": (tx, y2, 1, 0),
                           "label": (cx, (y1 + y2) / 2), "text": text})
            continue
        if direction == "down" and forward and jump == 1:
            x1 = sx + sw / 2
            x2 = tx + tw / 2
            my = sy + sh + gap_y / 2
            # one label row per edge, counted down from the row it left: 12px of mask above
            # the baseline is enough to reach back into that row
            routed.append({"points": [(x1, sy + sh), (x1, my), (x2, my), (x2, ty - 7)],
                           "head": (x2, ty, 0, 1),
                           "label": ((x1 + x2) / 2, sy + sh + 14 + slot * 16), "text": text})
            continue

        # everything else runs outside the figure in its own channel: a skip edge drawn
        # straight would go through every row it skips over
        channel += 1
        if direction == "right":
            bx = sx + sw / 2
            yb = max(y + h for _, y, _, h in boxes.values()) + 18 + channel * 12
            tx_mid = tx + tw / 2
            routed.append({"points": [(bx, sy + sh), (bx, yb), (tx_mid, yb),
                                      (tx_mid, ty + th + 7)],
                           "head": (tx_mid, ty + th, 0, -1),
                           "label": ((bx + tx_mid) / 2, yb + 2 + spread), "text": text})
        else:
            xr = max(x + w for x, _, w, _ in boxes.values()) + 20 + channel * 12
            y1 = sy + sh / 2
            y2 = ty + th / 2
            routed.append({"points": [(sx + sw, y1), (xr, y1), (xr, y2), (tx + tw + 7, y2)],
                           "head": (tx + tw, y2, 1, 0),
                           "label": (xr + text_w / 2 + 6, (y1 + y2) / 2 + spread), "text": text})

    label_w_max = {item["text"]: 0.62 * _FIG_EDGE_TEXT * len(item["text"]) + 10
                   for item in routed if item["text"]}
    right = max([x + w for x, _, w, _ in boxes.values()]
                + [point[0] for item in routed for point in item["points"]]
                + [item["label"][0] + label_w_max[item["text"]] / 2
                   for item in routed if item["text"]] + [0.0])
    bottom = max([y + h for _, y, _, h in boxes.values()]
                 + [point[1] for item in routed for point in item["points"]]
                 + [item["label"][1] + 2 for item in routed if item["text"]] + [0.0])
    return {"direction": direction, "boxes": boxes, "edges": routed, "layer": layer,
            "width": right + pad, "height": bottom + pad}


def _diagram_svg(spec: Dict[str, Any]) -> str:
    nodes = [n for n in (spec.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    edges = [e for e in (spec.get("edges") or []) if isinstance(e, dict)]
    if len(nodes) < 2:
        return ""
    geo = _diagram_geometry(nodes, edges)
    parts: List[str] = []
    for node in nodes:
        box = geo["boxes"].get(node["id"])
        if not box:
            continue
        x, y, w, h = box
        cls = "nd on" if str(node.get("group") or "").strip() else "nd"
        lines = _wrap_label(node["label"])
        top = y + h / 2 + 4.4 - (len(lines) - 1) * _FIG_LINE_H / 2
        body = "".join(
            f'<tspan x="{x + w / 2:.1f}" y="{top + i * _FIG_LINE_H:.1f}">{_svg_text(line)}</tspan>'
            for i, line in enumerate(lines))
        parts.append(f'<g><title>{_svg_text(node["label"])}</title>'
                     f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                     f'height="{h:.1f}"/>'
                     f'<text text-anchor="middle">{body}</text></g>')
    for edge in geo["edges"]:
        path = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in edge["points"])
        parts.append(f'<path class="ed" d="{path}"/>')
        parts.append(_arrow(*edge["head"]))
        parts.append(_edge_label(edge["label"][0], edge["label"][1], edge["text"]))
    spoken = "; ".join(
        f'{_spoken_label(next((n["label"] for n in nodes if n["id"] == e["from"]), ""))} to '
        f'{_spoken_label(next((n["label"] for n in nodes if n["id"] == e["to"]), ""))}'
        for e in edges[:6]) or ", ".join(_spoken_label(n["label"]) for n in nodes)
    # allow a narrow figure to breathe up to 1.15x its natural width, and let a wide one shrink
    width = geo["width"] * 1.15
    return (f'<svg class="fig-svg" style="max-width:{width:.0f}px" '
            f'viewBox="0 0 {geo["width"]:.0f} {geo["height"]:.0f}" '
            f'role="img" aria-label="{html.escape("Diagram: " + spoken, quote=True)}">'
            f'{"".join(parts)}</svg>'), width


def _sequence_grid(actors: List[str]) -> Dict[str, Any]:
    """Columns for a sequence. The actor box is as wide as its longest name, not a fixed 120px:
    a fixed box printed "RNA polymerase II" through the wall of its own rectangle."""
    head_w = max(120.0, 7.9 * max(len(actor) for actor in actors) + 24.0)
    col = head_w + 30.0
    pad = 16.0
    head_h = 32.0
    x_of = {actor: pad + i * col for i, actor in enumerate(actors)}
    width = pad * 2 + (len(actors) - 1) * col + head_w
    return {"head_w": head_w, "col": col, "pad": pad, "head_h": head_h,
            "x_of": x_of, "width": width}


def _sequence_svg(spec: Dict[str, Any]) -> str:
    actors = [str(a) for a in (spec.get("actors") or []) if str(a).strip()]
    messages = [m for m in (spec.get("messages") or []) if isinstance(m, dict)]
    if len(actors) < 2 or not messages:
        return ""
    grid = _sequence_grid(actors)
    pad, head_h, x_of, width = grid["pad"], grid["head_h"], grid["x_of"], grid["width"]
    head_w, mid = grid["head_w"], grid["head_w"] / 2
    top = pad
    first_msg = top + head_h + 34
    step = 42.0
    bottom = first_msg + (len(messages) - 1) * step + 26

    parts = []
    for actor in actors:
        x = x_of[actor]
        parts.append(f'<rect class="nd" x="{x:.1f}" y="{top:.1f}" width="{head_w:.1f}" '
                     f'height="{head_h:.1f}"/>'
                     f'<text x="{x + mid:.1f}" y="{top + head_h / 2 + 4.4:.1f}" text-anchor="middle">'
                     f'{_svg_text(actor)}</text>')
        parts.append(f'<line class="ll" x1="{x + mid:.1f}" y1="{top + head_h:.1f}" '
                     f'x2="{x + mid:.1f}" y2="{bottom:.1f}"/>')

    spoken = []
    for i, message in enumerate(messages):
        src, dst = str(message.get("from") or ""), str(message.get("to") or "")
        if src not in x_of or dst not in x_of:
            continue
        y = first_msg + i * step
        text = str(message.get("label") or "")
        lane = "ed reply" if message.get("reply") else "ed"
        if src == dst:                                  # a call that returns to itself
            x = x_of[src] + mid
            parts.append(f'<path class="{lane}" d="M {x:.1f},{y - 8:.1f} h 34 v 20 h -34"/>')
            parts.append(_arrow(x + 7, y + 12, -1, 0))
            parts.append(_edge_label(x + 44, y - 6, text) if text else "")
        else:
            x1 = x_of[src] + mid
            x2 = x_of[dst] + mid
            direction = 1 if x2 > x1 else -1
            parts.append(f'<path class="{lane}" d="M {x1:.1f},{y:.1f} H {x2 - 8 * direction:.1f}"/>')
            parts.append(_arrow(x2, y, direction, 0))
            parts.append(_edge_label((x1 + x2) / 2, y - 7, text) if text else "")
        spoken.append(f'{_spoken_label(src)} to {_spoken_label(dst)}: {_spoken_label(text)}')

    framed = width * 1.15
    return (f'<svg class="fig-svg" style="max-width:{framed:.0f}px" '
            f'viewBox="0 0 {width:.0f} {bottom + pad:.0f}" '
            f'role="img" aria-label="{html.escape("Sequence: " + "; ".join(spoken[:8]), quote=True)}">'
            f'{"".join(parts)}</svg>'), framed


def scene_from_figure(spec: Dict[str, Any], caption: str = "") -> Dict[str, Any]:
    """The same figure as an editable .excalidraw scene, so a reader can open it and move things.

    Written beside the course, never used by the page: the page draws SVG, this is the
    hand-off to excalidraw.com. Element ids, seeds and versions are fixed values, so
    re-exporting an unchanged course produces a byte-identical file.
    """
    kind = str(spec.get("type") or "").strip().lower()
    elements: List[Dict[str, Any]] = []
    font = 3                                    # Cascadia: closest to the page's mono labels
    ink = "#1e1e1e"

    def text_el(eid: str, body: str, x: float, y: float, w: float, h: float, size: int,
                container: str = "") -> Dict[str, Any]:
        item = {"type": "text", "id": eid, "x": x, "y": y, "width": w, "height": h,
                "angle": 0, "strokeColor": ink, "backgroundColor": "transparent",
                "fillStyle": "solid", "strokeWidth": 1, "roughness": 1, "opacity": 100,
                "groupIds": [], "frameId": None, "roundness": None,
                "seed": len(elements) * 7 + 11, "version": 1, "versionNonce": 1,
                "isDeleted": False, "boundElements": [],
                "text": body, "originalText": body, "fontSize": size, "fontFamily": font,
                "textAlign": "center", "verticalAlign": "middle", "lineHeight": 1.25,
                "containerId": container or None, "autoResize": True}
        return item

    if kind == "diagram":
        nodes = [n for n in (spec.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
        edges = [e for e in (spec.get("edges") or []) if isinstance(e, dict)]
        if len(nodes) < 2:
            return {}
        geo = _diagram_geometry(nodes, edges)
        for node in nodes:
            box = geo["boxes"].get(node["id"])
            if not box:
                continue
            x, y, w, h = box
            rid, tid = f"fig-{node['id']}", f"fig-{node['id']}-label"
            elements.append({
                "type": "rectangle", "id": rid, "x": x, "y": y, "width": w, "height": h,
                "angle": 0, "strokeColor": "#1e1e1e",
                "backgroundColor": "#e7f5ff" if node.get("group") else "#f8f9fa",
                "fillStyle": "solid", "strokeWidth": 2, "roughness": 1, "opacity": 100,
                "groupIds": [], "frameId": None, "roundness": {"type": 3},
                "seed": len(elements) * 7 + 3, "version": 1, "versionNonce": 1,
                "isDeleted": False, "boundElements": [{"type": "text", "id": tid}]})
            lines = _wrap_label(node["label"])
            elements.append(text_el(tid, "\n".join(lines), x + 2, y + 2,
                                    max(10.0, w - 4), max(18.0, h - 4), 13, container=rid))
        for j, edge in enumerate(geo["edges"]):
            origin_x, origin_y = edge["points"][0]
            points = [[round(px - origin_x, 2), round(py - origin_y, 2)]
                      for px, py in edge["points"]]
            elements.append({
                "type": "arrow", "id": f"fig-edge-{j}", "x": origin_x, "y": origin_y,
                "width": abs(points[-1][0]), "height": abs(points[-1][1]),
                "angle": 0, "strokeColor": "#495057", "backgroundColor": "transparent",
                "fillStyle": "solid", "strokeWidth": 1.5, "roughness": 1, "opacity": 100,
                "groupIds": [], "frameId": None, "roundness": {"type": 2},
                "seed": len(elements) * 7 + 5, "version": 1, "versionNonce": 1,
                "isDeleted": False, "boundElements": [], "points": points,
                "lastCommittedPoint": None, "startBinding": None, "endBinding": None,
                "startArrowhead": None, "endArrowhead": "arrow", "elbowed": False})
            if edge["text"]:
                lx, ly = edge["label"]
                elements.append(text_el(f"fig-edge-{j}-label", edge["text"], lx - 40, ly - 16,
                                        80, 16, 11))
    elif kind == "sequence":
        actors = [str(a) for a in (spec.get("actors") or []) if str(a).strip()]
        messages = [m for m in (spec.get("messages") or []) if isinstance(m, dict)]
        if len(actors) < 2 or not messages:
            return {}
        grid = _sequence_grid(actors)
        pad, head, x_of = grid["pad"], grid["head_h"], grid["x_of"]
        head_w = grid["head_w"]
        bottom = pad + head + 34 + max(0, len(messages) - 1) * 42 + 26
        for actor in actors:
            x = x_of[actor]
            rid, tid = f"fig-{actor}", f"fig-{actor}-label"
            elements.append({
                "type": "rectangle", "id": rid, "x": x, "y": pad, "width": head_w, "height": head,
                "angle": 0, "strokeColor": ink, "backgroundColor": "#f8f9fa", "fillStyle": "solid",
                "strokeWidth": 2, "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
                "roundness": {"type": 3}, "seed": len(elements) * 7 + 3, "version": 1,
                "versionNonce": 1, "isDeleted": False,
                "boundElements": [{"type": "text", "id": tid}]})
            elements.append(text_el(tid, actor, x + 2, pad + head / 2 - 9, head_w - 4, 18, 13,
                                    container=rid))
            elements.append({
                "type": "line", "id": f"fig-life-{actor}", "x": x + head_w / 2, "y": pad + head,
                "width": 0, "height": bottom - pad - head, "angle": 0, "strokeColor": "#adb5bd",
                "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 1,
                "roughness": 0, "opacity": 100, "groupIds": [], "frameId": None, "roundness": None,
                "seed": len(elements) * 7 + 5, "version": 1, "versionNonce": 1, "isDeleted": False,
                "boundElements": [], "points": [[0, 0], [0, bottom - pad - head]],
                "lastCommittedPoint": None, "startBinding": None, "endBinding": None,
                "startArrowhead": None, "endArrowhead": None})
        for i, message in enumerate(messages):
            y = pad + head + 34 + i * 42
            src, dst = str(message.get("from") or ""), str(message.get("to") or "")
            if src not in x_of or dst not in x_of or src == dst:
                continue
            x1, x2 = x_of[src] + head_w / 2, x_of[dst] + head_w / 2
            elements.append({
                "type": "arrow", "id": f"fig-msg-{i}", "x": x1, "y": y,
                "width": abs(x2 - x1), "height": 0, "angle": 0,
                "strokeColor": "#495057", "backgroundColor": "transparent", "fillStyle": "solid",
                "strokeWidth": 1.5, "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None,
                "roundness": {"type": 2}, "seed": len(elements) * 7 + 5, "version": 1,
                "versionNonce": 1, "isDeleted": False, "boundElements": [],
                "points": [[0, 0], [x2 - x1, 0]], "lastCommittedPoint": None,
                "startBinding": None, "endBinding": None, "startArrowhead": None,
                "endArrowhead": "arrow", "elbowed": False,
                "strokeStyle": "dashed" if message.get("reply") else "solid"})
            if message.get("label"):
                elements.append(text_el(f"fig-msg-{i}-label", str(message["label"]),
                                        (x1 + x2) / 2 - 40, y - 20, 80, 16, 11))
    else:
        return {}
    if caption:
        elements.append(text_el("fig-caption", caption, 14.0, -34.0, 520.0, 20, 13))
    return {"type": "excalidraw", "version": 2, "source": "coursewright",
            "elements": elements, "appState": {"viewBackgroundColor": "#ffffff"}, "files": {}}


def _framed_figure(body: str, width: float, classes: str) -> str:
    """A figure that is exactly as wide as its own drawing, centred in whatever column it lands
    in. Without the frame a diagram sits at the left edge of a full-bleed row, a hundred pixels
    away from the caption printed under it."""
    if width:
        return (f'<figure class="{classes}">'
                f'<div class="fig-frame" style="max-width:{width:.0f}px">{body}</div>'
                f'</figure>')
    return f'<figure class="{classes}">{body}</figure>'


def _figure(spec: Dict[str, Any]) -> tuple:
    """The figure, and the width it wants. `0.0` means it lays itself out."""
    kind = str(spec.get("type") or "").strip().lower()
    if kind == "diagram":
        return _diagram_svg(spec)
    if kind == "sequence":
        return _sequence_svg(spec)
    if kind == "flow":
        raw_steps = spec.get("steps")
        raw_steps = raw_steps if isinstance(raw_steps, list) else []
        steps = [str(s).strip() for s in raw_steps][:7]
        steps = [s for s in steps if s]
        if len(steps) < 2:
            return "", 0.0
        parts = "".join(
            f'<span class="flow-step"><span class="flow-n">{i + 1}</span>'
            f'<span class="flow-t">{_inline(step)}</span></span>'
            for i, step in enumerate(steps))
        cols = len(steps) if len(steps) <= 4 else 3
        spoken = html.escape(" then ".join(_spoken_label(s) for s in steps), quote=True)
        return (f'<div class="flow" style="--cols:{cols}" role="img" '
                f'aria-label="{spoken}">{parts}</div>', 0.0)
    if kind == "trajectory":
        n = spec.get("n") if isinstance(spec.get("n"), int) else 27
        return (f'<canvas class="plot" data-plot="trajectory" data-n="{max(3, min(int(n), 97))}"'
                f' role="img" aria-label="Trajectory of {n}"></canvas>', 0.0)
    if kind in {"bars", "line"}:
        points = []
        raw_points = spec.get("points")
        raw_points = raw_points if isinstance(raw_points, list) else []
        for raw in raw_points[:24]:
            if not isinstance(raw, dict):
                continue
            value = raw.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            points.append({"label": str(raw.get("label") or "")[:18], "value": float(value)})
        if len(points) < 2:
            return "", 0.0
        payload = html.escape(json.dumps(points, ensure_ascii=False), quote=True)
        xlabel = html.escape(str(spec.get("xlabel") or "")[:28], quote=True)
        ylabel = html.escape(str(spec.get("ylabel") or "")[:28], quote=True)
        label = html.escape(f"{kind} chart: {ylabel or 'values'} by {xlabel or 'item'}", quote=True)
        return (f'<canvas class="plot" data-plot="{kind}" data-points="{payload}" '
                f'data-x="{xlabel}" data-y="{ylabel}" role="img" aria-label="{label}"></canvas>', 0.0)
    return "", 0.0


# --------------------------------------------------------------------------
# exercise
# --------------------------------------------------------------------------
def _exercise(ex: Dict[str, Any], index: int, section_id: str = "") -> str:
    """One exercise. `data-ex` is the section id, not its position.

    Answers are persisted per course; an index key tied a saved answer to whatever section
    happened to land in that slot after a rebuild reordered them.
    """
    if not ex:
        return ""
    kind = ex["kind"]
    key = section_id or str(index)
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
    return (f'<section class="ex" {slug_attr} data-ex="{html.escape(key, quote=True)}">'
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
        visual_html, fig_w = _figure(section["visual"])
        if visual_html:
            caption = _inline(str(section["visual"].get("caption") or ""))
            visual = _framed_figure(
                visual_html + (f"<figcaption>{caption}</figcaption>" if caption else ""),
                fig_w, "fig fig-lead")
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
            f"{visual}{levels}{_exercise(section.get('exercise'), index, section['id'])}{ask}"
            f"</section>")


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
    if n == 0:
        # a videos-only course retrieved something, but nothing was read: "0 source(s)" under a
        # "thin sources" heading reads like a bug rather than a warning
        return ('<p class="notice"><span>⚠</span><span><b>No sources.</b> No page could be read for '
                'this course, so it comes from the model\'s own knowledge. The videos below were '
                'checked for relevance, not for accuracy.</span></p>')
    if n < 3:
        return (f'<p class="notice"><span>⚠</span><span><b>Thin sources:</b> only '
                f'{_plural(n, "source")} survived the research pass, so the advanced level is '
                'only partly grounded.</span></p>')
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
    n = len(prov["sources"])
    parts = []
    # A zero is not a provenance line. "0 sources read" under "How this course was made"
    # reads as a machine failure leaked onto the page; say what actually happened instead.
    if n:
        if prov["finders"]:
            parts.append("found with " + ", ".join(_TOOL_NAMES.get(f, f) for f in prov["finders"]))
        if prov["readers"]:
            parts.append("read with " + ", ".join(_TOOL_NAMES.get(r, r) for r in prov["readers"]))
        parts.append(_plural(n, "source") + " read")
    elif prov["finders"]:
        parts.append("searched with " + ", ".join(_TOOL_NAMES.get(f, f) for f in prov["finders"]))
        parts.append("nothing could be read")
    else:
        parts.append("written without a research pass")
        parts.append("no outside sources were read")
    recommended = len(spec["resources"]["reading"])
    if recommended:
        parts.append(f"{recommended} recommended")
    vids = len(spec["resources"]["videos"])
    if vids:
        parts.append(_plural(vids, "video") + ("" if n else " (not checked for accuracy)"))
    if prov["failures"]:
        parts.append(_plural(len(prov["failures"]), "dead end"))
    when = _dated(prov.get("generated") or "")
    if when:
        parts.append(f"made {when}")
    # Commas, not middots: a separator glyph that wraps to the next line reads as a stray
    # mark, and no CSS rule can tell a wrapped item from a first one. A comma never opens a
    # line, so the sentence stays clean at any width.
    line = ", ".join(parts)
    line = line[:1].upper() + line[1:] if line else line
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
        level_attr = f' data-level="{html.escape(v["level"], quote=True)}"' if v["level"] else ""
        videos += (
            f'<button class="vcard" type="button" data-yt="{html.escape(v["id"], quote=True)}"'
            f'{level_attr}>'
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
        read_level = f' data-level="{html.escape(r["level"], quote=True)}"' if r["level"] else ""
        reading += (f'<li{read_level}>{title_html}'
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
  --motion:{{motion}}; --maxw:38rem; --rail:12rem; --gap:2.25rem;
  /* the width of a full band: nav rail + measure + page padding. Every band uses it, so the
     cover, the depth bar, the rail and the article share one left and right edge. */
  --shell:calc(var(--rail) + var(--gap) + var(--maxw) + 3rem);
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:{{bg_d}}; --surface:{{surface_d}}; --ink:{{ink_d}}; --muted:{{muted_d}};
    --accent:{{accent_d}}; --accent2:{{accent2_d}}; --line:{{line_d}}; --ok:{{ok_d}}; --warn:{{warn_d}};
  }
}
*,*::before,*::after{box-sizing:border-box}
/* an author `display` beats the UA rule for [hidden]: without this, a filtered resource card
   keeps its grid display and stays on screen */
[hidden]{display:none!important}
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
/* the hero is display type, not body copy: it uses the width the pane gives it, in two
   columns, so the space beside the title carries the outcomes and the start button instead
   of sitting empty. The rail layout is the wide one; column/bleed stay deliberately narrow. */
.cover>.wrap{max-width:min(var(--shell),100%);
  display:grid;grid-template-columns:minmax(0,1fr);gap:1.8rem}
.cover-main{min-width:0}
/* the hero goes two-column as soon as the band is wide enough to hold both columns with
   dignity (46rem); the rail layout is the one whose band can actually reach that width */
@media (min-width:46rem){
  html[data-layout="rail"] .cover>.wrap{grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);
    column-gap:var(--gap);row-gap:0;align-items:start}
  html[data-layout="rail"] .cover-side{padding-top:1.9rem}
  html[data-layout="rail"] .cover-side .outcomes{margin-bottom:1.5rem}
  html[data-layout="rail"] .cover-side .cta{display:block;margin-bottom:1rem}
}

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
/* the shell band is the one that has to use what the pane gives it: rail + measure, not a
   centred column with two empty gutters (the desktop preview pane is ~837px, a phone-ish
   width a 1180px breakpoint never reached) */
.shell>.wrap{max-width:min(var(--shell),100%)}
/* the depth bar belongs to the same band as the cover and the rail, not to a centred
   column of its own */
.levels>.wrap{max-width:min(var(--shell),100%)}
.rail{display:none}
@media (min-width:52rem){
  html[data-layout="rail"] .shell>.wrap{display:grid;
    grid-template-columns:var(--rail) minmax(0,1fr);column-gap:var(--gap);align-items:start}
  html[data-layout="rail"] .rail{display:block;grid-column:1;grid-row:1;position:sticky;
    top:5.2rem;max-height:calc(100vh - 6.6rem);overflow-y:auto;padding-right:.3rem}
  html[data-layout="rail"] main{grid-column:2;grid-row:1;min-width:0;max-width:var(--maxw)}
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
/* below the rail breakpoint the article owns the whole band; a 900px line is worse than a
   little slack, so it keeps a reading measure and simply starts at the page margin */
@media (max-width:51.99rem){
  html[data-layout="rail"] main{max-width:min(42rem,100%)}
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
/* math is upright by default (it is arithmetic, not emphasis); only lone variables italic */
.m{font-style:normal;letter-spacing:0;white-space:nowrap;font-variant-numeric:tabular-nums}
.m i{font-style:italic;padding-right:.02em}
.m sub,.m sup{font-size:.68em;font-style:normal}
.m .fn{font-style:normal;letter-spacing:0}
/* sub/sup outside a math block: a figure label or a symbol named in prose */
sub,sup{font-size:.72em;line-height:0}
.flow-t sub,.flow-t sup,.steps sub,.steps sup,td sub,td sup{font-size:.76em}
/* a stacked fraction and a radicand, for LaTeX that slipped into a math block */
.m .frac{display:inline-flex;flex-direction:column;vertical-align:-0.42em;text-align:center;
  font-size:.94em;line-height:1.08;margin:0 .16em}
.m .frac .num{border-bottom:1px solid currentColor;padding:0 .24em .08em}
.m .frac .den{padding:.08em .24em 0}
.m .sqrt{border-top:1px solid currentColor;padding:.02em .2em 0 .12em}
.m .sqrt::before{content:"√";margin-right:.04em;border:0}
/* a parenthesis that has to span a stacked fraction, and long formulas that may wrap */
.m .dlg{display:inline-block;transform:scaleY(2.2);transform-origin:center;line-height:1}
.mathblock{overflow-x:auto}
.mathblock .m{white-space:normal}
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
/* two deliberate lines: the count/duration, then the depth it opens at */
.cta .est span{display:block}
.cta .est span + span{margin-top:.15rem}
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
/* a derivation is a table of rows: every line shares the three columns, so the = signs line
   up and a long right-hand side wraps under itself instead of starting over at the margin */
.mathblock{font-size:1.24rem;line-height:1.62;padding:1.2rem 0;color:var(--ink);
  display:grid;grid-template-columns:max-content min-content minmax(0,1fr);
  column-gap:.55rem;row-gap:.34rem;align-items:baseline}
.mathline{display:contents}
.mathblock .m{white-space:normal}
.m-lhs{text-align:right;color:var(--ink)}
.m-eq{color:var(--muted);padding:0 .1rem}
.m-rhs{min-width:0;overflow-wrap:normal;word-break:normal;text-align:left}
.math-note{grid-column:1/-1;font-size:.92rem;line-height:1.62;color:var(--muted);
  margin:.35rem 0 .1rem;max-width:52ch}
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
/* an SVG figure belongs to the page: it inherits the palette, so light, dark and print all
   work from one drawing instead of a baked image that only matches one theme */
.fig-svg{width:100%;height:auto;display:block;overflow:visible;font-family:var(--mono)}
.fig-frame{width:100%;margin-inline:auto}
.fig-svg text{fill:var(--ink);font-size:12.5px;font-family:var(--mono)}
.fig-svg .nd{fill:var(--surface);stroke:var(--line);stroke-width:1.3}
.fig-svg .nd.on{stroke:var(--accent);stroke-width:1.8}
.fig-svg .ed{fill:none;stroke:var(--muted);stroke-width:1.3;stroke-linejoin:round}
.fig-svg .reply{stroke-dasharray:4 4;opacity:.8}
.fig-svg .ll{stroke:var(--line);stroke-width:1.1;stroke-dasharray:3 5}
.fig-svg .ah{fill:var(--muted)}
.fig-svg .elabel{fill:color-mix(in srgb,var(--ink) 72%,var(--bg));font-size:11px}
.fig-svg .elabel-bg{fill:var(--bg)}

/* ---- layouts: the identity's third axis has to change something real ---- */
html[data-layout="column"] .rail,
html[data-layout="bleed"] .rail{display:none!important}
/* no rail means the measure is the whole band */
html[data-layout="column"]{--maxw:34rem;--rail:0rem;--gap:0rem}
html[data-layout="bleed"]{--maxw:42rem;--rail:0rem;--gap:0rem}
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
.prov-line{font-family:var(--mono);font-size:.72rem;letter-spacing:.04em;max-width:60ch}
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
  /* one column on paper: the hero's side column is a screen arrangement */
  .cover>.wrap{display:block}
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
  /* resources filtered to the screen depth come back for paper: the page prints all three */
  .vcard[hidden],ul.reading li[hidden]{display:grid!important}
  .lv::before{content:attr(data-lab);display:block;font-weight:600;font-size:8.5pt;letter-spacing:.12em;
    text-transform:uppercase;color:#000!important;margin-bottom:.35rem;border-top:1px solid #999;padding-top:.4rem}
  .sec{page-break-inside:auto}
  .sec-head,.ex,figure.fig,.vcard,.checkpoint{page-break-inside:avoid}
  iframe,canvas{display:none!important}
  /* an SVG figure is vector and prints: undo the screen palette for paper */
  .fig-svg .nd{fill:#fff!important;stroke:#666!important}
  .fig-svg .nd.on{stroke:#000!important}
  .fig-svg text,.fig-svg .elabel{fill:#000!important}
  .fig-svg .ed,.fig-svg .ll{stroke:#333!important}
  .fig-svg .ah{fill:#333!important}
  .fig-svg .elabel-bg{fill:#fff!important}
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
  /* resources are tagged with a depth; show the ones that match the depth on screen, and
     fall back to all of them when nothing matches rather than showing an empty shelf */
  function applyResourceLevels(level){
    var cards=[].slice.call(document.querySelectorAll('.vcard, ul.reading li'))
      .filter(function(c){return !!c.dataset.level});
    if(!cards.length)return;
    var any=cards.some(function(c){return c.dataset.level===level});
    /* an untagged item is for every depth: it is never hidden */
    cards.forEach(function(c){c.hidden=any&&c.dataset.level!==level});
  }
  function applyLevel(level,persist){
    root.dataset.level=level;state.level=level;
    document.querySelectorAll('.seg button').forEach(function(b){
      b.setAttribute('aria-pressed',String(b.dataset.level===level));
    });
    applyResourceLevels(level);
    if(persist){
      save();
      refreshResume();
      // canvases in a hidden level were sized from clientWidth 0; redraw the ones now visible
      if(typeof draw==='function')draw();
    }
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
  function refreshResume(){
    /* called from applyLevel too: the line names the depth, so it must not keep saying the
       depth the page opened at after you switch */
    if(!resume||!secs||!secs.length)return;
    var count=Object.keys(state.seen).length;
    if(!count)return;
    resume.hidden=false;
    resume.textContent=count+' of '+secs.length+' sections opened · depth '+state.level;
  }
  refreshResume();
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
      return;
    }
    /* no bridge (plain file, or the desktop preview pane): the honest fallback is the
       clipboard, and only the promise says whether the copy actually happened */
    var clip=navigator.clipboard;
    if(!clip||typeof clip.writeText!=='function'){cb(false,'no bridge and no clipboard');return}
    try{
      clip.writeText(prompt).then(
        function(){cb(false,'no bridge — copied to clipboard')},
        function(){cb(false,'no bridge and the clipboard refused')});
    }catch(e){cb(false,'no bridge and no clipboard')}
  }
  document.querySelectorAll('[data-hermes-send]').forEach(function(b){
    b.addEventListener('click',function(){send(b.getAttribute('data-hermes-send'),function(ok,err){
      if(!ok){b.textContent=/copied/.test(err||'')?'Copied to clipboard':'Could not send ('+err+')'}
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
      send(p,function(ok,err){
        b.textContent=ok?'Asked — see the chat'
          :(/copied/.test(err||'')?'Copied to your clipboard':'Could not send ('+err+')');
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
        if(pts.length<=6){
          /* every category is named when there is room: two labels on a three-bar chart
             leaves the middle bar anonymous, which reads as a missing value */
          pts.forEach(function(p,i){
            var lab=(p.label||'').slice(0,14); if(!lab)return;
            var lw=g.measureText(lab).width;
            g.fillText(lab,Math.min(Math.max(X(i)-lw/2,pad.l),w-pad.r-lw),h-6);
          });
        }else{
          if(first)g.fillText(first.slice(0,14),pad.l,h-6);
          if(last){var lw=g.measureText(last.slice(0,14)).width;g.fillText(last.slice(0,14),w-pad.r-lw,h-6)}
        }
        var mv=hi.toLocaleString();
        g.fillText(mv,Math.min(Math.max(X(maxIdx)-8,pad.l+2),w-pad.r-g.measureText(mv).width),Math.max(pad.t+16,Y(hi)-6));
      }
    });
  }
  draw();addEventListener('resize',draw);matchMedia('(prefers-color-scheme: dark)').addEventListener('change',draw);
})();
"""


def render(spec: Dict[str, Any]) -> str:
    """Build the full HTML document for a validated course spec."""
    prov = spec.get("provenance")
    prov = prov if isinstance(prov, dict) else {}
    sources = prov.get("sources")
    sources = sources if isinstance(sources, list) else []
    token = _CITE_LIMIT.set(min(40, len(sources)))
    try:
        return _render_page(spec)
    finally:
        _CITE_LIMIT.reset(token)


def _render_page(spec: Dict[str, Any]) -> str:
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
    # the reading time is for the depth the page opens at, not for all three levels at once
    opening = spec.get("default_level")
    opening = opening if opening in ("novice", "intermediate", "advanced") else "intermediate"
    words = sum(_level_words(section, opening) for section in spec["sections"])
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
        '<div class="cover-main">\n'
        f'<p class="kicker"><span>Course</span><span>·</span><span>{html.escape(spec["language"].upper())}</span></p>\n'
        f'<h1>{_inline(spec["title"])}</h1>\n'
        + (f'<p class="sub">{_inline(spec["subtitle"])}</p>\n' if spec["subtitle"] else "")
        + (f'<p class="thesis">{_inline(spec["thesis"])}</p>\n' if spec["thesis"] else "")
        + _notice(spec)
        + '</div>\n<div class="cover-side">\n'
        + (f'<ul class="outcomes">{outcomes}</ul>\n' if outcomes else "")
        + f'<div class="cta"><button class="btn primary" type="button" id="start">Start reading</button>'
        + f'<span class="est"><span>{len(spec["sections"])} sections · ~{minutes} min</span> '
        + f'<span>opens at {spec["default_level"]}</span></span></div>\n'
        + '<div class="resume" id="resume" hidden></div>\n'
        + "</div>\n</div></header>\n"
        + '<div class="levels"><div class="wrap"><span class="lab">Depth</span>'
        + f'<div class="seg" role="group" aria-label="Level of depth">{seg}</div>'
        + '<span class="spacer"></span>'
        + '<span class="pctwrap"><span class="track"><i id="trackfill"></i></span>'
        + '<span class="pct" id="pctlab">0% read</span></span></div></div>\n'
        + (f'<div class="shell"><div class="wrap">'
           + (f'<nav class="rail" aria-label="Sections"><ol>{rail_items}</ol></nav>'
              if rail_items else "")
           + f'<main>{sections}{_resources(spec)}{ask}</main></div></div>\n')
        + _provenance_footer(spec, identity)
        + f"<script>{_JS}</script>\n</body>\n</html>\n"
    )


def function_text(identity: Dict[str, str]) -> str:
    """Small credit line: which identity the page was drawn with (auditable)."""
    bits = [identity["palette"], identity["type_pairing"].replace("-", " "),
            identity["layout"], identity["motion"]]
    return "<span>design · " + " / ".join(bits) + "</span>"
