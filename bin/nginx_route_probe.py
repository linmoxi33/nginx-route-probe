#!/usr/bin/env python3
"""nginx-route-probe: resolve which server/location nginx would use for a URI.

Offline, zero-dependency reimplementation of the nginx request-routing decision
(server_name selection + location matching + rewrite/try_files internal redirects).
It never starts nginx and never opens a socket.

Exit codes:
  0  resolved, no findings at or above --fail-on
  1  resolved, but findings at or above --fail-on
  2  usage error or unparseable config
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

VERSION = "1.0.0"
MAX_CYCLES = 10


# --------------------------------------------------------------------------
# tokenizer / parser
# --------------------------------------------------------------------------
class ConfigError(Exception):
    pass


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in "{};":
            tokens.append(c)
            i += 1
            continue
        if c in "\"'":
            quote = c
            i += 1
            buf: list[str] = []
            closed = False
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == quote:
                    closed = True
                    i += 1
                    break
                buf.append(text[i])
                i += 1
            if not closed:
                raise ConfigError(f"unterminated quoted string at offset {i}")
            tokens.append("".join(buf))
            continue
        buf = []
        while i < n and text[i] not in " \t\r\n{};#":
            if text[i] == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            buf.append(text[i])
            i += 1
        tokens.append("".join(buf))
    return tokens


@dataclass
class Directive:
    name: str
    args: list[str]
    children: list["Directive"] | None = None
    line: int = 0


def parse(tokens: list[str], start: int = 0) -> tuple[list[Directive], int]:
    items: list[Directive] = []
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if tok == "}":
            return items, i + 1
        if tok == "{":
            raise ConfigError("unexpected '{'")
        name = tok
        i += 1
        args: list[str] = []
        while i < len(tokens) and tokens[i] not in "{};":
            args.append(tokens[i])
            i += 1
        if i >= len(tokens):
            raise ConfigError(f"unterminated directive {name!r} (missing ';' or '}}')")
        if tokens[i] == "{":
            children, i = parse(tokens, i + 1)
            items.append(Directive(name, args, children))
        elif tokens[i] == "}":
            raise ConfigError(f"missing ';' after {name!r}")
        else:  # ';'
            i += 1
            items.append(Directive(name, args, None))
    return items, i


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------
@dataclass
class Location:
    name: str
    modifier: str  # '', '=', '^~', '~', '~*', '@'
    server: "Server | None" = None
    parent: "Location | None" = None
    children: list["Location"] = field(default_factory=list)
    directives: list[Directive] = field(default_factory=list)
    index: int = 0

    @property
    def path(self) -> str:
        parts = []
        node: Location | None = self
        while node is not None:
            parts.append(node.modifier_join())
            node = node.parent
        return "".join(reversed(parts))

    def modifier_join(self) -> str:
        if self.modifier in ("", "^~"):
            return self.name
        return f"{self.modifier} {self.name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "modifier": self.modifier,
            "index": self.index,
            "nested": [c.as_dict() for c in self.children],
        }


@dataclass
class Server:
    listen: list[dict[str, Any]] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    directives: list[Directive] = field(default_factory=list)
    index: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "server_name": self.names,
            "listen": self.listen,
            "locations": [loc.as_dict() for loc in self.locations],
        }


@dataclass
class HttpServer:
    servers: list[Server] = field(default_factory=list)
    directives: list[Directive] = field(default_factory=list)


# --------------------------------------------------------------------------
# builder
# --------------------------------------------------------------------------
def _listen(entry: Directive) -> dict[str, Any]:
    args = list(entry.args)
    if not args:
        raise ConfigError("listen requires an argument")
    port = 80
    address = args[0]
    if address.startswith("unix:"):
        return {"address": address, "port": None, "default_server": "default_server" in args[1:]}
    if address.isdigit():
        port = int(address)
        address = "*"
    elif ":" in address:
        host, _, maybe_port = address.rpartition(":")
        if maybe_port.isdigit():
            port = int(maybe_port)
            address = host
    elif "." not in address:
        raise ConfigError(f"cannot understand listen address {args[0]!r}")
    return {
        "address": address,
        "port": port,
        "default_server": "default_server" in args[1:],
        "ssl": "ssl" in args[1:],
    }


def _build_location(entry: Directive, server: Server, parent: Location | None, counter: list[int]) -> Location:
    if not entry.args:
        raise ConfigError("location requires a pattern")
    args = entry.args
    modifier = ""
    if args[0] in ("=", "^~", "~", "~*"):
        if len(args) < 2:
            raise ConfigError(f"location {args[0]!r} requires a pattern")
        modifier, name = args[0], args[1]
    elif args[0].startswith("@"):
        modifier, name = "@", args[0]
    else:
        name = args[0]
    loc = Location(name=name, modifier=modifier, server=server, parent=parent, index=counter[0])
    counter[0] += 1
    for child in entry.children or []:
        if child.name == "location":
            loc.children.append(_build_location(child, server, loc, counter))
        else:
            loc.directives.append(child)
    return loc


def build(ast: list[Directive]) -> HttpServer:
    http = HttpServer()
    http_blocks = [d for d in ast if d.name == "http"]
    if not http_blocks:
        # accept a bare server{} file too
        http_blocks = [Directive("http", [], ast)]
    for http_block in http_blocks:
        for entry in http_block.children or []:
            if entry.name == "server":
                srv = Server(directives=list(entry.children or []))
                srv.index = len(http.servers)
                counter = [0]
                for d in entry.children or []:
                    if d.name == "listen":
                        srv.listen.append(_listen(d))
                    elif d.name == "server_name":
                        srv.names.extend(d.args)
                    elif d.name == "location":
                        srv.locations.append(_build_location(d, srv, None, counter))
                if not srv.listen:
                    srv.listen.append({"address": "*", "port": 80, "default_server": False, "ssl": False})
                http.servers.append(srv)
            else:
                http.directives.append(entry)
    if not http.servers:
        raise ConfigError("config contains no http { server { ... } } block")
    return http


# --------------------------------------------------------------------------
# server_name matching (nginx rules)
# --------------------------------------------------------------------------
def _name_matches(pattern: str, host: str) -> bool:
    pattern = pattern.lower()
    host = host.lower()
    if pattern == host:
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host.count(".") >= pattern.count(".")
    if pattern.startswith("."):
        return host.endswith(pattern) or host == pattern[1:]
    if pattern.endswith(".*"):
        return host.startswith(pattern[:-1])
    return False


def select_server(http: HttpServer, host: str, port: int | None) -> tuple[Server, list[dict[str, Any]]]:
    trace: list[dict[str, Any]] = []
    candidates = [s for s in http.servers if any(l["port"] in (None, port) for l in s.listen)] or http.servers
    trace.append({"step": "listen_filter", "port": port, "candidates": [s.index for s in candidates]})

    exact: Server | None = None
    wildcard: Server | None = None
    regex: Server | None = None
    for srv in candidates:
        for name in srv.names:
            if name.startswith("~"):
                try:
                    if re.search(name[1:], host, re.IGNORECASE):
                        regex = regex or srv
                except re.error:
                    continue
            elif name.startswith("*") or name.startswith(".") or name.endswith(".*"):
                if _name_matches(name, host):
                    wildcard = wildcard or srv
                    trace.append({"step": "server_name_wildcard", "pattern": name, "server": srv.index})
            elif _name_matches(name, host):
                exact = exact or srv
                trace.append({"step": "server_name_exact", "pattern": name, "server": srv.index})

    chosen = exact or wildcard or regex
    if chosen is not None:
        trace.append({"step": "server_selected", "server": chosen.index, "how": "server_name"})
        return chosen, trace
    default = next((s for s in candidates if any(l["default_server"] for l in s.listen)), None)
    chosen = default or candidates[0]
    trace.append(
        {
            "step": "server_selected",
            "server": chosen.index,
            "how": "default_server" if default is not None else "first_server_fallback",
        }
    )
    return chosen, trace


# --------------------------------------------------------------------------
# location matching (nginx rules)
# --------------------------------------------------------------------------
def _regex(loc: Location) -> re.Pattern[str]:
    flags = re.IGNORECASE if loc.modifier == "~*" else 0
    return re.compile(loc.name, flags)


def find_location(locations: list[Location], uri: str, trace: list[dict[str, Any]]) -> Location | None:
    for loc in locations:
        if loc.modifier == "=" and loc.name == uri:
            trace.append({"step": "exact_match", "location": loc.path})
            return loc

    prefix: Location | None = None
    for loc in locations:
        if loc.modifier in ("", "^~") and uri.startswith(loc.name):
            if prefix is None or len(loc.name) > len(prefix.name):
                prefix = loc
    if prefix is not None:
        trace.append({"step": "longest_prefix", "location": prefix.path, "len": len(prefix.name)})
        if prefix.children:
            inner = find_location(prefix.children, uri, trace)
            if inner is not None:
                return inner
        if prefix.modifier == "^~":
            trace.append({"step": "caret_tilde_stops_regex", "location": prefix.path})
            return prefix

    for loc in locations:
        if loc.modifier in ("~", "~*"):
            try:
                if _regex(loc).search(uri):
                    trace.append({"step": "regex_match", "location": loc.path, "order": loc.index})
                    return loc
            except re.error as exc:
                trace.append({"step": "regex_invalid", "location": loc.path, "error": str(exc)})

    if prefix is not None:
        trace.append({"step": "fallback_to_prefix", "location": prefix.path})
    return prefix


def _named(locations: list[Location], name: str) -> Location | None:
    for loc in locations:
        if loc.modifier == "@" and loc.name == name:
            return loc
        found = _named(loc.children, name)
        if found is not None:
            return found
    return None


# --------------------------------------------------------------------------
# resolution with rewrite / try_files internal redirects
# --------------------------------------------------------------------------
def _apply_rewrites(loc: Location, uri: str) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Return (new_uri, action, trace). action is 'last'|'break'|'redirect'|None."""
    trace: list[dict[str, Any]] = []
    for d in loc.directives:
        if d.name != "rewrite" or len(d.args) < 2:
            continue
        flags = d.args[2:]
        try:
            pattern = re.compile(d.args[0])
        except re.error:
            trace.append({"step": "rewrite_invalid_regex", "pattern": d.args[0]})
            continue
        if not pattern.search(uri):
            continue
        replacement = d.args[1]
        redirect = replacement.startswith(("http://", "https://", "$scheme"))
        new_uri = pattern.sub(replacement, uri, count=1)
        if redirect:
            trace.append({"step": "rewrite_external", "pattern": d.args[0], "to": new_uri})
            return new_uri, "redirect", trace
        if "last" in flags:
            trace.append({"step": "rewrite_last", "from": uri, "to": new_uri})
            return new_uri, "last", trace
        if "break" in flags:
            trace.append({"step": "rewrite_break", "from": uri, "to": new_uri})
            return new_uri, "break", trace
        trace.append({"step": "rewrite_continue", "from": uri, "to": new_uri})
        uri = new_uri
    return uri, None, trace


