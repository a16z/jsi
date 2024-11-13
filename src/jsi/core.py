# postpone the evaluation of annotations, treating them as strings at runtime
from __future__ import annotations

import contextlib
import io
import os
import threading
import time
from collections.abc import Callable, Sequence
from enum import Enum
from subprocess import PIPE, Popen, TimeoutExpired

from jsi.config.loader import Config, SolverDefinition
from jsi.utils import logger, timer

sat, unsat, error, unknown, timeout, killed = (
    "sat",
    "unsat",
    "error",
    "unknown",
    "timeout",
    "killed",
)


def try_closing(file: object):
    if hasattr(file, "close"):
        with contextlib.suppress(Exception):
            file.close()  # type: ignore


def try_reading(file: object) -> str | None:
    if isinstance(file, io.TextIOWrapper):
        with open(file.name) as f:
            return f.read()

    return None


def first_line(content: str) -> str:
    return content[: content.find("\n")]


class TaskResult(Enum):
    SAT = sat
    UNSAT = unsat
    ERROR = error
    UNKNOWN = unknown
    TIMEOUT = timeout
    KILLED = killed
    NOT_STARTED = "not started"


class TaskStatus(Enum):
    NOT_STARTED = 1

    # transition state while processes are being started
    # (some may have terminated already)
    STARTING = 2

    # processes are running
    RUNNING = 3

    # at least one process is terminating, no new processes can be started
    TERMINATING = 4

    # all processes have terminated
    TERMINATED = 5

    def __ge__(self, other: TaskStatus):
        if self.__class__ is other.__class__:
            return self.value >= other.value
        return NotImplemented

    def __gt__(self, other: TaskStatus):
        if self.__class__ is other.__class__:
            return self.value > other.value
        return NotImplemented

    def __le__(self, other: TaskStatus):
        if self.__class__ is other.__class__:
            return self.value <= other.value
        return NotImplemented

    def __lt__(self, other: TaskStatus):
        if self.__class__ is other.__class__:
            return self.value < other.value
        return NotImplemented


