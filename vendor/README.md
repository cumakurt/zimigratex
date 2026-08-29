# Vendored runtime

This directory is copied with the repository so `export.sh` and `import.sh` can
bootstrap on a Zimbra host that has no Python 3.11+, no `python3-venv`, and no
working OS CA store.

Zimbra FOSS is distributed for 64-bit x86_64 glibc Linux (RHEL, Ubuntu LTS,
Oracle Linux, Rocky Linux). Contents:

- `python/` pinned CPython 3.12.14 `x86_64-unknown-linux-gnu` archive (glibc 2.17+)
- `wheels/` pure-Python pip wheels for `rich` and packaging tools
- `certs/cacert.pem` Mozilla CA bundle extracted from `certifi`

`scripts/bootstrap.sh` uses these files before any network download. Refresh with:

```bash
./scripts/vendor-runtime.sh
```