def _try_files_named(loc: Location) -> str | None:
    for d in loc.directives:
        if d.name == "try_files" and d.args:
            last = d.args[-1]
            if last.startswith("@"):
                return last
    return None


def resolve(server: Server, uri: str, trace: list[dict[str, Any]]) -> dict[str, Any]:
    current = uri
    location: Location | None = None
    for cycle in range(MAX_CYCLES + 1):
        if cycle == MAX_CYCLES:
            trace.append({"step": "rewrite_limit_exceeded", "cycles": MAX_CYCLES})
            return {"uri": current, "location": location, "status": 500, "cycle": cycle, "loop": True}
        trace.append({"step": "match_cycle", "cycle": cycle, "uri": current})
        location = find_location(server.locations, current, trace)
        if location is None:
            trace.append({"step": "no_location", "uri": current})
            return {"uri": current, "location": None, "status": 404, "cycle": cycle, "loop": False}
        named = _try_files_named(location)
        new_uri, action, sub = _apply_rewrites(location, current)
        trace.extend(sub)
        if action == "redirect":
            return {"uri": new_uri, "location": location, "status": 301, "cycle": cycle, "loop": False}
        if action == "last" or (new_uri != current and action is None):
            current = new_uri
            continue
        if action is None and named is not None:
            target = _named(server.locations, named)
            if target is not None:
                trace.append({"step": "try_files_named_redirect", "to": named})
                return {"uri": current, "location": target, "status": 200, "cycle": cycle, "loop": False}
        return {"uri": current, "location": location, "status": 200, "cycle": cycle, "loop": False}
    return {"uri": current, "location": location, "status": 500, "cycle": MAX_CYCLES, "loop": True}


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------
def lint(http: HttpServer) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def add(level: str, code: str, message: str, **extra: Any) -> None:
        findings.append({"level": level, "code": code, "message": message, **extra})

    seen_names: dict[tuple[int, str], int] = {}
    for srv in http.servers:
        ports = {l["port"] for l in srv.listen}
        if not srv.names:
            add("warning", "no_server_name", f"server #{srv.index} has no server_name and only serves as a default", server=srv.index)
        for name in srv.names:
            if name.startswith("~"):
                continue
            for port in ports:
                key = (port, name.lower())
                if key in seen_names and seen_names[key] != srv.index:
                    add(
                        "warning",
                        "duplicate_server_name",
                        f"server_name {name!r} on port {port} is already claimed by server #{seen_names[key]}; this server is unreachable for that host",
                        server=srv.index,
                        conflict_with=seen_names[key],
                    )
                else:
                    seen_names[key] = srv.index
        _lint_locations(srv, srv.locations, add)

    return findings


