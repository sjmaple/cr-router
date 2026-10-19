#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp==3.14.1", "pyte==0.8.2"]
# ///
"""Drive a stock Codex PTY and assert visible waiting/clearing and transcript behavior."""

import argparse
import codecs
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time

import pyte

from bridge import DEMO_ANSWER, WAITING_MESSAGE, BridgeEvent

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument(
        "--interrupt",
        action="store_true",
        help="Interrupt the turn while the provider is pending",
    )
    args = parser.parse_args()
    evidence = Path(tempfile.mkdtemp(prefix="tui-check-", dir=ROOT))
    bridge_output = (evidence / "bridge.log").open("wb")
    server = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "bridge.py"),
            "--codex",
            args.codex,
            "--state-dir",
            str(evidence),
            "--delay",
            "5",
        ],
        stdout=bridge_output,
        stderr=subprocess.STDOUT,
    )
    tui_pid = None
    descriptor = None
    try:
        deadline = time.monotonic() + 45
        while not (evidence / "endpoint.json").exists():
            if server.poll() is not None:
                raise RuntimeError((evidence / "bridge.log").read_text())
            if time.monotonic() >= deadline:
                raise TimeoutError("Local bridge did not start")
            time.sleep(0.05)
        connection = json.loads((evidence / "endpoint.json").read_text())
        tui_pid, descriptor = pty.fork()
        if tui_pid == 0:
            os.chdir(evidence)
            environment = dict(
                os.environ, TERM="xterm-256color", CODEX_HOME=connection["codex_home"]
            )
            os.execve(
                args.codex,
                [
                    args.codex,
                    "--remote",
                    connection["endpoint"],
                    "--no-alt-screen",
                    "Run the local provider waiting-indicator demo.",
                ],
                environment,
            )
        fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        screen = pyte.Screen(140, 40)
        stream = pyte.Stream(screen)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        transcript_requested_at = None
        completion_seen_at = None
        waiting_seen = False
        streaming_seen = False
        waiting_frames = set()
        streaming_screen = ""
        final_screen = ""
        raw_output = bytearray()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if select.select([descriptor], [], [], 0.1)[0]:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                raw_output.extend(chunk)
                stream.feed(decoder.decode(chunk))
                if b"\x1b[6n" in chunk:
                    os.write(descriptor, b"\x1b[1;1R")
            visible = "\n".join(line.rstrip() for line in screen.display)
            for line in screen.display:
                if WAITING_MESSAGE in line:
                    waiting_frames.add(line[:2])
            if WAITING_MESSAGE in visible and not waiting_seen:
                waiting_seen = True
                (evidence / "waiting-screen.txt").write_text(visible + "\n")
                if args.interrupt:
                    os.write(descriptor, b"\x1b")
            if DEMO_ANSWER in visible and not streaming_seen:
                streaming_seen = True
                streaming_screen = visible
                (evidence / "streaming-screen.txt").write_text(visible + "\n")
            events_path = evidence / "events.json"
            if events_path.exists():
                events = json.loads(events_path.read_text())
                if any(
                    event["event"] == BridgeEvent.TURN_COMPLETED for event in events
                ):
                    completion_seen_at = completion_seen_at or time.monotonic()
            if (
                completion_seen_at is not None
                and time.monotonic() - completion_seen_at > 1
            ):
                if transcript_requested_at is None:
                    final_screen = visible
                    (evidence / "completed-screen.txt").write_text(visible + "\n")
                    os.write(descriptor, b"\x14")
                    transcript_requested_at = time.monotonic()
                elif time.monotonic() - transcript_requested_at > 1:
                    (evidence / "transcript-screen.txt").write_text(visible + "\n")
                    break
        (evidence / "tui.ansi").write_bytes(raw_output)
        transcript = (
            (evidence / "transcript-screen.txt").read_text()
            if transcript_requested_at
            else ""
        )
        with sqlite3.connect(
            f"file:{evidence / 'codex-home' / 'thread_history_1.sqlite'}?mode=ro",
            uri=True,
        ) as history:
            persisted_items = [
                json.loads(row[0])
                for row in history.execute("SELECT item_json FROM thread_items")
            ]
        (evidence / "persisted-items.json").write_text(
            json.dumps(persisted_items, indent=2) + "\n"
        )
        summary = {
            "codex_version": subprocess.check_output(
                [args.codex, "--version"], text=True
            ).strip(),
            "waiting_visible": waiting_seen,
            "waiting_indicator_animated": len(waiting_frames) > 1,
            "response_streamed": streaming_seen,
            "waiting_absent_when_streaming_starts": bool(streaming_screen)
            and WAITING_MESSAGE not in streaming_screen,
            "waiting_absent_after_completion": WAITING_MESSAGE not in final_screen
            and bool(final_screen),
            "answer_visible_after_completion": DEMO_ANSWER in final_screen,
            "transcript_opened": "TRANSCRIPT" in transcript.replace(" ", "").upper(),
            "waiting_absent_from_transcript": WAITING_MESSAGE not in transcript
            and bool(transcript),
            "waiting_absent_from_persisted_history": bool(persisted_items)
            and WAITING_MESSAGE not in json.dumps(persisted_items),
            "answer_in_persisted_history": DEMO_ANSWER in json.dumps(persisted_items),
            "turn_interrupted": any(
                event["event"] == BridgeEvent.TURN_COMPLETED
                and event["status"] == "interrupted"
                for event in events
            ),
            "evidence_directory": str(evidence),
        }
        (evidence / "verification.json").write_text(
            json.dumps(summary, indent=2) + "\n"
        )
        print(json.dumps(summary, indent=2))
        assert summary["waiting_visible"], "Waiting status did not render"
        assert summary["waiting_absent_after_completion"], (
            "Waiting status remained after completion"
        )
        assert summary["transcript_opened"], "Transcript view did not open"
        assert summary["waiting_absent_from_transcript"], (
            "Waiting status appeared in the transcript"
        )
        assert summary["waiting_absent_from_persisted_history"], (
            "Waiting status was persisted as conversation content"
        )
        if args.interrupt:
            assert summary["turn_interrupted"], "The turn was not interrupted"
        else:
            assert summary["waiting_indicator_animated"], (
                "The waiting indicator did not animate"
            )
            assert summary["waiting_absent_when_streaming_starts"], (
                "Waiting status remained when the reply started"
            )
            assert summary["answer_visible_after_completion"], (
                "Response did not complete successfully"
            )
            assert summary["answer_in_persisted_history"], (
                "Completed response was not saved"
            )
    finally:
        # Close the PTY before waiting: a TUI can block flushing its exit redraw
        # when the parent is no longer draining terminal output.
        if descriptor is not None:
            os.close(descriptor)
        if tui_pid:
            try:
                os.kill(tui_pid, signal.SIGTERM)
                exit_deadline = time.monotonic() + 5
                while os.waitpid(tui_pid, os.WNOHANG)[0] == 0:
                    if time.monotonic() >= exit_deadline:
                        os.kill(tui_pid, signal.SIGKILL)
                        os.waitpid(tui_pid, 0)
                        break
                    time.sleep(0.05)
            except (ProcessLookupError, ChildProcessError):
                pass
        server.terminate()
        server.wait(timeout=10)
        bridge_output.close()


if __name__ == "__main__":
    main()
