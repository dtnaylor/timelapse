#!/usr/bin/env python3
"""TUI-based downloader for GoPro media files over the camera's web server.

Single-screen live dashboard: an overall progress panel (full-width bar, stats
under it, per-file-type bars below) plus a scrolling list of downloaded files.
"""

import argparse
import os
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import requests
from rich import box
from rich.console import Console, Group
from rich.filesize import decimal as fmt_bytes
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

BASE_DIR_DEFAULT = os.path.expanduser("~/Pictures/GoPro")
GOPRO_URL_DEFAULT = "http://10.5.5.9:8080"
MEDIA_LIST_ENDPOINTS = ("/gopro/media/list", "/gp/gpMediaList")
PARALLEL_JOBS_DEFAULT = 15
CHUNK_SIZE = 64 * 1024
MAX_RETRIES = 3

TYPE_DIRS = {"JPG": "JPG", "GPR": "RAW", "MP4": "Videos"}
TYPE_LABELS = {"JPG": "JPG", "GPR": "RAW (GPR)", "MP4": "Video"}

ORANGE = "#ffb38a"
ORANGE_LIGHT = "#ffd0b0"
SCREEN_BG = "#0a0a0a"     # opencode background
FOOTER_BG = "#141414"     # opencode backgroundPanel (side panel gray)
FOOTER_FG = "#808080"     # opencode textMuted (gray)
SIDEBAR_W = 24
SPIN = "◐◓◑◒"

# Worker -> main events
# ("start", name)
# ("total", name, file_bytes)          # total size once known (may re-fire on retry)
# ("progress", name, bytes_on_disk)    # bytes completed on disk for this file
# ("retry", name, error)
# ("done", name, status, size, elapsed)


def parse_args():
    p = argparse.ArgumentParser(
        description="Download GoPro media files over the camera's web server with a live TUI."
    )
    p.add_argument("--base-dir", default=BASE_DIR_DEFAULT, help="base media library directory")
    p.add_argument("--url", default=GOPRO_URL_DEFAULT, help="GoPro web server base URL (host[:port])")
    p.add_argument("--jobs", type=int, default=PARALLEL_JOBS_DEFAULT, help="parallel download workers")
    p.add_argument("--subdir", default=None, help="subdirectory under base-dir for new files")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return p.parse_args()


def list_media(base_url):
    """Return media files across every DCIM directory.

    Uses GoPro's JSON media-list endpoint (HERO9+/OpenGoPro primary, legacy
    gpMediaList fallback), which reports files grouped by directory. Returns a
    sorted list of (name, download_url, size) for supported media; LRV/THM
    proxies are excluded.
    """
    for endpoint in MEDIA_LIST_ENDPOINTS:
        try:
            resp = requests.get(base_url + endpoint, timeout=15)
            if resp.status_code != 200:
                continue
            return _parse_media_list(base_url, resp.json())
        except (requests.RequestException, ValueError):
            continue
    raise requests.RequestException(
        f"no media-list endpoint available at {base_url}"
    )


def _parse_media_list(base_url, payload):
    media = []
    for group in payload.get("media", []):
        directory = group.get("d", "")
        for f in group.get("fs", []):
            name = f.get("n", "")
            ext = ext_key(name)
            if ext not in TYPE_DIRS:
                continue
            if name.upper().endswith((".LRV", ".THM")):
                continue
            url = f"{base_url}/videos/DCIM/{directory}/{name}"
            size = 0
            try:
                size = int(f.get("s", 0) or 0)
            except ValueError:
                size = 0
            media.append((name, url, size))
    names = set()
    deduped = []
    for name, url, size in media:
        if name in names:
            continue
        names.add(name)
        deduped.append((name, url, size))
    return sorted(deduped)


def scan_library(base_dir):
    """Single pass over BASE_DIR mapping filename -> absolute path (ignores .part)."""
    found = {}
    for root, _dirs, names in os.walk(base_dir):
        for name in names:
            if name.endswith(".part"):
                continue
            found.setdefault(name, os.path.join(root, name))
    return found


def scan_dir_size(directory):
    """Sum file sizes in directory (ignoring .part files)."""
    total = 0
    for root, _dirs, names in os.walk(directory):
        for name in names:
            if name.endswith(".part"):
                continue
            path = os.path.join(root, name)
            total += os.path.getsize(path)
    return total