def _lint_locations(server: Server, locations: list[Location], add) -> None:
    seen: dict[tuple[str, str], Location] = {}
    for loc in locations:
        key = (loc.modifier, loc.name)
        if key in seen:
            add(
                "error",
                "duplicate_location",
                f"duplicate location {loc.modifier_join()!r} in server #{server.index}; the second block at index {loc.index} is unreachable",
                server=server.index,
                location=loc.path,
                shadowed_by=seen[key].path,
            )
        else:
            seen[key] = loc
        if loc.modifier in ("~", "~*"):
            try:
                re.compile(loc.name)
            except re.error as exc:
                add("error", "invalid_regex", f"location {loc.name!r} is not a valid regex: {exc}", server=server.index, location=loc.path)
            if loc.children:
                add("warning", "nested_regex", f"nested locations inside regex location {loc.path!r} are matched only after the parent regex matches", server=server.index, location=loc.path)
        if loc.modifier == "=" and loc.children:
            add("error", "nested_in_exact", f"'=' location {loc.path!r} cannot contain nested locations", server=server.index, location=loc.path)
        _lint_locations(server, loc.children, add)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _read_requests(path: str) -> list[tuple[str, str, int | None]]:
    requests: list[tuple[str, str, int | None]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) == 1:
                requests.append(("", parts[0], None))
            elif len(parts) == 2:
                requests.append((parts[0], parts[1], None))
            elif len(parts) == 3:
                try:
                    port = int(parts[2])
                except ValueError:
                    raise ConfigError(f"{path}:{lineno}: port must be an integer")
                requests.append((parts[0], parts[1], port))
            else:
                raise ConfigError(f"{path}:{lineno}: expected 'host uri [port]'")
    return requests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nginx-route-probe",
        description="Resolve which server/location nginx would use for a URI, without starting nginx.",
    )
    parser.add_argument("--config", "-c", help="path to nginx.conf")
    parser.add_argument("--request", "-r", action="append", default=[], metavar="URI", help="URI to resolve (repeatable)")
    parser.add_argument("--host", default="", help="Host header for --request (default: '')")
    parser.add_argument("--port", type=int, default=80, help="listen port for --request (default: 80)")
    parser.add_argument("--requests", metavar="FILE", help="file with 'host uri [port]' per line")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--fail-on", choices=["error", "warning", "never"], default="error", help="lowest finding level that sets exit code 1")
    parser.add_argument("--print-config", action="store_true", help="print the parsed server/location model and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args(argv)

    if not args.config:
        print("nginx-route-probe: --config is required", file=sys.stderr)
        return 2
    try:
        with open(args.config, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"nginx-route-probe: cannot read config: {exc}", file=sys.stderr)
        return 2

    try:
        ast, _ = parse(tokenize(text))
        http = build(ast)
        requests: list[tuple[str, str, int | None]] = []
        if args.requests:
            requests.extend(_read_requests(args.requests))
        requests.extend((args.host, uri, args.port) for uri in args.request)
    except ConfigError as exc:
        print(f"nginx-route-probe: config error: {exc}", file=sys.stderr)
        return 2

    if not requests:
        print("nginx-route-probe: no request given (use --request URI or --requests FILE)", file=sys.stderr)
        return 2

    findings = lint(http)
    results = []
    for host, uri, port in requests:
        trace: list[dict[str, Any]] = []
        server, st = select_server(http, host, port)
        trace.extend(st)
        outcome = resolve(server, uri, trace)
        results.append(
            {
                "request": {"host": host, "uri": uri, "port": port},
                "server": {"index": server.index, "server_name": server.names},
                "location": None if outcome["location"] is None else outcome["location"].as_dict(),
                "matched_path": None if outcome["location"] is None else outcome["location"].path,
                "final_uri": outcome["uri"],
                "status": outcome["status"],
                "loop_guard": outcome["loop"],
                "trace": trace,
            }
        )

    report = {
        "tool": "nginx-route-probe",
        "version": VERSION,
        "config": args.config,
        "servers": [s.as_dict() for s in http.servers],
        "findings": findings,
        "results": results,
        "summary": {
            "requests": len(results),
            "findings": len(findings),
            "errors": sum(1 for f in findings if f["level"] == "error"),
            "warnings": sum(1 for f in findings if f["level"] == "warning"),
            "rewrite_loops": sum(1 for r in results if r["loop_guard"]),
        },
    }

    if args.print_config:
        print(json.dumps({"servers": report["servers"]}, indent=2, ensure_ascii=False))
        return 0

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for res in results:
            req = res["request"]
            print(f"{req['host'] or '*'} {req['uri']}  ->  server#{res['server']['index']} "
                  f"{res['matched_path'] or '<no location>'}  status={res['status']}"
                  + ("  [rewrite loop guard]" if res["loop_guard"] else ""))
        for f in findings:
            print(f"[{f['level']}] {f['code']}: {f['message']}")

    level_rank = {"warning": 1, "error": 2}
    threshold = {"error": 2, "warning": 1, "never": 99}[args.fail_on]
    worst = max((level_rank.get(f["level"], 0) for f in findings), default=0)
    return 1 if worst >= threshold else 0


if __name__ == "__main__":
    sys.exit(main())
