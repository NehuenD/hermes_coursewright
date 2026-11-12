"""Offline tests for outbound URL policy, child process environment and path confinement.

The HTTP client is injected with fake sockets, so no test here resolves a name or opens a
connection. The real guard's before/after loopback control is run separately in integration.
"""

import email.message
import io
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest

from tests.test_coursewright import cw, research, FakeCtx

netguard = research.netguard


class _FakeSocket:
    def __init__(self, payload):
        self.fp = io.BytesIO(payload)
        self.sent = b""

    def makefile(self, *args, **kwargs):
        return self.fp

    def sendall(self, data):
        self.sent += data

    def close(self):
        pass


def _reply(status="200 OK", body=b"ok", headers=""):
    return (f"HTTP/1.1 {status}\r\n{headers}Content-Length: {len(body)}\r\n\r\n").encode("utf-8") + body


def _public_resolver(host, port):
    return ["127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"]


def _connector(records, payloads):
    def connect(address, timeout):
        records.append((address, timeout))
        return payloads.pop(0)
    return connect


class TestNetguard(unittest.TestCase):
    def test_every_non_public_address_class_is_refused(self):
        for address in ("127.0.0.1", "127.0.0.2", "10.0.0.1", "172.16.0.1", "172.31.255.1",
                        "192.168.1.1", "169.254.169.254", "fe80::1", "::1", "100.64.0.1",
                        "100.127.255.255", "::ffff:127.0.0.1", "fc00::1", "fd12::1",
                        "0.0.0.0", "224.0.0.1"):
            self.assertFalse(netguard.is_public_ip(address), address)

    def test_real_public_addresses_are_allowed(self):
        for address in ("93.184.216.34", "1.1.1.1", "2606:2800:220:1:248:1893:25c8:1946"):
            self.assertTrue(netguard.is_public_ip(address), address)

    def test_url_scheme_and_authority_ambiguity_fail_closed(self):
        for url in ("file:///etc/passwd", "ftp://example.com/a", "gopher://example.com/a",
                    "javascript:alert(1)", "data:text/plain,hello", "/relative/path",
                    "http://" + "u:p@example.com/", "http://example.com\\@127.0.0.1/"):
            with self.subTest(url=url):
                self.assertFalse(netguard.is_safe_url(url, resolver=lambda *a: ["93.184.216.34"]))

    def test_encoded_ipv4_literal_is_still_loopback(self):
        self.assertFalse(netguard.is_safe_url("http://2130706433/"))

    def test_a_single_private_answer_out_of_many_refuses_the_name(self):
        connector = _connector([], [])
        with self.assertRaises(netguard.GuardError):
            netguard.fetch("http://example.test/",
                           resolver=lambda host, port: ["93.184.216.34", "127.0.0.1"],
                           connector=connector)

    def test_redirect_to_loopback_is_refused_before_a_second_socket(self):
        connections = []
        connector = _connector(connections, [_FakeSocket(_reply(
            "302 Found", b"", "Location: http://127.0.0.1/secret\r\n"))])
        with self.assertRaises(netguard.GuardError):
            netguard.fetch("http://example.test/start", resolver=_public_resolver,
                           connector=connector)
        self.assertEqual(len(connections), 1)

    def test_redirect_to_a_non_http_scheme_is_refused(self):
        connections = []
        connector = _connector(connections, [_FakeSocket(_reply(
            "302 Found", b"", "Location: file:///etc/passwd\r\n"))])
        with self.assertRaises(netguard.GuardError):
            netguard.fetch("http://example.test/start", resolver=_public_resolver,
                           connector=connector)
        self.assertEqual(len(connections), 1)

    def test_redirect_chain_is_bounded(self):
        connections = []
        sockets = [_FakeSocket(_reply("302 Found", b"", "Location: /next\r\n")) for _ in range(7)]
        connector = _connector(connections, sockets)
        with self.assertRaises(netguard.GuardError):
            netguard.fetch("http://example.test/start", resolver=_public_resolver,
                           connector=connector)
        self.assertEqual(len(connections), 6)      # original request plus five redirect hops

    def test_the_request_dials_the_checked_ip_and_preserves_host(self):
        connections = []
        used = _FakeSocket(_reply("200 OK", b"hello", "Content-Type: text/plain\r\n"))
        status, headers, body = netguard.fetch(
            "http://example.test/start?q=1", resolver=_public_resolver,
            connector=_connector(connections, [used]))
        self.assertEqual((status, body), (200, b"hello"))
        self.assertEqual(connections[0][0], ("93.184.216.34", 80))
        first_line = used.sent.split(b"\r\n")[0]
        self.assertEqual(first_line, b"GET /start?q=1 HTTP/1.1")
        self.assertIn(b"Host: example.test\r\n", used.sent)


class TestGuardedReader(unittest.TestCase):
    def setUp(self):
        self.real_fetch = research.netguard.fetch

    def tearDown(self):
        research.netguard.fetch = self.real_fetch

    def test_a_refused_target_does_not_reach_prose(self):
        def refuse(url, **kwargs):
            raise netguard.GuardError("private/internal target")

        research.netguard.fetch = refuse
        self.assertEqual(research.stdlib_read("http://127.0.0.1:9/"), "")

    def test_a_safe_page_keeps_the_plugins_user_agent(self):
        captured = {}
        headers = email.message.Message()
        headers["Content-Type"] = "text/html; charset=utf-8"

        def fetch(url, **kwargs):
            captured.update(kwargs)
            return 200, headers, b"<nav>chrome</nav><p>A spanner turns a nut.</p>"

        research.netguard.fetch = fetch
        text = research.stdlib_read("https://example.com/a")
        self.assertIn("A spanner turns a nut", text)
        self.assertNotIn("chrome", text)
        self.assertEqual(captured["headers"]["User-Agent"], research._UA)

    def test_a_binary_or_error_response_is_not_an_excerpt(self):
        headers = email.message.Message()
        headers["Content-Type"] = "text/html"
        research.netguard.fetch = lambda *a, **kw: (404, headers, b"not found")
        self.assertEqual(research.stdlib_read("https://example.com/missing"), "")
        headers.replace_header("Content-Type", "application/pdf")
        research.netguard.fetch = lambda *a, **kw: (200, headers, b"pdf")
        self.assertEqual(research.stdlib_read("https://example.com/file.pdf"), "")


