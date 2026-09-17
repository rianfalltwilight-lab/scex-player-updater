#!/usr/bin/env python3
"""Portable updater administration. Python 3.10+, standard library only.

Never inspect arbitrary environment values or operate the Minecraft server.
Paths in config/environment are relative to the updater repository root.
"""
import argparse
import contextlib
import copy
from datetime import datetime, timezone
import fnmatch
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import secrets
import shutil
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
ENV = {
    'PORTABLE_SOURCE_CLIENT': ('sourceClient',),
    'PORTABLE_PUBLISH_DIR': ('publishDir',),
    'PORTABLE_PACK_ID': ('packId',),
    'PORTABLE_PACK_NAME': ('packName',),
    'PORTABLE_VERSION': ('version',),
    'PORTABLE_UPDATE_HOST': ('update', 'host'),
    'PORTABLE_UPDATE_PORT': ('update', 'port'),
    'PORTABLE_UPDATE_BIND': ('update', 'bind'),
    'PORTABLE_UPDATE_SCHEME': ('update', 'scheme'),
}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def local_path(root, value):
    value = str(value).replace('\\', '/')
    if os.name != 'nt' and (PureWindowsPath(value).drive or value.startswith('//')):
        raise ValueError('Windows path on Linux/macOS: set a local path instead.')
    return Path(os.path.expanduser(value)) if Path(value).is_absolute() else root / value


