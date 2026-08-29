# Shared runtime bootstrap for export.sh and import.sh.
# shellcheck shell=bash

STANDALONE_RELEASE=20260825
STANDALONE_PYTHON_VERSION=3.12.14
STANDALONE_BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${STANDALONE_RELEASE}"
RUNTIME_DIR="${RUNTIME_DIR:-}"
STANDALONE_PYTHON="${STANDALONE_PYTHON:-}"
SITE_PACKAGES="${SITE_PACKAGES:-}"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-}"
RUNTIME_MODE="${RUNTIME_MODE:-}"

log() {
    printf 'info: %s\n' "$*" >&2
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

runtime_paths() {
    RUNTIME_DIR=$ROOT/.runtime
    STANDALONE_PYTHON=$RUNTIME_DIR/python/bin/python3
    SITE_PACKAGES=$RUNTIME_DIR/pydeps
}

python_is_usable() {
    local binary=$1
    local resolved=$binary
    if [[ ! -x $resolved ]]; then
        resolved=$(command -v "$binary") || return 1
    fi
    "$resolved" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1
}

python_has_venv() {
    local binary=$1
    "$binary" -c 'import venv, ensurepip' >/dev/null 2>&1
}

python_has_migration_imports() {
    local binary=$1
    local pythonpath=$2
    PYTHONPATH=$pythonpath "$binary" -c 'import zimigrate, cryptography, rich' >/dev/null 2>&1
}

source_pythonpath() {
    if [[ -d ${SITE_PACKAGES-} ]]; then
        printf '%s:%s\n' "$ROOT/src" "$SITE_PACKAGES"
    else
        printf '%s\n' "$ROOT/src"
    fi
}

find_system_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        if python_is_usable "$candidate" && python_has_venv "$candidate"; then
            printf '%s\n' "$(command -v "$candidate")"
            return 0
        fi
    done
    for candidate in python3.13 python3.12 python3.11 python3; do
        if python_is_usable "$candidate"; then
            printf '%s\n' "$(command -v "$candidate")"
            return 0
        fi
    done
    return 1
}

source_is_newer_than() {
    local stamp=$1
    [[ -f $stamp ]] || return 0
    [[ $ROOT/pyproject.toml -nt $stamp ]] && return 0
    find "$ROOT/src" -type f -name '*.py' -newer "$stamp" -print -quit | grep -q .
}

zimigrate_is_ready() {
    local python=$ROOT/.venv/bin/python
    local stamp=$ROOT/.venv/.zimigrate-source-stamp
    [[ -x $python ]] || return 1
    python_is_usable "$python" || return 1
    "$python" -c 'import zimigrate, cryptography, rich' >/dev/null 2>&1 || return 1
    [[ -x $ROOT/.venv/bin/zimigrate ]] || return 1
    [[ -f $stamp ]] || return 1
    if source_is_newer_than "$stamp"; then
        return 1
    fi
}

source_runtime_is_ready() {
    local python=${SYSTEM_PYTHON:-}
    runtime_paths
    if [[ -z $python ]]; then
        if [[ -x $STANDALONE_PYTHON ]] && python_is_usable "$STANDALONE_PYTHON"; then
            python=$STANDALONE_PYTHON
        elif python=$(find_system_python); then
            :
        else
            return 1
        fi
    fi
    python_is_usable "$python" || return 1
    python_has_migration_imports "$python" "$(source_pythonpath)"
}

