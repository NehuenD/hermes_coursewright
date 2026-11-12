"""Research: find and read real material for a course, and say what happened.

The ladder is probed, never assumed:

    find   web_search        (whatever web backend the user configured)
    read   web_extract       (same backend, markdown)
    rescue Firecrawl         (opt-in URL, live probe, extraction came up short)
    rescue guarded direct    (stdlib, per-hop URL/address policy, no core import)
    watch  yt-dlp + youtube_transcript_api  (keyless; transcripts decide quotability)

Every rung that fails is recorded, not papered over. Nothing here invents a source: a
course with no sources says so in its own page.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set

try:  # loaded as a package by the plugin loader
    from . import netguard
except ImportError:  # direct import during development
    import netguard  # type: ignore

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 7 * 24 * 3600
# The scraper is opt-in by explicit URL: an empty setting never knocks on a port on the user's
# machine. A self-hosted stack is configured by name (e.g. http://localhost:3002).
_FIRECRAWL_DEFAULT = ""
_READ_MAX_BYTES = 400_000
_THUMB_MAX_BYTES = 60_000
_THUMB_TOTAL_BYTES = 2_000_000
_MIN_VIDEO_SECONDS = 120
_MAX_VIDEO_SECONDS = 5400
# Domains that make a course's source list worse, not better.
_NOISE = ("pinterest.", "facebook.", "instagram.", "tiktok.", "quora.", "answers.yahoo",
          "coursehero.", "chegg.", "brainly.", "scribd.", "slideshare.", "studocu.")
_PREFERRED = ("wikipedia.org", "arxiv.org", "doi.org", "nature.com", "science.org",
              "mit.edu", "stanford.edu", "acm.org", "ieee.org", "developer.mozilla.org",
              ".gov", "nist.gov", "who.int")

_UA = "coursewright/0.1 (+hermes plugin; research for a course page)"

# Keyless finders. The plugin owns no credentials by design, so when the user's web
# backend has no search key (or no backend at all) research still has somewhere to go.
_WIKI_SEARCH = "https://{lang}.wikipedia.org/w/api.php"
_STACK_SEARCH = "https://api.stackexchange.com/2.3/search/advanced"
_ARXIV_SEARCH = "https://export.arxiv.org/api/query"


def _http_text(url: str, timeout: float = 14.0) -> str:
    # Hosts here are fixed above (_WIKI_SEARCH / _STACK_SEARCH / _ARXIV_SEARCH), not
    # attacker-influenced, so a private-address guard adds nothing. Untrusted targets go
    # through netguard in stdlib_read and _firecrawl_scrape.
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _find_wikipedia(topic: str, language: str, limit: int = 3) -> List[Dict[str, str]]:
    lang = (language or "en")[:2]
    query = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": topic,
                                    "format": "json", "srlimit": str(limit)})
    data = json.loads(_http_text(f"{_WIKI_SEARCH.format(lang=lang)}?{query}"))
    found = []
    for hit in (data.get("query", {}).get("search") or [])[:limit]:
        title = hit.get("title") or ""
        if not title:
            continue
        found.append({
            "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            "title": title,
            "excerpt": _strip_tags(hit.get("snippet", ""))[:300],
            "domain": f"{lang}.wikipedia.org",
        })
    return found


def _find_stackexchange(topic: str, limit: int = 2) -> List[Dict[str, str]]:
    query = urllib.parse.urlencode({"order": "desc", "sort": "relevance", "q": topic,
                                    "site": "stackoverflow", "pagesize": str(limit),
                                    "filter": "withbody"})
    data = json.loads(_http_text(f"{_STACK_SEARCH}?{query}", timeout=16))
    found = []
    for item in (data.get("items") or [])[:limit]:
        found.append({
            "url": item.get("link") or "",
            "title": _strip_tags(item.get("title", ""))[:180],
            "excerpt": _strip_tags(item.get("body", ""))[:400],
            "domain": "stackoverflow.com",
        })
    return [item for item in found if item["url"]]


def _find_arxiv(topic: str, limit: int = 2) -> List[Dict[str, str]]:
    query = urllib.parse.urlencode({"search_query": f"all:{topic}", "max_results": str(limit)})
    feed = _http_text(f"{_ARXIV_SEARCH}?{query}", timeout=18)
    found = []
    for entry in re.findall(r"<entry>(.*?)</entry>", feed, re.S)[:limit]:
        title = re.search(r"<title>(.*?)</title>", entry, re.S)
        summary = re.search(r"<summary>(.*?)</summary>", entry, re.S)
        ident = re.search(r"<id>(.*?)</id>", entry, re.S)
        if not ident:
            continue
        found.append({
            "url": ident.group(1).strip(),
            "title": html.unescape(" ".join((title.group(1) if title else "").split()))[:180],
            "excerpt": html.unescape(" ".join((summary.group(1) if summary else "").split()))[:400],
            "domain": "arxiv.org",
        })
    return found


def _keyless_find(topic: str, language: str) -> List[tuple]:
    """Try the keyless finders in order of usefulness for teaching; report what worked."""
    results: List[tuple] = []
    for name, finder in (("wikipedia", lambda: _find_wikipedia(topic, language)),
                         ("arxiv", lambda: _find_arxiv(topic)),
                         ("stackexchange", lambda: _find_stackexchange(topic))):
        try:
            found = [item for item in finder() if item.get("url")]
        except Exception as exc:
            logger.info("coursewright research: keyless finder %s failed: %s", name, exc)
            continue
        if found:
            results.append((name, found))
    return results


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _cache_path(cache_dir, topic: str, language: str, *, want_videos: bool = True,
                max_videos: int = 6, max_sources: int = 12):
    from pathlib import Path

    # the caps are part of the identity of the record: a course built with videos off must not
    # serve its video-less record to the next build that asked for videos
    digest = hashlib.sha1(
        f"{topic.strip().lower()}|{language}|v{int(bool(want_videos))}|"
        f"{int(max_videos)}|{int(max_sources)}".encode()).hexdigest()[:16]
    return Path(cache_dir) / f"research-{digest}.json"


def _load_cache(cache_dir, topic: str, language: str, **caps) -> Optional[dict]:
    try:
        path = _cache_path(cache_dir, topic, language, **caps)
        if not path.exists() or time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("sources") is None:
            return None
        # a record with nothing in it is an outage that got cached, not a result
        if not data.get("sources") and not data.get("videos"):
            return None
        data["cached"] = True
        return data
    except Exception:
        pass
    return None


def _save_cache(cache_dir, topic: str, language: str, record: dict, **caps) -> None:
    try:
        path = _cache_path(cache_dir, topic, language, **caps)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# rung probes
# --------------------------------------------------------------------------
def _tool_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _search(ctx, query: str, limit: int) -> List[Dict[str, str]]:
    """One web_search call through Hermes's configured backend."""
    try:
        raw = ctx.dispatch_tool("web_search", {"query": query, "limit": limit})
    except Exception as exc:
        logger.info("coursewright research: web_search failed (%s)", exc)
        return []
    data = _tool_json(raw)
    results = []
    if isinstance(data, dict):
        for item in ((data.get("data") or {}).get("web") or data.get("results") or []):
            if isinstance(item, dict) and item.get("url"):
                results.append({"url": str(item["url"]), "title": str(item.get("title") or ""),
                                "snippet": str(item.get("description") or item.get("snippet") or "")})
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("url"):
                results.append({"url": str(item["url"]), "title": str(item.get("title") or ""),
                                "snippet": str(item.get("description") or "")})
    return results