def no_links(path):
    """Reject symlinks and Windows junctions, including ancestors."""
    for p in (path, *path.parents):
        if p.is_symlink() or (p.exists() and getattr(p.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError(f'Linked path is not supported: {p}')


def client_candidate(path):
    return (path.is_dir() and not (path / 'server.properties').exists()
            and not (path / 'eula.txt').exists()
            and any((path / n).is_dir() for n in ('mods', 'config')))


def discover_clients(base):
    # Bounded known launcher layouts; never walk disks or read launcher accounts.
    possible = [base, base / '.minecraft', base / 'minecraft']
    for name in ('main-client', 'client', '客户端'):
        possible.extend([base / name, base / name / '.minecraft'])
    for container in (base / '.minecraft/versions', base / 'versions', base / 'instances'):
        if container.is_dir():
            no_links(container)
            for child in sorted(container.iterdir()):
                if child.is_dir():
                    possible.extend([child, child / '.minecraft', child / 'minecraft'])
    found = []
    for p in possible:
        if client_candidate(p):
            no_links(p)
            p = p.resolve()
            if p not in found:
                found.append(p)
    return found


def load_config(root, config_path=None, overrides=None, environ=None, allow_new_config=False):
    env = os.environ if environ is None else environ
    supplied = config_path or env.get('PORTABLE_CONFIG')
    path = local_path(root, supplied or 'tools/portable-pack.json')
    if supplied and not path.is_file() and not allow_new_config:
        raise ValueError(f'Explicit configuration not found: {path}')
    config = read_json(path) if path.exists() else read_json(root / 'tools/portable-pack.example.json')
    if not isinstance(config, dict) or not isinstance(config.get('update', {}), dict):
        raise ValueError('Configuration and update must be JSON objects.')
    if not path.exists():
        config['sourceClient'] = 'auto'
    origins = {}
    for name, keys in ENV.items():
        if env.get(name):
            target = config
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = env[name]
            origins['.'.join(keys)] = name
    for key, value in (overrides or {}).items():
        if value is not None:
            keys = key.split('.')
            target = config
            for part in keys[:-1]:
                target = target.setdefault(part, {})
            target[keys[-1]] = value
            origins[key] = 'command line'
    return config, path, origins


def resolve_config(root, config, require_source=True):
    c = copy.deepcopy(config)
    for key in ('includeRoots', 'includeFiles', 'excludeGlobs', 'preserveLocalChangeGlobs',
                'preserveLocalDeletionGlobs', 'additiveDirs', 'forceSyncGlobs', 'forceDeleteGlobs'):
        if key in c and (not isinstance(c[key], list) or any(not isinstance(v, str) for v in c[key])):
            raise ValueError(f'{key} must be a JSON array of strings.')
    if 'allowEmptyClient' in c and type(c['allowEmptyClient']) is not bool:
        raise ValueError('allowEmptyClient must be true or false, not a string.')
    raw = c.get('sourceClient', 'auto')
    if (not raw or raw == 'auto') and not require_source:
        source = root / 'main-client'
    elif not raw or raw == 'auto':
        candidates = discover_clients(root)
        if len(candidates) != 1:
            choices = '\n'.join(str(p) for p in candidates) or '(none)'
            raise ValueError('Client detection needs exactly one instance. Set --source-client or '
                             'PORTABLE_SOURCE_CLIENT explicitly. Candidates:\n' + choices)
        source = candidates[0]
    else:
        source = local_path(root, raw)
        no_links(source)
        source = source.resolve()
        if require_source and not client_candidate(source):
            raise ValueError('sourceClient must contain mods/config and must not be a server directory: '
                             + str(source))
    publish = local_path(root, c.get('publishDir', 'modpack-public/portable'))
    no_links(publish)
    publish = publish.resolve()
    root = root.resolve()
    if not publish.is_relative_to(root) or publish == root:
        raise ValueError('publishDir must be a dedicated subdirectory of this updater repository.')
    if require_source and (publish.is_relative_to(source) or source.is_relative_to(publish)):
        raise ValueError('Client source and publishDir must not overlap.')
    relative = publish.relative_to(root)
    if relative.parts[0] in ('tools', 'tests', 'docs', '.git', '.github', '一键脚本'):
        raise ValueError('publishDir overlaps updater source code.')
    u = c.setdefault('update', {})
    try:
        u['port'] = int(u.get('port', 18088))
    except (TypeError, ValueError):
        raise ValueError('update.port must be a number.') from None
    if not 1 <= u['port'] <= 65535:
        raise ValueError('update.port must be between 1 and 65535.')
    host = str(u.get('host', '127.0.0.1')).strip('[]')
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_unspecified:
            raise ValueError('Advertised host cannot be 0.0.0.0 or ::; set the player-facing host.')
    except ValueError:
        if host in ('0.0.0.0', '::') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
            raise ValueError('update.host must be a reachable hostname or IP, without a scheme or port.') from None
    u['host'] = host
    if u.get('scheme', 'http') not in ('http', 'https'):
        raise ValueError('update.scheme must be http or https.')
    u.setdefault('bind', '127.0.0.1')
    token_path = local_path(root, u.get('tokenFile', '.update-server-token'))
    no_links(token_path)
    token_path = token_path.resolve()
    if not token_path.is_relative_to(root) or token_path == root or token_path.is_relative_to(publish) or token_path.is_relative_to(source):
        raise ValueError('Token must be a private file inside the repository, outside client/publish directories.')
    c['sourceClient'], c['publishDir'] = str(source), str(publish)
    return c, source, publish, token_path


def client_metadata(source):
    """Use launcher version metadata when present; absent/ambiguous stays unknown."""
    files = [source / (source.name + '.json'), source / 'mmc-pack.json']
    result = {}
    for path in files:
        if not path.is_file():
            continue
        no_links(path)
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        data = read_json(path)
        for component in data.get('components', []):
            uid = component.get('uid', '')
            if uid == 'net.minecraft':
                result['minecraftVersion'] = component.get('version', '')
            for loader in ('neoforge', 'forge', 'fabric', 'quilt'):
                if loader in uid:
                    result['loader'] = {'type': loader, 'version': component.get('version', '')}
        if data.get('inheritsFrom'):
            result['minecraftVersion'] = data['inheritsFrom']
        for lib in data.get('libraries', []):
            name = lib.get('name', '').split(':')
            if len(name) >= 3:
                for group, artifact, loader in [('net.neoforged', 'neoforge', 'neoforge'),
                        ('net.minecraftforge', 'forge', 'forge'), ('net.fabricmc', 'fabric-loader', 'fabric'),
                        ('org.quiltmc', 'quilt-loader', 'quilt')]:
                    if name[:2] == [group, artifact]:
                        result['loader'] = {'type': loader, 'version': name[2]}
    return result


def safe_rel(name):
    n = str(name).replace('\\', '/')
    if not n or n.startswith('/') or any(p in ('', '.', '..') for p in n.split('/')):
        raise ValueError(f'Unsafe relative path: {name}')
    if any(ch in n for ch in ':\0\r\n<>"|?*') or any(p.endswith((' ', '.')) or
            re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p) for p in n.split('/')):
        raise ValueError(f'Path is not portable to Windows clients: {name}')
    return n


def validate_version(c, previous):
    version = str(c.get('version', ''))
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('version must use X.Y.Z.')
    old = previous.get('version')
    if old and old != version:
        level = c.get('releaseLevel')
        if level not in ('major', 'minor', 'patch') or not str(c.get('releaseDecision', '')).strip():
            raise ValueError('A version change needs releaseLevel and releaseDecision.')
        parts = [int(v) for v in str(old).split('.')]
        if len(parts) != 3:
            raise ValueError('Previous version is not X.Y.Z.')
        i = ('major', 'minor', 'patch').index(level)
        parts[i] += 1
        parts[i + 1:] = [0] * (2 - i)
        if version != '.'.join(map(str, parts)):
            raise ValueError('Version increment does not match releaseLevel.')
    if c.get('requireReleaseNotes') and (c.get('releaseNotesVersion') != version or not c.get('releaseNotes')):
        raise ValueError('releaseNotesVersion/releaseNotes must match this version.')


def token_value(path, create=False):
    if not path.exists() and create:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='ascii') as f:
            if os.name != 'nt':
                os.fchmod(f.fileno(), 0o600)
            f.write(secrets.token_hex(24) + '\n')
    if not path.is_file():
        raise ValueError('Token file missing; publish first.')
    token = path.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[-A-Za-z0-9_]{24,80}', token):
        raise ValueError('Invalid update token file.')
    return token