run_root() {
    if [[ $(id -u) -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        log "root or sudo is required to install OS packages"
        return 1
    fi
}

detect_os() {
    OS_ID=unknown
    OS_LIKE=
    if [[ -f /etc/os-release ]]; then
        # shellcheck source=/dev/null
        . /etc/os-release
        OS_ID=${ID:-unknown}
        OS_LIKE=${ID_LIKE:-}
    fi
}

install_apt_packages() {
    local package
    log "Installing OS packages with apt ($OS_ID)"
    run_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq || return 1
    run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip ca-certificates || return 1
    for package in python3.12 python3.12-venv python3.11 python3.11-venv; do
        run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            "$package" >/dev/null 2>&1 || true
    done
}

install_dnf_packages() {
    local installer=dnf
    command -v dnf >/dev/null 2>&1 || installer=yum
    log "Installing OS packages with $installer ($OS_ID)"
    run_root "$installer" install -y python3 python3-pip ca-certificates || return 1
    run_root "$installer" install -y python3.12 python3.12-pip >/dev/null 2>&1 || true
    run_root "$installer" install -y python3.11 python3.11-pip >/dev/null 2>&1 || true
}

install_zypper_packages() {
    log "Installing OS packages with zypper ($OS_ID)"
    run_root zypper --non-interactive install -y python3 python3-pip python3-venv \
        gcc libffi-devel libopenssl-devel ca-certificates || \
        run_root zypper --non-interactive install -y python311 python311-pip python311-devel \
            gcc libffi-devel libopenssl-devel ca-certificates || return 1
}

install_pacman_packages() {
    log "Installing OS packages with pacman ($OS_ID)"
    run_root pacman -Sy --noconfirm python python-pip python-virtualenv gcc libffi \
        openssl ca-certificates || return 1
}

install_apk_packages() {
    log "Installing OS packages with apk ($OS_ID)"
    run_root apk add --no-cache python3 py3-pip py3-virtualenv gcc musl-dev libffi-dev \
        openssl-dev ca-certificates || return 1
}

install_build_packages() {
    log "Installing compiler packages required to build Python wheels"
    detect_os
    case $OS_ID in
        ubuntu | debian | linuxmint | pop)
            run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
                build-essential libffi-dev libssl-dev python3-dev || return 1
            ;;
        rhel | centos | rocky | almalinux | ol | fedora | amzn)
            if command -v dnf >/dev/null 2>&1; then
                run_root dnf install -y gcc python3-devel libffi-devel openssl-devel || return 1
            else
                run_root yum install -y gcc python3-devel libffi-devel openssl-devel || return 1
            fi
            ;;
        sles | opensuse-leap | opensuse-tumbleweed | opensuse)
            run_root zypper --non-interactive install -y gcc libffi-devel libopenssl-devel \
                python3-devel || return 1
            ;;
        arch | manjaro)
            run_root pacman -Sy --noconfirm gcc libffi openssl || return 1
            ;;
        alpine)
            run_root apk add --no-cache gcc musl-dev libffi-dev openssl-dev python3-dev || return 1
            ;;
    esac
}

install_os_packages() {
    detect_os
    case $OS_ID in
        ubuntu | debian | linuxmint | pop)
            install_apt_packages
            ;;
        rhel | centos | rocky | almalinux | ol | fedora | amzn)
            install_dnf_packages
            ;;
        sles | opensuse-leap | opensuse-tumbleweed | opensuse)
            install_zypper_packages
            ;;
        arch | manjaro)
            install_pacman_packages
            ;;
        alpine)
            install_apk_packages
            ;;
        *)
            case $OS_LIKE in
                *debian*)
                    install_apt_packages
                    ;;
                *rhel* | *fedora*)
                    install_dnf_packages
                    ;;
                *suse*)
                    install_zypper_packages
                    ;;
                *)
                    log "Unsupported operating system '$OS_ID'; OS Python packages will not be installed"
                    return 1
                    ;;
            esac
            ;;
    esac
}

is_musl() {
    [[ -e /lib/ld-musl-x86_64.so.1 || -e /lib/ld-musl-aarch64.so.1 ]] && return 0
    command -v ldd >/dev/null 2>&1 || return 1
    ldd /bin/sh 2>&1 | grep -qi musl
}

standalone_target() {
    local arch libc=gnu
    arch=$(uname -m)
    case $arch in
        x86_64 | amd64)
            arch=x86_64
            ;;
        aarch64 | arm64)
            arch=aarch64
            ;;
        *)
            return 1
            ;;
    esac
    if is_musl; then
        libc=musl
    fi
    printf '%s-unknown-linux-%s\n' "$arch" "$libc"
}

standalone_digest() {
    case $1 in
        aarch64-unknown-linux-gnu)
            printf '%s\n' 4c250ec7cea2aedde2b2e8925d7aaf5ba4924895469d6b5c81c7bdc453341c65
            ;;
        aarch64-unknown-linux-musl)
            printf '%s\n' 92acf3228a5cfe27f492c96e39822d387900eb2cd400c5e93973f32d2fad7fbe
            ;;
        x86_64-unknown-linux-gnu)
            printf '%s\n' 7ce4a71285d913955a76053cc7605ea96da8ecada54dba9cf395245961816421
            ;;
        x86_64-unknown-linux-musl)
            printf '%s\n' cf814a8afed85f1994a9b7fd16146c7faeac4f21ab68fd91537b357fd0bb0899
            ;;
        *)
            return 1
            ;;
    esac
}

