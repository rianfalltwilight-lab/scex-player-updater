"""Resolve exact published files to official CDNs once, before publishing a manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import urllib.parse
import urllib.request

UA = 'SCEX-updater/2026.10.07 (exact-file source index)'
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request_json(url, body, key=''):
    headers = {'User-Agent': UA, 'Content-Type': 'application/json'}
    if key:
        headers['x-api-key'] = key
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    openers = [DIRECT]
    if urllib.request.getproxies():
        openers.append(urllib.request.build_opener())
    for opener in openers:
        try:
            with opener.open(request, timeout=20) as response:
                return json.load(response)
        except Exception:
            pass
    # Never include request headers, keys or private paths in output.
    raise RuntimeError(urllib.parse.urlsplit(url).hostname + ' metadata unavailable')


def fingerprint(data):
    """CurseForge MurmurHash2 fingerprint excludes ASCII whitespace bytes."""
    data = data.translate(None, b'\t\n\r ')
    m = 0x5bd1e995
    h = (1 ^ len(data)) & 0xffffffff
    end = len(data) // 4 * 4
    for (k,) in struct.iter_unpack('<I', data[:end]):
        k = k * m & 0xffffffff
        k ^= k >> 24
        k = k * m & 0xffffffff
        h = (h * m & 0xffffffff) ^ k
    for i, value in enumerate(data[end:]):
        h ^= value << (8 * i)
    if end != len(data):
        h = h * m & 0xffffffff
    h ^= h >> 13
    h = h * m & 0xffffffff
    return (h ^ h >> 15) & 0xffffffff


def provider(url):
    p = urllib.parse.urlsplit(str(url))
    if p.scheme != 'https' or p.username or p.password:
        return None
    host = (p.hostname or '').lower()
    if host == 'cdn.modrinth.com':
        return 'modrinth'
    if host.endswith('.forgecdn.net'):
        return 'curseforge'
    return None


def enrich(manifest, directory, cache, key=''):
    entries = cache.setdefault('files', {})
    eligible = [r for r in manifest['files'] if
                (r['path'].startswith('mods/') and r['path'].endswith('.jar')) or
                (r['path'].startswith(('resourcepacks/', 'shaderpacks/')) and r['path'].endswith('.zip'))]
    for row in eligible:
        sources = entries.setdefault(row['sha1'], {})
        for url in list(row.get('downloads') or []) + [row.get('url', '')]:
            name = provider(url)
            if name:
                sources[name] = url
    unknown = [r['sha1'] for r in eligible if not entries[r['sha1']].get('modrinth')]
    if unknown:
        try:
            for offset in range(0, len(unknown), 500):
                response = request_json('https://api.modrinth.com/v2/version_files',
                                        {'hashes': unknown[offset:offset+500], 'algorithm': 'sha1'})
                for sha, version in response.items():
                    for file in version.get('files', []):
                        if file.get('hashes', {}).get('sha1', '').lower() == sha.lower() and provider(file.get('url')) == 'modrinth':
                            entries[sha]['modrinth'] = file['url']
                            break
        except RuntimeError as exc:
            print(str(exc) + '; cached sources and server fallback retained')
    cf_status = 'key_required'
    if key:
        try:
            by_fp = {}
            for row in eligible:
                entry = entries[row['sha1']]
                if entry.get('curseforge'):
                    continue
                if 'fingerprint' not in entry:
                    data = (directory / row['path']).read_bytes()
                    if hashlib.sha1(data).hexdigest() != row['sha1']:
                        raise ValueError('Published file changed during source indexing')
                    entry['fingerprint'] = fingerprint(data)
                by_fp.setdefault(entry['fingerprint'], []).append(row)
            fps = list(by_fp)
            for offset in range(0, len(fps), 500):
                response = request_json('https://api.curseforge.com/v1/fingerprints/432',
                                        {'fingerprints': fps[offset:offset+500]}, key)
                for match in response.get('data', {}).get('exactMatches', []):
                    file = match['file']
                    sha = next((h['value'].lower() for h in file.get('hashes', []) if h['algo'] == 1), '')
                    url = file.get('downloadUrl')
                    # Fingerprints identify candidates, SHA-1 establishes exact-byte identity.
                    if sha in entries and provider(url) == 'curseforge' and any(r['sha1'] == sha for r in eligible):
                        entries[sha]['curseforge'] = url
            cf_status = 'configured'
        except RuntimeError as exc:
            cf_status = 'metadata_unavailable'
            print(str(exc) + '; cached sources and server fallback retained')
    counts = {'curseforge': 0, 'modrinth': 0, 'official_unique': 0}
    for row in eligible:
        entry = entries[row['sha1']]
        urls = [entry[p] for p in ('curseforge', 'modrinth') if provider(entry.get(p, '')) == p]
        if urls:
            row['downloads'] = urls
            row['url'] = urls[0]
            counts['official_unique'] += 1
        for name in ('curseforge', 'modrinth'):
            counts[name] += int(name in entry)
    manifest['downloadWorkers'] = max(1, min(8, int(manifest.get('downloadWorkers', 4))))
    manifest['preferredDownloadSources'] = ['curseforge', 'modrinth', 'server']
    manifest['downloadSourceStatus'] = {'curseforgeStatus': cf_status, **counts}
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--curseforge-key-file', type=Path)
    args = parser.parse_args()
    key = os.environ.get('CURSEFORGE_API_KEY', '').strip()
    if args.curseforge_key_file:
        key = args.curseforge_key_file.read_text('utf-8-sig').strip()
    manifest = json.loads(args.manifest.read_text('utf-8-sig'))
    cache = json.loads(args.cache.read_text('utf-8-sig')) if args.cache.exists() else {}
    counts = enrich(manifest, args.directory, cache, key)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    args.cache.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'matched': counts, 'curseforge_status': manifest['downloadSourceStatus']['curseforgeStatus']}))


if __name__ == '__main__':
    main()
