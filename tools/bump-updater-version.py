#!/usr/bin/env python3
"""Set the updater version in every engine copy at once (VERSION_BUMP_V1).

    python tools/bump-updater-version.py 2.27.27 [--mac-note "说明"] [--dry-run]
    python tools/bump-updater-version.py --check

Every tools/ file with an `UPDATER_VERSION = "x"` (Python) or `$script:UpdaterVersion = 'x'`
(PowerShell) line is changed; all copies must agree before the change. A changed macOS
.command also gets its `# @version N` raised by one and a history line `# vN+1：...`.
Each file keeps its encoding: UTF-8 BOM, CRLF or LF, exactly as before."""
import argparse
import os
from pathlib import Path
import re
import sys

TOOLS = Path(__file__).resolve().parent
PATTERNS = (re.compile(r'^(UPDATER_VERSION = ")([\d.]+)(")', re.M),
            re.compile(r"^(\$script:UpdaterVersion = ')([\d.]+)(')", re.M))
MAC_VERSION = re.compile(r"^(# @version )(\d+)", re.M)
BOM = b"\xef\xbb\xbf"


def key(version):
    return tuple(int(part) for part in version.split("."))


def load(path):
    raw = path.read_bytes()
    bom = raw.startswith(BOM)
    text = raw[len(BOM):].decode("utf-8") if bom else raw.decode("utf-8")
    return text, bom


def copies(tools):
    """{path: (text, bom, [versions])} for every file that carries an updater version."""
    found = {}
    for path in sorted(tools.iterdir()):
        if path.suffix not in (".py", ".ps1", ".command") or not path.is_file() or path.name == Path(__file__).name:
            continue
        text, bom = load(path)
        versions = [m.group(2) for pattern in PATTERNS for m in pattern.finditer(text)]
        if versions:
            found[path] = (text, bom, versions)
    return found


def bump(tools, new, mac_note=None, dry_run=False, out=print):
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        raise SystemExit(f"版本号格式应为 X.Y.Z：{new}")
    found = copies(tools)
    if not found:
        raise SystemExit("没有找到带更新器版本号的文件：" + str(tools))
    current = {v for _t, _b, versions in found.values() for v in versions}
    if len(current) != 1 or any(len(versions) != 1 for _t, _b, versions in found.values()):
        raise SystemExit("各副本的版本号不一致，先用 --check 查看：" + ", ".join(sorted(current)))
    old = current.pop()
    if key(new) <= key(old):
        raise SystemExit(f"新版本 {new} 必须大于当前版本 {old}")
    plan = []
    for path, (text, bom, _versions) in found.items():
        changed = text
        for pattern in PATTERNS:
            changed = pattern.sub(lambda m: m.group(1) + new + m.group(3), changed)
        note = ""
        if path.suffix == ".command":
            match = MAC_VERSION.search(changed)
            if match:
                number = int(match.group(2)) + 1
                changed = MAC_VERSION.sub(lambda m: m.group(1) + str(number), changed, count=1)
                newline = "\r\n" if "\r\n" in changed else "\n"
                body = changed.rstrip("\r\n")
                changed = body + newline + f"# v{number}：{mac_note or '更新器版本号改为 ' + new + '。'}" + newline
                note = f"（@version {match.group(2)} -> {number}）"
        plan.append((path, changed, bom, note))
    for path, changed, bom, note in plan:
        out(f"{path.name}: {old} -> {new}{note}")
        if dry_run:
            continue
        data = (BOM if bom else b"") + changed.encode("utf-8")
        temp = path.with_name(path.name + ".bump.tmp")
        temp.write_bytes(data)
        os.replace(temp, path)
    out(("将修改" if dry_run else "已修改") + f" {len(plan)} 个文件。")
    return old, [path for path, *_rest in plan]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", nargs="?", help="new version, X.Y.Z")
    parser.add_argument("--mac-note", help="history line for the macOS entry (default: the version change)")
    parser.add_argument("--dry-run", action="store_true", help="only list the changes")
    parser.add_argument("--check", action="store_true", help="list the version in every copy; fail when they differ")
    parser.add_argument("--tools", default=str(TOOLS), help=argparse.SUPPRESS)
    args = parser.parse_args()
    tools = Path(args.tools)
    if args.check:
        found = copies(tools)
        for path, (_t, _b, versions) in found.items():
            print(f"{path.name}: {', '.join(versions)}")
        versions = sorted({v for _t, _b, vs in found.values() for v in vs})
        if len(versions) == 1:
            print(f"{len(found)} 个文件一致：{versions[0]}")
            return 0
        print("不一致：" + (", ".join(versions) or "没有找到版本号"))
        return 1
    if not args.version:
        parser.error("需要新版本号，或使用 --check")
    bump(tools, args.version, args.mac_note, args.dry_run)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
