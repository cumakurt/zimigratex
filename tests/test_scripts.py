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

    def test_verify_sha256_accepts_a_known_digest(self) -> None:
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
