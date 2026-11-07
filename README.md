# coursewright

coursewright builds a self-contained HTML course on any topic. It writes the same material
at three depths, novices first, and switches between them in place without a reload. A
course has figures, exercises with feedback, recommended videos and reading, and a footer
that lists every source it read.

## Install

```bash
hermes plugins install https://github.com/NehuenD/hermes_coursewright
```

Or clone it into `$HERMES_HOME/plugins/coursewright` and turn it on:

```bash
hermes plugins enable coursewright
```

No API keys. Research runs through the web tools the host already has configured.

## Use

```
coursewright_plan(topic="how a transformer works")     plan first, cheap
coursewright_build(topic="how a transformer works")    plan and write in one call
coursewright_build(plan_path=".../plans/x.json")       write from a saved plan
coursewright_build(spec_path=".../course.json")        render an existing spec
coursewright_list()                                    courses on disk
coursewright_status()                                  last build, ok or failed
/course <topic> [--level=novice|intermediate|advanced] [--lang=es] [--sections=6]
/courses [slug]
```

`/course` returns at once and hands the work to the agent as a turn, so the build runs
where tool progress is visible and the finished course arrives in the conversation. If no
session can be woken the command falls back to a background build that posts its result.
The tools are synchronous, so a build the agent calls shows as a long tool call.

Each build writes `$HERMES_HOME/plugin-data/coursewright/courses/<slug>/index.html` and a
copy in `~/Courses/<slug>/`, with the spec and the research record beside the page.

## How a course gets written

1. Plan. One model call returns the title, thesis, outcomes, section briefs and a page
   identity.
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

A four section course costs one plan call and four draft calls, takes about five minutes on
a laptop, and produces a page near 95 KB with posters inlined.

## Configuration

| key | default | meaning |
|---|---|---|
| `default_level` | `intermediate` | depth the course opens at |
| `courses_dir` | `~/Courses` | where the visible copy goes, empty disables it |
| `enable_research` | `true` | search and read before writing |
| `enable_videos` | `true` | look for videos |
| `max_sources` | `12` | pages read per course |
| `max_videos` | `6` | videos collected per course |

## Reading a course

- `j` and `k`, or the arrow keys, move between sections. `1`, `2` and `3` set the depth.
  `g` returns to the top. The page ignores depth keys while you type in an answer box.
- Depth is an attribute on the document. The same section is re-voiced in place, so
  switching moves nothing on screen.
- Figures sit in a shared slot above the levels, so a novice sees the diagram an advanced
  reader sees.
- Print drops the rail, the depth bar, the buttons and the video posters, turns dark cards
  into outlined boxes, labels each level, and appends the URL after every link.
- Progress, depth and answers persist in `localStorage` under `coursewright:<slug>`.

## What it will not do

- The model never writes HTML or CSS. It fills a spec and the renderer draws the page, so
  the renderer drops an unknown block kind with a warning instead of rendering it raw.
- A course cannot claim research it did not do. With no sources the page prints an
  Unresearched notice under the call to action, and repeats it in the footer.
- A citation cannot point at a page that was not read. Markers resolve into the footer
  list, which names each domain.
- A model that calls a blog post a paper gets corrected, and a year survives only when it
  appears in the source text.
- The relevance filter drops a video about the wrong sense of the topic, and the build
  note reports how many it dropped.

## Limits

- The page can send a message only inside the chat preview frame, through
  `window.hermes.send`.
  Opened as a plain file, the ask buttons copy the question to the clipboard instead.
- Progress does not survive in the preview frame, because that frame has an opaque origin.
  It works when you open the page as a file.
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
| `samples/collatz.json` | a real six section course, three depths per section |

## Verify

```bash
hermes plugins doctor .
hermes plugins validate .
```

## License

MIT. See `LICENSE`.
