#!/usr/bin/env python3
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fnmatch
import hashlib
import http.client
import json
import os
import pathlib
import random
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

# 家宽更新源是 DDNS 直连地址，绝不该走系统代理。macOS 上 urllib 会自动读取系统代理
# （如 Clash 的“设为系统代理”），而代理节点到不了 18088 端口就会对每个请求整齐回 503。
# 官方 CDN 优先直连；仅在当前请求失败或持续低速后尝试现有代理。
# 默认 opener 仍强制直连，避免清单/家宽误入代理；官方源单独选 opener。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_DIRECT_OPENER)
_SYNC_UA = "portable-server-kit-sync/1.0"
_OFFICIAL_TIMEOUT_SEC = 8
# 官方源按速率判断，不是「8 秒内凑够一点点就算成功」。
# 旧门槛 256KB/8s = 32KB/s，70MB 模组会细水长流半小时还不回落。
_OFFICIAL_MIN_BYTES = 1024 * 1024
_OFFICIAL_MIN_RATE = 128 * 1024
_PRIVATE_PROBE_TIMEOUT_SEC = 1.2
_PUBLIC_PROBE_TIMEOUT_SEC = 8
_DOWNLOAD_WORKERS = 4
_PRINT_LOCK = threading.Lock()
_PROXY_URL = None
_PROXY_OPENER = None


def fetch_bytes(url: str, timeout: float = 20) -> bytes:
    """直连下载。优先 IPv4，避免 macOS 先卡在不通的 AAAA 上直到超时。"""
    req = urllib.request.Request(url, headers={"User-Agent": _SYNC_UA})
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == 'https':
        # Keep the DNS name for TLS SNI and certificate verification.
        with _DIRECT_OPENER.open(req, timeout=timeout) as response:
            return response.read()
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last_err = None
    families = (socket.AF_INET, socket.AF_INET6)
    for family in families:
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except Exception as exc:
            last_err = exc
            continue
        if not infos:
            continue
        ip = infos[0][4][0]
        try:
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(ip, port, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(ip, port, timeout=timeout)
            try:
                conn.request("GET", path, headers={"Host": host, "User-Agent": _SYNC_UA})
                resp = conn.getresponse()
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status} from {ip}")
                return resp.read()
            finally:
                conn.close()
        except Exception as exc:
            last_err = exc
    if last_err:
        raise last_err
    with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def sha1(path: pathlib.Path) -> str:
    if not path.is_file():
        return ""
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rel(rel: str) -> str:
    rel = str(rel).replace("\\", "/").strip()
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ValueError(f"unsafe manifest path: {rel}")
    return rel


def rel_key(rel: str) -> str:
    return safe_rel(rel).lower()


def join_safe(root: pathlib.Path, rel: str) -> pathlib.Path:
    base = root.absolute()
    target = base.joinpath(*safe_rel(rel).split("/"))
    # Do not follow a junction/symlink in any parent, even when the final file
    # itself has ordinary attributes. Root has already been resolved by main.
    if os.path.normcase(str(base.resolve())) != os.path.normcase(str(base)):
        raise LocalFileUpdateError(f"[路径错误] 更新目录经过链接，已停止写入：{root}")
    resolved = target.resolve()
    if not resolved.is_relative_to(base) or resolved != target:
        raise LocalFileUpdateError(f"[路径错误] 更新目标经过链接或越出目录，已停止写入：{rel}")
    return target


_PRIMARY_UPDATE_URL_FILES = (
    "UPDATE-URL.txt",
    "PORTABLE-UPDATE-URL.txt",
    "TFCR-update-url.txt",
    "_updater/UPDATE-URL.txt",
    "_updater/PORTABLE-UPDATE-URL.txt",
)


def _url_lines(raw: str) -> list:
    """Read one or more URL lines while tolerating CRLF and UTF-8 BOM files."""
    values = []
    for line in str(raw or "").replace("\r", "\n").split("\n"):
        value = line.strip()
        if value and not value.startswith("#"):
            values.append(value)
    return values


def read_update_urls(root: pathlib.Path, override: str) -> list:
    """Return the public update URL from the pack. Do not auto-probe LAN."""
    raw_values = []
    if override:
        raw_values.extend(_url_lines(override))
    else:
        for rel in _PRIMARY_UPDATE_URL_FILES:
            p = root.joinpath(*rel.split("/"))
            if p.is_file():
                try:
                    raw_values.extend(_url_lines(p.read_text(encoding="utf-8-sig")))
                except OSError:
                    continue

    out = []
    seen = set()
    for value in raw_values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    if not out:
        raise SystemExit("找不到 UPDATE-URL.txt。请确认玩家包没有被拆散，或使用 --manifest-url 手动指定 server-manifest.json 地址。")
    return out


def read_update_url(root: pathlib.Path, override: str) -> str:
    """Backward-compatible single-URL helper for callers outside this script."""
    return read_update_urls(root, override)[0]


def url_for(base_url: str, rel: str) -> str:
    parts = [urllib.parse.quote(part) for part in safe_rel(rel).split("/")]
    return base_url + "/".join(parts)


def mask_url(url: str) -> str:
    # 更新地址首个路径段是访问 token，玩家截图求助时容易泄漏，打印前掩码。
    return re.sub(r"(?<=://)([^/]+/)[^/]+(?=/)", r"\1***", str(url), count=1)


def is_protected_rel(rel: str) -> bool:
    # 同步自身生成的备份/归档目录，任何清理规则都不得删除。
    rel = str(rel).replace("\\", "/").lstrip("/")
    first = rel.split("/", 1)[0]
    return "/" in rel and (first == ".portable-sync-backups" or first.startswith("_disabled_"))


def glob_match(rel: str, globs) -> bool:
    rel = safe_rel(rel)
    rel_key = rel.casefold()
    for glob in globs or []:
        pattern = str(glob).replace("\\", "/").lstrip("/")
        if rel_key == pattern.casefold():
            return True
        if fnmatch.fnmatchcase(rel_key, pattern.casefold()):
            return True
    return False


def file_map(files) -> dict:
    out = {}
    for item in files or []:
        path = item.get("path")
        digest = item.get("sha1")
        if path and digest:
            out[rel_key(str(path))] = str(digest).lower()
    return out


def is_optional_helper(rel: str) -> bool:
    rel = safe_rel(rel)
    return rel in {
        "_updater/Windows-sync.bat", "更新mod-Windows端.bat", "更新mod-Mac端.command", "更新mod.bat", "启动游戏-Windows端.bat", "Windows-sync.bat",
        "_updater/macOS-sync.command", "启动游戏-Mac端.command", "macOS-sync.command",
        "_updater/portable-stage-daemon.ps1", "_updater/portable-stage-daemon.py",
    }

def ensure_executable_if_needed(path: pathlib.Path, rel: str):
    if safe_rel(rel).endswith(".command") and path.is_file():
        path.chmod(path.stat().st_mode | 0o755)

def backup_file(path: pathlib.Path, root: pathlib.Path, rel: str, backup_root: pathlib.Path):
    if not path.is_file():
        return
    dest = join_safe(backup_root, rel)
    if dest.exists():
        return  # Retain the version from before this run's first modification.
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)


def is_official_https_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url).strip())
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    return host not in ("", "localhost", "127.0.0.1", "::1")


def reset_download_policy():
    """Reset cached proxy detection for tests and each main() invocation."""
    global _PROXY_URL, _PROXY_OPENER
    _PROXY_URL = None
    _PROXY_OPENER = None


def normalize_proxy_url(raw: str) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower.startswith("socks"):
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    return raw


def parse_win_proxy_server(server: str) -> str:
    server = str(server or "").strip()
    if not server:
        return ""
    if "=" in server:
        parts = {}
        for item in server.split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            parts[key.strip().lower()] = value.strip()
        for key in ("https", "http"):
            if parts.get(key):
                return normalize_proxy_url(parts[key])
        return ""
    return normalize_proxy_url(server)


