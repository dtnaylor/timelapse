#!/usr/bin/env python3
"""Mock GoPro HERO web server for testing the downloader without a camera.

Serves GoPro's JSON media-list endpoint (/gopro/media/list) plus per-directory
file downloads under /videos/DCIM/<DIR>/ with byte-range support (200/206/416),
and generates a realistic set of test media files spread across multiple DCIM
directories on startup. Run `make server` (or this script directly) and point
download_gopro.py at the printed base URL.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

DCIM_ROOT = "/videos/DCIM"
CHUNK = 64 * 1024

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8080,
    "shots": 120,          # number of JPG shots
    "gprs": 120,           # number of .GPR raws
    "videos": 2,           # number of .MP4 clips
    "dirs": 2,             # number of DCIM directories to spread files across
    "jpg_size": 512 * 1024,
    "gpr_size": 2 * 1024 * 1024,
    "mp4_size": 1024 * 1024,
    "lrv_size": 256 * 1024,
}


def parse_args():
    p = argparse.ArgumentParser(description="Mock GoPro web server for local testing.")
    p.add_argument("--host", default=DEFAULTS["host"])
    p.add_argument("--port", type=int, default=DEFAULTS["port"],
                   help="port to listen on (auto-advances if busy)")
    p.add_argument("--dir", default=".gopro-mock", help="directory for generated test files")
    p.add_argument("--throttle", type=int, default=0, metavar="KB/S",
                   help="per-connection download cap in KB/s (0 = unlimited)")
    p.add_argument("--shots", type=int, default=DEFAULTS["shots"], help="number of JPG shots")
    p.add_argument("--gprs", type=int, default=DEFAULTS["gprs"], help="number of GPR raws")
    p.add_argument("--videos", type=int, default=DEFAULTS["videos"], help="number of MP4 clips")
    p.add_argument("--dirs", type=int, default=DEFAULTS["dirs"], metavar="N",
                   help="number of DCIM directories to spread files across")
    for name, size in (("jpg", "jpg_size"), ("gpr", "gpr_size"), ("mp4", "mp4_size"), ("lrv", "lrv_size")):
        p.add_argument(f"--{name}-size", type=int, default=DEFAULTS[size], metavar="BYTES",
                       help=f"{name.upper()} file size in bytes")
    return p.parse_args()


def shot_number(i):
    return f"G{i + 1:07d}"


def dcim_dir(i):
    return f"1{i:02d}GOPRO"


def generate_files(args):
    """Create the test media set under args.dir, spread across DCIM dirs.

    Returns an ordered list of (directory, filename)."""
    os.makedirs(args.dir, exist_ok=True)
    files = []
    dirs = max(1, args.dirs)

    def ensure(directory, name, size):
        sub = os.path.join(args.dir, directory)
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, name)
        files.append((directory, name))
        if os.path.isfile(path) and os.path.getsize(path) == size:
            return
        tmp = path + ".gen"
        chunk = b"X" * min(CHUNK, size)
        written = 0
        with open(tmp, "wb") as f:
            while written < size:
                n = min(CHUNK, size - written)
                f.write(chunk[:n])
                written += n
        os.replace(tmp, path)

    for i in range(args.shots):
        ensure(dcim_dir(i % dirs), f"{shot_number(i)}.JPG", args.jpg_size)
    for i in range(args.gprs):
        ensure(dcim_dir(i % dirs), f"{shot_number(i)}.GPR", args.gpr_size)
    for i in range(args.videos):
        base = shot_number(i)
        ensure(dcim_dir(i % dirs), f"{base}.MP4", args.mp4_size)
        ensure(dcim_dir(i % dirs), f"{base}.LRV", args.lrv_size)

    return files


def build_media_list(data_dir, files):
    """Build the /gopro/media/list JSON payload: files grouped by directory."""
    groups = {}
    for directory, name in files:
        path = os.path.join(data_dir, directory, name)
        groups.setdefault(directory, []).append({
            "n": name,
            "s": str(os.path.getsize(path)),
        })
    media = [{"d": d, "fs": fs} for d, fs in groups.items()]
    return json.dumps({"id": "mock", "media": media}).encode()


class GoProHandler(BaseHTTPRequestHandler):
    server_version = "GoProWebServer/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[mock-gopro] {self.address_string()} {fmt % args}\n")

    def do_GET(self):
        path = unquote(urlsplit(self.path).path.rstrip("/"))
        if path == "/gopro/media/list":
            return self.send_media_list()
        if path.startswith(DCIM_ROOT + "/"):
            return self.send_file_from_path(path)
        self.send_error(404)

    def send_media_list(self):
        body = self.server.media_list
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file_from_path(self, path):
        rel = path[len(DCIM_ROOT):].lstrip("/")
        parts = rel.split("/")
        if len(parts) != 2:
            self.send_error(404)
            return
        directory, name = parts
        if not re.match(r"^1\d\dGOPRO$", directory):
            self.send_error(404)
            return
        if not re.match(r"^[A-Za-z0-9]+\.(JPG|GPR|MP4|LRV|THM)$", name):
            self.send_error(404)
            return
        full = os.path.join(self.server.data_dir, directory, name)
        if not os.path.isfile(full):
            self.send_error(404)
            return
        self.stream_file(full)

    def stream_file(self, path):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = self._parse_range(rng, size)
            if start is None:
                self.send_error(416)
                return
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.send_header("Content-Length", str(size - start))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self._stream(path, start, size)
            return

        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._stream(path, 0, size)

    @staticmethod
    def _parse_range(rng, size):
        m = re.match(r"^bytes=(\d+)-$", rng)
        if not m:
            return None
        return int(m.group(1))

    def _stream(self, path, start, end):
        throttle = self.server.throttle
        delay = (CHUNK / (throttle * 1024.0)) if throttle else 0.0
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start
            while remaining > 0:
                n = min(CHUNK, remaining)
                data = f.read(n)
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)
                if delay:
                    time.sleep(delay)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, data_dir, files, throttle):
        super().__init__(addr, GoProHandler)
        self.data_dir = data_dir
        self.media_list = build_media_list(data_dir, files)
        self.throttle = throttle


def main():
    args = parse_args()
    files = generate_files(args)

    total = sum(
        os.path.getsize(os.path.join(args.dir, directory, name))
        for directory, name in files
    )
    host, port = args.host, args.port
    server = None
    while True:
        try:
            server = MockServer((host, port), args.dir, files, args.throttle)
            break
        except OSError:
            port += 1

    counts = {
        "JPG": sum(f[1].endswith(".JPG") for f in files),
        "GPR": sum(f[1].endswith(".GPR") for f in files),
        "MP4": sum(f[1].endswith(".MP4") for f in files),
        "LRV": sum(f[1].endswith(".LRV") for f in files),
    }
    dirs = sorted({d for d, _ in files})
    print("Mock GoPro serving:")
    print(f"  Media list: http://{host}:{port}/gopro/media/list")
    print(f"  Directories: {', '.join(dirs)}")
    print(f"  Data:     {os.path.abspath(args.dir)}")
    print(f"  Files:    {counts['JPG']} JPG · {counts['GPR']} GPR · {counts['MP4']} MP4 "
          f"(+{counts['LRV']} LRV)  ~{total / 1000 / 1000:.1f} MB")
    if args.throttle:
        print(f"  Throttle: {args.throttle} KB/s per connection")
    print("  Press Ctrl-C to stop (files are kept for resume tests).")
    print("", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMock GoPro stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
