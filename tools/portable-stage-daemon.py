#!/usr/bin/env python3
# portable-stage-daemon.py —— 客户端后台"暂存 daemon"（macOS / Linux 版，与 Windows 的 .ps1 对应）
#
# 把整合包更新的下载耗时，从"下次启动前干等"提前到"玩家在线正在游戏时后台悄悄拉"。
# 管理员在线发布更新后，本进程后台把变更文件下到 .portable-staging/；玩家下次重启游戏时，
# 由主更新器按最新清单验证缓存，再执行备份、安装与对账；旧 promote 仅提示。
#
# 设计铁律（与 .ps1 完全一致）：
#   1) 只写 .portable-staging/，绝不改运行中的 mods/*.jar；player-update-generic.py 仍是唯一权威对账器。
#   2) 只在清单变化时才 diff（条件请求 If-Modified-Since，未变回 304，几乎零负担）。
#   3) 生命周期自管：检测本实例的 java 进程 + 最大时长 + 单例锁自退，不残留后台进程。
#   4) macOS 用 osascript 弹原生系统通知（可靠）；另写 READY.json + 更新已就绪.txt。
#   5) 保持 LF 换行 + UTF-8 无 BOM。
import argparse
import datetime as dt
import fnmatch
import hashlib
import ipaddress
import re
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# 家宽更新源必须直连：macOS Clash 系统代理会把非标端口请求整齐变成 503。
# 官方 CDN 有系统代理就走代理（国内直连经常能通但很慢）。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_DIRECT_OPENER)
_STAGE_UA = "portable-server-kit-stage/1.0"
_OFFICIAL_TIMEOUT_SEC = 8
_OFFICIAL_MIN_BYTES = 1024 * 1024
_OFFICIAL_MIN_RATE = 128 * 1024
_PROXY_URL = None
_PROXY_OPENER = None


META_SKIP = {"server-manifest.json", "update-log.txt"}
STAGE_META = {"READY.json", "daemon.lock", "daemon.log"}
_PRIMARY_UPDATE_URL_FILES = (
    "UPDATE-URL.txt",
    "PORTABLE-UPDATE-URL.txt",
    "TFCR-update-url.txt",
    "_updater/UPDATE-URL.txt",
    "_updater/PORTABLE-UPDATE-URL.txt",
)


def sha1(path: pathlib.Path) -> str:
    if not path.is_file():
        return ""
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rel(rel: str) -> str:
    rel = str(rel).replace("\\", "/").strip().lstrip("/")
    if not rel:
        raise ValueError("empty")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        raise ValueError(f"unsafe: {rel}")
    return rel


def join_safe(root: pathlib.Path, rel: str) -> pathlib.Path:
    return root.joinpath(*safe_rel(rel).split("/"))


def glob_match(rel: str, globs) -> bool:
    rel = str(rel).replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatchcase(rel, str(g).replace("\\", "/").lstrip("/")) for g in globs or [])


def read_update_urls(root: pathlib.Path, override: str) -> list:
    if override and override.strip():
        return [override.strip()]
    urls = []
    seen = set()
    for rel in _PRIMARY_UPDATE_URL_FILES:
        p = root.joinpath(*rel.split("/"))
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        except Exception:
            continue
        for raw in lines:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            urls.append(value)
    return urls


def read_update_url(root: pathlib.Path, override: str) -> str:
    """Backward-compatible single-URL accessor for older callers."""
    urls = read_update_urls(root, override)
    return urls[0] if urls else ""


def is_local_or_private_host(host: str) -> bool:
    # URL_RULES_V1: identical to player-update-generic.py (tests/fixtures/url-order-cases.json).
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
    try:
        host = urllib.parse.urlparse(str(url).strip()).hostname or ""
    except Exception:
        return False
    return is_local_or_private_host(host)


def is_retired_cloud_url(url: str) -> bool:
    host = (urllib.parse.urlparse(str(url) or "").hostname or "").lower().rstrip(".")
    return host == "7caiyun.com" or host.endswith(".7caiyun.com")


