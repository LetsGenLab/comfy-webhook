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

    def test_enabled_plugin_registers_managed_delivery_without_callback_configuration(self):
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
            start(server, root, root, {"COMFY_WEBHOOK_MANAGED_ENABLED": "1"}, user_directory=root)
            self.assertEqual(box.call_args.args[0], str(Path(root)/"comfy-webhook"/"managed-outbox.sqlite"))
            install.assert_called_once_with(queue, box.return_value, "letsgen_callback_url", "letsgen-input", "letsgen")
            response = asyncio.run(handlers[("GET", "/letsgen/capabilities")](None))
            self.assertEqual(json.loads(response.body), {"callback": 1, "cleanup_ack": 1, "outbox_recovery": 2})

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
                   "COMFY_WEBHOOK_MANAGED_INPUT_PREFIX": "example-input",
                   "COMFY_WEBHOOK_MANAGED_OUTPUT_PREFIX": "example"}
            start(server, root, root, env)
            install.assert_called_once_with(queue, box.return_value, "job_callback",
                "example-input", "example")
            response = asyncio.run(handlers[("GET", "/adapter/capabilities")](None))
            self.assertEqual(json.loads(response.body), {"callback": 1, "cleanup_ack": 1, "outbox_recovery": 2})
            self.assertIn(("POST", "/adapter/outbox/{prompt_id}/ack"), handlers)
            self.assertIn(("DELETE", "/adapter/outbox/{prompt_id}/ack"), handlers)

    def test_requires_separate_callback_field_and_valid_api_prefix(self):
        base = {"COMFY_WEBHOOK_MANAGED_OUTBOX_PATH": "/tmp/private.sqlite"}
        invalid = [{**base, "COMFY_WEBHOOK_MANAGED_CALLBACK_FIELD": "webhook_url"},
                   {**base, "COMFY_WEBHOOK_MANAGED_API_PREFIX": "/../../unsafe"}]
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
            self.assertEqual(capability['callback_url_hashes'], [])
            self.assertEqual(capability['callback_url_from_job'], 1)
            self.assertIn(('POST', '/webhook-managed/staged/{prompt_id}/cleanup'), handlers)

    def test_machine_api_token_signs_callbacks_without_destination_configuration(self):
        handlers = {}
        def register(path):
            def decorate(handler):
                handlers[path] = handler
                return handler
            return decorate
        server = SimpleNamespace(routes=SimpleNamespace(get=register, post=register, delete=register),
                                 prompt_queue=SimpleNamespace(delete_history_item=lambda _: None))
        with tempfile.TemporaryDirectory() as root, patch('extension.managed.bootstrap.Outbox') as box, \
                patch('extension.managed.bootstrap.install_queue_hook'):
            box.return_value.start.return_value = (None, SimpleNamespace(is_alive=lambda: True))
            token = 'p' * 64
            start(server, root, root, {'COMFY_WEBHOOK_MANAGED_ENABLED': '1', 'COMFY_WEBHOOK_API_TOKEN': token},
                  user_directory=root)
            self.assertEqual(box.call_args.kwargs['signing_key'], token)
            self.assertTrue(box.call_args.kwargs['api_token_signing'])
            result = json.loads(asyncio.run(handlers['/letsgen/capabilities'](None)).body)
            self.assertEqual(result['callback_api_token'], 1)
            self.assertEqual(result['callback_url_hashes'], [])

    def test_signing_overrides_keep_legacy_capability_hashes(self):
        callbacks = ['https://staging.example/callback', 'https://prod.example/callback']
        keys = {callbacks[0]: 's' * 32, callbacks[1]: 'p' * 32}
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
        with tempfile.TemporaryDirectory() as root, patch('extension.managed.bootstrap.Outbox') as box, \
                patch('extension.managed.bootstrap.install_queue_hook'):
            box.return_value.start.return_value = (None, SimpleNamespace(is_alive=lambda: True))
            env = {'COMFY_WEBHOOK_MANAGED_ENABLED': '1',
                   'COMFY_WEBHOOK_MANAGED_SIGNING_KEYS': json.dumps(keys)}
            start(server, root, root, env, user_directory=root)
            self.assertEqual(box.call_args.kwargs['signing_keys'], keys)
            capability = json.loads(asyncio.run(handlers[('GET', '/letsgen/capabilities')](None)).body)
            self.assertEqual(capability['callback_signature'], 1)
            self.assertEqual(capability['callback_url_hashes'], sorted(hashlib.sha256(x.encode()).hexdigest() for x in callbacks))
            self.assertNotIn('callback_url_from_job', capability)
            start(server, root, root, {**env, 'COMFY_WEBHOOK_MANAGED_SIGNING_KEY': 'a' * 32}, user_directory=root)
            capability = json.loads(asyncio.run(handlers[('GET', '/letsgen/capabilities')](None)).body)
            self.assertEqual(capability['callback_url_from_job'], 1)

    def test_multi_receiver_staged_cleanup_accepts_either_key(self):
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
            async def iter_chunked(self, _size):
                yield b'{"files":[]}'
        with tempfile.TemporaryDirectory() as root, patch('extension.managed.bootstrap.Outbox') as box, \
                patch('extension.managed.bootstrap.install_queue_hook'):
            box.return_value.start.return_value = (None, SimpleNamespace(is_alive=lambda: True))
            box.return_value.cleanup_staged.return_value = True
            callbacks = ['https://staging.example/callback', 'https://prod.example/callback']
            keys = ['s' * 32, 'p' * 32]
            default_key = 'a' * 32
            env = {'COMFY_WEBHOOK_MANAGED_ENABLED':'1',
                   'COMFY_WEBHOOK_MANAGED_SIGNING_KEYS':json.dumps(dict(zip(callbacks, keys))),
                   'COMFY_WEBHOOK_MANAGED_SIGNING_KEY':default_key}
            start(server, root, root, env, user_directory=root)
            handler = handlers[('POST', '/letsgen/staged/{prompt_id}/cleanup')]
            def check(key):
                signature = 'sha256=' + hmac.new(key.encode(), b'{"files":[]}', hashlib.sha256).hexdigest()
                request = SimpleNamespace(content_length=None, content=Content(),
                    headers={'X-Comfy-Webhook-Signature': signature}, match_info={'prompt_id':'00000000-0000-4000-8000-000000000001'})
                return asyncio.run(handler(request)).status
            self.assertEqual([check(key) for key in [*keys, default_key]], [200, 200, 200])
            self.assertEqual(check('x' * 32), 401)

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
        install_managed(queue, Managed(), "managed_url", "input", "output")
        status = SimpleNamespace(status_str="success", messages=[])
        queue.currently_running[1] = (0, "job-1", {}, {"managed_url": "https://receiver.example/hook"})
        self.assertEqual(queue.task_done(1, {}, status), "done")
        self.assertEqual(emitted, [("managed", "https://receiver.example/hook", "succeeded")])
        emitted.clear()
        queue.currently_running[2] = (0, "job-2", {}, {"webhook_url": "https://other.example/hook"})
        self.assertEqual(queue.task_done(2, {}, status), "done")
        self.assertEqual(emitted, [("generic", "https://other.example/hook", "succeeded")])
