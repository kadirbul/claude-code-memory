"""Tests for compile.hold_system_awake() (WI-bugfix-e78c0f, owner option A 2026-10-16).

Run:  uv run --directory <project> python scripts/test_compile_keep_awake.py

No LLM calls, no network. A fake kernel32 records which power requests are taken, so
the checks pin both - SystemRequired (1), which does the work, and ExecutionRequired
(3) - and that a refusal or a ctypes error is logged, never fatal. One check takes the REAL request on
Windows: the API's own success value is the only thing a non-admin process can read
(`powercfg /requests` needs elevation).
"""
from __future__ import annotations

import contextlib
import ctypes
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compile as c  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")
        failures.append(label)


class FakeKernel32:
    """Stands in for ctypes.WinDLL('kernel32'); records every call."""

    def __init__(self, handle: int = 42, refuse: tuple[int, ...] = (), raises: bool = False):
        self.handle, self.refuse, self.raises = handle, refuse, raises
        self.set_types: list[int] = []
        self.cleared: list[int] = []
        self.closed = False
        self.last_error = 0

        def create(_ctx):
            if self.raises:
                raise OSError("simulated loader failure")
            return self.handle

        def set_req(_h, t):
            self.set_types.append(t)
            if t in self.refuse:
                self.last_error = 5  # ERROR_ACCESS_DENIED, set by THIS call only
                return 0
            self.last_error = 0  # a later success overwrites it, as Win32 may
            return 1

        def clear(_h, t):
            self.cleared.append(t)
            return 1

        def close(_h):
            self.closed = True
            return 1

        self.PowerCreateRequest, self.PowerSetRequest = create, set_req
        self.PowerClearRequest, self.CloseHandle = clear, close


def run_with(fake: FakeKernel32, platform: str = "win32"):
    """Call hold_system_awake() with a fake kernel32; return (result, output, release)."""
    import atexit
    registered = []
    # WinDLL and get_last_error exist only on Windows: patch both, and on restore put
    # back exactly what was there - including "absent" - so this runs on Linux too.
    saved = {n: getattr(ctypes, n) for n in ("WinDLL", "get_last_error") if hasattr(ctypes, n)}
    real_platform, real_register = sys.platform, atexit.register
    ctypes.WinDLL = lambda *_a, **_k: fake  # type: ignore[attr-defined]
    ctypes.get_last_error = lambda: fake.last_error  # type: ignore[attr-defined]
    sys.platform = platform  # type: ignore[misc]
    atexit.register = lambda f: registered.append(f)  # type: ignore[assignment]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            result = c.hold_system_awake()
    finally:
        for n in ("WinDLL", "get_last_error"):
            if n in saved:
                setattr(ctypes, n, saved[n])
            else:
                delattr(ctypes, n)
        sys.platform = real_platform  # type: ignore[misc]
        atexit.register = real_register  # type: ignore[assignment]
    return result, out.getvalue(), registered


print("hold_system_awake - which requests, and what a refusal does")
fake = FakeKernel32()
result, out, registered = run_with(fake)
check("takes SystemRequired(1) AND ExecutionRequired(3)", sorted(fake.set_types), [1, 3])
check("reports held", result, True)
check("logs the held line", "[keep-awake held" in out, True)
check("registers exactly one release at exit", len(registered), 1)
registered[0]()
check("release clears both requests", sorted(fake.cleared), [1, 3])
check("release closes the handle", fake.closed, True)

fake = FakeKernel32(handle=0)
result, out, registered = run_with(fake)
check("refused create -> False, not an exception", result, False)
check("refused create is logged", "[keep-awake refused" in out, True)
check("refused create takes no request", fake.set_types, [])

fake = FakeKernel32(refuse=(1, 3))
result, out, registered = run_with(fake)
check("refused set -> False", result, False)
check("refused set is logged as partial", "[keep-awake partial: 0 of 2" in out, True)

fake = FakeKernel32(refuse=(1,))
result, out, registered = run_with(fake)
check("first refused, second ok -> the FAILING call's code is logged",
      "type 1: error 5" in out, True)
check("release clears only what was held", (registered[0](), fake.cleared)[1], [3])

fake = FakeKernel32(raises=True)
result, out, registered = run_with(fake)
check("ctypes/loader error -> False, compile not stopped", result, False)
check("ctypes/loader error is logged", "[keep-awake failed: OSError" in out, True)

fake = FakeKernel32()
result, out, registered = run_with(fake, platform="linux")
check("non-Windows: no-op, False", result, False)
check("non-Windows: touches no kernel32", fake.set_types, [])
check("non-Windows: silent", out, "")
check("mock leaves ctypes as it found it", hasattr(ctypes, "WinDLL"), sys.platform == "win32")

if sys.platform == "win32":
    print("\nreal kernel32 (this machine)")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        real = c.hold_system_awake()
    check("real PowerSetRequest succeeds for both", real, True)
    print(f"        ({out.getvalue().strip()})")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("All checks passed.")
