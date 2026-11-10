# coursewright

coursewright builds a self-contained HTML course on any topic. It writes the same material
at three depths, novices first, and switches between them in place without a reload. A
course has figures, exercises with feedback, recommended videos and reading, and a footer
that lists every source it read.

## What it looks like

Four courses, each given its own visual identity by the rotation — same renderer, different
palette, type pairing and layout:

| | |
|---|---|
| ![graphite](docs/screenshots/cover-graphite.png) | ![sea glass](docs/screenshots/cover-seaglass.png) |
| ![carbon gold](docs/screenshots/cover-carbon-gold.png) | ![risograph](docs/screenshots/cover-risograph.png) |

Figures are drawn from the section's own outline — a sequence diagram for a mechanism, a chart
when the numbers are the point:

![a sequence diagram](docs/screenshots/figure-sequence.png)
![a chart](docs/screenshots/figure-chart.png)

## Install

```bash
hermes plugins install https://github.com/NehuenD/hermes_coursewright
```

Or clone it into `$HERMES_HOME/plugins/coursewright` and turn it on:

```bash
hermes plugins enable coursewright
```

No API keys, but it does need the network: the host model writes the course and the sources
are read from the web. Nothing else has to be configured — research uses whatever web tools
the host has, falls back to Wikipedia, arXiv and Stack Exchange when search returns nothing,
and reads pages with a built-in reader when the host's extractor cannot.

## Use

```
coursewright_plan(topic="how a transformer works")     plan first, cheap
coursewright_build(topic="how a transformer works")    plan and write in one call
coursewright_build(plan_path=".../plans/x.json")       write from a saved plan
coursewright_build(spec_path=".../course.json")        render an existing spec
coursewright_list()                                    courses on disk
coursewright_status()                                  last build, ok or failed
/course <topic> [--level=novice|intermediate|advanced] [--lang=es] [--sections=N]
/courses [slug]
```

`/course` returns at once and hands the work to the agent as a turn, so the build runs
where tool progress is visible and the finished course arrives in the conversation. If no
session can be woken the command falls back to a background build that posts its result.
The tools are synchronous, so a build the agent calls shows as a long tool call.

Each build writes `$HERMES_HOME/plugin-data/coursewright/courses/<slug>/index.html` and a
copy in `~/Courses/<slug>/`, with the spec and the research record beside the page.

## What a level has to contain

Each section is written three times, and none of the three is a summary. Every level runs the
same arc: what problem this part solves, the mechanism step by step, the worked example walked
through with its real values, and what it costs or where it fails. The budgets are there so the
explanation has room:

| level | words | blocks |
|---|---|---|
| novice | 350-500 | 5-7 |
| intermediate | 500-700 | 6-8 |
| advanced | 550-800 | 6-8 |

`author.py` enforces the parts a page cannot fake: a level under 300 words is sent back once,
and so is a level with no `steps`, `code`, `math` or `timeline` block — that block is where the
worked example lives. The planner gives each section a `teaches` (the mechanism, and the thing
readers get wrong) and an `example` (the concrete case: the actual text, numbers or code), so
the writer starts from a spine instead of a title.

## How a course gets written

1. Plan. One model call returns the title, thesis, outcomes, section briefs — each with the
   mechanism it establishes and the worked example it will carry — and a page identity.
2. Research. Find, read and watch, with each rung probed before use.
3. Draft. One call per section. Each call gets the outline and the numbered source
   excerpts, so a claim can point back with a `[n]` marker to the page it came from.
4. Picks. One small call selects videos and reading, and tags each with a depth.
5. Assemble. A section that fails twice is rebuilt thin and flagged. A section that wrote
   steps but no figure gets a flow figure drawn from those same steps.
6. Render. `spec.py` validates the spec and `render.py` draws the page.

| rung | call | what it gives |
|---|---|---|
| find | `web_search` | candidate URLs from the configured backend |
| read | `web_extract` | page text, cut down to prose excerpts |
| rescue | Firecrawl `/v1/scrape` | pages the backend refused, only when a probe says it answers |
| watch | `yt-dlp` | videos with captions and a poster |
| fallback | Wikipedia, arXiv, Stack Exchange | sources when search returns nothing, no key needed |

