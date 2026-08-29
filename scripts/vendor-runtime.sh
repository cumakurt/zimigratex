#!/usr/bin/env bash
# Populate vendor/ with the x86_64 glibc CPython runtime, CA bundle, and pip
# wheels used by export.sh and import.sh on Zimbra-supported Linux hosts.
# shellcheck shell=bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/bootstrap.sh
. "$ROOT/scripts/bootstrap.sh"

VENDOR=$ROOT/vendor
PYTHON_VENDOR=$VENDOR/python
WHEEL_VENDOR=$VENDOR/wheels
CERT_VENDOR=$VENDOR/certs
PIP_PACKAGES=(
    "rich>=13.9,<16"
    "setuptools>=69"
    wheel
    pip
    certifi
)

download_python_runtime() {
    local name digest destination
    mkdir -p "$PYTHON_VENDOR"
    name=$(standalone_archive_name x86_64-unknown-linux-gnu)
    digest=$(standalone_digest x86_64-unknown-linux-gnu)
    destination=$PYTHON_VENDOR/$name
    if [[ -f $destination ]] && verify_sha256 "$destination" "$digest"; then
        log "Vendored Python already present: $name"
        return 0
    fi
    log "Downloading $name"
    download_url "$STANDALONE_BASE_URL/$name" "$destination.partial"
    if ! verify_sha256 "$destination.partial" "$digest"; then
        rm -f "$destination.partial"
        die "Checksum mismatch for $name"
    fi
    mv "$destination.partial" "$destination"
}

download_wheels() {
    mkdir -p "$WHEEL_VENDOR"
    log "Downloading pure-Python pip wheels"
    python3 -m pip download \
        --dest "$WHEEL_VENDOR" \
        --only-binary=:all: \
        --python-version 312 \
        "${PIP_PACKAGES[@]}"
}

extract_ca_bundle() {
    local wheel
    mkdir -p "$CERT_VENDOR"
    shopt -s nullglob
    for wheel in "$WHEEL_VENDOR"/certifi-*.whl; do
        python3 - "$wheel" "$CERT_VENDOR/cacert.pem" <<'PY'
import sys, zipfile
from pathlib import Path
wheel, destination = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(wheel) as archive:
    payload = archive.read("certifi/cacert.pem")
destination.write_bytes(payload)
PY
        log "Wrote CA bundle from $(basename "$wheel")"
        shopt -u nullglob
        return 0
    done
    shopt -u nullglob
    die "certifi wheel was not downloaded; cannot write vendor/certs/cacert.pem"
}

write_checksums() {
    (
        cd "$VENDOR"
        find python wheels certs -type f ! -name SHA256SUMS | sort | xargs sha256sum
    ) >"$VENDOR/SHA256SUMS"
}

mkdir -p "$VENDOR"
download_python_runtime
download_wheels
extract_ca_bundle
write_checksums
log "Vendor directory is ready at $VENDOR"
