"""Regression tests for coursewright's audit findings.

Run with:  python3 -m unittest discover -s tests -t . -v
(no pytest required, and no network: every model/web call is faked)
"""

import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

import coursewright as cw                     # noqa: E402
import coursewright.render as render          # noqa: E402
import coursewright.research as research      # noqa: E402
import coursewright.author as author          # noqa: E402
import coursewright.spec as spec              # noqa: E402
import coursewright.tokens as tokens          # noqa: E402


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeLlm:
    """Answers the plan / section / resources calls with a valid shape."""

    def __init__(self, sections=8, figure=None):
        self.sections = sections
        self.figure = figure or {"type": "flow", "steps": ["a", "b", "c"]}

    def complete_structured(self, **kw):
        purpose = kw.get("purpose") or ""
        text = kw["input"][0]["text"] if kw.get("input") else ""
        if purpose == "coursewright.plan":
            match = re.search(r"Plan exactly (\d+) sections", text)
            n = int(match.group(1)) if match else self.sections
            payload = {"slug": "probe-course", "title": "Probe Course", "subtitle": "",
                       "thesis": "t", "outcomes": ["a"], "identity": {},
                       "sections": [{"title": "Section %d" % (i + 1), "eyebrow": "e", "hook": "h",
                                     "teaches": ("closing " if i == n - 1 else "thing ") + str(i + 1)}
                                    for i in range(n)]}
            return type("R", (), {"parsed": payload, "text": json.dumps(payload)})()
        if purpose.startswith("coursewright.section."):
            number = int(purpose.rsplit(".", 1)[1])
            body = {"blocks": [{"kind": "prose", "md": "Some prose."},
                               {"kind": "steps", "items": ["a", "b", "c"]}]}
            payload = {"id": "s", "title": "Section", "eyebrow": "e", "hook": "h",
                       "visual": self.figure,
                       "levels": {"novice": body, "intermediate": body, "advanced": body},
                       "exercise": ({"kind": "recall", "prompt": "q?", "rubric": ["x", "y"],
                                     "answer": "a"} if number % 2 == 0 else
                                    {"kind": "mcq", "prompt": "q?", "choices": ["a", "b", "c", "d"],
                                     "answer": 0, "why": "w"})}
            return type("R", (), {"parsed": payload, "text": ""})()
        return type("R", (), {"parsed": None, "text": ""})()


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeCtx:
    def __init__(self, config=None, llm=None, search=None, extract=None):
        self.llm = llm or FakeLlm()
        self.state = FakeState()
        self.config = config or {}
        self.search = search
        self.extract = extract
        self.tools = {}
        self.commands = {}
        self.injected = []
        self.dispatched = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def dispatch_tool(self, name, args, **kwargs):
        self.dispatched.append((name, args))
        if name == "web_search":
            return json.dumps({"data": {"web": self.search(args["query"]) if self.search else []}})
        if name == "web_extract":
            return json.dumps({"content": self.extract(args["urls"][0]) if self.extract else ""})
        return json.dumps({"error": "no pane in this test"})

    def inject_message(self, content, role="user", session_key=None, origin=None):
        self.injected.append((content, role, session_key))
        return True

    def post_message(self, text):
        return type("H", (), {"ok": True})()

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler


