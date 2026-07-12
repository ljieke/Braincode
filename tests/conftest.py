from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME_ROOT = PROJECT_ROOT / ".braincode" / "test-runtime"
TEST_RUN_ROOT = TEST_RUNTIME_ROOT / f"run-{os.getpid()}"
TEST_HOME = TEST_RUN_ROOT / "home"
TEST_TEMP = TEST_RUNTIME_ROOT / "tmp"
PYTEST_TEMP_ROOT = TEST_RUNTIME_ROOT / "pytest"

for directory in (TEST_HOME, TEST_TEMP, PYTEST_TEMP_ROOT):
    directory.mkdir(parents=True, exist_ok=True)

# Keep tests away from the real user profile and from host-managed temp
# directories whose ACLs may reject sandboxed or service accounts.
os.environ["HOME"] = str(TEST_HOME)
os.environ["USERPROFILE"] = str(TEST_HOME)
os.environ["TEMP"] = str(TEST_TEMP)
os.environ["TMP"] = str(TEST_TEMP)
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(PYTEST_TEMP_ROOT)
os.environ.setdefault("PYTHONUTF8", "1")
tempfile.tempdir = str(TEST_TEMP)


@pytest.fixture
def python_command() -> Callable[[str], str]:
    """Build a shell command that runs Python code on Windows and POSIX."""

    def build(code: str) -> str:
        arguments = [sys.executable, "-c", code]
        if os.name == "nt":
            return subprocess.list2cmdline(arguments)
        return shlex.join(arguments)

    return build


@pytest.fixture(scope="session", autouse=True)
def _cleanup_isolated_test_home() -> Generator[None, None, None]:
    yield
    shutil.rmtree(TEST_RUN_ROOT, ignore_errors=True)
