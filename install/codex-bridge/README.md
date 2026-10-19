# Codex upstream wait bridge prototype

This local prototype adds **Waiting on upstream provider..** to the stock Codex
Working indicator while a fake provider is pending. The text clears before the
provider reply starts and on interruption. It does not enter the transcript or
persisted conversation. The fake provider replies with `OK.`; the indicator adds
no chat messages or completion notices.

Requires Codex CLI 0.163.0, Python 3.11+, and [uv](https://docs.astral.sh/uv/).
From the router checkout:

```sh
bash install/codex-bridge/demo.sh
```

The default delay is eight seconds. Use `--delay 15` to change it, or
`--codex /path/to/codex` to choose a binary. State, config and synthetic
conversation records stay under the ignored `runtime/` directory. The demo uses
no provider credentials and sends no inference requests outside localhost.
Exit Codex normally to stop the bridge.

The TUI connects with `codex --remote` to a loopback WebSocket bridge, which
forwards app-server JSON-RPC over stdio. The fake Responses endpoint is also local.
Codex's `thread-id` header associates each provider request with its thread,
including background threads. The bridge injects `hook/started` and
`hook/completed` notifications only on the client side, with empty output entries
and a temporary `statusMessage`. The running indicator remains Codex's own.

This is a UI feasibility prototype using an experimental reuse of hook lifecycle
notifications. It is not installed or activated by the normal Router installer.
Real traffic needs explicit router dispatch/first-output/error signals rather
than treating a client-side timer or request start as evidence of provider delay.
The fake provider supplies those boundaries for this demo. Claude Code can consume
the same future provider-wait signal through its own UI adapter; this prototype
only implements Codex.

## Verification

The protocol tests exercise thread isolation, empty hook output, response ordering,
and rejection of browser-origin WebSocket connections:

```sh
uv run --with aiohttp==3.14.1 python -m unittest discover -s install/codex-bridge -p 'test_*.py'
```

The optional macOS/Linux terminal checks require the Codex binary and launch only
the local fake provider. They inspect the rendered terminal, transcript view and
Codex's saved conversation items, and save evidence under ignored `tui-check-*/`
directories:

```sh
uv run --script install/codex-bridge/verify_tui.py
uv run --script install/codex-bridge/verify_tui.py --interrupt
```

See [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server)
for the remote TUI and hook notification protocol.
