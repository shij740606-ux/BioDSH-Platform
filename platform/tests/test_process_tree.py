import os
import signal
import subprocess
import sys
import time

import psutil
import pytest

from biodsh_platform.process_tree import ExecutorProcess, terminate_process


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("timed out waiting for child process")
        time.sleep(0.02)


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize("leader_exits", [False, True])
def test_termination_removes_children_even_after_leader_exit(tmp_path, leader_exits):
    child_ready = tmp_path / "child.pid"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); " + (
        "" if leader_exits else "time.sleep(60)"
    )
    process = ExecutorProcess([sys.executable, "-c", parent], stdout=subprocess.DEVNULL)
    child_pid = None
    try:
        wait_for(lambda: child_ready.exists() and child_ready.stat().st_size > 0)
        child_pid = int(child_ready.read_text())
        assert alive(child_pid)
        if leader_exits:
            assert process.wait(timeout=5) == 0
        terminate_process(process, 0.1)
        wait_for(lambda: not alive(child_pid))
        assert process.poll() is not None
    finally:
        terminate_process(process, 0.1)
        process.close_tree()
        if child_pid and alive(child_pid):
            psutil.Process(child_pid).kill()


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal escalation")
def test_posix_group_gets_sigkill_after_leader_exits(tmp_path):
    # The parameterized test also checks the original regression on POSIX.
    # This test verifies that an already dead leader does not skip killpg.
    process = ExecutorProcess([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    terminate_process(process, 0.1)
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
