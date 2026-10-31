import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from extension.managed.bootstrap import start
from extension.events import install_queue_hook as install_generic
from extension.managed.events import install_queue_hook as install_managed


class BootstrapTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertIsNone(start(None, "/inputs", "/outputs", {}))

    def test_configures_capability_and_recovery_routes(self):
        handlers = {}
        def route(method):
            def register(path):
                def decorate(handler):
                    handlers[(method, path)] = handler
                    return handler
                return decorate
            return register
        routes = SimpleNamespace(get=route("GET"), post=route("POST"), delete=route("DELETE"))
        queue = SimpleNamespace(delete_history_item=lambda _: None)
        server = SimpleNamespace(routes=routes, prompt_queue=queue)
        thread = SimpleNamespace(is_alive=lambda: True)
        with tempfile.TemporaryDirectory() as root, patch("extension.managed.bootstrap.Outbox") as box, \
                patch("extension.managed.bootstrap.install_queue_hook") as install:
            box.return_value.start.return_value = (None, thread)
            env = {"COMFY_WEBHOOK_MANAGED_OUTBOX_PATH": str(Path(root)/"events.sqlite"),
                   "COMFY_WEBHOOK_MANAGED_API_PREFIX": "/adapter",
                   "COMFY_WEBHOOK_MANAGED_CALLBACK_FIELD": "job_callback",
                   "COMFY_WEBHOOK_MANAGED_CALLBACKS": "https://receiver.example/callback",
                   "COMFY_WEBHOOK_MANAGED_INPUT_PREFIX": "example-input",
                   "COMFY_WEBHOOK_MANAGED_OUTPUT_PREFIX": "example"}
            start(server, root, root, env)
            install.assert_called_once_with(queue, box.return_value, "job_callback",
                frozenset({"https://receiver.example/callback"}), "example-input", "example")
            response = asyncio.run(handlers[("GET", "/adapter/capabilities")](None))
            self.assertEqual(json.loads(response.body), {"callback": 1, "cleanup_ack": 1, "outbox_recovery": 2})
            self.assertIn(("POST", "/adapter/outbox/{prompt_id}/ack"), handlers)
            self.assertIn(("DELETE", "/adapter/outbox/{prompt_id}/ack"), handlers)

    def test_requires_a_safe_allowlist_and_separate_callback_field(self):
        base = {"COMFY_WEBHOOK_MANAGED_OUTBOX_PATH": "/tmp/private.sqlite"}
        invalid = [base, {**base, "COMFY_WEBHOOK_MANAGED_CALLBACKS": "http://example.test/hook"},
                   {**base, "COMFY_WEBHOOK_MANAGED_CALLBACKS": "https://example.test/hook",
                    "COMFY_WEBHOOK_MANAGED_CALLBACK_FIELD": "webhook_url"},
                   {**base, "COMFY_WEBHOOK_MANAGED_CALLBACKS": "https://example.test/hook",
                    "COMFY_WEBHOOK_MANAGED_API_PREFIX": "/../../unsafe"}]
        for env in invalid:
            with self.subTest(env=env), self.assertRaises(ValueError):
                start(None, "/inputs", "/outputs", env)

    def test_generic_and_managed_hooks_handle_separate_jobs(self):
        emitted = []
        class Queue:
            mutex = threading.Lock()
            currently_running = {}
            def task_done(self, item_id, history, status, process_item=None):
                return "done"
        class Generic:
            def add(self, event, callback, **kwargs):
                if callback:
                    emitted.append(("generic", callback, event["status"]))
        class Managed:
            def add(self, event, callback, inputs):
                if callback:
                    emitted.append(("managed", callback, event["status"]))
                return False
        queue = Queue()
        install_generic(queue, Generic())
        install_managed(queue, Managed(), "managed_url", {"https://receiver.example/hook"}, "input", "output")
        status = SimpleNamespace(status_str="success", messages=[])
        queue.currently_running[1] = (0, "job-1", {}, {"managed_url": "https://receiver.example/hook"})
        self.assertEqual(queue.task_done(1, {}, status), "done")
        self.assertEqual(emitted, [("managed", "https://receiver.example/hook", "succeeded")])
        emitted.clear()
        queue.currently_running[2] = (0, "job-2", {}, {"webhook_url": "https://other.example/hook"})
        self.assertEqual(queue.task_done(2, {}, status), "done")
        self.assertEqual(emitted, [("generic", "https://other.example/hook", "succeeded")])
