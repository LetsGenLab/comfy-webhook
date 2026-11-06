"""Durable, asynchronous completion delivery."""

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..cleanup import _open_folder, _remove_file

LOG = logging.getLogger("comfy-webhook-managed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Outbox:
    def __init__(self, path, callback, output_root=None, input_root=None, clear_history=None, *, input_prefix, output_prefix, signing_key=None, signing_keys=None):
        url = urllib.parse.urlsplit(callback)
        if ((callback and (url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment))
                or not os.path.isabs(path)):
            raise ValueError("Invalid callback configuration")
        self.path, self.callback, self.output_root, self.input_root = path, callback, output_root, input_root
        self.input_prefix, self.output_prefix = input_prefix, output_prefix
        if signing_key is not None and (not isinstance(signing_key, str) or not 32 <= len(signing_key.encode()) <= 1024):
            raise ValueError("Managed webhook signing key must be 32 to 1024 bytes")
        self._signing_key = signing_key.encode() if signing_key is not None else None
        if signing_keys is not None and (not isinstance(signing_keys, dict) or not signing_keys or any(
                not isinstance(url, str) or not isinstance(key, str) or not 32 <= len(key.encode()) <= 1024
                for url, key in signing_keys.items())):
            raise ValueError("Invalid managed webhook signing keys")
        self._signing_keys = {url: key.encode() for url, key in signing_keys.items()} if signing_keys is not None else None
        self.clear_history = clear_history
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        if os.name != "nt" and os.stat(path).st_mode & 0o077:
            raise ValueError("ComfyUI outbox must not be accessible to other users")
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, body BLOB NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0)")
            if "callback" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
                db.execute("ALTER TABLE events ADD COLUMN callback TEXT")
            if "input_files" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
                db.execute("ALTER TABLE events ADD COLUMN input_files TEXT")
            if self._signing_keys is not None:
                destinations = {row[0] or self.callback for row in db.execute("SELECT DISTINCT callback FROM events")}
                if not destinations.issubset(self._signing_keys):
                    raise ValueError("Pending callbacks require their original signing keys")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _request(self, callback, body):
        headers = {"Content-Type": "application/json", "User-Agent": "ComfyUI-Webhook-Managed/1.0"}
        key = self._signing_key if self._signing_keys is None else self._signing_keys.get(callback)
        if self._signing_keys is not None and key is None:
            raise ValueError("Callback has no configured signing key")
        if key is not None:
            headers["X-Comfy-Webhook-Signature"] = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
        return urllib.request.Request(callback, data=body, method="POST", headers=headers)

    def add(self, event, callback=None, input_files=()):
        if not callback and not self.callback:
            return False
        if self._signing_keys is not None and (callback or self.callback) not in self._signing_keys:
            return False
        body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO events(id, body, callback, input_files) VALUES (?, ?, ?, ?)",
                       (event["deliveryId"], body, callback, json.dumps(input_files)))
        return True

    def pending_event(self, prompt_id):
        """Return an exact durable completion, never the graph or callback URL."""
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", prompt_id):
            return None
        with self.connect() as db:
            row = db.execute("SELECT body FROM events WHERE id = ?", (prompt_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recover_cleanup(self, prompt_id, acknowledgement):
        """Complete a recovered event only after the receiver accepted its result."""
        if not isinstance(acknowledgement, dict) or acknowledgement.get("accepted") is not True:
            return False
        event = self.pending_event(prompt_id)
        if not event:
            return False
        if event.get("version") != 1 or event.get("deliveryId") != prompt_id or event.get("promptId") != prompt_id:
            return False
        if event.get("status") == "cleaned":
            return True
        status, result = event.get("status"), acknowledgement.get("status")
        delete_output = status == "succeeded" and result in ("succeeded", "duplicate") and acknowledgement.get("cleanup") is True
        invalid_output = status == "succeeded" and result in ("failed", "duplicate") and acknowledgement.get("reason") == "output_invalid" and acknowledgement.get("cleanup") is False
        if status == "succeeded":
            if not delete_output and not invalid_output:
                return False
        elif status in ("failed", "interrupted"):
            if result not in ("failed", "duplicate"):
                return False
        else:
            return False
        with self.connect() as db:
            row = db.execute("SELECT body, input_files FROM events WHERE id = ?", (prompt_id,)).fetchone()
        if not row or json.loads(row[0]) != event:
            return False
        if delete_output:
            self.cleanup_output(event)
        self.cleanup_inputs(prompt_id, json.loads(row[1] or "[]"))
        cleaned = json.dumps({"version": 1, "deliveryId": prompt_id, "promptId": prompt_id,
                              "status": "cleaned"}, separators=(",", ":")).encode()
        with self.connect() as db:
            changed = db.execute("UPDATE events SET body = ?, attempts = 0, next_at = 0 WHERE id = ? AND body = ?",
                                 (cleaned, prompt_id, row[0])).rowcount
        if not changed:
            return self.pending_event(prompt_id) == json.loads(cleaned)
        if delete_output and self.clear_history:
            try:
                self.clear_history(prompt_id)
            except Exception:
                LOG.warning("Could not clear acknowledged ComfyUI history")
        return True

    def forget_cleaned(self, prompt_id):
        """Forget only a cleanup the receiver has already acknowledged."""
        event = self.pending_event(prompt_id)
        if event is None:
            return True
        if event.get("status") != "cleaned":
            return False
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE id = ? AND body = ?",
                       (prompt_id, json.dumps(event, separators=(",", ":")).encode()))
        return True

    def deliver_one(self, now=None, opener=None):
        clock = time.time if now is None else lambda: now
        now = clock()
        with self.connect() as db:
            # Lease the event before network I/O so another delivery thread can
            # handle other jobs without sending this same event concurrently.
            # A crashed process leaves the event eligible again after the lease.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id, body, attempts, callback, input_files FROM events WHERE next_at <= ? ORDER BY rowid LIMIT 1", (now,)).fetchone()
            if row:
                # Delivery can include both a 120-second callback and a
                # 120-second cleanup acknowledgement.
                db.execute("UPDATE events SET next_at = ? WHERE id = ?", (now + 300, row[0]))
        if row is None:
            return False
        delivery_id, body, attempts, callback, input_files = row
        request = self._request(callback or self.callback, body)
        try:
            client = opener or urllib.request.build_opener(NoRedirect())
            with client.open(request, timeout=120) as response:
                if not 200 <= response.status < 300:
                    raise OSError("Callback returned non-success status")
                result = json.loads(response.read(65537))
                if not isinstance(result, dict) or result.get("accepted") is not True:
                    raise ValueError("Callback acknowledgement is invalid")
                status = result.get("status")
                event = json.loads(body)
                event_status = event.get("status") if isinstance(event, dict) else None
                if event_status == "cleaned":
                    if status != "cleaned":
                        raise ValueError("Cleanup callback acknowledgement is invalid")
                    with self.connect() as db:
                        db.execute("DELETE FROM events WHERE id = ? AND body = ?", (delivery_id, body))
                    return True
                if (status not in ("succeeded", "failed", "duplicate")
                        or (status == "succeeded" and (event_status != "succeeded" or result.get("cleanup") is not True))
                        or (status == "failed" and event_status not in ("failed", "interrupted")
                            and not (event_status == "succeeded" and result.get("reason") == "output_invalid"))):
                    # An ignored or incomplete acknowledgement does not prove
                    # that the Worker committed the terminal result. Keep the
                    # same event for retry, including after an extension restart.
                    raise ValueError("Callback is not terminally acknowledged")
                if (result.get("cleanupAckRequired") is True and event_status == "succeeded"
                        and result.get("cleanup") is not True
                        and not (status in ("failed", "duplicate") and result.get("reason") == "output_invalid")):
                    # A conflicting or invalid success may still name an output.
                    # Do not claim cleanup when the Worker did not authorize its
                    # deletion; retain the event for an operator to reconcile.
                    raise ValueError("Output cleanup is not authorized")
                if result.get("cleanup") is True:
                    self.cleanup_output(event)
                # Only a known, terminal Worker result releases input files.
                # An ignored or malformed callback is not an acknowledgement.
                if result.get("cleanup") is True or result.get("status") in ("failed", "duplicate"):
                    self.cleanup_inputs(delivery_id, json.loads(input_files or "[]"))
                if result.get("cleanupAckRequired") is True:
                    # A managed instance cannot be stopped until the Worker
                    # knows task-owned files were removed. Keep the durable
                    # original event until this second callback is accepted;
                    # both cleanup and delivery are safe to repeat.
                    ack = json.dumps({"version": 1, "deliveryId": delivery_id,
                                      "promptId": delivery_id, "status": "cleaned"}, separators=(",", ":")).encode()
                    cleanup_request = self._request(callback or self.callback, ack)
                    with client.open(cleanup_request, timeout=120) as cleanup_response:
                        if not 200 <= cleanup_response.status < 300:
                            raise OSError("Cleanup callback returned non-success status")
                        cleanup_result = json.loads(cleanup_response.read(65537))
                        if not isinstance(cleanup_result, dict) or cleanup_result.get("accepted") is not True or cleanup_result.get("status") != "cleaned":
                            raise ValueError("Cleanup callback acknowledgement is invalid")
        except (OSError, urllib.error.URLError, ValueError) as error:
            if attempts == 0:
                LOG.warning("ComfyUI callback deferred; retrying (%s)", type(error).__name__)
            with self.connect() as db:
                db.execute("UPDATE events SET attempts = ?, next_at = ? WHERE id = ?",
                           (attempts + 1, clock() + min(30, 2 ** min(attempts, 8)), delivery_id))
            return False
        # History is only metadata; clear our completed success after the
        # Worker has stored it and all required cleanup acknowledgements passed.
        if self.clear_history and event_status == "succeeded" and result.get("cleanup") is True:
            try:
                self.clear_history(delivery_id)
            except Exception:
                LOG.warning("Could not clear acknowledged ComfyUI history")
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE id = ?", (delivery_id,))
        return True

    def cleanup_inputs(self, prompt_id, files):
        if not files:
            return
        pattern = re.compile(re.escape(self.input_prefix) + r"-" + re.escape(prompt_id) + r"-[A-Za-z0-9_-]{1,100}\.(?:png|jpg|jpeg|webp)")
        if (not self.input_root or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", prompt_id)
                or not isinstance(files, list) or len(files) > 9 or any(not isinstance(name, str) or not pattern.fullmatch(name) for name in files)):
            raise ValueError("Invalid cleanup inputs")
        with _open_folder(os.path.realpath(self.input_root), "") as (root_fd, directory):
            for name in files:
                try:
                    _remove_file(root_fd, directory, name)
                except FileNotFoundError:
                    pass  # An earlier attempt may have already removed it.

    def cleanup_staged(self, prompt_id, files, queue):
        """Remove explicitly owned inputs only while this prompt is not queued or running."""
        if (not isinstance(prompt_id, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", prompt_id)
                or not isinstance(files, list) or len(files) > 9):
            raise ValueError("Invalid staged cleanup request")
        with queue.mutex:
            pending, running = getattr(queue, "queue", None), getattr(queue, "currently_running", None)
            if not isinstance(pending, list) or not isinstance(running, dict):
                return False
            entries = pending + list(running.values())
            if any(not isinstance(item, (tuple, list)) or len(item) < 2 for item in entries):
                return False
            if any(item[1] == prompt_id for item in entries) or self.pending_event(prompt_id) is not None:
                return False
            self.cleanup_inputs(prompt_id, files)
            return True

    def cleanup_output(self, event):
        """Delete only the acknowledged task video, never unrelated UI files."""
        prompt_id = event.get("promptId", "")
        if not self.output_root or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", prompt_id):
            raise ValueError("Invalid cleanup identity")
        files = [item for item in event.get("outputs", []) if isinstance(item, dict)
                 and item.get("type") == "output" and item.get("subfolder", "") == ""
                 and isinstance(item.get("filename"), str)
                 and re.fullmatch(re.escape(self.output_prefix) + r"-" + re.escape(prompt_id) + r"_[A-Za-z0-9_-]+\.mp4", item["filename"])]
        if len(files) != 1:
            raise ValueError("Invalid cleanup output")
        with _open_folder(os.path.realpath(self.output_root), "") as (root_fd, directory):
            try:
                _remove_file(root_fd, directory, files[0]["filename"])
            except FileNotFoundError:
                pass  # Previous attempt deleted the file before its outbox acknowledgement.

    def run(self, stop):
        while not stop.is_set():
            try:
                delivered = self.deliver_one()
            except Exception:
                LOG.exception("ComfyUI callback delivery failed")
                delivered = False
            if not delivered:
                stop.wait(2)

    def start(self):
        stop = threading.Event()
        self.threads = [threading.Thread(target=self.run, args=(stop,), name=f"comfy-managed-outbox-{i}", daemon=True)
                        for i in range(2)]
        for thread in self.threads:
            thread.start()
        return stop, self.threads[0]
