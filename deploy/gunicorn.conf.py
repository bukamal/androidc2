"""Gunicorn configuration.

Run:
    gunicorn -c deploy/gunicorn.conf.py 'app:create_app()'

Why gevent-geventwebsocket instead of the Flask dev server:

* The Werkzeug dev server logs a spurious
  ``AssertionError: write() before start_response`` for *every* WebSocket
  that closes. ``simple-websocket`` takes over the raw socket, then Werkzeug
  finalises a response cycle that never started. Purely cosmetic — but it
  fills the log with 500s that look like a broken tunnel.
* One worker with cooperative concurrency. Do **not** scale workers with
  ``-w N``: the connected-agent registry lives in process memory
  (``c2/agent_ws._agents``), so a second worker would not see devices held by
  the first. Moving that registry to Redis is the prerequisite for scaling
  out — see README "Scaling past one process".
"""

import multiprocessing
import os

bind = os.environ.get("C2_BIND", "127.0.0.1:5000")

# Single worker: see module docstring. Cooperative gevent handles many
# concurrent WebSockets inside one process.
workers = 1
worker_class = "geventwebsocket.gunicorn.workers.GeventWebSocketWorker"

# Keep the connection alive well past any tunnel/NAT idle timeout.
timeout = 120
graceful_timeout = 30
keepalive = 65

# Recycle periodically so a long-lived implant fleet cannot pin a bad memory
# growth in the process forever.
max_requests = 10000
max_requests_jitter = 1000

accesslog = "-"          # stdout, so journald captures it
errorlog = "-"
loglevel = os.environ.get("C2_LOGLEVEL", "info")
# Access logging on every socket frame is noise; keep it at warning.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'

proc_name = "androidc2"

preload_app = False       # Config reads env at import; do not fork-share it


def when_ready(server):
    server.log.info("androidc2 ready on %s (worker_class=%s)",
                    bind, worker_class)


def worker_int(prev_pid, prev_worker):
    server = getattr(worker_int, "_server", None)
    if server:
        server.log.info("worker %s interrupted, draining sockets", prev_pid)