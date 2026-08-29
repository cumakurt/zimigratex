#!/usr/bin/env bash
# Populate vendor/ with the standalone Python runtimes, CA bundle, and pip wheels
# used by export.sh and import.sh on hosts without OS Python or outbound TLS.
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
    "cryptography>=42"
    "rich>=13.9,<16"
    "setuptools>=69"
    wheel
    pip
    certifi
)
PIP_PLATFORMS=(
    manylinux2014_x86_64
    manylinux2014_aarch64
    manylinux_2_17_x86_64
    manylinux_2_17_aarch64
    manylinux_2_28_x86_64
    manylinux_2_28_aarch64
    musllinux_1_1_x86_64
    musllinux_1_1_aarch64
    musllinux_1_2_x86_64
    musllinux_1_2_aarch64
)

download_python_runtimes() {
    local target name digest destination
    mkdir -p "$PYTHON_VENDOR"
    for target in \
        x86_64-unknown-linux-gnu \
        aarch64-unknown-linux-gnu \
        x86_64-unknown-linux-musl \
        aarch64-unknown-linux-musl; do
        name=$(standalone_archive_name "$target")
        digest=$(standalone_digest "$target")
        destination=$PYTHON_VENDOR/$name
        if [[ -f $destination ]] && verify_sha256 "$destination" "$digest"; then
            log "Vendored Python already present: $name"
            continue
        fi
        log "Downloading $name"
        download_url "$STANDALONE_BASE_URL/$name" "$destination.partial"
        if ! verify_sha256 "$destination.partial" "$digest"; then
            rm -f "$destination.partial"
            die "Checksum mismatch for $name"
        fi
        mv "$destination.partial" "$destination"
    done
}

download_wheels() {
    local platform
    mkdir -p "$WHEEL_VENDOR"
    log "Downloading pip wheels for CPython 3.12"
    python3 -m pip download \
        --dest "$WHEEL_VENDOR" \
        --only-binary=:all: \
        --python-version 312 \
        --implementation cp \
        --abi cp312 \
        "${PIP_PACKAGES[@]}"
    for platform in "${PIP_PLATFORMS[@]}"; do
        log "Downloading pip wheels for $platform"
        python3 -m pip download \
            --dest "$WHEEL_VENDOR" \
            --only-binary=:all: \
            --python-version 312 \
            --implementation cp \
            --abi cp312 \
            --platform "$platform" \
            "${PIP_PACKAGES[@]}" || \
            log "No binary wheels for $platform; skipping"
    done
    prune_incompatible_wheels
}

prune_incompatible_wheels() {
    local wheel name
    shopt -s nullglob
    for wheel in "$WHEEL_VENDOR"/*.whl; do
        name=$(basename "$wheel")
        case $name in
            *[0-9]b[0-9]* | *[0-9]a[0-9]* | *[0-9]rc[0-9]*)
                log "Removing pre-release wheel $name"
                rm -f "$wheel"
                continue
                ;;
            *-cp313-* | *-cp314-* | *-cp311-* | *-cp310-* | *-cp39-* | *-cp38-*)
                if [[ $name == *-abi3-* ]]; then
                    continue
                fi
                log "Removing wheel not used by standalone CPython 3.12: $name"
                rm -f "$wheel"
                continue
                ;;
        esac
    done
    shopt -u nullglob
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
download_python_runtimes
download_wheels
extract_ca_bundle
write_checksums
log "Vendor directory is ready at $VENDOR"
