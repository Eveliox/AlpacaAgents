"""Loopback-only Layer 4 server. No broker imports, order routes, or disk chat log.

A single bounded worker queue owns snapshots AND controller callbacks. HTTP
threads only validate requests and await that queue. The callback is supplied
by the controller; chat can request a dry diagnostic cycle, never submission.
"""
from collections import deque
from concurrent.futures import Future, TimeoutError
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Empty, Full, Queue
import secrets
import threading
import time

from .dashboard import AGENTS, collect, render
from .dashboard_chat import answer_for, conversation_data, redact_secrets, script_policy


def _pick(row, keys):
    return {k: row.get(k) for k in keys.split()}


def display_snapshot(raw):
    """Explicit projection: collect() includes journal bodies and MUST NOT go on HTTP.

    Free text is additionally redacted. This is defense in depth, not permission
    to put secrets in reports. Unknown financial values retain their known flags.
    """
    d = _pick(raw, "now today control approvals daily_loss daily_net latched shadow_counts issues ledger_known orders_known")
    schemas = {
        "inventory": "contract qty basis opened",
        "closed": "trading_day contract pnl_units side",
        "intents": "created_at kind status symbol contract cost reason",
        "live": "created_at kind status symbol contract cost reason",
        "order_events": "timestamp event",
        "notifications": "timestamp level title",
        "traces": "started_at method path status outcome",
    }
    for name, keys in schemas.items():
        d[name] = [_pick(r, keys) for r in raw[name]]
    # The HTML renderer expects this column; deliberately do not expose IDs.
    for r in d["intents"]:
        r["broker_order_id"] = None
    d["cycles"] = []
    for c in raw["cycles"]:
        item = _pick(c, "started_at finished_at control_mode submit")
        stages = c.get("stages") if isinstance(c.get("stages"), dict) else {}
        item["stages"] = {}
        for name, keys in {
            "reconcile": "ok reasons error open_positions pending settled_cash daily_loss breaker",
            "entries": "skipped error", "halt": "reason", "scan": "shadow proposals skipped",
        }.items():
            if isinstance(stages.get(name), dict):
                item["stages"][name] = _pick(stages[name], keys)
                # Presence of an error key itself matters in presentation.
                if item["stages"][name].get("error") is None:
                    item["stages"][name].pop("error", None)
        d["cycles"].append(item)
    shadow = raw["shadow"]
    d["shadow"] = {"generated_at": shadow.get("generated_at")} if shadow else {}
    for name, keys in {"errors": "symbol reason", "shadow": "symbol playbook thesis"}.items():
        if isinstance(shadow.get(name), list):
            d["shadow"][name] = [_pick(r, keys) for r in shadow[name] if isinstance(r, dict)]
    d["research"] = []
    for r in raw["research"]:
        item = _pick(r, "symbol file first_bar last_bar generated_at options_pnl_modelled level")
        item["summary"] = {k: _pick(v, "resolved no_trade expectancy_r median_r profit_factor max_loss_r sample_sufficient")
                           for k, v in r["summary"].items() if isinstance(v, dict)}
        d["research"].append(item)

    def clean(value):
        if isinstance(value, str):
            return redact_secrets(value)
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {redact_secrets(k): clean(v) for k, v in value.items()}
        return value
    return clean(d)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


class Busy(Exception):
    pass


class _LoopbackServer(ThreadingHTTPServer):
    request_queue_size = 16
    daemon_threads = True

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Includes disconnects during header reads; never log raw requests.


