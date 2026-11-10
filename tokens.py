"""The design-token space.

The model chooses NAMES from these tables; it never writes CSS. `resolve()`
validates a course's requested identity and substitutes a default for anything
unknown, so a course can look different every time but never look broken.
"""

from __future__ import annotations

from typing import Any, Dict

# --------------------------------------------------------------------------
# Palettes
#
# Every palette is pre-checked: body text >= 4.5:1 on bg and surface, large
# display text >= 3:1, accent on surface >= 3:1. Both a light and a dark set are
# defined; the page follows prefers-color-scheme unless `data-scheme` forces one.
# --------------------------------------------------------------------------
PALETTES: Dict[str, Dict[str, Dict[str, str]]] = {
    "ink-copper": {
        "light": {"bg": "#faf7f2", "surface": "#f2ece1", "ink": "#1d1a16", "muted": "#5d5449",
                  "accent": "#a4522a", "accent2": "#2c5f57", "line": "#ddd3c3", "ok": "#3f6b3a", "warn": "#9a6a12"},
        "dark": {"bg": "#14120f", "surface": "#1e1a16", "ink": "#efe9df", "muted": "#b0a496",
                 "accent": "#e08d5c", "accent2": "#7fb3a8", "line": "#332d26", "ok": "#8bbf83", "warn": "#d8a844"},
    },
    "graphite": {
        "light": {"bg": "#fbfbfa", "surface": "#f1f2f0", "ink": "#16181a", "muted": "#616a70",
                  "accent": "#1f6feb", "accent2": "#7a4dff", "line": "#dcdfe0", "ok": "#2f7a4d", "warn": "#9a6a12"},
        "dark": {"bg": "#101113", "surface": "#181a1d", "ink": "#e9ecef", "muted": "#939ba3",
                 "accent": "#5b9dff", "accent2": "#a98bff", "line": "#2a2d31", "ok": "#63c08a", "warn": "#d8a844"},
    },
    "sea-glass": {
        "light": {"bg": "#f5faf8", "surface": "#e9f3ef", "ink": "#0f1a17", "muted": "#52655f",
                  "accent": "#0f766e", "accent2": "#9a5b2a", "line": "#cfe2da", "ok": "#2f7a4d", "warn": "#8a6410"},
        "dark": {"bg": "#0c1412", "surface": "#122220", "ink": "#e4f0eb", "muted": "#8ba79f",
                 "accent": "#5ec8b8", "accent2": "#e0a86c", "line": "#203430", "ok": "#6fc48d", "warn": "#d8a844"},
    },
    "carbon-gold": {
        "light": {"bg": "#fcfaf4", "surface": "#f4efe1", "ink": "#161410", "muted": "#6a6252",
                  "accent": "#8a6a12", "accent2": "#3f4b5b", "line": "#e2d9c4", "ok": "#3f6b3a", "warn": "#9a6a12"},
        "dark": {"bg": "#121110", "surface": "#1c1a17", "ink": "#f2ece0", "muted": "#a79d8c",
                 "accent": "#d9b25f", "accent2": "#8fa6c4", "line": "#312c25", "ok": "#8bbf83", "warn": "#d8a844"},
    },
    "plum-chalk": {
        "light": {"bg": "#fbf8fb", "surface": "#f2ecf4", "ink": "#191320", "muted": "#6b6076",
                  "accent": "#6d3b93", "accent2": "#8a5a2b", "line": "#e0d6e8", "ok": "#3f6b3a", "warn": "#9a6a12"},
        "dark": {"bg": "#120f16", "surface": "#1b1620", "ink": "#efe8f4", "muted": "#a294ae",
                 "accent": "#b98ee0", "accent2": "#d8a06a", "line": "#2d2635", "ok": "#8bbf83", "warn": "#d8a844"},
    },
    "risograph": {
        "light": {"bg": "#fffdf7", "surface": "#f6f1e4", "ink": "#1a1a1a", "muted": "#5f5c55",
                  "accent": "#e2452b", "accent2": "#1b4d8f", "line": "#e6ddc9", "ok": "#2f7a4d", "warn": "#9a6a12"},
        "dark": {"bg": "#141310", "surface": "#1d1b16", "ink": "#f4f0e6", "muted": "#a09a8c",
                 "accent": "#ff6a4d", "accent2": "#6f9fe0", "line": "#332f27", "ok": "#8bbf83", "warn": "#d8a844"},
    },
}

