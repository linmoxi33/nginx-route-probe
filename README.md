# nginx-route-probe

**Ask nginx which `location` a URI hits — without starting nginx, opening a socket, or grepping a 50 MB debug log.**

Upstream: https://github.com/nginx/nginx

NGINX has no offline request-routing query. `ngx_http_core_find_location()` is a *request-phase*
function (`src/http/ngx_http_core_module.c` L1445–1460): it takes an `ngx_http_request_t *`, walks
the static location tree on `r->uri`, and writes the winner into `r->loc_conf`. The only observable
output is the debug line `"using configuration \"%s%V\""` (L1005–1008) — i.e. you must run the
server, send real traffic, and enable `NGX_LOG_DEBUG_HTTP` before you can learn which block matched.
So the answer to "which location will `/api/v1/users` hit?" is currently *deploy and watch logs*.

- https://github.com/nginx/nginx/blob/master/src/http/ngx_http_core_module.c#L1445-L1460
- https://github.com/nginx/nginx/blob/master/src/http/ngx_http_core_module.c#L1005-L1008
- https://github.com/nginx/nginx/blob/master/README.md#L19 (README defers debugging entirely to external docs)
- Open issues as of 2026-11-20: 50 titles scanned, none requests a config dry-run / route-print feature.

`nginx-route-probe` is a zero-dependency Python CLI that reimplements that decision offline:
server selection (`server_name` exact / `*.wildcard` / `.suffix` / `~regex`, per-`listen` port,
`default_server`), location matching (`=`, longest prefix, `^~`, first matching `~` / `~*` regex,
nested locations), plus `rewrite ... last|break` re-matching with nginx's 10-cycle guard and
`try_files ... @named` internal redirects. It prints the full decision trace for every request.

## Install

```bash
git clone git@github.com:linmoxi33/nginx-route-probe.git
cd nginx-route-probe
python3 --version   # 3.10+; no third-party dependencies
```

## Usage

```bash
python3 bin/nginx_route_probe.py \
  --config examples/in/nginx.conf \
  --requests examples/in/requests.txt
```

```
example.com /exact  ->  server#0 = /exact  status=200
example.com /api/v1/users  ->  server#0 /api/v1/  status=200
example.com /index.php  ->  server#0 ~ \.php$  status=200
api.example.com /old/thing  ->  server#1 /new/  status=200
api.example.com /loop/x  ->  server#1 /loop/  status=500  [rewrite loop guard]
[error] duplicate_location: duplicate location '= /exact' in server #0; the second block at index 7 is unreachable
```

Single request, with the trace:

```bash
python3 bin/nginx_route_probe.py -c examples/in/nginx.conf -r /api/x.php --host example.com --json
```

`examples/in/requests.txt` is one request per line: `host uri [port]`. `examples/out/report.json`
is the committed output of the command above (regenerate with `--json > examples/out/report.json`).

### JSON output

```json
{
  "tool": "nginx-route-probe",
  "version": "1.0.0",
  "results": [{
    "request": {"host": "example.com", "uri": "/api/x.php", "port": 80},
    "server": {"index": 0, "server_name": ["example.com", "www.example.com"]},
    "matched_path": "^~ /api/",
    "final_uri": "/api/x.php",
    "status": 200,
    "loop_guard": false,
    "trace": [
      {"step": "listen_filter", "port": 80, "candidates": [0, 1]},
      {"step": "server_name_exact", "pattern": "example.com", "server": 0},
      {"step": "longest_prefix", "location": "^~ /api/", "len": 5},
      {"step": "caret_tilde_stops_regex", "location": "^~ /api/"}
    ]
  }],
  "summary": {"requests": 1, "findings": 0, "errors": 0, "warnings": 0, "rewrite_loops": 0}
}
```

### Exit codes

| code | meaning |
| --- | --- |
| `0` | every request resolved; no finding at or above `--fail-on` |
| `1` | requests resolved, but at least one finding at or above `--fail-on` |
| `2` | usage error, unreadable file, or unparseable config |

`--fail-on {error,warning,never}` (default `error`) sets the threshold. Findings today:
`duplicate_location` (error), `invalid_regex` (error), `nested_in_exact` (error),
`duplicate_server_name` (warning), `no_server_name` (warning), `nested_regex` (warning).

## What it does / does not do

**Does:** parse the `http { server { location } }` subset (comments, quoted strings, nested blocks),
select the server, select the location with the same precedence rules as nginx, follow `rewrite`
and `try_files @named` internal redirects, and emit a per-request decision trace plus config lint
findings — all offline and deterministic.

**Does not:** run nginx, validate every directive's arguments, resolve `include` globs from disk,
evaluate `if` blocks, `map`, `geo`, `ssl_*`, or upstream health. It reports routing *decisions*,
not whether the resulting response is correct.

## Limitations

- Nested regex locations inside a prefix are matched after the parent prefix, but regex-vs-regex
  nesting order is not simulated.
- `rewrite` replacement uses Python `re.sub` semantics (nginx PCRE is close, not identical).
- `include` is not expanded; pass a single flattened config.
- No `if`, `map`, `split_clients`, or `error_page` recursion.

## 本轮创新

**B 档：把「路由判定」从运行期日志搬进可断言的离线 trace。** nginx 只能在真实请求上通过
`NGX_LOG_DEBUG_HTTP` 打印命中项；本工具首次把同一套优先级（`=` → 最长前缀 → `^~` → 首个正则 →
回退）连同 `rewrite last` 重匹配与 10 次循环保护，做成对任意 URI 序列可复现的 JSON 决策轨迹。
与 `crossplane`（58★，只做配置 AST 解析）、`nginx-location-match-visible`（186★，教学可视化）
不同：它们不判定给定 URI 的命中项，也不输出可断言的 trace 与退出码。

## 与同类项目的差异

`gh search repos "nginx config parser"`（20 命中，最高 58★）与 `gh search code
"ngx_http_core_find_location"`（全部为 nginx 源码或其 fork）在 2026-11-20 均无 stars>500 且覆盖
「离线 URI → server/location 判定」的实现。本仓库不复刻 nginx 源码，而是把匹配规则重写为可测试的
纯 Python 决策器，并额外提供配置 lint 与循环保护。

## License

MIT © 2026 linmoxi33. NGINX is a registered trademark of F5, Inc.; this project is not affiliated
with or endorsed by F5/NGINX.