standalone_archive_name() {
    printf 'cpython-%s+%s-%s-install_only_stripped.tar.gz\n' \
        "$STANDALONE_PYTHON_VERSION" "$STANDALONE_RELEASE" "$1"
}

verify_sha256() {
    local file=$1 expected=$2
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$expected" "$file" | sha256sum -c - >/dev/null
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s  %s\n' "$expected" "$file" | shasum -a 256 -c - >/dev/null
    else
        python3 - "$expected" "$file" <<'PY'
import hashlib, sys
expected, path = sys.argv[1], sys.argv[2]
digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
raise SystemExit(0 if digest == expected else 1)
PY
    fi
}

download_url() {
    local url=$1 destination=$2
    if command -v curl >/dev/null 2>&1; then
        curl -fL --connect-timeout 15 --retry 3 --retry-delay 2 --max-time 300 \
            -o "$destination" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --timeout=15 --tries=3 -O "$destination" "$url"
    else
        log "curl or wget is required to download a standalone Python runtime"
        return 1
    fi
}

ensure_standalone_python() {
    local target archive_name archive digest tag_file
    runtime_paths
    tag_file=$RUNTIME_DIR/python.tag
    if ! target=$(standalone_target); then
        log "No standalone Python build is available for $(uname -m)"
        return 1
    fi
    if ! digest=$(standalone_digest "$target"); then
        log "No standalone Python checksum is configured for $target"
        return 1
    fi
    archive_name=$(standalone_archive_name "$target")
    if [[ -x $STANDALONE_PYTHON ]] && python_is_usable "$STANDALONE_PYTHON" && \
        [[ -f $tag_file && $(cat "$tag_file") == "$archive_name" ]]; then
        return 0
    fi
    log "Downloading standalone Python $STANDALONE_PYTHON_VERSION ($target)"
    mkdir -p "$RUNTIME_DIR"
    archive=$(mktemp "$RUNTIME_DIR/python.XXXXXX.tar.gz")
    if ! download_url "$STANDALONE_BASE_URL/$archive_name" "$archive"; then
        rm -f "$archive"
        return 1
    fi
    if ! verify_sha256 "$archive" "$digest"; then
        rm -f "$archive"
        log "Standalone Python archive checksum mismatch"
        return 1
    fi
    rm -rf "$RUNTIME_DIR/python"
    if ! tar -xzf "$archive" -C "$RUNTIME_DIR"; then
        rm -f "$archive"
        return 1
    fi
    rm -f "$archive"
    if [[ ! -x $STANDALONE_PYTHON ]] || ! python_is_usable "$STANDALONE_PYTHON"; then
        log "Standalone Python extracted but is not usable"
        return 1
    fi
    printf '%s\n' "$archive_name" >"$tag_file"
}

ensure_python() {
    local selected
    runtime_paths
    if selected=$(find_system_python) && python_has_venv "$selected"; then
        SYSTEM_PYTHON=$selected
        return 0
    fi
    if selected=$(find_system_python); then
        log "Python is present but the venv module is missing"
    else
        log "Python 3.11+ was not found"
    fi
    if install_os_packages; then
        if selected=$(find_system_python) && python_has_venv "$selected"; then
            SYSTEM_PYTHON=$selected
            return 0
        fi
        if selected=$(find_system_python); then
            SYSTEM_PYTHON=$selected
            log "Using system Python without venv support"
            return 0
        fi
        log "OS package installation succeeded but Python 3.11+ is still missing"
    else
        log "OS package installation was skipped or failed"
    fi
    log "Falling back to a standalone Python runtime"
    if ensure_standalone_python; then
        SYSTEM_PYTHON=$STANDALONE_PYTHON
        return 0
    fi
    if selected=$(find_system_python); then
        SYSTEM_PYTHON=$selected
        log "Continuing with $SYSTEM_PYTHON after standalone download failed"
        return 0
    fi
    die "Python 3.11+ is unavailable from the OS and the standalone runtime could not be installed"
}