class Command:
    """High level wrapper for a subprocess, with extra metadata (start/end time,
    timeout, etc).

    Does not spawn a process until start() is called.

    Proxies data access to the underlying Popen instance (once started)."""

    # human readable identifier for the command (not necessarily the binary name)
    name: str

    # command line arguments
    args: Sequence[str]
    input_file: str | None
    stdout: io.TextIOWrapper | int | None
    stderr: io.TextIOWrapper | int | None
    stdout_text: str | None
    stderr_text: str | None

    # extra arguments to pass to Popen
    kwargs: dict[str, object]

    # metadata
    start_time: float | None
    end_time: float | None
    has_timed_out: bool
    on_kill_list: bool

    # internal fields
    _process: Popen[str] | None
    _result: TaskResult | None
    _lock: threading.Lock

    # to facilitate testing
    start_delay_ms: int
    timer: threading.Timer | None

    def __init__(
        self,
        name: str,
        args: Sequence[str],
        input_file: str | None = None,
        stdout: io.TextIOWrapper | int | None = None,
        stderr: io.TextIOWrapper | int | None = None,
        start_delay_ms: int = 0,
        **kwargs: object,  # type: ignore
    ):
        self.name = name
        self.args = args
        self.input_file = input_file
        self.stdout = stdout
        self.stderr = stderr
        self.kwargs = kwargs

        # cached stdout/stderr
        self.stdout_text = None
        self.stderr_text = None

        # internal fields
        self._process = None
        self._result = None
        self._lock = threading.Lock()

        # metadata
        self.start_time = None
        self.end_time = None
        self.has_timed_out = False
        self.on_kill_list = False

        # testing
        self.start_delay_ms = start_delay_ms
        self.timer = None

    def parts(self) -> list[str]:
        parts = [*self.args]
        if self.input_file:
            parts.append(str(self.input_file))
        return parts

    def start(self) -> None:
        with self._lock:
            if self._process is not None:
                raise RuntimeError("Process already started")

            if self.start_delay_ms:
                # kick off a thread that will wait and then start the process
                delay = self.start_delay_ms
                self.start_delay_ms = 0

                logger.debug(f"delaying start of {self.bin_name()} by {delay}ms")
                timer = threading.Timer(delay / 1000, self.start)
                timer.daemon = True  # don't block the program from exiting
                timer.start()
                self.timer = timer

            else:
                logger.debug(f"starting {self.parts()}")
                self.start_time = time.time()
                self._process = Popen(
                    self.parts(),
                    **self.kwargs,  # type: ignore
                    stdout=self.stdout,
                    stderr=self.stderr,
                    text=True,
                )  # type: ignore

    def wait(self, timeout: float | None = None):
        # wait if the process has a delayed start
        if self.timer:
            while not self._process:
                # if the process is marked for killing, stop waiting
                if self.on_kill_list:
                    break

                time.sleep(0.01)

        # skip waiting if the process is not started
        if self._process is None:
            return

        return self._process.wait(timeout)

    def bin_name(self):
        return self.name

    def done(self):
        return self._process is not None and self._process.poll() is not None

    def started(self):
        return self._process is not None

    def elapsed(self) -> float | None:
        """Returns the elapsed time in seconds.

        Returns None if the process has not started or not finished."""

        if not self.end_time or not self.start_time:
            return None

        return self.end_time - self.start_time

    def _ensure_started(self):
        if not self.started():
            raise RuntimeError(f"Process not started: {self.bin_name()}")

    def _ensure_finished(self):
        self._ensure_started()
        if not self.done():
            raise RuntimeError(f"Process still running: {self._process!r}")

    def ok(self):
        """Throws if not done. Returns True if the process return sat or unsat."""
        self._ensure_finished()

        # unfortunately can't just use returncode == 0 here because:
        # - stp can return 0 when it fails to parse the input file
        # - boolector returns non 0 even when it's happy
        # - ...

        return self.result() in (TaskResult.SAT, TaskResult.UNSAT)

    def maybe_ok(self):
        """Non-throwing version of ok().

        Returns True if the process has finished and returned sat or unsat.
        Returns False in every other case, including not started, timeout, error, etc.
        """

        try:
            return self.ok()
        except RuntimeError:
            return False

    def _get_result(self) -> str:
        # only valid if the process has finished
        if not self._process:
            raise RuntimeError("Process not started")

        if self.has_timed_out:
            return timeout

        if self._process.returncode == -15:
            return timeout if self.has_timed_out else killed

        stdout_content, stderr_content = self.read_io()
        if not stdout_content:
            if stderr_content and "error" in stderr_content:
                return error

            if self.returncode != 0:
                return error

            return unknown

        line = first_line(stdout_content).strip()
        logger.debug(f"result for {self.bin_name()}: {line}")
        if line == "sat":
            return sat
        elif line == "unsat":
            return unsat
        elif "error" in line:
            return error
        elif "ASSERT(" in line:
            # stp may not return sat as the first line
            # when there is a counterexample
            return sat
        elif self.has_timed_out:
            return timeout
        else:
            return unknown

    def result(self) -> TaskResult:
        if not self.started():
            return TaskResult.NOT_STARTED

        self._ensure_finished()

        # cache the result
        if not self._result:
            result_str = self._get_result()
            self._result = TaskResult(result_str)

        return self._result

    def _read_io(self) -> tuple[str | None, str | None]:
        stdout_content, stderr_content = None, None

        if self.stdout == PIPE or self.stderr == PIPE:
            stdout_content, stderr_content = self.communicate()

        if stdout_content is None:
            stdout_content = try_reading(self.stdout)

        if stderr_content is None:
            stderr_content = try_reading(self.stderr)

        return stdout_content, stderr_content

    def read_io(self) -> tuple[str | None, str | None]:
        self._ensure_finished()

        # return cached values if available
        if self.stdout_text or self.stderr_text:
            return self.stdout_text, self.stderr_text

        stdout, stderr = self._read_io()
        self.stdout_text, self.stderr_text = stdout, stderr
        return stdout, stderr

    #
    # pass through methods for Popen
    #

    def communicate(
        self,
        input: str | None = None,  # noqa: A002
        timeout: float | None = None,
    ) -> tuple[str, str]:
        assert self._process is not None
        stdout, stderr = self._process.communicate(input, timeout)

        return (
            (stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout) or "",
            (stderr.decode("utf-8") if isinstance(stderr, bytes) else stderr) or "",
        )

    def terminate(self):
        self._ensure_started()
        assert self._process is not None
        self._process.terminate()

    def kill(self):
        self._ensure_started()
        assert self._process is not None
        self._process.kill()

    @property
    def returncode(self):
        if not self._process:
            return None

        return self._process.returncode

    @property
    def pid(self):
        self._ensure_started()
        assert self._process is not None
        return self._process.pid


def base_commands(
    solver_names: Sequence[str],
    solver_definitions: dict[str, SolverDefinition],
    available_solvers: dict[str, str],
    config: Config,
) -> list[Command]:
    """Command "templates" corresponding to a particular configuration,
    but without input and output filenames."""
    commands: list[Command] = []

    for solver_name in solver_names:
        solver_def: SolverDefinition | None = solver_definitions.get(solver_name)
        if not solver_def:
            raise RuntimeError(f"unknown solver: {solver_name}")

        executable_name = solver_def.executable
        executable_path = available_solvers.get(executable_name)
        if not executable_path:
            continue

        args = [executable_path]

        # append the model option if requested
        if config.model and (model_arg := solver_def.model):
            args.append(model_arg)

        # append solver-specific extra arguments
        args.extend(solver_def.args)
        commands.append(
            Command(
                name=solver_name,
                args=args,
            )
        )

    return commands


