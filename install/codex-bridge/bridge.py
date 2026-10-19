#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp==3.14.1"]
# ///
"""Local-only Codex bridge experiment with a deterministic Responses provider.

Run demo.sh for an isolated stock Codex TUI with an eight-second provider wait.
Hook lifecycle notifications go only to the TUI, never back to app-server.
This proves the UI path; production attribution still requires explicit router
dispatch, first-output, error, and cancellation signals rather than a timer.
The hook notifications are an experimental reuse of Codex 0.163.0's protocol.
"""

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import time
import uuid

from aiohttp import WSMsgType, web


WAITING_MESSAGE = "Waiting on upstream provider.."
DEMO_ANSWER = "OK."
PROTOTYPE_ROOT = Path(__file__).resolve().parent


class Method(StrEnum):
    TURN_STARTED = "turn/started"
    TURN_COMPLETED = "turn/completed"
    HOOK_STARTED = "hook/started"
    HOOK_COMPLETED = "hook/completed"


class BridgeEvent(StrEnum):
    PROVIDER_WAIT_STARTED = "provider_wait_started"
    PROVIDER_WAIT_COMPLETED = "provider_wait_completed"
    TURN_COMPLETED = "turn_completed"
    PROVIDER_RECEIVED = "demo_provider_received"
    PROVIDER_COMPLETED = "demo_provider_completed"
    PROVIDER_CANCELLED = "demo_provider_cancelled"


class ResponseEvent(StrEnum):
    CREATED = "response.created"
    OUTPUT_ITEM_ADDED = "response.output_item.added"
    CONTENT_PART_ADDED = "response.content_part.added"
    OUTPUT_TEXT_DELTA = "response.output_text.delta"
    OUTPUT_TEXT_DONE = "response.output_text.done"
    CONTENT_PART_DONE = "response.content_part.done"
    OUTPUT_ITEM_DONE = "response.output_item.done"
    COMPLETED = "response.completed"


class ResponseStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ResponseItem(StrEnum):
    RESPONSE = "response"
    MESSAGE = "message"
    OUTPUT_TEXT = "output_text"


class MessageRole(StrEnum):
    ASSISTANT = "assistant"


# Values follow the HookRunSummary schema generated from Codex 0.163.0.
class HookStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"


class HookEvent(StrEnum):
    USER_PROMPT_SUBMIT = "userPromptSubmit"


class HookMode(StrEnum):
    SYNC = "sync"


class HookHandler(StrEnum):
    COMMAND = "command"


class HookScope(StrEnum):
    TURN = "turn"


class HookSource(StrEnum):
    PLUGIN = "plugin"


@dataclass
class ActiveTurn:
    thread_id: str
    turn_id: str
    hook_run: dict | None = None


