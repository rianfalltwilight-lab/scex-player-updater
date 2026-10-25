#!/usr/bin/env bash
# @version 16
set -e
cd "$(dirname "$0")"
SCRIPT_DIR="$PWD"
INSTANCE_DIR="$SCRIPT_DIR"
if [ "$(basename "$SCRIPT_DIR")" = "_updater" ]; then
  INSTANCE_DIR="$(cd .. && pwd)"
fi
# ROOT_DELEGATE_V1: started from the instance root, run the _updater copy, so the helper
# refresh happens there and the instance root keeps only the entry files. A copy that does
# not parse is skipped; this run then repairs it through the normal sync.
if [ "$SCRIPT_DIR" = "$INSTANCE_DIR" ] && [ -z "${PORTABLE_ROOT_DELEGATED:-}" ] && [ -f "$INSTANCE_DIR/_updater/macOS-sync.command" ]; then
  if bash -n "$INSTANCE_DIR/_updater/macOS-sync.command" >/dev/null 2>&1; then
    export PORTABLE_ROOT_DELEGATED=1
    exec bash "$INSTANCE_DIR/_updater/macOS-sync.command" "$@"
  fi
fi

printf '\033]0;便携 Minecraft 玩家同步\007'
echo "============================================================"
echo "  便携 Minecraft 玩家同步"
echo "============================================================"
echo "实例目录: $INSTANCE_DIR"
echo

MANIFEST_URLS="${PORTABLE_MANIFEST_URL:-}"
# MAC_PYTHON_V1: /usr/bin/python3 exists even without the Command Line Tools; it is a
# stub that opens the "install command line developer tools" dialog. Probe by running it.
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys' >/dev/null 2>&1; then
  echo "[需要 Python 3] 这台 Mac 还不能运行更新器。"
  echo "  如果屏幕上弹出「需要安装命令行开发者工具」的窗口：这是正常的，点「安装」，等它装完（约几分钟）。"
  echo "  没有弹窗时：到 https://www.python.org/downloads/macos/ 下载安装 Python 3。"
  echo "  装好后重新双击本文件即可。"
  read -r -p "按回车关闭。"
  exit 1
fi

python_source_ok() {
  python3 -c 'import pathlib,sys; compile(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8-sig"), sys.argv[1], "exec")' "$1" >/dev/null 2>&1
}
# 旧版本自更新曾经原地覆盖正在执行的 Python 文件，异常中断会留下半个文件。
# 完整包根目录保留一份同源副本；若 _updater 副本无法解析，先原子恢复它。
if [ "$SCRIPT_DIR" != "$INSTANCE_DIR" ] && [ -f "$SCRIPT_DIR/player-update-generic.py" ] && [ -f "$INSTANCE_DIR/player-update-generic.py" ]; then
  if ! python_source_ok "$SCRIPT_DIR/player-update-generic.py" && python_source_ok "$INSTANCE_DIR/player-update-generic.py"; then
    repair_tmp="$SCRIPT_DIR/.player-update-generic.py.repair.$$"
    if cp "$INSTANCE_DIR/player-update-generic.py" "$repair_tmp" 2>/dev/null; then
      chmod +x "$repair_tmp" 2>/dev/null || true
      mv -f "$repair_tmp" "$SCRIPT_DIR/player-update-generic.py"
      echo "[修复] 已从包内副本恢复损坏的 _updater/player-update-generic.py。"
    fi
  fi
fi

set +e
python3 - "$MANIFEST_URLS" "$SCRIPT_DIR" "$0" <<'PY'
import hashlib, http.client, ipaddress, json, os, pathlib, re, signal, socket, subprocess, sys, tempfile, time, urllib.parse, urllib.request

# 更新源是家宽直连地址，绝不走系统代理：macOS 上 urllib 会自动读系统代理（Clash“设为系统
# 代理”），代理到不了 18088 就把自刷新请求整齐变成 503。空 ProxyHandler 强制直连、绕过代理。
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

def fetch_bytes(url, timeout=12):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == 'https':
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last = None
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except Exception as exc:
            last = exc
            continue
        if not infos:
            continue
        ip = infos[0][4][0]
        try:
            conn = http.client.HTTPConnection(ip, port, timeout=timeout)
            try:
                conn.request("GET", path, headers={"Host": host, "User-Agent": _ENTRY_UA})
                resp = conn.getresponse()
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status} from {ip}")
                return resp.read()
            finally:
                conn.close()
        except Exception as exc:
            last = exc
    if last:
        raise last
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()

