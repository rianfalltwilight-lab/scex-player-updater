#!/usr/bin/env python3
"""Summarise the download service's manifest request log (UPDATE_ADOPTION_V1): how many
distinct players fetched the manifest per day, by updater version and component.

    python3 tools/update-adoption.py [--days 7] [--folder logs/update-access]

Updaters before 2.27.24 sent "portable-server-kit-*" User-Agents and are counted as
"2.27.23 或更早". A visitor id is valid for one day only (see secure-update-server.py)."""
import argparse
import collections
import os
import re

UA_RE = re.compile(r"SCEX-updater/(?P<version>[\w.]+) \((?P<component>[\w-]+)")


def classify(agent):
    match = UA_RE.match(agent or "")
    if match:
        return match.group("version"), match.group("component")
    if (agent or "").startswith("portable-server-kit") or (agent or "").startswith("portable-windows-repair"):
        return "2.27.23 或更早", (agent or "").split("/", 1)[0].replace("portable-server-kit-", "")
    return "其他", (agent or "-")[:24]


def main():
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "logs", "update-access")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder", default=default)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    try:
        names = sorted(name for name in os.listdir(args.folder) if re.fullmatch(r"update-access-\d{8}\.log", name))
    except OSError:
        print("还没有访问记录：" + os.path.abspath(args.folder))
        return 1
    for name in names[-args.days:]:
        day = name[14:22]
        versions = collections.defaultdict(set)
        components = collections.defaultdict(set)
        everyone = set()
        with open(os.path.join(args.folder, name), encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 4:
                    continue
                _time, _status, visitor, agent = parts
                version, component = classify(agent)
                everyone.add(visitor)
                versions[version].add(visitor)
                components[(version, component)].add(visitor)
        print(f"{day[:4]}-{day[4:6]}-{day[6:]}：{len(everyone)} 位玩家取过清单")
        for version in sorted(versions, key=lambda v: (-len(versions[v]), v)):
            detail = "，".join(f"{c} {len(s)}" for (v, c), s in sorted(components.items()) if v == version)
            print(f"  {version}：{len(versions[version])}（{detail}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
