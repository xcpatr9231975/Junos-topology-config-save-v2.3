#!/usr/bin/env python3
"""junos-config-save: save, list, diff and load Junos configurations over SSH.

Single file, Python standard library only; needs the system `ssh` and `scp`.

  save     snapshot configs (stanza/set/json/xml, any mix) - read-only
  list     list saved snapshots
  diff     diff a saved file against live config or another saved file
  load     copy a file to the router and load it (dry run by default)
  confirm  confirm a pending 'commit confirmed'

Router inventory (routers.json) - location from --routers-json, the
JUNOS_CS_ROUTERS_JSON environment variable, the existing $TOPONAME server inventory
when present, or routers.json next to this script:

  {
    "defaults": {"user": "admin", "password": "..."},
    "routers": {
      "r1": "192.0.2.1",
      "r2": {"host": "192.0.2.2", "user": "root", "password": "...", "port": 22,
             "tags": ["core"]}
    }
  }

A flat {"name": "ip"} file is also accepted. A router without a password uses
your SSH key/agent. $TOPONAME devices inventories use type "junos", name, mgmt_ip,
and login "username/password"; other device types are excluded.
Passwords are handed to ssh through SSH_ASKPASS and the
environment - never on a command line, in a file written by this tool, or in
any output. Keep routers.json out of version control.

Load safety: without --commit, 'load' only loads, shows the diff, runs
'commit check' and rolls back. A real commit needs BOTH --commit and
--confirm-minutes N ('commit confirmed N', which rolls back on its own unless
you run 'confirm' in time).
"""
import argparse
import atexit
import concurrent.futures
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve()
ENV_PASSWORD = "JUNOS_CS_PASSWORD"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
COMMENT_RE = re.compile(r"^[A-Za-z0-9 ._:,-]{1,80}$")
ERROR_RE = re.compile(r"^\s*(error:|commit failed|configuration check-out failed)", re.I | re.M)

# format -> (display pipe, extension, inheritance by default)
FORMATS = {
    "stanza": ("", ".config", True),
    "set": ("| display set", ".set", False),
    "json": ("| display json", ".json", False),
    "xml": ("| display xml", ".xml", False),
}
EXT_TO_FORMAT = {".config": "stanza", ".conf": "stanza", ".set": "set", ".json": "json", ".xml": "xml"}


class Fail(Exception):
    pass


# ---------------------------------------------------------------- inventory
class Router:
    def __init__(self, name, host, user, password, port, tags):
        self.name, self.host, self.user = name, host, user
        self.password, self.port, self.tags = password, port, tags


def routers_json_path(args):
    value = args.routers_json or os.environ.get("JUNOS_CS_ROUTERS_JSON")
    if value:
        return Path(value).expanduser()
    topology_path = Path("/home/jnpr/projects/$TOPONAME/router.json")
    if os.name != "nt" and topology_path.is_file():
        return topology_path
    return SCRIPT.with_name("routers.json")


def inventory_entries(data):
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise Fail("Inventory defaults must be an object")
    if "devices" in data:
        if not isinstance(data["devices"], list):
            raise Fail("Inventory devices must be a list")
        entries = {}
        for device in data["devices"]:
            if not isinstance(device, dict):
                raise Fail("Each inventory device must be an object")
            if device.get("type") != "junos":
                continue
            name = device.get("name")
            if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                raise Fail("Junos device has a missing/invalid name")
            if name in entries:
                raise Fail(f"Duplicate router name: {name}")
            entry = {"host": device.get("mgmt_ip"), "tags": device.get("tags", [])}
            login = device.get("login")
            if login:
                if not isinstance(login, str) or "/" not in login:
                    raise Fail(f"Router {name}: login must use username/password format")
                user, password = login.split("/", 1)
                if not user or not password:
                    raise Fail(f"Router {name}: login needs both username and password")
                entry.update(user=user, password=password)
            for key in ("user", "password", "port"):
                if key in device:
                    entry[key] = device[key]
            entries[name] = entry
    else:
        entries = data.get("routers", data)
    if not isinstance(entries, dict):
        raise Fail("Inventory routers must be an object")
    if not entries:
        raise Fail("Inventory contains no Junos routers")
    return defaults, entries