A four section course costs one plan call and four draft calls; an eight section course lands
near 160 KB with posters inlined and takes roughly ten to twenty minutes, depending on the
model. The planner is asked for 6-8 sections and the builder keeps all eight: the closing
section a plan asks for is not dropped.

Depth is not padding: the drafting call gets a section spine (what it establishes, and the
worked example it carries) and the length budget to use it. An eight section course measures
about 4,000 words at novice, 4,800 at intermediate and 5,400 at advanced — a short book, not a
leaflet — and every level walks at least one example through its actual values.

## Configuration

| key | default | meaning |
|---|---|---|
| `default_level` | `intermediate` | depth the course opens at |
| `courses_dir` | `~/Courses` | where the visible copy goes, empty disables it |
| `enable_research` | `true` | search and read before writing |
| `enable_videos` | `true` | look for videos |
| `max_sources` | `12` | pages read per course |
| `max_videos` | `6` | videos collected per course |
| `research_timeout` | `150` | seconds spent searching and reading before it writes anyway |
| `firecrawl_url` | `""` | scraper used when the host extractor returns nothing: empty probes a local stack on `http://localhost:3002`, `off` disables it, a full URL points anywhere |
| `firecrawl_key` | `""` | bearer token for that scraper, kept as a Hermes secret (`.env` name `FIRECRAWL_API_KEY`) and read through the secret scope |

### Debugging a build

`COURSEWRIGHT_DUMP=<dir>` writes each section's raw model reply, and the version it was coerced
into, under that directory. It is off unless you set it yourself, it is the only place the
plugin writes outside its own data directory, and it only ever contains prompts and model
output — the same text the course is built from. Use it when a section comes out wrong and you
want to see what the model actually said.

## What it needs from the host

The plugin is written to run on someone else's Hermes, so it probes instead of assuming, and
`coursewright_status` reports what it found:

| capability | used when | if it is missing |
|---|---|---|
| host model (`ctx.llm`) | always | the build fails with the reason rather than writing a stub |
| `web_search` | finding pages | keyless finders: Wikipedia, arXiv, Stack Exchange |
| `web_extract` | reading pages | the scraper below, then a built-in `urllib` reader |
| a scraper | second reader | `urllib` still reads plain HTML |
| `yt-dlp` | videos | the watch list is empty and the build says why |
| desktop preview pane | showing the page | a path and an `open …` line, and `::preview` only on desktop |

`search`, `readers`, `firecrawl`, `videos`, `transcripts`, `thumbnails` and `surface` are
what `coursewright_status` reports under `environment`. Two builds of one course cannot run
at once, across processes as well as threads.

## Reading a course

- `j` and `k`, or the arrow keys, move between sections. `1`, `2` and `3` set the depth.
  `g` returns to the top. The page ignores depth keys while you type in an answer box.
- The page uses the width it is given. From 832px up, the section rail docks beside the text
  and the hero runs two columns (title and thesis left, outcomes and the start button
  right); every band shares one left edge. Below that the rail drops and the text takes the
  full width. The `column` and `bleed` identities stay narrower on purpose.
- Depth is an attribute on the document. The same section is re-voiced in place, so
  switching moves nothing on screen.
- Figures sit in a shared slot above the levels, so a novice sees the diagram an advanced
  reader sees.
- Figures are drawn, not pasted. A `diagram` (layered graph: architecture, state machine,
  data flow, feedback loop) and a `sequence` (parties exchanging messages in time order)
  are laid out by the renderer and emitted as inline SVG that inherits the page palette, so
  one drawing serves light, dark and print. The model supplies nodes, edges and labels; it
  never supplies coordinates, sizes or colours. Each of those figures is also written beside
  the course as `figures/<section>.excalidraw`, so a reader can open it at excalidraw.com,
  move a box and keep the result.
