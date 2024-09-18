# postpone the evaluation of annotations, treating them as strings at runtime
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from subprocess import Popen, TimeoutExpired
from typing import Any

from loguru import logger

sat, unsat, error, unknown, timeout, killed = (
    "sat",
    "unsat",
    "error",
    "unknown",
    "timeout",
    "killed",
)

# for yices, needs to include (get-model) in the SMT2 file to generate the model
SOLVERS = {
    "bitwuzla": "bitwuzla --produce-models".split(),
    "boolector": "boolector --model-gen --output-number-format=hex".split(),
    "cvc4": "cvc4 --produce-models".split(),
    "cvc5": "cvc5 --produce-models".split(),
    "stp": "stp --print-counterex --SMTLIB2".split(),
    "yices-smt2": [],
    "z3": "z3 --model".split(),
}


def try_closing(file: Any):
    if hasattr(file, "close"):
        with contextlib.suppress(Exception):
            file.close()


def first_line(file: Any) -> str:
    try:
        if hasattr(file, "seekable") and file.seekable():
            file.seek(0)
        first_line = file.readline()
    except io.UnsupportedOperation:
        # If seeking fails, try reading without seeking
        first_line = file.readline()

    if isinstance(first_line, bytes):
        first_line = first_line.decode("utf-8")

    return first_line


class TaskResult(Enum):
    SAT = sat
    UNSAT = unsat
    ERROR = error
    UNKNOWN = unknown
    TIMEOUT = timeout
    KILLED = killed


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


@dataclass(frozen=True)
class Config:
    early_exit: bool = True
    timeout_seconds: float = 0
    debug: bool = False


@dataclass
class Command:
    """High level wrapper for a subprocess, with extra metadata (start/end time,
    timeout, etc).

    Does not spawn a process until start() is called.

    Proxies data access to the underlying Popen instance (once started)."""

    executable: str
    args: Sequence[str] = field(default_factory=list)
    input_file: pathlib.Path | None = None
    stdout: io.TextIOWrapper | int | None = None
    stderr: io.TextIOWrapper | int | None = None

    # extra arguments to pass to Popen
    kwargs: dict[str, Any] = field(default_factory=dict)

    # metadata
    start_time: float | None = None
    end_time: float | None = None
    has_timed_out: bool = False
    on_kill_list: bool = False

    # internal fields
    _process: Popen[str] | None = None
    _lock: threading.Lock = threading.Lock()
    _result: str | None = None

    # to facilitate testing
    start_delay_ms: int = 0

    def parts(self) -> list[str]:
        parts = [self.executable, *self.args]
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
            else:
                logger.debug(f"starting {self.bin_name()}")
                self.start_time = time.time()
                self._process = Popen(
                    self.parts(), **self.kwargs, stdout=self.stdout, stderr=self.stderr
                )  # type: ignore

    def wait(self, timeout: float | None = None):
        # skip waiting if the process is not started
        if self._process is None:
            return

        return self._process.wait(timeout)

    def bin_name(self):
        return self.executable

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

    def _get_result(self):
        if self._process is None:
            raise RuntimeError("Process not started")

        if self._process.returncode == -15:
            return timeout if self.has_timed_out else killed

        # FIXME: currently assumes that stdout is a file
        stdout = self._process.stdout

        if not stdout:
            raise RuntimeError("no stdout")

        line = first_line(stdout)
        if line == "sat\n":
            return sat
        elif line == "unsat\n":
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
        self._ensure_finished()
        if self._result is None:
            self._result = self._get_result()
        return TaskResult(self._result)

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
        self._ensure_finished()
        assert self._process is not None
        return self._process.returncode

    @property
    def pid(self):
        self._ensure_started()
        assert self._process is not None
        return self._process.pid


@dataclass
class Task:
    """Mutable class that keeps track of a high level task (query to be solved),
    involving potentially multiple solver subprocesses.

    Exposes synchronization primitives and enforces valid state transitions:
    NOT_STARTED → STARTING → RUNNING → TERMINATING → TERMINATED

    It is possible to skip states forward, but going back is not possible, e.g.:
    - STARTING → TERMINATING is allowed
    - RUNNING → NOT_STARTED is not allowed"""

    name: str
    processes: list[Command] = field(default_factory=list)
    output: str | None = None
    _result: TaskResult | None = None
    _status: TaskStatus = field(default=TaskStatus.NOT_STARTED, repr=False)
    _lock: threading.Lock = threading.Lock()

    @property
    def status(self):
        with self._lock:
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
            if new_status < status:
                raise ValueError(f"can not switch from {status} to {new_status}")

            # hard error
            if required_status is not None and status != required_status:
                raise ValueError(f"expected status {required_status}, got {status}")

            # soft error
            if expected_status is not None and status != expected_status:
                logger.warning(f"expected status {expected_status}, got {status}")
                return

            logger.debug(f"setting status to {new_status}")
            self._status = new_status

    @property
    def result(self) -> TaskResult:
        with self._lock:
            return self._result or TaskResult.UNKNOWN

    @result.setter
    def result(self, result: TaskResult):
        with self._lock:
            if self._result is not None:
                logger.warning(f"result already set to {self._result}")
            self._result = result