def set_input_output(commands: list[Command], config: Config):
    file = config.input_file
    output = config.output_dir

    assert file is not None
    assert output is not None

    basename = os.path.basename(file)

    for command in commands:
        command.input_file = file
        stdout_file = os.path.join(output, f"{basename}.{command.name}.out")
        stderr_file = os.path.join(output, f"{basename}.{command.name}.err")
        command.stdout = open(stdout_file, "w")  # noqa: SIM115
        command.stderr = open(stderr_file, "w")  # noqa: SIM115


class Task:
    """Mutable class that keeps track of a high level task (query to be solved),
    involving potentially multiple solver subprocesses.

    Exposes synchronization primitives and enforces valid state transitions:
    NOT_STARTED → STARTING → RUNNING → TERMINATING → TERMINATED

    It is possible to skip states forward, but going back is not possible, e.g.:
    - STARTING → TERMINATING is allowed
    - RUNNING → NOT_STARTED is not allowed"""

    name: str
    processes: list[Command]
    output: str | None
    _result: TaskResult | None
    _status: TaskStatus
    _lock: threading.Lock

    def __init__(self, name: str):
        self.name = name
        self.processes = []
        self._status = TaskStatus.NOT_STARTED
        self._lock = threading.Lock()

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, new_status: TaskStatus):
        with self._lock:
            if new_status < self._status:
                raise ValueError(f"can not switch from {self._status} to {new_status}")

            logger.debug(f"setting status to {new_status}")
            self._status = new_status

    def set_status(
        self,
        new_status: TaskStatus,
        required_status: TaskStatus | None = None,
        expected_status: TaskStatus | None = None,
    ):
        with self._lock:
            status = self._status

            # soft error
            if expected_status is not None and status != expected_status:
                logger.warning(f"expected status {expected_status}, got {status}")
                return

            # hard error
            if required_status is not None and status != required_status:
                raise ValueError(f"expected status {required_status}, got {status}")

            if new_status < status:
                raise ValueError(f"can not switch from {status} to {new_status}")

            logger.debug(f"setting status to {new_status}")
            self._status = new_status

    @property
    def result(self) -> TaskResult:
        has_timeouts = False
        has_errors = False

        for command in self.processes:
            if not command.started() or not command.done():
                continue

            if command.ok():
                return command.result()

            if command.has_timed_out:
                has_timeouts = True

            if command.result() == TaskResult.ERROR:
                has_errors = True

        if has_timeouts:
            return TaskResult.TIMEOUT

        if has_errors:
            return TaskResult.ERROR

        return TaskResult.UNKNOWN


def set_process_group():
    # with suppress(AttributeError, ImportError):
    logger.debug("setting process group")
    os.setpgrp()