def pick_default_subdir(base_dir):
    latest = None
    try:
        entries = os.listdir(base_dir)
    except FileNotFoundError:
        return "session-1"
    dirs = [d for d in (os.path.join(base_dir, e) for e in entries) if os.path.isdir(d)]
    if dirs:
        latest = max(dirs, key=lambda d: (os.path.getmtime(d), d))
    return os.path.basename(latest) if latest else "session-1"


def ext_key(name):
    return os.path.splitext(name)[1][1:].upper()


def format_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _download_once(name, dest, url, q, stop):
    """Download one file to dest via a .part sibling; returns (status, size, elapsed)."""
    started = time.monotonic()
    part = str(dest) + ".part"

    start = os.path.getsize(part) if os.path.exists(part) else 0
    if start == 0 and os.path.exists(dest):
        start = os.path.getsize(dest)

    headers = {"Range": f"bytes={start}-"} if start > 0 else {}
    with requests.get(url, stream=True, headers=headers, timeout=30) as resp:
        if resp.status_code == 416:
            # Range starts at the end: file is already complete on disk.
            return "skipped", start, time.monotonic() - started
        if resp.status_code not in (200, 206):
            resp.raise_for_status()

        remaining = 0
        try:
            remaining = int(resp.headers.get("Content-Length", 0))
        except ValueError:
            remaining = 0
        resumed = resp.status_code == 206

        # If we sent a Range request but got a 200 (full file) back,
        # the file is already complete on disk — skip re-downloading.
        if start > 0 and not resumed:
            return "skipped", start, time.monotonic() - started

        # A resume that reports no remaining bytes means the file is already
        # complete on disk (some GoPro firmware answers an at-EOF range with a
        # 206 + empty body instead of 416). Refuse to clobber `dest` with an
        # empty/partial file.
        if start > 0 and remaining <= 0:
            return "skipped", start, time.monotonic() - started

        file_total = remaining if not resumed else (start + remaining)
        if file_total:
            q.put(("total", name, file_total))

        mode = "ab" if resumed else "wb"
        base = start if resumed else 0
        written = 0
        with open(part, mode) as f:
            for chunk in resp.iter_content(CHUNK_SIZE):
                if stop.is_set():
                    raise InterruptedError()
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                q.put(("progress", name, base + written))

        # Only replace `dest` once we actually wrote fresh bytes to `.part`;
        # otherwise we'd overwrite a good file with a truncated one.
        if written > 0:
            os.replace(part, dest)

    status = "resumed" if start > 0 else "new"
    size = file_total if file_total is not None else (start + written)
    return status, size, time.monotonic() - started


def run_worker(name, dest, url, q, stop):
    q.put(("start", name))
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            status, size, elapsed = _download_once(name, dest, url, q, stop)
            q.put(("done", name, status, size, elapsed))
            return
        except InterruptedError:
            q.put(("done", name, "interrupted", 0, 0.0))
            return
        except Exception as exc:
            q.put(("retry", name, str(exc)))
            if attempt >= MAX_RETRIES:
                q.put(("done", name, "error", 0, 0.0))
                return
            time.sleep(2 ** attempt)


class SyncState:
    def __init__(self, total_files):
        self.total_files = total_files
        self.done = 0
        self.downloaded = 0
        self.skipped = 0
        self.errors = 0
        self.retries = 0
        self.session_bytes = 0
        self.subdir_bytes = 0


