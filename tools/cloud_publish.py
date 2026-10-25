#!/usr/bin/env python3
"""Publish an existing portable pack to S3 without operating Minecraft.

Only manifest-listed files and updater bootstrap helpers are uploaded. Content
is immutable; the live manifest is written last. No automatic remote deletion.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from portable_server import no_links, publish_lock

IMMUTABLE = 'public, max-age=31536000, immutable'
LIVE = 'no-cache, no-store, must-revalidate'
ENGINES = ('player-update-generic.py', 'player-update-generic.ps1',
           'portable-stage-daemon.py', 'portable-stage-daemon.ps1')
HELPERS = {name: name for name in ENGINES}
HELPERS.update({'portable-bootstrap-refresh.ps1': 'portable-bootstrap-refresh.ps1',
                'portable-windows-repair.ps1': 'portable-windows-repair.ps1',
                'Windows-sync.bat': 'portable-windows-sync.bat',
                'macOS-sync.command': 'portable-macos-sync.command'})
URL_FILES = {'UPDATE-URL.txt', 'PORTABLE-UPDATE-URL.txt', 'UPDATE-URL-LAN.txt',
             'TFCR-update-url.txt', '_updater/UPDATE-URL.txt',
             '_updater/PORTABLE-UPDATE-URL.txt'}


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Invalid relative path')
    parts = value.split('/')
    if any(p in ('', '.', '..') or ':' in p or p.endswith((' ', '.')) for p in parts):
        raise ValueError('Invalid relative path')
    if any(ord(c) < 32 for c in value) or any(c in value for c in '*?<>|"'):
        raise ValueError('Invalid relative path')
    return value


def checked_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Cloud URLs require HTTPS without credentials/query/fragment')
    return value.rstrip('/')


def hash_file(path):
    no_links(path)
    sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha1.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return sha1.hexdigest(), sha256.hexdigest(), size


def prepare(source, public_base, updater_dir=None, mirror_urls=None,
            manifest_bytes=None, file_overrides=None):
    """Validate all inputs before any write. Never modify the prepared pack."""
    source = Path(source).absolute()
    no_links(source)
    manifest_path = source / 'server-manifest.json'
    no_links(manifest_path)
    original_manifest = manifest_path.read_bytes() if manifest_bytes is None else manifest_bytes
    manifest = json.loads(original_manifest.decode('utf-8-sig'))
    if not manifest.get('files') or not manifest.get('version') or not manifest.get('packId'):
        raise ValueError('Expected a prepared portable manifest with packId/version/files')
    base = checked_url(public_base)
    manifest = copy.deepcopy(manifest)
    if manifest.get('signature'):
        raise ValueError('Signed input requires a compatible re-signing workflow')
    manifest['updateUrl'] = base + '/server-manifest.json'
    manifest['cloudFormat'] = 1
    if mirror_urls is not None:
        from urllib.parse import urlsplit
        mirrors = []
        for url in mirror_urls:
            u = urlsplit(url)
            if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError('Invalid mirror manifest URL')
            if url not in mirrors and url != manifest['updateUrl']:
                mirrors.append(url)
        manifest['mirrorManifestUrls'] = mirrors[:3]
    inputs, blobs, seen = {}, {}, set()
    rows = manifest['files']
    for row in rows:
        rel = relative(row['path'])
        if rel.casefold() in seen or rel == 'server-manifest.json':
            raise ValueError('Duplicate or recursive manifest path')
        seen.add(rel.casefold())
        path = Path((file_overrides or {}).get(rel, source / rel))
        actual_sha1, _, actual_size = hash_file(path)
        if actual_sha1 != row['sha1'].lower() or actual_size != row['size']:
            raise ValueError('Source differs from prepared manifest: ' + rel)
        inputs[rel] = path
    if updater_dir is not None:
        for name, source_name in HELPERS.items():
            path = Path(updater_dir) / source_name
            no_links(path)
            if name in ENGINES and 'downloadPath' not in path.read_text(encoding='utf-8-sig'):
                raise ValueError('Updater engine lacks cloud support: ' + name)
            rel = '_updater/' + name
            if rel not in inputs:
                if rel.casefold() in seen:
                    raise ValueError('Updater path case collision')
                seen.add(rel.casefold())
                rows.append({'path': rel})
            inputs[rel] = path.read_bytes()
    bootstrap = {}
    for row in rows:
        rel = row['path']
        item = inputs[rel]
        if rel in URL_FILES:
            item = ('\n'.join([manifest['updateUrl']] + manifest.get('mirrorManifestUrls', [])) + '\n').encode('utf-8')
        if isinstance(item, bytes):
            sha1, sha256, size = hashlib.sha1(item).hexdigest(), hashlib.sha256(item).hexdigest(), len(item)
        else:
            sha1, sha256, size = hash_file(item)
        row.update(sha1=sha1, sha256=sha256, size=size, downloadPath='blobs/' + sha256)
        # A replacement helper or rewritten URL no longer matches an upstream URL.
        if updater_dir is not None and rel in {'_updater/' + n for n in HELPERS} or rel in URL_FILES:
            for field in ('url', 'urls', 'downloads', 'downloadUrl', 'downloadUrls', 'officialUrls'):
                row.pop(field, None)
        blobs.setdefault(row['downloadPath'], {'source': item, 'sha256': sha256, 'size': size})
        if rel.startswith('_updater/') or rel in URL_FILES:
            bootstrap[rel] = row['downloadPath']
    return {'manifest': manifest, 'blobs': blobs, 'bootstrap': bootstrap,
            'inputManifestSha256': hashlib.sha256(original_manifest).hexdigest()}


def prepare_release_plan(directory, source, public_base, updater_dir=None, mirror_urls=None):
    """Overlay a verified release plan in memory; neither live source is written."""
    directory, source = Path(directory), Path(source).absolute()
    for name in ('release-plan.json', 'manifest.proposed.json'):
        no_links(directory / name)
    release = json.loads((directory / 'release-plan.json').read_text(encoding='utf-8-sig'))
    if Path(release['baseline_root']).resolve() != source.resolve():
        raise ValueError('Release plan baseline root differs from source')
    baseline_path = source / 'server-manifest.json'
    if hash_file(baseline_path)[1] != release['baseline_manifest_sha256'].lower():
        raise ValueError('Release plan baseline changed; regenerate before staging')
    baseline = json.loads(baseline_path.read_text(encoding='utf-8-sig'))
    expected = {r['path']: (r['sha1'].lower(), r['size']) for r in baseline['files']}
    for row in release['remove_paths']:
        rel = relative(row['path'])
        if hash_file(source / rel)[1] != row['sha256'].lower():
            raise ValueError('Release predecessor changed')
        del expected[rel]
    overrides = {}
    for row in release['files']:
        rel = relative(row['path'])
        digest1, digest256, size = hash_file(Path(row['source']))
        if digest256 != row['sha256'].lower() or size != row['size']:
            raise ValueError('Release candidate changed')
        overrides[rel] = Path(row['source'])
        expected[rel] = (digest1, size)
    raw = (directory / 'manifest.proposed.json').read_bytes()
    proposed = json.loads(raw.decode('utf-8-sig'))
    actual = {r['path']: (r['sha1'].lower(), r['size']) for r in proposed['files']}
    if (actual != expected or len(actual) != len(proposed['files'])
            or proposed['version'] != release['version']
            or proposed['packId'] != baseline['packId']):
        raise ValueError('Proposed manifest differs from release plan')
    return prepare(source, public_base, updater_dir, mirror_urls, raw, overrides)


def contract_digest(plan):
    """Bind content and player policy; timestamps and per-file route hints may vary."""
    contract = copy.deepcopy(plan['manifest'])
    contract.pop('generatedAt', None)
    for row in contract['files']:
        for field in ('url', 'urls', 'downloads', 'downloadUrl', 'downloadUrls', 'officialUrls'):
            row.pop(field, None)
    contract['files'].sort(key=lambda row: row['path'])
    for key in ('forceDeleteGlobs', 'forceSyncGlobs'):
        if key in contract:
            contract[key] = sorted(set(contract[key]))
    return hashlib.sha256(encode(contract)).hexdigest()


def bridge_source(plan, target, backup_root):
    """Keep the old direct-layout endpoint as a verified migration bridge."""
    target, backup_root = Path(target).absolute(), Path(backup_root).absolute()
    no_links(target)
    no_links(backup_root)
    manifest_path = target / 'server-manifest.json'
    if hash_file(manifest_path)[1] != plan['inputManifestSha256']:
        raise ValueError('Origin manifest changed during cloud publication')
    # Revalidate the origin before changing it; another release may have changed files.
    prepare(target, plan['manifest']['updateUrl'].rsplit('/', 1)[0])
    replacements = {}
    for rel, key in plan['bootstrap'].items():
        blob = plan['blobs'][key]
        current = target / rel
        if not current.exists() or hash_file(current)[1] != blob['sha256']:
            item = blob['source']
            data = item if isinstance(item, bytes) else item.read_bytes()
            if hashlib.sha256(data).hexdigest() != blob['sha256']:
                raise ValueError('Bridge source changed after verification')
            replacements[rel] = data
    direct = copy.deepcopy(plan['manifest'])
    direct.pop('cloudFormat', None)
    for row in direct['files']:
        row.pop('downloadPath', None)
    replacements['server-manifest.json'] = encode(direct) + b'\n'
    backup = backup_root / ('cloud-bridge-' + uuid.uuid4().hex)
    backup.mkdir(parents=True)
    previous = {}
    for rel in replacements:
        source = target / relative(rel)
        no_links(source)
        previous[rel] = source.read_bytes() if source.is_file() else None
        if previous[rel] is not None:
            saved = backup / rel
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(previous[rel])
    (backup / 'rollback.json').write_bytes(encode({'target': str(target),
        'files': [{'path': rel, 'existed': data is not None,
                   'sha256': hashlib.sha256(data).hexdigest() if data is not None else None}
                  for rel, data in previous.items()]}))
    changed = []
    def replace(rel, data):
        output = target / rel
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name('.' + output.name + '.' + uuid.uuid4().hex + '.next')
        try:
            temp.write_bytes(data)
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
    try:
        # Refresh engines before handing old clients their new URL; manifest last.
        ordered = sorted(replacements, key=lambda rel: 2 if rel == 'server-manifest.json' else 1 if rel in URL_FILES else 0)
        for rel in ordered:
            data = replacements[rel]
            replace(rel, data)
            changed.append(rel)
        prepare(target, direct['updateUrl'].rsplit('/', 1)[0])
    except Exception:
        for rel in reversed(changed):
            if previous[rel] is None:
                (target / rel).unlink(missing_ok=True)
            else:
                replace(rel, previous[rel])
        raise
    return {'backup': str(backup), 'changedFiles': list(replacements),
            'manifestSha256': hash_file(manifest_path)[1]}


def stage(plan, store, progress=None):
    """Upload and fully read back immutable objects, without changing live endpoints."""
    manifest = plan['manifest']
    previous = store.head('server-manifest.json')
    previous_etag = previous.get('ETag') if previous else None
    uploaded = reused = 0
    verified = {}
    for key, blob in plan['blobs'].items():
        old = store.head(key)
        if old:
            recorded_hash = old.get('Metadata', {}).get('sha256')
            if (old.get('ContentLength') != blob['size']
                    or recorded_hash is not None and recorded_hash != blob['sha256']):
                raise ValueError('Existing immutable object has unexpected identity: ' + key)
            reused += 1
        else:
            store.put(key, blob['source'], IMMUTABLE, blob['sha256'])
            uploaded += 1
        identity = store.head(key)
        if not identity or not identity.get('ETag'):
            raise ValueError('Provider must return an object identity for staged activation')
        # Public readback validates actual bytes, including pre-existing objects.
        store.verify(key, blob['sha256'], blob['size'])
        verified[key] = {'etag': identity['ETag'], 'size': blob['size'], 'sha256': blob['sha256']}
        if progress:
            progress(uploaded + reused, len(plan['blobs']))
    raw = encode(manifest)
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = 'manifests/' + digest + '.json'
    store.put(snapshot, raw, IMMUTABLE, digest)
    store.verify(snapshot, digest, len(raw))
    latest = store.head('server-manifest.json')
    if (latest.get('ETag') if latest else None) != previous_etag:
        raise ValueError('Live manifest changed while staging; regenerate before maintenance')
    return {'status': 'STAGED_NOT_ACTIVATED', 'schema': 1,
            'storeId': getattr(store, 'identity', None),
            'version': manifest['version'], 'packId': manifest['packId'],
            'contractSha256': contract_digest(plan), 'previousETag': previous_etag,
            'manifestSha256': digest, 'manifestSize': len(raw), 'snapshot': snapshot,
            'uploaded': uploaded, 'reused': reused, 'verifiedBlobs': verified,
            'files': len(manifest['files']), 'remoteDeleted': 0}


def check_staged(plan, store, receipt):
    """Recheck fresh remote identities against full readback evidence, without writes."""
    if (receipt.get('status') != 'STAGED_NOT_ACTIVATED' or receipt.get('schema') != 1
            or receipt.get('storeId') != getattr(store, 'identity', None)
            or receipt.get('contractSha256') != contract_digest(plan)):
        raise ValueError('Staged receipt does not match this candidate/store/policy')
    previous = store.head('server-manifest.json')
    if (previous.get('ETag') if previous else None) != receipt['previousETag']:
        raise ValueError('Live manifest changed since staging; activation cancelled')
    recorded = receipt['verifiedBlobs']
    if set(recorded) != set(plan['blobs']):
        raise ValueError('Staged blob inventory differs')
    if hasattr(store, 'refresh_inventory'):
        store.refresh_inventory()
    for key, blob in plan['blobs'].items():
        old = recorded[key]
        current = store.head(key)
        if (not current or not old.get('etag') or current.get('ETag') != old['etag']
                or current.get('ContentLength') != blob['size'] or old['size'] != blob['size']
                or old['sha256'] != blob['sha256']):
            raise ValueError('Staged cloud object changed or disappeared: ' + key)
    if receipt['snapshot'] != 'manifests/' + receipt['manifestSha256'] + '.json':
        raise ValueError('Invalid staged snapshot identity')
    store.verify(receipt['snapshot'], receipt['manifestSha256'], receipt['manifestSize'])
    return {'status': 'STAGED_CHECK_PASS', 'version': plan['manifest']['version'],
            'files': len(plan['manifest']['files'])}


def activate_staged(plan, store, receipt):
    """Only activate a frozen, fully verified candidate; do not reread every blob."""
    check_staged(plan, store, receipt)
    manifest = plan['manifest']
    raw = encode(manifest)
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = 'manifests/' + digest + '.json'
    if snapshot != receipt['snapshot']:
        store.put(snapshot, raw, IMMUTABLE, digest)
        store.verify(snapshot, digest, len(raw))
    # Bootstrap refresh runs before manifest parsing in existing players.
    for rel, blob_key in plan['bootstrap'].items():
        blob = plan['blobs'][blob_key]
        store.put(rel, blob['source'], LIVE, blob['sha256'])
        store.verify(rel, blob['sha256'], blob['size'])
    latest = store.head('server-manifest.json')
    if (latest.get('ETag') if latest else None) != receipt['previousETag']:
        raise ValueError('Another publisher changed the cloud manifest; switch cancelled')
    store.activate(raw, digest, receipt['previousETag'])
    # If this fails, the manifest may already be live: inspect before retrying.
    store.verify('server-manifest.json', digest, len(raw))
    return {'status': 'PUBLISHED_AND_READ_BACK', 'version': manifest['version'],
            'manifestSha256': digest, 'snapshot': snapshot, 'uploaded': receipt['uploaded'],
            'reused': receipt['reused'], 'files': len(manifest['files']), 'remoteDeleted': 0,
            'activationMode': 'PREVERIFIED_STAGED_OBJECTS'}


def publish(plan, store, progress=None):
    """Compatibility path: stage and activate in one invocation."""
    return activate_staged(plan, store, stage(plan, store, progress))


class S3Store:
    def __init__(self, cfg):
        import boto3
        import urllib3
        from botocore.config import Config
        self.prefix = relative(cfg['prefix']).rstrip('/')
        self.bucket = cfg['bucket']
        self.base = checked_url(cfg['publicBaseUrl'])
        endpoint = checked_url(cfg['endpointUrl'])
        self.identity = hashlib.sha256(encode({key: cfg[key] for key in
            ('bucket', 'prefix', 'publicBaseUrl', 'endpointUrl', 'region')})).hexdigest()
        key = os.environ[cfg.get('accessKeyEnv', 'SCEX_CLOUD_ACCESS_KEY')]
        secret = os.environ[cfg.get('secretKeyEnv', 'SCEX_CLOUD_SECRET_KEY')]
        self.api = boto3.client('s3', endpoint_url=endpoint,
                               region_name=cfg['region'], aws_access_key_id=key,
                               aws_secret_access_key=secret,
                               config=Config(s3={'addressing_style': 'path'}, proxies={},
                                             connect_timeout=15, read_timeout=60,
                                             retries={'max_attempts': 3, 'mode': 'standard'}))
        # Reuse direct HTTPS connections for serial full-byte readback.
        self.http = urllib3.PoolManager(cert_reqs='CERT_REQUIRED', maxsize=1)
        self.blob_inventory = None

    def refresh_inventory(self):
        self.blob_inventory = None

    def head(self, key):
        if key.startswith('blobs/'):
            # LIST provides sizes in pages, avoiding one serial HEAD per small file.
            # Full public SHA-256 readback still verifies every reused object.
            if self.blob_inventory is None:
                self.blob_inventory = {}
                args = {'Bucket': self.bucket, 'Prefix': self.prefix + '/blobs/'}
                while True:
                    page = self.api.list_objects_v2(**args)
                    for item in page.get('Contents', []):
                        rel = item['Key'][len(self.prefix) + 1:]
                        self.blob_inventory[rel] = {'ContentLength': item['Size'], 'ETag': item['ETag']}
                    if not page.get('IsTruncated'):
                        break
                    args['ContinuationToken'] = page['NextContinuationToken']
            return self.blob_inventory.get(key)
        try:
            return self.api.head_object(Bucket=self.bucket, Key=self.prefix + '/' + key)
        except self.api.exceptions.ClientError as exc:
            if exc.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
                return None
            raise

    def put(self, key, source, cache, digest):
        args = {'Bucket': self.bucket, 'Key': self.prefix + '/' + key,
                'CacheControl': cache, 'Metadata': {'sha256': digest},
                'ContentType': 'application/json; charset=utf-8' if key.endswith('.json') else 'application/octet-stream'}
        # File streams trigger S3 Expect: 100-continue even for tiny files.
        # Bound the buffer to avoid thousands of unnecessary handshake waits.
        if not isinstance(source, bytes) and source.stat().st_size <= 256 * 1024:
            source = source.read_bytes()
        if isinstance(source, bytes):
            response = self.api.put_object(Body=source, **args)
            size = len(source)
        else:
            with source.open('rb') as stream:
                response = self.api.put_object(Body=stream, **args)
            size = source.stat().st_size
        if key.startswith('blobs/') and self.blob_inventory is not None:
            self.blob_inventory[key] = {'ContentLength': size, 'ETag': response.get('ETag')}

    def activate(self, raw, digest, previous_etag):
        # Conditional writes must be supported by the provider. Never silently
        # fall back to unconditional overwrite when another machine can publish.
        condition = {'IfMatch': previous_etag} if previous_etag else {'IfNoneMatch': '*'}
        self.api.put_object(Bucket=self.bucket, Key=self.prefix + '/server-manifest.json',
                            Body=raw, CacheControl=LIVE, ContentType='application/json; charset=utf-8',
                            Metadata={'sha256': digest}, **condition)

    def verify(self, key, digest, size):
        url = self.base + '/' + '/'.join(quote(p, safe='') for p in key.split('/'))
        # No secret is sent to the public CDN. Bytes are verified, not the ETag.
        import urllib3
        for attempt in range(3):
            total, sha, response = 0, hashlib.sha256(), None
            try:
                response = self.http.request('GET', url, preload_content=False, retries=False,
                    timeout=urllib3.Timeout(connect=15, read=60),
                    headers={'Cache-Control': 'no-cache', 'User-Agent': 'SCEX-Cloud-Publisher/1'})
                if response.status != 200:
                    raise ValueError('Public object HTTP status ' + str(response.status))
                for chunk in response.stream(1024 * 1024):
                    total += len(chunk)
                    if total > size:
                        raise ValueError('Public object is larger than expected')
                    sha.update(chunk)
                if total != size or sha.hexdigest() != digest:
                    raise ValueError('Public object readback differs: ' + key)
                return
            except (urllib3.exceptions.HTTPError, OSError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Upload and switch live manifest; default is local plan only')
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--bridge-source', type=Path, help='Update this old direct-layout source after cloud readback')
    parser.add_argument('--backup-root', type=Path, help='Required for --bridge-source')
    parser.add_argument('--lock-root', type=Path, help='Native publisher root to serialize with local releases')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--stage-only', action='store_true', help='Upload/read back immutable files; leave players on current release')
    mode.add_argument('--activate-staged', type=Path, help='Activate using a matching stage receipt')
    mode.add_argument('--check-staged', type=Path, help='Read-only candidate and cloud identity preflight')
    parser.add_argument('--release-plan', type=Path, help='Offline plan directory; overlay candidate without copying the live pack')
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding='utf-8-sig'))
    relative(cfg['prefix'])
    checked_url(cfg['endpointUrl'])
    if args.bridge_source and (not args.apply or not args.backup_root):
        parser.error('--bridge-source requires --apply and --backup-root')
    if args.stage_only and (not args.apply or not args.receipt or args.bridge_source):
        parser.error('--stage-only requires --apply and --receipt, and forbids --bridge-source')
    if args.activate_staged and (not args.apply or args.release_plan):
        parser.error('--activate-staged requires --apply and the final prepared source')
    if args.check_staged and args.bridge_source:
        parser.error('--check-staged forbids --bridge-source')
    if args.release_plan and not (args.stage_only or args.check_staged or not args.apply):
        parser.error('--release-plan cannot directly activate a live release')
    with publish_lock(args.lock_root or Path(__file__).resolve().parents[1]):
        if args.release_plan:
            plan = prepare_release_plan(args.release_plan, args.source, cfg['publicBaseUrl'],
                                        Path(__file__).parent, cfg.get('mirrorManifestUrls'))
        else:
            plan = prepare(args.source, cfg['publicBaseUrl'], Path(__file__).parent, cfg.get('mirrorManifestUrls'))
        receipt_path = args.activate_staged or args.check_staged
        staged = None
        if receipt_path:
            no_links(receipt_path)
            staged = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
        if args.check_staged:
            result = check_staged(plan, S3Store(cfg), staged)
        elif not args.apply:
            result = {'status': 'LOCAL_PLAN_ONLY', 'files': len(plan['manifest']['files']),
                      'uniqueBlobs': len(plan['blobs']),
                      'uniqueBytes': sum(b['size'] for b in plan['blobs'].values()),
                      'remoteDeleted': 0}
        else:
            def report_progress(done, total):
                if done == 1 or done % 25 == 0 or done == total:
                    print(json.dumps({'verifiedBlobs': done, 'totalBlobs': total}), flush=True)
            store = S3Store(cfg)
            if args.stage_only:
                result = stage(plan, store, report_progress)
            elif args.activate_staged:
                result = activate_staged(plan, store, staged)
            else:
                result = publish(plan, store, report_progress)
            if args.bridge_source:
                result['bridge'] = bridge_source(plan, args.bridge_source, args.backup_root)
        if args.receipt:
            no_links(args.receipt)
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_bytes(encode(result) + b'\n')
        print(json.dumps({k: v for k, v in result.items() if k != 'verifiedBlobs'}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # SDK/network exceptions may contain endpoint credentials or signed URLs.
        print('Cloud publication failed (' + type(exc).__name__ + '). '
              'Live manifest may already be switched if its final readback failed; '
              'inspect remote state before retrying. Credentials are not printed.', file=sys.stderr)
        sys.exit(1)
