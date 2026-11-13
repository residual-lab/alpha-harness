"""Every module's own ``python -m`` self-check, each in a process of its own."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src"
# Entry points rather than checks: the app itself, and the two schema writers.
NOT_CHECKS = {"alpha_harness.__main__", "alpha_harness.openapi", "alpha_harness.brain.platform"}
CHECKS = sorted(
    module
    for path in SRC.rglob("*.py")
    if 'if __name__ == "__main__":' in path.read_text(encoding="utf-8")
    and (module := ".".join(path.relative_to(SRC).with_suffix("").parts)) not in NOT_CHECKS
)


def run(module: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - this interpreter, on our own modules
            [sys.executable, "-m", module], capture_output=True, text=True, timeout=120, check=False
        )
    except subprocess.TimeoutExpired as late:
        return subprocess.CompletedProcess(late.cmd, -1, "", f"still running after {late.timeout}s")


@cache
def outcomes() -> dict[str, subprocess.CompletedProcess[str]]:
    # Eight at a time: each is a Python of its own with numpy or DuckDB loaded, ~200 MB.
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(CHECKS, pool.map(run, CHECKS), strict=True))


@pytest.mark.parametrize("module", CHECKS)
def test_self_check(module: str) -> None:
    done = outcomes()[module]
    assert done.returncode == 0, done.stdout + done.stderr