# --------------------------------------------------------------------------
# Type pairings — system-safe stacks only, no web fonts, no network.
# --------------------------------------------------------------------------
# A named face first, then the platform's own idea of "serif": a page that ships no web fonts
# renders in whatever the reader has, and the difference between Source Serif and the
# fallback is most of why one page looks designed and the other looks defaulted.
_SERIF = ('"Source Serif 4", ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, '
          '"Book Antiqua", "Hoefler Text", Charter, Georgia, "DejaVu Serif", serif')
_SANS = ('"Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, '
         '"Helvetica Neue", "DejaVu Sans", Arial, sans-serif')
_MONO = ('ui-monospace, "JetBrains Mono", "Cascadia Code", "SF Mono", Menlo, Consolas, '
         '"DejaVu Sans Mono", monospace')

TYPE_PAIRINGS: Dict[str, Dict[str, Any]] = {
    "editorial-serif": {
        "label": "Editorial serif",
        "display": _SERIF, "text": _SERIF, "mono": _MONO,
        "text_size": "19px", "leading": "1.68", "display_tracking": "-0.021em", "display_weight": "600",
    },
    "technical-grotesk": {
        "label": "Technical grotesk",
        "display": _SANS, "text": _SANS, "mono": _MONO,
        "text_size": "18px", "leading": "1.62", "display_tracking": "-0.03em", "display_weight": "650",
    },
    "humanist": {
        "label": "Humanist",
        "display": _SANS, "text": _SERIF, "mono": _MONO,
        "text_size": "19px", "leading": "1.7", "display_tracking": "-0.018em", "display_weight": "600",
    },
    "mono-accent": {
        "label": "Mono accent",
        "display": _MONO, "text": _SERIF, "mono": _MONO,
        "text_size": "19px", "leading": "1.66", "display_tracking": "-0.04em", "display_weight": "600",
    },
    "literary": {
        "label": "Literary",
        "display": _SERIF, "text": _SERIF, "mono": _MONO,
        "text_size": "20px", "leading": "1.78", "display_tracking": "-0.012em", "display_weight": "500",
    },
}

# --------------------------------------------------------------------------
# Layout grammars
# --------------------------------------------------------------------------
LAYOUTS = {
    "column": "single column, generous margins",
    "rail": "sticky section rail on the left, resources rail on the right",
    "bleed": "full-width figures, narrow text column",
}

# --------------------------------------------------------------------------
# Motion budgets
# --------------------------------------------------------------------------
MOTIONS: Dict[str, float] = {"still": 0.0, "restrained": 1.0, "lively": 1.7}

def assign(history: list) -> dict:
    """Pick the least-used combination across existing courses.

    The model reliably chose the same restrained, safe identities build after build, so the
    six palettes and three layouts never all appeared. Coverage is a property of the set, not
    of one decision: pick what has been used least.
    """
    from collections import Counter

    def least_used(field: str, options) -> str:
        opts = list(options)
        # never repeat the most recent course's choice while a fresh option exists:
        # "least used" alone would happily serve three bleed layouts in a row
        recent = (history[0] or {}).get(field) if history else None
        fresh = [option for option in opts if option != recent]
        if fresh:
            opts = fresh
        counts = Counter(row.get(field) for row in history if row.get(field))
        return sorted(opts, key=lambda option: (counts.get(option, 0), list(options).index(option)))[0]

    return {
        "palette": least_used("palette", list(PALETTES)),
        "type_pairing": least_used("type_pairing", list(TYPE_PAIRINGS)),
        "layout": least_used("layout", list(LAYOUTS)),
        "motion": least_used("motion", list(MOTIONS)),
    }


DEFAULT_IDENTITY = {
    "palette": "ink-copper",
    "type_pairing": "editorial-serif",
    "layout": "rail",
    "motion": "restrained",
    "rationale": "",
}

_IDENTITY_KEYS = tuple(DEFAULT_IDENTITY)


def resolve(identity: Any) -> Dict[str, str]:
    """Validated identity: unknown values fall back to the default, never crash."""
    out = dict(DEFAULT_IDENTITY)
    if not isinstance(identity, dict):
        return out
    table = {"palette": PALETTES, "type_pairing": TYPE_PAIRINGS, "layout": LAYOUTS, "motion": MOTIONS}
    for key in _IDENTITY_KEYS:
        value = identity.get(key)
        if key == "rationale":
            out["rationale"] = str(value or "")[:280]
            continue
        if isinstance(value, str) and value.strip().lower() in table[key]:
            out[key] = value.strip().lower()
    return out
