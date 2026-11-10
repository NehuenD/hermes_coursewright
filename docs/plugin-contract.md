# What Nous documents about plugins, and where coursewright stands

Source: the official documentation set (`/docs/llms-full.txt`, fetched 2026-11-10), the
authoring guide `developer-guide/plugins/index.md`, the catalog pages, and the local source at
`~/.hermes/hermes-agent` (Hermes **v0.23.1**). Where the docs and the source disagree, the
source wins and it is noted.

---

## 1. How a plugin is structured

A native Python plugin is a directory with a manifest and an entrypoint:

```
~/.hermes/plugins/<name>/
├── plugin.yaml      # identity + declarations
├── __init__.py      # register(ctx) — the only required entrypoint
├── schemas.py       # tool schemas (what the model reads)
└── tools.py         # handlers (what runs)
```

Rules that decide whether it is even discovered:

- **Flat or one category level deep.** `plugins/web/firecrawl/` works; two levels does not.
- **Manifest name must match the directory** for user plugins that override a bundled key.
- Native plugins need **both** `plugin.yaml` and `__init__.py` with `register(ctx)`.
  Portable Agent Plugins v1 packages (`plugin.json`) need neither and are a separate adapter.
- Discovery precedence, later wins: bundled → `$HERMES_HOME/plugins/` → `./.hermes/plugins/`
  (opt-in) → the `hermes_agent.plugins` entry point.
- `memory/`, `context_engine/`, `model-providers/`, `cron_providers/`, `computer_use/`,
  `config_backends/` are excluded from general discovery and have their own loaders.

## 2. `register(ctx)` is the whole contract

Called once at load, on a daemon thread with a **10 s deadline** (`plugins.load_timeout_seconds`,
`0` disables, max 600). If it raises, the plugin is disabled and the error shows in
`hermes plugins list`; anything it registered is unwound. Registering after the deadline is
ignored (the context is marked abandoned).

Surfaces, all documented:

| Purpose | Call |
|---|---|
| Tool the model can call | `ctx.register_tool(name, toolset, schema, handler, check_fn=, override=)` |
| Lifecycle observer | `ctx.register_hook("<hook>", fn)` — fn must accept `**kwargs` |
| Payload rewrite | `ctx.register_middleware(kind, fn)` — `tool_request`, `llm_request`, `tool_execution`, `llm_execution` |
| Slash command, every surface | `ctx.register_command("name", fn, description=)` |
| CLI subcommand, local only | `ctx.register_cli_command(...)` |
| Prompt section | `ctx.register_system_prompt_section(...)` |
| Bundled skill | `ctx.register_skill(...)` |
| Model call | `ctx.llm`, `ctx.llm.complete_structured(...)` |
| Real tool call from a command | `ctx.dispatch_tool(name, args)` — approvals, redaction and budget apply |

Handler contract: **returns a JSON string**, accepts `**kwargs`. `pre_tool_call` fails closed;
bounded hooks fail open after 30 s.

## 3. Manifest v2 fields

`manifest_version: 2` is additive over v1; unknown keys are ignored (never a load failure).

| Field | Meaning |
|---|---|
| `manifest_version` / `api_version` | file format (≤ 2) vs runtime API generation — separate axes |
| `name`, `version`, `description`, `author`, `license`, `homepage`, `tags` | identity |
| `provides_tools`, `provides_hooks` | **declarative**; validated, not a loading switch |
| `capabilities` | consent list: `tools.override`, `llm.model_override`, `llm.provider_override`, `llm.agent_id_override`, `llm.profile_override`, `llm.task_override`, `llm.side_question`, `gateway.platform_actions` |
| `requires_env` | gate + install prompt; simple or `{name, description, url, secret}` |
| `requires_auth`, `oauth` | sign-ins Hermes stores and refreshes — the plugin never sees tokens |
| `requires_plugins` | advisory deps; drives load order (topological), never blocks |
| `python_dependencies` / `pyproject.toml` | deps installed with consent, inside Hermes core constraints; **pin upper bounds** |
| `python_runtime: external` | sidecar plugin manages its own interpreter |
| `config_schema` | typed settings under `plugins.entries.<id>.settings`; also renders the Desktop settings form |
| `config_keys` | dotted paths in Hermes' own config the plugin reads |
| `requires_hermes` | SemVer floor, enforced by the loader (`">=0.23"`) |
| `provides_locales` | language pack ids |