class TestScraperUrlGuard(unittest.TestCase):
    def setUp(self):
        self.real_check = research.netguard.is_safe_url

    def tearDown(self):
        research.netguard.is_safe_url = self.real_check

    def test_an_unsafe_scrape_target_is_refused_before_http(self):
        research.netguard.is_safe_url = lambda url: False
        self.assertEqual(research._firecrawl_scrape("http://127.0.0.1/",
                                                    "http://localhost:3000"), "")

    def test_only_the_payload_url_is_guarded(self):
        # A user-configured local scraper is supposed to sit on loopback; its endpoint is not
        # the fetched target. The fake records both so this distinction cannot drift.
        posts = []

        class Response:
            status_code = 200

            def json(self):
                return {"data": {"markdown": "text"}}

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, json):
                posts.append((url, json["url"]))
                return Response()

        fake_module = types.ModuleType("httpx")
        fake_module.Client = Client
        previous = sys.modules.get("httpx")
        sys.modules["httpx"] = fake_module
        research.netguard.is_safe_url = lambda url: url.startswith("https://example.com/")
        try:
            text = research._firecrawl_scrape("https://example.com/a", "http://127.0.0.1:3000")
        finally:
            if previous is None:
                del sys.modules["httpx"]
            else:
                sys.modules["httpx"] = previous
        self.assertEqual(text, "text")
        self.assertEqual(posts, [("http://127.0.0.1:3000/v1/scrape", "https://example.com/a")])


class TestChildEnvironment(unittest.TestCase):
    def test_ytdlp_gets_a_minimal_environment_not_provider_secrets(self):
        real_run, real_which = research.subprocess.run, research.shutil.which
        previous = {key: os.environ.get(key) for key in
                    ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HERMES_TOKEN")}
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs.get("env", {})))
            return type("Proc", (), {"stdout": ""})()

        os.environ.update({"OPENAI_API_KEY": "fake-secret", "ANTHROPIC_API_KEY": "fake-secret",
                           "HERMES_TOKEN": "fake-secret"})
        research.shutil.which = lambda name: "/opt/bin/yt-dlp"
        research.subprocess.run = fake_run
        try:
            research._yt_search("testing", 3)
            env = research._child_env()
        finally:
            research.subprocess.run, research.shutil.which = real_run, real_which
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(len(calls), 1)
        for forbidden in previous:
            self.assertNotIn(forbidden, calls[0][1])
            self.assertNotIn(forbidden, env)
        self.assertIn("PATH", calls[0][1])
        self.assertEqual(calls[0][1]["HOME"], os.environ["HOME"])
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "AWS_ACCESS_KEY_ID"):
            self.assertNotIn(key, calls[0][1])


class TestConfinedJsonPaths(unittest.TestCase):
    def setUp(self):
        self.store = pathlib.Path(tempfile.mkdtemp()) / "store"
        self.outside = pathlib.Path(tempfile.mkdtemp()) / "outside"
        self.store.mkdir()
        self.outside.mkdir()
        self.spec = self.store / "courses" / "demo" / "spec.json"
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text('{"title": "known"}', encoding="utf-8")
        self.foreign = self.outside / "secrets.json"
        self.foreign.write_text('{"token": "sensitive"}', encoding="utf-8")
        self.real_store = cw._store
        cw._store = lambda: self.store

    def tearDown(self):
        cw._store = self.real_store
        shutil.rmtree(self.store, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_a_relative_course_spec_stays_working(self):
        doc, error = cw._confined_json("courses/demo/spec.json", "spec_path")
        self.assertEqual(doc, {"title": "known"})
        self.assertEqual(error, "")

    def test_an_absolute_foreign_path_is_refused_before_reading(self):
        doc, error = cw._confined_json(str(self.foreign), "spec_path")
        self.assertIsNone(doc)
        self.assertIn(str(self.store.resolve()), error)
        self.assertNotIn("sensitive", error)

    def test_dotdot_cannot_climb_out(self):
        doc, _ = cw._confined_json("../outside/secrets.json", "plan_path")
        self.assertIsNone(doc)

    def test_a_symlink_that_leaves_the_store_is_refused(self):
        link = self.store / "links.json"
        link.symlink_to(self.foreign)
        doc, error = cw._confined_json(str(link), "spec_path")
        self.assertIsNone(doc)
        self.assertIn("inside", error)

    def test_a_non_object_is_not_a_spec(self):
        bad = self.store / "array.json"
        bad.write_text("[]", encoding="utf-8")
        doc, error = cw._confined_json(str(bad), "spec_path")
        self.assertIsNone(doc)
        self.assertIn("object", error)

    def test_build_refuses_a_foreign_spec_path(self):
        result = cw._build_course(FakeCtx(), {"spec_path": str(self.foreign)})
        self.assertFalse(result["ok"])
        self.assertIn("spec_path", result["error"])
        self.assertIn(str(self.store.resolve()), result["error"])