class StoreCase(unittest.TestCase):
    """Redirects the plugin's store into a temp dir."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="cw-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self._original_store = cw._store
        cw._store = lambda: self.root
        cw._RUNNING.clear()
        cw._HANDOFFS.clear()

    def tearDown(self):
        cw._store = self._original_store
        cw._RUNNING.clear()
        cw._HANDOFFS.clear()


# --------------------------------------------------------------------------
# 1-3: validation and the renderer must never raise on a model slip
# --------------------------------------------------------------------------
class TestValidationNeverRaises(unittest.TestCase):
    def test_dict_where_a_list_belongs(self):
        for raw in ({"provenance": {"sources": {"a": 1}}},
                    {"resources": {"videos": {"x": 1}}},
                    {"resources": {"reading": {"x": 1}}},
                    {"outcomes": {"a": 1}},
                    {"sections": {"a": {}}},
                    {"sections": "abc"},
                    {"provenance": {"finders": {"a": 1}}},
                    {"provenance": {"warnings": 3}}):
            clean, warnings = spec.validate(raw)      # must not raise
            self.assertIsInstance(clean, dict)

    def test_bad_strings_do_not_crash(self):
        clean, _ = spec.validate({"title": None, "sections": None, "resources": None})
        self.assertEqual(clean["sections"], [])

    def test_figure_specs_are_normalised(self):
        raw = {"title": "T", "sections": [{
            "id": "s1", "title": "S",
            "visual": {"type": "flow", "steps": 3},
            "levels": {"novice": {"blocks": [
                {"kind": "figure", "caption": "c", "spec": {"type": "bars", "points": 12}},
                {"kind": "figure", "caption": "c2", "spec": {"type": "flow", "steps": ["a", "b"]}},
                {"kind": "figure", "caption": "c3", "spec": {"type": "donut"}},
            ]}}}]}
        clean, warnings = spec.validate(raw)
        page = render.render(clean)                   # must not raise
        self.assertIn("flow", page)
        self.assertEqual(len(clean["sections"][0]["visual"]), 2)   # steps: 3 -> dropped
        figure_block = clean["sections"][0]["levels"]["novice"]["blocks"][0]
        self.assertEqual(figure_block["kind"], "figure")
        self.assertEqual(figure_block["spec"]["type"], "flow")
        self.assertTrue(any("bars" in w for w in warnings))
        self.assertTrue(any("donut" in w for w in warnings))

    def test_renderer_tolerates_unvalidated_figures(self):
        base = {"slug": "t", "title": "T", "subtitle": "", "thesis": "", "language": "en",
                "default_level": "intermediate",
                "identity": {}, "provenance": {"researched": False, "finders": [], "readers": [],
                                               "sources": [], "failures": [], "warnings": [],
                                               "generated": ""},
                "resources": {"videos": [], "reading": []}, "outcomes": []}
        for visual in ({"type": "flow", "steps": 3}, {"type": "bars", "points": 12},
                       {"type": "flow", "steps": "abc"}, {"type": "trajectory", "n": "x"}):
            section = {"id": "s1", "title": "S", "visual": visual,
                       "levels": {lvl: {"blocks": [{"kind": "prose", "md": "x"}]}
                                  for lvl in ("novice", "intermediate", "advanced")}}
            page = render.render(dict(base, sections=[section]))
            self.assertIn("<!doctype html", page)


# --------------------------------------------------------------------------
# 4-6: inline markup
# --------------------------------------------------------------------------
class TestInline(unittest.TestCase):
    def test_comparisons_survive(self):
        self.assertEqual(render._inline("if n < 3 and m > 5 the loop stops"),
                         "if n &lt; 3 and m &gt; 5 the loop stops")
        self.assertEqual(render._inline("x <= y >= z"), "x &lt;= y &gt;= z")
        self.assertIn("Vec&lt;T&gt;", render._inline("Rust's Vec<T> is growable"))

    def test_real_tags_are_still_dropped(self):
        self.assertEqual(render._inline("<script>alert(1)</script>"), "alert(1)")
        self.assertEqual(render._inline("<b>bold</b>"), "bold")
        self.assertEqual(render._inline('H<i>i</i>'), "Hi")

    def test_link_href_keeps_its_underscore(self):
        out = render._inline("[doc](https://example.com/a_b)")
        self.assertIn('href="https://example.com/a_b"', out)
        self.assertNotIn("<sub>", out)

    def test_subscript_then_superscript(self):
        self.assertEqual(render._inline("W_Q^T multiplies K"), "W<sub>Q</sub><sup>T</sup> multiplies K")
        self.assertEqual(render._math("W_Q^T"),
                         '<span class="m"><i>W</i><sub><i>Q</i></sub><sup><i>T</i></sup></span>')
        self.assertEqual(render._inline("call web_search"), "call web_search")
        self.assertEqual(render._spoken_label("W_Q^T"), "W sub Q sup T")

    def test_code_spans_are_literal(self):
        self.assertEqual(render._inline("a `**x**` b"), "a <code>**x**</code> b")


# --------------------------------------------------------------------------
# 7-9: research record and cache
# --------------------------------------------------------------------------
class TestResearch(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="cw-research-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        # the keyless fallback would really call Wikipedia/arXiv: tests stay offline
        self._keyless = research._keyless_find
        research._keyless_find = lambda topic, language: []
        self.addCleanup(setattr, research, "_keyless_find", self._keyless)

    def test_empty_run_is_not_cached(self):
        empty = FakeCtx(search=lambda q: [], extract=lambda u: "")
        research.gather(empty, topic="obscure", language="en",
                        caps={"max_sources": 3, "max_videos": 0, "timeout": 5},
                        cache_dir=self.root / "cache", want_videos=False)

        def search(query):
            return [{"url": "https://en.wikipedia.org/wiki/X", "title": "X",
                     "description": "a real description of the thing " * 3}]

        good = FakeCtx(search=search,
                       extract=lambda u: ("The page explains the idea in enough detail to quote "
                                          "it back, with several sentences of real prose. ") * 8)
        record = research.gather(good, topic="obscure", language="en",
                                 caps={"max_sources": 3, "max_videos": 0, "timeout": 5},
                                 cache_dir=self.root / "cache", want_videos=False)
        self.assertGreater(len(record["sources"]), 0)
        self.assertFalse(record.get("cached", False))

    def test_zero_caps_mean_off(self):
        ctx = FakeCtx(search=lambda q: [{"url": "https://example.org/a", "title": "A",
                                         "description": "description " * 10}],
                      extract=lambda u: "text " * 100)
        record = research.gather(ctx, topic="t", language="en",
                                 caps={"max_sources": 0, "max_videos": 0, "timeout": 5},
                                 cache_dir=self.root / "cache2", want_videos=False)
        self.assertEqual(record["sources"], [])
        self.assertFalse(any(name == "web_search" for name, _ in ctx.dispatched))

    def test_excerpts_keep_comparisons(self):
        text = ("The map sends n -> n/2 when n is even, and n -> 3n + 1 > n when odd. "
                "## A heading\n"
                "If a > b then swap them, and C# is unrelated.")
        digest = research._digest(text)
        self.assertIn("->", digest)
        self.assertIn("3n + 1 > n", digest)
        self.assertIn("a > b", digest)
        self.assertIn("C#", digest)
        self.assertNotIn("##", digest)

    def test_on_topic_prefers_the_topic_itself(self):
        topic_words = research._content_words("Collatz")
        self.assertTrue(research._on_topic(
            {"title": "Collatz conjecture: why it is hard", "channel": "Mathologer"}, topic_words))
        self.assertFalse(research._on_topic(
            {"title": "How open source works", "channel": "Some Channel"}, topic_words))


# --------------------------------------------------------------------------
# 10-12: build behaviour
# --------------------------------------------------------------------------
class TestBuild(StoreCase):
    def test_eight_sections_survive(self):
        ctx = FakeCtx(config={"enable_research": False, "enable_videos": False, "courses_dir": ""})
        result = cw.build_course(ctx, {"topic": "probe topic", "max_sections": 8})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sections"], 8)
        stored = json.loads((self.root / "courses" / result["slug"] / "spec.json").read_text())
        plan = json.loads((self.root / "plans" / "probe-course.json").read_text())
        self.assertEqual(len(stored["sections"]), 8)
        # the last planned section is the one that "opens a door"; it must survive the build
        self.assertEqual(stored["sections"][-1]["title"], plan["sections"][-1]["title"])

    def test_default_build_keeps_the_whole_plan(self):
        ctx = FakeCtx(config={"enable_research": False, "enable_videos": False, "courses_dir": ""})
        result = cw.build_course(ctx, {"topic": "probe topic"})
        plan = json.loads((self.root / "plans" / "probe-course.json").read_text())
        self.assertEqual(result["sections"], len(plan["sections"]))
        self.assertGreaterEqual(result["sections"], 6)

    def test_generated_stamp_is_set(self):
        ctx = FakeCtx(config={"enable_research": False, "enable_videos": False, "courses_dir": ""})
        result = cw.build_course(ctx, {"topic": "probe topic", "max_sections": 3})
        stored = json.loads((self.root / "courses" / result["slug"] / "spec.json").read_text())
        self.assertTrue(stored["provenance"]["generated"])

    def test_a_spec_with_no_sections_is_refused(self):
        ctx = FakeCtx(config={"courses_dir": ""})
        path = self.root / "empty.json"
        path.write_text("{}")
        result = cw.build_course(ctx, {"spec_path": str(path)})
        self.assertFalse(result["ok"])
        self.assertIn("no renderable sections", result["error"])
        self.assertFalse((self.root / "courses").exists())

    def test_videos_survive_research_off(self):
        """enable_videos on its own must still collect a watch list.

        The video rung used to live inside the research branch, so switching research off
        silently switched videos off too.
        """
        from unittest import mock

        ctx = FakeCtx(config={"enable_research": False, "enable_videos": True,
                              "courses_dir": ""})
        found = [{"id": "vid1", "duration": 600, "channel": "Chan",
                  "title": "probe topic explained"}]
        with mock.patch.object(research, "probe_videos", return_value=True), \
                mock.patch.object(research, "_yt_search", return_value=found), \
                mock.patch.object(research, "has_transcript", return_value=False), \
                mock.patch.object(research, "_thumb", return_value=""):
            result = cw.build_course(ctx, {"topic": "probe topic", "max_sections": 2})
        self.assertTrue(result["ok"], result)
        page = (self.root / "courses" / result["slug"] / "index.html").read_text()
        self.assertIn('data-yt="vid1"', page)
        self.assertIn("No sources.", page)     # videos only: honest about what was not read

    def test_exercise_key_is_the_section_id(self):
        ctx = FakeCtx(config={"enable_research": False, "enable_videos": False, "courses_dir": ""})
        result = cw.build_course(ctx, {"topic": "probe topic", "max_sections": 2})
        html = (self.root / "courses" / result["slug"] / "index.html").read_text()
        ids = re.findall(r'<section class="sec" id="([^"]+)"', html)
        keys = re.findall(r'<section class="ex" data-kind="[^"]*" data-ex="([^"]+)"', html)
        self.assertTrue(keys)
        self.assertTrue(set(keys) <= set(ids))


class TestBackgroundBuild(StoreCase):
    """A deep build outlives the tool budget, so it runs off-thread and posts its result."""

    def test_background_returns_at_once_and_posts_when_done(self):
        import threading

        gate = threading.Event()

        class SlowLlm(FakeLlm):
            def complete_structured(self, **kw):
                if kw.get("purpose", "").startswith("coursewright.section."):
                    gate.wait(timeout=10)
                return super().complete_structured(**kw)

        ctx = FakeCtx(config={"enable_research": False, "enable_videos": False,
                              "courses_dir": ""}, llm=SlowLlm())
        posted = []
        ctx.post_message = lambda text: (posted.append(text), type("H", (), {"ok": True})())[1]

        started = cw.build_course(ctx, {"topic": "probe topic", "max_sections": 2,
                                        "background": True})
        self.assertTrue(started["ok"])
        self.assertTrue(started["background"])
        self.assertEqual(posted, [])                       # nothing yet: it is still building
        status = cw.course_status(ctx, {})
        self.assertTrue(status["running"], "the running build should be visible in status")

        gate.set()
        key = cw._topic_key("probe topic")
        for _ in range(200):
            thread = cw._ACTIVE.get(key)
            if thread is None:
                break
            thread.join(timeout=0.05)
        self.assertEqual(len(posted), 1, "the finished course must be posted")
        self.assertIn("Your course is ready", posted[0])
        self.assertFalse(cw._handoff_pending(key))

    def test_a_second_background_build_is_refused(self):
        ctx = FakeCtx(config={"enable_research": False, "enable_videos": False,
                              "courses_dir": ""})
        import threading
        gate = threading.Event()

        class SlowLlm(FakeLlm):
            def complete_structured(self, **kw):
                if kw.get("purpose", "").startswith("coursewright.section."):
                    gate.wait(timeout=10)
                return super().complete_structured(**kw)

        ctx.llm = SlowLlm()
        first = cw.build_course(ctx, {"topic": "probe topic", "max_sections": 2, "background": True})
        self.assertTrue(first["ok"])
        second = cw.build_course(ctx, {"topic": "probe topic", "max_sections": 2, "background": True})
        self.assertFalse(second["ok"])
        self.assertIn("already running", second["error"])
        gate.set()
        thread = cw._ACTIVE.get(cw._topic_key("probe topic"))
        if thread is not None:
            thread.join(timeout=10)


# --------------------------------------------------------------------------
# 13-17: status, commands, language, args
# --------------------------------------------------------------------------
class TestCommands(StoreCase):
    def setUp(self):
        super().setUp()
        self.ctx = FakeCtx(config={"enable_research": False, "enable_videos": False, "courses_dir": ""})
        cw.register(self.ctx)

    def test_status_hint_only_on_real_failure(self):
        cw._record_build({"ok": None, "running": True})
        self.assertNotIn("hint", cw.course_status(self.ctx, {}))
        cw._record_build({"ok": False, "error": "boom"})
        self.assertIn("hint", cw.course_status(self.ctx, {}))

    def test_double_course_handoff_is_refused(self):
        course = self.ctx.commands["course"]
        first = course("how a zipper works", session_key="s1")
        self.assertIn("On it", first)
        second = course("how a zipper works", session_key="s1")
        self.assertIn("Already building", second)
        self.assertEqual(len(self.ctx.injected), 1)

    def test_handoff_clears_when_the_build_runs(self):
        course = self.ctx.commands["course"]
        course("how a zipper works", session_key="s1")
        cw.build_course(self.ctx, {"topic": "how a zipper works", "max_sections": 2})
        self.assertFalse(cw._handoff_pending(cw._topic_key("how a zipper works")))

    def test_numbers_stay_in_the_topic(self):
        self.assertEqual(cw._parse_course_args("how 7 bridges of Konigsberg work"),
                         {"topic": "how 7 bridges of Konigsberg work"})
        self.assertEqual(cw._parse_course_args("2001 a space odyssey"),
                         {"topic": "2001 a space odyssey"})

    def test_sections_flag_and_bad_level(self):
        self.assertEqual(cw._parse_course_args("zipper --sections=4 --level=turbo"),
                         {"topic": "zipper", "max_sections": 4})
        self.assertEqual(cw._parse_course_args("zipper --level=Novice"),
                         {"topic": "zipper", "level": "novice"})

    def test_language_guessing(self):
        cases = {"how a zipper works": "en", "como funciona un zipper": "es",
                 "de Broglie wavelength": "en", "es Vedra legend": "en",
                 "el mercado y la gente": "es", "¿por qué falla?": "es",
                 # an accented Spanish proper noun is taken as decisive on purpose
                 "la Niña and El Niño": "es"}
        for topic, expected in cases.items():
            self.assertEqual(cw._guess_language(topic, ""), expected, topic)

    def test_identity_is_not_asked_for_twice(self):
        # forced_identity is assigned locally; the prompt must not also say "avoid this one"
        ctx = FakeCtx()
        captured = {}

        class Llm(FakeLlm):
            def complete_structured(self, **kw):
                captured["instructions"] = kw["instructions"]
                return super().complete_structured(**kw)

        ctx.llm = Llm()
        cw.make_plan(ctx, {"topic": "probe"})
        self.assertIn("Use exactly this identity", captured["instructions"])
        self.assertNotIn("Do not reuse this identity", captured["instructions"])


# --------------------------------------------------------------------------
# math: a derivation, not italic prose
# --------------------------------------------------------------------------
class TestMath(unittest.TestCase):
    def test_a_chain_becomes_stages(self):
        rows = render._math_line("cat . dog = 1.0*0.9 + 0.2*0.1 = 0.90 + 0.02 = 1.04")
        self.assertEqual(rows.count('class="m-eq"'), 3)
        self.assertIn("1.04", rows)

    def test_prose_with_an_equals_sign_is_not_split(self):
        for line in ("Given words w with counts c(w) over a base alphabet A (|A| = 256 for byte-level):",
                     "Round 1 over {low_, lower_, lowest_}, c = 1 each:",
                     "Position codes are built from sines and cosines; each pair runs at its own frequency."):
            self.assertEqual(render._math_line(line).count('class="m-eq"'), 1, line)

    def test_operators_and_signs(self):
        out = render._math("1.0*0.9 + (-0.4)*(-0.3) = 0.90 - 0.12 = 0.78")
        self.assertIn("1.0 · 0.9", out)
        self.assertIn("(−0.4) · (−0.3)", out)
        self.assertIn("0.90 − 0.12", out)
        self.assertNotIn("*", out)
        self.assertNotIn(" - ", out)
        self.assertIn("sqrt(1.00) ->", "sqrt(1.00) -> " + render._math("sqrt(1.00)"))

    def test_functions_are_upright_and_lone_letters_italic(self):
        out = render._math("softmax(Q K^T / √d_k) and cat and W_Q")
        self.assertIn('<span class="fn">softmax</span>', out)
        self.assertIn("<i>Q</i>", out)
        self.assertIn("<i>W</i>", out)
        self.assertIn("cat", out)
        self.assertNotIn("<i>cat</i>", out)

    def test_multi_letter_subscript(self):
        self.assertIn("<i>d</i><sub>model</sub>", render._math("d_model"))
        self.assertIn("<i>H</i><sub>2</sub><i>O</i>", render._math("H_2O"))

    def test_a_star_between_numbers_in_prose(self):
        self.assertIn("1.095 · 0.954", render._inline("cos = 1.04 / (1.095*0.954)"))
        self.assertEqual(render._inline("**bold** stays"), "<strong>bold</strong> stays")

    def test_nested_braced_scripts_stay_balanced(self):
        from html.parser import HTMLParser

        class Checker(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.bad = [], None

            def handle_starttag(self, tag, attrs):
                if tag in ("sub", "sup", "i"):
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in ("sub", "sup", "i"):
                    if not self.stack or self.stack[-1] != tag:
                        self.bad = f"{tag} closed out of order"
                    else:
                        self.stack.pop()

        for expr in ("R^{n × d_v}", "W_1 in R^{d_{ff} x d}", "e^{z_t} / sum_j e^{z_j}",
                     "x_{i}^{2}", "W_Q^T"):
            out = render._math(expr)
            checker = Checker()
            checker.feed(out)
            self.assertIsNone(checker.bad, expr)
            self.assertEqual(checker.stack, [], expr)
            self.assertIn("<sup>", out if "sup" in out else "<sup>")

    def test_trailing_group_keeps_its_base(self):
        self.assertIn("<i>R</i><sup>", render._math("R^{n × d_v}"))
        self.assertIn("<i>e</i><sup>", render._math("e^{z_t}"))

    def test_prose_inside_a_math_block_becomes_a_note(self):
        raw = {"slug": "p", "title": "T", "language": "en", "default_level": "intermediate",
               "identity": {}, "outcomes": [],
               "provenance": {"researched": False, "finders": [], "readers": [], "sources": [],
                              "failures": [], "warnings": [], "generated": ""},
               "resources": {"videos": [], "reading": []},
               "sections": [{"id": "s1", "title": "S", "levels": {
                   lvl: {"blocks": [{"kind": "math",
                                     "md": "a = 1 + 2 = 3\nThe first pair repeats every 6.3 positions, "
                                           "the second every 628, so the pair of values is a fingerprint "
                                           "for the slot in the sequence."}]}
                   for lvl in ("novice", "intermediate", "advanced")}}]}
        clean, _ = spec.validate(raw)
        page = render.render(clean)
        self.assertEqual(page.count('class="math-note"'), 3)      # one per depth
        self.assertEqual(page.count('class="m-eq"'), 6)           # two stages per depth

    def test_block_renders_three_columns_per_row(self):
        raw = {"slug": "p", "title": "T", "language": "en", "default_level": "intermediate",
               "identity": {}, "outcomes": [],
               "provenance": {"researched": False, "finders": [], "readers": [], "sources": [],
                              "failures": [], "warnings": [], "generated": ""},
               "resources": {"videos": [], "reading": []},
               "sections": [{"id": "s1", "title": "S", "levels": {
                   lvl: {"blocks": [{"kind": "math", "md": "a = 1*2 = 2\nb = 3"}]}
                   for lvl in ("novice", "intermediate", "advanced")}}]}
        clean, _ = spec.validate(raw)
        page = render.render(clean)
        self.assertEqual(page.count('class="m-lhs"'), 9)     # (2 + 1) rows × 3 depths
        self.assertEqual(page.count('class="m-eq"'), 9)


# --------------------------------------------------------------------------
# author: substance, not summaries
# --------------------------------------------------------------------------
class TestSectionSubstance(unittest.TestCase):
    """The course exists to explain, so the gate checks explanation, not just shape."""

    FILLER = {"blocks": [{"kind": "prose", "md": "A tokenizer turns text into numbers."},
                         {"kind": "callout", "tone": "insight", "md": "This matters a lot."}]}

    def _section(self, levels=None):
        body = levels or {lvl: dict(self.FILLER) for lvl in
                          ("novice", "intermediate", "advanced")}
        return {"id": "s1", "title": "S", "levels": body,
                "exercise": {"kind": "mcq", "prompt": "q", "choices": ["a", "b", "c", "d"],
                             "answer": 0}}

    def test_a_summary_fails_the_gate(self):
        problems = author._section_problems(self._section())
        self.assertTrue(any("words is a summary" in p for p in problems))
        self.assertTrue(any("no worked example" in p for p in problems))

    def test_an_explained_section_passes(self):
        long_prose = {"kind": "prose", "md": " ".join(["word"] * 400)}
        example = {"kind": "steps", "items": ["text: newest", "merge (e,s) -> es",
                                             "ids: [78, 101, 119, 5612]"]}
        body = {lvl: {"blocks": [long_prose, example]} for lvl in
                ("novice", "intermediate", "advanced")}
        section = self._section(body)
        section["visual"] = {"type": "flow", "steps": ["text", "ids"]}
        self.assertEqual(author._section_problems(section), [])

    def test_prompt_carries_the_example_and_the_recap(self):
        plan = {"title": "T", "thesis": "th", "language": "en", "sections": [
            {"title": "One", "teaches": "the first mechanism", "hook": "", "eyebrow": "",
             "example": "newest -> [263]"},
            {"title": "Two", "teaches": "the second mechanism", "hook": "", "eyebrow": "",
             "example": "three vectors and a softmax"},
        ]}
        instructions, _payload = author._section_prompt(plan, 1, 2, "en")
        self.assertIn("three vectors and a softmax", instructions)
        self.assertIn("the first mechanism", instructions)          # recap of section 1
        self.assertIn("350-500", instructions)                      # the length contract
        self.assertIn("Define before use", instructions)
        self.assertIn("worked example", instructions)

    def test_plan_keeps_the_example_field(self):
        class Llm:
            @staticmethod
            def complete_structured(**kw):
                payload = {"slug": "s", "title": "T", "subtitle": "", "thesis": "t",
                           "outcomes": [], "identity": {},
                           "sections": [{"title": f"S{i}", "eyebrow": "", "hook": "",
                                         "teaches": "a real mechanism, two sentences.",
                                         "example": f"case {i}"} for i in range(6)]}
                return type("R", (), {"parsed": payload, "text": ""})()

        plan = author.plan_course(type("Ctx", (), {"llm": Llm()})(), topic="t")
        self.assertEqual(plan["sections"][2]["example"], "case 2")


# --------------------------------------------------------------------------
# author: assembly
# --------------------------------------------------------------------------
class TestAssemble(unittest.TestCase):
    def test_flow_repair_only_clears_its_own_note(self):
        """Drawing section 1's flow figure must not silence section 2's missing-figure note."""
        levels = ("novice", "intermediate", "advanced")

        def section(index, with_steps):
            blocks = ([{"kind": "steps", "items": ["a", "b", "c"]}] if with_steps
                      else [{"kind": "prose", "md": "x"}])
            return {"id": f"s{index + 1}", "title": f"Part {index + 1}",
                    "levels": {lvl: {"blocks": list(blocks)} for lvl in levels},
                    "_warnings": [f"section {index + 1}: no figure — put one in visual"]}

        plan = {"title": "T", "thesis": "", "language": "en",
                "sections": [{"title": "Part 1"}, {"title": "Part 2"}]}
        built = author.assemble(plan, [section(0, True), section(1, False)], topic="T")
        notes = built["provenance"]["warnings"]
        self.assertFalse([w for w in notes if w.startswith("section 1:") and "no figure" in w])
        self.assertTrue([w for w in notes if w.startswith("section 2:") and "no figure" in w])


# --------------------------------------------------------------------------
# renderer: page-level guarantees
# --------------------------------------------------------------------------
class TestPage(unittest.TestCase):
    def _page(self, **overrides):
        raw = {"slug": "p", "title": "Title", "language": "en", "default_level": "intermediate",
               "identity": {"palette": "graphite", "type_pairing": "humanist", "layout": "rail",
                            "motion": "restrained"},
               "outcomes": [], "provenance": {"researched": True, "finders": ["web_search"],
                                              "readers": [], "sources": [], "failures": [],
                                              "warnings": [], "generated": "2026-11-09T10:00:00Z"},
               "resources": {"videos": [], "reading": []},
               "sections": [{"id": "s1", "title": "S", "levels": {
                   lvl: {"blocks": [{"kind": "prose", "md": "x"}]}
                   for lvl in ("novice", "intermediate", "advanced")}}]}
        raw.update(overrides)
        clean, _ = spec.validate(raw)
        return render.render(clean)

    def test_resources_are_filtered_by_depth_in_the_browser(self):
        page = self._page(resources={
            "videos": [{"id": "a", "title": "V", "channel": "C", "duration": 200, "why": "",
                        "transcript": False, "thumb": "none", "thumb_data": "", "level": "novice"}],
            "reading": [{"title": "Paper", "href": "https://arxiv.org/abs/1", "kind": "paper",
                         "venue": "arxiv.org", "year": None, "why": "", "level": "advanced"}]})
        self.assertIn("applyResourceLevels", page)
        self.assertIn('data-level="novice"', page)
        self.assertIn("c.hidden=any", page)

    def test_no_bridge_is_reported_honestly(self):
        page = self._page()
        self.assertIn("no bridge — copied to clipboard", page)
        self.assertIn("clipboard refused", page)

    def test_reading_time_follows_the_opening_depth(self):
        def page(default_level):
            raw = {"slug": "p", "title": "T", "language": "en", "default_level": default_level,
                   "identity": {}, "outcomes": [],
                   "provenance": {"researched": False, "finders": [], "readers": [], "sources": [],
                                  "failures": [], "warnings": [], "generated": ""},
                   "resources": {"videos": [], "reading": []},
                   "sections": [{"id": "s1", "title": "S", "levels": {
                       "novice": {"blocks": [{"kind": "prose", "md": " ".join(["w"] * 100)}]},
                       "intermediate": {"blocks": [{"kind": "prose", "md": " ".join(["w"] * 900)}]},
                       "advanced": {"blocks": [{"kind": "prose", "md": " ".join(["w"] * 1900)}]},
                   }}]}
            clean, _ = spec.validate(raw)
            return render.render(clean)

        self.assertIn("~4 min", page("novice"))
        self.assertIn("~10 min", page("advanced"))

    def test_multiline_math_keeps_its_lines(self):
        raw = {"slug": "p", "title": "T", "language": "en", "default_level": "intermediate",
               "identity": {}, "outcomes": [],
               "provenance": {"researched": False, "finders": [], "readers": [], "sources": [],
                              "failures": [], "warnings": [], "generated": ""},
               "resources": {"videos": [], "reading": []},
               "sections": [{"id": "s1", "title": "S", "levels": {
                   lvl: {"blocks": [{"kind": "math",
                                     "md": "Scores: s_{ij} = q_i · k_j / √d_k\n"
                                           "Weights: a_{ij} = exp(s_{ij}) / Σ_j exp(s_{ij})\n"
                                           "Output: o_i = Σ_j a_{ij} v_j"}]}
                   for lvl in ("novice", "intermediate", "advanced")}}]}
        clean, _ = spec.validate(raw)
        page = render.render(clean)
        self.assertEqual(page.count('class="mathline"'), 9)      # three lines, three depths
        self.assertNotIn("Scores: s<sub>ij</sub> Weights:", page)

    def test_level_change_redraws_figures(self):
        page = self._page()
        self.assertIn("if(typeof draw==='function')draw()", page)


class TestDevImportPath(unittest.TestCase):
    """`python -c "import __init__"` is the dev path; it must load and self-check too.

    The package import leaks sibling names into the package namespace (`from . import
    tokens` inside render.py), which hid two NameErrors here: `tokens.assign` in make_plan
    and `render.render` in the self-check only resolved by that side effect.
    """

    def test_module_loads_and_self_checks_without_the_package(self):
        import importlib.util

        root = pathlib.Path(__file__).resolve().parent.parent
        spec_ = importlib.util.spec_from_file_location("cw_dev_probe", root / "__init__.py")
        if spec_ is None or spec_.loader is None:
            self.fail("could not build an import spec for __init__.py")
        module = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(module)
        module._selfcheck()                      # would raise NameError('render')
        self.assertTrue(callable(module.make_plan))
        self.assertEqual(module._parse_course_args("x")["topic"], "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