Settings and state have different owners: settings are user-visible (`config.yaml`), state is
plugin-owned (`$HERMES_HOME/plugin-data/<name>/`, 10 MiB, atomic, profile-scoped).

## 4. Three owners of data — and the one that matters at review time

- `config.yaml` → `plugins.entries.<id>.settings` via `ctx.get_config` / `ctx.set_config`.
- `$HERMES_HOME/plugin-data/<name>/` → `plugin_data_dir(name)` / `plugin_db(name)`.
- `.env` → **only** through `agent.secret_scope.get_secret(NAME)` (catalog rule 11).

Catalog rule 11 makes that last one a hard gate: a plugin "never reads `.env` files,
`auth.json`, another tool's login or any other credential store directly", and a bare
`os.environ` read is called out because it can pick up another profile's key under
multiplexing.

## 5. Isolation

`plugins.isolation: host` moves third-party Python into a per-profile host process with JSON-RPC
`ctx`. Refused there: platform adapters and handlers, browser-vault fills, dashboard auth,
approval transports, `post_message`, and `register_cli_command`. `hermes plugins validate`
prints an isolation verdict, so a "would this survive the host?" answer is free before shipping.

## 6. Getting it onto someone else's machine

Three paths, in order of trust:

1. **Drop the directory** into `$HERMES_HOME/plugins/<name>/` and `hermes plugins enable <name>`.
   Plugins are opt-in; `plugins.disabled` beats `plugins.enabled`.
2. **`hermes plugins install <owner/repo | git-url>`** — custom source, no review, warning
   banner, branch tip rather than a pin.
3. **The catalog** — the reviewed path, installed by name at a pinned commit.

Catalog entry schema (`plugin-catalog/<name>.yaml`), enforced by
`hermes_cli/plugin_catalog.py`:

| Field | Rule |
|---|---|
| `name` | `[a-z0-9_-]{1,64}`, the key used by `hermes plugins install` |
| `repo` | `https://` URL |
| `sha` | **full 40-hex commit** — `^[0-9a-f]{40}$`; short SHAs are rejected |
| `tier` | `official` / `verified` / `community` |
| `category` | one of `desktop, memory, platform, web, tools, voice, automation, models, general` |
| `description`, `maintainer`, `title`, `version` (quoted), `platforms`, `requires_hermes` | metadata |
| `capabilities` | `provides_tools`, `provides_hooks`, `provides_middleware`, `requires_env` |
| `docs_url`, `known_issues`, `image`, `screenshots`, `onboarding` | optional |

Admission rules (17 of them, the ones that change design):

- one file per entry, merged by a maintainer only; **no self-serve registry**;
- **exact SHA pins only**; SHA bumps are new PRs;
- **no self-updating code** — a plugin may not replace its own files;
- **declared capabilities must match reality** (validation fails otherwise);
- install scanner runs: `dangerous` blocks, `caution` is reviewed;
- **credentials only through Hermes** (rule 11);
- approvals and unattended runs respected — no auto-approve, nothing hangs under cron;
- risky behaviour disclosed in the PR and README;
- `requires_hermes` is a SemVer floor, honoured by the loader;
- no skins/forks of bundled plugins; one listing per lineage;
- **no unscannable payloads** — no binaries, no `.pyc`, no opaque archives.

Catalog CI runs, at the pinned commit:

```bash
hermes plugins validate /path/to/plugin --install-deps
```

---

## 7. coursewright against that contract