def load_routers(args):
    path = routers_json_path(args)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Fail(f"Cannot read router inventory {path}: {exc}")
    if not isinstance(data, dict):
        raise Fail(f"{path}: top level must be a JSON object")
    defaults, entries = inventory_entries(data)
    routers = {}
    for name, entry in entries.items():
        if isinstance(entry, str):
            entry = {"host": entry}
        if not isinstance(entry, dict):
            raise Fail(f"{path}: router {name} must be a host string or object")
        host = entry.get("host")
        user = entry.get("user", defaults.get("user", "jnpr"))
        if not NAME_RE.match(name):
            raise Fail(f"{path}: invalid router name {name!r}")
        if not host or not HOST_RE.match(str(host)):
            raise Fail(f"{path}: router {name} has a missing/invalid host")
        if not NAME_RE.match(str(user)):
            raise Fail(f"{path}: router {name} has an invalid user")
        password = entry.get("password", defaults.get("password")) or None
        if password is not None and not isinstance(password, str):
            raise Fail(f"{path}: router {name} password must be a string")
        port = entry.get("port", defaults.get("port", 22))
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise Fail(f"{path}: router {name} port must be an integer from 1 to 65535")
        tags = entry.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise Fail(f"{path}: router {name} tags must be a list of strings")
        routers[name] = Router(
            name, str(host), str(user),
            password, port, tags,
        )
    return routers


def select_routers(args, routers):
    names = []
    if args.routers == ["all"]:
        names = list(routers)
    else:
        unknown = [n for n in args.routers if n not in routers]
        if unknown:
            raise Fail(f"Unknown routers: {', '.join(unknown)}")
        names = list(args.routers)
    for tag in args.tag or []:
        names += [n for n, r in routers.items() if tag in r.tags]
    names = list(dict.fromkeys(names))
    if not names:
        raise Fail("No routers selected (give names, 'all' or --tag)")
    return [routers[n] for n in names]


# ---------------------------------------------------------------- ssh
_askpass = {}


def askpass_wrapper():
    if "path" not in _askpass:
        folder = tempfile.mkdtemp(prefix="jcs-")
        atexit.register(shutil.rmtree, folder, True)
        if os.name == "nt":
            path = os.path.join(folder, "askpass.cmd")
            body = f'@echo off\r\n"{sys.executable}" "{SCRIPT}" --askpass\r\n'
        else:
            path = os.path.join(folder, "askpass.sh")
            body = f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}" --askpass\n'
        with open(path, "w", newline="") as f:
            f.write(body)
        os.chmod(path, 0o700)
        _askpass["path"] = path
    return _askpass["path"]


class Ssh:
    def __init__(self, strict_hostkey=True):
        self.strict = "yes" if strict_hostkey else "accept-new"

    def _options(self, router):
        opts = ["-o", f"StrictHostKeyChecking={self.strict}", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=5"]
        opts += ["-o", "NumberOfPasswordPrompts=1"] if router.password else ["-o", "BatchMode=yes"]
        return opts

    def _env(self, router):
        env = os.environ.copy()
        if router.password:
            env.update({"SSH_ASKPASS": askpass_wrapper(), "SSH_ASKPASS_REQUIRE": "force",
                        ENV_PASSWORD: router.password})
            env.setdefault("DISPLAY", "jcs:0")
        return env

    def _run(self, argv, router, stdin, timeout):
        kwargs = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=self._env(router), **kwargs)
        return proc.returncode, proc.stdout, clean_stderr(proc.stderr)

    def run(self, router, remote_cmd=None, stdin=None, timeout=60):
        argv = ["ssh", "-p", str(router.port)] + self._options(router) + [f"{router.user}@{router.host}"]
        if remote_cmd:
            argv.append(remote_cmd)
        return self._run(argv, router, stdin, timeout)

    def put(self, router, local, remote_path, timeout=120):
        argv = ["scp", "-P", str(router.port)] + self._options(router) + [
            str(local), f"{router.user}@{router.host}:{remote_path}"]
        return self._run(argv, router, None, timeout)