def _extract(ctx, urls: List[str], char_limit: int = 3000) -> str:
    try:
        raw = ctx.dispatch_tool("web_extract", {"urls": urls[:3], "char_limit": char_limit})
    except Exception as exc:
        logger.info("coursewright research: web_extract failed (%s)", exc)
        return ""
    data = _tool_json(raw)
    if isinstance(data, dict):
        text = data.get("content") or data.get("text") or ""
        if not text and isinstance(data.get("results"), list):
            text = "\n".join(str(r.get("content") or "") for r in data["results"] if isinstance(r, dict))
        return str(text)
    return raw if isinstance(raw, str) else ""


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _score(result: Dict[str, str]) -> int:
    domain = _domain(result.get("url", ""))
    score = 0
    if any(p in domain for p in _PREFERRED):
        score += 3
    if "/docs" in result.get("url", "") or "reference" in result.get("url", ""):
        score += 1
    if len(result.get("snippet") or "") > 80:
        score += 1
    return score


_BOILERPLATE = re.compile(
    r"(skip to (main )?content|content selection saved|describe the issue below|"
    r"cookie (policy|settings)|accept all|privacy policy|terms of (use|service)|"
    r"sign in|log in|subscribe|newsletter|advertisement|share this|related articles|"
    r"table of contents|back to top|we use cookies|your browser)", re.I)
