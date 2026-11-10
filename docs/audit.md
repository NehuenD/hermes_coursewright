# Production audit — v0.4.6

Everything below was run, not read. One real defect found and fixed; one UX wart found and
documented. Date: 2026-11-10.

## Contract and packaging

| Check | Command | Result |
|---|---|---|
| Test suite | `python -m unittest discover -s tests -t .` | **98 tests, OK**, no network |
| Catalog CI parity | `hermes plugins validate . --install-deps` | **15/15**, dependency install included |
| Loader/registry | `hermes plugins doctor .` | registrations OK, **no warnings** |
| Catalog entry | `entry_from_mapping()` from `hermes_cli.plugin_catalog` | parses: sha 40 hex, `category: tools`, `version 0.4.6`, `requires_hermes >=0.23` |
| Repo hygiene | `git ls-files` | 23 files, 3.1 MB, no binaries, no `.pyc` |

## Fresh-profile install (the "someone else's machine" test)

```
HERMES_HOME=/tmp/cw-fresh hermes plugins install https://github.com/NehuenD/hermes_coursewright --no-enable
HERMES_HOME=/tmp/cw-fresh hermes plugins enable coursewright
```

Cloned, resolved and installed the four declared dependencies, enabled, and registered the tool
set (`hermes tools list` → `🔌 Coursewright`). Driving `coursewright_status` through the real
plugin manager in that home:

```json
{"ok": true, "store": "/tmp/cw-fresh/plugin-data/coursewright",
 "readers": ["firecrawl", "urllib"], "surface": "local", "courses": 0,
 "search": {"ok": false, "detail": "no web backend: keyless rungs only (wikipedia, arxiv, stackexchange)"}}
```

Honest degradation on a bare install: no host extractor, no search backend, still two readers
and the keyless finders, and it says so rather than failing.

**Defect found here** — see below.

## Security

Hostile payloads through the *real* path (`spec.validate` → `render`), in every field a model
can author: title, subtitle, section title, hook, prose blocks, markdown links, figure labels,
captions, source titles and URLs, video titles and ids, reading links.

| Probe | Result |
|---|---|
| `<script>` tags in output | 1 — the page's own player script |
| `javascript:` in any `href`/`src` | 0 |
| Inline event handlers | 0 (only matches inside escaped text) |
| `<img onerror>` / `<svg onload>` executing | 0 |
| Payload text surviving as markup | 0 |

`spec._safe_url` drops non-http(s) URLs from sources, reading and quote hrefs; `_inline` escapes
before applying its markup subset; `_svg_text` escapes SVG. A `javascript:` source URL never
reaches the page.

Path traversal: `_slug()` reduces anything to `[a-z0-9-]`, so `../../etc/passwd` becomes
`etc-passwd`; every write lands under the plugin's own data directory or the configured
`courses_dir`.

Subprocess: the single `subprocess.run` is `yt-dlp` with an argv list (no shell), and the search
query is passed as `ytsearchN:<query>` so a leading `-` cannot become a flag.

## Rendering

| Check | Result |
|---|---|
| Determinism across two processes | **identical** (sha256 `b299e1d8…`) for the same spec |
| Duplicate `id` attributes | 19 ids, **0 duplicates** |
| Tag balance | **0** unclosed or stray tags |
| Figure alignment (15 figures, 4 courses, 3 layouts) | 0 off-centre, 0 overflow |

## Interaction (headless Chromium, real page)

| Behaviour | Result |
|---|---|
| Depth switch | `intermediate` → `advanced` (644 words) → `novice` (449 words) |
| Exercise click | correct answer marked `right`, feedback text, "why" panel revealed |
| Reload | level restored, earlier answer restored (picked `wrong`, correct `right`) |
| Video card | 3 cards in the page, depth-filtered to 1 visible, click opens `youtube-nocookie.com/embed/<id>` |
| Console errors | none |

## Defect found and fixed

**Pillow's upper bound excluded the version Hermes itself ships.** `python_dependencies` said
`Pillow>=10,<13`; Hermes core pins `Pillow==13.0.0`, so a fresh install loaded the plugin with:

```
Plugin coursewright declares Python dependencies that are not installed: Pillow>=10,<13.
For an enabled plugin, run hermes pm repair, then restart Hermes.
```

`hermes pm repair` cannot fix a bound that excludes the installed version — the user would have
hit a wall on first run. Core pins checked: Pillow 13.0.0, httpx 0.28.1,
youtube-transcript-api 1.2.4. Bound widened to `Pillow>=10,<14` in 0.4.6; the same fresh-profile
install now shows no warning.

## Wart found, not fixed

**`/course` waits for a free agent turn.** The command hands the build off by injecting a turn
rather than starting it, so typing `/course …` while the agent is mid-work queues the build until
that turn ends. Observed live: the first attempt produced no build at all; the identical command
during a free turn built and posted normally. The tool path (`coursewright_build` with
`background: true`) does not have this problem. Fix: have the command start the same thread the
tool starts, and use the injected turn only for the reply.

## Not exercised

- **Two builds in parallel.** Per-slug locks and the identity claim are reviewed by reading, not
  run concurrently; each cycle costs ~10 minutes of model time.
- **Model refusal mid-build.** Covered by the suite (`test_a_refused_model_fails_the_build`) but
  not reproduced against a live provider.
- **Windows and macOS.** No path or shell assumptions found in review; nothing was run there.
