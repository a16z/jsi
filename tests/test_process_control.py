import time
from signal import SIGKILL
from subprocess import PIPE, TimeoutExpired
from unittest.mock import patch

import psutil
import pytest
from loguru import logger

from jsi.core import (
    Command,
    Config,
    ProcessController,
    Task,
    TaskResult,
    TaskStatus,
    sat,
    unknown,
    unsat,
)

# enable debug logging
logger.enable("jsi")


def mock_process(
    sleep_ms: int = 0,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    start_delay_ms: int = 0,
):
    args = ["tests/mockprocess.py"]
    if sleep_ms:
        args.append("--sleep-ms")
        args.append(str(sleep_ms))
    if exit_code:
        args.append("--exit-code")
        args.append(str(exit_code))
    if stdout:
        args.append("--stdout")
        args.append(stdout)
    if stderr:
        args.append("--stderr")
        args.append(stderr)

    return Command(
        "python",
        args=args,
        stdout=PIPE,
        stderr=PIPE,
        start_delay_ms=start_delay_ms,
    )


def test_real_process():
    command = Command("echo", args=["hello", "world"], stdout=PIPE)
    command.start()
    stdout, stderr = command.communicate(timeout=0.01)

    assert command.returncode == 0
    assert stdout.strip() == "hello world"
    assert not stderr


def test_mock_process():
    command = mock_process()
    command.start()
    stdout, stderr = command.communicate(timeout=0.1)

    assert command.returncode == 0
    assert not stdout
    assert not stderr


def test_mock_process_options():
    command = mock_process(
        sleep_ms=10,
        exit_code=42,
        stdout="beep",
        stderr="boop",
    )
    command.start()
    stdout, stderr = command.communicate(timeout=0.1)

    print(f"{stdout=}")
    print(f"{stderr=}")

    assert command.returncode == 42
    assert stdout.strip() == "beep"
    assert stderr.strip() == "boop"


def test_mock_process_timeout():
    command = mock_process(sleep_ms=1000)
    command.start()
    with pytest.raises(TimeoutExpired):
        command.communicate(timeout=0.001)

    command.kill()
    stdout, stderr = command.communicate()
    assert command.returncode == -SIGKILL
    assert not stdout
    assert not stderr


def test_mock_process_must_start_first():
    command = mock_process()

    with pytest.raises(RuntimeError, match="Process not started"):
        command.kill()


def test_mock_process_can_not_start_twice():
    command = mock_process()

    assert not command.started()
    command.start()
    assert command.started()

    with pytest.raises(RuntimeError, match="Process already started"):
        command.start()


def test_command_kill():
    # big enough that we would notice if it was not killed
    command = mock_process(sleep_ms=60000)

    # when we start it, the pid should exist
    command.start()
    assert psutil.pid_exists(command.pid)

    # when we kill it, the pid should no longer exist
    command.kill()
    command.wait()
    assert not psutil.pid_exists(command.pid)


def test_delayed_start_mocked_time():
    with patch("threading.Timer") as mock_timer:  # type: ignore
        command = mock_process(start_delay_ms=100)
        command.start()

        # Check initial state
        assert not command.started()
        assert not command.done()

        # Verify Timer was called with correct arguments
        mock_timer.assert_called_once_with(0.1, command.start)

        # Simulate timer completion (calls the wrapped start method)
        command.start()
        command.wait()

        # Check final state
        assert command.started()
        assert command.done()
        assert command.returncode == 0


@pytest.mark.slow
def test_delayed_start_real_time():
    command = mock_process(start_delay_ms=100)
    command.start()
    assert not command.started()
    assert not command.done()

    time.sleep(0.2)
    assert command.started()
    assert command.done()
    assert command.returncode == 0


def test_controller_start_empty_commands():
    controller = ProcessController(task=Task(name="test"), commands=[], config=Config())

    with pytest.raises(RuntimeError, match="No commands to run"):
        controller.start()


@pytest.mark.parametrize(
    "command,expected",
    [
        (mock_process(sleep_ms=0, stdout="beep boop"), unknown),
        (mock_process(sleep_ms=0, stdout="", exit_code=1), unknown),
        (mock_process(sleep_ms=100, stdout=sat, exit_code=1), sat),
        (mock_process(sleep_ms=100, stdout=unsat), unsat),
    ],
)
def test_controller_start_single_command_and_join(command: Command, expected: str):
    # returns immediately, non-SMT result
    task = Task(name="test")

    controller = ProcessController(task=task, commands=[command], config=Config())

    controller.start()
    controller.join()

    assert task.status >= TaskStatus.STARTING
    assert command.started()
    assert command.done()
    assert task.status is TaskStatus.TERMINATED
    assert task.result == command.result() == TaskResult(expected)
