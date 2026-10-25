#!/usr/bin/env python3
"""Summarise the download service's manifest request log (UPDATE_ADOPTION_V1): how many
distinct players fetched the manifest per day, by updater version and component, the last
version and component each visitor used, and which visitors need attention.

    python3 tools/update-adoption.py [--days 7] [--folder logs/update-access] [--public modpack-public/portable]

The current updater version is the UPDATER_VERSION of the published engine; content
releases change the manifest version without changing the updater. Updaters before
2.27.24 sent "portable-server-kit-*" User-Agents and are counted as "2.27.23 或更早".
A visitor id is valid for one day only (see secure-update-server.py), so the same player
shows up with a new id every day. Addresses that ran the release tool's checks that day
are listed apart and not counted as players. Read-only."""
import argparse
import collections
import json
import os
import re

UA_RE = re.compile(r"SCEX-updater/(?P<version>[\w.]+) \((?P<component>[\w-]+)")
RELEASE_CHECK_PREFIX = "SCEX-release-verify/"
OLD = "2.27.23 或更早"
# 2.27.24 remembers that the manifest must be signed; signatures were removed in 2.27.25.
SIGNATURE_STICKY = {"2.27.24"}
OK_STATUS = {"200", "206", "304"}


def classify(agent):
    match = UA_RE.match(agent or "")
    if match:
        return match.group("version"), match.group("component")
    if (agent or "").startswith("portable-server-kit") or (agent or "").startswith("portable-windows-repair"):
        return OLD, (agent or "").split("/", 1)[0].replace("portable-server-kit-", "")
    return "其他", (agent or "-")[:24]


def version_key(version):
    return tuple(int(p) for p in version.split(".")) if re.fullmatch(r"\d+(\.\d+)+", version or "") else None


def read_day(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 4:
                rows.append(parts)
    return rows


def manifest_version(manifest_path):
    try:
        with open(manifest_path, encoding="utf-8-sig") as handle:
            return str(json.load(handle).get("version") or "") or None
    except (OSError, ValueError):
        return None


def updater_version(engine_path):
    """UPDATER_VERSION of the published engine: what an up-to-date player sends."""
    try:
        with open(engine_path, encoding="utf-8-sig") as handle:
            match = re.search(r'^UPDATER_VERSION = "([\d.]+)"', handle.read(), re.M)
        return match.group(1) if match else None
    except OSError:
        return None


def summarise_day(day, rows, current):
    """Printable lines for one day, and the attention items."""
    checkers = {visitor for _t, _s, visitor, agent in rows if agent.startswith(RELEASE_CHECK_PREFIX)}
    players = collections.OrderedDict()
    for time, status, visitor, agent in rows:
        if visitor not in checkers:
            players.setdefault(visitor, []).append((time, status) + classify(agent))
    versions = collections.defaultdict(set)
    components = collections.defaultdict(set)
    for visitor, seen in players.items():
        for _time, _status, version, component in seen:
            versions[version].add(visitor)
            components[(version, component)].add(visitor)
    title = f"{day[:4]}-{day[4:6]}-{day[6:]}：{len(players)} 位玩家取过清单"
    if version_key(current):
        latest = [v for v, seen in players.items()
                  if max((version_key(s[2]) or () for s in seen), default=()) >= version_key(current)]
        title += f"，{len(latest)} 位已用更新器 {current}"
    if checkers:
        title += f"；另有 {len(checkers)} 个发布验收地址（不计入）"
    lines = [title]
    for version in sorted(versions, key=lambda v: (-len(versions[v]), v)):
        detail = "，".join(f"{c} {len(s)}" for (v, c), s in sorted(components.items()) if v == version)
        lines.append(f"  {version}：{len(versions[version])}（{detail}）")
    attention = []
    if players:
        lines.append("  每位访客最后一次（编号只在当天有效）：")
    for visitor, seen in sorted(players.items(), key=lambda item: item[1][-1][0]):
        time, status, version, component = seen[-1]
        others = sorted({f"{v} {c}" for _t, _s, v, c in seen[:-1]} - {f"{version} {component}"})
        extra = "；当天还用过 " + "、".join(others) if others else ""
        lines.append(f"    {visitor}  {time[11:19]}  {version} {component}  HTTP {status}{extra}")
        when = f"{day[:4]}-{day[4:6]}-{day[6:]} {time[11:19]} 访客 {visitor}"
        if version in SIGNATURE_STICKY:
            attention.append(f"{when}：最后用的是 {version}（{component}）。这个版本会要求清单带签名，服务器已不再提供签名；"
                             "如果他更新失败（签名校验失败），请让他删除实例目录里的 .portable-sync-state.json 后重新运行更新器。")
        elif status not in OK_STATUS:
            attention.append(f"{when}：最后一次取清单返回 HTTP {status}（{version} {component}）。")
    return lines, attention


def main():
    tools = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder", default=os.path.join(tools, os.pardir, "logs", "update-access"))
    public = os.path.join(tools, os.pardir, "modpack-public", "portable")
    parser.add_argument("--public", default=public,
                        help="published folder: _updater/player-update-generic.py gives the current updater version")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    try:
        names = sorted(name for name in os.listdir(args.folder) if re.fullmatch(r"update-access-\d{8}\.log", name))
    except OSError:
        print("还没有访问记录：" + os.path.abspath(args.folder))
        return 1
    current = updater_version(os.path.join(args.public, "_updater", "player-update-generic.py"))
    pack = manifest_version(os.path.join(args.public, "server-manifest.json"))
    if current:
        print(f"当前更新器版本：{current}" + (f"（整合包清单版本 {pack}）" if pack else ""))
    attention = []
    for name in names[-args.days:]:
        lines, items = summarise_day(name[14:22], read_day(os.path.join(args.folder, name)), current)
        print("\n".join(lines))
        attention.extend(items)
    print("需要关注：" + ("" if attention else "无"))
    for item in attention:
        print("  " + item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
