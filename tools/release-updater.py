#!/usr/bin/env python3
"""Updater release tool (RELEASE_TOOL_V1): ship a new updater version to the live update
servers without a full content publish. Each step can be re-run and it is safe to stop
between steps; nothing is installed before `deploy`.

  preflight  production still serves the previous release (hashes from its receipt)
  backup     copy every file this release touches from each host (never overwrites)
  prepare    per-host payloads and deploy plans; manifest rows; signed manifest
  e2e        local transition test (previous engines and entry -> this release; localhost)
  dryrun     run the generated deploy scripts against copies of the backup
  stage      copy payloads to each host's temp folder and verify every hash
  deploy     install host by host: files, optional service restart, manifest + signature last
  verify     HTTP checks through every address, signature, Range, live bootstrap checks
  finalize   confirm production hashes, remove the stages, write the receipt

  python tools/release-updater.py --config <hosts.json> --release <release.json> <step>

The host config (addresses, users, paths, token and signing-key locations) stays outside
the repository; see docs in the release receipt for the expected keys."""
import argparse
import base64
import copy
import datetime as dt
import functools
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parents[1]
PUBLIC = "modpack-public/portable/"
HELPERS = ["player-update-generic.ps1", "player-update-generic.py", "portable-stage-daemon.ps1", "portable-stage-daemon.py",
           "portable-bootstrap-refresh.ps1", "macOS-sync.command", "Windows-sync.bat"]
PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Release:
    def __init__(self, config_path, release_path):
        self.config = read_json(config_path)
        self.spec = read_json(release_path)
        self.version = self.spec["version"]
        self.previous = self.spec["previousVersion"]
        self.commit = self.spec["commit"]
        self.work = Path(self.spec["workdir"])
        self.out = self.work / "release"
        self.backup = Path(self.config["backupRoot"]) / self.spec["backupName"]
        self.hosts = self.config["hosts"]
        self.order = self.config["order"]
        self.stage_name = "scex-updater-" + self.version.replace(".", "-")
        self.token = Path(self.config["tokenFile"]).read_text(encoding="utf-8-sig").strip()

    # ---- remote helpers ------------------------------------------------------------
    def ssh(self, host, command, stdin=None, timeout=180):
        argv = self.hosts[host]["ssh"] + [command]
        result = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{host}: remote command failed ({result.returncode}): " + result.stderr.decode("utf-8", "replace")[:800])
        return result.stdout.decode("utf-8", "replace").strip()

    def remote_powershell(self, host, script, timeout=180):
        encoded_script = base64.b64encode(("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);" + script).encode("utf-16-le")).decode()
        return self.ssh(host, "powershell -NoProfile -NonInteractive -EncodedCommand " + encoded_script, timeout=timeout)

    def remote_python(self, host, program, timeout=180):
        return self.ssh(host, self.hosts[host]["python"] + " -", stdin=program.encode("utf-8"), timeout=timeout)

    def remote_hashes(self, host, paths):
        root = self.hosts[host]["root"]
        if self.hosts[host]["kind"] == "posix":
            program = ("import hashlib,json,pathlib\nroot=pathlib.Path(%r)\npaths=json.loads(%r)\n"
                       "print(json.dumps({p:(hashlib.sha256((root/p).read_bytes()).hexdigest() if (root/p).is_file() else None) for p in paths}))\n"
                       % (root, json.dumps(paths, ensure_ascii=False)))
            return json.loads(self.remote_python(host, program))
        script = ("$root='" + root + "';$out=@{};$paths=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
                  + base64.b64encode(json.dumps(paths, ensure_ascii=False).encode()).decode() + "'))|ConvertFrom-Json;"
                  "foreach($rel in $paths){$p=Join-Path $root $rel;$out[$rel]=if(Test-Path -LiteralPath $p -PathType Leaf)"
                  "{(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()}else{$null}};$out|ConvertTo-Json -Compress")
        return json.loads(self.remote_powershell(host, script) or "{}")

    def scp_from(self, host, rel, target):
        source = f"{self.hosts[host]['address']}:{self.hosts[host]['root']}/{rel}"
        result = subprocess.run(self.hosts[host]["scp"] + [source, str(target)], capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"{host}: cannot copy {rel}: " + result.stderr.decode("utf-8", "replace")[:300])

    # ---- what this release touches -------------------------------------------------
    def committed(self, rel):
        blob = subprocess.run(["git", "-C", str(REPO), "show", f"{self.commit}:{rel}"], capture_output=True, check=True).stdout
        if (REPO / rel).read_bytes() != blob:
            raise SystemExit(f"working tree differs from {self.commit[:12]}: {rel}")
        return blob

    def changed_repo_files(self):
        out = subprocess.run(["git", "-C", str(REPO), "-c", "core.quotepath=false", "diff", "--name-only",
                              self.spec["previousCommit"], self.commit, "--", "tools", "一键脚本"],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout.split("\n")
        return [rel for rel in out if rel and (REPO / rel).is_file()]

    def publisher_map(self):
        """repo path -> public paths, read from the committed publisher's copy steps."""
        text = self.committed("tools/portable-publish.ps1").decode("utf-8-sig")
        mapping = {}
        for source, dest, where in re.findall(r"Copy-ToolFile -SourceName '([^']+)' -DestinationName '([^']+)' -DestinationRoot \$(updaterRoot|publishDir)", text):
            mapping.setdefault("tools/" + source, []).append(("_updater/" if where == "updaterRoot" else "") + dest)
        for source, dest in re.findall(r"Copy-RootFile -SourceRel '([^']+)' -DestinationName '([^']+)' -DestinationRoot \$publishDir", text):
            mapping.setdefault(source.replace("\\", "/"), []).append(dest)
        return mapping

    def guide_bytes(self, config):
        source = REPO / "tools/portable-publish.ps1"
        with tempfile.TemporaryDirectory(prefix="release-guide-") as tmp:
            out, script = Path(tmp) / "guide.txt", Path(tmp) / "guide.ps1"
            script.write_text("$ast=[Management.Automation.Language.Parser]::ParseFile($env:G_SOURCE,[ref]$null,[ref]$null)\n"
                              "$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-PlayerGuideText'},$false)\n"
                              "Invoke-Expression $fn.Extent.Text\n"
                              "[IO.File]::WriteAllText($env:G_OUT,(Get-PlayerGuideText -PackName $env:G_PACK -ServerAddress $env:G_ADDRESS),(New-Object Text.UTF8Encoding($true)))\n",
                              encoding="utf-8-sig")
            env = dict(os.environ, G_SOURCE=str(source), G_OUT=str(out), G_PACK=config.get("packName") or config.get("packId"),
                       G_ADDRESS=(config.get("server") or {}).get("address") or config["update"]["host"])
            subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], env=env, check=True, capture_output=True, timeout=60)
            return out.read_bytes()

    def touched(self, host):
        """Server paths, public paths and metadata this release may change on a host."""
        mapping = self.publisher_map()
        server, public = [], set()
        for rel in self.changed_repo_files():
            server.append(rel)
            public.update(PUBLIC + p for p in mapping.get(rel, []))
        public.update(PUBLIC + p for p in ("server-manifest.json", "server-manifest.json.sig", "update-log.txt", "使用说明.txt"))
        public.update(PUBLIC + p for p in self.spec.get("removeRows", []))
        return sorted(set(server) | {"tools/portable-pack.json"}), sorted(public)

    # ---- steps ---------------------------------------------------------------------
    def preflight(self):
        receipt = read_json(self.spec["previousReceipt"])
        ok = True
        report = {}
        for host in self.order:
            expected = {e["path"]: e["after"] for e in receipt["hosts"][host]["files"]}
            actual = self.remote_hashes(host, list(expected))
            bad = [p for p in expected if actual.get(p) != expected[p]]
            manifest_version = None
            if not bad:
                manifest_version = json.loads(self.remote_text(host, PUBLIC + "server-manifest.json")).get("version")
            report[host] = {"checked": len(expected), "mismatched": bad, "manifestVersion": manifest_version}
            ok = ok and not bad and manifest_version == self.previous
        print(json.dumps({"productionMatchesPrevious": ok, **report}, ensure_ascii=False, indent=2))
        return ok

    def remote_text(self, host, rel):
        root = self.hosts[host]["root"]
        if self.hosts[host]["kind"] == "posix":
            return self.remote_python(host, "import pathlib,sys\nsys.stdout.write(pathlib.Path(%r).joinpath(%r).read_text(encoding='utf-8-sig'))\n" % (root, rel))
        return self.remote_powershell(host, "[IO.File]::ReadAllText((Join-Path '" + root + "' '" + rel + "'),[Text.Encoding]::UTF8)")

    def run_backup(self):
        records, absent = [], {}
        for host in self.order:
            server, public = self.touched(host)
            present = self.remote_hashes(host, server + public)
            absent[host] = [p for p in server + public if present[p] is None]
            for rel in server + public:
                if present[rel] is None:
                    continue
                target = self.backup / host / rel
                if target.exists():
                    raise SystemExit("refusing to replace existing backup: " + str(target))
                target.parent.mkdir(parents=True, exist_ok=True)
                self.scp_from(host, rel, target)
                data = target.read_bytes()
                if sha256(data) != present[rel]:
                    raise SystemExit(f"{host}: backup hash differs for {rel}")
                records.append({"host": host, "path": rel, "sha256": present[rel], "size": len(data)})
            manifest = read_json(self.backup / host / (PUBLIC + "server-manifest.json"))
            assert manifest["version"] == self.previous, (host, manifest["version"])
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "baseline-receipt.json").write_bytes(encoded({"version": self.previous, "backupRoot": str(self.backup),
                                                                   "absentOnHost": absent, "files": records}))
        print(json.dumps({"baseline": self.previous, "backedUpFiles": len(records), "absentOnHost": absent}, ensure_ascii=False))

    def prepare(self):
        signing = load_module(REPO / "tools/manifest_signing.py", "release_signing")
        reader = load_module(REPO / "tools/player-update-generic.py", "release_reader")
        key_text = Path(self.config["signingKey"]).read_text(encoding="utf-8-sig")
        baseline = read_json(self.work / "baseline-receipt.json")
        mapping = self.publisher_map()
        changed = self.changed_repo_files()
        if self.out.exists():
            shutil.rmtree(self.out)
        receipt = {"version": self.version, "previousVersion": self.previous, "updaterRevision": self.spec["revision"],
                   "preparedAt": now(), "sourceRepository": str(REPO), "sourceCommit": self.commit,
                   "previousCommit": self.spec["previousCommit"], "rollbackRoot": str(self.backup), "hosts": {}}
        stamp = now()
        for host in self.order:
            old = self.backup / host
            absent = set(baseline["absentOnHost"][host])
            manifest = read_json(old / (PUBLIC + "server-manifest.json"))
            original = copy.deepcopy(manifest)
            config = read_json(old / "tools/portable-pack.json")
            manifest.update(version=self.version, generatedAt=stamp, updaterRevision=self.spec["revision"], releaseNotes=self.spec["notes"])
            rows = {r["path"]: r for r in manifest["files"]}
            payloads, changed_rows, added_rows = {}, [], []
            for rel in changed:
                data = self.committed(rel)
                if rel not in absent or rel.startswith("tools/"):
                    payloads[rel] = data  # new tool files are installed too
                for public in mapping.get(rel, []):
                    payloads[PUBLIC + public] = data
                    if public in rows:
                        rows[public].update(size=len(data), sha1=hashlib.sha1(data).hexdigest())
                        changed_rows.append(public)
                    else:
                        added_rows.append({"path": public, "sha1": hashlib.sha1(data).hexdigest(), "size": len(data)})
            guide = self.guide_bytes(config)
            if "使用说明.txt" in rows and rows["使用说明.txt"]["sha1"] != hashlib.sha1(guide).hexdigest():
                rows["使用说明.txt"].update(size=len(guide), sha1=hashlib.sha1(guide).hexdigest())
                payloads[PUBLIC + "使用说明.txt"] = guide
                changed_rows.append("使用说明.txt")
            removed = set(self.spec.get("removeRows", []))
            files = [r for r in manifest["files"] if r["path"] not in removed]
            for row in added_rows:  # next to the last row of the same folder
                folder = row["path"].rsplit("/", 1)[0] + "/" if "/" in row["path"] else ""
                index = max([i for i, r in enumerate(files) if (r["path"].rsplit("/", 1)[0] + "/" if "/" in r["path"] else "") == folder] or [len(files) - 1])
                files.insert(index + 1, row)
            manifest["files"] = files
            keep_old = [r for r in original["files"] if r["path"] not in removed and r["path"] not in changed_rows]
            keep_new = [r for r in files if r["path"] not in changed_rows and r["path"] not in {a["path"] for a in added_rows}]
            assert keep_old == keep_new, host
            assert len({r["path"].lower() for r in files}) == len(files)
            config.update(version=self.version, releaseVersion=self.version, releaseNotesVersion=self.version, releaseLevel="patch",
                          updaterRevision=self.spec["revision"], releaseNotes=self.spec["notes"], releaseModChanges=[],
                          releaseOtherChanges=self.spec["notes"], releaseDecision=self.spec["decision"])
            payloads["tools/portable-pack.json"] = encoded(config)
            old_log = (old / (PUBLIC + "update-log.txt")).read_text(encoding="utf-8-sig")
            title = f"> {dt.date.today():%Y/%m/%d} v{self.version}\n{self.spec['logTitle']}\n\n"
            payloads[PUBLIC + "update-log.txt"] = (title + "\n".join("- " + n for n in self.spec["notes"]) + "\n\n" + old_log).encode("utf-8")
            manifest_bytes = encoded(manifest)
            signature = signing.sign(manifest_bytes, key_text)
            key_id = reader.verify_manifest_signature(manifest_bytes, signature)  # the shipped readers trust this key
            entries = []
            for rel, data in payloads.items():
                before_path = old / rel
                entries.append({"path": rel, "before": sha256(before_path.read_bytes()) if before_path.is_file() and rel not in absent else None,
                                "after": sha256(data), "size": len(data)})
            for rel, data in ((PUBLIC + "server-manifest.json", manifest_bytes), (PUBLIC + "server-manifest.json.sig", signature)):
                payloads[rel] = data
                before_path = old / rel
                entries.append({"path": rel, "before": sha256(before_path.read_bytes()) if before_path.is_file() else None,
                                "after": sha256(data), "size": len(data), "commit": True})
            for rel, data in payloads.items():
                target = self.out / host / "payload" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            plan = {"host": host, "version": self.version, "previousVersion": self.previous, "files": entries}
            (self.out / host / "deploy-plan.json").write_bytes(encoded(plan))
            self.write_deploy_script(host)
            receipt["hosts"][host] = {"managedFiles": len(files), "changedPlayerRows": changed_rows,
                                      "addedPlayerRows": [r["path"] for r in added_rows], "removedPlayerRows": sorted(removed),
                                      "signingKeyId": key_id, "files": entries}
        (self.out / "release-receipt.json").write_bytes(encoded(receipt))
        print(json.dumps({h: {k: (len(v) if isinstance(v, list) else v) for k, v in receipt["hosts"][h].items()} for h in self.order}, ensure_ascii=False))

    def write_deploy_script(self, host):
        info = self.hosts[host]
        suffix = "." + self.stage_name + ".tmp"
        if info["kind"] == "posix":
            text = DEPLOY_POSIX.replace("__ROOT__", repr(info["root"])).replace("__VERSION__", repr(self.version)).replace("__SUFFIX__", repr(suffix))
            (self.out / host / "deploy.py").write_text(text, encoding="utf-8", newline="\n")
        else:
            root = info["root"].replace("/", "\\")
            text = DEPLOY_WINDOWS.replace("__ROOT__", "'" + root + "'").replace("__VERSION__", "'" + self.version + "'").replace("__SUFFIX__", "'" + suffix + "'")
            (self.out / host / "deploy.ps1").write_text(text, encoding="utf-8-sig", newline="\r\n")

    def dryrun(self):
        with tempfile.TemporaryDirectory(prefix="release-dryrun-") as tmp:
            for host in self.order:
                info = self.hosts[host]
                root = Path(tmp) / host / "root"
                stage = Path(tmp) / host / "stage"
                shutil.copytree(self.backup / host, root)
                shutil.copytree(self.out / host, stage)
                plan = read_json(stage / "deploy-plan.json")
                if info["kind"] == "posix":
                    script = stage / "deploy.py"
                    script.write_text(script.read_text(encoding="utf-8").replace(repr(info["root"]), repr(root.as_posix())), encoding="utf-8")
                    commands = [[sys.executable, str(script), "payload"], [sys.executable, str(script), "commit"]]
                else:
                    script = stage / "deploy.ps1"
                    windows_root = info["root"].replace("/", "\\")
                    script.write_text(script.read_text(encoding="utf-8-sig").replace("'" + windows_root + "'", "'" + str(root) + "'"), encoding="utf-8-sig")
                    commands = [[PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Mode", mode] for mode in ("payload", "commit")]
                for command in commands:
                    result = subprocess.run(command, capture_output=True, timeout=180)
                    if result.returncode:
                        raise SystemExit(f"{host} dry run failed: " + (result.stdout + result.stderr).decode("utf-8", "replace")[-1500:])
                bad = [e["path"] for e in plan["files"] if not (root / e["path"]).is_file() or sha256((root / e["path"]).read_bytes()) != e["after"]]
                if bad or list(root.rglob("*" + self.stage_name + ".tmp")):
                    raise SystemExit(f"{host} dry run left wrong files: {bad}")
                again = subprocess.run(commands[0], capture_output=True, timeout=180)
                if again.returncode == 0:
                    raise SystemExit(f"{host}: a second payload run was not refused")
                print(json.dumps({"host": host, "dryRunVerified": len(plan["files"]), "secondRunRefused": True}))

    def remote_stage(self, host):
        info = self.hosts[host]
        return info["stage"].rstrip("/") + "/" + self.stage_name

    def stage(self):
        for host in self.order:
            info = self.hosts[host]
            stage = self.remote_stage(host)
            if info["kind"] == "posix":
                self.ssh(host, f"test ! -e '{stage}'")
            else:
                self.ssh(host, f"powershell -NoProfile -Command \"if (Test-Path -LiteralPath '{stage}') {{ exit 3 }}\"")
            result = subprocess.run(info["scp"] + ["-r", str(self.out / host), f"{info['address']}:{stage}"], capture_output=True, timeout=600)
            if result.returncode:
                raise SystemExit(f"{host}: scp failed: " + result.stderr.decode("utf-8", "replace")[:400])
            plan = read_json(self.out / host / "deploy-plan.json")
            paths = ["payload/" + e["path"] for e in plan["files"]]
            if info["kind"] == "posix":
                program = ("import hashlib,json,pathlib\ns=pathlib.Path(%r)\nprint(json.dumps({p:hashlib.sha256((s/p).read_bytes()).hexdigest() for p in json.loads(%r)}))\n"
                           % (stage, json.dumps(paths, ensure_ascii=False)))
                staged = json.loads(self.remote_python(host, program))
            else:
                script = ("$s='" + stage + "';$o=@{};foreach($p in ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
                          + base64.b64encode(json.dumps(paths, ensure_ascii=False).encode()).decode()
                          + "'))|ConvertFrom-Json)){$o[$p]=(Get-FileHash -LiteralPath (Join-Path $s $p) -Algorithm SHA256).Hash.ToLowerInvariant()};$o|ConvertTo-Json -Compress")
                staged = json.loads(self.remote_powershell(host, script))
            bad = [e["path"] for e in plan["files"] if staged.get("payload/" + e["path"]) != e["after"]]
            if bad:
                raise SystemExit(f"{host}: staged hash mismatch: {bad}")
            print(json.dumps({"host": host, "stage": stage, "stagedFiles": len(paths)}))

    def snapshot(self):
        state = {}
        for host in self.order:
            info = self.hosts[host]
            ports = info.get("gamePorts") or []
            if ports and info["kind"] == "posix":
                listeners = self.ssh(host, "lsof -nP " + " ".join(f"-iTCP:{p}" for p in ports) + " -sTCP:LISTEN -Fp 2>/dev/null || true")
                state[host + ":game"] = sorted(line for line in listeners.splitlines() if line.startswith("p"))
            if info.get("serviceStatus"):
                state[host + ":service"] = self.ssh(host, info["serviceStatus"] + " || true").split()
        return state

    def run_deploy_phase(self, host, phase):
        info = self.hosts[host]
        stage = self.remote_stage(host)
        if info["kind"] == "posix":
            return json.loads(self.ssh(host, f"{info['python']} {stage}/deploy.py {phase}"))
        return json.loads(self.ssh(host, f"powershell -NoProfile -ExecutionPolicy Bypass -File {stage}/deploy.ps1 -Mode {phase}"))

    def deploy(self):
        report = {"startedAt": now(), "before": self.snapshot(), "steps": []}
        restarted = []
        try:
            for host in self.order:
                info = self.hosts[host]
                report["steps"].append({host: self.run_deploy_phase(host, "payload")})
                plan = read_json(self.out / host / "deploy-plan.json")
                if self.spec.get("restartService") and info.get("serviceRestart") and any(
                        e["path"] == "tools/secure-update-server.py" for e in plan["files"]):
                    self.restart_service(host)
                    restarted.append(host)
                report["steps"].append({host: self.run_deploy_phase(host, "commit")})
                print(json.dumps(report["steps"][-2:], ensure_ascii=False), flush=True)
            report["after"] = self.snapshot()
            for key, value in report["before"].items():
                host = key.split(":")[0]
                if key.endswith(":service") and host in restarted:
                    continue
                if report["after"].get(key) != value:
                    raise SystemExit(f"{key} changed during deployment: {value} -> {report['after'].get(key)}")
            report["restartedServices"] = restarted
            report["gameUnchanged"] = True
            report["completedAt"] = now()
        finally:
            (self.out / "deployment-receipt.json").write_bytes(encoded(report))
        print(json.dumps({k: report.get(k) for k in ("before", "after", "restartedServices", "gameUnchanged")}, ensure_ascii=False))

    def restart_service(self, host):
        """Restart the download service, prove it serves the manifest, or put the old server back."""
        info = self.hosts[host]
        lan = self.config["verifyUrls"].get(host)
        def healthy():
            for _ in range(20):
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(lan.format(token=self.token), timeout=5) as response:
                        if response.status == 200 and json.loads(response.read().decode("utf-8-sig")).get("files"):
                            return True
                except Exception:
                    pass
                time.sleep(1)
            return False
        self.ssh(host, info["serviceRestart"])
        if lan and healthy():
            print(json.dumps({"host": host, "serviceRestarted": True}), flush=True)
            return
        # Roll the server script back and restart again before anything else changes.
        backup_file = self.backup / host / "tools/secure-update-server.py"
        result = subprocess.run(info["scp"] + [str(backup_file), f"{info['address']}:{info['root']}/tools/secure-update-server.py"], capture_output=True, timeout=120)
        self.ssh(host, info["serviceRestart"])
        raise SystemExit(f"{host}: the new download service did not answer; restored the previous server script "
                         f"(scp rc={result.returncode}, healthy again: {healthy()}). The manifest was not changed.")

    def verify(self):
        receipt = read_json(self.out / "release-receipt.json")
        primary = self.config["primary"]
        public = self.out / primary / "payload" / PUBLIC
        manifest_bytes = (public / "server-manifest.json").read_bytes()
        signature = (public / "server-manifest.json.sig").read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        reader = load_module(REPO / "tools/player-update-generic.py", "release_verify_reader")
        key_id = reader.verify_manifest_signature(manifest_bytes, signature)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def fetch(url, headers=None):
            request = urllib.request.Request(url, headers={"User-Agent": f"SCEX-release-verify/{self.version}", **(headers or {})})
            with opener.open(request, timeout=25) as response:
                return response.read(), response.status, dict(response.headers)
        urls = {name: url.format(token=self.token) for name, url in self.config["verifyUrls"].items() if name == primary}
        urls["playerEntry"] = manifest["updateUrl"]
        rows = receipt["hosts"][primary]["changedPlayerRows"] + receipt["hosts"][primary]["addedPlayerRows"]
        report = {"version": self.version, "verifiedAt": now(), "signingKeyId": key_id, "endpoints": {}}
        for name, url in urls.items():
            data, status, _ = fetch(url)
            assert status == 200 and data == manifest_bytes, name + ": manifest differs"
            sig, status, _ = fetch(url + ".sig")
            assert status == 200 and sig == signature, name + ": signature differs"
            base = url.rsplit("/", 1)[0] + "/"
            for rel in rows:
                payload, status, _ = fetch(base + urllib.parse.quote(rel, safe="/"))
                assert status == 200 and payload == (public / rel).read_bytes(), name + ": payload differs " + rel
            full = (public / "_updater/player-update-generic.py").read_bytes()
            part, status, headers = fetch(base + "_updater/player-update-generic.py", {"Range": "bytes=19-50"})
            assert status == 206 and part == full[19:51], name + ": range"
            import gzip as _gzip
            packed, status, headers = fetch(url, {"Accept-Encoding": "gzip"})
            gzip_ok = headers.get("Content-Encoding") == "gzip" and _gzip.decompress(packed) == manifest_bytes
            report["endpoints"][name] = {"manifestSha256": sha256(data), "files": len(rows), "rangeStatus": 206, "gzip": gzip_ok}
            print(json.dumps({"endpoint": name, "version": self.version, "verifiedFiles": len(rows), "gzip": gzip_ok}))
        report["liveBootstrap"] = self.verify_bootstrap(manifest, public)
        (self.out / "http-verification.json").write_bytes(encoded(report))
        print(json.dumps({"liveBootstrap": report["liveBootstrap"]}, ensure_ascii=False))

    def verify_bootstrap(self, manifest, public):
        old = self.backup / self.config["primary"] / PUBLIC / "_updater/portable-bootstrap-refresh.ps1"
        results = {}
        with tempfile.TemporaryDirectory(prefix="release-live-bootstrap-") as tmp:
            for label in ("previous", "slim"):
                fixture = Path(tmp) / label
                scripts = fixture / "_updater"
                scripts.mkdir(parents=True)
                if label == "previous":
                    (scripts / old.name).write_bytes(old.read_bytes())
                    (fixture / "UPDATE-URL.txt").write_text(manifest["updateUrl"] + "\n", encoding="utf-8")
                else:
                    for name in HELPERS:
                        shutil.copyfile(public / "_updater" / name if (public / "_updater" / name).is_file()
                                        else self.backup / self.config["primary"] / PUBLIC / "_updater" / name, scripts / name)
                    (scripts / "UPDATE-URL.txt").write_text(manifest["updateUrl"] + "\n", encoding="utf-8")
                result = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(scripts / "portable-bootstrap-refresh.ps1"),
                                         "-InstanceDir", str(fixture), "-ScriptDir", str(scripts)], capture_output=True, timeout=240)
                log = (result.stdout + result.stderr).decode("utf-8", "replace").replace(manifest["updateUrl"].rsplit("/", 1)[0], "[更新地址]")
                (self.out / f"bootstrap-{label}.log").write_text(log, encoding="utf-8")
                assert result.returncode == 0, log[-800:]
                for name in HELPERS:
                    row = next(r for r in manifest["files"] if r["path"] == "_updater/" + name)
                    assert hashlib.sha1((scripts / name).read_bytes()).hexdigest() == row["sha1"], (label, name)
                results[label] = {"current": log.count("已是最新"), "refreshed": log.count("已刷新")}
        assert results["slim"]["refreshed"] == 0, results
        return results

    def e2e(self):
        """Previous root entry / engines against this release, served from localhost."""
        primary = self.config["primary"]
        new_public = self.out / primary / "payload" / PUBLIC
        old_public = self.backup / primary / PUBLIC
        manifest = json.loads((new_public / "server-manifest.json").read_text(encoding="utf-8"))
        previous = json.loads((old_public / "server-manifest.json").read_text(encoding="utf-8-sig"))
        signing = load_module(REPO / "tools/manifest_signing.py", "release_e2e_signing")
        key_text = Path(self.config["signingKey"]).read_text(encoding="utf-8-sig")
        server_module = load_module(REPO / "tools/secure-update-server.py", "release_e2e_server")

        url_rows = {"_updater/UPDATE-URL.txt", "_updater/PORTABLE-UPDATE-URL.txt", "_updater/UPDATE-URL-LAN.txt",
                    "UPDATE-URL.txt", "PORTABLE-UPDATE-URL.txt", "UPDATE-URL-LAN.txt"}

        def scoped(m, source_dirs):
            """Root and _updater rows whose exact bytes this tool has (address rows always)."""
            rows = []
            for row in m["files"]:
                path = row["path"]
                if "/" in path and not path.startswith("_updater/"):
                    continue
                if path in url_rows or any((d / path).is_file() and hashlib.sha1((d / path).read_bytes()).hexdigest() == row["sha1"] for d in source_dirs):
                    rows.append(row)
            return rows
        report = {}
        with tempfile.TemporaryDirectory(prefix="release-e2e-") as tmp:
            base = Path(tmp)
            web = base / "web"
            token = "e2e-token"
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(server_module.SecureUpdateHandler, directory=str(web), token=token))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_port}/{token}/server-manifest.json"
            url_text = (url + "\r\n").encode("utf-8")
            served = []
            for row in scoped(manifest, [new_public, old_public]):
                if row["path"] in url_rows:
                    data = url_text
                else:
                    data = next((d / row["path"]).read_bytes() for d in (new_public, old_public) if (d / row["path"]).is_file()
                                and hashlib.sha1((d / row["path"]).read_bytes()).hexdigest() == row["sha1"])
                (web / row["path"]).parent.mkdir(parents=True, exist_ok=True)
                (web / row["path"]).write_bytes(data)
                served.append({"path": row["path"], "sha1": hashlib.sha1(data).hexdigest(), "size": len(data)})
            local = dict(manifest, files=served)
            local.pop("downloadSourceStatus", None)
            raw = encoded(local)
            (web / "server-manifest.json").write_bytes(raw)
            (web / "server-manifest.json.sig").write_bytes(signing.sign(raw, key_text))
            expected = {r["path"]: r["sha1"] for r in served}
            old_rows = scoped(previous, [old_public])

            def client(label):
                folder = base / label / ".minecraft/versions/SCEX"
                rows = []
                for row in old_rows:
                    data = url_text if row["path"] in url_rows else (old_public / row["path"]).read_bytes()
                    (folder / row["path"]).parent.mkdir(parents=True, exist_ok=True)
                    (folder / row["path"]).write_bytes(data)
                    rows.append({"path": row["path"], "sha1": hashlib.sha1(data).hexdigest(), "size": len(data)})
                (folder / ".portable-sync-state.json").write_text(json.dumps({"format": 3, "version": previous["version"], "files": rows}), encoding="utf-8")
                return folder

            def check(folder, label, signed=True):
                """Rows match this release; signed=False for previous engines, which do not
                know signatures (the next run with the new engine turns the requirement on)."""
                wrong = [p for p, s in expected.items() if not (folder / p).is_file() or hashlib.sha1((folder / p).read_bytes()).hexdigest() != s]
                state = read_json(folder / ".portable-sync-state.json")
                gone = [r["path"] for r in old_rows if r["path"] not in expected and (folder / r["path"]).exists()]
                sticky = state.get("manifestSignature") == "required"
                assert not wrong and not gone and state.get("version") == self.version and (sticky or not signed), (label, wrong, gone, state.get("version"), sticky)
                return sorted(p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))

            def run(argv, cwd, **env):
                environment = dict(os.environ, PYTHONUTF8="1", NO_PROXY="127.0.0.1", no_proxy="127.0.0.1", **env)
                environment.pop("PORTABLE_SYNC_INTERACTIVE", None)
                result = subprocess.run(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
                return result.returncode, result.stdout.decode("utf-8", "replace")
            try:
                folder = client("A")
                code, out = run(["cmd", "/d", "/c", str(folder / "更新mod-Windows端.bat")], folder, PORTABLE_SYNC_ONLY="1", PORTABLE_MANIFEST_URL=url)
                (self.out / "e2e-A1.log").write_text(out, encoding="utf-8")
                assert code == 0, out[-1500:]
                root_files = check(folder, "A1")
                code, out = run(["cmd", "/d", "/c", str(folder / "更新mod-Windows端.bat")], folder, PORTABLE_SYNC_ONLY="1")
                (self.out / "e2e-A2.log").write_text(out, encoding="utf-8")
                assert code == 0 and "清单签名校验通过" in out, out[-1500:]
                report["A"] = {"rootFilesAfter": root_files, "secondRunVerifiedSignature": True}
                for label, argv in (("B", [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "{u}/player-update-generic.ps1", "-InstanceDir", "{c}", "-ManifestUrl", url, "-NoPause"]),
                                    ("C", [sys.executable, "{u}/player-update-generic.py", "--instance-dir", "{c}", "--manifest-url", url])):
                    folder = client(label)
                    engine = base / label / "engine"
                    shutil.copytree(folder / "_updater", engine)
                    code, out = run([a.replace("{u}", str(engine)).replace("{c}", str(folder)) for a in argv], folder)
                    (self.out / f"e2e-{label}.log").write_text(out, encoding="utf-8")
                    assert code == 0, (label, out[-1500:])
                    check(folder, label, signed=False)
                    # The next run uses the engine that the previous one just installed.
                    code, out = run([a.replace("{u}", str(folder / "_updater")).replace("{c}", str(folder)) for a in argv], folder)
                    (self.out / f"e2e-{label}2.log").write_text(out, encoding="utf-8")
                    assert code == 0 and "清单签名校验通过" in out, (label, out[-1500:])
                    report[label] = {"rootFilesAfter": check(folder, label), "nextRunVerifiedSignature": True}
            finally:
                server.shutdown()
                server.server_close()
        (self.out / "e2e-transition.json").write_bytes(encoded(report))
        print(json.dumps(report, ensure_ascii=False))

    def finalize(self):
        verified = {}
        for host in self.order:
            plan = read_json(self.out / host / "deploy-plan.json")
            actual = self.remote_hashes(host, [e["path"] for e in plan["files"]])
            bad = [e["path"] for e in plan["files"] if actual.get(e["path"]) != e["after"]]
            assert not bad, (host, bad)
            verified[host] = len(plan["files"])
            stage = self.remote_stage(host)
            if self.hosts[host]["kind"] == "posix":
                self.remote_python(host, "import pathlib,shutil\ns=pathlib.Path(%r)\nassert s.name==%r and not s.is_symlink()\nshutil.rmtree(s)\n" % (stage, self.stage_name))
            else:
                self.remote_powershell(host, "$s='" + stage + "';if((Split-Path -Leaf $s) -ne '" + self.stage_name + "'){throw 'unexpected'};"
                                       "if(((Get-Item -LiteralPath $s -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'link'};"
                                       "Remove-Item -LiteralPath $s -Recurse -Force")
        receipt_path = self.out / "release-receipt.json"
        receipt = read_json(receipt_path)
        deploy = read_json(self.out / "deployment-receipt.json")
        http_report = read_json(self.out / "http-verification.json")
        assert deploy.get("gameUnchanged") and http_report["version"] == self.version
        receipt.update(status="PUBLISHED_VERIFIED", completedAt=now(), productionFilesVerified=verified,
                       restartedServices=deploy.get("restartedServices", []), validation=self.spec.get("validation", {}),
                       cleanup={h: self.remote_stage(h) for h in self.order})
        receipt_path.write_bytes(encoded(receipt))
        print(json.dumps({"status": "PUBLISHED_VERIFIED", "version": self.version, "productionFilesVerified": verified}))


DEPLOY_POSIX = r'''from pathlib import Path
import hashlib, json, os, shutil, sys
# Generated by tools/release-updater.py. "payload" installs every file except the manifest
# and its signature; "commit" installs those two last. Every target must still hold its
# backed-up hash (new files: must not exist) before anything is replaced.
stage = Path(__file__).resolve().parent
root = Path(__ROOT__)
plan = json.loads((stage / "deploy-plan.json").read_text(encoding="utf-8"))
assert plan["version"] == __VERSION__
mode = sys.argv[1]
assert mode in ("payload", "commit")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


for entry in plan["files"]:
    target = root / entry["path"]
    assert target.parent.resolve().is_relative_to(root.resolve()) and not target.is_symlink(), entry["path"]
    expected = entry["before"] if (mode == "payload" or entry.get("commit")) else entry["after"]
    assert sha(target) == expected, "Source changed: " + entry["path"]
    assert sha(stage / "payload" / entry["path"]) == entry["after"], "Payload mismatch: " + entry["path"]
selected = [e for e in plan["files"] if bool(e.get("commit")) == (mode == "commit")]
for entry in selected:
    target = root / entry["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + __SUFFIX__)
    assert not temp.exists(), "Unexpected deployment temporary file"
    shutil.copyfile(stage / "payload" / entry["path"], temp)
    os.chmod(temp, (target.stat().st_mode & 0o777) if entry["before"] else (0o755 if entry["path"].endswith(".command") else 0o644))
    assert sha(temp) == entry["after"]
    os.replace(temp, target)
    assert sha(target) == entry["after"]
print(json.dumps({"phase": mode, "version": plan["version"], "verifiedFiles": len(selected),
                  "created": sum(1 for e in selected if e["before"] is None)}))
'''

DEPLOY_WINDOWS = r'''param([ValidateSet('payload', 'commit')][string]$Mode)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Generated by tools/release-updater.py; see the posix variant for the rules.
$root = __ROOT__
$plan = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'deploy-plan.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
if ($plan.version -ne __VERSION__) { throw 'Invalid deployment plan' }
function Get-Sha([string]$Path) { if ([IO.File]::Exists($Path)) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() } return $null }
foreach ($entry in $plan.files) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $entry.path))
    if (-not $target.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid target' }
    $expected = if ($Mode -eq 'payload' -or $entry.commit) { $entry.before } else { $entry.after }
    if ((Get-Sha $target) -ne $expected) { throw "Source changed: $($entry.path)" }
    if ((Get-Sha (Join-Path (Join-Path $PSScriptRoot 'payload') $entry.path)) -ne $entry.after) { throw "Payload mismatch: $($entry.path)" }
}
$created = 0
$selected = @($plan.files | Where-Object { [bool]$_.commit -eq ($Mode -eq 'commit') })
foreach ($entry in $selected) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $entry.path))
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $target))
    $temp = $target + __SUFFIX__
    if (Test-Path -LiteralPath $temp) { throw 'Unexpected temporary file' }
    Copy-Item -LiteralPath (Join-Path (Join-Path $PSScriptRoot 'payload') $entry.path) -Destination $temp
    if ((Get-Sha $temp) -ne $entry.after) { throw 'Staging mismatch' }
    if ($null -eq $entry.before) { [IO.File]::Move($temp, $target); $created++ }
    else { [IO.File]::Replace($temp, $target, [System.Management.Automation.Language.NullString]::Value) }
    if ((Get-Sha $target) -ne $entry.after) { throw 'Install mismatch' }
}
@{ phase = $Mode; version = $plan.version; verifiedFiles = $selected.Count; created = $created } | ConvertTo-Json -Compress
'''


def main():
    parser = argparse.ArgumentParser(description="SCEX updater release tool")
    parser.add_argument("--config", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("step", choices=["preflight", "backup", "prepare", "e2e", "dryrun", "stage", "deploy", "verify", "finalize"])
    args = parser.parse_args()
    release = Release(args.config, args.release)
    step = {"preflight": release.preflight, "backup": release.run_backup, "prepare": release.prepare, "e2e": release.e2e,
            "dryrun": release.dryrun, "stage": release.stage, "deploy": release.deploy, "verify": release.verify,
            "finalize": release.finalize}[args.step]
    result = step()
    return 1 if result is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