class ProcessController:
    """High level orchestration class that manages the lifecycle of a task
    and its associated subprocesses.

    Parameters:
    - task: the task to be solved
    - commands: the commands to use to solve the task
    - config: the configuration for the controller
    - exit_callback: a callback that is called when one of the processes finishes
    """

    task: Task
    commands: list[Command]
    config: Config
    start_callback: Callable[[Command, Task], None] | None
    exit_callback: Callable[[Command, Task], None] | None
    _monitors: list[threading.Thread]
    _launchers: list[threading.Timer]

    def __init__(
        self,
        task: Task,
        commands: list[Command],
        config: Config,
        start_callback: Callable[[Command, Task], None] | None = None,
        exit_callback: Callable[[Command, Task], None] | None = None,
    ):
        self.task = task
        self.commands = commands
        self.config = config
        self.start_callback = start_callback
        self.exit_callback = exit_callback
        self._monitors = []
        self._launchers = []

    def start(self):
        """Start the task by spawning subprocesses for each command.

        Can only be called once, and fails if the task is not in the NOT_STARTED state.

        Transitions the task from NOT_STARTED → STARTING → RUNNING.

        This does not block, the subprocesses are monitored in separate threads. In
        order to wait for the task to finish, call join()."""

        if not self.commands:
            raise RuntimeError("No commands to run")

        # fail if we're already processing the task
        task = self.task
        task.set_status(TaskStatus.STARTING, required_status=TaskStatus.NOT_STARTED)

        set_process_group()

        interval_seconds = self.config.interval_seconds
        if interval_seconds:
            for i, command in enumerate(self.commands):
                launcher = threading.Timer(
                    i * interval_seconds, function=self._launch_process, args=(command,)
                )
                launcher.daemon = True

                self._launchers.append(launcher)
                launcher.start()

            # wait for all launchers to finish until setting status to RUNNING
            for launcher in self._launchers:
                launcher.join()

        else:
            with timer("_launch_process"):
                for command in self.commands:
                    self._launch_process(command)

        # it's possible that some processes finished already and the status has switched
        # to TERMINATING/TERMINATED, in that case we don't want to go back to RUNNING
        task.set_status(TaskStatus.RUNNING, expected_status=TaskStatus.STARTING)

    def _launch_process(self, command: Command):
        task = self.task
        if task.status != TaskStatus.STARTING:
            logger.debug(f"aborting command starts, task is {task.status!r}")
            return

        command.start()
        task.processes.append(command)

        # spawn a thread that will monitor this process
        monitor = threading.Thread(target=self._monitor_process, args=(command,))
        self._monitors.append(monitor)
        monitor.start()

        if self.start_callback:
            self.start_callback(command, task)

    def _monitor_process(self, command: Command):
        """Monitor the given process for completion, wait until configured timeout.

        If the timeout is reached, a thread is spawned to kill the process.

        :param command:
            The process to monitor.
        """

        try:
            command.wait(timeout=(self.config.timeout_seconds or None))
            if not command.done():
                raise RuntimeError(f"{command.bin_name()} not done after wait")
        except TimeoutExpired:
            logger.debug(f"timeout expired for {command.bin_name()}")
            command.has_timed_out = True
            self._kill_process(command)
        finally:
            self._on_proc_finished(command)

    def join(self):
        if self.task.status == TaskStatus.NOT_STARTED:
            raise RuntimeError("can not join controller before it is started")

        logger.debug("waiting for all launchers to finish")

        # wait on the status first, to avoid missing new launcher or monitor threads
        while self.task.status < TaskStatus.RUNNING:
            pass

        for launcher in self._launchers:
            launcher.join()

        logger.debug("waiting for all monitors to finish")
        for monitor in self._monitors:
            monitor.join()

    def kill(self) -> bool:
        """Kill all processes associated with the current task.

        :return:
            True if the task was killed, False otherwise.
        """

        # atomic lookup of the task status
        task = self.task
        task_status = task.status

        if task_status == TaskStatus.NOT_STARTED:
            raise RuntimeError("can not kill controller before it is started")

        if task_status < TaskStatus.STARTING:
            logger.debug(f"can not kill task {task.name!r} with status {task_status!r}")
            return False

        if task_status >= TaskStatus.TERMINATING:
            logger.debug(f"task {task.name!r} is already {task_status!r}")
            return False

        for launcher in self._launchers:
            logger.debug(f"cancelling launcher {launcher!r}")
            launcher.cancel()

        logger.debug(f"killing solvers for {task.name!r}")
        task.status = TaskStatus.TERMINATING
        pool: list[threading.Thread] = []

        for command in task.processes:
            killer = threading.Thread(target=self._kill_process, args=(command,))
            pool.append(killer)
            killer.start()

        if pool:
            logger.debug("waiting for all killers to finish")

        for killer in pool:
            killer.join()

        task.status = TaskStatus.TERMINATED
        return True

    def _kill_process(self, command: Command):
        if command.on_kill_list:
            logger.debug(f"{command.bin_name()} already on kill list")
            return

        # mark the command for killing
        # this is important even for unstarted commands, it marks that
        # the command is being killed and monitors can stop waiting for termination
        command.on_kill_list = True

        if not command.started():
            logger.debug(f"no need to kill {command.bin_name()}, not started yet")
            return

        if command.done():
            logger.debug(f"{command.bin_name()} already terminated")
            return

        logger.debug(f"terminating {command.bin_name()}")
        command.terminate()

        # Wait for process to terminate gracefully
        grace_period_seconds = 1
        try:
            logger.debug(f"waiting for {command.bin_name()} to terminate gracefully")
            command.wait(timeout=grace_period_seconds)
        except TimeoutExpired:
            if command.done():
                logger.debug(f"{command.bin_name()} terminated gracefully")
                return

            logger.debug(f"{command!r} still running after {grace_period_seconds}s")
            command.kill()

    def _on_proc_finished(self, command: Command):
        # Update the end_time in command
        command.end_time = time.time()

        # Close the output and error files
        try_closing(command.stdout)
        try_closing(command.stderr)

        task = self.task

        if self.exit_callback:
            self.exit_callback(command, task)

        if command.started():
            elapsed = command.elapsed()
            exitcode = command.returncode
            logger.debug(f"{command.bin_name()} returned {exitcode} in {elapsed:.2f}s")

            if (
                command.ok()
                and self.config.early_exit
                and task.status < TaskStatus.TERMINATED
            ):
                self.kill()

            # we could be in STARTING or TERMINATING here
            if task.status != TaskStatus.RUNNING:
                return

        # check if all commands have finished
        if all(command.done() for command in task.processes):
            task.set_status(TaskStatus.TERMINATED)