# MANIFEST_TRUST_BEGIN: identical in player-update-generic.py, portable-stage-daemon.py and
# the macOS entry heredoc; tests/test_manifest_trust.py compares the copies.
# MANIFEST_SIGNATURE_V1: the publisher signs server-manifest.json into server-manifest.json.sig
# (RSA-3072, PKCS#1 v1.5, SHA-256). Once a sync has verified a signed manifest, the state says
# "manifestSignature": "required" and unsigned or wrongly signed manifests are refused.
# MANIFEST_CACHE_V1: the last verified manifest and signature are kept with their ETags, so an
# unchanged manifest costs two small 304 responses; the server may send it gzip-compressed.
import base64, gzip, hmac, http.client, socket, stat as _trust_stat, urllib.error  # noqa: E401

UPDATER_VERSION = "2.27.24"
TRUSTED_MANIFEST_KEYS = {"d95f8fce25c000bb": "36G6hVExT3FYPdPFSYf/TTxIqZ0Q8ybtxLqtR8KXgZ8yc2CNEJA+kW27ZO8vzQB7wrjKG7H58r0bkyHQpFRlcU0JygsWTypUzdCeNioTe5PP9vixwORTM37E4O4Dr+/IaLfxUQK+VsWhVMV30Wn2RWBrjLsVg5d9WeK0HC/Sm/VR2NJfCH8d9PLMFRptpp3z3mAaATBdyIMFh6+wlv241rayh/hLTSj6ZGtTcedlMrbY4lNR/b9VV0C/abhvQxU32qKOgLJEHmSxHh2YdzreW9lMBvzQOPztftnZjr1uCqm1+3H0B53iqHmqwvNf5MpQiajBK5bbm/tkXM/AVwuIe5z9kNDAWgDYr7FxiwlX5Glg5C3i4KQJK5eQn8X8m4GW22e/lfYEggvfPiNNYIn9k2C7itxI3W8jxe8GBuiNKiTuDkzOjw0RoOYfOYW3IGvVqy+MRt51bEp3XoVgw/DyeTi+RDQHR56n4Pkr78ISuVWOQKqHO4R7j4jSIM9maEM5"}
MANIFEST_CACHE_DIR = ".portable-manifest-cache"
_DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


class ManifestTrustError(Exception):
    """The manifest signature is missing, invalid or made with an untrusted key."""


def verify_manifest_signature(data, signature_file):
    """Return the id of the trusted key that signed data, or raise ManifestTrustError."""
    try:
        info = json.loads(signature_file.decode("utf-8-sig"))
        key_id = str(info["keyId"])
        signature = base64.b64decode(str(info["signature"]), validate=True)
        algorithm = info.get("algorithm")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ManifestTrustError("签名文件格式错误") from None
    if algorithm != "RSA-PKCS1-v1_5-SHA256":
        raise ManifestTrustError("签名算法不受支持")
    modulus = TRUSTED_MANIFEST_KEYS.get(key_id)
    if not modulus:
        raise ManifestTrustError("签名密钥不受信任")
    n = int.from_bytes(base64.b64decode(modulus), "big")
    size = (n.bit_length() + 7) // 8
    value = int.from_bytes(signature, "big")
    if len(signature) != size or value >= n:
        raise ManifestTrustError("签名与清单不符")
    digest = _DIGEST_INFO_SHA256 + hashlib.sha256(data).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    if not hmac.compare_digest(pow(value, 65537, n).to_bytes(size, "big"), expected):
        raise ManifestTrustError("签名与清单不符")
    return key_id


def manifest_signature_required(root):
    try:
        state = json.loads((pathlib.Path(root) / ".portable-sync-state.json").read_text(encoding="utf-8-sig"))
        return isinstance(state, dict) and state.get("manifestSignature") == "required"
    except (OSError, ValueError):
        return False


def _trust_get(url, headers, timeout):
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


def _trust_fetch(url, etag, cached, timeout, user_agent, optional=False):
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip", "Cache-Control": "no-cache"}
    if etag and cached is not None:
        headers["If-None-Match"] = etag
    status, response_headers, body = _trust_get(url, headers, timeout)
    if status == 304 and cached is not None:
        return cached, etag
    if status == 404 and optional:
        return None, ""
    if status != 200:
        raise urllib.error.HTTPError(url, status, f"HTTP {status}", None, None)
    if "gzip" in str(response_headers.get("content-encoding", "")).lower():
        body = gzip.decompress(body)
    return body, str(response_headers.get("etag", ""))


