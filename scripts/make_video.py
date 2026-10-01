#!/usr/bin/env python3
"""Compose a demo video: the terminal log next to the browser doing the work.

Run the agent with --record-video; Playwright records every browser (the main one
and each parallel worker) and `runs/<ts>/trace.jsonl` carries every tool call with
a timestamp. This script replays the trace as a terminal panel and stacks it beside
the recordings. While parallel workers run, the browser side becomes a grid of the
worker windows, so the parallelism is visible.

    python scripts/make_video.py runs/20260930-120000 -o demo.mp4
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATHS = [
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/consolab.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
BG = (14, 16, 22)
BG_HEX = "0x0e1016"
W, H = 1440, 900
CELL_W, CELL_H = 720, 450
COLOURS = {
    "task": (120, 210, 255),
    "tool": (240, 200, 90),
    "result": (135, 142, 160),
    "error": (240, 110, 110),
    "note": (200, 130, 240),
    "gate": (255, 160, 60),
    "sub": (110, 170, 255),
    "worker_start": (110, 200, 255),
    "worker_done": (130, 220, 180),
    "parallel_delegate": (180, 160, 255),
    "report": (120, 230, 150),
    "plain": (215, 220, 230),
}


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines() or [""]:
        while len(raw) > width:
            out.append(raw[:width])
            raw = raw[width:]
        out.append(raw)
    return out


def trace_to_lines(path: Path, cols: int) -> list[tuple[float, str, str]]:
    """Flatten the trace into (timestamp, colour-key, text) terminal lines."""
    lines: list[tuple[float, str, str]] = []

    def push(t: float, kind: str, text: str, indent: str = "") -> None:
        for chunk in wrap(text, cols - len(indent)):
            lines.append((t, kind, indent + chunk))

    for record in (json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()):
        t, kind = record["t"], record["kind"]
        if kind == "task":
            push(t, "task", f"task> {record['task']}")
            push(t, "result", f"model {record.get('model', '')} · {record.get('url', '')}")
        elif kind == "tool_call":
            args = json.dumps(record.get("args", {}), ensure_ascii=False)
            push(t, "tool", f"▸ {record['tool']}({args[:200]})")
        elif kind == "tool_result":
            body = [
                line
                for line in (record.get("content") or "").strip().splitlines()
                if line.strip() and not line.lstrip().startswith(("<page_content", "</page_content"))
            ]
            for line in body[:3]:
                push(t, "error" if record.get("error") else "result", "  " + line[: cols - 4])
        elif kind == "gate":
            verdict = "allowed" if record.get("allowed") else "REFUSED"
            push(
                t,
                "gate",
                f"⚠ safety gate [{record.get('risk')}] {record.get('reason', '')} → {verdict}",
            )
        elif kind == "compaction":
            push(t, "note", f"• context compacted {record['before']} → {record['after']} tokens")
        elif kind == "subagent_start":
            push(t, "sub", f"│ sub-agent: {record['instruction'][:180]}")
        elif kind == "subagent_tool":
            push(t, "sub", f"│   ▸ {record['tool']}")
        elif kind == "subagent_done":
            push(t, "sub", f"│ ↩ returned {len(record.get('answer', ''))} chars")
        elif kind == "parallel_delegate":
            n = len(record.get("tasks") or [])
            push(t, "parallel_delegate", f"⚡ parallel_delegate · {n} worker(s)")
            for i, task in enumerate(record.get("tasks") or [], 1):
                goal = (task.get("goal") if isinstance(task, dict) else str(task)) or ""
                push(t, "parallel_delegate", f"  [{i}] {goal[:160]}")
        elif kind == "worker_start":
            push(
                t,
                "worker_start",
                f"│ worker {record.get('worker_id')}: {record.get('goal', '')[:160]}",
            )
        elif kind == "worker_tool":
            args = json.dumps(record.get("args", {}), ensure_ascii=False)
            push(t, "sub", f"│ w{record.get('worker_id')} ▸ {record['tool']}({args[:140]})")
        elif kind == "worker_done":
            push(
                t,
                "worker_done",
                f"│ worker {record.get('worker_id')} → {record.get('status')} "
                f"({len(record.get('report', '') or '')} chars)",
            )
        elif kind == "assistant":
            for line in record.get("text", "").splitlines()[:4]:
                push(t, "plain", line)
        elif kind == "ask_user":
            push(t, "note", f"? {record['question']}")
            push(t, "note", f"  you> {record['answer']}")
        elif kind == "run_done":
            push(t, "report", f"── result: {record['status']} ──")
            push(t, "report", record.get("report", ""))
    return lines


_REMUXED: dict[str, Path] = {}


def probe(path: Path) -> float:
    """Duration of a recording, in seconds.

    A webm whose writer was interrupted carries no duration in its header, and
    Playwright leaves one behind whenever the browser goes away abruptly. Remuxing
    rebuilds the header without re-encoding, which is fast and lossless.
    """
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    duration = json.loads(out).get("format", {}).get("duration")
    if duration in (None, "N/A"):
        fixed = _REMUXED.get(str(path))
        if fixed is None:
            fixed = Path(tempfile.mkdtemp(prefix="agentvid-fix-")) / (path.stem + ".webm")
            print(f"  {path.name}: header has no duration, remuxing")
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-c", "copy", str(fixed)],
                check=True,
            )
            _REMUXED[str(path)] = fixed
        return probe(fixed)
    return float(duration)


def _resolve(video: str, *fallback_dirs: Path) -> Path | None:
    path = Path(video)
    if path.exists():
        return path
    for d in fallback_dirs:
        for candidate in d.rglob(path.name):
            return candidate
    return None


def main_marks(records: list[dict], run_dir: Path) -> list[tuple[float, Path, float]]:
    """(trace time, recording, recording start) each time the visible main tab changes."""
    opened: dict[str, float] = {}
    marks: list[tuple[float, str]] = []
    for rec in records:
        video = rec.get("video")
        if not video or rec["kind"] == "worker_video":
            continue
        opened.setdefault(video, rec["t"])
        if not marks or marks[-1][1] != video:
            marks.append((rec["t"], video))
    out = []
    for t, video in marks:
        path = _resolve(video, run_dir / "video")
        if path:
            out.append((t, path, opened[video]))
    return out


def parallel_windows(records: list[dict], run_dir: Path) -> list[tuple[float, float, dict]]:
    """Spans of trace time where workers ran, with each worker's recording."""
    windows = []
    for i, rec in enumerate(records):
        if rec["kind"] != "parallel_delegate":
            continue
        start, end, videos = rec["t"], rec["t"], {}
        for later in records[i + 1 :]:
            if later["kind"] == "parallel_delegate":
                break
            if later["kind"] == "worker_video":
                path = _resolve(later["video"], run_dir / "workers")
                if path:
                    videos[later["worker_id"]] = (later["t"], path)
            elif later["kind"] == "worker_done":
                end = max(end, later["t"])
        if videos and end - start > 1:
            windows.append((start, end, videos))
    return windows


