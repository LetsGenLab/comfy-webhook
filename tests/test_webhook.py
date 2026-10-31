import json
import importlib.util
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from extension.events import event_from_completion, install_queue_hook, workflow_callback
from extension.nodes import Webhook
from extension.outbox import Outbox


class WebhookTests(unittest.TestCase):
    def test_node_is_an_independent_output_and_needs_no_video_connection(self):
        self.assertTrue(Webhook.OUTPUT_NODE)
        self.assertEqual(Webhook.RETURN_TYPES, ())
        self.assertEqual(set(Webhook.INPUT_TYPES()["required"]), {"url"})
        self.assertEqual(Webhook().configure("https://example.com/hook"), ())

    def test_real_package_entry_enables_node_without_environment_settings(self):
        class Queue:
            mutex = threading.Lock()
            currently_running = {}
            def task_done(self, *args, **kwargs): pass
        queue = Queue()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(__file__).resolve().parent.parent
            spec = importlib.util.spec_from_file_location(
                "webhook_package_test", root / "__init__.py", submodule_search_locations=[str(root)])
            module = importlib.util.module_from_spec(spec)
            fake_server = SimpleNamespace(PromptServer=SimpleNamespace(instance=SimpleNamespace(prompt_queue=queue)))
            fake_paths = SimpleNamespace(get_user_directory=lambda: tmp)
            with patch.dict(sys.modules, {"webhook_package_test": module, "server": fake_server, "folder_paths": fake_paths}), \
                    patch.dict("os.environ", {}, clear=True):
                try:
                    spec.loader.exec_module(module)
                    self.assertTrue(module.NODE_CLASS_MAPPINGS["ComfyWebhook"].OUTPUT_NODE)
                    self.assertTrue((Path(tmp) / "comfy-webhook" / "outbox.sqlite").exists())
                    self.assertTrue(queue._comfy_webhook_hook)
                finally:
                    if hasattr(module, "_stop"):
                        module._stop.set()
                        module._thread.join(timeout=5)
                        self.assertFalse(module._thread.is_alive())

    def test_workflow_url_is_available_when_job_fails_before_node_execution(self):
        graph = {"1": {"class_type": "ComfyWebhook", "inputs": {"url": "https://example.com/hook"}}}
        recorded = []
        class Queue:
            mutex = threading.Lock()
            currently_running = {1: (0, "job-1", graph, {})}
            def task_done(self, *args, **kwargs): return "done"
        queue = Queue()
        install_queue_hook(queue, SimpleNamespace(add=lambda event, url: recorded.append((event, url))))
        status = SimpleNamespace(status_str="error", messages=[("execution_error", {"node_id": "other"})])
        self.assertEqual(queue.task_done(1, {}, status), "done")
        self.assertEqual(recorded[0][0]["status"], "failed")
        self.assertEqual(recorded[0][1], "https://example.com/hook")

    def test_workflow_video_output_is_delivered_to_node_url(self):
        graph = {"5": {"class_type": "ComfyWebhook", "inputs": {"url": "https://example.com/video"}}}
        class Queue:
            mutex = threading.Lock()
            currently_running = {1: (0, "video-job", graph, {})}
            def task_done(self, *args, **kwargs): return "done"
        class Response:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Receiver:
            def open(self, request, timeout):
                self.url, self.body = request.full_url, json.loads(request.data)
                return Response()
        with tempfile.TemporaryDirectory() as tmp:
            box = Outbox(str(Path(tmp) / "events.sqlite"))
            queue = Queue()
            install_queue_hook(queue, box)
            history = {"outputs": {"9": {"gifs": [{"filename": "result.mp4", "subfolder": "", "type": "output"}]}}}
            queue.task_done(1, history, SimpleNamespace(status_str="success", messages=[]))
            receiver = Receiver()
            self.assertTrue(box.deliver_one(now=100, opener=receiver))
            self.assertEqual(receiver.url, "https://example.com/video")
            self.assertEqual(receiver.body["outputs"][0]["filename"], "result.mp4")

    def test_node_url_must_be_literal_and_unambiguous(self):
        self.assertIsNone(workflow_callback({}))
        node = {"class_type": "ComfyWebhook", "inputs": {"url": "https://example.com/hook"}}
        self.assertEqual(workflow_callback({"1": node}), "https://example.com/hook")
        with self.assertRaises(ValueError): workflow_callback({"1": node, "2": node})
        with self.assertRaises(ValueError): workflow_callback({"1": {"class_type": "ComfyWebhook", "inputs": {"url": ["2", 0]}}})

    def test_http_error_then_success_end_to_end(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(503 if len(received) == 1 else 204)
                self.end_headers()
            def log_message(self, *_args): pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                box = Outbox(str(Path(tmp) / "events.sqlite"), f"http://127.0.0.1:{server.server_port}/hook")
                event = {"deliveryId": "job-1", "promptId": "job-1", "status": "failed", "outputs": []}
                box.add(event)
                self.assertFalse(box.deliver_one(now=100))
                self.assertTrue(box.deliver_one(now=101))
                self.assertEqual(received, [event, event])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_events_cover_success_error_and_interrupt_without_input_values(self):
        history = {"outputs": {"9": {"images": [{"filename": "result.png", "type": "output"},
                                                 {"filename": "../bad", "type": "output"}]}}}
        success = event_from_completion("job-1", SimpleNamespace(status_str="success", messages=[]), history)
        self.assertEqual(success["status"], "succeeded")
        self.assertEqual([x["filename"] for x in success["outputs"]], ["result.png"])
        error = event_from_completion("job-1", SimpleNamespace(status_str="error", messages=[
            ("execution_error", {"node_id": "9", "exception_type": "ValueError", "current_inputs": "hidden"})]), history)
        self.assertEqual(error["status"], "failed")
        self.assertEqual(error["error"], {"nodeId": "9", "type": "ValueError"})
        self.assertNotIn("hidden", json.dumps(error))
        interrupted = event_from_completion("job-1", SimpleNamespace(status_str="error", messages=[("execution_interrupted", {})]), {})
        self.assertEqual(interrupted["status"], "interrupted")

    def test_per_job_destination_and_any_success_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Outbox(str(Path(tmp) / "events.sqlite"), "https://example.com/default")
            box.add({"deliveryId": "job-1", "status": "failed"}, "http://localhost:1234/custom")
            calls = []
            class Response:
                status = 204
                def __enter__(self): return self
                def __exit__(self, *args): pass
            class Receiver:
                def open(self, request, timeout):
                    calls.append((request.full_url, json.loads(request.data), timeout))
                    return Response()
            self.assertTrue(box.deliver_one(now=100, opener=Receiver()))
            self.assertEqual(calls, [("http://localhost:1234/custom", {"deliveryId": "job-1", "status": "failed"}, 120)])
            self.assertFalse(box.deliver_one(now=100, opener=Receiver()))

    def test_failure_retries_exact_event_and_destination_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.sqlite")
            box = Outbox(path, retry_initial=2, retry_max=4)
            event = {"deliveryId": "job-1", "status": "failed"}
            box.add(event, "https://example.com/custom")
            class Offline:
                def open(self, request, timeout): raise OSError("offline")
            self.assertFalse(box.deliver_one(now=100, opener=Offline()))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT attempts, next_at FROM events").fetchone(), (1, 102))
            restored = Outbox(path, retry_initial=2, retry_max=4)
            self.assertFalse(restored.deliver_one(now=101, opener=Offline()))
            self.assertFalse(restored.deliver_one(now=102, opener=Offline()))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT attempts, next_at FROM events").fetchone(), (2, 106))
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
            class Receiver:
                def open(self, request, timeout):
                    self.request = request
                    return Response()
            receiver = Receiver()
            self.assertTrue(restored.deliver_one(now=106, opener=receiver))
            self.assertEqual(receiver.request.full_url, "https://example.com/custom")
            self.assertEqual(json.loads(receiver.request.data), event)

    def test_hook_persists_before_queue_completion_and_passes_per_job_url(self):
        captured = []
        class Queue:
            mutex = threading.Lock()
            currently_running = {1: (0, "job-1", {"1": {"class_type": "ComfyWebhook", "inputs": {"url": "https://example.com/node"}}}, {"webhook_url": "https://example.com/custom"})}
            def task_done(self, item_id, history_result, status, process_item=None):
                self.assert_recorded = len(captured)
                return "done"
        queue = Queue()
        install_queue_hook(queue, SimpleNamespace(add=lambda event, url: captured.append((event, url))))
        self.assertEqual(queue.task_done(1, {}, SimpleNamespace(status_str="error", messages=[])), "done")
        self.assertEqual(queue.assert_recorded, 1)
        self.assertEqual(captured[0][0]["status"], "failed")
        self.assertEqual(captured[0][1], "https://example.com/custom")
        with self.assertRaises(RuntimeError): install_queue_hook(queue, None)

    def test_queue_finishes_even_if_event_cannot_be_saved(self):
        class Queue:
            mutex = threading.Lock()
            currently_running = {1: (0, "job-1")}
            def task_done(self, *args, **kwargs): return "done"
        queue = Queue()
        box = SimpleNamespace(add=lambda *args: (_ for _ in ()).throw(OSError("disk full")))
        install_queue_hook(queue, box)
        with self.assertLogs("comfy-webhook", level="ERROR"):
            self.assertEqual(queue.task_done(1, {}, SimpleNamespace(status_str="error", messages=[])), "done")

    def test_invalid_settings_and_no_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.sqlite")
            box = Outbox(path)
            self.assertFalse(box.add({"deliveryId": "job-1"}))
            for url in ("file:///tmp/file", "https://user:password@example.com/"):
                with self.subTest(url=url), self.assertRaises(ValueError): box.add({"deliveryId": "job-1"}, url)
            with self.assertRaises(ValueError): Outbox(path, retry_initial=10, retry_max=1)
            with self.assertRaises(ValueError): Outbox("relative.sqlite")


if __name__ == "__main__":
    unittest.main()