def _trust_cache_folder(root):
    folder = pathlib.Path(root) / MANIFEST_CACHE_DIR
    try:
        info = os.lstat(folder)
    except OSError:
        return folder
    if _trust_stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return None  # never follow a link planted in place of the cache folder
    return folder


def _trust_cache_read(root):
    folder = _trust_cache_folder(root)
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        manifest = (folder / "server-manifest.json").read_bytes()
        signature = (folder / "server-manifest.json.sig").read_bytes()
        if (hashlib.sha256(manifest).hexdigest() != meta["manifestSha256"] or
                hashlib.sha256(signature).hexdigest() != meta["signatureSha256"]):
            return None
        return {"manifest": manifest, "manifestEtag": str(meta.get("manifestEtag") or ""),
                "signature": signature, "signatureEtag": str(meta.get("signatureEtag") or "")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _trust_cache_write(root, manifest, manifest_etag, signature, signature_etag):
    folder = _trust_cache_folder(root)
    if folder is None:
        return
    meta = {"manifestEtag": manifest_etag, "manifestSha256": hashlib.sha256(manifest).hexdigest(),
            "signatureEtag": signature_etag, "signatureSha256": hashlib.sha256(signature).hexdigest()}
    try:
        folder.mkdir(exist_ok=True)
        for name, data in (("server-manifest.json", manifest), ("server-manifest.json.sig", signature),
                           ("meta.json", json.dumps(meta).encode("utf-8"))):
            temporary = folder / f"{name}.{os.getpid()}.tmp"
            temporary.write_bytes(data)
            os.replace(temporary, folder / name)
    except OSError:
        pass  # the cache only saves bandwidth


def fetch_trusted_manifest(url, root=None, timeout=20, user_agent="SCEX-updater", use_cache=True, required=None):
    """Return (manifest bytes, signing key id or None). ManifestTrustError means the
    signature is missing or wrong; OSError/ValueError mean the source is unreachable."""
    if required is None:
        required = bool(root) and manifest_signature_required(root)
    cache = _trust_cache_read(root) if (root and use_cache) else None
    for attempt in (1, 2):
        manifest, manifest_etag = _trust_fetch(url, cache and cache["manifestEtag"], cache and cache["manifest"], timeout, user_agent)
        signature, signature_etag = _trust_fetch(url + ".sig", cache and cache["signatureEtag"], cache and cache["signature"],
                                                 timeout, user_agent, optional=True)
        if signature is None:
            if required:
                raise ManifestTrustError("更新清单缺少签名")
            return manifest, None
        try:
            key_id = verify_manifest_signature(manifest, signature)
        except ManifestTrustError:
            try:
                named = json.loads(signature.decode("utf-8-sig")).get("manifestSha256")
            except (ValueError, AttributeError):
                named = None
            if attempt == 1 and named and named != hashlib.sha256(manifest).hexdigest():
                cache = None  # a publish is replacing the two files: fetch both again
                time.sleep(1.5)
                continue
            raise
        if root and use_cache and not (cache and manifest is cache["manifest"] and signature is cache["signature"]):
            _trust_cache_write(root, manifest, manifest_etag, signature, signature_etag)
        return manifest, key_id
    raise ManifestTrustError("签名与清单不符")
# MANIFEST_TRUST_END
_ENTRY_UA = f"SCEX-updater/{UPDATER_VERSION} (mac-entry {sys.version_info[0]}.{sys.version_info[1]}; {sys.platform})"


def watch_matches(pid, root):
    try:
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True, stderr=subprocess.DEVNULL).strip()
        # macOS ps does not preserve argv quotes. Match the exact --instance-dir
        # argument including spaces, ending at the next option or command end.
        expected = re.escape(str(root))
        return (re.search(r"portable-stage-daemon\.py(?:[\"']|\s|$)", command) is not None and
                re.search(r"--instance-dir\s+[\"']?" + expected + r"/?[\"']?(?=\s+--|$)", command) is not None and
                re.search(r"--mode\s+[\"']?watch(?:[\"']|\s|$)", command) is not None)
    except (OSError, subprocess.SubprocessError):
        return False

