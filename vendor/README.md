# Vendored runtime

This directory is copied with the repository so `export.sh` and `import.sh` can
bootstrap on a Zimbra host that has no Python 3.11+, no `python3-venv`, and no
working OS CA store.

Contents:

- `python/` pinned CPython 3.12.14 standalone archives (linux gnu/musl, x86_64/aarch64)
- `wheels/` binary pip wheels for `cryptography`, `rich`, and their dependencies
- `certs/cacert.pem` Mozilla CA bundle extracted from `certifi`

`scripts/bootstrap.sh` uses these files before any network download. Refresh with:

```bash
./scripts/vendor-runtime.sh
```