def clean_stderr(text):
    keep = [l for l in text.splitlines()
            if "Permanently added" not in l and "password" not in l.lower() and l.strip()]
    return "\n".join(keep)[:500]


# ---------------------------------------------------------------- helpers
def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def clean_identifier(value):
    ident = value.strip().replace(" ", "_").replace("/", "_").replace("\\", "_")
    if not ident or ".." in ident or not re.match(r"^[A-Za-z0-9._-]+$", ident):
        raise Fail("identifier may only contain letters, digits, '.', '_' and '-'")
    return ident


def output_dir(args):
    return Path(args.output_dir or os.environ.get("JUNOS_CS_OUTPUT_DIR") or Path.cwd()).expanduser()


def saved_folder(base, identifier):
    root = base.resolve()
    folder = (root / "config" / clean_identifier(identifier)).resolve()
    if root not in folder.parents:
        raise Fail("Configuration directory must stay inside the save directory")
    return folder


def browse_files(base):
    files = []
    config_root = base / "config"
    if base.resolve() not in config_root.resolve().parents:
        raise Fail("Configuration directory must stay inside the save directory")
    if not config_root.exists():
        return files
    for session in sorted(config_root.iterdir()):
        if not session.is_dir() or session.is_symlink() or not NAME_RE.fullmatch(session.name):
            continue
        folder = saved_folder(base, session.name)
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix in EXT_TO_FORMAT and path.name != "manifest.json":
                stat = path.stat()
                files.append({"identifier": session.name, "filename": path.name,
                              "size_bytes": stat.st_size, "modified": stat.st_mtime})
    return files


def saved_file(base, identifier, filename):
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        raise Fail("Invalid saved filename")
    folder = saved_folder(base, identifier)
    path = folder / filename
    if (path.is_symlink() or not path.is_file() or path.suffix not in EXT_TO_FORMAT
            or filename == "manifest.json"):
        raise Fail("Saved configuration not found")
    return path


def parse_formats(value):
    names = list(FORMATS) if value.strip().lower() == "all" else [v.strip().lower() for v in value.split(",") if v.strip()]
    bad = [n for n in names if n not in FORMATS]
    if bad or not names:
        raise Fail(f"Unknown format(s) {bad or value!r}; choose from {', '.join(FORMATS)} or 'all'")
    return list(dict.fromkeys(names))


def pipeline(fmt, inheritance):
    display, _ext, default_inherit = FORMATS[fmt]
    inherit = default_inherit if inheritance == "auto" else inheritance == "yes"
    parts = ["show configuration"] + (["| display inheritance"] if inherit else [])
    if display:
        parts.append(display)
    return " ".join(parts) + " | no-more"


def fetch(ssh, router, fmt, inheritance="auto", save_to=None):
    cmd = pipeline(fmt, inheritance) + (f" | save {save_to}" if save_to else "")
    rc, out, err = ssh.run(router, cmd, timeout=90)
    if rc != 0:
        raise Fail(f"ssh rc={rc}: {err}")
    if save_to:
        return out
    if not out.strip() or ERROR_RE.match(out):
        raise Fail(f"router returned no configuration: {out.strip()[:200] or 'empty output'}")
    return out


# ---------------------------------------------------------------- save
def save_router(ssh, router, formats, ident, stamp, ident_dir, inheritance, router_copy):
    entries, errors = {}, []
    for fmt in formats:
        filename = f"{router.name}.{ident}.{stamp}{FORMATS[fmt][1]}"
        try:
            text = fetch(ssh, router, fmt, inheritance)
            (ident_dir / filename).write_text(text, encoding="utf-8", newline="\n")
            entry = {"filename": filename, "size_bytes": len(text.encode("utf-8"))}
            if router_copy:
                fetch(ssh, router, fmt, inheritance, save_to=f"/var/tmp/{filename}")
                entry["router_copy"] = f"/var/tmp/{filename}"
            entries[fmt] = entry
        except (Fail, subprocess.TimeoutExpired, OSError) as exc:
            errors.append(f"{fmt}: {exc}")
    return router.name, entries, errors