def main():
    args = parse_args()
    base_dir = os.path.abspath(os.path.expanduser(args.base_dir))
    url = args.url.rstrip("/")
    sub = args.subdir or pick_default_subdir(base_dir)
    target_dir = os.path.join(base_dir, sub)

    for d in ("JPG", "RAW", "Videos"):
        os.makedirs(os.path.join(target_dir, d), exist_ok=True)

    print("Fetching file list from GoPro...")
    try:
        media = list_media(url)
    except requests.RequestException as exc:
        print(f"Error: could not reach the GoPro web server at {url}")
        print(f"  {exc}")
        sys.exit(1)

    if not media:
        print("No media files found on GoPro.")
        return

    existing = scan_library(base_dir)
    new_count = sum(1 for f in media if f[0] not in existing)

    print("")
    print("============================================================")
    print(" GoPro Media Sync Summary")
    print("============================================================")
    print(f" Base Library:   {base_dir}")
    print(f" Target Folder:  {target_dir} (for new files)")
    print(f" Total Found:    {len(media)} media files on GoPro")
    print(f" Truly New:      {new_count} files pending download")
    print(f" Parallel Jobs:  {args.jobs} workers")
    print("============================================================")
    print("")

    if not args.yes:
        try:
            response = input("Proceed with download? [y/N]: ")
        except EOFError:
            response = ""
        if response.strip().lower() not in ("y", "yes"):
            print("Download canceled.")
            return

    console = Console()
    q = queue.Queue()
    stop = threading.Event()
    state = SyncState(len(media))
    state.subdir_bytes = scan_dir_size(target_dir)

    # --- overall bar: full width, count-based -------------------------
    overall = Progress(
        BarColumn(bar_width=None, complete_style=ORANGE, finished_style=ORANGE),
        TaskProgressColumn(text_format="{task.percentage:>3.0f}%", style=ORANGE),
        console=console,
        expand=True,
    )
    overall_task = overall.add_task("", total=state.total_files)

    # --- per-type bars: bar only, file-count based, one third each ---------
    type_progress = {}
    type_task = {}
    for ext, label in TYPE_LABELS.items():
        p = Progress(BarColumn(bar_width=None, complete_style=ORANGE_LIGHT, finished_style=ORANGE_LIGHT), console=console, expand=True)
        count = sum(1 for f in media if ext_key(f[0]) == ext)
        if count == 0:
            type_task[ext] = p.add_task("", total=1, completed=1)
        else:
            type_task[ext] = p.add_task("", total=count)
        type_progress[ext] = p

    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in TYPE_LABELS:
        grid.add_column(ratio=1)
    grid.add_row(
        *[
            Group(Text(label, style="bold"), type_progress[ext])
            for ext, label in TYPE_LABELS.items()
        ]
    )

    rows = OrderedDict()          # name -> [type, size_str, time_str, status]
    prog = {}                     # name -> [total_bytes, bytes_on_disk]

    def build_live(tick, now):
        elapsed = now - t_start

        table = Table(box=None, header_style="bold", pad_edge=False, padding=(0, 1))
        table.add_column("File", no_wrap=True, overflow="ellipsis")
        table.add_column("Type", style="dim")
        table.add_column("Size", justify="right")
        table.add_column("Time", justify="right")
        table.add_column("Status", width=14)

        height = console.size.height or 25
        shown = max(4, height - 12)
        items = list(rows.items())
        if len(items) > shown:
            table.add_row(f"… {len(items) - shown} more completed", "", "", "", style="dim")
            items = items[-shown:]
        for name, (typ, size, dur, status) in items:
            if name in prog:
                total, done = prog[name]
                if total:
                    status = ProgressBar(
                        total=total,
                        completed=min(done, total),
                        width=10,
                        complete_style=ORANGE_LIGHT,
                        finished_style=ORANGE_LIGHT,
                    )
                else:
                    status = f"downloading {SPIN[tick % len(SPIN)]}"
            table.add_row(name, typ, size, dur, status)

        footer = Text.assemble(
            (f"{state.total_files - state.done} remaining", ""),
            (f"  ·  {state.downloaded} downloaded", ""),
            (f"  ·  {state.skipped} skipped", ""),
            (f"  ·  {state.retries} retries", ""),
            (f"  ·  {state.errors} errors", ""),
        )
        footer.stylize(f"{FOOTER_FG} on {FOOTER_BG}")
        footer.append(" " * (console.width - footer.cell_len), style=f"on {FOOTER_BG}")

        panel = Panel(
            Group(overall, Text(""), grid),
            box=box.ROUNDED,
            border_style=ORANGE,
            style=f"on {SCREEN_BG}",
            title="GoPro Sync",
            expand=True,
        )
        if console.height:
            table_bg = Panel(
                table,
                box=box.SQUARE,
                border_style=f"{SCREEN_BG} on {SCREEN_BG}",
                style=f"on {SCREEN_BG}",
                expand=True,
            )

            def _heading(text):
                row = Text(f"  {text}", style=f"bold on {FOOTER_BG}")
                row.append(" " * (SIDEBAR_W - row.cell_len), style=f"on {FOOTER_BG}")
                return row

            def _stat(text):
                row = Text(f"  {text}", style=f"{FOOTER_FG} on {FOOTER_BG}")
                row.append(" " * (SIDEBAR_W - row.cell_len), style=f"on {FOOTER_BG}")
                return row

            def _blank():
                return Text(" " * SIDEBAR_W, style=f"on {FOOTER_BG}")

            sidebar_rows = [
                _blank(),
                _heading("Downloads"),
                _stat(f"{state.done} / {state.total_files} downloaded"),
                _stat(f"{state.skipped} skipped"),
                _stat(f"{state.retries} retries"),
                _stat(f"{state.errors} errors"),
                _blank(),
                _heading("Elapsed"),
                _stat(format_duration(elapsed)),
                _blank(),
                _heading("Size"),
                _stat(f"Session: {fmt_bytes(state.session_bytes)}"),
                _stat(f"Subdir:  {fmt_bytes(state.subdir_bytes)}"),
            ]
            bottom_h = console.height - 6  # panel = 4 content rows + 2 ROUNDED borders
            sidebar = Group(
                *sidebar_rows,
                *[_blank()] * max(0, bottom_h - len(sidebar_rows)),
            )
            bottom = Layout()
            bottom.split_row(
                Layout(table_bg, name="table", ratio=1),
                Layout(sidebar, name="sidebar", size=SIDEBAR_W),
            )
            layout = Layout()
            layout.split_column(
                Layout(panel, name="top", size=6),
                Layout(bottom, name="bottom", ratio=1),
            )
            return layout
        stats_line = Text.assemble(
            (f"{state.done} / {state.total_files} files", "bold"),
            (f"  ·  {format_duration(elapsed)}", "dim"),
        )
        return Group(panel, stats_line, table, footer)

    def drain():
        while True:
            try:
                ev = q.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "start":
                _, name = ev
                prog[name] = [None, 0]
                ext = ext_key(name)
                rows[name] = [TYPE_LABELS[ext], "", "", "queued"]
            elif kind == "total":
                _, name, total = ev
                prog[name][0] = total
            elif kind == "progress":
                _, name, done = ev
                prog[name][1] = done
            elif kind == "retry":
                _, name, _err = ev
                state.retries += 1
            elif kind == "done":
                _, name, status, size, elapsed = ev
                prog.pop(name, None)
                state.done += 1
                overall.advance(overall_task, 1)
                type_progress[ext_key(name)].advance(type_task[ext_key(name)], 1)
                if status == "skipped":
                    state.skipped += 1
                elif status in ("new", "resumed"):
                    state.downloaded += 1
                    state.session_bytes += size or 0
                    if name in in_target:
                        state.subdir_bytes += size or 0
                elif status == "error":
                    state.errors += 1
                ext = ext_key(name)
                if rows.get(name):
                    if status in ("error", "interrupted"):
                        size_str, time_str = "—", "—"
                    else:
                        size_str, time_str = fmt_bytes(size), f"{elapsed:.1f}s"
                    rows[name] = [
                        TYPE_LABELS[ext],
                        size_str,
                        time_str,
                        status,
                    ]
                    rows.move_to_end(name)

    futures = {}
    pool = ThreadPoolExecutor(max_workers=args.jobs)
    in_target = set()
    for name, file_url, _size in media:
        ext = ext_key(name)
        dest = existing.get(name) or os.path.join(target_dir, TYPE_DIRS[ext], name)
        if os.path.commonpath([dest, target_dir]) == target_dir:
            in_target.add(name)
        futures[pool.submit(run_worker, name, dest, file_url, q, stop)] = name

    interrupted = False
    tick = 0
    t_start = time.monotonic()
    try:
        with Live(
            build_live(0, time.monotonic()),
            console=console,
            screen=True,
            refresh_per_second=10,
        ) as live:
            while True:
                drain()
                if all(f.done() for f in futures):
                    drain()
                    live.update(build_live(tick, time.monotonic()), refresh=True)
                    break
                live.update(build_live(tick, time.monotonic()), refresh=True)
                tick += 1
                time.sleep(0.05)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)

    console.print()
    if interrupted:
        console.print("[bold red]Interrupted.[/bold red] Partial files saved as .part — safe to resume on the next run.")
        return

    console.print("[bold]Sync complete[/bold]")
    console.print(f"  Files:    {state.total_files} found · {state.downloaded} downloaded · "
                  f"{state.skipped} already present · {state.errors} errors")
    console.print(f"  Duration: {format_duration(time.monotonic() - t_start)}")


if __name__ == "__main__":
    main()