_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    out = []
    for part in parts:
        clean = part.strip(" #*_>-\t")
        if not clean or _BOILERPLATE.search(clean):
            continue
        words = len(clean.split())
        letters = sum(ch.isalpha() or ch.isspace() for ch in clean)
        if words < 6 or words > 60:
            continue
        if letters / max(len(clean), 1) < 0.72:
            continue
        out.append(clean)
    return out


def _digest(text: str, limit: int = 800) -> str:
    """Prose, not navigation chrome: the first few real sentences of the page."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _demark(text)
    text = re.sub(r"\s+", " ", text)
    picked, total = [], 0
    for sentence in _sentences(text):
        picked.append(sentence)
        total += len(sentence) + 1
        if total >= limit:
            break
    return " ".join(picked)[:limit]


def _demark(text: str) -> str:
    """Drop markdown syntax without eating the maths.

    The old blanket `[*_`#>|]+` turned "n -> n/2" into "n - n/2" and "3n + 1 > n" into
    "3n + 1 n": excerpts are handed to the writer, so a lost ">" flips an inequality. Only
    line-leading markers (headings, blockquotes) are syntax; a ">" or "#" mid-sentence is not.
    """
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", " ", text or "")
    text = re.sub(r"(?m)^[ \t]{0,3}>[ \t]?", " ", text)
    text = re.sub(r"[*_`|]+", " ", text)
    return text


def _dedupe_key(source: Dict[str, str]) -> str:
    """Same paper behind two URLs (abs page + listing) is one source."""
    url = source.get("url", "")
    match = _ARXIV_ID.search(url)
    if match:
        return "arxiv:" + match.group(1)
    title = re.sub(r"^\[[^\]]+\]\s*", "", source.get("title", "") or "")
    title = re.sub(r"\s*[|—-]\s*(arxiv|wikipedia|.*\.org).*$", "", title, flags=re.I)
    key = re.sub(r"[^a-z0-9]+", "", title.lower())[:60]
    return key or url


def _clean(text: str, limit: int = 900) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _demark(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def probe_web(ctx) -> bool:
    """Does the configured web backend actually answer? A config key is not evidence."""
    return bool(_search(ctx, "wikipedia", 1))


def firecrawl_target(caps: Optional[Dict[str, Any]]) -> tuple:
    """``(base_url, api_key)`` from settings; ``("", "")`` means "do not use a scraper".

    Empty is off; a full URL opts in to a self-hosted stack or a cloud API.
    ``off``/``none``/``no``/``false`` are recognised aliases for off, so a local Compose stack
    must now be named explicitly instead of being probed behind the user's back.
    """
    caps = caps or {}
    raw = str(caps.get("firecrawl_url") or "").strip()
    if raw.lower() in {"off", "none", "no", "false", "disabled", "-"}:
        return "", ""
    base = raw or _FIRECRAWL_DEFAULT
    if not base.startswith(("http://", "https://")):
        return "", ""
    return base.rstrip("/"), str(caps.get("firecrawl_key") or "").strip()


def probe_firecrawl(base: str = "", key: str = "", timeout: float = 2.5) -> bool:
    """Does that scraper actually answer? A configured URL is not evidence."""
    if not base:
        return False
    try:
        import httpx

        headers = {"Authorization": f"Bearer {key}"} if key else {}
        with httpx.Client(timeout=timeout, headers=headers) as client:
            return client.get(base + "/").status_code < 500
    except Exception:
        return False


def _firecrawl_scrape(url: str, base: str = "", key: str = "", timeout: float = 25.0) -> str:
    if not base:
        return ""
    # Guard the URL handed to the scraper, not the scraper's own endpoint: a self-hosted stack
    # of the user's own sits at a private address by design. The remote service follows its own
    # redirects, which is out of our reach; only the target we choose can be checked here.
    if not netguard.is_safe_url(url):
        logger.info("coursewright research: unsafe URL not sent to scraper: %s", _domain(url))
        return ""
    try:
        import httpx

        headers = {"Authorization": f"Bearer {key}"} if key else {}
        with httpx.Client(timeout=timeout, headers=headers) as client:
            resp = client.post(base + "/v1/scrape",
                               json={"url": url, "formats": ["markdown"], "onlyMainContent": True})
            if resp.status_code != 200:
                return ""
            payload = resp.json()
            return str(((payload.get("data") or {}).get("markdown")) or "")
    except Exception:
        return ""


_BLOCK_TAG = re.compile(r"(?i)<(?:br|/p|/div|/li|/tr|/h[1-6]|/section|/article)\s*/?>")
_DROP_TAG = re.compile(
    r"(?is)<(script|style|noscript|svg|nav|header|footer|aside|form|button|iframe)[^>]*>.*?</\1>")


def stdlib_read(url: str, timeout: float = 12.0) -> str:
    """Last rung of the reader ladder, guarded by the plugin's own outbound URL policy.

    It exists for the hosts the other two rungs miss — a proxy that passes HTML but blocks a
    scraping API, a cloud scraper with no key configured. Rougher text than a real extractor,
    but a course with a rough excerpt beats a course with no source at all. The target came out
    of a search result: netguard checks scheme and resolved addresses, and re-checks every
    redirect hop before dialling the address it validated. Nothing here falls back to unguarded
    urllib. See ``netguard.py`` for the exact policy, in stdlib only (a public plugin surface).
    """
    try:
        headers = {"User-Agent": _UA,
                   "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}
        status, response_headers, raw = netguard.fetch(url, timeout=timeout, headers=headers,
                                                       max_bytes=_READ_MAX_BYTES)
        if status >= 400:
            return ""
        kind = str(response_headers.get_content_type() or "")
        if kind and not (kind.startswith("text/") or "xhtml" in kind or "xml" in kind):
            return ""                          # a PDF or an image is not an excerpt
        charset = response_headers.get_content_charset() or "utf-8"
    except Exception:
        return ""
    try:
        page = raw.decode(charset, "replace")
    except LookupError:
        page = raw.decode("utf-8", "replace")
    return html_to_text(page)


def html_to_text(page: str) -> str:
    """HTML to readable text: drop the chrome, keep the paragraphs."""
    page = _DROP_TAG.sub(" ", page or "")
    page = _BLOCK_TAG.sub("\n", page)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", page))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# videos
# --------------------------------------------------------------------------
def probe_videos() -> bool:
    return bool(shutil.which("yt-dlp"))


# A child process must not inherit the parent's provider keys and tokens. yt-dlp is a
# third-party binary that loads third-party extractor code and makes network requests; a
# YouTube search needs PATH, a home for ~/.config/yt-dlp, and locale settings, nothing
# else. Windows needs the listed USERPROFILE/TEMP/TMP/SYSTEMROOT family. Deliberately no
# proxy variables here: they can carry proxy credentials, so they stay out of the child.
_CHILD_ENV_KEYS = ("PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "SYSTEMROOT",
                   "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


def _child_env() -> dict:
    """Minimal environment for an external tool; do not add a "convenient" key to this."""
    return {key: os.environ[key] for key in _CHILD_ENV_KEYS if os.environ.get(key)}


def _yt_search(topic: str, limit: int, queries: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if not shutil.which("yt-dlp"):
        return []
    wanted_count = max(3, min(limit * 3, 18))
    videos: List[Dict[str, Any]] = []
    seen: set = set()
    for query in (queries or [topic]):
        cmd = ["yt-dlp", "--flat-playlist", "--no-warnings", "--socket-timeout", "12",
               "--print", "%(id)s\t%(duration)s\t%(channel)s\t%(title)s",
               f"ytsearch{wanted_count}:{query}"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=45,
                                 env=_child_env()).stdout
        except Exception as exc:
            logger.info("coursewright research: yt-dlp failed for %r (%s)", query, exc)
            continue
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4 or not parts[0].strip() or parts[0].strip() in seen:
                continue
            try:
                duration = int(float(parts[1]))
            except (TypeError, ValueError):
                duration = 0
            title = parts[3].strip()
            if not title or not (_MIN_VIDEO_SECONDS <= duration <= _MAX_VIDEO_SECONDS):
                continue
            seen.add(parts[0].strip())
            videos.append({"id": parts[0].strip(), "duration": duration,
                           "channel": parts[2].strip(), "title": title})
    return videos


def has_transcript(video_id: str) -> bool:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        YouTubeTranscriptApi().fetch(video_id, languages=["en", "es", "pt"])
        return True
    except Exception:
        return False


def _thumb(video_id: str) -> str:
    """Inline a small WebP poster so the page stays self-contained and looks designed."""
    try:
        import httpx
        from PIL import Image

        # Fixed CDN host; video_id is a YouTube id, not an attacker-chosen host, so no guard.
        url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            resp = client.get(url)
        if resp.status_code != 200 or not resp.content:
            return ""
        image = Image.open(io.BytesIO(resp.content)).convert("RGB")
        image.thumbnail((480, 270))
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=68, method=4)
        blob = buffer.getvalue()
        if len(blob) > _THUMB_MAX_BYTES:
            return ""
        return "data:image/webp;base64," + base64.b64encode(blob).decode("ascii")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# gather
# --------------------------------------------------------------------------
_STOPWORDS = {"how", "what", "why", "the", "a", "an", "of", "and", "or", "to", "in", "on",
              "for", "with", "does", "do", "is", "are", "it", "its", "this", "that"}


def _content_words(text: str) -> set:
    return {word for word in re.findall(r"[a-z]{4,}", (text or "").lower()) if word not in _STOPWORDS}


def _on_topic(video: Dict[str, Any], topic_words: Set[str], query_words: Optional[Set[str]] = None,
              minimum: int = 2) -> bool:
    """A video is kept only if it shares enough vocabulary with what we searched for.

    Without this, "how a garbage collector finds live objects" returns footage of bin lorries.
    Sharing one word with the *topic itself* is enough ("Collatz" is the whole signal); the
    generic words the query templates add ("explained", "open problems") are not evidence, so
    they only count in pairs.
    """
    query_words = query_words or set()
    if not topic_words and not query_words:
        return True
    haystack = _content_words(f"{video.get('title', '')} {video.get('description', '')} "
                              f"{video.get('channel', '')}")
    if topic_words and (haystack & topic_words):
        return True
    return len(haystack & query_words) >= minimum


def _queries(topic: str) -> List[str]:
    return [
        f"{topic} explained",
        f"{topic} how it works technical details",
        f"{topic} limitations criticism open problems",
    ]


def gather(ctx, *, topic: str, language: str = "en", caps: Optional[Dict[str, Any]] = None,
           cache_dir=None, force: bool = False, want_videos: bool = True,
           video_queries: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run the ladder. Returns a research record; never raises."""
    caps = caps or {}
    # an explicit 0 means "off": `or` turned max_videos=0 into 6, both here and in the cache key
    max_sources = int(caps["max_sources"]) if caps.get("max_sources") is not None else 12
    max_videos = int(caps["max_videos"]) if caps.get("max_videos") is not None else 6
    deadline = time.monotonic() + float(caps.get("timeout") or 150)

    if cache_dir and not force:
        cached = _load_cache(cache_dir, topic, language, want_videos=want_videos,
                             max_videos=max_videos, max_sources=max_sources)
        if cached:
            logger.info("coursewright research: cache hit for %r", topic)
            return cached

    record: Dict[str, Any] = {
        "topic": topic, "language": language, "researched": False,
        "finders": [], "readers": [], "sources": [], "videos": [],
        "failures": [], "warnings": [], "fetched_at": _now(),
    }

    # a build with research off still wants its videos: max_sources == 0 means "do not read"
    want_sources = max_sources > 0
    web_ok = probe_web(ctx) if want_sources else False
    if web_ok:
        record["finders"].append("web_search")

    candidates: List[Dict[str, str]] = []
    if web_ok:
        seen_domains: Dict[str, int] = {}
        for query in _queries(topic):
            if time.monotonic() > deadline:
                record["warnings"].append("research hit its time budget; stopped early")
                break
            for result in _search(ctx, query, 5):
                url = result["url"]
                if any(noise in url.lower() for noise in _NOISE) or not url.startswith("http"):
                    continue
                domain = _domain(url)
                if seen_domains.get(domain, 0) >= 2:
                    continue
                seen_domains[domain] = seen_domains.get(domain, 0) + 1
                candidates.append(result)
        candidates.sort(key=_score, reverse=True)
        candidates = candidates[: max_sources + 4]
    elif want_sources:
        record["failures"].append("web search backend: no results (nothing configured, or it is down)")

    if want_sources and len(candidates) < 4:
        # keyless rungs: wikipedia / arxiv / stackexchange, all without credentials
        seen_urls = {item["url"] for item in candidates}
        for name, found in _keyless_find(topic, language):
            fresh = [item for item in found if item["url"] not in seen_urls]
            if not fresh:
                continue
            record["finders"].append(name)
            seen_urls.update(item["url"] for item in fresh)
            candidates.extend(fresh)
        if candidates:
            candidates.sort(key=_score, reverse=True)

    # read
    read_count = 0
    seen_keys: set = set()
    fc_base, fc_key = firecrawl_target(caps)
    fc_ok = probe_firecrawl(fc_base, fc_key) if fc_base else False
    if fc_base:
        record["readers"].append("firecrawl" if fc_ok else "firecrawl (unreachable)")
    for index, candidate in enumerate(candidates if want_sources else []):
        if read_count >= max_sources or time.monotonic() > deadline:
            break
        text = _extract(ctx, [candidate["url"]])
        reader = "web_extract"
        if len(text.strip()) < 200 and fc_ok and index < 8:
            # rescue 1: the configured scraper, only when it answers a live probe
            text = _firecrawl_scrape(candidate["url"], fc_base, fc_key)
            reader = "firecrawl"
        if len(text.strip()) < 200:
            # rescue 2: the guarded last rung; refuses private targets and redirect escapes
            text = stdlib_read(candidate["url"])
            reader = "guarded http"
        excerpt = _digest(text, 800)
        if len(excerpt) < 160:
            record["failures"].append(f"could not read {_domain(candidate['url']) or candidate['url']}")
            continue
        key = _dedupe_key(candidate)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        read_count += 1
        if reader not in record["readers"]:
            record["readers"].append(reader)
        record["sources"].append({
            "id": len(record["sources"]) + 1,
            "url": candidate["url"],
            "title": candidate["title"] or _domain(candidate["url"]),
            "domain": _domain(candidate["url"]),
            "excerpt": excerpt,
        })

    # videos
    if want_videos and probe_videos() and time.monotonic() < deadline:
        if "yt-dlp" not in record["finders"]:
            record["finders"].append("yt-dlp")
        found = _yt_search(topic, max_videos, queries=video_queries)
        # the topic's own words are the strongest signal; the query words only help when the
        # model-supplied queries are specific (the template queries add generic filler)
        topic_words = _content_words(topic)
        query_words: set = set()
        for query in (video_queries or []):
            query_words |= _content_words(query)
        kept = [video for video in found if _on_topic(video, topic_words, query_words)]
        dropped = len(found) - len(kept)
        if dropped:
            record["warnings"].append(
                f"{dropped} video result(s) ignored as off-topic for this course")
        budget = _THUMB_TOTAL_BYTES
        for video in kept[: max_videos * 2]:
            if len(record["videos"]) >= max_videos or time.monotonic() > deadline:
                break
            video["transcript"] = has_transcript(video["id"])
            thumb = _thumb(video["id"])
            if thumb and budget - len(thumb) > 0:
                video["thumb"] = thumb
                budget -= len(thumb)
            record["videos"].append(video)
    elif want_videos:
        record["failures"].append("video search: yt-dlp not installed")

    # "researched" means something real was retrieved, not that a backend was configured
    record["researched"] = bool(record["sources"] or record["videos"])
    if want_sources and len(record["sources"]) < 3:
        record["warnings"].append(
            f"only {len(record['sources'])} source(s) could be read; the advanced level is lightly grounded")
    # An empty run is an outage, not a result: caching it served "0 sources" to every rebuild
    # for a week, long after the backend recovered. _load_cache ignores such records too.
    if cache_dir and (record["sources"] or record["videos"]):
        _save_cache(cache_dir, topic, language, record, want_videos=want_videos,
                    max_videos=max_videos, max_sources=max_sources)
    logger.info("coursewright research: %d sources, %d videos, %d failures",
                len(record["sources"]), len(record["videos"]), len(record["failures"]))
    return record


