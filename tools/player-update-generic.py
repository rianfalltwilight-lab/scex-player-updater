#!/usr/bin/env python3
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fnmatch
import hashlib
import http.client
import ipaddress
import json
import os
import pathlib
import random
import re
import shutil
import socket
import stat
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
# OFFICIAL_BREAKER_V1: per-run, per-host circuit breaker for official sources.
# A host that fails for this many consecutive files is skipped for the rest of
# the run; one success resets its count. File-specific failures (hash mismatch,
# HTTP 4xx other than 408/429) neither count nor reset.
_OFFICIAL_FAILURE_LIMIT = 3
_OFFICIAL_LOCK = threading.Lock()
_OFFICIAL_FAILURES = {}
_OFFICIAL_TRIPPED = set()
# Directories already proven link-free during this run (see join_safe).
_LINK_SAFE_DIRS = set()
_CANONICAL_BASES = {}


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


# MANIFEST_CACHE_BEGIN: identical in player-update-generic.py, portable-stage-daemon.py and
# the macOS entry heredoc; tests/test_manifest_cache.py compares the copies.
# MANIFEST_CACHE_V1: the last manifest is kept with its ETag, so an unchanged manifest costs
# one small 304 response; the server may send it gzip-compressed.
import gzip, http.client, io, socket, stat as _cache_stat, urllib.error  # noqa: E401 (the heredoc lacks these)

UPDATER_VERSION = "2.27.28"
MANIFEST_CACHE_DIR = ".portable-manifest-cache"


def _manifest_get(url, headers, timeout):
    """One GET without any proxy (IPv4 first for plain HTTP). Returns (status, headers, body)."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "http" and parsed.hostname:
        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        port = parsed.port or 80
        last = None
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                address = socket.getaddrinfo(parsed.hostname, port, family, socket.SOCK_STREAM)[0][4][0]
            except (OSError, IndexError) as exc:
                last = last or exc  # a refused IPv4 connection says more than a missing IPv6 address
                continue
            connection = http.client.HTTPConnection(address, port, timeout=timeout)
            try:
                connection.request("GET", path, headers=dict(headers, Host=parsed.netloc.rsplit("@", 1)[-1]))
                response = connection.getresponse()
                return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
            except (OSError, http.client.HTTPException) as exc:
                last = exc
            finally:
                connection.close()
        raise last if isinstance(last, OSError) else OSError(str(last or "无法连接更新源"))
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, b""


def _manifest_cache_folder(root):
    folder = pathlib.Path(root) / MANIFEST_CACHE_DIR
    try:
        info = os.lstat(folder)
    except OSError:
        return folder
    if _cache_stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return None  # never follow a link planted in place of the cache folder
    return folder


def _manifest_cache_read(root):
    folder = _manifest_cache_folder(root)
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        manifest = (folder / "server-manifest.json").read_bytes()
        if hashlib.sha256(manifest).hexdigest() != meta["manifestSha256"]:
            return None
        return manifest, str(meta.get("manifestEtag") or "")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _manifest_cache_write(root, manifest, etag):
    folder = _manifest_cache_folder(root)
    if folder is None or not etag:
        return
    meta = {"manifestEtag": etag, "manifestSha256": hashlib.sha256(manifest).hexdigest()}
    try:
        folder.mkdir(exist_ok=True)
        for name, data in (("server-manifest.json", manifest), ("meta.json", json.dumps(meta).encode("utf-8"))):
            temporary = folder / f"{name}.{os.getpid()}.tmp"
            temporary.write_bytes(data)
            os.replace(temporary, folder / name)
    except OSError:
        pass  # the cache only saves bandwidth


def fetch_manifest(url, root=None, timeout=20, user_agent="SCEX-updater", use_cache=True):
    """Return the manifest bytes; OSError/ValueError mean the source is unreachable."""
    cached = _manifest_cache_read(root) if (root and use_cache) else None
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip", "Cache-Control": "no-cache"}
    if cached and cached[1]:
        headers["If-None-Match"] = cached[1]
    status, response_headers, body = _manifest_get(url, headers, timeout)
    if status == 304 and cached:
        return cached[0]
    if status != 200:
        raise urllib.error.HTTPError(url, status, f"HTTP {status}", None, io.BytesIO(body or b""))
    if "gzip" in str(response_headers.get("content-encoding", "")).lower():
        body = gzip.decompress(body)
    if root and use_cache:
        _manifest_cache_write(root, body, str(response_headers.get("etag", "")))
    return body
# MANIFEST_CACHE_END
_SYNC_UA = f"SCEX-updater/{UPDATER_VERSION} (python {sys.version_info[0]}.{sys.version_info[1]}; {sys.platform})"


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


def _is_link_or_reparse(path: pathlib.Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


_NAME_SURROGATE_TAGS = (0xA000000C, 0xA0000003)  # IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT


def _is_name_surrogate(path: pathlib.Path) -> bool:
    """A symlink or junction; cloud placeholders and other data reparse points are not."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) in _NAME_SURROGATE_TAGS


