# junos-config-save

**Author:** Carlos Patriawan / HPE JNPR

Save, compare, and load Junos configurations over SSH using a single Python
file. An optional HTML dashboard provides router selection, automatic backups,
saved-file browsing, and a preview-first configuration load workflow.

The CLI uses Python's standard library and the system OpenSSH tools. It does
not require NETCONF, PyEZ, Paramiko, or a Python package installation.

## Features

| Capability | Behavior |
|---|---|
| Automatic backups | Saves SET, STANZA, and JSON for each selected router by default |
| Flexible CLI formats | Select any combination of SET, STANZA, JSON, and XML |
| Multiple routers | Select names, all routers, or tags; save routers in parallel |
| Organized snapshots | Groups files and a manifest by identifier |
| Configuration comparison | Compare saved files with each other or with live configuration |
| Load preview | Load candidate configuration, display differences, run commit check, and roll back |
| Confirmed commit | Explicitly apply a validated change with an automatic rollback timer |
| Permanent confirmation | Confirm the pending change after checking router health |
| Dashboard Browse | List saved files, download them, or select SET/STANZA files for Load |
| Inventory authentication | Use per-router/default credentials or SSH keys |
| Host identity checking | Refuse unknown or changed SSH host keys by default |

**Save does not change the router's configuration.** Load previews modify the
candidate configuration temporarily, use an exclusive configuration lock, and
roll back without committing. They should still be scheduled appropriately.

## Requirements

- Python **3.10 or later**.
- System `ssh` and `scp` executables available on `PATH`.
- SSH connectivity to the routers and an account with the required Junos permissions.
- Trusted router host keys already installed in the executing account's
  `known_hosts`, with fingerprints verified through an independent channel.
- For password authentication, OpenSSH with `SSH_ASKPASS_REQUIRE` support
  (OpenSSH 8.4 or later) and a working askpass helper mechanism.

The CLI is intended for Windows and Unix-like systems. Password authentication
depends on the installed OpenSSH implementation; validate it in your environment
before a production rollout.

## Quick start

1. Download the project files.
2. Create your private router inventory outside version control.
3. Verify and install trusted router SSH host keys.
4. Run a read-only save against one router first.

```powershell
python config_save.py --help
python config_save.py save --routers-json "C:\private\routers.json" -i baseline router-a
```

This saves SET, STANZA, and JSON automatically. There is no dependency installation
step. A separate inventory file is required; no real or sample inventory files
are included in the repository.

## Inventory

The inventory location is selected in this order:

1. `--routers-json PATH`.
2. The `JUNOS_CS_ROUTERS_JSON` environment variable.
3. The existing `$TOPONAME` server inventory, if present on a Unix-like host.
4. `routers.json` beside the Python script.

Three JSON structures are supported:

| Structure | Fields |
|---|---|
| Flat map | Router names mapped to host strings |
| Nested map | `routers` maps names to host strings or router objects; optional `defaults` provides shared settings |
| Device inventory | `devices` is a list; only entries with `type` equal to `junos` are selected |

Nested router objects support:

| Field | Purpose |
|---|---|
| `host` | IP address or hostname |
| `user` | SSH username; falls back to `defaults.user`, then `jnpr` |
| `password` | SSH password; falls back to `defaults.password`; empty means use SSH keys |
| `port` | SSH port; falls back to `defaults.port`, then 22 |
| `tags` | List of labels for router selection |

Device inventories use `name` and `mgmt_ip`. An optional `login` string uses
`username/password`, split at the first slash. Explicit device `user`, `password`,
and `port` override the corresponding login/default values. Non-Junos hosts and
testers are excluded.

Keep the inventory private and restrict its filesystem permissions. Passwords
are supplied to SSH through an askpass helper and a child-process environment
variable, not command-line arguments. The helper does not write passwords to disk.
This does not protect against privileged inspection of the executing machine.

## Save layout