class DemoBridge:
    def __init__(self, codex: str, state_dir: Path, delay: float):
        self.codex = codex
        self.state_dir = state_dir
        self.delay = delay
        self.websocket = None
        self.active_turns = {}
        self.event_log = []
        self.port = None

    def record(self, event, **fields):
        self.event_log.append({"event": event, "time": time.time(), **fields})
        (self.state_dir / "events.json").write_text(
            json.dumps(self.event_log, indent=2) + "\n"
        )

    def configure(self):
        codex_home = self.state_dir / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text(f"""model = "gpt-6.1-sol"
model_provider = "bridge_demo"
model_reasoning_effort = "low"
approval_policy = "never"
sandbox_mode = "read-only"
check_for_update_on_startup = false
suppress_unstable_features_warning = true
cli_auth_credentials_store = "file"
web_search = "disabled"

[model_providers.bridge_demo]
name = "Local bridge demo provider"
base_url = "http://127.0.0.1:{self.port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false

[features]
hooks = true
apps = false
plugins = false
memories = false

[analytics]
enabled = false

[feedback]
enabled = false
""")

    def child_environment(self):
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.state_dir / "codex-home")
        # The demo has no auth and must never inherit production provider credentials.
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "CODEX_API_KEY",
            "WEAVE_API_KEY",
        ):
            environment.pop(name, None)
        return environment

    async def send(self, method, params):
        if self.websocket is not None and not self.websocket.closed:
            await self.websocket.send_json({"method": method, "params": params})

    async def begin_provider_wait(self, turn):
        if turn.hook_run is not None:
            await self.end_provider_wait(turn)
        turn.hook_run = {
            "id": "weave-upstream-" + uuid.uuid4().hex,
            "eventName": HookEvent.USER_PROMPT_SUBMIT,
            "handlerType": HookHandler.COMMAND,
            "executionMode": HookMode.SYNC,
            "scope": HookScope.TURN,
            "source": HookSource.PLUGIN,
            "sourcePath": str(Path(__file__).resolve()),
            "displayOrder": 0,
            "startedAt": int(time.time() * 1000),
            "status": HookStatus.RUNNING,
            "statusMessage": WAITING_MESSAGE,
            "entries": [],
        }
        await self.send(Method.HOOK_STARTED, self.hook_params(turn))
        self.record(
            BridgeEvent.PROVIDER_WAIT_STARTED,
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
        )

    def hook_params(self, turn):
        return {
            "threadId": turn.thread_id,
            "turnId": turn.turn_id,
            "run": turn.hook_run,
        }

    async def end_provider_wait(self, turn):
        if turn.hook_run is None:
            return
        hook = turn.hook_run
        hook["status"] = HookStatus.COMPLETED
        hook["completedAt"] = int(time.time() * 1000)
        hook["durationMs"] = hook["completedAt"] - hook["startedAt"]
        hook["statusMessage"] = None
        await self.send(Method.HOOK_COMPLETED, self.hook_params(turn))
        self.record(
            BridgeEvent.PROVIDER_WAIT_COMPLETED,
            thread_id=turn.thread_id,
            duration_ms=hook["durationMs"],
        )
        turn.hook_run = None

    async def upstream_to_tui(self, process, websocket):
        pending = bytearray()
        while chunk := await process.stdout.read(65536):
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, tail = pending.partition(b"\n")
                pending = bytearray(tail)
                notification = json.loads(line)
                if notification.get("method") == Method.TURN_STARTED:
                    params = notification["params"]
                    self.active_turns[params["threadId"]] = ActiveTurn(
                        params["threadId"], params["turn"]["id"]
                    )
                elif notification.get("method") == Method.TURN_COMPLETED:
                    params = notification["params"]
                    turn = self.active_turns.pop(params["threadId"], None)
                    if turn is not None:
                        await self.end_provider_wait(turn)
                    self.record(
                        BridgeEvent.TURN_COMPLETED,
                        thread_id=params["threadId"],
                        status=params["turn"]["status"],
                    )
                await websocket.send_str(line.decode())

    async def connect_tui(self, request):
        # Match Codex's own loopback listener: a website must not drive app-server.
        if "Origin" in request.headers:
            raise web.HTTPForbidden()
        if self.websocket is not None:
            raise web.HTTPConflict(
                text="This isolated prototype supports one TUI connection at a time."
            )
        websocket = web.WebSocketResponse(max_msg_size=0)
        await websocket.prepare(request)
        self.websocket = websocket
        with (self.state_dir / "app-server.stderr.log").open("ab") as stderr:
            process = await asyncio.create_subprocess_exec(
                self.codex,
                "app-server",
                "--listen",
                "stdio://",
                cwd=self.state_dir,
                env=self.child_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
            )
            forwarding = asyncio.create_task(self.upstream_to_tui(process, websocket))
            try:
                async for message in websocket:
                    if message.type == WSMsgType.TEXT:
                        process.stdin.write(message.data.encode() + b"\n")
                        await process.stdin.drain()
                    elif message.type == WSMsgType.ERROR:
                        raise websocket.exception()
            finally:
                forwarding.cancel()
                with suppress(asyncio.CancelledError):
                    await forwarding
                self.websocket = None
                self.active_turns.clear()
                if process.returncode is None:
                    process.terminate()
                await process.wait()
        return websocket

    async def responses(self, request):
        body = await request.json()
        thread_id = request.headers.get("thread-id")
        self.record(
            BridgeEvent.PROVIDER_RECEIVED,
            model=body.get("model"),
            thread_id=thread_id,
            header_names=sorted(request.headers),
        )
        turn = self.active_turns[thread_id]
        await self.begin_provider_wait(turn)
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        response_id = "resp_demo_" + uuid.uuid4().hex
        message_id = "msg_demo_" + uuid.uuid4().hex
        response_body = {
            "id": response_id,
            "object": ResponseItem.RESPONSE,
            "created_at": int(time.time()),
            "status": ResponseStatus.IN_PROGRESS,
            "model": body.get("model"),
            "output": [],
        }
        sequence_number = 0

        async def emit(event_type, **payload):
            nonlocal sequence_number
            event = {"type": event_type, "sequence_number": sequence_number, **payload}
            sequence_number += 1
            await response.write(
                (f"event: {event_type}\ndata: {json.dumps(event)}\n\n").encode()
            )

        try:
            # A provider can acknowledge a response well before producing useful output.
            await emit(ResponseEvent.CREATED, response=response_body)
            await asyncio.sleep(self.delay)
            await self.end_provider_wait(turn)
            message = {
                "id": message_id,
                "type": ResponseItem.MESSAGE,
                "role": MessageRole.ASSISTANT,
                "status": ResponseStatus.IN_PROGRESS,
                "content": [],
            }
            await emit(ResponseEvent.OUTPUT_ITEM_ADDED, output_index=0, item=message)
            await emit(
                ResponseEvent.CONTENT_PART_ADDED,
                item_id=message_id,
                output_index=0,
                content_index=0,
                part={"type": ResponseItem.OUTPUT_TEXT, "text": "", "annotations": []},
            )
            for word in DEMO_ANSWER.split():
                await emit(
                    ResponseEvent.OUTPUT_TEXT_DELTA,
                    item_id=message_id,
                    output_index=0,
                    content_index=0,
                    delta=word + " ",
                )
                await asyncio.sleep(0.06)
            await emit(
                ResponseEvent.OUTPUT_TEXT_DONE,
                item_id=message_id,
                output_index=0,
                content_index=0,
                text=DEMO_ANSWER,
            )
            part = {
                "type": ResponseItem.OUTPUT_TEXT,
                "text": DEMO_ANSWER,
                "annotations": [],
            }
            await emit(
                ResponseEvent.CONTENT_PART_DONE,
                item_id=message_id,
                output_index=0,
                content_index=0,
                part=part,
            )
            message.update(status=ResponseStatus.COMPLETED, content=[part])
            await emit(ResponseEvent.OUTPUT_ITEM_DONE, output_index=0, item=message)
            response_body.update(
                status=ResponseStatus.COMPLETED,
                output=[message],
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
            await emit(ResponseEvent.COMPLETED, response=response_body)
            await response.write_eof()
            self.record(BridgeEvent.PROVIDER_COMPLETED)
        except (ConnectionResetError, asyncio.CancelledError):
            await self.end_provider_wait(turn)
            self.record(BridgeEvent.PROVIDER_CANCELLED)
        return response

    async def health(self, request):
        return web.json_response(
            {"mode": "local-fake-provider", "delay_seconds": self.delay}
        )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--delay", type=float, default=8)
    parser.add_argument("--state-dir", type=Path, default=PROTOTYPE_ROOT / "runtime")
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch the stock Codex terminal UI after starting the bridge",
    )
    args = parser.parse_args()
    if args.codex is None:
        parser.error("codex must be installed, or pass --codex /absolute/path/to/codex")
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge = DemoBridge(args.codex, state_dir, args.delay)
    application = web.Application(client_max_size=0)
    application.add_routes(
        [
            web.get("/", bridge.connect_tui),
            web.post("/v1/responses", bridge.responses),
            web.get("/healthz", bridge.health),
        ]
    )
    runner = web.AppRunner(application, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    bridge.port = runner.addresses[0][1]
    bridge.configure()
    endpoint = f"ws://127.0.0.1:{bridge.port}"
    (state_dir / "endpoint.json").write_text(
        json.dumps({"endpoint": endpoint, "codex_home": str(state_dir / "codex-home")})
        + "\n"
    )
    command = [
        args.codex,
        "--remote",
        endpoint,
        "--no-alt-screen",
        "Run the local provider waiting-indicator demo.",
    ]
    try:
        if args.tui:
            tui = await asyncio.create_subprocess_exec(
                *command, cwd=state_dir, env=bridge.child_environment()
            )
            await tui.wait()
        else:
            print(
                f"Local fake provider: {args.delay:g}s delay, zero inference calls.\n",
                flush=True,
            )
            print(
                f"Connect from another terminal:\nenv CODEX_HOME={shlex.quote(str(state_dir / 'codex-home'))} {shlex.join(command)}",
                flush=True,
            )
            stop = asyncio.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(signum, stop.set)
            await stop.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