try_venv_install() {
    python_has_venv "$SYSTEM_PYTHON" || return 1
    if [[ ! -x $ROOT/.venv/bin/python ]] || ! python_is_usable "$ROOT/.venv/bin/python"; then
        if [[ -e $ROOT/.venv ]]; then
            log "Recreating virtualenv at $ROOT/.venv"
            rm -rf "$ROOT/.venv"
        else
            log "Creating virtualenv at $ROOT/.venv"
        fi
        "$SYSTEM_PYTHON" -m venv "$ROOT/.venv" || return 1
    fi
    "$ROOT/.venv/bin/python" -m pip install -q --upgrade pip || return 1
    log "Installing zimigrate into $ROOT/.venv"
    if ! "$ROOT/.venv/bin/python" -m pip install -q "$ROOT"; then
        install_build_packages || true
        "$ROOT/.venv/bin/python" -m pip install -q "$ROOT" || return 1
    fi
    touch "$ROOT/.venv/.zimigrate-source-stamp"
    zimigrate_is_ready
}

install_source_dependencies() {
    local python=$1
    runtime_paths
    mkdir -p "$SITE_PACKAGES"
    "$python" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$python" -m pip install -q --target "$SITE_PACKAGES" \
        "cryptography>=42" "rich>=13.9,<16"
}

try_source_runtime_install() {
    runtime_paths
    if python_has_migration_imports "$SYSTEM_PYTHON" "$(source_pythonpath)"; then
        return 0
    fi
    log "Installing migration dependencies next to the source tree"
    if ! install_source_dependencies "$SYSTEM_PYTHON"; then
        if [[ $SYSTEM_PYTHON != "$STANDALONE_PYTHON" ]] && ensure_standalone_python; then
            SYSTEM_PYTHON=$STANDALONE_PYTHON
            install_source_dependencies "$SYSTEM_PYTHON" || return 1
        else
            return 1
        fi
    fi
    python_has_migration_imports "$SYSTEM_PYTHON" "$(source_pythonpath)"
}

ensure_app() {
    if try_venv_install; then
        RUNTIME_MODE=venv
        return 0
    fi
    log "Virtualenv installation failed; using a static source-tree runtime"
    if try_source_runtime_install; then
        RUNTIME_MODE=source
        return 0
    fi
    if [[ $SYSTEM_PYTHON != "${STANDALONE_PYTHON-}" ]] && ensure_standalone_python; then
        SYSTEM_PYTHON=$STANDALONE_PYTHON
        log "Retrying with standalone Python $SYSTEM_PYTHON"
        if try_venv_install; then
            RUNTIME_MODE=venv
            return 0
        fi
        if try_source_runtime_install; then
            RUNTIME_MODE=source
            return 0
        fi
    fi
    die "zimigrate could not be installed from OS packages, pip, or the standalone runtime"
}

ensure_runtime() {
    runtime_paths
    if zimigrate_is_ready; then
        log "Runtime is ready; skipping package and virtualenv setup"
        RUNTIME_MODE=venv
        return 0
    fi
    if source_runtime_is_ready; then
        if [[ -z ${SYSTEM_PYTHON-} ]]; then
            if [[ -x $STANDALONE_PYTHON ]] && python_is_usable "$STANDALONE_PYTHON"; then
                SYSTEM_PYTHON=$STANDALONE_PYTHON
            else
                SYSTEM_PYTHON=$(find_system_python)
            fi
        fi
        log "Static source runtime is ready; skipping package setup"
        RUNTIME_MODE=source
        return 0
    fi
    ensure_python
    log "Using Python $SYSTEM_PYTHON"
    ensure_app
}

run_zimigrate() {
    local command=$1
    shift
    if [[ ! -f $ROOT/pyproject.toml ]]; then
        die "pyproject.toml was not found in $ROOT; run this script from the zimigrate repository"
    fi
    ensure_runtime
    if [[ $RUNTIME_MODE == venv && -x $ROOT/.venv/bin/zimigrate ]]; then
        exec "$ROOT/.venv/bin/zimigrate" "$command" "$@"
    fi
    if [[ $RUNTIME_MODE == venv && -x $ROOT/.venv/bin/python ]]; then
        exec "$ROOT/.venv/bin/python" -m zimigrate "$command" "$@"
    fi
    exec env PYTHONPATH="$(source_pythonpath)" "$SYSTEM_PYTHON" -m zimigrate "$command" "$@"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    die "source scripts/bootstrap.sh from export.sh or import.sh"
fi
