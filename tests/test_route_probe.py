#!/usr/bin/env python3
"""Behavioural tests for nginx-route-probe (stdlib unittest only)."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "nginx_route_probe.py")

spec = importlib.util.spec_from_file_location("nr", BIN)
nr = importlib.util.module_from_spec(spec)
sys.modules["nr"] = nr
spec.loader.exec_module(nr)


def load(text):
    ast, _ = nr.parse(nr.tokenize(text))
    return nr.build(ast)


BASE = """
http {
  server {
    listen 80 default_server;
    server_name example.com www.example.com;
    location = /exact { return 200; }
    location ^~ /api/ { return 200; }
    location /api/v1/ { return 200; }
    location ~ \\.php$ { return 200; }
    location ~* \\.(png|jpg)$ { return 200; }
    location /static/ { return 200; }
    location / { return 200; }
  }
  server {
    listen 80;
    server_name api.example.com;
    location / { rewrite ^/old/(.*)$ /new/$1 last; }
    location /new/ { return 200; }
    location /loop/ { rewrite ^ /loop/ last; }
  }
}
"""


class TokenizerTests(unittest.TestCase):
    def test_comments_are_dropped(self):
        self.assertEqual(nr.tokenize("# hi\nserver {}\n"), ["server", "{", "}"])

    def test_quoted_string_keeps_spaces_and_braces(self):
        self.assertEqual(nr.tokenize('x "a b{c}";'), ["x", "a b{c}", ";"])

    def test_hash_inside_quotes_is_literal(self):
        self.assertEqual(nr.tokenize('x "a#b";'), ["x", "a#b", ";"])

    def test_unterminated_quote_raises(self):
        with self.assertRaises(nr.ConfigError):
            nr.tokenize('x "abc;')


class ParserTests(unittest.TestCase):
    def test_nested_block(self):
        ast, _ = nr.parse(nr.tokenize("http { server { listen 80; } }"))
        self.assertEqual(ast[0].name, "http")
        self.assertEqual(ast[0].children[0].name, "server")
        self.assertIsNone(ast[0].children[0].children[0].children)

    def test_missing_semicolon_raises(self):
        with self.assertRaises(nr.ConfigError):
            nr.parse(nr.tokenize("http { server { listen 80 } }"))

    def test_listen_parses_port_and_default(self):
        http = load("http { server { listen 8080 default_server; } }")
        self.assertEqual(http.servers[0].listen[0]["port"], 8080)
        self.assertTrue(http.servers[0].listen[0]["default_server"])

    def test_no_server_block_raises(self):
        with self.assertRaises(nr.ConfigError):
            load("http { }")


class LocationMatchTests(unittest.TestCase):
    def setUp(self):
        self.http = load(BASE)
        self.srv = self.http.servers[0]

    def match(self, uri):
        trace = []
        return nr.find_location(self.srv.locations, uri, trace), trace

    def test_exact_beats_prefix_and_regex(self):
        loc, _ = self.match("/exact")
        self.assertEqual(loc.modifier, "=")

    def test_caret_tilde_stops_regex_for_longest_prefix(self):
        loc, trace = self.match("/api/x.php")
        self.assertEqual(loc.modifier, "^~")
        self.assertTrue(any(s["step"] == "caret_tilde_stops_regex" for s in trace))

    def test_longer_plain_prefix_beats_shorter_caret_tilde(self):
        loc, _ = self.match("/api/v1/users")
        self.assertEqual(loc.modifier, "")
        self.assertEqual(loc.name, "/api/v1/")

    def test_regex_checked_in_config_order(self):
        loc, trace = self.match("/x.php")
        self.assertEqual(loc.modifier, "~")
        self.assertTrue(any(s["step"] == "regex_match" for s in trace))

    def test_regex_is_case_insensitive_for_tilde_star(self):
        loc, _ = self.match("/IMG/LOGO.PNG")
        self.assertEqual(loc.modifier, "~*")

    def test_falls_back_to_longest_prefix_without_regex(self):
        loc, trace = self.match("/static/app.js")
        self.assertEqual(loc.name, "/static/")
        self.assertTrue(any(s["step"] == "fallback_to_prefix" for s in trace))

    def test_root_location_matches_everything(self):
        loc, _ = self.match("/unknown/path")
        self.assertEqual(loc.name, "/")

    def test_no_match_returns_none(self):
        srv = load("http { server { location /only/ { return 200; } } }").servers[0]
        self.assertIsNone(nr.find_location(srv.locations, "/other", []))


class ServerSelectionTests(unittest.TestCase):
    def setUp(self):
        self.http = load(BASE)

    def test_exact_server_name(self):
        srv, _ = nr.select_server(self.http, "api.example.com", 80)
        self.assertEqual(srv.index, 1)

    def test_default_server_for_unknown_host(self):
        srv, trace = nr.select_server(self.http, "nope.invalid", 80)
        self.assertEqual(srv.index, 0)
        self.assertEqual(trace[-1]["how"], "default_server")

    def test_wildcard_server_name(self):
        http = load("http { server { server_name *.example.com; } }")
        srv, _ = nr.select_server(http, "a.example.com", 80)
        self.assertEqual(srv.index, 0)

    def test_port_filter(self):
        http = load("http { server { listen 8080; server_name a.test; } server { listen 80; } }")
        srv, _ = nr.select_server(http, "a.test", 80)
        self.assertEqual(srv.index, 1)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.http = load(BASE)

    def resolve(self, srv_index, uri):
        trace = []
        return nr.resolve(self.http.servers[srv_index], uri, trace), trace

    def test_rewrite_last_rematches_location(self):
        out, trace = self.resolve(1, "/old/thing")
        self.assertEqual(out["location"].name, "/new/")
        self.assertTrue(any(s["step"] == "rewrite_last" for s in trace))

    def test_rewrite_loop_guard_returns_500(self):
        out, trace = self.resolve(1, "/loop/x")
        self.assertTrue(out["loop"])
        self.assertEqual(out["status"], 500)
        self.assertTrue(any(s["step"] == "rewrite_limit_exceeded" for s in trace))

    def test_try_files_named_location(self):
        http = load(
            "http { server { location / { try_files $uri @app; } location @app { return 200; } } }"
        )
        trace = []
        out = nr.resolve(http.servers[0], "/whatever", trace)
        self.assertEqual(out["location"].modifier, "@")
        self.assertTrue(any(s["step"] == "try_files_named_redirect" for s in trace))


class LintTests(unittest.TestCase):
    def test_duplicate_location_is_error(self):
        http = load("http { server { location /a/ { return 200; } location /a/ { return 204; } } }")
        findings = nr.lint(http)
        codes = [f["code"] for f in findings]
        self.assertIn("duplicate_location", codes)
        dup = next(f for f in findings if f["code"] == "duplicate_location")
        self.assertEqual(dup["level"], "error")

    def test_duplicate_server_name_is_warning(self):
        http = load("http { server { server_name a.test; } server { server_name a.test; } }")
        codes = [f["code"] for f in nr.lint(http)]
        self.assertIn("duplicate_server_name", codes)

    def test_invalid_regex_is_error(self):
        http = load("http { server { location ~ [ { return 200; } } }")
        self.assertIn("invalid_regex", [f["code"] for f in nr.lint(http)])

    def test_missing_server_name_is_warning(self):
        http = load("http { server { listen 80; } }")
        self.assertIn("no_server_name", [f["code"] for f in nr.lint(http)])

    def test_clean_config_has_no_findings(self):
        http = load("http { server { server_name a.test; location / { return 200; } } }")
        self.assertEqual(nr.lint(http), [])


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, BIN, *args], capture_output=True, text=True, cwd=ROOT
        )

    def test_exit_0_for_clean_request(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("http { server { server_name a.test; location / { return 200; } } }")
            path = fh.name
        try:
            proc = self.run_cli("--config", path, "--request", "/x", "--host", "a.test")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("/x", proc.stdout)
        finally:
            os.unlink(path)

    def test_exit_1_when_finding_present(self):
        proc = self.run_cli("--config", "examples/in/nginx.conf", "--requests", "examples/in/requests.txt")
        self.assertEqual(proc.returncode, 1)

    def test_exit_2_on_bad_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("http { server { listen 80 } }")
            path = fh.name
        try:
            proc = self.run_cli("--config", path, "--request", "/")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("config error", proc.stderr)
        finally:
            os.unlink(path)

    def test_exit_2_without_request(self):
        proc = self.run_cli("--config", "examples/in/nginx.conf")
        self.assertEqual(proc.returncode, 2)

    def test_json_output_is_parseable_and_stable(self):
        first = self.run_cli(
            "--config", "examples/in/nginx.conf", "--requests", "examples/in/requests.txt", "--json"
        )
        second = self.run_cli(
            "--config", "examples/in/nginx.conf", "--requests", "examples/in/requests.txt", "--json"
        )
        self.assertEqual(first.stdout, second.stdout)
        data = json.loads(first.stdout)
        self.assertEqual(data["summary"]["requests"], 9)
        self.assertEqual(len(data["results"]), 9)
        self.assertTrue(all("trace" in r for r in data["results"]))

    def test_fail_on_never_returns_0_despite_findings(self):
        proc = self.run_cli(
            "--config", "examples/in/nginx.conf", "--request", "/exact",
            "--host", "example.com", "--fail-on", "never",
        )
        self.assertEqual(proc.returncode, 0)

    def test_trace_explains_decision(self):
        proc = self.run_cli(
            "--config", "examples/in/nginx.conf", "--request", "/api/x.php",
            "--host", "example.com", "--json",
        )
        data = json.loads(proc.stdout)
        steps = [s["step"] for s in data["results"][0]["trace"]]
        self.assertIn("longest_prefix", steps)
        self.assertIn("caret_tilde_stops_regex", steps)


if __name__ == "__main__":
    unittest.main(verbosity=2)