def is_usable_update_url(url):
    """Only absolute http(s) URLs with a host take part in ordering."""
    try:
        parsed = urllib.parse.urlsplit(str(url).strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def order_update_urls(urls: list, last_good: str = "") -> list:
    """URL_RULES_V1: same order as the main updater: drop retired hosts, the
    first public URL, then listed LAN URLs. ``last_good`` cannot reorder."""
    seen = set()
    out = []
    usable = [str(url or "").strip() for url in (urls or [])]
    usable = [url for url in usable if is_usable_update_url(url) and not is_retired_cloud_url(url)]
    public = [url for url in usable if not is_private_update_url(url)]
    for url in public[:1] + [url for url in usable if is_private_update_url(url)]:
        if url.lower() not in seen:
            seen.add(url.lower())
            out.append(url)
    return out


def probe_timeout_for_url(url: str) -> float:
    return 8


def fetch_manifest_probe(url: str):
    """Probe once and require a JSON manifest before starting a long-lived daemon.
    Returns (text, last_modified) so the first watch round reuses this download."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _STAGE_UA, "Cache-Control": "no-cache"})
        with _DIRECT_OPENER.open(req, timeout=probe_timeout_for_url(url)) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            payload = resp.read()
            last_modified = resp.headers.get("Last-Modified") if getattr(resp, "headers", None) else None
        text = payload.decode("utf-8-sig", errors="ignore")
        manifest = json.loads(text)
        if isinstance(manifest, dict) and isinstance(manifest.get("files"), list):
            return text, last_modified
    except Exception:
        pass
    return None


def probe_manifest_url(url: str) -> bool:
    return fetch_manifest_probe(url) is not None


def log(log_path: pathlib.Path, msg: str):
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify_mac(text: str):
    # macOS 原生系统通知（可靠、不抢焦点）。非 macOS 静默跳过。
    if sys.platform != "darwin":
        return
    safe = text.replace('"', "'").replace("\\", "")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "整合包更新已就绪" sound name "Glass"'],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def game_running(instance_root: pathlib.Path) -> bool:
    # 严格：只认命令行精确含本实例路径的进程 = 本实例的游戏在跑。守 A 实例的 daemon 绝不会被
    # 主客户端/别的实例带偏。macOS 的 ps 一定读得到完整命令行，没命中就是没在跑；ps 本身失败才保守当作在跑。
    try:
        out = subprocess.run(["ps", "-axww", "-o", "command="],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return True
    needle = str(instance_root)
    for line in out.splitlines():
        if "java" in line.lower() and needle in line:
            return True
    return False


class Manifest:
    def __init__(self):
        self.last_modified = None

    def if_changed(self, url: str):
        # 条件请求：带上一版 Last-Modified，未变服务器回 304（urllib 抛 HTTPError code=304）。
        req = urllib.request.Request(url)
        if self.last_modified:
            req.add_header("If-Modified-Since", self.last_modified)
        req.add_header("Cache-Control", "no-cache")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                lm = resp.headers.get("Last-Modified")
                if lm:
                    self.last_modified = lm
                data = resp.read()
            return True, data.decode("utf-8-sig", errors="ignore")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return False, None
            raise


def is_official_https_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url).strip())
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    return host not in ("", "localhost", "127.0.0.1", "::1")


def official_urls(item) -> list:
    raw = []
    if isinstance(item, dict):
        url = item.get("url")
        if url:
            raw.append(str(url).strip())
        for extra in item.get("downloads") or []:
            if extra:
                raw.append(str(extra).strip())
    seen = set()
    out = []
    for url in raw:
        key = url.lower()
        if key in seen or not is_official_https_url(url):
            continue
        seen.add(key)
        out.append(url)
    return out


def detect_system_proxy() -> str:
    flag = str(os.environ.get("PORTABLE_SYNC_NOPROXY") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return ""
    override = str(os.environ.get("PORTABLE_SYNC_PROXY") or "").strip()
    if override:
        if "://" not in override:
            override = "http://" + override
        if override.lower().startswith("socks"):
            return ""
        return override
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            if "://" not in value:
                value = "http://" + value
            if value.lower().startswith("socks"):
                continue
            return value
    return ""


def get_proxy_url() -> str:
    global _PROXY_URL
    if _PROXY_URL is None:
        _PROXY_URL = detect_system_proxy()
    return _PROXY_URL


def official_opener_for(url: str):
    proxy = get_proxy_url()
    if not proxy:
        return _DIRECT_OPENER
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith("192.168.") or host.startswith("10."):
        return _DIRECT_OPENER
    global _PROXY_OPENER
    if _PROXY_OPENER is None:
        _PROXY_OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return _PROXY_OPENER


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


def install_verified_file(temporary, destination):
    # Same-volume atomic replacement; never unlink the previous file first.
    if destination.is_symlink():
        raise OSError('Refusing to replace a symlink')
    old_mode = destination.stat().st_mode if destination.is_file() else None
    try:
        if os.name == 'nt' and old_mode is not None:
            destination.chmod(old_mode | 0o200)
        os.replace(temporary, destination)
    except OSError:
        if old_mode is not None and destination.is_file():
            destination.chmod(old_mode)
        raise


def download_verify(url: str, dest: pathlib.Path, expected: str, opener=None, timeout=120, min_bytes_after_timeout=0, min_rate_bps=0):
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".portable-stage-", suffix=".tmp", dir=dest.parent)
    os.close(fd)
    tmp = pathlib.Path(tmp_name)
    opener = opener or _DIRECT_OPENER
    req = urllib.request.Request(url, headers={"User-Agent": _STAGE_UA})
    try:
        with opener.open(req, timeout=timeout) as resp, tmp.open("wb") as out:
            copy_response(resp, out, timeout, min_bytes_after_timeout, min_rate_bps)
        actual = sha1(tmp)
        if actual != expected.lower():
            raise RuntimeError(f"SHA1 mismatch expected={expected} actual={actual}")
        install_verified_file(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def download_fail_reason(exc: BaseException) -> str:
    msg = str(exc)
    if "SHA1" in msg:
        return "哈希不符"
    if "too slow" in msg.lower() or "太慢" in msg:
        return "太慢"
    if "timed out" in msg.lower() or "timeout" in msg.lower():
        return "超时"
    if "404" in msg:
        return "404"
    if "403" in msg:
        return "403"
    return "连接失败"


class OfficialBreaker:
    """OFFICIAL_BREAKER_V1 for one watch round, same rule as the main updater:
    a host failing for LIMIT consecutive files is skipped for the rest of the
    round; a success resets it; file-specific failures do not count."""
    LIMIT = 3

    def __init__(self):
        self.failures = {}
        self.tripped = set()

    def allowed(self, host: str) -> bool:
        return host not in self.tripped

    def record(self, host: str, ok: bool, counted: bool = True) -> bool:
        """Record one file's outcome; True when this failure trips the host."""
        if ok:
            self.failures[host] = 0
            return False
        if not counted:
            return False
        self.failures[host] = self.failures.get(host, 0) + 1
        if self.failures[host] >= self.LIMIT and host not in self.tripped:
            self.tripped.add(host)
            return True
        return False


def counts_against_source(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return not (400 <= exc.code < 500 and exc.code not in (408, 429))
    return "SHA1 mismatch" not in str(exc)


def download_staged_entry(item, base: str, dest: pathlib.Path, expected: str, rel: str, log_path: pathlib.Path, breaker=None) -> str:
    breaker = breaker if breaker is not None else OfficialBreaker()
    for url in official_urls(item if isinstance(item, dict) else {}):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not breaker.allowed(host):
            continue
        started = time.time()
        try:
            download_verify(
                url, dest, expected,
                opener=official_opener_for(url),
                timeout=_OFFICIAL_TIMEOUT_SEC,
                min_bytes_after_timeout=_OFFICIAL_MIN_BYTES,
                min_rate_bps=_OFFICIAL_MIN_RATE,
            )
            breaker.record(host, True)
            return "official"
        except Exception as exc:
            log(log_path, f"[预下载] 官方源不可用（{time.time() - started:.1f}s，{download_fail_reason(exc)}），改走更新服务：{rel}")
            if breaker.record(host, False, counts_against_source(exc)):
                log(log_path, f"[预下载] 官方源 {host} 连续 {OfficialBreaker.LIMIT} 个文件下载失败，本轮不再尝试，其余文件直接走更新服务。")
    remote = item.get("downloadPath", rel) if isinstance(item, dict) else rel
    if remote != rel and not re.fullmatch(r"blobs/[0-9a-f]{64}", str(remote)):
        raise ValueError("Invalid cloud downloadPath")
    download_verify(url_for(base, remote), dest, expected, opener=_DIRECT_OPENER)
    return "home"


def url_for(base: str, rel: str) -> str:
    return base + "/".join(urllib.parse.quote(p) for p in safe_rel(rel).split("/"))


def load_sync_baseline(instance_root: pathlib.Path) -> dict:
    # 基线 = 上次同步写入状态文件的清单哈希（rel 小写 → sha1）。
    baseline = {}
    state_path = instance_root / ".portable-sync-state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
            for item in state.get("files") or []:
                p, h = item.get("path"), item.get("sha1")
                if p and h:
                    try:
                        baseline[safe_rel(str(p)).lower()] = str(h).lower()
                    except ValueError:
                        continue
        except Exception:
            pass
    return baseline


def load_sync_verified(instance_root: pathlib.Path) -> dict:
    """size/mtime/sha1 records the main updater wrote for files it just proved."""
    state_path = instance_root / ".portable-sync-state.json"
    try:
        records = json.loads(state_path.read_text(encoding="utf-8-sig")).get("verified") or {}
    except Exception:
        return {}
    return {str(key).lower(): value for key, value in records.items() if isinstance(value, dict)} if isinstance(records, dict) else {}


def verified_record_matches(verified, size: int, mtime: float) -> bool:
    """Same rule as player-update-generic.py: truncated or rounded whole seconds."""
    try:
        v_size = int(verified.get("size", -1))
        v_mtime = int(verified.get("mtime", -1))
    except (TypeError, ValueError, AttributeError):
        return False
    return v_size == size and v_mtime in (int(mtime), int(round(mtime)))


def run_promote(instance_root: pathlib.Path, stage_root: pathlib.Path, notice_path: pathlib.Path):
    # Compatibility for old launchers. Only the main updater may install verified cache.
    print("[暂存] 缓存将由主更新器按最新清单校验后安装；此步骤不改动客户端或缓存。")


class SyncLock:
    """Same persistent OS lock as both main engines; no PID or lock-file deletion."""
    def __init__(self, root):
        self.path = pathlib.Path(root) / ".portable-sync.lock"
        self.handle = None

    def acquire(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            api.CreateFileW.restype = wintypes.HANDLE
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            api.CloseHandle.restype = wintypes.BOOL
            handle = api.CreateFileW(str(self.path), 0xC0000000, 0, None, 4, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                if error in (32, 33):
                    return False
                raise ctypes.WinError(error)
            self.handle = handle
            self.api = api
        else:
            import fcntl
            stream = self.path.open("a+b")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                return False
            except BaseException:
                stream.close()
                raise
            self.handle = stream
        return True

    def release(self):
        if self.handle is not None:
            if os.name == "nt":
                self.api.CloseHandle(self.handle)
            else:
                self.handle.close()
            self.handle = None


def run_watch(instance_root, stage_root, ready_path, notice_path, lock_path, log_path,
              manifest_url, interval, max_runtime_min, grace_min, idle_exit_checks,
              initial_text=None, initial_last_modified=None):
    # 单例锁
    if lock_path.is_file():
        try:
            old = int(lock_path.read_text().strip())
        except Exception:
            old = 0
        if pid_alive(old):
            print(f"[暂存] 已有 daemon 在运行（PID {old}），本次退出。")
            return
    lock_path.write_text(str(os.getpid()))

    base = manifest_url.rsplit("/", 1)[0] + "/"
    log(log_path, f"暂存 daemon 启动 (PID {os.getpid()})，间隔 {interval}s，最长 {max_runtime_min} 分钟。")
    proxy = get_proxy_url()
    if proxy:
        log(log_path, f"已检测到系统代理，官方源走代理；家宽更新服务始终直连。")
    else:
        log(log_path, "未检测到系统代理，官方源直连（太慢则回落更新服务）。")

    local_cache = {}

    def local_sha(path: pathlib.Path, record=None) -> str:
        try:
            st = path.stat()
        except OSError:
            return ""
        if not stat.S_ISREG(st.st_mode):
            return ""
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key in local_cache:
            return local_cache[key]
        # DAEMON_VERIFIED_V1: the sync that started this watcher just proved most
        # files; trust its size+mtime record instead of re-reading ~1 GB at launch.
        recorded = str(record.get("sha1") or "").lower() if isinstance(record, dict) else ""
        if re.fullmatch(r"[0-9a-f]{40}", recorded) and verified_record_matches(record, st.st_size, st.st_mtime):
            h = recorded
        else:
            h = sha1(path)
        local_cache[key] = h
        return h

    manifest = Manifest()
    if initial_last_modified:
        manifest.last_modified = initial_last_modified
    pending_text = initial_text
    start = time.time()
    last_text = ""
    idle = 0
    staged_total = 0
    first = True
    try:
        while True:
            time.sleep(3 if first else interval)
            first = False

            elapsed_min = (time.time() - start) / 60.0
            if elapsed_min >= max_runtime_min:
                log(log_path, "达到最长运行时间，退出。")
                break
            if elapsed_min >= grace_min:
                if game_running(instance_root):
                    idle = 0
                else:
                    idle += 1
                    if idle >= idle_exit_checks:
                        log(log_path, "检测到游戏已退出，暂存 daemon 收工。")
                        break
            sync_lock = SyncLock(instance_root)
            try:
                if not sync_lock.acquire():
                    log(log_path, "主更新器正在同步，跳过本轮预下载。")
                    continue
                if pending_text is not None:
                    # The startup probe already downloaded this manifest.
                    changed, text, pending_text = True, pending_text, None
                else:
                    try:
                        changed, text = manifest.if_changed(manifest_url)
                    except Exception as exc:
                        log(log_path, f"清单获取失败：{exc}")
                        continue
                if not changed or not text:
                    continue
                if text == last_text:
                    continue
                try:
                    data = json.loads(text)
                except Exception:
                    log(log_path, "清单解析失败，跳过本轮。")
                    continue
                files = data.get("files") or []
                if not files:
                    last_text = text
                    continue
                last_text = text
                preserve = data.get("preserveLocalChangeGlobs") or []
                preserve_deletions = data.get("preserveLocalDeletionGlobs") or []
                force_sync = data.get("forceSyncGlobs") or []
                preserve_player = bool(data.get("preservePlayerCustomizations", True))
                plat = "mac" if sys.platform == "darwin" else ("windows" if sys.platform.startswith("win") else "linux")
                platform_excludes = (data.get("platformExcludeGlobs") or {}).get(plat) or []
                # 基线 = 上次同步的清单哈希：本地文件与基线不符即玩家改过，不预下载（与 Promote 同一防线）。
                baseline = load_sync_baseline(instance_root)
                verified = load_sync_verified(instance_root)
                breaker = OfficialBreaker()

                log(log_path, "检测到新清单，开始比对并后台预下载……")
                new_staged = 0
                staged_list = []
                for item in files:
                    try:
                        rel = safe_rel(str(item.get("path", "")))
                    except ValueError:
                        continue
                    if rel in META_SKIP:
                        continue
                    expected = str(item.get("sha1", "")).lower()
                    if not expected:
                        continue
                    is_force_sync = glob_match(rel, force_sync)
                    if not is_force_sync and glob_match(rel, preserve):
                        continue
                    if platform_excludes and glob_match(rel, platform_excludes):
                        continue
                    local_hash = local_sha(join_safe(instance_root, rel), verified.get(rel.lower()))
                    if local_hash == expected:
                        continue
                    base_hash = baseline.get(rel.lower(), "")
                    # 与 player-update-generic.py 保持同一语义：已在上次清单中、
                    # 当前被玩家删除、且命中删除保留规则的文件不预下载。
                    # 若旧 daemon 已留下暂存件，也在这里丢弃，避免下一次落位复活。
                    if (not local_hash and base_hash and preserve_player and
                            not is_force_sync and glob_match(rel, preserve_deletions)):
                        stage_path = join_safe(stage_root, rel)
                        if stage_path.is_file():
                            try:
                                stage_path.unlink()
                                log(log_path, f"[预下载] 保留玩家删除，丢弃旧暂存：{rel}")
                            except Exception as exc:
                                log(log_path, f"[预下载] 无法清理旧暂存：{rel}；{exc}")
                        continue
                    if not is_force_sync and local_hash:
                        # 本地存在但与基线不符（或没有基线记录）= 玩家改过：不预下载，交给权威同步器裁决。
                        if not base_hash or local_hash != base_hash:
                            continue
                    stage_path = join_safe(stage_root, rel)
                    if stage_path.is_file() and sha1(stage_path) == expected:
                        continue
                    try:
                        source = download_staged_entry(item, base, stage_path, expected, rel, log_path, breaker)
                        log(log_path, f"[预下载/官方源] {rel}" if source == "official" else f"[预下载] {rel}")
                        new_staged += 1
                        staged_list.append(rel)
                    except Exception as exc:
                        log(log_path, f"[预下载失败] {rel}；{exc}")

                if new_staged > 0:
                    staged_total += new_staged
                    version = str(data.get("version") or "")
                    ready_path.write_text(json.dumps({
                        "version": version,
                        "stagedAt": dt.datetime.now().isoformat(timespec="seconds"),
                        "count": staged_total,
                        "lastBatch": staged_list,
                    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    notice_path.write_text(
                        f"整合包有更新，已在后台下载完成 {staged_total} 个文件。\n"
                        f"重启游戏即可自动生效（无需再等下载）。\n版本：{version}\n",
                        encoding="utf-8")
                    log(log_path, f"本轮预下载完成 {new_staged} 个，累计 {staged_total} 个；已写就绪标记。")
                    notify_mac(f"整合包更新已在后台下载完成（{staged_total} 个文件），退出游戏重开即可生效")
                else:
                    log(log_path, "清单有变化，但无需预下载的新文件（可能只是元数据/被保留项）。")
            except Exception as exc:
                log(log_path, f"本轮异常已跳过：{exc}")
            finally:
                sync_lock.release()
    finally:
        try:
            if lock_path.read_text().strip() == str(os.getpid()):
                lock_path.unlink()
        except Exception:
            pass
        log(log_path, "暂存 daemon 已退出。")


def show_watch_banner():
    """WATCH_BANNER_V1: on Windows this runs in its own minimized console window;
    tell the player what it is. On macOS/Linux it runs without a window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("整合包后台预下载（可以关闭，不影响游戏）")
    except Exception:
        pass
    try:
        print("=" * 60)
        print("  这是整合包后台预下载窗口：玩游戏时悄悄下载管理员新发布的更新，")
        print("  下次启动时就不用再等。可以直接关闭，不影响游戏；下次运行更新器会重新开启。")
        print("  游戏退出后它会自己结束。")
        print("=" * 60, flush=True)
    except (OSError, ValueError, UnicodeError, AttributeError):
        pass  # redirected to a non-UTF-8 pipe or no console at all: the banner is optional


def main() -> int:
    ap = argparse.ArgumentParser(description="Portable staging daemon (macOS/Linux)")
    ap.add_argument("--instance-dir", default=".")
    ap.add_argument("--manifest-url", default="")
    ap.add_argument("--mode", choices=("watch", "promote"), default="watch")
    ap.add_argument("--interval-seconds", type=int, default=30)
    ap.add_argument("--max-runtime-minutes", type=int, default=720)
    ap.add_argument("--grace-minutes", type=int, default=15)
    ap.add_argument("--idle-exit-checks", type=int, default=3)
    args = ap.parse_args()

    instance_root = pathlib.Path(args.instance_dir).resolve()
    stage_root = instance_root / ".portable-staging"
    ready_path = stage_root / "READY.json"
    notice_path = instance_root / "更新已就绪.txt"
    lock_path = stage_root / "daemon.lock"
    log_path = stage_root / "daemon.log"

    if args.mode == "promote":
        run_promote(instance_root, stage_root, notice_path)
        return 0

    stage_root.mkdir(parents=True, exist_ok=True)
    show_watch_banner()
    manifest_urls = read_update_urls(instance_root, args.manifest_url)
    if not manifest_urls:
        log(log_path, "未找到 UPDATE-URL，暂存 daemon 退出。")
        return 0
    last_good = ""
    state_file = instance_root / ".portable-sync-state.json"
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8-sig"))
            last_good = str(state.get("lastManifestUrl") or state.get("manifestUrl") or "")
        except Exception:
            last_good = ""
    manifest_urls = order_update_urls(manifest_urls, last_good)
    manifest_url = ""
    selected_index = -1
    probe = None
    for index, candidate in enumerate(manifest_urls):
        probe = fetch_manifest_probe(candidate)
        if probe is not None:
            manifest_url = candidate
            selected_index = index
            break
    if not manifest_url:
        log(log_path, "更新源暂时不可达，暂存 daemon 本轮不启动；下次玩家同步会重试。")
        return 0
    if selected_index > 0:
        log(log_path, "已切换到备用更新地址。")
    run_watch(instance_root, stage_root, ready_path, notice_path, lock_path, log_path,
              manifest_url, args.interval_seconds, args.max_runtime_minutes,
              args.grace_minutes, args.idle_exit_checks,
              initial_text=probe[0], initial_last_modified=probe[1])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