def detect_system_proxy() -> str:
    flag = str(os.environ.get("PORTABLE_SYNC_NOPROXY") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return ""
    override = normalize_proxy_url(os.environ.get("PORTABLE_SYNC_PROXY") or "")
    if override:
        return override
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = normalize_proxy_url(os.environ.get(key) or "")
        if value:
            return value
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if int(enable) == 1:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    parsed = parse_win_proxy_server(str(server or ""))
                    if parsed:
                        return parsed
        except Exception:
            pass
    return ""


def get_proxy_url() -> str:
    global _PROXY_URL
    if _PROXY_URL is None:
        _PROXY_URL = detect_system_proxy()
    return _PROXY_URL


def describe_proxy(url: str = "") -> str:
    parsed = urllib.parse.urlparse(url or get_proxy_url())
    host = parsed.hostname or ""
    if not host:
        return ""
    return f"{host}:{parsed.port}" if parsed.port else host


def is_local_or_private_host(host: str) -> bool:
    host = (host or "").lower().strip("[]")
    if host in ("", "localhost", "127.0.0.1", "::1"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 127 or a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return True
    return False


def is_private_update_url(url: str) -> bool:
    return is_local_or_private_host(urllib.parse.urlparse(str(url) or "").hostname or "")


def probe_timeout_for_url(url: str) -> float:
    host = urllib.parse.urlparse(str(url) or "").hostname or ""
    return _PRIVATE_PROBE_TIMEOUT_SEC if is_local_or_private_host(host) else _PUBLIC_PROBE_TIMEOUT_SEC


def order_manifest_urls(urls, last_good: str = "") -> list:
    """Prefer public addresses, then explicit LAN fallbacks; never invent a LAN URL."""
    seen = set()
    out = []

    def add(url):
        key = str(url or "").strip()
        if not key:
            return
        low = key.lower()
        if low in seen:
            return
        seen.add(low)
        out.append(key)

    public = [url for url in (urls or []) if not is_private_update_url(url)]
    # 玩家自己把 UPDATE-URL 写成局域网地址时保留；自动探测产生的 192.168 一律丢掉。
    candidates = public or list(urls or [])
    if last_good in candidates and not is_private_update_url(last_good):
        add(last_good)
    for url in candidates:
        add(url)
    if last_good and not is_private_update_url(last_good):
        add(last_good)
    for url in (urls or []):
        if is_private_update_url(url):
            add(url)
    return out


def file_verify_record(path: pathlib.Path, digest: str) -> dict:
    st = path.stat()
    return {"sha1": str(digest).lower(), "size": int(st.st_size), "mtime": int(st.st_mtime)}


def looks_like_current_file(path: pathlib.Path, expected: str, manifest_size, previous_hash: str, verified=None) -> bool:
    """Skip SHA1 when last completed sync already proved this exact file."""
    if not path.is_file() or not expected:
        return False
    size = path.stat().st_size
    if manifest_size not in (None, "", 0, "0"):
        try:
            if size != int(manifest_size):
                return False
        except (TypeError, ValueError):
            pass
    expected = str(expected).lower()
    previous_hash = str(previous_hash or "").lower()
    verified = verified or {}
    v_sha = str(verified.get("sha1") or "").lower()
    if previous_hash == expected:
        if v_sha == expected:
            try:
                if int(verified.get("size", -1)) == size and int(verified.get("mtime", -1)) == int(path.stat().st_mtime):
                    return True
            except (TypeError, ValueError):
                pass
            return sha1(path) == expected
        return sha1(path) == expected
    if v_sha == expected:
        try:
            if int(verified.get("size", -1)) == size and int(verified.get("mtime", -1)) == int(path.stat().st_mtime):
                return True
        except (TypeError, ValueError):
            pass
    return sha1(path) == expected


def home_timeout_sec(size=0) -> int:
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return 180
    return max(120, min(300, int(size / (128 * 1024)) + 60))


def sync_print(message: str) -> None:
    with _PRINT_LOCK:
        print(message)


def proxy_bypass_hosts() -> list:
    raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def url_bypasses_proxy(url: str) -> bool:
    host = (urllib.parse.urlparse(str(url)).hostname or "").lower()
    if is_local_or_private_host(host):
        return True
    for item in proxy_bypass_hosts():
        if item == "*":
            return True
        if item.startswith("."):
            if host.endswith(item) or host == item[1:]:
                return True
        elif host == item or host.endswith("." + item):
            return True
    return False


def official_opener():
    proxy = get_proxy_url()
    if not proxy:
        return _DIRECT_OPENER
    global _PROXY_OPENER
    if _PROXY_OPENER is None:
        _PROXY_OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return _PROXY_OPENER


def opener_for_official_url(url: str):
    if get_proxy_url() and not url_bypasses_proxy(url):
        return official_opener()
    return _DIRECT_OPENER


def official_urls(item) -> list:
    raw = []
    if isinstance(item, dict):
        for extra in item.get("downloads") or []:
            if extra:
                raw.append(str(extra).strip())
        url = item.get("url")
        if url:
            raw.append(str(url).strip())
    seen = set()
    out = []
    for url in raw:
        key = url.lower()
        if key in seen or not is_official_https_url(url):
            continue
        seen.add(key)
        out.append(url)
    return out


def download_fail_reason(exc: BaseException) -> str:
    msg = str(exc)
    if "SHA1" in msg or "校验失败" in msg:
        return "哈希不符"
    if "too slow" in msg.lower() or "太慢" in msg or "过低" in msg:
        return "太慢"
    if "timed out" in msg.lower() or "timeout" in msg.lower() or "超时" in msg:
        return "超时"
    if "404" in msg:
        return "404"
    if "403" in msg:
        return "403"
    if "name or service" in msg.lower() or "nodename" in msg.lower() or "getaddrinfo" in msg.lower():
        return "DNS"
    return "连接失败"


def copy_response(resp, out, timeout: int, min_bytes_after_timeout: int = 0, min_rate_bps: int = 0):
    started = time.time()
    got = 0
    next_check = float(timeout or 0)
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        out.write(chunk)
        got += len(chunk)
        if next_check <= 0:
            continue
        elapsed = time.time() - started
        if elapsed < next_check:
            continue
        if min_rate_bps and got < min_rate_bps * elapsed:
            raise TimeoutError("official source too slow")
        if min_bytes_after_timeout and got < min_bytes_after_timeout:
            raise TimeoutError("official source too slow")
        next_check = elapsed + max(1.0, float(timeout))


class UpdateError(RuntimeError):
    """A player-facing, localized failure with no private endpoint details."""


class LocalFileUpdateError(UpdateError):
    """A local write/replace failure; changing download sources cannot fix it."""


def local_file_error(exc, path, phase="写入文件"):
    code = getattr(exc, "winerror", None)
    number = getattr(exc, "errno", None)
    if code in (32, 33):
        return LocalFileUpdateError(
            f"[文件占用] {path}\n文件正在被其他程序使用，无法{phase}。原有文件未删除。"
            "请完全退出 Minecraft（不是返回标题界面），关闭打开该文件的压缩软件；"
            "若仍失败，请关闭该游戏实例残留的后台进程后重新运行更新器。")
    if code in (39, 112) or number == 28:
        return LocalFileUpdateError(f"[空间不足] 无法{phase}：{path}。请清理该磁盘空间后重新更新。")
    if code == 5 or isinstance(exc, PermissionError):
        return LocalFileUpdateError(f"[权限不足] 无法{phase}：{path}。请检查文件夹权限和安全软件的拦截记录后重试。")
    diagnostic = f"系统错误码 {code if code is not None else number}" if code is not None or number is not None else type(exc).__name__
    return LocalFileUpdateError(f"[替换失败] 无法{phase}：{path}（{diagnostic}）。请检查文件路径和磁盘状态后重试。")


def network_error_reason(exc):
    if isinstance(exc, urllib.error.HTTPError):
        details = {403: "更新源拒绝访问", 404: "更新源没有此文件", 429: "更新服务繁忙"}
        return details.get(exc.code, "更新服务返回错误") + f"（HTTP {exc.code}）"
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if "too slow" in str(cause).lower():
        return "下载速度持续过低"
    if isinstance(cause, (TimeoutError, socket.timeout)) or "timed out" in str(cause).lower():
        return "网络连接或读取超时"
    if isinstance(cause, socket.gaierror):
        return "域名解析失败"
    if "ssl" in type(cause).__name__.lower() or "certificate" in str(cause).lower():
        return "安全连接失败，请检查网络、代理和系统时间"
    return "无法连接更新源或连接已中断"


def windows_file_probe(path):
    """Return a Windows open error, if any; never change the tested file."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    create = api.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.WinError(ctypes.get_last_error())
    api.CloseHandle(handle)
    return None


def preflight_managed_files(root, files):
    """Probe Windows sharing locks before edits; POSIX open files differ."""
    if os.name != "nt":
        return
    seen = set()
    for item in files:
        rel = safe_rel(str(item["path"]))
        key = rel.casefold()
        if key in seen or is_optional_helper(rel):
            continue  # Entry-point shells may legitimately hold their own scripts.
        seen.add(key)
        target = join_safe(root, rel)
        if not target.is_file():
            continue
        exc = windows_file_probe(target)
        if exc is not None:
            if exc.winerror in (2, 3):
                continue  # A file removed between the existence check and open.
            message = str(local_file_error(exc, rel, "开始同步"))
            raise LocalFileUpdateError(message + "\n本次预检未通过，尚未开始替换、删除或清理客户端文件。") from None


def install_verified_file(temporary, destination, expected_sha1=None):
    # Same-volume atomic replacement; never unlink the previous file first.
    if destination.is_symlink():
        raise LocalFileUpdateError(f"[路径错误] 更新目标是链接，已停止替换：{destination}")
    destination = join_safe(destination.parent, destination.name)
    if _ACTIVE_JOURNAL is not None:
        try:
            rel = destination.relative_to(_ACTIVE_JOURNAL.root).as_posix()
        except ValueError:
            rel = None
        if rel is not None:
            try:
                _ACTIVE_JOURNAL.record(rel, expected_sha1 or sha1(temporary))
            except OSError as exc:
                raise local_file_error(exc, _ACTIVE_JOURNAL.path, "保存恢复记录") from None
    old_mode = destination.stat().st_mode if destination.is_file() else None
    try:
        if os.name == 'nt' and old_mode is not None:
            destination.chmod(old_mode | 0o200)
        os.replace(temporary, destination)
    except OSError as exc:
        # MoveFileEx may report access denied for an open destination. Confirm a
        # sharing lock before labeling it a permission problem.
        if getattr(exc, "winerror", None) == 5:
            probe_error = windows_file_probe(destination)
            if probe_error is not None and probe_error.winerror in (32, 33):
                exc = probe_error
        if old_mode is not None and destination.is_file():
            try:
                destination.chmod(old_mode)
            except OSError:
                pass  # Keep the original failure; never delete the destination.
        raise local_file_error(exc, destination, "替换文件") from None


_BUSY_STATUSES = (429, 500, 502, 503, 504)
_BUSY_MAX_RETRIES = 5
_BUSY_MAX_DELAY_SEC = 30.0


def busy_status(exc):
    code = getattr(exc, "code", None)
    return f"HTTP {code}" if code else type(exc).__name__


def busy_retry_delay(exc, busy_attempt):
    """Seconds to wait before retrying a busy/overloaded source, or None."""
    code = getattr(exc, "code", None)
    if not isinstance(exc, urllib.error.HTTPError) or code not in _BUSY_STATUSES:
        return None
    if busy_attempt >= _BUSY_MAX_RETRIES:
        return None
    delay = min(_BUSY_MAX_DELAY_SEC, 1.5 * (2 ** busy_attempt))
    try:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after and str(retry_after).strip().isdigit():
            delay = min(_BUSY_MAX_DELAY_SEC, max(delay, float(retry_after)))
    except Exception:
        pass
    return delay + random.uniform(0, delay * 0.25)


def download_file(url: str, dest: pathlib.Path, expected_sha1: str, opener=None, timeout=60, retries=1, min_bytes_after_timeout=0, min_rate_bps=0, backoff=True):
    expected_sha1 = str(expected_sha1).lower()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha1):
        raise UpdateError("[清单无效] 文件校验值无效，已停止下载。")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        remove_obsolete_parts(dest, expected_sha1)
        tmp = part_path(dest, expected_sha1)
    except OSError as exc:
        raise local_file_error(exc, dest, "创建下载临时文件") from None
    opener = opener or _DIRECT_OPENER
    if tmp.is_file() and sha1(tmp) == expected_sha1:
        install_verified_file(tmp, dest, expected_sha1)
        return
    attempt = busy_attempt = 0
    range_restarted = False
    while True:
        offset = tmp.stat().st_size if tmp.is_file() else 0
        headers = {"User-Agent": _SYNC_UA, "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as response:
                status = getattr(response, "status", None) or 200
                response_headers = getattr(response, "headers", {})
                content_range = str(response_headers.get("Content-Range", ""))
                total = None
                if status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not match or int(match[1]) != offset or int(match[2]) < offset or int(match[3]) <= int(match[2]):
                        if tmp.exists():
                            tmp.unlink()
                        if range_restarted:
                            raise UpdateError("[下载失败] 更新源返回的续传范围无效，请稍后重试。")
                        range_restarted = True
                        continue
                    total = int(match[3])
                    mode = "ab"
                    if offset:
                        sync_print(f"[续传] {dest.name}：从 {offset} 字节继续下载。")
                elif status == 200:
                    mode = "wb"  # A server may ignore Range; never append a full body.
                    offset = 0
                else:
                    raise UpdateError(f"[下载失败] 更新源返回 HTTP {status}，请稍后重试。")
                with tmp.open(mode) as out:
                    copy_response(response, out, timeout, min_bytes_after_timeout, min_rate_bps)
                received = tmp.stat().st_size - offset
                length = response_headers.get("Content-Length")
                if (length is not None and received != int(length)) or (total is not None and tmp.stat().st_size != total):
                    raise ConnectionError("incomplete response")
            break
        except Exception as exc:
            if isinstance(exc, LocalFileUpdateError):
                raise
            if isinstance(exc, OSError) and not isinstance(exc, urllib.error.URLError) and (
                    getattr(exc, "winerror", None) in (5, 32, 33, 39, 112) or getattr(exc, "errno", None) in (13, 28)):
                raise local_file_error(exc, dest, "写入下载临时文件") from None
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 416 and offset and not range_restarted:
                exc.close()
                tmp.unlink(missing_ok=True)
                range_restarted = True
                continue
            delay = busy_retry_delay(exc, busy_attempt) if backoff else None
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if delay is not None:
                busy_attempt += 1
                sync_print(f"[同步] 更新服务繁忙（{busy_status(exc)}），{delay:.1f} 秒后重试：{mask_url(url)}")
                time.sleep(delay)
                continue
            if attempt < retries:
                attempt += 1
                time.sleep(2)
                continue
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError(f"[下载失败] {dest.name}：{network_error_reason(exc)}。已保留下载进度，请检查网络后重试。") from None
    if sha1(tmp) != expected_sha1:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"[校验失败] {dest.name}：下载内容与清单不一致，旧文件未替换。请重新更新；反复失败时联系管理员。")
    install_verified_file(tmp, dest, expected_sha1)


# HYBRID_DOWNLOAD_V1: mirrors supply bytes only, never policy or a different release.
_HYBRID = None
_HYBRID_LOCK = threading.Lock()

def configure_hybrid(manifest, manifest_url):
    global _HYBRID
    urls = manifest.get('mirrorManifestUrls') or []
    _HYBRID = {'manifest': manifest, 'url': manifest_url, 'urls': urls[:3],
               'sources': None, 'failed': set()} if urls else None

def mirror_identity(manifest):
    rows = {}
    for row in manifest.get('files', []):
        rel = safe_rel(row['path'])
        if rel.casefold() in rows or not re.fullmatch('[0-9a-fA-F]{40}', str(row.get('sha1', ''))):
            raise ValueError('Invalid mirror file inventory')
        rows[rel.casefold()] = (int(row['size']), row['sha1'].lower())
    return str(manifest.get('packId', '')), str(manifest.get('version', '')), rows

def hybrid_url(source, rel):
    row = source['rows'][rel]
    remote = row.get('downloadPath', rel)
    if remote != rel and not re.fullmatch(r'blobs/[0-9a-f]{64}', str(remote)):
        raise ValueError('Invalid mirror downloadPath')
    return url_for(source['base'], remote)

def prepare_hybrid():
    h = _HYBRID
    if h is None:
        return []
    with _HYBRID_LOCK:
        if h['sources'] is not None:
            return h['sources']
        identity = mirror_identity(h['manifest'])
        sources = []
        seen = set()
        for url in [h['url']] + list(h['urls']):
            parsed = urllib.parse.urlsplit(str(url))
            if url in seen or parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                continue
            seen.add(url)
            try:
                m = h['manifest'] if url == h['url'] else json.loads(fetch_bytes(url, timeout=4).decode('utf-8-sig'))
                if mirror_identity(m) != identity:
                    sync_print('[混合源] 已忽略版本或文件清单不一致的备用源。')
                    continue
                source = {'base': url.rsplit('/', 1)[0]+'/', 'rows': {r['path']: r for r in m['files']}, 'score': 0.0}
                sample = max(m['files'], key=lambda r: int(r.get('size', 0)))
                started = time.monotonic()
                try:
                    request = urllib.request.Request(hybrid_url(source, sample['path']), headers={'User-Agent': _SYNC_UA, 'Range': 'bytes=0-131071'})
                    with _DIRECT_OPENER.open(request, timeout=3) as response:
                        data = response.read(131072)
                    source['score'] = len(data) / max(time.monotonic()-started, 0.001)
                except Exception as exc:
                    if isinstance(exc, urllib.error.HTTPError):
                        exc.close()
                    pass  # keep an unmeasured source as last-resort fallback
                sources.append(source)
            except Exception:
                sync_print('[混合源] 一个备用源暂不可用，继续使用其余来源。')
        sources.sort(key=lambda s: s['score'], reverse=True)
        h['sources'] = sources
        if sources:
            host = urllib.parse.urlsplit(sources[0]['base']).hostname
            sync_print(f'[混合源] 本机采样优先选择 {host}；失败或持续低速会自动换源。')
        return sources

def download_hybrid_entry(item, dest, expected, rel):
    sources = prepare_hybrid()
    error = None
    for source in sources:
        if source['base'] in _HYBRID['failed']:
            continue
        try:
            download_file(hybrid_url(source, rel), dest, expected,
                          opener=_DIRECT_OPENER, timeout=home_timeout_sec(item.get("size", 0)), retries=0,
                          min_rate_bps=65536 if len(sources)>1 else 0)
            return 'home'
        except LocalFileUpdateError:
            raise
        except Exception as exc:
            error = exc
            sync_print('[混合源] 下载失败、低速或校验不符，正在切换备用源。')
    if error:
        raise error
    raise RuntimeError('本轮所有更新源均不可用，请稍后重试。')


def download_manifest_entry(item, base_url: str, dest: pathlib.Path, expected_sha1: str, rel: str) -> str:
    # Prefer official exact-hash URLs, then the pack server/mirrors.
    for url in official_urls(item if isinstance(item, dict) else {}):
        openers = [_DIRECT_OPENER]
        proxy = opener_for_official_url(url)
        if proxy is not _DIRECT_OPENER:
            openers.append(proxy)
        for opener in openers:
            try:
                download_file(url, dest, expected_sha1, opener=opener,
                              timeout=_OFFICIAL_TIMEOUT_SEC, retries=0,
                              min_bytes_after_timeout=_OFFICIAL_MIN_BYTES,
                              min_rate_bps=_OFFICIAL_MIN_RATE, backoff=False)
                return "official"
            except LocalFileUpdateError:
                raise
            except Exception as exc:
                sync_print(f"[同步] {urllib.parse.urlsplit(url).hostname} 不可用（{download_fail_reason(exc)}），继续尝试下一下载源：{rel}")
    if _HYBRID is not None:
        return download_hybrid_entry(item, dest, expected_sha1, rel)
    size = 0
    if isinstance(item, dict):
        size = item.get("size") or 0
    remote = item.get("downloadPath", rel) if isinstance(item, dict) else rel
    if remote != rel and not re.fullmatch(r"blobs/[0-9a-f]{64}", str(remote)):
        raise ValueError("Invalid cloud downloadPath")
    download_file(
        url_for(base_url, remote), dest, expected_sha1,
        opener=_DIRECT_OPENER, timeout=home_timeout_sec(size),
    )
    return "home"


def existing_hash_index(root: pathlib.Path) -> dict:
    # One SHA1 can map to several local files (TACZ gun-pack sounds are duplicated
    # across assets/ccrp and assets/classicr), so keep a list per hash instead of
    # only the first. Keeping only one made later entries with the same hash point
    # at a path an earlier adopt had already moved away -> "does not exist" crash
    # (2026-07-08 player-reported).
    out = {}
    for dirname in ("mods", "config", "defaultconfigs", "kubejs", "scripts", "resourcepacks", "shaderpacks", "data"):
        base = root / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            try:
                path = join_safe(root, rel)
            except LocalFileUpdateError:
                continue  # A shared/link directory is not an owned adoption source.
            if rel.startswith("mods/.connector/") or "/.connector/" in rel:
                continue
            digest = sha1(path)
            if not digest:
                continue
            out.setdefault(digest, []).append(path)
    return out


def adopt_source(index: dict, expected: str, target: pathlib.Path, manifest_targets: set):
    """Pick a local file with matching sha1 to satisfy `target` without re-downloading.
    Returns (path, mode): 'move' for a spare copy safe to consume, or 'copy' when the
    only matches are themselves files the manifest needs (duplicate instead of stealing).
    Prunes stale entries so a hash shared by many identical files can't point at a path
    an earlier adopt already moved away."""
    paths = index.get(expected)
    if not paths:
        return None
    alive = [p for p in paths if p.is_file()]
    index[expected] = alive
    target_res = str(target.resolve()).lower()
    spare = None
    twin = None
    for p in alive:
        pr = str(p.resolve()).lower()
        if pr == target_res:
            continue
        if pr in manifest_targets:
            if twin is None:
                twin = p
        elif spare is None:
            spare = p
    if spare is not None:
        index[expected] = [p for p in alive if p is not spare]
        return spare, "move"
    if twin is not None:
        return twin, "copy"
    return None


def set_option_line(path: pathlib.Path, key: str, value: str):
    lines = []
    found = False
    if path.is_file():
        lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    out = []
    for line in lines:
        if ":" in line and line.split(":", 1)[0] == key:
            out.append(f"{key}:{value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}:{value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def apply_option_defaults(root: pathlib.Path, manifest: dict, backup_root: pathlib.Path, preserved_deletions=None, initial_sync=False) -> int:
    preserved_deletions = preserved_deletions or set()
    config = manifest.get("playerOptions") or {}
    defaults = config.get("defaults") or {}
    if not defaults:
        return 0
    mode = str(config.get("apply") or "missing")
    targets = config.get("targets") or ["options.txt"]
    changed = 0
    for rel in targets:
        rel = safe_rel(rel)
        if rel.lower() in preserved_deletions:
            continue
        path = join_safe(root, rel)
        exists = path.is_file()
        if mode == "missing" and exists:
            continue
        current_values = {}
        if exists:
            for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    current_values[key] = value
        pending = [
            (str(key), str(value))
            for key, value in defaults.items()
            if current_values.get(str(key)) != str(value)
        ]
        if not pending:
            continue
        if exists:
            backup_file(path, root, rel, backup_root)
        for key, value in pending:
            set_option_line(path, key, value)
            changed += 1
    if changed:
        print(f"[修复] 已写入安全默认选项：{changed} 项")
    return changed


def ensure_required_resource_packs(
    root: pathlib.Path,
    manifest: dict,
    backup_root: pathlib.Path,
    preserved_deletions=None,
) -> int:
    """Merge server-required resource packs into options.txt without replacing player packs."""
    preserved_deletions = preserved_deletions or set()
    config = manifest.get("playerOptions") or {}
    requested = config.get("requiredResourcePacks") or []
    if not isinstance(requested, list):
        return 0

    required: list[str] = []
    for value in requested:
        pack_id = str(value).strip()
        if not pack_id or pack_id in required:
            continue
        if pack_id.startswith("file/"):
            try:
                pack_rel = safe_rel("resourcepacks/" + pack_id[5:])
            except ValueError:
                print(f"[警告] 忽略不安全的必选资源包：{pack_id}")
                continue
            if not join_safe(root, pack_rel).is_file():
                print(f"[警告] 必选资源包尚未同步，暂不启用：{pack_id}")
                continue
        required.append(pack_id)
    if not required:
        return 0

    changed = 0
    for target_rel in config.get("targets") or ["options.txt"]:
        rel = safe_rel(str(target_rel))
        if rel.lower() in preserved_deletions:
            continue
        path = join_safe(root, rel)
        if not path.is_file():
            continue

        raw_value = None
        language = None
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key == "resourcePacks":
                raw_value = value
            elif key == "lang":
                language = value.strip()
        if not language:
            print(f"[警告] {rel} 未找到 lang:zh_cn；Relics 中文需要把游戏语言设为简体中文。未自动修改玩家语言。")
        elif language != "zh_cn":
            print(f"[警告] {rel} 当前语言为 {language}；Relics 中文需要 lang:zh_cn。未自动修改玩家语言。")
        try:
            current = json.loads(raw_value) if raw_value is not None else ["vanilla"]
        except (TypeError, json.JSONDecodeError):
            print(f"[警告] 无法解析 {rel} 中的 resourcePacks，已保留原值。")
            continue
        if not isinstance(current, list) or any(not isinstance(item, str) for item in current):
            print(f"[警告] {rel} 中的 resourcePacks 不是字符串列表，已保留原值。")
            continue

        # Remove any earlier occurrence, then append: Minecraft's later entries
        # have higher priority, so the server localization overrides mod_resources.
        desired = [item for item in current if item not in required]
        desired.extend(required)
        # NeoForge inserts a missing required mod_resources pack at TOP on launch.
        # Put the missing baseline below player packs before the game can do so.
        if config.get("ensureModResourcesBaseline") is True and "mod_resources" not in desired:
            index = desired.index("vanilla") + 1 if "vanilla" in desired else 0
            desired.insert(index, "mod_resources")
        if desired == current:
            print(f"[检查] 服务器汉化包已启用且处于最高优先级：{rel}")
            continue
        backup_file(path, root, rel, backup_root)
        set_option_line(
            path,
            "resourcePacks",
            json.dumps(desired, ensure_ascii=False, separators=(",", ":")),
        )
        changed += 1
        print(f"[修复] 已启用服务器汉化包并保留其他资源包：{rel}")
    return changed


def nbt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def named_tag(tag_type: int, name: str) -> bytes:
    return bytes([tag_type]) + nbt_string(name)


def write_servers_dat(path: pathlib.Path, name: str, ip: str):
    data = bytearray()
    data += named_tag(10, "")
    data += named_tag(9, "servers")
    data += bytes([10])
    data += struct.pack(">i", 1)
    data += named_tag(8, "name") + nbt_string(name)
    data += named_tag(8, "ip") + nbt_string(ip)
    data += named_tag(1, "acceptTextures") + bytes([1])
    data += bytes([0])
    data += bytes([0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))


def show_launcher_hints(root: pathlib.Path):
    launcher_root = root / "_launchers"
    if not launcher_root.is_dir():
        return
    items = [
        ("PCL", launcher_root / "PCL.exe"),
        ("PCL 最近启动脚本", launcher_root / "PCL" / "LatestLaunch.bat"),
        ("HMCL", launcher_root / "HMCL.jar"),
    ]
    found = [(name, path) for name, path in items if path.is_file()]
    if not found:
        return
    print("[启动器] 已同步启动器文件：")
    for name, path in found:
        print(f"[启动器] {name}: {path}")


def launcher_instance_name(root: pathlib.Path, manifest: dict) -> str:
    launcher = manifest.get("launcher") or {}
    pcl = manifest.get("pcl") or {}
    name = str(launcher.get("instanceName") or "").strip()
    if not name:
        name = str(pcl.get("instanceName") or "").strip()
    if name:
        return name
    candidates = [p for p in root.glob("*.json") if p.name not in {"launcher_profiles.json", ".portable-sync-state.json"}]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_size).stem
    return root.name


def minecraft_home_for_instance(root: pathlib.Path):
    parent = root.parent
    grandparent = parent.parent if parent else None
    if grandparent and parent.name.lower() == "versions" and grandparent.name.lower() == ".minecraft":
        return grandparent.parent
    return None


def ensure_neoforge_client_extra(root: pathlib.Path, manifest: dict, backup_root: pathlib.Path) -> bool:
    """把已安装的纯资源 client-extra 加入当前实例 classpath，供 CSL 读取版本元数据。"""
    if str((manifest.get("loader") or {}).get("type") or "").strip().lower() != "neoforge":
        return False

    def unfinished(reason):
        print(f"[启动修复未完成] {reason}；当前实例版本 JSON 未改。")
        return False

    home = minecraft_home_for_instance(root)
    if home is None:
        return unfinished("当前目录不是 .minecraft/versions/<实例>，无法定位客户端库")
    mc = str(manifest.get("minecraftVersion") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", mc):
        return unfinished("清单缺少有效 Minecraft 版本")
    try:
        preferred = root / (root.name + ".json")
        paths = [preferred] if preferred.is_file() else sorted(root.glob("*.json"))
        candidates = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"] and (data.get("mainClass") or data.get("inheritsFrom")):
                candidates.append((path, data))
        if len(candidates) != 1:
            return unfinished("找不到唯一的当前实例版本 JSON")
        path, data = candidates[0]
        libraries = data.get("libraries", [])
        if not isinstance(libraries, list):
            return unfinished("当前实例 libraries 格式不适用")
        game_args = (data.get("arguments") or {}).get("game") or []
        if not isinstance(game_args, list):
            return unfinished("当前实例游戏参数格式不适用")

        def argument(name):
            for index, value in enumerate(game_args):
                if value == name and index + 1 < len(game_args) and isinstance(game_args[index + 1], str):
                    return game_args[index + 1]
            return ""

        names = [str(item.get("name") or "") for item in libraries if isinstance(item, dict)]
        if not argument("--fml.neoForgeVersion") and not any(name.startswith("net.neoforged:neoforge:") for name in names):
            return unfinished("当前实例版本 JSON 没有 NeoForge 标记，已跳过")
        declared_mc = argument("--fml.mcVersion")
        parent_version = str(data.get("inheritsFrom") or "")
        if (declared_mc and declared_mc != mc) or (re.fullmatch(r"\d+\.\d+(?:\.\d+)?", parent_version) and parent_version != mc):
            return unfinished("当前实例 Minecraft 版本与更新清单不一致")
        neoform = argument("--fml.neoFormVersion") or argument("--fml.mcpVersion")
        if neoform and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", neoform):
            return unfinished("当前实例 NeoForm 版本格式不适用")
        minecraft_dir = home / ".minecraft"
        client_dir = minecraft_dir / "libraries" / "net" / "minecraft" / "client"
        if neoform:
            coordinate_version = mc + "-" + neoform
            jars = [client_dir / coordinate_version / ("client-" + coordinate_version + "-extra.jar")]
        else:
            jars = sorted(client_dir.glob(mc + "-*/client-*-extra.jar")) if client_dir.is_dir() else []
        valid = []
        invalid = []
        for jar in jars:
            if not jar.is_file():
                continue
            # 只读取 FML 已安装库目录里的标准 extra 工件；不扫描别的实例，不下载依赖。
            if jar.name != "client-" + jar.parent.name + "-extra.jar":
                continue
            try:
                with zipfile.ZipFile(jar) as archive:
                    entries = archive.namelist()
                    if any(name.lower().endswith(".class") for name in entries):
                        invalid.append("client-extra 包含 class，拒绝加入普通 classpath")
                        continue
                    if entries.count("version.json") != 1:
                        invalid.append("client-extra 缺少唯一 version.json")
                        continue
                    metadata = json.loads(archive.read("version.json").decode("utf-8-sig"))
                    if metadata.get("id") != mc:
                        invalid.append("client-extra 内 Minecraft 版本与更新清单不一致")
                        continue
                valid.append(jar)
            except (OSError, ValueError, zipfile.BadZipFile):
                invalid.append("client-extra 无法读取")
        if len(valid) != 1:
            if len(valid) > 1:
                return unfinished("同一 Minecraft 版本存在多个 client-extra，且实例未指定 NeoForm 版本")
            return unfinished(invalid[0] if invalid else "缺少当前实例已安装的 client-extra.jar，请先用启动器补全游戏库后重新更新")
        jar = valid[0]
        name = "net.minecraft:client:" + jar.parent.name + ":extra"
        relative = jar.relative_to(minecraft_dir / "libraries").as_posix()
        for item in libraries:
            if not isinstance(item, dict):
                continue
            existing = str(item.get("name") or "")
            if existing == name:
                downloads = item.get("downloads")
                artifact = downloads.get("artifact") if isinstance(downloads, dict) else None
                ordinary = downloads is None or (isinstance(artifact, dict) and artifact.get("path", relative) == relative)
                if ordinary and not item.get("rules") and not item.get("natives") and not item.get("extract"):
                    print("[启动修复] 当前实例已包含 client-extra 普通库，无需修改。")
                    return False
                return unfinished("client-extra 已有特殊规则或分类器配置，请检查启动器版本配置")
            if existing.startswith("net.minecraft:client:") and existing.endswith(":extra"):
                return unfinished("当前实例已声明其他 client-extra 版本，未覆盖玩家配置")
        libraries.append({"name": name, "downloads": {"artifact": {"path": relative, "sha1": sha1(jar), "size": jar.stat().st_size}}})
        data["libraries"] = libraries
        encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        backup_file(path, root, path.name, backup_root)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
                temporary = pathlib.Path(stream.name)
                stream.write(encoded)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        print("[启动修复] 已补齐当前实例 client-extra 资源库。请完全退出游戏并重新打开启动器，再启动游戏。")
        return True
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return unfinished("无法修复 client-extra 启动配置：" + str(exc))


def stable_path_cache(path: pathlib.Path) -> int:
    # Python 内建 hash() 受哈希随机化影响，每次运行都变；用 SHA1 保证跨运行稳定。
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2147483647


def write_pcl_global_config(home: pathlib.Path, mc_dir_value: str, instance_name: str, backup_root: pathlib.Path, backup_prefix: str = ""):
    # 温和写入：PCL.ini 缺失才创建；Setup.ini 只维护 LaunchFolderSelect 一行，
    # 保留玩家自己的其他 PCL 设置，避免每次同步重置启动器个人配置。
    pcl_ini = home / "PCL.ini"
    if not pcl_ini.is_file():
        cache = stable_path_cache(home)
        pcl_ini.write_text(
            f"InstanceCache:{cache}\nVersion:{instance_name}\nCardKey1:2\nCardValue1:2:{instance_name}:1:\nCardCount:1\n",
            encoding="utf-8",
        )

    pcl_dir = home / "PCL"
    pcl_dir.mkdir(parents=True, exist_ok=True)
    setup = pcl_dir / "Setup.ini"
    desired = f"LaunchFolderSelect:{mc_dir_value}"
    if setup.is_file():
        lines = setup.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        if desired in lines:
            return
        backup_file(setup, home, backup_prefix + "PCL/Setup.ini", backup_root)
        kept = [ln for ln in lines if not ln.startswith("LaunchFolderSelect:")]
        setup.write_text("\n".join([desired] + kept) + "\n", encoding="utf-8")
    else:
        setup.write_text(desired + "\n", encoding="utf-8")


def copy_launcher_to_root(root: pathlib.Path, rel: str, backup_root: pathlib.Path):
    source = join_safe(root / "_launchers", rel)
    if not source.is_file():
        return None
    dest = join_safe(root, rel)
    # 启动器（HMCL / PCL 等）会自我升级：玩家在启动器里点了“升级”后，本地 jar/exe 比包内
    # 自带的更新。这里只做「首次落位」——缺失才复制，已存在就保留玩家自己的版本，绝不用包内
    # 旧版覆盖回去，否则每次同步都把玩家升级好的 HMCL 打回原形（2026-07-11 实锤）。
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    if dest.suffix == ".command":
        dest.chmod(dest.stat().st_mode | 0o755)
    return dest


def apply_launcher_profile(root: pathlib.Path, manifest: dict, backup_root: pathlib.Path):
    if not (root / "_launchers").is_dir():
        return
    instance_name = launcher_instance_name(root, manifest)
    pack_name = str(manifest.get("packName") or instance_name).strip() or instance_name
    mc_version = str(manifest.get("minecraftVersion") or "").strip()
    loader = manifest.get("loader") or {}
    loader_type = str(loader.get("type") or "").strip()
    loader_version = str(loader.get("version") or "").strip()

    for rel in ("PCL.exe", "Plain Craft Launcher.exe", "SakuraLauncher.exe", "HMCL.jar"):
        copy_launcher_to_root(root, rel, backup_root)

    mc_home = minecraft_home_for_instance(root)
    if mc_home:
        for rel in ("PCL.exe", "Plain Craft Launcher.exe", "SakuraLauncher.exe", "HMCL.jar"):
            source = join_safe(root / "_launchers", rel)
            if source.is_file():
                dest = mc_home / rel
                # 同 copy_launcher_to_root：启动器自升级后不回退，缺失才落位。
                if dest.is_file():
                    continue
                shutil.copy2(source, dest)

    profile_path = root / "launcher_profiles.json"
    backup_file(profile_path, root, "launcher_profiles.json", backup_root)
    profile = {
        "profiles": {
            "Portable": {
                "icon": "Furnace",
                "name": pack_name,
                "lastVersionId": instance_name,
                "type": "custom",
                "lastUsed": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000Z"),
            }
        },
        "selectedProfile": "Portable",
        "clientToken": "23323323323323323323323323323333",
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pcl_ini = root / "PCL.ini"
    backup_file(pcl_ini, root, "PCL.ini", backup_root)
    cache = stable_path_cache(root)
    pcl_ini.write_text(
        f"InstanceCache:{cache}\nVersion:{instance_name}\nCardKey1:2\nCardValue1:2:{instance_name}:1:\nCardCount:1\n",
        encoding="utf-8",
    )

    pcl_dir = root / "PCL"
    pcl_dir.mkdir(parents=True, exist_ok=True)
    setup = pcl_dir / "Setup.ini"
    backup_file(setup, root, "PCL/Setup.ini", backup_root)
    loader_label = loader_type[:1].upper() + loader_type[1:] if loader_type else ""
    info = f"正式版 {mc_version}, {loader_label} {loader_version}".strip().rstrip(",") if mc_version else pack_name
    lines = [
        "State:9",
        f"VersionVanillaName:{mc_version}" if mc_version else "",
        "VersionArgumentIndieV2:True",
        f"Info:{info}",
        "VersionLiteLoader:False",
    ]
    if loader_type.lower() == "forge" and loader_version:
        lines.append(f"VersionForge:{loader_version}")
    if loader_type.lower() == "fabric" and loader_version:
        lines.append(f"VersionFabric:{loader_version}")
    lines.extend([
        "Logo:pack://application:,,,/Plain Craft Launcher 2;component/Images/Blocks/Anvil.png",
        f"LaunchFolderSelect:{root}",
    ])
    setup.write_text("\n".join(line for line in lines if line) + "\n", encoding="utf-8")

    if mc_home:
        profile_home = mc_home / ".minecraft" / "launcher_profiles.json"
        backup_file(profile_home, mc_home, ".minecraft/launcher_profiles.json", backup_root)
        profile_home.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_pcl_global_config(mc_home, "$.minecraft\\", instance_name, backup_root)

    # Mirror launcher config for older wrappers that still start from _launchers.
    for rel in ("launcher_profiles.json", "PCL.ini", "PCL/Setup.ini"):
        src = join_safe(root, rel)
        dst = join_safe(root / "_launchers", rel)
        if src.is_file():
            backup_file(dst, root, "_launchers/" + rel, backup_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    print(f"[启动器] 已写入本地实例配置：{instance_name}")

def apply_server_list(root: pathlib.Path, manifest: dict, backup_root: pathlib.Path, preserved_deletions=None, initial_sync=False):
    preserved_deletions = preserved_deletions or set()
    config = manifest.get("serverList") or {}
    if not config.get("enabled", False):
        return
    target = safe_rel(config.get("target") or "servers.dat")
    name = str(config.get("name") or manifest.get("serverName") or "Minecraft Server")
    address = str(config.get("address") or manifest.get("serverAddress") or "")
    if not address:
        return
    if target.lower() in preserved_deletions:
        return
    path = join_safe(root, target)
    if bool(config.get("writeIfMissingOnly", True)):
        if path.is_file():
            return
        if not initial_sync:
            return
    backup_file(path, root, target, backup_root)
    write_servers_dat(path, name, address)
    print(f"[修复] 已写入服务器列表：{name} / {address}")


def move_to_disabled(path: pathlib.Path, disabled: pathlib.Path) -> pathlib.Path:
    disabled.mkdir(parents=True, exist_ok=True)
    dest = disabled / path.name
    if dest.exists():
        dest = disabled / f"{path.stem}.{dt.datetime.now().strftime('%Y%m%d%H%M%S')}{path.suffix}"
    shutil.move(str(path), dest)
    return dest


def remove_connector_cache(root: pathlib.Path):
    cache = root / "mods" / ".connector"
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)
        print("[修复] 已清理 Connector 运行缓存：mods/.connector")


def disable_launcher_repair_index(*roots: pathlib.Path):
    for root in roots:
        index = root / "modrinth.index.json"
        if index.is_file():
            dest = move_to_disabled(index, root / "_disabled_launcher_repair")
            print(f"[修复] 已禁用启动器修包索引：{dest}")


def jar_mod_id(path: pathlib.Path):
    try:
        with zipfile.ZipFile(path) as zf:
            for name in ("fabric.mod.json", "quilt.mod.json"):
                try:
                    data = json.loads(zf.read(name).decode("utf-8-sig"))
                    if data.get("id"):
                        return str(data["id"]).lower()
                    if isinstance(data.get("quilt_loader"), dict) and data["quilt_loader"].get("id"):
                        return str(data["quilt_loader"]["id"]).lower()
                except KeyError:
                    pass
            for name in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                try:
                    text = zf.read(name).decode("utf-8", "ignore")
                except KeyError:
                    continue
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("modId") and "=" in line:
                        return line.split("=", 1)[1].strip().strip('"\'').lower()
            try:
                info = json.loads(zf.read("mcmod.info").decode("utf-8-sig"))
                if isinstance(info, list) and info and info[0].get("modid"):
                    return str(info[0]["modid"]).lower()
            except Exception:
                pass
    except Exception:
        return None
    return None


def disable_duplicate_mods(root: pathlib.Path, manifest: dict):
    mods = root / "mods"
    if not mods.is_dir():
        return
    managed_paths = set()
    managed_sha1 = {}
    for item in manifest.get("files") or []:
        rel = safe_rel(item.get("path", ""))
        if fnmatch.fnmatchcase(rel, "mods/*.jar"):
            managed_paths.add(rel.lower())
            managed_sha1[str(item.get("sha1", "")).lower()] = rel
    disabled = root / "_disabled_mod_duplicates"
    moved = 0
    for jar in sorted(mods.glob("*.jar")):
        rel = jar.relative_to(root).as_posix()
        if rel.lower() in managed_paths:
            continue
        digest = sha1(jar)
        if digest in managed_sha1:
            dest = move_to_disabled(jar, disabled)
            moved += 1
            print(f"[修复] 已按 SHA1 归档旧重复 mod：{rel} -> {dest.name}")
    groups = {}
    for jar in sorted(mods.glob("*.jar")):
        mod_id = jar_mod_id(jar)
        if mod_id:
            groups.setdefault(mod_id, []).append(jar)
    for mod_id, jars in groups.items():
        if len(jars) <= 1:
            continue
        keep = sorted(jars, key=lambda p: ((p.relative_to(root).as_posix().lower() in managed_paths), p.stat().st_mtime), reverse=True)[0]
        for jar in jars:
            if jar == keep:
                continue
            dest = move_to_disabled(jar, disabled)
            moved += 1
            print(f"[修复] 已归档重复 modId={mod_id}；保留 {keep.name}，移动 {jar.name}")
    if moved:
        print(f"[修复] 重复 mod 清理完成，移动 {moved} 个")


def sync_instance(root, args) -> int:
    global _ACTIVE_JOURNAL
    reset_download_policy()
    manifest_urls = read_update_urls(root, args.manifest_url)
    state_path = join_safe(root, ".portable-sync-state.json")
    state = read_state(root)
    last_good = "" if args.manifest_url else str(state.get("lastManifestUrl") or "")
    if not args.manifest_url:
        manifest_urls = order_manifest_urls(manifest_urls, last_good)
    print("[同步] 清单候选：" + "；".join(mask_url(url) for url in manifest_urls))
    proxy = get_proxy_url()
    if proxy:
        print(f"[同步] 已检测到系统代理 {describe_proxy(proxy)}，官方源先直连，受阻后可使用现有代理；更新服务直连。")
    else:
        print("[同步] 未检测到系统代理，官方源直连（太慢会在约 8 秒内回落更新服务）。")
    manifest = None
    selected_manifest_url = ""
    last_exc = None
    endpoint_errors = []
    for candidate in manifest_urls:
        timeout = probe_timeout_for_url(candidate)
        attempts = 1 if timeout <= 2 else 2
        for attempt in range(attempts):
            try:
                raw = fetch_bytes(candidate, timeout=timeout)
                candidate_manifest = json.loads(raw.decode("utf-8-sig"))
                if not candidate_manifest.get("files"):
                    raise ValueError("manifest 没有 files")
                manifest = candidate_manifest
                selected_manifest_url = candidate
                break
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(1)
        if manifest is not None:
            break
        endpoint_errors.append(f"{mask_url(candidate)} -> {last_exc}")

    # 完整懒人包通常自带 server-manifest.json。更新源暂时不可达时，
    # 用本地清单继续做哈希对账，已有文件可以直接完成同步，避免把整包判成失败。
    if manifest is None:
        local_manifest = root / "server-manifest.json"
        try:
            if local_manifest.is_file():
                local_candidate = json.loads(local_manifest.read_text(encoding="utf-8-sig"))
                if local_candidate.get("files"):
                    manifest = local_candidate
                    selected_manifest_url = manifest_urls[0]
                    print("[降级] 更新源暂时不可达，改用包内 server-manifest.json 做本地对账；缺失文件仍需更新源恢复后补下。")
        except Exception:
            manifest = None
    if manifest is None:
        raise SystemExit(
            f"[清单获取失败] {network_error_reason(last_exc)}。已尝试 {len(manifest_urls)} 个更新地址。"
            "请确认当前更新地址可达，以及路由器已把更新端口转到服机。"
        )
    manifest_url = selected_manifest_url
    if last_good and manifest_url == last_good:
        print(f"[同步] 沿用上次可用更新地址：{mask_url(manifest_url)}")
    elif manifest_url != manifest_urls[0]:
        print(f"[同步] 已切换到可用备用地址：{mask_url(manifest_url)}")
    validate_manifest(root, manifest)
    files = manifest.get("files") or []
    _ACTIVE_JOURNAL = RecoveryJournal(root, manifest)
    previous_files = _ACTIVE_JOURNAL.previous_files(state.get("files") or [])
    if not files:
        raise SystemExit("清单没有 files 文件列表。")
    base_url = manifest_url.rsplit("/", 1)[0] + "/"
    configure_hybrid(manifest, manifest_url)
    initial_sync = not bool(previous_files)
    previous_version = str(state.get("version") or "")
    current_version = str(manifest.get("version") or "")
    version_changed = previous_version != current_version
    previous = file_map(previous_files)
    # key 是小写化的，删除旧文件时要还原状态文件里的原始大小写（大小写敏感卷才找得到文件）。
    previous_rel = {}
    for item in previous_files:
        p = item.get("path")
        if p and item.get("sha1"):
            try:
                previous_rel[rel_key(str(p))] = safe_rel(str(p))
            except ValueError:
                continue
    new = file_map(files)
    verified = {}
    raw_verified = state.get("verified") or {}
    if isinstance(raw_verified, dict):
        for raw_key, raw_val in raw_verified.items():
            try:
                verified[rel_key(str(raw_key))] = raw_val if isinstance(raw_val, dict) else {}
            except ValueError:
                continue
    preserve_changes = manifest.get("preserveLocalChangeGlobs") or []
    preserve_deletions = manifest.get("preserveLocalDeletionGlobs") or []
    force_delete_globs = manifest.get("forceDeleteGlobs") or []
    # 强制同步：匹配的文件无视保留规则、一律以服务端为准（覆盖前照常备份）。
    # 服务端确需推送玩家常改的配置时，临时把路径加进 forceSyncGlobs 发布一版即可。
    force_sync_globs = manifest.get("forceSyncGlobs") or []
    # 按操作系统跳过的文件（如 YSM 不支持 macOS）：本平台不下载，本地已有则备份后移除。
    plat = "mac" if sys.platform == "darwin" else ("windows" if sys.platform.startswith("win") else "linux")
    platform_excludes = (manifest.get("platformExcludeGlobs") or {}).get(plat) or []
    additive_dirs = manifest.get("additiveDirs") or ["shaderpacks", "resourcepacks", "schematics", "saves", "screenshots"]
    cleanup = manifest.get("cleanup") or {}
    preserve_player = bool(manifest.get("preservePlayerCustomizations", True))
    adopt_existing = bool(manifest.get("adoptExistingFiles", True))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:6]
    backup_root = root / ".portable-sync-backups" / stamp

    planned_cleanup = cleanup_candidates(root, force_delete_globs)
    extra_targets = [{"path": p.relative_to(root).as_posix()} for p in planned_cleanup]
    extra_targets.extend({"path": str(rel)} for rel in ((manifest.get("playerOptions") or {}).get("targets") or ["options.txt"]))
    preflight_managed_files(root, list(files) + previous_files + extra_targets)

    if cleanup.get("removeConnectorCache", True):
        remove_connector_cache(root)
    if cleanup.get("disableLauncherRepairIndex", True):
        disable_launcher_repair_index(pathlib.Path.cwd(), root)

    # 接管索引惰性构建：日常同步通常没有缺失文件，这时不必对 mods/config/resourcepacks 等
    # 目录做全量 SHA1 扫描（大包一次要几十秒）。只在真的遇到缺失文件需要接管时才建一次。
    existing = None

    def adopt_index():
        nonlocal existing
        if existing is None:
            print("[同步] 有文件缺失，正在建立本地文件哈希索引（仅本次需要）...")
            existing = existing_hash_index(root)
        return existing
    # All manifest target paths: lets adopt tell a spare copy (movable) from a twin
    # the manifest still needs elsewhere (copy instead of steal).
    manifest_targets = set()
    for item in files:
        try:
            manifest_targets.add(str(join_safe(root, str(item["path"])).resolve()).lower())
        except (ValueError, KeyError):
            continue
    downloaded = adopted = skipped = preserved = removed = 0
    updated_paths = []
    adopted_paths = []
    forced_applied_paths = []
    preserved_deletions = set()
    # 保留了玩家修改、但服务端同一文件本次其实也有更新的清单：结尾集中提醒一次。
    preserved_conflicts = []
    download_jobs = []
    for item in files:
        rel = safe_rel(str(item["path"]))
        key = rel.lower()
        expected = str(item["sha1"]).lower()
        target = join_safe(root, rel)
        exists = target.is_file()
        if platform_excludes and glob_match(rel, platform_excludes):
            # 本平台不适用（如 macOS 不支持 YSM）：不下载。本地残留由下方扫描统一清除。
            continue
        had_previous = key in previous
        previous_hash = previous.get(key, "")
        if exists and looks_like_current_file(target, expected, item.get("size"), previous_hash, verified.get(key)):
            ensure_executable_if_needed(target, rel)
            verified[key] = file_verify_record(target, expected)
            skipped += 1
            continue
        current = sha1(target) if exists else ""
        if exists and current == expected:
            ensure_executable_if_needed(target, rel)
            verified[key] = file_verify_record(target, expected)
            skipped += 1
            continue
        force_sync = glob_match(rel, force_sync_globs)
        if not exists and had_previous and preserve_player and not force_sync and glob_match(rel, preserve_deletions):
            print(f"[保留玩家删除] {rel}")
            preserved_deletions.add(key)
            preserved += 1
            continue
        if exists and preserve_player and not force_sync and glob_match(rel, preserve_changes):
            # 三方对比：current==previous_hash 说明玩家没改过，照常走下面的更新；
            # 改过（或没有基线）就保留。若服务端同一文件本次也变了，记下来结尾集中提醒。
            if not previous_hash or current != previous_hash:
                if had_previous and expected != previous_hash:
                    preserved_conflicts.append(rel)
                print(f"[保留玩家修改] {rel}")
                preserved += 1
                continue
        if not exists and adopt_existing:
            picked = adopt_source(adopt_index(), expected, target, manifest_targets)
            if picked is not None:
                src, mode = picked
                target.parent.mkdir(parents=True, exist_ok=True)
                _ACTIVE_JOURNAL.record(rel, expected)
                if mode == "move":
                    # Same-hash adoption is usually a managed rename. Content is unchanged,
                    # but the old local path is still being mutated, so preserve it first.
                    try:
                        old_rel = src.relative_to(root).as_posix()
                    except ValueError as exc:
                        raise ValueError(f"adopt source escaped instance root: {src}") from exc
                    backup_file(src, root, old_rel, backup_root)
                    shutil.move(str(src), target)
                else:
                    shutil.copy2(str(src), target)
                if sha1(target) == expected:
                    ensure_executable_if_needed(target, rel)
                    verified[key] = file_verify_record(target, expected)
                    print(f"[接管本地文件] {rel}")
                    adopted += 1
                    adopted_paths.append(rel)
                    continue
                # adopted content didn't verify (rare); fall through to a normal download
        if exists:
            backup_file(target, root, rel, backup_root)
        download_jobs.append((item, target, expected, rel, key, force_sync))

    def run_download_job(job):
        item, target, expected, rel, key, force_sync = job
        try:
            if install_stage_cache(root, rel, target, expected):
                source = "cache"
            else:
                source = download_manifest_entry(item, base_url, target, expected, rel)
            ensure_executable_if_needed(target, rel)
            sync_print(f"[预下载缓存] {rel}" if source == "cache" else (f"[官方源] {rel}" if source == "official" else f"[更新] {rel}"))
            return key, rel, force_sync, file_verify_record(target, expected), None
        except Exception as exc:
            if is_optional_helper(rel):
                sync_print(f"[警告] 辅助脚本未更新：{rel}；{exc}")
                return key, rel, force_sync, None, "optional"
            if not isinstance(exc, UpdateError):
                exc = local_file_error(exc, rel) if isinstance(exc, OSError) else UpdateError(f"[文件更新失败] {rel}。请截图联系管理员（{type(exc).__name__}）。")
            return key, rel, force_sync, None, exc

    if download_jobs:
        workers = max(1, min(8, int(manifest.get("downloadWorkers", _DOWNLOAD_WORKERS)), len(download_jobs)))
        print(f"[同步] 需要更新 {len(download_jobs)} 个文件（并行 {workers}）。")
        if workers == 1:
            results = [run_download_job(job) for job in download_jobs]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(run_download_job, download_jobs))
        hard_error = None
        for key, rel, force_sync, record, err in results:
            if record is not None:
                verified[key] = record
                downloaded += 1
                updated_paths.append(rel)
                if force_sync:
                    forced_applied_paths.append(rel)
            elif err == "optional":
                preserved += 1
            elif hard_error is None:
                hard_error = err
        if hard_error is not None:
            raise hard_error

    for old_key, old_hash in previous.items():
        if old_key in new:
            continue
        old_rel = previous_rel.get(old_key, old_key)
        try:
            target = join_safe(root, old_rel)
        except ValueError:
            continue
        if not target.is_file():
            continue
        # tacz 等大件：清单撤下也不删。mods/** 虽也在删除保留里，
        # 但官方换版本时未改过的旧 jar 仍要清掉，避免双模组。
        keep_by_glob = preserve_player and glob_match(old_rel, preserve_deletions)
        is_pack_mod = old_rel.replace("\\", "/").lower().startswith("mods/")
        if keep_by_glob and not is_pack_mod:
            print(f"[保留本地额外文件] {old_rel}")
            preserved += 1
            continue
        if sha1(target) != old_hash:
            print(f"[保留本地额外文件] {old_rel}")
            preserved += 1
            continue
        backup_file(target, root, old_rel, backup_root)
        target.unlink()
        print(f"[删除旧文件] {old_rel}")
        removed += 1

    for path in planned_cleanup:
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        path = join_safe(root, rel)
        backup_file(path, root, rel, backup_root)
        path.unlink()
        print(f"[强制删除] {rel}")
        removed += 1

    # 本平台排除项：清除本地任何匹配文件（含清单外的孤儿，如 macOS 上 YSM 残留的 builtin 模型）。
    if platform_excludes:
        for path in cleanup_candidates(root, platform_excludes):
            rel = path.relative_to(root).as_posix()
            backup_file(path, root, rel, backup_root)
            path.unlink()
            print(f"[跳过本平台并清除] {rel}")
            removed += 1

    for dirname in additive_dirs:
        try:
            join_safe(root, dirname).mkdir(parents=True, exist_ok=True)
        except ValueError:
            pass

    apply_option_defaults(root, manifest, backup_root, preserved_deletions, initial_sync)
    required_pack_changes = ensure_required_resource_packs(
        root, manifest, backup_root, preserved_deletions
    )
    if required_pack_changes:
        print(
            "[重要] 已修改资源包顺序。请完全退出 Minecraft 后重新启动客户端；"
            "游戏运行中不会自动采用外部 options.txt 变更。"
        )
    apply_server_list(root, manifest, backup_root, preserved_deletions, initial_sync)
    apply_launcher_profile(root, manifest, backup_root)
    ensure_neoforge_client_extra(root, manifest, backup_root)
    show_launcher_hints(root)
    if cleanup.get("removeConnectorCache", True):
        remove_connector_cache(root)
    if cleanup.get("disableDuplicateMods", True):
        extras = []
        mods_dir = root / "mods"
        if mods_dir.is_dir():
            managed_mod_paths = set()
            for item in files:
                try:
                    mod_rel = safe_rel(str(item.get("path") or "")).lower()
                except ValueError:
                    continue
                if mod_rel.startswith("mods/") and mod_rel.endswith(".jar"):
                    managed_mod_paths.add(mod_rel)
            extras = [
                jar for jar in mods_dir.glob("*.jar")
                if jar.relative_to(root).as_posix().lower() not in managed_mod_paths
            ]
        if extras or downloaded or adopted or removed or initial_sync:
            disable_duplicate_mods(root, manifest)
    if cleanup.get("disableLauncherRepairIndex", True):
        disable_launcher_repair_index(pathlib.Path.cwd(), root)

    keep_keys = set(new.keys())
    verified = {key: value for key, value in verified.items() if key in keep_keys}
    state_out = {
        "format": 3,
        "packId": manifest.get("packId") or manifest.get("pack") or "",
        "packName": manifest.get("packName") or manifest.get("name") or "",
        "version": manifest.get("version") or "",
        "manifestUrl": manifest_url,
        "lastManifestUrl": manifest_url,
        "skipOfficial": False,
        "syncedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "files": files,
        "verified": verified,
    }
    save_state(root, state_out)
    _ACTIVE_JOURNAL.finish()
    config_applied = [
        rel for rel in updated_paths + adopted_paths
        if rel.lower().startswith(("config/", "defaultconfigs/"))
    ]
    forced_keys = {rel.lower() for rel in forced_applied_paths}
    print(f"[同步] 完成。更新={downloaded} 配置变更={len(config_applied)} 接管={adopted} 跳过={skipped} 保留={preserved} 删除={removed} 备份={backup_root}")
    if config_applied:
        print(f"[配置已应用] 共 {len(config_applied)} 项：")
        for rel in config_applied:
            suffix = "（强制同步；原文件如存在已备份）" if rel.lower() in forced_keys else ""
            print(f"  - {rel}{suffix}")
    release_notes = [str(note).strip() for note in (manifest.get("releaseNotes") or []) if str(note).strip()]
    if release_notes and (version_changed or downloaded or adopted or removed or initial_sync):
        print(f"[本版说明] {current_version or '当前版本'}")
        for note in release_notes:
            print(f"  - {note}")
    if preserved_conflicts:
        print(f"[提示] 以下 {len(preserved_conflicts)} 个文件保留了你的本地修改，但服务端本次也更新了它们（未应用服务端版）：")
        for rel in preserved_conflicts:
            print(f"  - {rel}")
        print("[提示] 如相关模组出现异常，删除对应文件后重新同步，即可取回服务端最新版本。")
    return 0



_ACTIVE_JOURNAL = None


def atomic_json(path, value):
    path = join_safe(path.parent, path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".portable-sync-write-", suffix=".tmp", dir=path.parent)
    temp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def read_state_file(path):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise ValueError("invalid state")
    for item in value["files"]:
        safe_rel(str(item["path"]))
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(item.get("sha1", ""))):
            raise ValueError("invalid state hash")
    return value


def read_state(root):
    path = join_safe(root, ".portable-sync-state.json")
    previous = join_safe(root, ".portable-sync-state.prev.json")
    if not path.exists() and not previous.exists():
        return {}
    for candidate in (path, previous):
        try:
            value = read_state_file(candidate)
            if candidate == previous:
                sync_print("[恢复] 上次同步状态无法读取，已采用上一份有效状态，继续保留玩家修改和删除。")
            return value
        except (OSError, ValueError, KeyError, TypeError):
            continue
    raise UpdateError("[状态损坏] 同步状态及其备份均无法读取。已停止更新，请保留此目录并联系管理员，避免误恢复你删除的文件。")


def save_state(root, value):
    path = join_safe(root, ".portable-sync-state.json")
    previous = join_safe(root, ".portable-sync-state.prev.json")
    if path.is_file():
        try:
            old = read_state_file(path)
        except (OSError, ValueError, KeyError, TypeError):
            old = None
        if old is not None:
            atomic_json(previous, old)
    elif not path.exists() and not previous.exists():
        atomic_json(previous, value)  # First successful sync also has a recovery point.
    atomic_json(path, value)


@contextlib.contextmanager
def instance_lock(root):
    path = join_safe(root, ".portable-sync.lock")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        create = api.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = create(str(path), 0xC0000000, 0, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            code = ctypes.get_last_error()
            if code in (32, 33):
                raise UpdateError("[更新器已运行] 此实例已有更新或预下载任务正在处理，请等待它完成后再试。")
            raise local_file_error(ctypes.WinError(code), path, "取得更新锁")
        try:
            yield
        finally:
            api.CloseHandle(handle)
    else:
        import fcntl
        with path.open("a+b") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpdateError("[更新器已运行] 此实例已有更新或预下载任务正在处理，请等待它完成后再试。") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class LogTee:
    def __init__(self, stream, log, mutex):
        self.stream, self.log, self.mutex = stream, log, mutex
    def write(self, text):
        # Logs retain endpoint hosts only, never access-token paths or queries.
        def hide(match):
            try:
                parsed = urllib.parse.urlsplit(match.group(0))
                return parsed.scheme + "://" + (parsed.hostname or "更新源") + "/***"
            except ValueError:
                return "[更新地址已隐藏]"
        clean = re.sub(r"https?://[^\s；，。]+", hide, str(text))
        with self.mutex:
            self.stream.write(text)
            self.log.write(clean)
            self.log.flush()
        return len(text)
    def flush(self):
        self.stream.flush()
        self.log.flush()
    def __getattr__(self, name):
        return getattr(self.stream, name)


@contextlib.contextmanager
def sync_log(root):
    path = join_safe(root, ".portable-sync-logs/latest.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as log:
        lock = threading.Lock()
        with contextlib.redirect_stdout(LogTee(sys.stdout, log, lock)), contextlib.redirect_stderr(LogTee(sys.stderr, log, lock)):
            yield


class RecoveryJournal:
    def __init__(self, root, manifest):
        self.root = root
        self.path = join_safe(root, ".portable-sync-pending.json")
        self.lock = threading.Lock()
        self.data = {"format": 1, "packId": str(manifest.get("packId") or manifest.get("pack") or ""),
                     "version": str(manifest.get("version") or ""), "applied": {}}
        if self.path.is_file():
            try:
                old = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if str(old.get("packId") or "") == self.data["packId"]:
                    for record in (old.get("applied") or {}).values():
                        rel = safe_rel(str(record["path"]))
                        digest = str(record["sha1"]).lower()
                        if re.fullmatch(r"[0-9a-f]{40}", digest) and sha1(join_safe(root, rel)) == digest:
                            self.data["applied"][rel.lower()] = {"path": rel, "sha1": digest}
            except (ValueError, KeyError, TypeError, AttributeError):
                raise UpdateError("[恢复记录损坏] 未完成更新的记录无法读取，已停止更新，请联系管理员。") from None
    def previous_files(self, files):
        rows = {rel_key(str(row["path"])): dict(row) for row in files}
        rows.update(self.data["applied"])
        if self.data["applied"]:
            sync_print(f"[恢复] 已识别上次未完成更新写入的 {len(self.data['applied'])} 个文件，将继续按最新清单处理。")
        return list(rows.values())
    def record(self, rel, digest):
        rel = safe_rel(rel)
        with self.lock:
            self.data["applied"][rel.lower()] = {"path": rel, "sha1": str(digest).lower()}
            atomic_json(self.path, self.data)
    def finish(self):
        if self.path.exists():
            self.path.unlink()


def validate_manifest(root, manifest):
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise UpdateError("[清单无效] 更新清单没有有效的文件列表，请联系管理员。")
    seen = set()
    for row in rows:
        try:
            rel = safe_rel(str(row["path"]))
            join_safe(root, rel)
            digest = str(row["sha1"])
            if not re.fullmatch(r"[0-9a-fA-F]{40}", digest):
                raise ValueError("hash")
            if "size" in row and int(row["size"]) < 0:
                raise ValueError("size")
        except (KeyError, TypeError, ValueError):
            raise UpdateError("[清单无效] 文件路径、大小或校验值无效，请联系管理员。") from None
        key = rel.lower()
        if key in seen:
            raise UpdateError(f"[清单无效] 文件路径重复：{rel}。已停止更新，请联系管理员。")
        seen.add(key)
        if glob_match(rel, manifest.get("forceDeleteGlobs") or []):
            raise UpdateError(f"[清单无效] 同一文件同时要求安装和删除：{rel}。请联系管理员。")
    for rel in (manifest.get("playerOptions") or {}).get("targets") or ["options.txt"]:
        join_safe(root, str(rel))


def part_path(destination, expected):
    suffix = hashlib.sha256(destination.name.encode("utf-8")).hexdigest()[:16]
    return join_safe(destination.parent, f".portable-part-{expected.lower()}-{suffix}.tmp")


def remove_obsolete_parts(destination, expected):
    current = part_path(destination, expected)
    suffix = hashlib.sha256(destination.name.encode("utf-8")).hexdigest()[:16]
    pattern = re.compile(r"\.portable-part-[0-9a-f]{40}-" + suffix + r"\.tmp")
    if destination.parent.is_dir():
        for path in destination.parent.glob(f".portable-part-*-{suffix}.tmp"):
            if path != current and pattern.fullmatch(path.name):
                join_safe(destination.parent, path.name).unlink(missing_ok=True)


def install_stage_cache(root, rel, destination, expected):
    cached = join_safe(root, ".portable-staging/" + rel)
    if not cached.is_file() or sha1(cached) != expected:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    remove_obsolete_parts(destination, expected)
    temporary = part_path(destination, expected)
    shutil.copyfile(cached, temporary)
    if sha1(temporary) != expected:
        temporary.unlink()
        return False
    install_verified_file(temporary, destination, expected)
    return True


def cleanup_candidates(root, patterns):
    """Exact paths are direct lookups; wildcard rules share one pruned walk."""
    found = {}
    wildcards = []
    for pattern in patterns or []:
        pattern = safe_rel(str(pattern))
        if is_protected_rel(pattern):
            continue
        if "*" in pattern or "?" in pattern:
            wildcards.append(pattern)
        else:
            path = join_safe(root, pattern)
            if path.is_file():
                found[str(path).lower()] = path
    if wildcards:
        for directory, dirs, names in os.walk(root, followlinks=False):
            base = pathlib.Path(directory)
            dirs[:] = [name for name in dirs if not name.startswith((".portable-", "_disabled_"))
                       and not (base / name).is_symlink()
                       and not getattr(base / name, "is_junction", lambda: False)()]
            for name in names:
                path = base / name
                rel = path.relative_to(root).as_posix()
                if not is_protected_rel(rel) and glob_match(rel, wildcards):
                    path = join_safe(root, rel)
                    if path.is_file():
                        found[str(path).lower()] = path
    return sorted(found.values())


def main() -> int:
    global _ACTIVE_JOURNAL
    parser = argparse.ArgumentParser(description="便携 Minecraft 玩家更新器")
    parser.add_argument("--instance-dir", default=".")
    parser.add_argument("--manifest-url", default="")
    args = parser.parse_args()
    root = pathlib.Path(args.instance_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with instance_lock(root):
        with sync_log(root):
            try:
                return sync_instance(root, args)
            except SystemExit as exc:
                if exc.code:
                    print(str(exc), file=sys.stderr)
                raise
            except Exception as exc:
                if isinstance(exc, UpdateError):
                    message = str(exc)
                elif isinstance(exc, OSError):
                    message = str(local_file_error(exc, getattr(exc, "filename", None) or "客户端目录"))
                else:
                    message = f"[更新失败] 更新未完成，请联系管理员（{type(exc).__name__}）。"
                print(message, file=sys.stderr)
                print("[日志] 错误已保存到 .portable-sync-logs/latest.log。", file=sys.stderr)
                raise UpdateError(message) from None
            finally:
                _ACTIVE_JOURNAL = None


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[同步已取消] 更新尚未完成，请重新运行更新器后再启动游戏。", file=sys.stderr)
        raise SystemExit(130)
    except UpdateError as exc:
        print(str(exc), file=sys.stderr)
        print("[同步失败] 更新尚未完成，请处理上述问题后重新运行更新器。", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        if isinstance(exc, OSError):
            message = str(local_file_error(exc, getattr(exc, "filename", None) or "客户端目录"))
        else:
            message = f"[更新失败] 更新未完成，请保留截图联系管理员（{type(exc).__name__}）。"
        print(message, file=sys.stderr)
        raise SystemExit(1)