def url_for(c, token):
    u = c['update']
    host = '[' + u['host'] + ']' if ':' in u['host'] else u['host']
    return f"{u.get('scheme', 'http')}://{host}:{u['port']}/{token}/server-manifest.json"


def file_sha1(path):
    digest = hashlib.sha1()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def publish_lock(root):
    path = root / '.portable-publish-lock'
    try:
        path.mkdir()
    except FileExistsError:
        raise ValueError('Publisher lock exists. Check the recorded PID before removing a stale lock.') from None
    try:
        (path / 'owner.json').write_text(json.dumps({'pid': os.getpid(), 'host': platform.node()}), encoding='utf-8')
        # Use the legacy PowerShell lock path as well. Its exclusive FileShare
        # conflicts with this open handle on Windows; flock covers Unix writers.
        lock_file = root / 'tmp/portable-publish.lock'
        no_links(lock_file)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with lock_file.open('a+b') as stream:
            if stream.tell() == 0:
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        (path / 'owner.json').unlink(missing_ok=True)
        path.rmdir()


def publish(root, config):
    c, source, target, token_path = resolve_config(root, config)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', str(c.get('packId', ''))):
        raise ValueError('packId must contain letters, digits, dot, underscore or hyphen.')
    if c.get('launchers', {}).get('include'):
        raise ValueError('This portable publisher does not bundle launchers. Use launchers.include=false; existing client launchers are detected locally.')
    with publish_lock(root):
        previous = {}
        if target.exists() and any(target.iterdir()):
            if not (target / 'server-manifest.json').is_file():
                raise ValueError('Nonempty publishDir is not a recognized update source; refusing to replace it.')
            previous = read_json(target / 'server-manifest.json')
            if previous.get('packId') != c['packId'] or previous.get('format') != 2:
                raise ValueError('Existing update source belongs to a different pack or format.')
        validate_version(c, previous)
        token = token_value(token_path, create=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.portable-build-', dir=target.parent))
        backup = None
        try:
            seen = {}
            def add_file(src, rel):
                rel = safe_rel(rel)
                no_links(src)
                if not src.is_file():
                    raise ValueError(f'Missing input: {src}')
                key = rel.casefold()
                if key in seen:
                    if seen[key] == rel:
                        return
                    raise ValueError(f'Case-colliding paths cannot be published: {rel}')
                seen[key] = rel
                out = stage / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, out)
            for name in c.get('includeRoots', []):
                name = safe_rel(name)
                if name.split('/')[0].lower() in ('saves', 'world', 'logs', '.git', '_updater'):
                    raise ValueError(f'Not a distributable client root: {name}')
                base = source / name
                no_links(base)
                if not base.exists():
                    continue
                if not base.is_dir():
                    raise ValueError(f'includeRoots entry is not a directory: {name}')
                for directory, dirs, files in os.walk(base):
                    for entry in dirs + files:
                        no_links(Path(directory) / entry)
                    for entry in files:
                        src = Path(directory) / entry
                        rel = src.relative_to(source).as_posix()
                        if not any(fnmatch.fnmatchcase(rel.lower(), str(g).lower()) for g in c.get('excludeGlobs', [])):
                            add_file(src, rel)
            for name in c.get('includeFiles', []):
                name = safe_rel(name)
                if name.startswith('.') or name.lower() in ('server.properties', 'eula.txt', 'accounts.json', 'launcher_accounts.json'):
                    raise ValueError(f'Private/server file cannot be distributed: {name}')
                if not any(fnmatch.fnmatchcase(name.lower(), str(g).lower()) for g in c.get('excludeGlobs', [])):
                    add_file(source / name, name)
            if not seen and not c.get('allowEmptyClient', False):
                raise ValueError('No distributable client files found. Check sourceClient/includeRoots; '
                                 'set allowEmptyClient=true only for an intentional empty release.')
            tools = ['player-update-generic.py', 'player-update-generic.ps1',
                     'portable-stage-daemon.py', 'portable-stage-daemon.ps1',
                     'portable-bootstrap-refresh.ps1', 'portable-windows-repair.ps1',
                     'portable-windows-repair.bat', 'player-self-repair.ps1']
            for name in tools:
                add_file(root / 'tools' / name, '_updater/' + name)
            for src, dest in [('portable-windows-sync.bat', '_updater/Windows-sync.bat'),
                              ('portable-client-entry.bat', '更新mod-Windows端.bat'),
                              ('portable-macos-sync.command', '_updater/macOS-sync.command'),
                              ('portable-unix-sync.sh', '更新mod-Linux端.sh'),
                              ('portable-unix-sync.sh', '更新mod-Mac端.command')]:
                add_file(root / 'tools' / src, dest)
            add_file(root / '一键脚本/一键客户端自助修复.bat', '一键客户端自助修复.bat')
            url = url_for(c, token)
            for name in ('UPDATE-URL.txt', 'PORTABLE-UPDATE-URL.txt'):
                (stage / name).write_text(url + '\n', encoding='utf-8')
            (stage / 'README-sync.txt').write_text('Windows: 更新mod-Windows端.bat may be copied alone beside the launcher.\nLinux/macOS: run the shell entry inside the client instance.\nKeep _updater and UPDATE-URL.txt in the instance.\n', encoding='utf-8')
            rows = []
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    rows.append({'path': path.relative_to(stage).as_posix(), 'size': path.stat().st_size, 'sha1': file_sha1(path)})
            manifest = {k: c[k] for k in ('packId', 'packName', 'version', 'minecraftVersion', 'loader',
                'preserveLocalChangeGlobs', 'preserveLocalDeletionGlobs', 'additiveDirs', 'adoptExistingFiles',
                'forceSyncGlobs', 'forceDeleteGlobs', 'cleanup', 'playerOptions', 'serverList', 'platformExcludeGlobs') if k in c}
            manifest.update(format=2, pack=c['packId'], name=c['packId'],
                generatedAt=datetime.now(timezone.utc).isoformat(), updateUrl=url,
                preservePlayerCustomizations=True, files=rows,
                releaseNotes=c.get('releaseNotes', []) if c.get('releaseNotesVersion') == c['version'] else [])
            # Missing optional policy must not enable destructive defaults in old clients.
            manifest.setdefault('cleanup', {k: False for k in ('removeConnectorCache', 'disableLauncherRepairIndex', 'disableDuplicateMods')})
            manifest.setdefault('serverList', {'enabled': False})
            (stage / 'server-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            if target.exists():
                backup = target.with_name('.portable-backup-' + uuid.uuid4().hex)
                target.rename(backup)
            try:
                stage.rename(target)
            except BaseException:
                if backup:
                    backup.rename(target)
                raise
            return {'version': c['version'], 'files': len(rows), 'publishDir': str(target),
                    'backup': str(backup) if backup else None, 'playerUrlFile': str(target / 'UPDATE-URL.txt')}
        finally:
            if stage.exists():
                assert stage.parent == target.parent and stage.name.startswith('.portable-build-')
                shutil.rmtree(stage)


def serve(root, config):
    # Serving an existing release must work even when the build client is unmounted.
    c, _, target, token_path = resolve_config(root, config, require_source=False)
    manifest = read_json(target / 'server-manifest.json')
    if manifest.get('packId') != config['packId']:
        raise ValueError('Configured packId does not match published content.')
    token = token_value(token_path)
    if manifest.get('updateUrl') != url_for(c, token):
        raise ValueError('Host/port/scheme/token changed since publication; publish again before serving.')
    spec = importlib.util.spec_from_file_location('portable_http', root / 'tools/secure-update-server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import functools
    import ssl
    u = c['update']
    server_cls = module.DualStackThreadingHTTPServer if ':' in u['bind'] else module.QuietThreadingHTTPServer
    handler = functools.partial(module.SecureUpdateHandler, directory=str(target), token=token)
    cert = u.get('certFile')
    if cert and u.get('scheme') != 'https':
        raise ValueError('TLS certificate configured but update.scheme is not https.')
    if u.get('scheme') == 'https' and not cert and not u.get('reverseProxyTls'):
        raise ValueError('HTTPS needs certFile/keyFile, or reverseProxyTls=true for an existing TLS proxy.')
    with server_cls((u['bind'], u['port']), handler) as httpd:
        if cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            key = local_path(root, u['keyFile']) if u.get('keyFile') else None
            ctx.load_cert_chain(local_path(root, cert), key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print(f"Serving updates on {u['bind']}:{u['port']}; Ctrl+C to stop.", flush=True)
        httpd.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description='SCEX updater: detect, configure, publish and serve on Windows/Linux.')
    parser.add_argument('command', choices=('doctor', 'init', 'publish', 'serve'))
    parser.add_argument('--config')
    parser.add_argument('--source-client')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--bind')
    parser.add_argument('--version')
    args = parser.parse_args(argv)
    try:
        overrides = {'sourceClient': args.source_client, 'update.host': args.host,
                     'update.port': args.port, 'update.bind': args.bind, 'version': args.version}
        c, path, origins = load_config(ROOT, args.config, overrides, allow_new_config=args.command == 'init')
        resolved, source, target, _ = resolve_config(ROOT, c, require_source=args.command != 'serve')
        metadata = client_metadata(source) if args.command != 'serve' else {}
        for key, value in metadata.items():
            if not c.get(key):
                c[key] = value
                resolved[key] = value
        if args.command == 'doctor':
            warnings = []
            if resolved['update']['host'] in ('127.0.0.1', 'localhost', '::1'):
                warnings.append('Player URL is local-only. Set --host / PORTABLE_UPDATE_HOST for remote players.')
            if resolved['update']['bind'] in ('127.0.0.1', '::1'):
                warnings.append('Download service binds locally; use a reverse proxy or --bind 0.0.0.0 / ::.')
            print(json.dumps({'os': platform.system(), 'python': platform.python_version(),
                'interpreter': sys.executable, 'sourceClient': str(source), 'publishDir': str(target),
                'configExists': path.exists(), 'overrides': origins, 'clientMetadata': metadata,
                'warnings': warnings}, ensure_ascii=False, indent=2))
        elif args.command == 'init':
            if path.exists():
                raise ValueError('Configuration already exists; edit it or use environment/CLI overrides.')
            no_links(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as f:
                json.dump(resolved, f, ensure_ascii=False, indent=2)
                f.write('\n')
            print('Configuration created: ' + str(path))
        elif args.command == 'publish':
            print(json.dumps(publish(ROOT, c), ensure_ascii=False, indent=2))
        else:
            serve(ROOT, c)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('Updater: ' + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
