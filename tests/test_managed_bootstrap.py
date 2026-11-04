import asyncio
import hashlib
import hmac
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
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(start(None, root, root, {}, user_directory=root))

    def test_enabled_plugin_registers_managed_delivery_with_configured_allowlist(self):
        handlers = {}
        def route(method):
            def register(path):
                def decorate(handler):
                    handlers[(method, path)] = handler
                    return handler
                return decorate
            return register
        queue = SimpleNamespace(delete_history_item=lambda _: None)
        server = SimpleNamespace(routes=SimpleNamespace(get=route("GET"), post=route("POST"), delete=route("DELETE")), prompt_queue=queue)
        thread = SimpleNamespace(is_alive=lambda: True)
        with tempfile.TemporaryDirectory() as root, patch("extension.managed.bootstrap.Outbox") as box, \
                patch("extension.managed.bootstrap.install_queue_hook") as install:
            box.return_value.start.return_value = (None, thread)
            start(server, root, root, {"COMFY_WEBHOOK_MANAGED_ENABLED": "1",
                  "COMFY_WEBHOOK_MANAGED_CALLBACKS": "https://receiver.example/callback"}, user_directory=root)
            self.assertEqual(box.call_args.args[0], str(Path(root)/"comfy-webhook"/"managed-outbox.sqlite"))
            install.assert_called_once_with(queue, box.return_value, "letsgen_callback_url", frozenset({"https://receiver.example/callback"}), "letsgen-input", "letsgen")
            response = asyncio.run(handlers[("GET", "/letsgen/capabilities")](None))
            self.assertEqual(json.loads(response.body), {"callback": 1, "cleanup_ack": 1, "outbox_recovery": 2})

    def test_enabled_mode_requires_callback_allowlist(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "callback allowlist"):
                start(None, root, root, {"COMFY_WEBHOOK_MANAGED_ENABLED": "1"}, user_directory=root)

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

    def test_signature_capability_is_advertised_when_configured(self):
        handlers = {}
        def route(method):
            def register(path):
                def decorate(handler):
                    handlers[(method, path)] = handler
                    return handler
                return decorate
            return register
        server = SimpleNamespace(routes=SimpleNamespace(get=route('GET'), post=route('POST'), delete=route('DELETE')),
                                 prompt_queue=SimpleNamespace(delete_history_item=lambda _: None))
        thread = SimpleNamespace(is_alive=lambda: True)
        with tempfile.TemporaryDirectory() as root, patch('extension.managed.bootstrap.Outbox') as box, \
                patch('extension.managed.bootstrap.install_queue_hook'):
            box.return_value.start.return_value = (None, thread)
            env = {'COMFY_WEBHOOK_MANAGED_OUTBOX_PATH': str(Path(root)/'events.sqlite'),
                   'COMFY_WEBHOOK_MANAGED_CALLBACKS': 'https://receiver.example/callback',
                   'COMFY_WEBHOOK_MANAGED_SIGNING_KEY': 'a' * 32}
            start(server, root, root, env)
            self.assertEqual(box.call_args.kwargs['signing_key'], 'a' * 32)
            response = asyncio.run(handlers[('GET', '/webhook-managed/capabilities')](None))
            capability = json.loads(response.body)
            self.assertEqual(capability['callback_signature'], 1)
            self.assertEqual(capability['callback_field'], 'managed_webhook_url')
            self.assertEqual(capability['input_prefix'], 'comfy-input')
            self.assertEqual(capability['output_prefix'], 'comfy')
            self.assertEqual(capability['staged_cleanup'], 1)
            self.assertEqual(capability['callback_url_hashes'], [hashlib.sha256(b'https://receiver.example/callback').hexdigest()])
            self.assertIn(('POST', '/webhook-managed/staged/{prompt_id}/cleanup'), handlers)

    def test_staged_cleanup_requires_signature_and_reads_full_chunked_body(self):
        handlers = {}
        def route(method):
            def register(path):
                def decorate(handler):
                    handlers[(method, path)] = handler
                    return handler
                return decorate
            return register
        server = SimpleNamespace(routes=SimpleNamespace(get=route('GET'), post=route('POST'), delete=route('DELETE')),
                                 prompt_queue=SimpleNamespace(delete_history_item=lambda _: None))
        class Content:
            def __init__(self, chunks): self.chunks = chunks
            async def iter_chunked(self, _size):
                for chunk in self.chunks: yield chunk
        thread = SimpleNamespace(is_alive=lambda: True)
        with tempfile.TemporaryDirectory() as root, patch('extension.managed.bootstrap.Outbox') as box, \
                patch('extension.managed.bootstrap.install_queue_hook'):
            box.return_value.start.return_value = (None, thread)
            box.return_value.cleanup_staged.return_value = True
            key='a'*32
            env={'COMFY_WEBHOOK_MANAGED_OUTBOX_PATH':str(Path(root)/'events.sqlite'),
                 'COMFY_WEBHOOK_MANAGED_CALLBACKS':'https://receiver.example/callback',
                 'COMFY_WEBHOOK_MANAGED_SIGNING_KEY':key}
            start(server,root,root,env)
            handler=handlers[('POST','/webhook-managed/staged/{prompt_id}/cleanup')]
            body=b'{"files":[]}'
            signature='sha256='+hmac.new(key.encode(),body,hashlib.sha256).hexdigest()
            def request(chunks, header):
                return SimpleNamespace(content_length=None,content=Content(chunks),headers=header,
                                       match_info={'prompt_id':'00000000-0000-4000-8000-000000000001'})
            self.assertEqual(asyncio.run(handler(request([body],{}))).status,401)
            self.assertEqual(asyncio.run(handler(request([body],{'X-Comfy-Webhook-Signature':'sha256='+'0'*64}))).status,401)
            self.assertEqual(asyncio.run(handler(request([body[:4],body[4:]],{'X-Comfy-Webhook-Signature':signature}))).status,200)
            self.assertEqual(asyncio.run(handler(request([body,b'x'*4096],{'X-Comfy-Webhook-Signature':signature}))).status,413)
            self.assertEqual(box.return_value.cleanup_staged.call_count,1)

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