def stop_previous_watch(root):
    record = root / ".portable-staging/daemon.lock"
    if not record.is_file():
        return
    try:
        pid = int(record.read_text().strip())
    except ValueError:
        pid = 0
    if pid > 0 and watch_matches(pid, root):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(30):
            if not watch_matches(pid, root):
                break
            time.sleep(0.1)
        if watch_matches(pid, root):
            raise RuntimeError("旧后台预下载仍在运行，请关闭后重试。")
        print("[更新器] 已停止本实例的旧后台预下载。")
    if record.is_file() and record.read_text().strip() == str(pid):
        record.unlink()

original_self = pathlib.Path(sys.argv[3]).resolve().read_bytes()
manifest_urls = []
raw_override = sys.argv[1].strip()
script_dir = pathlib.Path(sys.argv[2]).resolve()
self_path = pathlib.Path(sys.argv[3]).resolve()
instance_guess = script_dir.parent if script_dir.name == "_updater" else script_dir
try:
    stop_previous_watch(instance_guess)
except (OSError, RuntimeError):
    print("[更新器] 无法安全停止旧后台预下载，请关闭该实例的更新器后重试。")
    raise SystemExit(1)
if raw_override:
    manifest_urls = re.split(r"[\r\n\x1f]+", raw_override)
else:
    # ROOT_SLIM_V1: since 2.27.23 the URL files live in _updater; older packs keep them in the root.
    for root in (instance_guess, script_dir, instance_guess / "_updater"):
        for name in ("UPDATE-URL.txt", "PORTABLE-UPDATE-URL.txt", "TFCR-update-url.txt", "UPDATE-URL-LAN.txt"):
            path = root / name
            if path.is_file():
                manifest_urls.extend(path.read_text(encoding="utf-8-sig").splitlines())
manifest_urls = [value.strip() for value in manifest_urls if value.strip() and not value.strip().startswith("#")]

# URL_RULES_BEGIN: keep identical to player-update-generic.py (tests/fixtures/url-order-cases.json).
def is_local_or_private_host(host):
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

def is_private_update_url(url):
    try:
        return is_local_or_private_host(urllib.parse.urlsplit(str(url).strip()).hostname or "")
    except ValueError:
        return False

def is_retired_cloud_url(url):
    try:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "7caiyun.com" or host.endswith(".7caiyun.com")