class Graph:
    """Collects ffmpeg inputs and filter chains; every clip gets its own input."""

    def __init__(self, fps: int) -> None:
        self.fps = fps
        self.inputs: list[str] = []
        self.chains: list[str] = []
        self._n = 0

    def label(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def clip(self, path: Path, src_start: float, length: float, w: int, h: int) -> str:
        """`length` seconds of `path` from `src_start`, frozen at either end if short."""
        actual = _REMUXED.get(str(path), path)
        duration = probe(path)
        lead = max(0.0, -src_start)
        start = min(max(0.0, src_start), max(0.0, duration - 0.2))
        take = max(0.2, min(duration - start, length - lead))
        self.inputs.append(str(actual))
        idx = len(self.inputs)  # input 0 is the terminal panel
        out = self.label("c")
        self.chains.append(
            f"[{idx}:v]trim=start={start:.2f}:duration={take:.2f},setpts=PTS-STARTPTS,"
            f"fps={self.fps},scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={BG_HEX},setsar=1,"
            f"tpad=start_mode=clone:start_duration={lead:.2f}:"
            f"stop_mode=clone:stop_duration={length:.2f},trim=duration={length:.2f},"
            f"setpts=PTS-STARTPTS[{out}]"
        )
        return out

    def blank(self, length: float, w: int, h: int) -> str:
        out = self.label("k")
        self.chains.append(
            f"color=c={BG_HEX}:s={w}x{h}:r={self.fps}:d={length:.2f},setsar=1[{out}]"
        )
        return out

    def grid(self, cells: list[str], length: float) -> str:
        out = self.label("g")
        if len(cells) == 1:
            self.chains.append(
                f"[{cells[0]}]pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={BG_HEX}[{out}]"
            )
        elif len(cells) == 2:
            self.chains.append(
                f"[{cells[0]}][{cells[1]}]hstack=inputs=2,"
                f"pad={W}:{H}:0:(oh-ih)/2:color={BG_HEX}[{out}]"
            )
        else:
            cells = cells[:4] + [self.blank(length, CELL_W, CELL_H) for _ in range(4 - len(cells))]
            joined = "".join(f"[{c}]" for c in cells)
            self.chains.append(
                f"{joined}xstack=inputs=4:layout=0_0|{CELL_W}_0|0_{CELL_H}|{CELL_W}_{CELL_H}[{out}]"
            )
        return out


def build_browser_track(
    records: list[dict], run_dir: Path, graph: Graph, begin: float, end: float
) -> list[str]:
    """Labels of consecutive segments covering trace time [begin, end]."""
    marks = main_marks(records, run_dir)
    windows = parallel_windows(records, run_dir)
    segments: list[str] = []

    def main_span(a: float, b: float) -> None:
        if b - a < 0.3 or not marks:
            return
        for i, (t, path, opened) in enumerate(marks):
            stop = marks[i + 1][0] if i + 1 < len(marks) else float("inf")
            lo, hi = max(a, t if i else a), min(b, stop)
            if hi - lo >= 0.3:
                segments.append(graph.clip(path, lo - opened, hi - lo, W, H))

    cursor = begin
    for start, stop, videos in windows:
        main_span(cursor, start)
        length = stop - start
        cells = [
            graph.clip(path, start - t_video, length, CELL_W, CELL_H)
            for _wid, (t_video, path) in sorted(videos.items())
        ]
        segments.append(graph.grid(cells, length))
        cursor = stop
    main_span(cursor, end)
    return segments


def render_panel(
    lines: list[tuple[float, str, str]],
    now: float,
    size: tuple[int, int],
    font: ImageFont.FreeTypeFont,
    line_h: int,
    rows: int,
) -> Image.Image:
    img = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, size[0], 26], fill=(26, 30, 40))
    draw.text((12, 6), "hydra — terminal", font=font, fill=(150, 160, 180))

    visible = [l for l in lines if l[0] <= now][-rows:]
    y = 34
    for _, kind, text in visible:
        draw.text((12, y), text, font=font, fill=COLOURS.get(kind, COLOURS["plain"]))
        y += line_h
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--panel-width", type=int, default=900)
    ap.add_argument("--font-size", type=int, default=15)
    ap.add_argument(
        "--hold",
        type=float,
        default=8.0,
        help="Freeze on the last frame this long, so the final report is readable.",
    )
    ap.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Speed both streams up by this factor.",
    )
    args = ap.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg/ffprobe are required (install ffmpeg and add it to PATH)")
        return 2

    trace_path = args.run_dir / "trace.jsonl"
    if not trace_path.exists() or not (args.run_dir / "video").is_dir():
        print(f"need {trace_path} and a recording in {args.run_dir / 'video'} (--record-video)")
        return 2

    records = [
        json.loads(l) for l in trace_path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    begin = next((r["t"] for r in records if r["kind"] == "page_opened"), 0.0)
    end = max(r["t"] for r in records) + 1.0

    graph = Graph(args.fps)
    segments = build_browser_track(records, args.run_dir, graph, begin, end)
    if not segments:
        print("no usable recording found")
        return 2
    total = end - begin
    grids = len(parallel_windows(records, args.run_dir))
    print(f"{len(segments)} segment(s), {grids} parallel grid(s), {total:.0f}s of run time")

    speed = "" if args.speed == 1.0 else f",setpts=PTS/{args.speed}"
    joined = "".join(f"[{s}]" for s in segments)
    graph.chains.append(
        f"{joined}concat=n={len(segments)}:v=1:a=0,fps={args.fps}{speed},"
        f"tpad=stop_mode=clone:stop_duration={args.hold}[b]"
    )

    font = load_font(args.font_size)
    line_h = args.font_size + 5
    cols = max(40, int((args.panel_width - 24) / (args.font_size * 0.6)))
    rows = max(10, (H - 44) // line_h)
    lines = trace_to_lines(trace_path, cols)

    tmp = Path(tempfile.mkdtemp(prefix="agentvid-"))
    frames = int((total / args.speed + args.hold) * args.fps)
    for i in range(frames):
        now = begin + i / args.fps * args.speed
        render_panel(lines, now, (args.panel_width, H), font, line_h, rows).save(
            tmp / f"f{i:05d}.png"
        )
    print(f"rendered {frames} terminal frames at {args.panel_width}x{H}")

    out = args.output or (args.run_dir / "demo.mp4")
    cmd = ["ffmpeg", "-y", "-framerate", str(args.fps), "-i", str(tmp / "f%05d.png")]
    for path in graph.inputs:
        cmd += ["-i", path]
    script = tmp / "filter.txt"
    script.write_text(
        ";".join(graph.chains) + ";[0:v][b]hstack=inputs=2:shortest=1[v]", encoding="utf-8"
    )
    cmd += [
        "-filter_complex_script",
        str(script),
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-2500:])
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"wrote {out}  ({W + args.panel_width}x{H})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