def save_all(args, ssh, routers, formats, ident, inheritance="auto", router_copy=False, jobs=4):
    stamp = utc_stamp()
    ident_dir = saved_folder(output_dir(args), ident)
    ident_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = ident_dir / "manifest.json"
    manifest = {"identifier": ident, "mapping": {}}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    manifest.setdefault("created", now)
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(save_router, ssh, r, formats, ident, stamp, ident_dir, inheritance, router_copy)
                   for r in routers]
        for future in concurrent.futures.as_completed(futures):
            name, entries, errors = future.result()
            for fmt, entry in entries.items():
                entry["saved_at"] = now
                manifest["mapping"].setdefault(name, {})[fmt] = entry
                print(f"OK    {name:<18} {fmt:<6} {entry['filename']} ({entry['size_bytes']} bytes)")
            for message in errors:
                failures += 1
                print(f"FAIL  {name:<18} {message}", file=sys.stderr)
    manifest["updated"] = now
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Saved under {ident_dir}")
    return failures


def cmd_save(args):
    routers = select_routers(args, load_routers(args))
    ident = clean_identifier(args.identifier or f"save-{utc_stamp()}")
    failures = save_all(args, Ssh(args.strict_hostkey), routers, parse_formats(args.format), ident,
                        args.inheritance, args.router_copy, args.jobs)
    return 1 if failures else 0


# ---------------------------------------------------------------- list / diff
def cmd_list(args):
    base = output_dir(args)
    if not base.exists():
        print("No saves yet.")
        return 0
    for entry in browse_files(base):
        print(f"config/{entry['identifier']}/{entry['filename']} ({entry['size_bytes']} bytes)")
    return 0


def cmd_diff(args):
    saved = saved_file(output_dir(args), args.identifier, args.file)
    folder = saved.parent
    if not saved.is_file():
        raise Fail(f"Not found: {saved}")
    old = saved.read_text(encoding="utf-8")
    if args.against:
        other = saved_file(output_dir(args), args.identifier, args.against)
        if not other.is_file():
            raise Fail(f"Not found: {other}")
        new, label = other.read_text(encoding="utf-8"), args.against
    else:
        routers = load_routers(args)
        name = args.file.split(".", 1)[0]
        if name not in routers:
            raise Fail(f"Cannot infer router from filename: {args.file}")
        fmt = EXT_TO_FORMAT.get(saved.suffix)
        if not fmt:
            raise Fail(f"Unsupported file type: {saved.suffix}")
        new, label = fetch(Ssh(args.strict_hostkey), routers[name], fmt, args.inheritance), f"{name} (live)"
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=args.file, tofile=label, lineterm=""))
    print("\n".join(diff) if diff else "No differences.")
    return 0


# ---------------------------------------------------------------- load / confirm
def load_script(mode, remote, commit_minutes, comment):
    lines = ["configure exclusive", f"load {mode} {remote}", "show | compare", "commit check"]
    if commit_minutes:
        lines.append(f'commit confirmed {commit_minutes} comment "{comment}"')
    else:
        lines.append("rollback 0")
    lines += ["exit", ""]
    return "\n".join(lines)


def run_cli(ssh, router, script):
    rc, out, err = ssh.run(router, stdin=script, timeout=180)
    if rc != 0:
        raise Fail(f"ssh rc={rc}: {err}")
    return out


def has_changes(output):
    return bool(re.search(r"^[+-]", output, re.M))


