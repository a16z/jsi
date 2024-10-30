import time
from signal import SIGKILL, SIGTERM
from subprocess import PIPE, TimeoutExpired
from unittest.mock import patch

import pytest

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
from jsi.utils import LogLevel, logger, pid_exists
from jsi.utils import simple_stderr as stderr

# enable debug logging
logger.enable(console=stderr, level=LogLevel.DEBUG)


def cmd(
    sleep_ms: int = 0,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    start_delay_ms: int = 0,
):
    args = ["python", "tests/mockprocess.py"]
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
        f"python-sleep{sleep_ms}-exit{exit_code}-stdout{stdout}-stderr{stderr}-start{start_delay_ms}",
        args=args,
        stdout=PIPE,
        stderr=PIPE,
        start_delay_ms=start_delay_ms,
    )


def test_real_process():
    command = Command("hello-world", args=["echo", "hello", "world"], stdout=PIPE)
    command.start()
    stdout, stderr = command.communicate(timeout=0.01)

    assert command.returncode == 0
    assert stdout.strip() == "hello world"
    assert not stderr


def test_cmd():
    command = cmd()
    command.start()
    stdout, stderr = command.communicate(timeout=0.2)

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
    stdout, stderr = command.communicate(timeout=0.2)

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
    assert pid_exists(command.pid)

    # when we kill it, the pid should no longer exist
    command.kill()
    command.wait()
    assert not pid_exists(command.pid)


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

    # give it some time to complete (allow some wiggle room for slow CI)
    time.sleep(0.4)
    assert command.started()
    assert command.done()
    assert command.returncode == 0


def test_controller_start_empty_commands_raises():
    controller = ProcessController(task=Task(name="test"), commands=[], config=Config())

    with pytest.raises(RuntimeError, match="No commands to run"):
        controller.start()


def test_controller_start_twice_raises():
    controller = ProcessController(
        task=Task(name="test"), commands=[cmd()], config=Config()
    )

    controller.start()

    with pytest.raises(ValueError, match="expected status"):
        controller.start()

    controller.join()


def test_controller_join_before_start_raises():
    controller = ProcessController(task=Task(name="test"), commands=[], config=Config())

    with pytest.raises(RuntimeError, match="can not join controller"):
        controller.join()


def test_controller_join_twice():
    controller = ProcessController(
        task=Task(name="test"), commands=[cmd()], config=Config()
    )

    controller.start()
    controller.join()
    controller.join()  # this fine, just returns immediately


def test_controller_kill_before_start():
    controller = ProcessController(task=Task(name="test"), commands=[], config=Config())

    with pytest.raises(
        RuntimeError, match="can not kill controller before it is started"
    ):
        controller.kill()


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
    assert not pid_exists(command1.pid)
    assert not pid_exists(command2.pid)


def test_controller_early_exit_with_slow_command():
    command1 = cmd(sleep_ms=5000, stdout="unsat")
    command2 = cmd(sleep_ms=0, stdout="sat")

    task = Task(name="test")
    commands = [command1, command2]
    config = Config(early_exit=True)
    controller = ProcessController(task=task, commands=commands, config=config)

    controller.start()
    assert task.status >= TaskStatus.STARTING

    controller.join()

    # verify that command1 was killed because of early exit
    assert command1.done() and not command1.ok()
    assert not command1.has_timed_out
    assert command1.returncode == -SIGTERM
    assert (t1 := command1.elapsed()) and t1 < 0.1

    assert command2.done() and command2.ok()
    assert (t2 := command2.elapsed()) and t2 < 0.1

    assert task.status is TaskStatus.TERMINATED
    assert task.result == TaskResult.SAT


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


@pytest.mark.parametrize(
    "command1,command2,expected",
    [
        # first command returns sat result fast, still waits for second command
        (cmd(stdout=sat), cmd(start_delay_ms=50, sleep_ms=50, stdout=sat), sat),
        # first command returns unsat result fast, still waits for second command
        (cmd(stdout=unsat), cmd(start_delay_ms=50, sleep_ms=50, stdout=unsat), unsat),
        # commands return different results, first result is returned
        (cmd(stdout=sat), cmd(start_delay_ms=50, sleep_ms=50, stdout=unsat), sat),
        # both commands return weird results
        (cmd(stdout="beep"), cmd(stdout="boop"), unknown),
    ],
)
def test_controller_no_early_exit(command1: Command, command2: Command, expected: str):
    task = Task(name="test")
    commands = [command1, command2]
    config = Config(early_exit=False)
    controller = ProcessController(task=task, commands=commands, config=config)

    controller.start()
    assert task.status >= TaskStatus.STARTING

    controller.join()

    assert task.status is TaskStatus.TERMINATED
    assert task.result == TaskResult(expected)

    for command in commands:
        assert command.done()
        assert not command.has_timed_out
        assert command.returncode == 0
        assert not pid_exists(command.pid)


# TODO: test with timeout (no successful result, successful result then kills, etc.)
def test_controller_timeout_single_command():
    task = Task(name="test")
    command = cmd(sleep_ms=1000)

    config = Config(early_exit=False, timeout_seconds=0.001)
    controller = ProcessController(task=task, commands=[command], config=config)

    controller.start()
    controller.join()

    assert command.done()
    assert not pid_exists(command.pid)
    assert command.has_timed_out
    assert command.returncode == -SIGTERM
    assert (elapsed := command.elapsed()) and elapsed < 0.1

    assert task.status is TaskStatus.TERMINATED
    assert task.result == TaskResult.TIMEOUT


def test_controller_interval_early_exit():
    task = Task(name="test")
    command1 = cmd(sleep_ms=0, stdout="sat")
    command2 = cmd(sleep_ms=0, stdout="unsat")

    # will exit as soon as command1 is finished, won't wait the full interval
    interval = 10
    start = time.perf_counter()
    config = Config(early_exit=True, interval_seconds=interval)
    controller = ProcessController(
        task=task, commands=[command1, command2], config=config
    )

    controller.start()
    assert task.status >= TaskStatus.STARTING

    controller.join()

    assert (elapsed := time.perf_counter() - start) and elapsed < interval / 2

    assert command1.done()
    assert not command2.started()
    assert task.status is TaskStatus.TERMINATED


def test_controller_interval_full_run():
    task = Task(name="test")
    command1 = cmd(sleep_ms=0, stdout="sat")
    command2 = cmd(sleep_ms=0, stdout="unsat")
    command3 = cmd(sleep_ms=0, stdout="unknown")

    # will exit as soon as command1 is finished, won't wait the full interval
    interval = 0.1
    start = time.perf_counter()
    config = Config(early_exit=False, interval_seconds=interval)
    commands = [command1, command2, command3]
    controller = ProcessController(task=task, commands=commands, config=config)

    controller.start()
    assert task.status >= TaskStatus.STARTING

    controller.join()

    assert (elapsed := time.perf_counter() - start) and elapsed > 2 * interval

    assert command1.done()
    assert command2.done()
    assert command3.done()

    assert command1.start_time and command2.start_time and command3.start_time
    assert command1.start_time < command2.start_time + interval
    assert command2.start_time < command3.start_time + interval
    assert task.status is TaskStatus.TERMINATED
