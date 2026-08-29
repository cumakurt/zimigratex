# zimigrate

[Türkçe dokümantasyon](README.tr.md)

`zimigrate` creates a resumable local export of Zimbra provisioning data
and mailbox content, then imports the manually transferred directory on the local
destination Zimbra server. Export and import probe `zmprov`, `zmmailbox`, and
`zmcontrol` instead of requiring a single Zimbra release. It does not connect to
another server with SSH and never starts an import after export.

## Simple workflow

Install `zimigrate` on both Zimbra servers. Copy the whole repository, including the
`vendor/` directory. `export.sh` and `import.sh` use that directory first: a pinned
CPython 3.12 runtime, pip wheels, and a CA bundle. They install OS Python packages or
download from GitHub only when `vendor/` is missing. `export_data` is created in the
current working directory, so change to a volume with enough free space and run:

```bash
/path/to/zimigratex/export.sh
```

The command exports all selected data from the local
Zimbra installation, and creates:

```text
./export_data/
```

If the process is interrupted, `Ctrl+C` stops in-flight Zimbra commands immediately.
Rerun `./export.sh` or `zimigrate export` from the same directory to resume.
Successful units are skipped and incomplete units resume. Export completion never
starts an import.

## Backup a domain or one account

`export.sh` and `import.sh` pass extra arguments to `zimigrate`. Use `--user` or
`--domain` (repeatable, comma-separated values allowed) to limit work to one mailbox
or one domain and the objects it needs. A scoped run skips the category prompt and
does not copy global or per-server configuration.

```bash
./export.sh --user user@example.com
./export.sh --domain example.com
./export.sh --archive ./backup_example --domain example.com
```

Import can apply the same filter to a **full** archive, so you can export everything
once and restore a single mailbox later:

```bash
./import.sh --user user@example.com
./import.sh --domain example.com
```

`--user` restores that account, its domain, its COS, and its mailbox. `--domain`
also restores the domain's alias domains, accounts, and distribution lists. Resume the
same scoped command; changing `--user`/`--domain` mid-archive requires a new archive
directory (export) or a copied archive with a fresh `state.sqlite3` (import).

After the export process has stopped, manually transfer the entire `export_data`
directory to the destination. Preserve permissions and files such as
`export_data/manifest.json`. For example, with removable
or mounted storage:

```bash
cp -a export_data /mnt/transfer/
```

Before exporting, `zimigrate` displays domains/COS, accounts, mailbox content,
distribution lists, global settings, and server settings for operator selection. Account
and mailbox selections automatically include dependencies. Local `zmprov gqu` usage,
archive growth, worker temporary space, and a safety reserve are checked; insufficient
space aborts before data is written. The report is
`export_data/reports/export-disk-assessment.json`.

On the destination server, place the copied directory in the current working directory:

```text
./export_data/
```

Then run:

```bash
/path/to/zimigratex/import.sh
```

The command performs a complete validation before any
destination change. It reads every record, checks SHA-256 values, scans every ZIP/TGZ
mailbox archive, and verifies manifest counts. Only after all checks pass does it run
destination command preflight and begin the local import. A validation failure exits
without constructing the importer or changing the destination.

After validation, import displays the archive categories for selection. Global and server
settings require their explicit allowlists. Before any target mutation, current
`zmvolume -l` message/index volumes and temporary space are checked. Insufficient space
aborts the import and writes `export_data/reports/import-disk-assessment.json`.

Interrupted imports resume with the same command. A successful import automatically
compares destination objects, portable attributes, aliases, identities, signatures,
data sources, distribution members, and mailbox checkpoints with the archive:

```bash
./import.sh
```

The same target verification can be repeated independently:

```bash
zimigrate verify-target
```

Incomplete accounts remain in `maintenance` to prevent concurrent user writes. Their
source status is restored only after all metadata and mailbox stages succeed. Account
creates and password hashes are written with `zmprov -l` (LDAP-direct) so Zimbra does
not re-hash an `{SSHA}` value. After that restore, zimigrate runs SOAP `zmprov fc account`
so mailboxd drops its cached LDAP entry instead of serving the empty password or
`maintenance` status until `ldap_cache_account_maxage` (default 15 minutes) expires.