Current state, measured rather than assumed (`hermes plugins validate .`,
`hermes plugins doctor .`, `hermes_cli.plugin_catalog.entry_from_mapping()` on our entry).

### What is already right

| Requirement | State |
|---|---|
| `plugin.yaml` + `__init__.py` with `register(ctx)` | ✓ flat layout, one directory |
| Manifest v2 | ✓ `manifest_version: 2` |
| Declared tools match registrations | ✓ 4/4, validated |
| `config_schema` with defaults, labels, descriptions | ✓ 10 keys, one `secret` |
| Tool handlers return JSON, never raise | ✓ every handler wrapped |
| No core overrides, no self-updater, no binaries | ✓ `no core override` and `security scan` pass; 14 tracked files, 3.3 MB |
| Registration self-check | ✓ `_selfcheck()` runs inside `register()` and fails the load loudly |
| Isolation audit | ✓ only one in-process-only call (`ctx.post_message`) |

`hermes plugins validate .` → **Validation passed**.
`hermes plugins doctor .` → registrations OK, **4 warnings** (below).

### Gaps, in the order they block shipping

| # | Gap | Evidence | Fix |
|---|---|---|---|
| 1 | **Catalog entry is rejected by the real parser** | `sha must be a full 40-character hex commit SHA (got '866217e')` | pin the full SHA; regenerate it in the release step |
| 2 | **`category: learning` is not a valid shelf** | `CATALOG_CATEGORIES` = desktop, memory, platform, web, tools, voice, automation, models, general | `category: tools` (or `general`) |
| 3 | **Python deps have no upper bounds** | doctor: 4 × "has no upper bound"; rule 10 asks for oldest API-compatible floor + bound | `httpx>=0.27,<1`, `Pillow>=10,<13`, `yt-dlp>=2025.1,<2027`, `youtube-transcript-api>=1.0,<2` |
| 4 | **`requires_hermes` not declared** | validate: "requires_hermes — not declared"; rule 14 wants it in the manifest **and** the entry | `requires_hermes: ">=0.23"` in both |
| 5 | **Secret read outside the Hermes route** | `__init__.py:209` falls back to `os.environ.get("FIRECRAWL_API_KEY")` | declare `env: FIRECRAWL_API_KEY` on the `secret` config key and read it through the plugin's secret route; delete the env fallback |
| 6 | **`post_message` blocks host isolation** | validate: "in-process only: ctx.post_message()" | probe and degrade: progress notices only when the live handle exists |
| 7 | **Entry advertises a command the schema cannot carry** | `capabilities.provides_commands: [course, courses]` — not in the catalog capability schema, silently ignored | drop it; describe `/course` in `description`, or accept that users won't see it in the catalog UI |
| 8 | **Extra entry keys** | `license`, `notes`, `tags` — unknown to the catalog parser, ignored | fold disclosures into `description`; keep the file to the documented schema |

### Not a gap, worth knowing

- We call `ctx.llm` **without** declaring `capabilities` — correct. Plain calls need no
  capability; only model/provider/agent overrides do, and we request none.
- `version: 0.4.4` is unquoted. It parses (not a number), but rule 14 says quote it, and a
  future `0.5` would parse as a float.
- The 30 s slash-command budget and the "a command's return value ends the turn" rule already
  match how `/course` hands off, and the 420 s tool budget is why `coursewright_build` takes
  `background: true`.
- `hermes plugins validate --install-deps` (what catalog CI runs) has not been run here; it
  installs the four deps into a scratch environment and would surface any conflict with
  Hermes' core constraints.

### Fix order

1. `sha` (full) + `category` — the entry cannot be parsed at all today.
2. Dependency bounds + `requires_hermes` — the two things a reviewer will ask for first.
3. Secret route for `FIRECRAWL_API_KEY` — the only admission-rule violation (rule 11).
4. `post_message` probe — keeps the plugin portable to `plugins.isolation: host`.
5. Tidy the entry file, then run `validate --install-deps` and open the PR.