def set_process_group():
    # with suppress(AttributeError, ImportError):
    logger.debug("setting process group")
    os.setpgrp()


@dataclass(frozen=True)
class ProcessController:
    """High level orchestration class that manages the lifecycle of a task
    and its associated subprocesses.

    Parameters:
    - task: the task to be solved
    - commands: the commands to use to solve the task
    - config: the configuration for the controller
    """

    task: Task
    commands: list[Command]
    config: Config
    monitors: list[threading.Thread] = field(default_factory=list)

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

        for command in self.commands:
            task.processes.append(command)

            # spawn a thread that will monitor this process
            monitor = threading.Thread(target=self._monitor_process, args=(command,))
            self.monitors.append(monitor)
            monitor.start()

        # it's possible that some processes finished already and the status has switched
        # to TERMINATING/TERMINATED, in that case we don't want to go back to RUNNING
        task.set_status(TaskStatus.RUNNING, expected_status=TaskStatus.STARTING)

    def _monitor_process(self, command: Command):
        """Monitor the given process for completion.

        :param command:
            The process to monitor.
        """

        bin_name = command.bin_name()
        try:
            # Wait for the process to complete
            # [note](https://docs.python.org/3/library/asyncio-subprocess.html#asyncio.subprocess.Process)
            #   the Process.wait() method is asynchronous,
            #   whereas subprocess.Popen.wait() is implemented as a blocking busy loop;
            task_status = self.task.status
            if task_status == TaskStatus.STARTING:
                logger.debug(f"starting {bin_name}")
                command.start()
                command.wait(timeout=(self.config.timeout_seconds or None))
            else:
                logger.debug(f"not starting {bin_name}, task is {task_status}")
        except TimeoutExpired:
            logger.debug(f"timeout expired for {bin_name}")
            command.has_timed_out = True

            # spawn a killer thread
            threading.Thread(target=self._kill_process, args=(command,)).start()
        finally:
            # Close the output and error files
            try_closing(command.stdout)
            try_closing(command.stderr)

            # notify the controller that the process has finished
            if command.started() and command.done():
                self._on_process_finished(command)

    def join(self):
        # TODO: add a timeout to avoid hanging forever
        # TODO: what if the monitors have not be started yet?
        # TODO: enforce specific task status?
        for monitor in self.monitors:
            monitor.join()

    def kill(self) -> bool:
        """Kill all processes associated with the current task.

        :return:
            True if the task was killed, False otherwise.
        """

        logger.debug("killing all processes")

        task = self.task

        # atomic lookup of the task status (and acquire the lock only once)
        task_status = task.status

        if task_status.value < TaskStatus.RUNNING.value:
            logger.debug(f"can not kill task {task.name!r} with status {task_status!r}")
            return False

        if task_status.value >= TaskStatus.TERMINATING.value:
            logger.debug(f"task {task.name!r} is already {task_status!r}")
            return False

        logger.debug(f"killing solvers for {task.name!r}")
        task.status = TaskStatus.TERMINATING
        pool: list[threading.Thread] = []

        for command in task.processes:
            bin_name = command.bin_name()
            if not command.started():
                logger.debug(f"not killing unstarted process {bin_name}")
                continue

            if command.on_kill_list:
                logger.debug(f"{bin_name} already marked for killing")
                continue

            if command.done():
                logger.debug(f"{bin_name} already terminated")
                continue

            logger.debug(f"terminating {bin_name}")
            command.on_kill_list = True
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
        if not command.done():
            command.terminate()

        # Wait for process to terminate gracefully
        grace_period_seconds = 1
        try:
            command.wait(timeout=grace_period_seconds)
        except TimeoutExpired:
            if command.done():
                return

            logger.debug(f"{command!r} still running after {grace_period_seconds}s")
            command.kill()

    def _on_process_finished(self, command: Command):
        # Update the end_time in command
        command.end_time = time.time()

        elapsed = command.elapsed()
        exitcode = command.returncode

        # only log "natural" exits, ignore kills
        if not command.on_kill_list:
            logger.info(f"{command.bin_name()} returned {exitcode} in {elapsed:.2f}s")

        task = self.task
        if (
            command.ok()
            and self.config.early_exit
            and task.status < TaskStatus.TERMINATED
        ):
            self.kill()
            logger.debug(f"setting result to {command.result()}")
            task.result = command.result()

        # we could be in STARTING or TERMINATING here
        if task.status != TaskStatus.RUNNING:
            return

        # check if all commands have finished
        if all(command.done() for command in task.processes):
            task.set_status(TaskStatus.TERMINATED)
            # set task result if it is not already set
            if task.result is TaskResult.UNKNOWN:
                task.result = command.result()