## What is exported

- domains, alias domains, and domain attributes;
- classes of service (COS), with source IDs remapped to destination IDs;
- accounts and calendar resources, including password hashes, aliases, preferences,
  forwarding/filter attributes, identities, signatures, and supported data sources;
- static and dynamic distribution lists, aliases, attributes, and static members;
- mail, calendars, contacts, tasks, and Briefcase content through Zimbra REST ZIP/TGZ;
- a protected snapshot of global and per-server LDAP configuration.

Live authentication tokens are never exported. System account metadata is archived,
but system mailboxes and destination service identities are excluded by default because
overwriting installation-owned accounts can break Zimbra. Global and per-server
configuration is also archived but is applied only through an explicit attribute
allowlist. These topology-specific values can contain host names, certificates, server
IDs, ports, and LDAP/MTA settings that are unsafe to copy blindly.

Zimbra stores four data-source credential fields encoded against the source
`zimbraDataSourceId`. Export decodes those fields using Zimbra's long-standing
LDAP encoding only inside the process, writes the plaintext into the archive,
and lets the destination encrypt it against its newly generated data-source ID. Copying
the LDAP ciphertext directly would create unusable credentials.

## Requirements

- Python 3.11 or newer and the Python `rich` package;
- a 64-bit x86_64 glibc Linux host that Zimbra FOSS supports (RHEL 7–9, Ubuntu 18.04–24.04 LTS, Oracle Linux, Rocky Linux);
- `zimigrate` installed on both the source and destination servers;
- `/opt/zimbra/bin/zmprov`, `zmmailbox`, `zmcontrol`, and `zmhostname` locally available;
- execution as the `zimbra` user, or local `sudo -n -u zimbra` permission;
- enough archive space plus temporary space for one plaintext mailbox chunk per worker;
- a local Zimbra FOSS installation whose administrative commands pass preflight.

Install from the repository root (the directory that contains `pyproject.toml`,
not `src/zimigrate`). The wrapper scripts are the supported operator path:

```bash
/path/to/zimigratex/export.sh
/path/to/zimigratex/import.sh
```

If Python 3.11+, a virtualenv, and a current installation of the repository source are
already present, the scripts skip OS package installation and env setup. Source changes
invalidate the runtime stamp and trigger a package reinstall, preventing an old `.venv`
from silently running stale migration code. Extra arguments are passed through, for
example `./export.sh --archive /srv/migration/export_data`.

Copy `vendor/` with the repository. The wrappers extract the vendored x86_64 glibc
CPython archive from `vendor/python/`, install `rich` from `vendor/wheels/` without
contacting PyPI, and use `vendor/certs/cacert.pem` when a download is still required.
Refresh those files on a machine with internet access:

```bash
./scripts/vendor-runtime.sh
```

If `vendor/` is absent and OS Python 3.11+ cannot be installed, the wrappers download
the same pinned CPython 3.12 runtime, verify its SHA-256 digest, and continue. Missing
optional packages such as `python3-venv` no longer block `ca-certificates`. If the
mail host's TLS trust store cannot verify GitHub, the download retries after installing
CA certificates and, as a last resort, without TLS verification; the pinned SHA-256
digest still rejects a substituted archive. If a
virtualenv still cannot be created, they run `zimigrate` from the repository `src/`
tree after installing `rich` next to `.runtime/`.

Manual install remains available:

```bash
cd /path/to/zimigratex
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Export and import draw a live status panel on an interactive terminal. The panel
stays on one screen and updates host, inventory, disk capacity, and per-phase
progress in place instead of printing a new `info:` line for every object.
`--verbose`, `--json-logs`, a non-TTY, `TERM=dumb`, or `ZIMIGRATE_PLAIN_OUTPUT=1`
restore the classic line-oriented logs. `status` and `preflight` still print JSON.

No configuration file is needed for the default workflow. The source and destination
commands always execute against the local Zimbra installation. Multi-mailbox source
content may still be routed by Zimbra to an account's `zimbraMailHost` over its admin
REST port; this is not SSH command execution.

## Status and standalone validation

All commands use `./export_data` by default:

```bash
zimigrate status
zimigrate verify --deep
zimigrate verify-target
```

`verify --deep` performs the same archive-level data validation that import always
performs automatically.

## Optional advanced configuration

The no-argument workflow uses safe defaults: all normal accounts are selected, mailbox
content and visible secret hashes are included, eight bounded workers are used, existing
objects are merged, and mailbox conflicts are skipped. Use
[config.example.toml](config.example.toml) only when those policies must change:

```bash
cp config.example.toml migration.toml
zimigrate export --config migration.toml
zimigrate import --config migration.toml
```

Configuration cannot enable remote execution. Both commands remain local. Useful
advanced settings include:

- worker count, retries, timeout, account include/exclude patterns;
- `--user` / `--domain` for one-mailbox or one-domain backup and restore;
- full or year-chunk mailbox exports and ZIP/TGZ format;
- destination mailhost mapping for multi-mailbox installations;
- existing-object and mailbox-conflict policies;
- reviewed allowlists for global or per-server attributes.

Strict attribute application is enabled by default. A target schema rejection stops the
import instead of reporting success with missing preferences or filters. Set
`strict_attributes = false` only after reviewing the generated warning report; service,
connection, and timeout errors are never downgraded to attribute warnings. Year chunks
use numeric UTC epoch boundaries so their ranges neither overlap nor depend on account
locale. With `mailbox_conflict_resolution = "reset"`, only the first chunk resets the
mailbox; later chunks use the idempotent `skip` policy so an earlier chunk is not erased.

`--archive` is also retained for advanced layouts:

```bash
zimigrate export --archive /srv/migration/export_data
zimigrate import --archive /srv/migration/export_data
```

When an advanced config changes import behavior, manually copy that config to the
destination and pass it again. Import policies are bound to the first import checkpoint;
they cannot silently change during a resume.

## Reliability and security

The archive uses SHA-256 checksums, atomic writes, `0600` files, a `0700` directory,
and SQLite checkpoints. Records and mailbox payloads are stored in plaintext. Worker
submission and execution are bounded, and only classified transient command failures use
limited exponential retries. Temporary mailbox chunks exist only under `export_data/.tmp`
while being processed and are removed afterward. Sensitive `zmprov` values use stdin
batch input instead of process arguments.

Do not copy an archive while `zimigrate` is running. Keep the directory on trusted,
preferably disk-encrypted storage. A copied archive contains account names, password
hashes when secret export is enabled, mailbox content, and checkpoint metadata.
See [SECURITY.md](SECURITY.md) for details.

## Known boundaries

- This is an application-level migration, not a physical LDAP/MariaDB/blob restore.
- Certificates, private key files, `zmlocalconfig`, OS packages, MTA queues, DNS,
  firewall rules, and commercial Network Edition backup state are not installed.
- Cross-mailbox share IDs and topology-specific IDs can change. Validate delegated
  folders and recreate grants when necessary.
- A live source can change during export. Use a maintenance/freeze window for the final
  run; mailbox REST export is not a cluster-wide transactional snapshot.
- Import disk assessment uses each archive member's expanded size, not only its compressed
  ZIP/TGZ size. In a multi-mailbox destination, paths on a remote mailbox host cannot be
  measured through the local filesystem; confirm free space on every mapped host before
  cutover.
- Destination version pinning is optional. By default any local Zimbra release that can
  run `zmprov`/`zmmailbox`/`zmcontrol` is accepted; set
  `import.expected_target_version_pattern` only when the operator wants to require a
  specific `zmcontrol -v` string.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
ruff check src tests
ruff format --check src tests
bandit -q -r src
python -m compileall -q src tests
```

## Author

Cuma KURT

- GitHub: [https://github.com/cumakurt/zimigratex](https://github.com/cumakurt/zimigratex)
- LinkedIn: [https://www.linkedin.com/in/cuma-kurt-34414917/](https://www.linkedin.com/in/cuma-kurt-34414917/)

## License

Copyright (C) 2026 Cuma KURT.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, version 3 only. See [LICENSE](LICENSE).
