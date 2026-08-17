#!/usr/bin/env python3
"""Mock GoPro HERO web server for testing the downloader without a camera.

Serves a GoPro-style directory listing at /videos/DCIM/100GOPRO/ with byte-range
support (200/206/416), and generates a realistic set of test media files on
startup. Run `make server` (or this script directly) and point download_gopro_tui
at the printed URL.
"""

import argparse
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

DIR_PATH = "/videos/DCIM/100GOPRO"
CHUNK = 64 * 1024

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8080,
    "shots": 120,          # number of JPG shots
    "gprs": 120,           # number of .GPR raws
    "videos": 2,           # number of .MP4 clips
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
    for name, size in (("jpg", "jpg_size"), ("gpr", "gpr_size"), ("mp4", "mp4_size"), ("lrv", "lrv_size")):
        p.add_argument(f"--{name}-size", type=int, default=DEFAULTS[size], metavar="BYTES",
                       help=f"{name.upper()} file size in bytes")
    return p.parse_args()


def shot_number(i):
    return f"G{i + 1:07d}"


def generate_files(args):
    """Create the test media set in args.dir. Returns the list of filenames."""
    os.makedirs(args.dir, exist_ok=True)
    files = []

    def ensure(name, size):
        path = os.path.join(args.dir, name)
        files.append(name)
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
        ensure(f"{shot_number(i)}.JPG", args.jpg_size)
    for i in range(args.gprs):
        ensure(f"{shot_number(i)}.GPR", args.gpr_size)
    for i in range(args.videos):
        base = shot_number(i)
        ensure(f"{base}.MP4", args.mp4_size)
        ensure(f"{base}.LRV", args.lrv_size)

    return files


def build_listing(files):
    hrefs = "".join(
        f'<p><a href="{DIR_PATH}/{f}">{f}</a></p>'
        for f in files
    )
    return (
        f"<html><head><title>Index of {DIR_PATH}</title></head><body>"
        f"<h1>Index of {DIR_PATH}</h1>{hrefs}</body></html>"
    ).encode()


class GoProHandler(BaseHTTPRequestHandler):
    server_version = "GoProWebServer/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[mock-gopro] {self.address_string()} {fmt % args}\n")

    def do_GET(self):
        path = unquote(urlsplit(self.path).path.rstrip("/"))
        if path == DIR_PATH:
            return self.send_listing()
        if path.startswith(DIR_PATH + "/"):
            name = os.path.basename(path)
            return self.send_file(name)
        self.send_error(404)

    def send_listing(self):
        body = self.server.listing
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, name):
        if not re.match(r"^[A-Za-z0-9]+\.(JPG|GPR|MP4|LRV)$", name):
            self.send_error(404)
            return
        path = os.path.join(self.server.data_dir, name)
        if not os.path.isfile(path):
            self.send_error(404)
            return
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
        self.listing = build_listing(files)
        self.throttle = throttle


def main():
    args = parse_args()
    files = generate_files(args)

    total = sum(os.path.getsize(os.path.join(args.dir, f)) for f in files)
    host, port = args.host, args.port
    server = None
    while True:
        try:
            server = MockServer((host, port), args.dir, files, args.throttle)
            break
        except OSError:
            port += 1

    counts = {
        "JPG": sum(f.endswith(".JPG") for f in files),
        "GPR": sum(f.endswith(".GPR") for f in files),
        "MP4": sum(f.endswith(".MP4") for f in files),
        "LRV": sum(f.endswith(".LRV") for f in files),
    }
    print("Mock GoPro serving:")
    print(f"  Listing:  http://{host}:{port}{DIR_PATH}/")
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
