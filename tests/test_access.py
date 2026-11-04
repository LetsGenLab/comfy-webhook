import asyncio
import base64
import unittest
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from extension.access import install


class AccessTests(unittest.TestCase):
    def test_unconfigured_does_not_change_existing_server(self):
        server = SimpleNamespace(app=SimpleNamespace(middlewares=[]))
        self.assertFalse(install(server, {}))
        self.assertEqual(server.app.middlewares, [])

    def test_authenticates_api_and_browser_and_rejects_other_requests(self):
        server = SimpleNamespace(app=SimpleNamespace(middlewares=[]))
        token = "x" * 32
        self.assertTrue(install(server, {"COMFY_WEBHOOK_API_TOKEN": token}))
        middleware = server.app.middlewares[0]
        calls = []
        async def handler(_request):
            calls.append(True)
            return "ok"
        def request(header):
            return SimpleNamespace(headers={"Authorization": header} if header else {})
        basic = base64.b64encode(("comfy:" + token).encode()).decode()
        for header in ("", "Bearer incorrect", "Basic invalid", "Basic " + base64.b64encode(("wrong:" + token).encode()).decode()):
            self.assertEqual(asyncio.run(middleware(request(header), handler)).status, 401)
        self.assertEqual(calls, [])
        for header in ("Bearer " + token, "Basic " + basic):
            self.assertEqual(asyncio.run(middleware(request(header), handler)), "ok")
        self.assertEqual(len(calls), 2)

    def test_rejects_weak_or_multiline_tokens(self):
        for token in ("short", "x" * 32 + "\n"):
            server = SimpleNamespace(app=SimpleNamespace(middlewares=[]))
            with self.assertLogs("comfy-webhook", level="ERROR"):
                self.assertTrue(install(server, {"COMFY_WEBHOOK_API_TOKEN": token}))
            async def handler(_request): return "unexpected"
            result = asyncio.run(server.app.middlewares[0](SimpleNamespace(headers={"Authorization": "Bearer " + token}), handler))
            self.assertEqual(result.status, 401)

    def test_real_server_rejects_unauthenticated_requests(self):
        async def exercise():
            app = web.Application()
            install(SimpleNamespace(app=app), {"COMFY_WEBHOOK_API_TOKEN": "x" * 32})
            async def response(_request): return web.json_response({"ok": True})
            app.router.add_get("/system_stats", response)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                self.assertEqual((await client.get("/system_stats")).status, 401)
                self.assertEqual((await client.get("/system_stats", headers={"Authorization": "Bearer " + "x" * 32})).status, 200)
            finally:
                await client.close()
        asyncio.run(exercise())