def cmd_load(args):
    if bool(args.commit) != (args.confirm_minutes is not None):
        raise Fail("--commit and --confirm-minutes must be given together (omit both for a dry run)")
    if args.confirm_minutes is not None and not 1 <= args.confirm_minutes <= 120:
        raise Fail("--confirm-minutes must be between 1 and 120")
    local = Path(args.file).expanduser()
    if not local.is_file() or local.stat().st_size == 0:
        raise Fail(f"File not found or empty: {local}")
    fmt = EXT_TO_FORMAT.get(local.suffix.lower())
    if fmt not in ("set", "stanza"):
        raise Fail("Only .set and .config/.conf files can be loaded (JSON/XML are archive only)")
    mode = args.mode or ("set" if fmt == "set" else "merge")
    if (mode == "set") != (fmt == "set"):
        raise Fail(f"--mode {mode} does not match a {local.suffix} file")
    comment = args.comment or f"junos-config-save load {local.name}"
    if not COMMENT_RE.match(comment):
        raise Fail("--comment may only contain letters, digits, spaces and . _ : , -")
    routers = select_routers(args, load_routers(args))
    ssh = Ssh(args.strict_hostkey)
    stamp = utc_stamp()

    if not args.no_snapshot:
        print("== Pre-change snapshot ==")
        if save_all(args, ssh, routers, ["stanza", "set"], f"pre-load-{stamp}", jobs=args.jobs):
            raise Fail("Pre-change snapshot failed; nothing was loaded (use --no-snapshot to skip)")

    remotes = {r.name: f"/var/tmp/jcs-{r.name}-{stamp}{local.suffix.lower()}" for r in routers}
    try:
        print(f"== Dry run: mode={mode}, file={local.name} ==")
        problems = False
        changed = []
        for r in routers:
            rc, _out, err = ssh.put(r, local, remotes[r.name])
            if rc != 0:
                raise Fail(f"{r.name}: scp failed: {err}")
            out = run_cli(ssh, r, load_script(mode, remotes[r.name], None, comment))
            print(f"--- {r.name} ---\n{out.strip()}\n")
            if ERROR_RE.search(out) or "configuration check succeeds" not in out:
                problems = True
                print(f"{r.name}: DRY RUN FAILED", file=sys.stderr)
            elif has_changes(out):
                changed.append(r)
            else:
                print(f"{r.name}: no changes")
        if problems:
            raise Fail("Dry run failed on at least one router; nothing was committed")
        if not args.commit:
            print("Dry run OK. Nothing committed. Re-run with --commit --confirm-minutes N to apply.")
            return 0
        if mode == "override":
            print("NOTE: override replaces the entire candidate configuration.")
        for r in changed:
            out = run_cli(ssh, r, load_script(mode, remotes[r.name], args.confirm_minutes, comment))
            print(f"--- {r.name} (commit confirmed {args.confirm_minutes}) ---\n{out.strip()}\n")
            if ERROR_RE.search(out) or "commit complete" not in out:
                raise Fail(f"{r.name}: commit did not complete; stopping. Verify state before continuing")
        names = " ".join(r.name for r in changed) or "(no router changed)"
        print(f"Committed with auto-rollback in {args.confirm_minutes} min. Verify, then run:\n"
              f"  python {SCRIPT.name} confirm {names}")
        return 0
    finally:
        for r in routers:
            try:
                ssh.run(r, f"file delete {remotes[r.name]}", timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                pass


def cmd_confirm(args):
    routers = select_routers(args, load_routers(args))
    ssh = Ssh(args.strict_hostkey)
    failures = 0
    for r in routers:
        out = run_cli(ssh, r, 'configure exclusive\ncommit comment "junos-config-save confirm"\nexit\n')
        ok = "commit complete" in out and not ERROR_RE.search(out)
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {r.name}\n{out.strip()}\n")
    return 1 if failures else 0


# ---------------------------------------------------------------- cli
_web_lock = threading.Lock()
_web_previews = {}
_web_pending = {}
WEB_SAVE_ROOT = Path("/home/jnpr/projects/$TOPONAME")


def web_action(body):
    """Run dashboard requests; caller must authenticate and enforce same origin."""
    inventory = Path("/home/jnpr/projects/$TOPONAME/router.json")
    requested = body.get("inventory", str(inventory))
    if requested != str(inventory):
        raise Fail("Execution currently requires the existing $TOPONAME router.json")
    action = body.get("action")
    if action == "browse":
        return {"ok": True, "files": browse_files(WEB_SAVE_ROOT),
                "output": f"Saved configurations under {WEB_SAVE_ROOT}/config/<identifier>/"}
    if action == "read":
        identifier = body.get("identifier")
        if not isinstance(identifier, str):
            raise Fail("Identifier must be text")
        path = saved_file(WEB_SAVE_ROOT, identifier, body.get("filename"))
        if path.stat().st_size > 10_000_000:
            raise Fail("Configuration is too large to open in the dashboard")
        return {"ok": True, "filename": path.name, "content": path.read_text(encoding="utf-8"),
                "output": f"Opened config/{identifier}/{path.name}"}
    names = body.get("routers")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise Fail("Select at least one router")
    known = load_routers(argparse.Namespace(routers_json=str(inventory)))
    if any(n not in known for n in names) or len(names) != len(set(names)):
        raise Fail("Unknown or duplicate router selection")
    saves = WEB_SAVE_ROOT
    common = ["--routers-json", str(inventory), "--output-dir", str(saves)]
    now = time.time()
    for token, state in list(_web_previews.items()):
        if state["expires"] < now:
            del _web_previews[token]

    def run(argv):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)] + argv, capture_output=True,
            text=True, timeout=900,
        )
        return {"ok": result.returncode == 0,
                "output": result.stdout + result.stderr}

    if action == "save":
        identifier = body.get("identifier") or f"save-{utc_stamp()}"
        if not isinstance(identifier, str):
            raise Fail("Identifier must be text")
        return run(["save"] + common + ["-i", clean_identifier(identifier),
                                       "-f", "set,stanza,json"] + names)
    if action == "preview":
        filename, content = body.get("filename"), body.get("content")
        if not isinstance(filename, str) or not isinstance(content, str) or not content.strip():
            raise Fail("Choose a nonempty .set or .config/.conf file")
        suffix = Path(filename).suffix.lower()
        if suffix not in (".set", ".config", ".conf"):
            raise Fail("Only SET or STANZA configuration files can be loaded")
        mode = body.get("mode") or ("set" if suffix == ".set" else "merge")
        if mode not in ("set", "merge") or (mode == "set") != (suffix == ".set"):
            raise Fail("Load mode must match the configuration format")
        with tempfile.TemporaryDirectory(prefix="jcs-web-") as folder:
            path = Path(folder) / ("change" + suffix)
            path.write_text(content, encoding="utf-8")
            result = run(["load"] + common + ["-F", str(path), "--mode", mode] + names)
        if result["ok"]:
            token = secrets.token_urlsafe(32)
            _web_previews[token] = dict(names=names, content=content, suffix=suffix,
                                        mode=mode, expires=now + 600)
            result["preview_token"] = token
        return result
    if action == "commit":
        minutes = body.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 120:
            raise Fail("Rollback timer must be 1-120 minutes")
        state = _web_previews.pop(body.get("token", ""), None)
        if not state or state["names"] != names:
            raise Fail("Preview expired or selection changed; preview again")
        with tempfile.TemporaryDirectory(prefix="jcs-web-") as folder:
            path = Path(folder) / ("change" + state["suffix"])
            path.write_text(state["content"], encoding="utf-8")
            result = run(["load"] + common + [
                "-F", str(path), "--mode", state["mode"],
                "--commit", "--confirm-minutes", str(minutes),
            ] + names)
        if result["ok"]:
            changed = re.findall(r"^--- ([A-Za-z0-9._-]+) \(commit confirmed \d+\) ---$",
                                 result["output"], re.M)
            if changed:
                token = secrets.token_urlsafe(32)
                _web_pending[token] = dict(names=changed, expires=time.time() + minutes * 60)
                result.update(confirm_token=token, routers=changed)
        return result
    if action == "confirm":
        state = _web_pending.pop(body.get("token", ""), None)
        if not state or state["expires"] <= now or state["names"] != names:
            raise Fail("No matching pending commit; verify router state")
        return run(["confirm"] + common + names)
    raise Fail("Unknown action")