def is_usable_update_url(url):
    """Only absolute http(s) URLs with a host take part in ordering."""
    try:
        parsed = urllib.parse.urlsplit(str(url).strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def order_manifest_urls(urls, last_good=""):
    """Drop retired hosts; the first public URL, then listed LAN URLs in file order."""
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
# URL_RULES_END

manifest_urls = order_manifest_urls(manifest_urls)
if not manifest_urls:
    print("[更新器] 未找到更新地址，将使用包内同步脚本。")
    raise SystemExit(0)
selected_manifest_url = None
selected_manifest = None
trust_failed = False
for candidate in manifest_urls:
    try:
        raw_manifest, _key_id = fetch_trusted_manifest(candidate, instance_guess, timeout=8, user_agent=_ENTRY_UA)
        parsed = json.loads(raw_manifest.decode("utf-8-sig"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), list):
            continue
        selected_manifest_url, selected_manifest = candidate, parsed
        break
    except ManifestTrustError:
        trust_failed = True  # MANIFEST_SIGNATURE_V1: never refresh scripts from an unverified manifest
        continue
    except (OSError, ValueError, http.client.HTTPException):
        continue
if not selected_manifest_url:
    if trust_failed:
        print("[更新器] 更新清单签名校验没有通过，已保留本地脚本；主同步器会再次校验。")
    else:
        print("[更新器] 更新源暂时不可达，已保留本地脚本；主同步器将重试备用地址。")
    raise SystemExit(0)
if selected_manifest_url != manifest_urls[0]:
    print("[更新器] 已切换到备用更新地址。")
base = selected_manifest_url.rsplit("/", 1)[0] + "/"

def install_bytes(payload, dest, executable=False):
    # A sibling temporary file keeps replacement atomic without modifying a running inode.
    fd, name = tempfile.mkstemp(prefix="." + dest.name + ".portable-new-", dir=str(dest.parent))
    temp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
        if executable:
            temp.chmod(0o755)
        os.replace(temp, dest)
    finally:
        if temp.exists():
            temp.unlink()

restart = False
rows_by_path = {}
for row in selected_manifest["files"]:
    if isinstance(row, dict) and isinstance(row.get("path"), str):
        rows_by_path.setdefault(row["path"], []).append(row)
for name in ("player-update-generic.py", "player-update-generic.ps1", "portable-stage-daemon.py", "portable-stage-daemon.ps1", "portable-bootstrap-refresh.ps1", "Windows-sync.bat", "macOS-sync.command"):
    entries = rows_by_path.get("_updater/" + name, [])
    if (len(entries) != 1 or not re.fullmatch(r"[0-9a-fA-F]{40}", str(entries[0].get("sha1", ""))) or
            not isinstance(entries[0].get("size"), int) or entries[0]["size"] < 0):
        print(f"[更新器] 清单未提供有效校验信息，保留本地 {name}。")
        continue
    entry = entries[0]
    dest = script_dir / name
    url = urllib.parse.urljoin(base, "_updater/" + urllib.parse.quote(name)) + "?t=" + str(int(time.time() * 1000))
    try:
        # SELF_REFRESH_LOCAL_FIRST_V1: a local copy matching the manifest needs no download.
        payload = dest.read_bytes() if dest.is_file() else None
        if payload is not None and (len(payload) != entry["size"] or hashlib.sha1(payload).hexdigest() != entry["sha1"].lower()):
            payload = None
        fresh = payload is None
        if fresh:
            payload = fetch_bytes(url, timeout=12)
            if len(payload) != entry["size"] or hashlib.sha1(payload).hexdigest() != entry["sha1"].lower():
                raise ValueError("清单大小或 SHA1 校验失败")
            if name.endswith(".py"):
                compile(payload.decode("utf-8-sig"), name, "exec")  # Parse only; never execute downloaded code.
        if fresh:
            install_bytes(payload, dest, executable=name.endswith(".command"))
            print(f"[更新器] 已刷新 {name}")
        else:
            print(f"[更新器] 已是最新 {name}")
        if name == "macOS-sync.command" and self_path != dest and self_path.read_bytes() != payload:
            install_bytes(payload, self_path, executable=True)
            restart = True
        elif name == "macOS-sync.command" and self_path == dest:
            # The running bash still holds the old inode; re-exec once after replacement.
            restart = payload != original_self
    except (OSError, ValueError, SyntaxError, http.client.HTTPException):
        print(f"[更新器] 跳过自刷新 {name}（下载或清单校验失败，已保留原文件）。")
if restart:
    raise SystemExit(42)

PY
status=$?
set -e
if [ "$status" = "42" ]; then
  echo "[更新器] 当前脚本已更新，正在自动重启..."
  exec "$0"
fi

if [ "$status" != "0" ]; then
  echo "[更新器] 启动准备失败，退出码 $status。"
  exit "$status"
fi

set +e
PORTABLE_SYNC_INTERACTIVE=1 python3 "$SCRIPT_DIR/player-update-generic.py" --instance-dir "$INSTANCE_DIR"
status=$?
set -e
for script in "$INSTANCE_DIR/更新mod-Mac端.command" "$INSTANCE_DIR/启动游戏-Mac端.command" "$INSTANCE_DIR/macOS-sync.command" "$INSTANCE_DIR/_updater/macOS-sync.command"; do
  if [ -f "$script" ]; then
    chmod +x "$script" 2>/dev/null || true
  fi
done

echo
if [ "$status" = "0" ]; then
  echo "[同步] 完成。"
  if [ "${PORTABLE_SYNC_ONLY:-}" = "1" ]; then exit 0; fi
  LAUNCHER_DIR="$INSTANCE_DIR/_launchers"
  HMCL_JAR="$INSTANCE_DIR/HMCL.jar"
  if [ ! -f "$HMCL_JAR" ]; then
    HMCL_JAR="$LAUNCHER_DIR/HMCL.jar"
  fi
  if [ -f "$HMCL_JAR" ]; then
    # HMCL 默认把「工作目录/.minecraft」当游戏目录；实例目录本身是 .minecraft/versions/<名字>，
    # 必须从包含 .minecraft 的上层目录启动，HMCL 才能看到这个实例。
    HMCL_WORKDIR="$INSTANCE_DIR"
    _parent="$(dirname "$INSTANCE_DIR")"
    _grand="$(dirname "$_parent")"
    if [ "$(basename "$_parent")" = "versions" ] && [ "$(basename "$_grand")" = ".minecraft" ]; then
      HMCL_WORKDIR="$(dirname "$_grand")"
      echo "[启动器] 检测到 .minecraft/versions 结构，将从该目录启动 HMCL 以显示实例：$HMCL_WORKDIR"
    else
      echo "[提示] 实例不在 .minecraft/versions 目录下；若拉起的 HMCL 显示没有实例，请改用你导入整合包的那个 HMCL 启动。"
    fi
    echo "[启动器] 正在启动 HMCL..."
    if command -v java >/dev/null 2>&1; then
      (cd "$HMCL_WORKDIR" && nohup java -jar "$HMCL_JAR" >/dev/null 2>&1) &
      echo "[启动器] HMCL 已启动。"
    elif open "$HMCL_JAR" >/dev/null 2>&1; then
      echo "[启动器] HMCL 已启动。"
    else
      echo "[启动器] 找到 HMCL，但无法自动启动。请安装 Java，或手动打开：$HMCL_JAR"
    fi
  else
    echo "[启动器] 未找到 HMCL：$INSTANCE_DIR/HMCL.jar 或 $LAUNCHER_DIR/HMCL.jar"
  fi
  # 旧 Watch 已在同步前核验并停止；同步成功后启动当前版本。
  if [ -f "$SCRIPT_DIR/portable-stage-daemon.py" ]; then
    nohup python3 "$SCRIPT_DIR/portable-stage-daemon.py" --instance-dir "$INSTANCE_DIR" --mode watch >/dev/null 2>&1 &
    echo "[更新] 后台预下载已启动：管理员发布更新后会自动下好，重开游戏即可用上（无需再等下载）。"
  fi
else
  echo "[同步] 失败，退出码 $status。"
  # MAC_REPAIR_V1: offer the full check (every file hashed, damaged ones downloaded again).
  if [ -t 0 ] && [ -f "$SCRIPT_DIR/player-update-generic.py" ]; then
    echo
    read -r -p "是否运行完整检查修复（逐个校验所有文件，损坏的重新下载）？输入 Y 回车确认，直接回车跳过：" answer || answer=""
    case "$answer" in
      y|Y|yes|YES|是)
        set +e
        PORTABLE_SYNC_INTERACTIVE=1 python3 "$SCRIPT_DIR/player-update-generic.py" --instance-dir "$INSTANCE_DIR" --verify-all
        status=$?
        set -e
        if [ "$status" = "0" ]; then
          echo "[检查] 修复完成，可以启动游戏了。"
        fi
        ;;
    esac
  fi
  if [ "$status" != "0" ] && [ -f "$INSTANCE_DIR/更新诊断包.zip" ]; then
    echo "[诊断] 请把实例目录里的「更新诊断包.zip」发给管理员，不需要截图。"
  fi