```text
<project-directory>/
  config/
    <identifier>/
      router-a.<identifier>.<UTC-timestamp>.set
      router-a.<identifier>.<UTC-timestamp>.config
      router-a.<identifier>.<UTC-timestamp>.json
      manifest.json
```

The manifest records filenames, formats, sizes, and save times. Reusing an
identifier keeps timestamped files and updates its manifest.

The base project directory is selected by `--output-dir`, then
`JUNOS_CS_OUTPUT_DIR`, otherwise the shell's current working directory.
`config/<identifier>/` is appended automatically.

For the `$TOPONAME` dashboard deployment:

```text
/home/jnpr/projects/$TOPONAME/config/$IDENTIFIER/
```

`$TOPONAME` is a literal deployment placeholder, not a runtime environment-variable
lookup. Replace it in the Python, HTML, and tests with your topology name before
deployment, including the matching portal route. For CLI use in another project,
pass its directory as `--output-dir`.

## CLI workflows

Examples assume the inventory is beside the script or configured through the
environment. Replace router names and paths with your own.

### Save

```powershell
# Automatic SET, STANZA, and JSON backup
python config_save.py save -i baseline router-a router-b

# Select all inventory routers
python config_save.py save -i maintenance all

# Select by tag and control parallelism
python config_save.py save -i core-backup --tag core --jobs 4

# Optional CLI format override, including XML
python config_save.py save -i archive -f all router-a

# Use a specific project directory
python config_save.py save --output-dir "C:\backups\project-a" -i baseline router-a
```

Additional options:

- `--inheritance auto|yes|no`: controls `display inheritance`; `auto` includes it
  for STANZA only.
- `--router-copy`: also saves a copy under `/var/tmp` on each router.
- Omit `-i` to generate a `save-<timestamp>` identifier.

### List and compare

```powershell
python config_save.py list

# Replace the filename with an actual file from list
python config_save.py diff baseline router-a.baseline.20260101_120000.set

# Compare two saved files within the same identifier
python config_save.py diff baseline router-a.baseline.20260101_120000.set --against router-a.baseline.20260102_120000.set
```

Without `--against`, diff fetches live configuration in the saved file's format.
Only the current `config/<identifier>/` layout is listed; older layouts are not
automatically migrated.

### Preview a load

```powershell
python config_save.py load -F change.set router-a
```

The default workflow:

1. Take a pre-change snapshot in STANZA and SET.
2. Copy the change file to the router.
3. Enter `configure exclusive`.
4. Load the candidate, show the comparison, and run `commit check`.
5. Roll back the candidate and exit without committing.

`.set` files use `load set`; `.config` and `.conf` files use `load merge`.
JSON and XML are archive formats only and cannot be loaded by this tool.

### Apply with automatic rollback

```powershell
python config_save.py load -F change.set router-a --commit --confirm-minutes 5

# Only after independently checking router health and connectivity
python config_save.py confirm router-a
```

Both `--commit` and `--confirm-minutes` are required for an actual commit.
The timer accepts 1-120 minutes. Each selected router must pass the initial
preview before any commit is attempted.

Important limits:

- Commits across multiple routers are **not atomic**. A later failure can leave
  earlier routers with a pending confirmed commit.
- On a failure or timeout, inspect router state before retrying or confirming.
- The tool checks CLI response text; a successful commit is not a network-health test.
- CLI `--mode override` explicitly replaces the entire candidate configuration.
  The dashboard does not offer override.
- CLI `--no-snapshot` bypasses pre-change backups; use cautiously.
- Temporary router upload files are cleaned up on completion where possible.

## HTML dashboard

### User workflow

1. Open the deployed dashboard and authenticate to the portal.
2. Set the inventory location and click **Load settings**.
3. Select router checkboxes.
4. Choose **save**, enter an identifier, and click **Submit Save / Preview Load**.
   All three backup formats are saved automatically.