def _canonical_base(base: pathlib.Path):
    """SHORT_PATH_V1: the resolved form of base, or None if a link is on the way.
    A resolved path can also differ because of 8.3 short names (C:\\Users\\RUNNER~1)
    or letter case; those are the same directory, not a link."""
    resolved = base.resolve()
    if os.path.normcase(str(resolved)) == os.path.normcase(str(base)):
        return base
    probe = base
    while True:
        if _is_name_surrogate(probe):
            return None
        if probe.parent == probe:
            return resolved
        probe = probe.parent


def join_safe(root: pathlib.Path, rel: str) -> pathlib.Path:
    base = root.absolute()
    # Do not follow a junction/symlink in any parent, even when the final file
    # itself has ordinary attributes. Root has already been resolved by main.
    # PATH_CACHE_V1: a manifest has thousands of files but few directories, so
    # each existing directory is resolved once per run instead of once per file.
    base_key = os.path.normcase(str(base))
    canonical = _CANONICAL_BASES.get(base_key)
    if canonical is None:
        canonical = _canonical_base(base)
        if canonical is None:
            raise LocalFileUpdateError(f"[路径错误] 更新目录经过链接，已停止写入：{root}")
        if base.is_dir():
            _CANONICAL_BASES[base_key] = canonical
            _LINK_SAFE_DIRS.add(os.path.normcase(str(canonical)))
    base = canonical
    target = base.joinpath(*safe_rel(rel).split("/"))
    parent = target.parent
    parent_key = os.path.normcase(str(parent))
    if parent_key not in _LINK_SAFE_DIRS:
        resolved_parent = parent.resolve()
        if not resolved_parent.is_relative_to(base) or resolved_parent != parent:
            raise LocalFileUpdateError(f"[路径错误] 更新目标经过链接或越出目录，已停止写入：{rel}")
        if parent.is_dir():
            _LINK_SAFE_DIRS.add(parent_key)
    # Plain files need no resolve; links and other reparse points (including
    # cloud placeholders) keep the original full-resolution check.
    if _is_link_or_reparse(target):
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
    """测试和每次 main() 开头清空缓存，避免上次运行的熔断/代理/路径结果串台。"""
    global _PROXY_URL, _PROXY_OPENER
    _PROXY_URL = None
    _PROXY_OPENER = None
    with _OFFICIAL_LOCK:
        _OFFICIAL_FAILURES.clear()
        _OFFICIAL_TRIPPED.clear()
    _LINK_SAFE_DIRS.clear()
    _CANONICAL_BASES.clear()


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
    # URL_RULES_V1: same classification in every updater entry point
    # (tests/fixtures/url-order-cases.json): loopback, RFC 1918, link-local,
    # IPv6 ULA/link-local, localhost and mDNS .local names.
    host = (host or "").strip().strip("[]").lower().rstrip(".")
    if host in ("", "localhost") or host.endswith(".local"):
        return True
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host) and ":" not in host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    b = ip.packed
    if ip.version == 4:
        return b[0] in (10, 127) or (b[0] == 172 and 16 <= b[1] <= 31) or (b[0] == 192 and b[1] == 168) or (b[0] == 169 and b[1] == 254)
    return ip == ipaddress.IPv6Address("::1") or (b[0] & 0xFE) == 0xFC or (b[0] == 0xFE and (b[1] & 0xC0) == 0x80)