def describe(ctx=None, caps: Optional[Dict[str, Any]] = None, *, probe_search: bool = True) -> Dict[str, Any]:
    """What this host can actually do, probed rather than assumed.

    Every entry says what the plugin will *use*, not what is configured: a key in a config
    file is not a working backend, and a course built on that assumption is a course with no
    sources. Cheap by default; the one network call is a single search when a ctx is given.
    """
    caps = caps or {}
    fc_base, fc_key = firecrawl_target(caps)
    search_ok = probe_web(ctx) if (ctx is not None and probe_search) else None

    readers = ["guarded http"]                 # no key or search backend required; stdlib guard
    if search_ok is not False:
        readers.insert(0, "web_extract")
    if fc_base and probe_firecrawl(fc_base, fc_key):
        readers.insert(0, "firecrawl")

    video_ok = probe_videos()
    transcript_ok = False
    try:
        import youtube_transcript_api  # noqa: F401
        transcript_ok = True
    except Exception:
        pass
    try:
        import PIL  # noqa: F401
        thumb_ok = True
    except Exception:
        thumb_ok = False

    return {
        "search": {
            "ok": search_ok,
            "detail": ("web_search answered" if search_ok else
                       "no web backend: keyless rungs only (wikipedia, arxiv, stackexchange)"
                       if search_ok is False else "not probed"),
        },
        "readers": readers,
        "firecrawl": {"configured": bool(fc_base), "url": fc_base, "key": bool(fc_key)},
        "videos": {"ok": video_ok,
                   "detail": "yt-dlp present" if video_ok else "yt-dlp not installed: no videos"},
        "transcripts": {"ok": transcript_ok},
        "thumbnails": {"ok": thumb_ok},
    }


def source_block(record: Optional[Dict[str, Any]], limit: int = 8) -> str:
    """The compact source list handed to the writer: id, title, excerpt."""
    if not record or not record.get("sources"):
        return ""
    lines = []
    for source in record["sources"][:limit]:
        lines.append(f"[{source['id']}] {source['title']} ({source['domain']})\n"
                     f"    {source['excerpt'][:520]}")
    return "\n".join(lines)
