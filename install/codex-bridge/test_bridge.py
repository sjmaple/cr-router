import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from bridge import ActiveTurn, DemoBridge, HookStatus, Method, ResponseEvent


class RecordingWebSocket:
    closed = False

    def __init__(self):
        self.notifications = []

    async def send_json(self, notification):
        self.notifications.append(json.loads(json.dumps(notification)))


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.bridge = DemoBridge("unused-codex", Path(self.state.name), delay=0)
        self.socket = RecordingWebSocket()
        self.bridge.websocket = self.socket

    async def test_wait_notifications_have_only_transient_text(self):
        turn = ActiveTurn("thread-1", "turn-1")
        await self.bridge.begin_provider_wait(turn)
        await self.bridge.end_provider_wait(turn)
        await self.bridge.end_provider_wait(turn)

        self.assertEqual(
            [notification["method"] for notification in self.socket.notifications],
            [Method.HOOK_STARTED, Method.HOOK_COMPLETED],
        )
        started, completed = [
            notification["params"]["run"] for notification in self.socket.notifications
        ]
        self.assertEqual(started["status"], HookStatus.RUNNING)
        self.assertEqual(started["statusMessage"], "Waiting on upstream provider..")
        self.assertEqual(started["entries"], [])
        self.assertEqual(completed["status"], HookStatus.COMPLETED)
        self.assertIsNone(completed["statusMessage"])
        self.assertEqual(completed["entries"], [])
        self.assertEqual(started["id"], completed["id"])

    async def test_background_thread_does_not_clear_foreground_wait(self):
        foreground = ActiveTurn("foreground", "turn-1")
        background = ActiveTurn("background", "turn-2")
        await self.bridge.begin_provider_wait(foreground)
        await self.bridge.begin_provider_wait(background)
        await self.bridge.end_provider_wait(background)

        self.assertEqual(foreground.hook_run["status"], HookStatus.RUNNING)
        self.assertIsNone(background.hook_run)
        self.assertEqual(
            self.socket.notifications[-1]["params"]["threadId"], "background"
        )

    async def test_provider_reply_is_unchanged_and_wait_clears_before_output(self):
        turn = ActiveTurn("foreground", "turn-1")
        self.bridge.active_turns[turn.thread_id] = turn
        application = web.Application()
        application.router.add_post("/v1/responses", self.bridge.responses)

        async with TestClient(TestServer(application)) as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "fixture-model", "stream": True},
                headers={"thread-id": turn.thread_id},
            )
            self.assertEqual(response.status, 200)
            output = []
            async for line in response.content:
                if not line.startswith(b"data: "):
                    continue
                event = json.loads(line[6:])
                if event["type"] == ResponseEvent.OUTPUT_TEXT_DELTA:
                    output.append(event["delta"])
                    self.assertEqual(
                        self.socket.notifications[-1]["method"], Method.HOOK_COMPLETED
                    )
                    self.assertIsNone(
                        self.socket.notifications[-1]["params"]["run"]["statusMessage"]
                    )
            self.assertEqual("".join(output).strip(), "OK.")

    async def test_browser_cannot_connect_to_app_server(self):
        self.bridge.websocket = None
        application = web.Application()
        application.router.add_get("/", self.bridge.connect_tui)
        async with TestClient(TestServer(application)) as client:
            with self.assertRaises(WSServerHandshakeError) as rejected:
                await client.ws_connect(
                    "/", headers={"Origin": "https://example.invalid"}
                )
            self.assertEqual(rejected.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