def handle_web(handler):
    from urllib.parse import urlparse
    origin = handler.headers.get("Origin")
    if (not origin or urlparse(origin).netloc != handler.headers.get("Host")
            or handler.headers.get("X-Junos-Config-Save") != "1"):
        handler.send_json(403, {"error": "Same-origin dashboard requests only"})
        return
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= 2_000_000:
            raise Fail("Request must be between 1 byte and 2 MB")
        body = json.loads(handler.rfile.read(length))
        if not isinstance(body, dict):
            raise Fail("Request must be a JSON object")
        if not _web_lock.acquire(blocking=False):
            handler.send_json(409, {"error": "Another configuration operation is running"})
            return
        try:
            result = web_action(body)
        finally:
            _web_lock.release()
        handler.send_json(200 if result["ok"] else 422, result)
    except (Fail, ValueError, TypeError) as exc:
        handler.send_json(400, {"error": str(exc)})
    except subprocess.TimeoutExpired:
        handler.send_json(504, {"error": "Operation timed out. State may be partial; verify routers before retrying."})
    except OSError as exc:
        handler.send_json(500, {"error": f"Execution failed: {exc}"})


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--routers-json", help="router inventory (default: $JUNOS_CS_ROUTERS_JSON, existing $TOPONAME server router.json, or routers.json beside the script)")
    common.add_argument("--output-dir", help="project directory for config/<identifier>/ (default: $JUNOS_CS_OUTPUT_DIR or current working directory)")
    common.add_argument("--jobs", type=int, default=4, help="parallel routers for save (default 4)")
    common.add_argument("--strict-hostkey", action="store_true", default=True,
                        help="refuse unknown SSH host keys (always enabled by the CLI)")

    def targets(p):
        p.add_argument("routers", nargs="*", help="router names from the inventory, or 'all'")
        p.add_argument("--tag", action="append", help="also select routers with this tag (repeatable)")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("save", parents=[common], help="save router configs")
    targets(p)
    p.add_argument("-i", "--identifier", help="label (default save-<timestamp>)")
    p.add_argument("-f", "--format", default="set,stanza,json", help="stanza,set,json,xml in any mix, or 'all' (default set,stanza,json)")
    p.add_argument("--inheritance", choices=["auto", "yes", "no"], default="auto",
                   help="'display inheritance' (auto: stanza only)")
    p.add_argument("--router-copy", action="store_true", help="also save a copy in /var/tmp on the router")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("list", parents=[common], help="list saved snapshots")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("diff", parents=[common], help="diff a saved file against live or another saved file")
    p.add_argument("identifier")
    p.add_argument("file")
    p.add_argument("--against", help="second saved file in the same identifier")
    p.add_argument("--inheritance", choices=["auto", "yes", "no"], default="auto")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("load", parents=[common], help="load a .set/.config file (dry run unless --commit)")
    targets(p)
    p.add_argument("-F", "--file", required=True, help="local .set (load set) or .config (load merge/override)")
    p.add_argument("--mode", choices=["set", "merge", "override"], help="default: set for .set, merge for .config")
    p.add_argument("--commit", action="store_true", help="really commit (requires --confirm-minutes)")
    p.add_argument("--confirm-minutes", type=int, help="'commit confirmed N' auto-rollback window (1-120)")
    p.add_argument("--comment", help="commit comment")
    p.add_argument("--no-snapshot", action="store_true", help="skip the automatic pre-change save")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("confirm", parents=[common], help="confirm a pending 'commit confirmed'")
    targets(p)
    p.set_defaults(func=cmd_confirm)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--askpass"]:
        sys.stdout.write(os.environ.get(ENV_PASSWORD, "") + "\n")
        return 0
    args = build_parser().parse_args(argv)
    if args.command in ("save", "load", "confirm") and not args.routers and not args.tag:
        build_parser().error(f"{args.command}: give router names, 'all' or --tag")
    try:
        return args.func(args)
    except Fail as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"ERROR: timed out: {exc.cmd[0] if exc.cmd else ''}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
