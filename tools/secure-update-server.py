#!/usr/bin/env python3
import argparse
import atexit
import functools
import datetime
import email.utils
import os
import posixpath
import re
import socket
import ssl
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit


def install_pid_file(path):
    """Record the serving process and remove the record on normal exit."""
    if not path:
        return
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pid = str(os.getpid())
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write(pid + "\n")

    def cleanup():
        try:
            with open(path, "r", encoding="ascii") as handle:
                if handle.read().strip() != pid:
                    return
            os.remove(path)
        except (FileNotFoundError, OSError):
            pass

    atexit.register(cleanup)


class SecureUpdateHandler(SimpleHTTPRequestHandler):
    server_version = "TFCRUpdateServer/1.0"
    _buffer_gate = threading.BoundedSemaphore(1)
    _SOCKET_TIMEOUT = 30

    def setup(self):
        super().setup()
        self.connection.settimeout(self._SOCKET_TIMEOUT)

    def __init__(self, *args, token="", kitdir="", **kwargs):
        self.token = token.strip("/")
        # kitdir: public tokenless channel at /kit/ for toolkit self-update files
        # (the kit itself is meant to be public; only flat .zip/.txt basenames are served).
        self.kitdir = kitdir
        super().__init__(*args, **kwargs)

    def _kit_file(self, decoded):
        if not self.kitdir:
            return None
        parts = [part for part in decoded.split("/") if part]
        if len(parts) != 2 or parts[0] != "kit":
            return None
        name = parts[1]
        if name in ("", ".", "..") or os.path.dirname(name):
            return None
        if not (name.endswith(".zip") or name.endswith(".txt")):
            return None
        return os.path.join(self.kitdir, name)

    def list_directory(self, path):
        self.send_error(HTTPStatus.NOT_FOUND, "No directory listing")
        return None

    def do_POST(self):
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Only GET and HEAD are allowed")

    def do_PUT(self):
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Only GET and HEAD are allowed")

    def do_DELETE(self):
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Only GET and HEAD are allowed")

    def translate_path(self, path):
        raw_path = urlsplit(path).path
        decoded = unquote(raw_path)
        kit_file = self._kit_file(decoded)
        if kit_file is not None:
            return kit_file
        parts = [part for part in decoded.split("/") if part]
        if not parts or parts[0] != self.token:
            return "__blocked__"
        safe_parts = []
        for part in parts[1:]:
            if part in ("", ".", "..") or os.path.dirname(part):
                continue
            safe_parts.append(part)
        return os.path.join(self.directory, *safe_parts)

    def send_head(self):
        raw_path = urlsplit(self.path).path
        decoded = unquote(raw_path)
        clean = posixpath.normpath(decoded)
        if "/.." in clean or clean.endswith("/.."):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return None
        if self._kit_file(clean) is not None:
            return self._send_file_head()
        expected_prefix = "/" + self.token
        if clean != expected_prefix and not clean.startswith(expected_prefix + "/"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return None
        if "/.." in clean or clean.endswith("/.."):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return None
        return self._send_file_head()

    def _send_file_head(self):
        """Serve one stable open file; byte ranges never enter the full-file buffer."""
        self._byte_range = None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            handle = open(path, 'rb')
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, 'File not found')
            return None
        try:
            stat = os.fstat(handle.fileno())
            size = stat.st_size
            etag = '"{:x}-{:x}-{:x}"'.format(stat.st_ino, stat.st_mtime_ns, size)
            modified = datetime.datetime.fromtimestamp(int(stat.st_mtime), datetime.timezone.utc)

            def date_matches(value):
                try:
                    date = email.utils.parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=datetime.timezone.utc)
                    return modified <= date
                except (TypeError, ValueError, OverflowError):
                    return False

            none_match = self.headers.get('If-None-Match')
            unchanged = False
            if none_match:
                unchanged = any(value.strip().removeprefix('W/') in ('*', etag)
                                for value in none_match.split(','))
            elif self.headers.get('If-Modified-Since'):
                unchanged = date_matches(self.headers['If-Modified-Since'])
            if unchanged:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header('ETag', etag)
                self.send_header('Last-Modified', self.date_time_string(stat.st_mtime))
                self.end_headers()
                handle.close()
                return None

            byte_range = self.headers.get('Range')
            if_range = self.headers.get('If-Range')
            if byte_range and if_range:
                # A changed or weak validator requires a fresh full response.
                valid = if_range == etag if if_range.startswith(('"', 'W/')) else date_matches(if_range)
                if not valid:
                    byte_range = None
            start, end = 0, size - 1
            if byte_range:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', byte_range.strip())
                valid = bool(match and (match[1] or match[2]) and size)
                if valid:
                    if match[1]:
                        start = int(match[1])
                        end = min(int(match[2]), size - 1) if match[2] else size - 1
                    else:
                        suffix = int(match[2])
                        start = max(0, size - suffix)
                    valid = 0 <= start <= end < size
                if not valid:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header('Content-Range', 'bytes */{}'.format(size))
                    self.send_header('Content-Length', '0')
                    self.send_header('ETag', etag)
                    self.end_headers()
                    handle.close()
                    return None
                self._byte_range = (start, end)
            self.send_response(HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK)
            self.send_header('Content-type', self.guess_type(path))
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Last-Modified', self.date_time_string(stat.st_mtime))
            self.send_header('ETag', etag)
            self.send_header('Accept-Ranges', 'bytes')
            if byte_range:
                self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, end, size))
            self.end_headers()
            return handle
        except BaseException:
            handle.close()
            raise

    # Read the whole file into memory and close the handle immediately instead of
    # streaming from the open handle for the entire (possibly slow) transfer.
    # A long-held handle blocks the publish script from replacing/deleting the file
    # on Windows (2026-07-08: a player download made portable-publish.ps1 abort
    # mid-delete and left the update source half-broken). Files here are small
    # (mods/resourcepacks, tens of MB); cap guards against pathological sizes.
    _BUFFER_CAP = 256 * 1024 * 1024

    def do_GET(self):
        f = self.send_head()
        if f is None:
            return
        try:
            if getattr(self, '_byte_range', None) is not None:
                start, end = self._byte_range
                f.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = f.read(min(64 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
                return
            try:
                size = os.fstat(f.fileno()).st_size
            except (OSError, AttributeError, ValueError):
                size = None
            if size is not None and size <= self._BUFFER_CAP and self._buffer_gate.acquire(blocking=False):
                try:
                    data = f.read()
                    f.close()
                    f = None
                    self.wfile.write(data)
                finally:
                    self._buffer_gate.release()
            else:
                self.copyfile(f, self.wfile)
        finally:
            if f is not None:
                f.close()

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        return

class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            return
        if exc_type is not None and issubclass(exc_type, ssl.SSLError):
            return
        super().handle_error(request, client_address)


class DualStackThreadingHTTPServer(QuietThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

def main():
    parser = argparse.ArgumentParser(description="Serve TFCR update files behind a secret URL prefix.")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--token", required=True)
    parser.add_argument("--certfile", default="", help="PEM cert chain; enables HTTPS when set")
    parser.add_argument("--keyfile", default="", help="PEM private key; defaults to certfile if omitted")
    parser.add_argument("--kitdir", default="", help="serve toolkit self-update files at tokenless /kit/ when set")
    parser.add_argument("--pid-file", default="", help="write the serving process PID here")
    args = parser.parse_args()

    handler = functools.partial(SecureUpdateHandler, directory=args.directory, token=args.token, kitdir=args.kitdir)
    server_class = DualStackThreadingHTTPServer if ":" in args.bind else QuietThreadingHTTPServer
    with server_class((args.bind, args.port), handler) as httpd:
        # Only publish the PID after the socket bind succeeds. A failed start
        # must not replace a useful PID record with a process that immediately exits.
        install_pid_file(args.pid_file)
        if args.certfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(args.certfile, args.keyfile or None)
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()


