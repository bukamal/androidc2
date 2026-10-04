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

cp .env.example .env          # then edit it
python3 app.py
```

Open `http://127.0.0.1:5000` and log in. If you did not set
`C2_OPERATOR_PASSWORD`, the password is printed in the startup banner and
persisted at `data/c2_operator_password.secret`.

The server binds **loopback by default**. Expose it through a tunnel you
control rather than setting `C2_HOST=0.0.0.0` on a machine with a routable
address.

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