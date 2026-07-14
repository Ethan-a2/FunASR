#!/usr/bin/env python3
"""Stream system audio to FunASR Runtime WebSocket and print live captions."""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import ssl as ssl_module
import subprocess
import sys
import time


DEFAULT_CHUNK_SIZE = "5,10,5"


@dataclass
class CaptionState:
    final_text: str = ""
    online_text: str = ""
    first_audio_send_at: float | None = None
    last_audio_send_at: float | None = None
    first_text_at: float | None = None


class JsonlWriter:
    def __init__(self, path: str | None):
        self.file = None
        if path:
            output_path = Path(path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self.file = output_path.open("a", encoding="utf-8")

    def write(self, event: dict):
        if not self.file:
            return
        self.file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.file.flush()

    def close(self):
        if self.file:
            self.file.close()


def parse_chunk_size(value: str) -> list[int]:
    try:
        parts = [int(part.strip()) for part in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError("chunk size must look like 5,10,5") from error
    if len(parts) != 3 or any(part <= 0 for part in parts):
        raise argparse.ArgumentTypeError("chunk size must contain three positive integers")
    return parts


def run_text(command: list[str]) -> str:
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def list_sources() -> int:
    return subprocess.run(["pactl", "list", "short", "sources"], check=False).returncode


def detect_monitor_source() -> str:
    try:
        default_sink = run_text(["pactl", "get-default-sink"])
        if default_sink:
            return f"{default_sink}.monitor"
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    try:
        sources = run_text(["pactl", "list", "short", "sources"])
    except FileNotFoundError as error:
        raise SystemExit("pactl not found; pass --audio-file or install PulseAudio/PipeWire tools") from error
    except subprocess.CalledProcessError as error:
        raise SystemExit("cannot query PulseAudio/PipeWire sources; pass --source explicitly") from error

    for line in sources.splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[1].endswith(".monitor"):
            return columns[1]
    raise SystemExit("no *.monitor source found; run `pactl list short sources` to inspect sources")


def load_hotwords(value: str) -> str:
    if not value:
        return ""
    if not os.path.exists(value):
        return value

    hotwords: dict[str, int] = {}
    with open(value, encoding="utf-8") as file:
        for line in file:
            parts = line.strip().split()
            if not parts:
                continue
            word = " ".join(parts[:-1]) if len(parts) > 1 else parts[0]
            try:
                weight = int(parts[-1]) if len(parts) > 1 else 20
            except ValueError:
                word = " ".join(parts)
                weight = 20
            hotwords[word] = weight
    return json.dumps(hotwords, ensure_ascii=False)


def build_ffmpeg_command(args: argparse.Namespace, source: str | None) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", args.ffmpeg_loglevel]
    if args.audio_file:
        if args.realtime_file:
            command.append("-re")
        command.extend(["-i", args.audio_file])
    else:
        command.extend(["-f", "pulse", "-i", source or detect_monitor_source()])

    command.extend(
        [
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(args.sample_rate),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "pipe:1",
        ]
    )
    return command


def update_caption_state(state: CaptionState, mode: str, text: str) -> tuple[str, str]:
    if mode in ("offline", "2pass-offline"):
        state.online_text = ""
        state.final_text += text
        return "final", state.final_text
    if mode in ("online", "2pass-online"):
        state.online_text += text
        return "online", state.final_text + state.online_text
    return mode or "message", state.final_text + text


def now_label() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def send_audio(websocket, process: subprocess.Popen, args: argparse.Namespace, state: CaptionState):
    frame_ms = 60 * args.chunk_size[1] / args.chunk_interval
    frame_bytes = max(2, int(args.sample_rate * 2 * frame_ms / 1000))
    frame_bytes -= frame_bytes % 2
    started_at = time.monotonic()

    try:
        while True:
            if args.duration and time.monotonic() - started_at >= args.duration:
                break
            chunk = await asyncio.to_thread(process.stdout.read, frame_bytes)
            if not chunk:
                break
            if state.first_audio_send_at is None:
                state.first_audio_send_at = time.time()
            state.last_audio_send_at = time.time()
            await websocket.send(chunk)
    finally:
        await websocket.send(json.dumps({"is_speaking": False}, ensure_ascii=False))


async def receive_captions(websocket, state: CaptionState, writer: JsonlWriter):
    while True:
        raw_message = await websocket.recv()
        received_at = time.time()
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            print(f"[{now_label()}] message {raw_message}", flush=True)
            continue

        text = message.get("text", "")
        mode = message.get("mode", "")
        latency_first_ms = None
        latency_last_ms = None

        if text and mode in ("online", "2pass-online") and state.first_text_at is None:
            state.first_text_at = received_at
            if state.first_audio_send_at is not None:
                latency_first_ms = (received_at - state.first_audio_send_at) * 1000
            if state.last_audio_send_at is not None:
                latency_last_ms = (received_at - state.last_audio_send_at) * 1000
            print(
                "[latency] first_text "
                f"from_first_chunk={latency_first_ms or 0:.1f}ms "
                f"from_last_chunk={latency_last_ms or 0:.1f}ms",
                flush=True,
            )

        caption_type, caption_text = update_caption_state(state, mode, text)
        if text:
            print(f"[{now_label()}] {caption_type:<6} {caption_text}", flush=True)

        writer.write(
            {
                "received_at": datetime.fromtimestamp(received_at).isoformat(timespec="milliseconds"),
                "mode": mode,
                "text": text,
                "caption_type": caption_type,
                "caption_text": caption_text,
                "is_final": bool(message.get("is_final", False)),
                "latency_first_ms": latency_first_ms,
                "latency_last_ms": latency_last_ms,
                "raw": message,
            }
        )


async def run_client(args: argparse.Namespace):
    try:
        import websockets
    except ImportError as error:
        raise SystemExit("Install WebSocket client dependency: pip install websockets") from error

    source = args.source or (None if args.audio_file else detect_monitor_source())
    ffmpeg_command = build_ffmpeg_command(args, source)
    print(f"[audio] {'file ' + args.audio_file if args.audio_file else 'source ' + source}")
    print(f"[ffmpeg] {shlex.join(ffmpeg_command)}")

    process = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, bufsize=0)
    if process.stdout is None:
        raise SystemExit("failed to open ffmpeg stdout")

    scheme = "wss" if args.ssl else "ws"
    uri = f"{scheme}://{args.host}:{args.port}"
    ssl_context = ssl_module.create_default_context() if args.ssl else None
    state = CaptionState()
    writer = JsonlWriter(args.jsonl)

    hello = {
        "mode": args.mode,
        "chunk_size": args.chunk_size,
        "chunk_interval": args.chunk_interval,
        "encoder_chunk_look_back": args.encoder_chunk_look_back,
        "decoder_chunk_look_back": args.decoder_chunk_look_back,
        "audio_fs": args.sample_rate,
        "wav_name": args.wav_name,
        "wav_format": "pcm",
        "is_speaking": True,
        "hotwords": load_hotwords(args.hotword),
        "itn": args.use_itn,
    }

    try:
        async with websockets.connect(uri, ssl=ssl_context, max_size=None) as websocket:
            print(f"[connect] {uri}")
            await websocket.send(json.dumps(hello, ensure_ascii=False))

            receiver = asyncio.create_task(receive_captions(websocket, state, writer))
            sender = asyncio.create_task(send_audio(websocket, process, args, state))
            await sender
            try:
                await asyncio.wait_for(receiver, timeout=args.final_wait)
            except asyncio.TimeoutError:
                receiver.cancel()
    finally:
        writer.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live caption system audio with FunASR WebSocket Runtime")
    parser.add_argument("--host", default="127.0.0.1", help="FunASR WebSocket host")
    parser.add_argument("--port", type=int, default=int(os.getenv("FUNASR_WS_HOST_PORT", "10096")))
    parser.add_argument("--ssl", action="store_true", help="Use wss:// instead of ws://")
    parser.add_argument("--mode", default="2pass", choices=("online", "offline", "2pass"))
    parser.add_argument("--source", help="PulseAudio/PipeWire monitor source, e.g. alsa_output...monitor")
    parser.add_argument("--list-sources", action="store_true", help="List PulseAudio/PipeWire sources and exit")
    parser.add_argument("--audio-file", help="Read an audio/video file through ffmpeg instead of system audio")
    parser.add_argument("--no-realtime-file", dest="realtime_file", action="store_false", help="Do not throttle --audio-file with ffmpeg -re")
    parser.set_defaults(realtime_file=True)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--chunk-size", type=parse_chunk_size, default=parse_chunk_size(DEFAULT_CHUNK_SIZE))
    parser.add_argument("--chunk-interval", type=int, default=10)
    parser.add_argument("--encoder-chunk-look-back", type=int, default=4)
    parser.add_argument("--decoder-chunk-look-back", type=int, default=0)
    parser.add_argument("--hotword", default="", help="Hotword string or hotword file path")
    parser.add_argument("--use-itn", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--wav-name", default="system-audio")
    parser.add_argument("--duration", type=float, help="Stop streaming after N seconds; omit for continuous captions")
    parser.add_argument("--final-wait", type=float, default=8.0, help="Seconds to wait for final 2pass text after streaming stops")
    parser.add_argument("--jsonl", help="Append received caption events to a JSONL file")
    parser.add_argument("--ffmpeg-loglevel", default="error", help="ffmpeg loglevel, e.g. error, warning, info")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.list_sources:
        return list_sources()
    asyncio.run(run_client(args))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[stop] interrupted", file=sys.stderr)
        raise SystemExit(130)