- Math is plain text: `W_Q` renders as a subscript base plus script, `K^T` as a
  superscript, `R^(n×d)` and `sqrt(d_k)` too, and only in a `math` block does it turn
  LaTeX it was told not to write into a stacked fraction and a radical rather than
  printing it. A multi-letter base (`web_search`) is left alone: it is an identifier.
- Print drops the rail, the depth bar, the buttons and the video posters, turns dark cards
  into outlined boxes, labels each level, and appends the URL after every link. The SVG
  figures re-colour for paper (white boxes, black type) and print as line art; the canvas
  charts are screen-only and do not print.
- Videos and reading are tagged with a depth and the page shows the ones for the depth you
  are reading; items that carry no depth are always shown, and when nothing matches the
  current depth the whole list is shown rather than an empty shelf.
- Progress, depth and answers persist in `localStorage` under `coursewright:<slug>`. Answers
  are keyed by section id, so a rebuild that reorders sections does not move them.

## What it will not do

- The model never writes HTML or CSS. It fills a spec and the renderer draws the page, so
  the renderer drops an unknown block kind with a warning instead of rendering it raw.
- A course cannot claim research it did not do. With no sources the page prints an
  Unresearched notice under the call to action, and the footer says what actually happened
  in words, never as a count of zero: "written without a research pass, no outside sources
  were read, 2 dead ends". A course with videos but no readable page says the videos were
  checked for relevance, not for accuracy.
- A citation cannot point at a page that was not read. Markers resolve into the footer
  list, which names each domain, and a marker past the end of that list is dropped rather
  than linked to nothing.
- A model that calls a blog post a paper gets corrected, and a year survives only when it
  appears in the source text.
- A figure whose shape makes no sense (`steps` that is not a list, one bar) is dropped with
  a build note instead of taking the whole render down with it.
- Comparisons in prose survive: `n < 3`, `a >= b` and `Vec<T>` are text, not tags.
- The relevance filter drops a video about the wrong sense of the topic, and the build
  note reports how many it dropped.

## Limits

- The page can send a message only where the host injects `window.hermes` — the chat's
  inline preview frame. Opened as a plain file or in the desktop's preview pane (an
  Electron webview with no bridge), the ask buttons fall back to the clipboard, and say
  plainly when the copy was refused instead of claiming it worked.
- Progress, depth and answers persist through `localStorage` under `coursewright:<slug>`
  wherever the page has a real origin: opened as a file, and in the desktop preview pane
  (measured). An opaque-origin frame has no storage, so there the page runs stateless.
- Grading is split in two. The rubric check runs locally and answers at once. The
  "Ask Arden to grade it" button sends the question, the rubric and the answer into chat.
- Exercise kind alternates by section, multiple choice then recall, so both feedback
  paths exist in every course.
- The plugin detects video transcripts and reports them, and does not quote them at a
  timestamp yet.
- A slash command has a 30 second budget in the desktop client, so `/course` does not build
  inside the command: it hands the build to the agent as a turn, and the tool call is where
  the progress shows. If no live session can be woken — a gateway that is not running, a chat
  that has since closed — it falls back to a background build that posts its result, and
  `/courses` shows the course either way.
- One build per topic at a time: a second `/course` (or a tool call) for a topic already being
  built is refused instead of racing the first for the same directory.
- A running gateway holds the plugin modules and tool schemas it loaded. After changing the
  code, start a new session or restart the gateway.

## Files

| file | role |
|---|---|
| `plugin.yaml` | manifest, tool list, config schema |
| `__init__.py` | registration, the four tools, `/course` and `/courses` |
| `author.py` | plan, draft, resource picks, assembly, prompt construction |
| `research.py` | the find, read, rescue and watch rungs, caching, deduplication |
| `spec.py` | spec validation and the closed block set |
| `render.py` | the page: HTML, CSS, JS, figures, print rules |
| `tokens.py` | the design token space and identity assignment |
| `tests/` | regression tests for validation, rendering, research and the commands |
| `samples/collatz.json` | a real six section course, three depths per section |

## Verify

```bash
hermes plugins doctor .
hermes plugins validate .
python3 -m unittest discover -s tests -t .   # regression tests, no network
```

## License

MIT. See `LICENSE`.
