"""Persistent, asynchronous HTTP delivery with retry."""

import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from .cleanup import capture, remove_unchanged

LOG = logging.getLogger("comfy-webhook")


def validate_url(value):
    if not isinstance(value, str):
        raise ValueError("Invalid webhook URL")
    url = urllib.parse.urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Invalid webhook URL")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Outbox:
    def __init__(self, path, callback="", retry_initial=1, retry_max=30, timeout=120, output_root=None):
        if callback:
            validate_url(callback)
        if not os.path.isabs(path):
            raise ValueError("Outbox path must be absolute")
        if (not all(math.isfinite(v) and v > 0 for v in (retry_initial, retry_max, timeout))
                or retry_max < retry_initial):
            raise ValueError("Invalid retry settings")
        self.path, self.callback = path, callback
        self.output_root = output_root
        self.retry_initial, self.retry_max, self.timeout = retry_initial, retry_max, timeout
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        if os.name != "nt" and os.stat(path).st_mode & 0o077:
            raise ValueError("Outbox must not be accessible to other users")
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, body BLOB NOT NULL, callback TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            if "cleanup" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN cleanup TEXT")
            if "delivered" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def add(self, event, callback=None, *, auto_clean=False, cached_nodes=frozenset()):
        destination = validate_url(callback) if callback is not None else self.callback
        if not destination:
            return False
        body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()
        cleanup = json.dumps(capture(self.output_root, event.get("outputs", []), cached_nodes)) if auto_clean else None
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO events(id, body, callback, cleanup) VALUES (?, ?, ?, ?)",
                       (event["deliveryId"], body, destination, cleanup))
        return True

    def deliver_one(self, now=None, opener=None):
        clock = time.time if now is None else lambda: now
        with self.connect() as db:
            row = db.execute("SELECT id, body, callback, attempts, cleanup, delivered FROM events WHERE next_at <= ? ORDER BY rowid LIMIT 1", (clock(),)).fetchone()
        if row is None:
            return False
        delivery_id, body, callback, attempts, cleanup, delivered = row
        try:
            if not delivered:
                request = urllib.request.Request(callback, data=body, method="POST", headers={
                    "Content-Type": "application/json", "User-Agent": "ComfyUI-Webhook/1.0",
                })
                client = opener or urllib.request.build_opener(NoRedirect())
                with client.open(request, timeout=self.timeout) as response:
                    if not 200 <= response.status < 300:
                        raise OSError("Webhook returned a non-success status")
                if cleanup:
                    with self.connect() as db:
                        db.execute("UPDATE events SET delivered=1 WHERE id=? AND body=?", (delivery_id, body))
            if cleanup:
                remove_unchanged(self.output_root, json.loads(cleanup))
        except (OSError, ValueError) as error:
            if attempts == 0:
                LOG.warning("Webhook delivery deferred; retrying (%s)", type(error).__name__)
            delay = min(self.retry_max, self.retry_initial * 2 ** min(attempts, 30))
            with self.connect() as db:
                db.execute("UPDATE events SET attempts = ?, next_at = ? WHERE id = ?", (attempts + 1, clock() + delay, delivery_id))
            return False
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE id = ? AND body = ?", (delivery_id, body))
        return True

    def run(self, stop):
        while not stop.is_set():
            try:
                delivered = self.deliver_one()
            except Exception:
                LOG.exception("Webhook delivery failed")
                delivered = False
            if not delivered:
                stop.wait(1)

    def start(self):
        stop = threading.Event()
        thread = threading.Thread(target=self.run, args=(stop,), name="comfy-webhook", daemon=True)
        thread.start()
        return stop, thread