class Studio:
    def __init__(self, runtime: Path, *, cycle=None, every=None, clock=None):
        self.runtime, self.cycle, self.every = runtime, cycle, every
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.token = secrets.token_urlsafe(32)
        self.jobs = Queue(maxsize=16)
        self.stop = threading.Event()
        self.worker = None
        self.schedule = "scheduled" if every else "on demand"
        self.server = _LoopbackServer(("127.0.0.1", 0), self._handler())
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.url = "http://" + self.host
        self.rate_lock, self.requests = threading.Lock(), deque()

    def start(self):
        self.worker = threading.Thread(target=self._work, name="studio-worker")
        self.worker.start()
        self.http = threading.Thread(target=self.server.serve_forever, name="studio-http")
        self.http.start()
        return self

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.http.join()
        # Wait for an in-progress cycle; do not release RuntimeLock while it runs.
        self.worker.join()

    def call(self, operation):
        if self.stop.is_set():
            raise Busy()
        future = Future()
        try:
            self.jobs.put_nowait((future, operation))
        except Full:
            raise Busy() from None
        try:
            return future.result(timeout=120)
        except TimeoutError:
            future.cancel()  # queued diagnostics must not start after client timeout
            raise Busy() from None

    def _work(self):
        due = time.monotonic() if self.every else None
        while not self.stop.is_set():
            if due is not None and time.monotonic() >= due:
                try:
                    report = self.cycle(dry_run=False)
                    from .controller import HALT_PREFIXES
                    rec = report["stages"].get("reconcile", {})
                    reasons = rec.get("reasons", [])
                    closed = reasons and all(r == "MARKET_CLOSED" or r.startswith("NOT_A_SESSION") for r in reasons)
                    halted = "error" in rec or any(r.startswith(HALT_PREFIXES) for r in reasons)
                    self.schedule = "market closed" if closed else "halted: needs human review" if halted else "scheduled"
                    due = None if closed or halted else time.monotonic() + self.every
                except Exception:
                    # No exception text in HTTP/logs: transports can contain secrets.
                    self.schedule = "halted: cycle failed; inspect local records"
                    due = None
            if self.stop.is_set():
                break
            try:
                future, operation = self.jobs.get(timeout=0.1)
            except Empty:
                continue
            if self.stop.is_set():
                future.cancel()
                break
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(operation())
                except Exception as exc:
                    future.set_exception(exc)
        while not self.jobs.empty():
            future, _ = self.jobs.get_nowait()
            future.cancel()

    def snapshot(self):
        return display_snapshot(collect(self.runtime, now=self.clock()))

    def context(self, d):
        context = conversation_data(d, AGENTS, live=True)
        context['topics']['briefing']['text'] += '\nController schedule: ' + self.schedule + '.'
        return context

    def metadata(self, d):
        return {"live": True, "read_at": d["now"].isoformat(), "schedule": self.schedule,
                "last_cycle": d["cycles"][0].get("finished_at") if d["cycles"] else None}

    def ask(self, agent, text):
        if redact_secrets(text) != text:
            return {"text": "Possible credential hidden. Never paste keys here; rotate exposed keys. Message not saved.",
                    "source": "Local privacy guard"}
        if agent == "houston" and text.strip().lower() == "reconcile":
            if self.cycle is None:
                return {"text": "Diagnostic cycle unavailable. Broker state is unknown.", "source": "Controller"}
            try:
                self.cycle(dry_run=True)
            except Exception:
                return {"text": "Diagnostic cycle failed. Broker state is unknown. Check credentials and local records; no order submission was requested.",
                        "source": "Controller diagnostic"}
            d = self.snapshot()
            reply = dict(self.context(d)["topics"]["blockers"])
            reply["text"] = "Dry diagnostic cycle completed; no order submission requested.\n" + reply["text"]
        else:
            d = self.snapshot()
            reply = dict(answer_for(text, agent, self.context(d)))
        return {**reply, **self.metadata(d)}

    def _allowed_rate(self):
        with self.rate_lock:
            now = time.monotonic()
            while self.requests and now - self.requests[0] >= 1:
                self.requests.popleft()
            if len(self.requests) >= 10:
                return False
            self.requests.append(now)
            return True

    def _handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LocalStudio"
            sys_version = ""

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass  # No URLs, tokens, questions, or transport exceptions in logs.

            def send_error(self, code, message=None, explain=None):
                self.respond(code)

            def respond(self, code, body=b"", content_type="application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", script_policy(live=True) + "; frame-ancestors 'none'")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if body:
                    self.wfile.write(body)

            def dispatch(self):
                try:
                    self.route()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    self.close_connection = True
                except Busy:
                    self.respond(503)
                except Exception:
                    self.respond(503)  # Do not reflect exception text/credentials.

            def route(self):
                if (self.headers.get_all("Host") != [app.host]
                        or self.headers.get("Sec-Fetch-Site") == "cross-site"
                        or (self.headers.get("Origin") is not None and self.headers.get_all("Origin") != [app.url])):
                    self.respond(403)
                    return
                if not app._allowed_rate():
                    self.respond(429)
                    return
                lengths = self.headers.get_all('Content-Length', [])
                if self.headers.get('Transfer-Encoding') or len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
                    self.respond(400)
                    return
                if lengths and int(lengths[0]) > 8192:
                    self.respond(413)
                    return
                # Root is the same-origin bootstrap, not an unauthenticated API.
                if self.command == "GET" and self.path == "/":
                    page = app.call(lambda: render(app.snapshot(), studio={"token": app.token, "base_url": app.url, "schedule": app.schedule}))
                    self.respond(200, page.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if self.path not in ("/api/snapshot", "/api/ask"):
                    self.respond(404)
                    return
                tokens = self.headers.get_all("X-Studio-Token", [])
                if len(tokens) != 1 or not secrets.compare_digest(tokens[0].encode('utf-8'), app.token.encode('ascii')):
                    self.respond(401)
                    return
                if self.path == "/api/snapshot" and self.command == "GET":
                    def current():
                        d = app.snapshot()
                        visible = dict(d)
                        if not d['ledger_known']:
                            visible.update(daily_loss=None, daily_net=None, latched=None)
                        return {"snapshot": visible, "context": app.context(d), **app.metadata(d)}
                    result = app.call(current)
                elif self.path == "/api/ask" and self.command == "POST":
                    if self.headers.get_all("Origin") != [app.url]:
                        self.respond(403)
                        return
                    lengths = self.headers.get_all("Content-Length", [])
                    if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
                        self.respond(400)
                        return
                    size = int(lengths[0])
                    if size > 8192:
                        self.respond(413)
                        return
                    if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
                        self.respond(415)
                        return
                    try:
                        body = json.loads(self.rfile.read(size).decode("utf-8"), object_pairs_hook=_unique_object)
                        if (not isinstance(body, dict) or set(body) != {"agent", "text"}
                                or body["agent"] not in [a[0] for a in AGENTS]
                                or not isinstance(body["text"], str) or not 1 <= len(body["text"].strip()) <= 800
                                or len(body["text"]) > 800):
                            raise ValueError()
                    except (ValueError, UnicodeError, TypeError):
                        self.respond(400)
                        return
                    result = app.call(lambda: app.ask(body["agent"], body["text"]))
                else:
                    self.respond(405)
                    return
                self.respond(200, json.dumps(result, default=str, allow_nan=False).encode("utf-8"))

            do_GET = dispatch
            do_POST = dispatch
            do_OPTIONS = dispatch

        return Handler


def serve(runtime, *, cycle, every=None):
    """Called while the controller owns RuntimeLock. Ctrl+C joins the worker."""
    import sys
    app = Studio(runtime, cycle=cycle, every=every).start()
    print(f"Paper workspace: {app.url}/", file=sys.stderr, flush=True)
    print(f"Local session token (do not share): {app.token}", file=sys.stderr, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        app.close()
