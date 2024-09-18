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


def cmd(
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


def test_cmd():
    command = cmd()
    command.start()
    stdout, stderr = command.communicate(timeout=0.1)

    assert command.returncode == 0
    assert not stdout
    assert not stderr


def test_cmd_options():
    command = cmd(
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


def test_cmd_timeout():
    command = cmd(sleep_ms=1000)
    command.start()
    with pytest.raises(TimeoutExpired):
        command.communicate(timeout=0.001)

    command.kill()
    stdout, stderr = command.communicate()
    assert command.returncode == -SIGKILL
    assert not stdout
    assert not stderr


def test_cmd_must_start_first():
    command = cmd()

    with pytest.raises(RuntimeError, match="Process not started"):
        command.kill()


def test_cmd_can_not_start_twice():
    command = cmd()

    assert not command.started()
    command.start()
    assert command.started()

    with pytest.raises(RuntimeError, match="Process already started"):
        command.start()


def test_command_kill():
    # big enough that we would notice if it was not killed
    command = cmd(sleep_ms=60000)

    # when we start it, the pid should exist
    command.start()
    assert psutil.pid_exists(command.pid)

    # when we kill it, the pid should no longer exist
    command.kill()
    command.wait()
    assert not psutil.pid_exists(command.pid)


def test_delayed_start_mocked_time():
    with patch("threading.Timer") as mock_timer:  # type: ignore
        command = cmd(start_delay_ms=100)
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
    command = cmd(start_delay_ms=100)
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
        (cmd(sleep_ms=0, stdout="beep boop"), unknown),
        (cmd(sleep_ms=0, stdout="", exit_code=1), unknown),
        (cmd(sleep_ms=100, stdout=sat, exit_code=1), sat),
        (cmd(sleep_ms=100, stdout=unsat), unsat),
    ],
)
def test_controller_start_single_command_and_join(command: Command, expected: str):
    task = Task(name="test")
    controller = ProcessController(task=task, commands=[command], config=Config())

    controller.start()
    assert task.status >= TaskStatus.STARTING

    controller.join()
    assert command.done()
    assert task.status is TaskStatus.TERMINATED
    assert task.result == command.result() == TaskResult(expected)


@pytest.mark.parametrize(
    "command1,command2,expected",
    [
        # first command returns weird result fast, early exit not triggered
        (cmd(stdout="beep boop"), cmd(sleep_ms=50, stdout="sat"), sat),
        # first command errors fast, early exit not triggered
        (cmd(stderr="error", exit_code=1), cmd(sleep_ms=50, stdout="unsat"), unsat),
        # both commands return weird results
        (cmd(stdout="beep beep"), cmd(stdout="boop boop"), unknown),
        # one command is really slow, early sat exit triggered
        (cmd(sleep_ms=5000, stdout="unsat"), cmd(sleep_ms=50, stdout="sat"), sat),
        # one command is really slow, early unsat exit triggered
        (cmd(sleep_ms=5000, stdout="sat"), cmd(sleep_ms=50, stdout="unsat"), unsat),
        # early exit triggered even with strange exit code and stderr output
        (cmd(sleep_ms=5000, stdout="unsat"), cmd(sleep_ms=50, stdout="sat"), sat),
        # one command is really slow, early unsat exit triggered
        (cmd(sleep_ms=5000, stdout="sat"), cmd(sleep_ms=50, stdout="unsat"), unsat),
    ],
)
def test_controller_start_double_command_early_exit(
    command1: Command, command2: Command, expected: str
):
    task = Task(name="test")
    commands = [command1, command2]
    config = Config(early_exit=True)
    controller = ProcessController(task=task, commands=commands, config=config)

    controller.start()
    assert task.status >= TaskStatus.STARTING

    controller.join()
    assert command1.done()
    assert command2.done()
    assert task.status is TaskStatus.TERMINATED
    assert task.result == TaskResult(expected)

    # both commands should terminate "fast" (allow some wiggle room for slow CI)
    assert (t1 := command1.elapsed()) and t1 < 1
    assert (t2 := command2.elapsed()) and t2 < 1

    # there should be no process left running
    assert not psutil.pid_exists(command1.pid)
    assert not psutil.pid_exists(command2.pid)


def test_controller_early_exit_with_slow_start():
    # command1 takes forever to even start
    command1 = cmd(start_delay_ms=5000, stdout="unsat")

    # command2 is fast and returns sat
    command2 = cmd(sleep_ms=50, stdout="sat")

    task = Task(name="test")
    commands = [command1, command2]
    config = Config(early_exit=True)
    controller = ProcessController(task=task, commands=commands, config=config)

    controller.start()
    assert task.status >= TaskStatus.STARTING

    controller.join()

    # the task should be terminated, without even waiting for command1 to run
    assert not command1.started()
    assert command2.done()
    assert task.status is TaskStatus.TERMINATED
    assert task.result == TaskResult.SAT


# TODO: test with no early exit
# TODO: test with timeout (no successful result, successful result then kills, etc.)