5. Click **Browse saved configurations** to list or download files.
6. To load, select **Use for Load** on a saved SET/STANZA file, or upload a local
   configuration file and choose a Load action.
7. Submit the preview and review the differences and commit-check results.
8. Use **Commit previewed load** only after reviewing the preview and rollback timer.
9. Verify router health, then use **Confirm pending commit permanently** if appropriate.

JSON files can be downloaded but not selected for Load. Deselecting every router
does not silently select all routers.

The username/password settings fields generate a downloadable defaults file;
they do not override the server's execution inventory. Inventory passwords are
not displayed, and passwords are not stored in browser storage.

### Deployment boundary

The CLI works independently. The HTML is **not a standalone web server**.
Opening it as a local file supports command building and local inventory
inspection, but cannot execute router operations.

Execution requires integration into an existing authenticated portal:

- The current page calls `/$TOPONAME/dashboard/api/config-v22`.
- After authenticating and authorizing the request, the portal dispatches it to
  `config_save.handle_web(handler)`.
- The handler must expose `headers`, `rfile`, and `send_json(status, payload)`.
- The adapter requires a matching Origin/Host and a custom request header.
  These checks supplement authentication; they do not replace it.
- The deployment template uses a fixed `$TOPONAME` inventory and project root.
  A different portal requires adapting those integration points; the CLI's
  `--routers-json` and `--output-dir` do not configure the dashboard.
- Browsers can read a selected local inventory or same-origin JSON URL, not
  arbitrary server filesystem paths.
- Successful previews produce single-use tokens that expire after ten minutes
  or a portal restart. Pending confirmation state is also in memory.
- Concurrent dashboard operations are rejected rather than run together.

The private portal authentication implementation and its original backend code
are deliberately not part of this repository.

## Security and publication

- Use verified SSH host keys. Unknown or changed keys fail explicitly.
- Use HTTPS for the portal. HTTP Basic authentication and configuration payloads
  are not encrypted over HTTP; the existing isolated lab uses HTTP.
- Backups, load comparisons, downloaded files, and operation output may contain
  credentials or other sensitive configuration. **Automatic secret scrubbing
  is not implemented.**
- Protect saved configurations and inventory URLs with appropriate portal
  authorization and filesystem permissions.
- Do not expose the portal adapter without authentication.
- No router operation runs merely by opening the page.

The Git ignore file is a publication allowlist. Only these files are eligible
for an ordinary `git add`:

```text
.gitignore
README.md
config_save.py
config-save.html
test_config_save.py
```

All router inventories, including samples, saved configurations, manifests,
internal reference code, caches, and backups are excluded. Do not force-add
excluded files. Before publishing, inspect both the file list and content:

```powershell
git diff --cached --name-only
git diff --cached
```

Ignore rules do not remove previously tracked files. A security review and
passing tests are not a guarantee of production security.

## Tests and troubleshooting

Run the offline suite without connecting to any routers:

```powershell
python -B -m unittest -v test_config_save
```

Tests cover inventory parsing, automatic formats and layout, saved-file access,
strict host-key defaults, and the dashboard's preview/commit/confirm flow using
simulated SSH or subprocess execution. They do not prove compatibility with
every Junos or OpenSSH release.

| Symptom | Check |
|---|---|
| Host key verification fails | Independently verify the fingerprint and install the trusted key for the executing account |
| Password login fails | Inventory credentials, OpenSSH askpass support, and router authentication policy |
| Dashboard returns 401 | Portal login and authorization |
| Dashboard returns 403 | Origin/Host forwarding and required request headers |
| Preview token is rejected | Preview again after a selection change, expiry, or server restart |
| Browse shows no files | Project directory and the `config/<identifier>/` layout |
| Load or commit times out | Inspect actual router state before any retry |

CLI exit codes: `0` success, `1` router-operation failure, `2` argument or validation
error. Use `python config_save.py <command> --help` for all command options.
