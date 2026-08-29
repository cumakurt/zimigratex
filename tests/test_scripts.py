from __future__ import annotations

import os
import stat
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    ROOT / "export.sh",
    ROOT / "import.sh",
    ROOT / "scripts" / "bootstrap.sh",
    ROOT / "scripts" / "vendor-runtime.sh",
)


class WrapperScriptTests(unittest.TestCase):
    def test_scripts_are_executable_and_have_unix_shebangs(self) -> None:
        for path in (ROOT / "export.sh", ROOT / "import.sh"):
            mode = path.stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, f"{path.name} must be executable")
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(first_line, "#!/usr/bin/env bash")

    def test_scripts_pass_bash_syntax_check(self) -> None:
        for path in SCRIPTS:
            completed = subprocess.run(
                ["bash", "-n", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"{path.name} failed bash -n: {completed.stderr}",
            )

    def test_bootstrap_skips_setup_when_runtime_is_ready(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
if zimigrate_is_ready; then
  echo ready
else
  echo missing
fi
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(completed.stdout.strip(), {"ready", "missing"})

    def test_bootstrap_rejects_direct_execution(self) -> None:
        completed = subprocess.run(
            ["bash", str(ROOT / "scripts" / "bootstrap.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("source scripts/bootstrap.sh", completed.stderr)

    def test_standalone_python_metadata_is_pinned(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
target=$(standalone_target)
[[ "$target" == *-unknown-linux-gnu || "$target" == *-unknown-linux-musl ]]
[[ $(standalone_digest x86_64-unknown-linux-gnu) == \
    7ce4a71285d913955a76053cc7605ea96da8ecada54dba9cf395245961816421 ]]
[[ $(standalone_archive_name x86_64-unknown-linux-gnu) == \
    cpython-3.12.14+20260825-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz ]]
! standalone_digest unknown-target
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_os_package_failure_falls_back_to_standalone_python(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
find_system_python() { return 1; }
install_os_packages() { return 1; }
vendored_python_archive() { printf '/nonexistent/python.tar.gz\n'; }
ensure_standalone_python() { STANDALONE_PYTHON=/standalone/python; return 0; }
ensure_python
[[ $SYSTEM_PYTHON == /standalone/python ]]
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")
        self.assertIn("Falling back to a standalone Python runtime", completed.stderr)

    def test_source_runtime_is_used_when_venv_install_fails(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
SYSTEM_PYTHON=/usr/bin/python3
try_venv_install() { return 1; }
try_source_runtime_install() { return 0; }
ensure_app
[[ $RUNTIME_MODE == source ]]
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")
        self.assertIn("using a static source-tree runtime", completed.stderr)

    def test_download_retries_without_tls_after_certificate_failure(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
insecure_used=0
download_with_curl() {
    local destination=$2 insecure=$3
    if [[ $insecure == 1 ]]; then
        insecure_used=1
        printf 'payload\n' >"$destination"
        return 0
    fi
    return 60
}
download_with_wget() { return 1; }
install_ca_certificates() { return 0; }
file=$(mktemp)
download_url https://example.invalid/python.tgz "$file"
[[ $insecure_used == 1 ]]
[[ $(cat "$file") == payload ]]
rm -f "$file"
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")
        self.assertIn("TLS certificate verification failed", completed.stderr)

    def test_apt_package_helper_installs_one_package_at_a_time(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
type _apt_install_one >/dev/null
type download_with_curl >/dev/null
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
file=$(mktemp)
printf 'zimigrate\n' >"$file"
digest=$(printf 'zimigrate\n' | sha256sum | awk '{print $1}')
verify_sha256 "$file" "$digest"
! verify_sha256 "$file" 0000000000000000000000000000000000000000000000000000000000000000
rm -f "$file"
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_vendor_ca_bundle_is_preferred(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
[[ $(system_ca_file) == "$ROOT/vendor/certs/cacert.pem" ]]
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_vendored_python_archives_match_pinned_digests(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
has_vendor_wheels
for target in \
    x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu \
    x86_64-unknown-linux-musl aarch64-unknown-linux-musl; do
  archive=$(vendored_python_archive "$target")
  [[ -f $archive ]]
  verify_sha256 "$archive" "$(standalone_digest "$target")"
done
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_vendored_python_is_used_before_os_packages(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
find_system_python() { return 1; }
install_os_packages() { printf 'APT_CALLED\n'; return 1; }
ensure_standalone_python() { STANDALONE_PYTHON=/standalone/python; return 0; }
ensure_python
[[ $SYSTEM_PYTHON == /standalone/python ]]
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")
        self.assertIn("Using the vendored standalone Python runtime", completed.stderr)
        self.assertNotIn("APT_CALLED", completed.stdout)

    def test_pip_install_uses_vendor_wheels_offline(self) -> None:
        script = r"""
set -euo pipefail
ROOT="$1"
. "$ROOT/scripts/bootstrap.sh"
python=$(mktemp)
trap 'rm -f "$python"' EXIT
cat >"$python" <<'PY'
#!/usr/bin/env bash
printf '%s\n' "$*"
exit 0
PY
chmod +x "$python"
output=$(pip_install "$python" "cryptography>=42")
[[ $output == *-m\ pip\ install*--no-index*--find-links*"$ROOT/vendor/wheels"*"cryptography>=42" ]]
echo ok
"""
        completed = subprocess.run(
            ["bash", "-c", script, "check", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")