def is_private_update_url(url: str) -> bool:
    return is_local_or_private_host(urllib.parse.urlparse(str(url) or "").hostname or "")


def probe_timeout_for_url(url: str) -> float:
    host = urllib.parse.urlparse(str(url) or "").hostname or ""
    return _PRIVATE_PROBE_TIMEOUT_SEC if is_local_or_private_host(host) else _PUBLIC_PROBE_TIMEOUT_SEC


def is_usable_update_url(url):
    """Only absolute http(s) URLs with a host take part in ordering."""
    try:
        parsed = urllib.parse.urlsplit(str(url).strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def order_manifest_urls(urls, last_good: str = "") -> list:
    """URL_RULES_V1: use only the first public URL, then the
    LAN URLs listed in the URL files, in file order. Old public addresses left in
    legacy files are never probed. Never invents a LAN address. ``last_good`` is
    accepted for compatibility; with one public candidate it cannot reorder."""
    seen = set()
    out = []
    usable = [str(url or "").strip() for url in (urls or [])]
    usable = [url for url in usable if is_usable_update_url(url)]
    public = [url for url in usable if not is_private_update_url(url)]
    # LAN_FALLBACK_V1: LAN URLs listed in UPDATE-URL go last, for routers without NAT hairpin.
    for url in public[:1] + [url for url in usable if is_private_update_url(url)]:
        if url.lower() not in seen:
            seen.add(url.lower())
            out.append(url)
    return out


def file_verify_record(path: pathlib.Path, digest: str) -> dict:
    st = path.stat()
    return {"sha1": str(digest).lower(), "size": int(st.st_size), "mtime": int(st.st_mtime)}


def verified_record_matches(verified, size: int, mtime: float) -> bool:
    """Size + whole-second mtime fast path. Python truncates mtime; a rounded
    value is accepted too. (PowerShell records before 2.27.22 were also shifted
    by the local time zone; those miss this fast path and are hashed once.)"""
    try:
        v_size = int(verified.get("size", -1))
        v_mtime = int(verified.get("mtime", -1))
    except (TypeError, ValueError, AttributeError):
        return False
    return v_size == size and v_mtime in (int(mtime), int(round(mtime)))


def current_file_state(path: pathlib.Path, expected: str, manifest_size, previous_hash: str = "", verified=None):
    """Return (is_current, sha1). sha1 is the expected hash when the last sync's
    size/mtime record proved the file, the computed hash when it had to be read,
    and None when the size already rules the file out. Callers reuse it instead
    of hashing the same file twice."""
    if not path.is_file() or not expected:
        return False, None
    st = path.stat()
    if manifest_size not in (None, "", 0, "0"):
        try:
            if st.st_size != int(manifest_size):
                return False, None
        except (TypeError, ValueError):
            pass
    expected = str(expected).lower()
    verified = verified if isinstance(verified, dict) else {}
    if str(verified.get("sha1") or "").lower() == expected and verified_record_matches(verified, st.st_size, st.st_mtime):
        return True, expected
    digest = sha1(path)
    return digest == expected, digest


def looks_like_current_file(path: pathlib.Path, expected: str, manifest_size, previous_hash: str, verified=None) -> bool:
    """Skip SHA1 when last completed sync already proved this exact file."""
    return current_file_state(path, expected, manifest_size, previous_hash, verified)[0]


def entry_size(item) -> int:
    try:
        return max(0, int((item or {}).get("size") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


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


def close_http_error(exc):
    """Release an HTTPError's response. Python 3.9 raises KeyError for an HTTPError
    built without a response body, so closing must never mask the real error."""
    try:
        exc.close()
    except Exception:
        pass


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
        raise file_specific_error("[清单无效] 文件校验值无效，已停止下载。")
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
                            raise file_specific_error("[下载失败] 更新源返回的续传范围无效，请稍后重试。")
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
                close_http_error(exc)
                tmp.unlink(missing_ok=True)
                range_restarted = True
                continue
            delay = busy_retry_delay(exc, busy_attempt) if backoff else None
            if isinstance(exc, urllib.error.HTTPError):
                close_http_error(exc)
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
            error = UpdateError(f"[下载失败] {dest.name}：{network_error_reason(exc)}。已保留下载进度，请检查网络后重试。")
            error.http_status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            raise error from None
    if sha1(tmp) != expected_sha1:
        tmp.unlink(missing_ok=True)
        raise file_specific_error(f"[校验失败] {dest.name}：下载内容与清单不一致，旧文件未替换。请重新更新；反复失败时联系管理员。")
    install_verified_file(tmp, dest, expected_sha1)


def file_specific_error(message: str) -> UpdateError:
    """A failure tied to one file or manifest row, not to the source host."""
    error = UpdateError(message)
    error.file_specific = True
    return error


def counts_against_source(exc: BaseException) -> bool:
    """True when a failure says the host is unreachable, slow or overloaded."""
    if isinstance(exc, LocalFileUpdateError) or getattr(exc, "file_specific", False):
        return False
    code = getattr(exc, "http_status", None)
    if code is None and isinstance(exc, urllib.error.HTTPError):
        code = exc.code
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return not (code is not None and 400 <= code < 500 and code not in (408, 429))


def official_host(url: str) -> str:
    return (urllib.parse.urlsplit(str(url)).hostname or "").lower()


def official_host_tripped(host: str) -> bool:
    with _OFFICIAL_LOCK:
        return host in _OFFICIAL_TRIPPED


def record_official_result(host: str, ok: bool, counted: bool = True) -> None:
    tripped = False
    with _OFFICIAL_LOCK:
        if ok:
            _OFFICIAL_FAILURES[host] = 0
            return
        if not counted:
            return
        count = _OFFICIAL_FAILURES.get(host, 0) + 1
        _OFFICIAL_FAILURES[host] = count
        if count >= _OFFICIAL_FAILURE_LIMIT and host not in _OFFICIAL_TRIPPED:
            _OFFICIAL_TRIPPED.add(host)
            tripped = True
    if tripped:
        sync_print(f"[同步] 官方源 {host} 连续 {_OFFICIAL_FAILURE_LIMIT} 个文件下载失败，本轮不再尝试，其余文件直接走更新服务。")


def download_manifest_entry(item, base_url: str, dest: pathlib.Path, expected_sha1: str, rel: str) -> str:
    # Prefer official exact-hash URLs, then the pack server.
    for url in official_urls(item if isinstance(item, dict) else {}):
        host = official_host(url)
        if official_host_tripped(host):
            continue
        openers = [_DIRECT_OPENER]
        proxy = opener_for_official_url(url)
        if proxy is not _DIRECT_OPENER:
            openers.append(proxy)
        counted = True
        for opener in openers:
            try:
                download_file(url, dest, expected_sha1, opener=opener,
                              timeout=_OFFICIAL_TIMEOUT_SEC, retries=0,
                              min_bytes_after_timeout=_OFFICIAL_MIN_BYTES,
                              min_rate_bps=_OFFICIAL_MIN_RATE, backoff=False)
                record_official_result(host, True)
                return "official"
            except LocalFileUpdateError:
                raise
            except Exception as exc:
                counted = counted and counts_against_source(exc)
                sync_print(f"[同步] {host} 不可用（{download_fail_reason(exc)}），继续尝试下一下载源：{rel}")
        record_official_result(host, False, counted)
    size = 0
    if isinstance(item, dict):
        size = item.get("size") or 0
    download_file(
        url_for(base_url, rel), dest, expected_sha1,
        opener=_DIRECT_OPENER, timeout=home_timeout_sec(size),
    )
    return "home"


def existing_hash_index(root: pathlib.Path, sizes=None) -> dict:
    # One SHA1 can map to several local files (TACZ gun-pack sounds are duplicated
    # across assets/ccrp and assets/classicr), so keep a list per hash instead of
    # only the first. Keeping only one made later entries with the same hash point
    # at a path an earlier adopt had already moved away -> "does not exist" crash
    # (2026-07-08 player-reported).
    # ADOPT_SIZE_FILTER_V1: a file can only match a missing entry of equal size,
    # so when the missing sizes are known only same-size files are hashed.
    out = {}
    for dirname in ("mods", "config", "defaultconfigs", "kubejs", "scripts", "resourcepacks", "shaderpacks", "data"):
        base = root / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if sizes is not None:
                try:
                    if path.stat().st_size not in sizes:
                        continue
                except OSError:
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
        if not manifest_urls:
            raise SystemExit("[清单获取失败] UPDATE-URL.txt 里没有可用的 http/https 更新地址，请重新获取玩家包。")
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
                raw = fetch_manifest(candidate, root, timeout=timeout, user_agent=_SYNC_UA)
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
    # MAC_REPAIR_V1: --verify-all ignores the size+mtime records and hashes every managed file,
    # so damaged files are found and downloaded again (personal changes still follow the rules).
    raw_verified = {} if getattr(args, "verify_all", False) else (state.get("verified") or {})
    if getattr(args, "verify_all", False):
        print("[检查] 完整校验：逐个计算所有受管文件的哈希，损坏或缺失的会重新下载。")
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
    while True:
        try:
            preflight_managed_files(root, list(files) + previous_files + extra_targets)
            break
        except LocalFileUpdateError as exc:
            # LOCK_WAIT_V1: a player who forgot to close the game can retry in place.
            if not str(exc).startswith("[文件占用]") or not interactive_session():
                raise
            print(str(exc))
            print("[等待] 完全退出 Minecraft（以及占用该文件的程序）后，按回车重新检查；直接关闭本窗口即可取消。")
            try:
                input()
            except EOFError:
                raise exc from None

    if cleanup.get("removeConnectorCache", True):
        remove_connector_cache(root)
    if cleanup.get("disableLauncherRepairIndex", True):
        disable_launcher_repair_index(pathlib.Path.cwd(), root)

    # 接管索引惰性构建：日常同步通常没有缺失文件，这时不必对 mods/config/resourcepacks 等
    # 目录做全量 SHA1 扫描（大包一次要几十秒）。只在真的遇到缺失文件需要接管时才建一次。
    existing = None

    def missing_entry_sizes():
        """Sizes of entries absent locally, or None if any of them has no size."""
        sizes = set()
        for row in files:
            try:
                row_rel = safe_rel(str(row["path"]))
                if platform_excludes and glob_match(row_rel, platform_excludes):
                    continue
                if join_safe(root, row_rel).is_file():
                    continue
                sizes.add(int(row["size"]))
            except (KeyError, TypeError, ValueError):
                return None
        return sizes

    def adopt_index():
        nonlocal existing
        if existing is None:
            print("[同步] 有文件缺失，正在建立本地文件哈希索引（仅本次需要）...")
            existing = existing_hash_index(root, missing_entry_sizes())
        return existing
    # All manifest target paths: lets adopt tell a spare copy (movable) from a twin
    # the manifest still needs elsewhere (copy instead of steal). join_safe has
    # already rejected links, so the joined path is the resolved path.
    manifest_targets = set()
    for item in files:
        try:
            manifest_targets.add(str(join_safe(root, str(item["path"]))).lower())
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
        is_current, current = current_file_state(target, expected, item.get("size"), previous_hash, verified.get(key)) if exists else (False, "")
        if is_current:
            ensure_executable_if_needed(target, rel)
            verified[key] = file_verify_record(target, expected)
            skipped += 1
            continue
        if exists and current is None:
            current = sha1(target)  # size differs from the manifest; hash once for the three-way check
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
            detail_print(f"[预下载缓存] {rel}" if source == "cache" else (f"[官方源] {rel}" if source == "official" else f"[更新] {rel}"))
            progress.file_done(entry_size(item))
            return key, rel, force_sync, file_verify_record(target, expected), None
        except Exception as exc:
            if is_optional_helper(rel):
                sync_print(f"[警告] 辅助脚本未更新：{rel}；{exc}")
                return key, rel, force_sync, None, "optional"
            if not isinstance(exc, UpdateError):
                exc = local_file_error(exc, rel) if isinstance(exc, OSError) else UpdateError(f"[文件更新失败] {rel}。请截图联系管理员（{type(exc).__name__}）。")
            return key, rel, force_sync, None, exc

    if download_jobs:
        # JOURNAL_BATCH_V1: record every intended file once before downloading.
        # Recovery keeps only entries whose on-disk hash matches, so intents that
        # never completed are ignored; installs then add nothing new to write.
        try:
            _ACTIVE_JOURNAL.record_many((job[3], job[2]) for job in download_jobs)
        except OSError as exc:
            raise local_file_error(exc, _ACTIVE_JOURNAL.path, "保存恢复记录") from None
        workers = max(1, min(8, int(manifest.get("downloadWorkers", _DOWNLOAD_WORKERS)), len(download_jobs)))
        progress = DownloadProgress([entry_size(job[0]) for job in download_jobs])
        print(f"[同步] 需要更新 {len(download_jobs)} 个文件，共 {megabytes(progress.total_bytes)} MB（并行 {workers}）。"
              "逐个文件的明细写在 .portable-sync-logs/latest.log。")
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
    tidy_after_sync(root, backup_root)
    remove_root_helper_copies(root, files)
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


def mask_urls(text) -> str:
    """Logs and diagnostics keep endpoint hosts only, never access-token paths or queries."""
    def hide(match):
        try:
            parsed = urllib.parse.urlsplit(match.group(0))
            return parsed.scheme + "://" + (parsed.hostname or "更新源") + "/***"
        except ValueError:
            return "[更新地址已隐藏]"
    return re.sub(r"https?://[^\s；，。\"']+", hide, str(text))


# Set by sync_log: writes a line to the log file only (per-file download details).
_LOG_ONLY = None


def detail_print(message: str) -> None:
    """PROGRESS_V1: per-file details go to the log; the console shows progress lines."""
    sink = _LOG_ONLY
    if sink is None:
        sync_print(message)
    else:
        sink(message + "\n")


class LogTee:
    def __init__(self, stream, log, mutex):
        self.stream, self.log, self.mutex = stream, log, mutex
    def write(self, text):
        clean = mask_urls(text)
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
    global _LOG_ONLY
    path = join_safe(root, ".portable-sync-logs/latest.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = join_safe(root, ".portable-sync-logs/previous.log")
    if path.is_file():
        # Keep the last run's log, so a failed run's log survives one re-run.
        shutil.copyfile(path, previous)
    with path.open("w", encoding="utf-8", newline="\n") as log:
        lock = threading.Lock()

        def log_only(text):
            with lock:
                log.write(mask_urls(text))
                log.flush()
        _LOG_ONLY = log_only
        try:
            with contextlib.redirect_stdout(LogTee(sys.stdout, log, lock)), contextlib.redirect_stderr(LogTee(sys.stderr, log, lock)):
                yield
        finally:
            _LOG_ONLY = None


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
        self.record_many([(rel, digest)])
    def record_many(self, rows):
        """Add entries and write the journal once; already-recorded entries are free."""
        with self.lock:
            changed = False
            for rel, digest in rows:
                rel = safe_rel(rel)
                entry = {"path": rel, "sha1": str(digest).lower()}
                if self.data["applied"].get(rel.lower()) != entry:
                    self.data["applied"][rel.lower()] = entry
                    changed = True
            if changed or not self.path.is_file():
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


_BACKUP_DIR_NAME = re.compile(r"\d{8}-\d{6}(?:-[0-9A-Za-z]+)*")
_STAGE_KEEP = {"daemon.lock", "daemon.log"}
READY_NOTICE_NAME = "更新已就绪.txt"


def _clear_tree(directory: pathlib.Path, keep_names=()) -> int:
    """Delete regular files and empty folders below directory; never follow or
    delete links or other reparse points. Returns the number of files removed."""
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        path = pathlib.Path(entry.path)
        if entry.name in keep_names or _is_link_or_reparse(path):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                removed += _clear_tree(path)
                path.rmdir()
                continue
            try:
                path.unlink()
            except PermissionError:
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                path.unlink()
            removed += 1
        except OSError:
            continue  # A locked leftover is harmless; the next sync tries again.
    return removed


def prune_old_backups(root: pathlib.Path, keep: pathlib.Path) -> int:
    """BACKUP_RETENTION_V1: keep only the newest backup. A run that backed up
    nothing keeps the previous one, so a rollback point always survives."""
    if not keep.is_dir():
        return 0
    backups = join_safe(root, ".portable-sync-backups")
    pruned = 0
    for entry in sorted(backups.iterdir()):
        if entry.name == keep.name or not _BACKUP_DIR_NAME.fullmatch(entry.name):
            continue
        if _is_link_or_reparse(entry) or not entry.is_dir():
            continue
        _clear_tree(entry)
        try:
            entry.rmdir()
            pruned += 1
        except OSError:
            pass
    return pruned


def clear_stage_cache(root: pathlib.Path) -> int:
    """STAGE_CLEANUP_V1: after a successful sync every staged file is either
    installed or superseded; keep only the watcher's lock and log."""
    stage = join_safe(root, ".portable-staging")
    if not stage.is_dir():
        return 0
    removed = _clear_tree(stage, _STAGE_KEEP)
    notice = join_safe(root, READY_NOTICE_NAME)
    if notice.is_file():
        try:
            notice.unlink()
        except OSError:
            pass
    return removed


DIAGNOSTIC_NAME = "更新诊断包.zip"
# DIAGNOSTIC_V1: short codes the player can quote; the first matching tag wins.
_ERROR_CODES = (("[文件占用]", "E-LOCK"), ("[权限不足]", "E-PERM"), ("[空间不足]", "E-DISK"),
                ("[替换失败]", "E-REPLACE"), ("[路径错误]", "E-PATH"), ("[校验失败]", "E-HASH"),
                ("[下载失败]", "E-NET"), ("[清单获取失败]", "E-FETCH"), ("[清单无效]", "E-MANIFEST"),
                ("[状态损坏]", "E-STATE"), ("[恢复记录损坏]", "E-STATE"), ("[更新器已运行]", "E-BUSY"),
                ("[预检失败]", "E-PRECHECK"))


def error_code(message: str) -> str:
    text = str(message)
    for tag, code in _ERROR_CODES:
        if tag in text:
            return code
    return "E-OTHER"


def write_diagnostic_package(root: pathlib.Path, code: str, message: str):
    """Zip the masked logs and a short summary into the instance root for the
    player to send to the admin. Never raises; returns the path or None."""
    try:
        import platform
        target = join_safe(root, DIAGNOSTIC_NAME)
        temporary = target.with_name(target.name + ".tmp")
        summary = {"errorCode": code, "message": mask_urls(message), "engine": "python",
                   "python": sys.version.split()[0], "platform": platform.platform(),
                   "createdAt": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        try:
            state = read_state_file(join_safe(root, ".portable-sync-state.json"))
            summary.update(packId=state.get("packId"), syncedVersion=state.get("version"),
                           syncedAt=state.get("syncedAt"), managedFiles=len(state.get("files") or []))
        except Exception:
            summary["syncedVersion"] = None
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            for rel in (".portable-sync-logs/latest.log", ".portable-sync-logs/previous.log", ".portable-staging/daemon.log"):
                path = root.joinpath(*rel.split("/"))
                if path.is_file():
                    archive.writestr(rel, mask_urls(path.read_text(encoding="utf-8", errors="replace")[-2_000_000:]))
        os.replace(temporary, target)
        return target
    except Exception:
        return None


def interactive_session() -> bool:
    """Only the player entry points opt in; tests and scripted runs never wait."""
    try:
        if os.environ.get("PORTABLE_SYNC_INTERACTIVE") != "1" or sys.stdin is None or not sys.stdin.isatty():
            return False
        if os.name != "nt":
            return True
        # Windows reports the NUL device as a tty; only a real console can answer.
        import ctypes
        import msvcrt
        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except (AttributeError, ValueError, OSError, ImportError):
        return False


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.5))
    if seconds < 60:
        return f"{max(1, seconds)} 秒"
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"


def megabytes(value: float) -> str:
    return f"{value / 1048576:.1f}"


class DownloadProgress:
    """PROGRESS_V1: one summary line every few seconds instead of one line per file."""

    def __init__(self, sizes, interval: float = 3.0):
        self.total_files = len(sizes)
        self.total_bytes = sum(sizes)
        self.done_files = 0
        self.done_bytes = 0
        self.interval = interval
        self.started = time.monotonic()
        self.last = self.started
        self.lock = threading.Lock()

    def file_done(self, size: int) -> None:
        with self.lock:
            self.done_files += 1
            self.done_bytes += size
            now = time.monotonic()
            if self.done_files < self.total_files and now - self.last < self.interval:
                return
            self.last = now
            line = self.render(now)
        sync_print(line)

    def render(self, now: float) -> str:
        text = f"[进度] {self.done_files}/{self.total_files} 个文件，{megabytes(self.done_bytes)}/{megabytes(self.total_bytes)} MB"
        elapsed = max(now - self.started, 0.001)
        rate = self.done_bytes / elapsed
        if self.done_files < self.total_files and rate > 0:
            text += f"，{megabytes(rate)} MB/s，预计还需 {format_duration((self.total_bytes - self.done_bytes) / rate)}"
        return text + "。"


_ROOT_HELPER_NAMES = ("player-update-generic.py", "player-update-generic.ps1", "portable-stage-daemon.py",
                      "portable-stage-daemon.ps1", "portable-bootstrap-refresh.ps1", "Windows-sync.bat", "macOS-sync.command")


def remove_root_helper_copies(root: pathlib.Path, files) -> int:
    """ROOT_DELEGATE_V1: the entry points now run the _updater copies. Root copies that older
    entry points refreshed are removed once _updater holds the verified same file."""
    rows = {str(row.get("path")): row for row in files or [] if isinstance(row, dict)}
    removed = 0
    for name in _ROOT_HELPER_NAMES:
        row = rows.get("_updater/" + name)
        copy = root / name
        if not row or name in rows or _is_link_or_reparse(copy) or not copy.is_file():
            continue
        try:
            if sha1(join_safe(root, "_updater/" + name)) != str(row.get("sha1") or "").lower():
                continue
            copy.unlink()
            removed += 1
        except (OSError, UpdateError):
            continue
    if removed:
        print(f"[清理] 已删除实例根目录里 {removed} 个旧的更新器副本（_updater 里有校验过的同一版本）。")
    return removed


def tidy_after_sync(root: pathlib.Path, backup_root: pathlib.Path) -> None:
    try:
        stale = join_safe(root, DIAGNOSTIC_NAME)
        if stale.is_file():
            stale.unlink()  # an old failure package would only confuse the player now
    except (OSError, UpdateError):
        pass
    try:
        pruned = prune_old_backups(root, backup_root)
        if pruned:
            print(f"[清理] 已删除 {pruned} 份旧备份，只保留本次备份。")
    except (OSError, UpdateError) as exc:
        print(f"[清理] 旧备份未能清理，不影响本次更新：{type(exc).__name__}")
    try:
        cleared = clear_stage_cache(root)
        if cleared:
            print(f"[清理] 已清理预下载缓存 {cleared} 个文件。")
    except (OSError, UpdateError) as exc:
        print(f"[清理] 预下载缓存未能清理，不影响本次更新：{type(exc).__name__}")


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
                       # Path.is_junction needs Python 3.12; the reparse tag works on 3.8+.
                       and not _is_name_surrogate(base / name)]
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
    parser.add_argument("--verify-all", action="store_true", help="hash every managed file and repair damaged ones")
    args = parser.parse_args()
    root = pathlib.Path(args.instance_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with instance_lock(root):
        with sync_log(root):
            try:
                return sync_instance(root, args)
            except SystemExit as exc:
                if exc.code and not isinstance(exc.code, int):
                    print(str(exc), file=sys.stderr)
                    report_failure(root, str(exc))
                raise
            except Exception as exc:
                if isinstance(exc, UpdateError):
                    message = str(exc)
                elif isinstance(exc, OSError):
                    message = str(local_file_error(exc, getattr(exc, "filename", None) or "客户端目录"))
                else:
                    message = f"[更新失败] 更新未完成，请联系管理员（{type(exc).__name__}）。"
                print(message, file=sys.stderr)
                report_failure(root, message)
                raise UpdateError(message) from None
            finally:
                _ACTIVE_JOURNAL = None


def report_failure(root, message):
    code = error_code(message)
    print(f"[错误编号] {code}", file=sys.stderr)
    package = write_diagnostic_package(root, code, message)
    if package is not None:
        print(f"[诊断] 已生成「{DIAGNOSTIC_NAME}」（在实例目录里），请把这个文件发给管理员，不需要截图。", file=sys.stderr)
    print("[日志] 错误已保存到 .portable-sync-logs/latest.log。", file=sys.stderr)


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
