#!/usr/bin/env python3
import argparse
import atexit
import functools
import gzip
import hashlib
import io
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


class AccessLog:
    """UPDATE_ADOPTION_V1: one line per manifest request (time, status, a daily visitor
    hash, User-Agent) so the admin can see which updater versions players run. The salt
    lives only in memory and changes every day: IP addresses are never written and
    visitors cannot be linked across days. Files older than KEEP_DAYS are removed."""
    KEEP_DAYS = 30

    def __init__(self, folder):
        self.folder = folder
        self.lock = threading.Lock()
        self.day = None
        self.salt = b""

    def record(self, status, user_agent, address):
        if not self.folder:
            return
        now = datetime.datetime.now()
        day = now.strftime("%Y%m%d")
        agent = re.sub(r"[\x00-\x1f\x7f]+", " ", str(user_agent or "-"))[:160]
        with self.lock:
            if day != self.day:
                self.day, self.salt = day, os.urandom(16)
                self._prune(now)
            visitor = hashlib.sha256(self.salt + str(address).encode("utf-8")).hexdigest()[:12]
            os.makedirs(self.folder, exist_ok=True)
            with open(os.path.join(self.folder, "update-access-" + day + ".log"), "a", encoding="utf-8") as handle:
                handle.write("{}\t{}\t{}\t{}\n".format(now.isoformat(timespec="seconds"), status, visitor, agent))

    def _prune(self, now):
        cutoff = (now - datetime.timedelta(days=self.KEEP_DAYS)).strftime("%Y%m%d")
        try:
            for name in os.listdir(self.folder):
                match = re.fullmatch(r"update-access-(\d{8})\.log", name)
                if match and match.group(1) < cutoff:
                    os.remove(os.path.join(self.folder, name))
        except OSError:
            pass


ACCESS_LOG = AccessLog("")


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
            # MANIFEST_CACHE_V1: JSON (the manifest) may be sent gzip-compressed; that
            # representation has its own ETag, and either ETag matches If-None-Match.
            gzip_etag = etag[:-1] + '-gz"'
            use_gzip = (path.endswith('.json') and 1024 <= size <= self._GZIP_CAP and not self.headers.get('Range')
                        and 'gzip' in (self.headers.get('Accept-Encoding') or '').lower())
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
                unchanged = any(value.strip().removeprefix('W/') in ('*', etag, gzip_etag)
                                for value in none_match.split(','))
            elif self.headers.get('If-Modified-Since'):
                unchanged = date_matches(self.headers['If-Modified-Since'])
            if unchanged:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header('ETag', gzip_etag if use_gzip else etag)
                self.send_header('Last-Modified', self.date_time_string(stat.st_mtime))
                self.end_headers()
                handle.close()
                return None

            if use_gzip:
                body = self._gzip_body(path, etag, handle)
                handle.close()
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-type', self.guess_type(path))
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Last-Modified', self.date_time_string(stat.st_mtime))
                self.send_header('ETag', gzip_etag)
                self.send_header('Vary', 'Accept-Encoding')
                self.end_headers()
                return io.BytesIO(body)

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

    _GZIP_CAP = 16 * 1024 * 1024
    _gzip_lock = threading.Lock()
    _gzip_cache = {}

    @classmethod
    def _gzip_body(cls, path, etag, handle):
        """Compress once per manifest version; later requests reuse the bytes."""
        with cls._gzip_lock:
            cached = cls._gzip_cache.get(path)
            if cached and cached[0] == etag:
                return cached[1]
        body = gzip.compress(handle.read(), 6)
        with cls._gzip_lock:
            if len(cls._gzip_cache) > 8:
                cls._gzip_cache.clear()
            cls._gzip_cache[path] = (etag, body)
        return body

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

    def log_request(self, code='-', size='-'):
        try:
            if urlsplit(self.path).path.endswith('/server-manifest.json'):
                ACCESS_LOG.record(int(code) if str(code).isdigit() else code,
                                  self.headers.get('User-Agent', '') if self.headers else '', self.client_address[0])
        except Exception:
            pass  # the adoption log must never break a download

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
    parser.add_argument("--access-log-dir", default="auto",
                        help="manifest request log folder; 'auto' = <tools>/../logs/update-access, '' = off")
    args = parser.parse_args()
    folder = args.access_log_dir
    if folder == "auto":
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "logs", "update-access")
    if folder:
        folder = os.path.abspath(folder)
        served = os.path.abspath(args.directory)
        if os.path.commonpath([folder, served]) == served:
            folder = ""  # never write logs into the served folder
    ACCESS_LOG.folder = folder

    handler = functools.partial(SecureUpdateHandler, directory=args.directory, token=args.token, kitdir=args.kitdir)
    server_class = DualStackThreadingHTTPServer if ":" in args.bind else QuietThreadingHTTPServer
    with server_class((args.bind, args.port), handler) as httpd:
        if args.certfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(args.certfile, args.keyfile or None)
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        # Publish the PID only after bind and TLS setup succeed, so a failed
        # start never replaces a useful PID record.
        install_pid_file(args.pid_file)
        httpd.serve_forever()


if __name__ == "__main__":
    main()