fi
read -r -p "按回车关闭。" _ || true
exit "$status"

# 维护注意（新代 os.replace 版）：本文件自更新改为「同目录临时文件 + os.replace
# 原子换入」，运行中的 bash 持有旧 inode，替换不再影响正在执行的脚本；
# player-update-generic.py 的下载本来就是先落临时文件再 rename，同样安全。
# 因此运行本代代码的客户端更新到未来版本不再受「字节偏移/只能改尾部」限制，
# 但仍须保持 LF 换行 + UTF-8 无 BOM。
#
# 版本号头在 v9 过渡期曾暂时冻结，避免旧代 copy2 原地覆盖脚本后按旧字节偏移
# 继续解析而出现 unexpected EOF。当前自更新已改成同目录临时文件 + os.replace，
# v12 用于把“公开更新源优先 + 状态复用”安全推送到存量 Mac 客户端。
# v14：地址规则与主同步器统一（只用第一个公网地址，再试局域网，过滤已退役云端）；
# 自刷新先比对本地哈希，一致就不下载。
# v15：缺 python3 时说明命令行开发者工具弹窗；也从 _updater 读取更新地址；
# 文件被占用时可按回车重试；失败时提示发送「更新诊断包.zip」。
# v16：清单签名校验（MANIFEST_SIGNATURE_V1）；从根目录启动时改为运行 _updater 里的副本；
# 同步失败时可直接运行完整检查修复（_updater/macOS-self-repair.command 是同一功能的独立入口）。
