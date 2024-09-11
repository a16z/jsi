# postpone the evaluation of annotations, treating them as strings at runtime
from __future__ import annotations

import enum
import io
import os
import pathlib
import threading
import time
from dataclasses import dataclass, field
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
    "yices-smt2": "yices-smt2".split(),
    "z3": "z3 --model".split(),
}


class TaskResult(enum.Enum):
    SAT = sat
    UNSAT = unsat
    ERROR = error
    UNKNOWN = unknown
    TIMEOUT = timeout
    KILLED = killed


class TaskStatus(enum.Enum):
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


@dataclass(frozen=True)
class Config:
    early_exit: bool = True
    timeout_seconds: float = 0
    debug: bool = False


@dataclass(frozen=True)
class Command:
    """High level inputs for a command to be run by in a subprocess."""

    args: list[str]
    stdout: io.TextIOWrapper | None = None
    stderr: io.TextIOWrapper | None = None

    # extra arguments to pass to Popen
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class DelayedPopen:
    """A mutable class that:
    - accepts a Command as input
    - exposes synchronized access to a start() method
    - proxies data access to the underlying Popen instance (once started)"""

    command: Command
    process: Popen[str] | None = None
    lock: threading.Lock = threading.Lock()

    def start(self) -> Popen[str]:
        with self.lock:
            if self.process is not None:
                raise RuntimeError("Process already started")

            self.process = Popen(self.command, **self.kwargs)  # type: ignore
            return self.process

    def __getattr__(self, name: str) -> Any:
        if not self.is_started():
            raise AttributeError("Process not started. Call start() first.")
        return getattr(self.process, name)

    def is_started(self):
        with self.lock:
            return self.process is not None

    def wait(self, timeout: float | None = None):
        with self.lock:
            # skip waiting if the process is not started
            if self.process is None:
                return

        return self.process.wait(timeout)


@dataclass
class ProcessMetadata:
    """Mutable class that keeps track of the state of a process metadata
    such as start time, end time, whether it has timed out, etc."""

    process: DelayedPopen
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    has_timed_out: bool = False
    on_kill_list: bool = False
    _result: str | None = None

    def bin_name(self):
        return self.process.command.args[0]

    def done(self):
        return self.process.is_started() and self.process.poll() is not None

    def _ensure_finished(self):
        if not self.done():
            raise ValueError(f"process {self.process!r} is still running")

    def ok(self):
        self._ensure_finished()

        # unfortunately can't just use returncode == 0 here because:
        # - stp can return 0 when it fails to parse the input file
        # - boolector returns non 0 even when it's happy
        # - ...

        return self.result() in (TaskResult.SAT, TaskResult.UNSAT)

    def _get_result(self):
        if self.process.returncode == -15:
            return timeout if self.has_timed_out else killed

        # FIXME: currently assumes that stdout is a file
        with open(self.process.stdout) as f:
            line = f.readline()
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
    processes: list[ProcessMetadata] = field(default_factory=list)
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
            if new_status.value < self._status.value:
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
            if new_status.value < status.value:
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

        task = self.task

        # fail if we're already processing the task
        task.set_status(TaskStatus.STARTING, required_status=TaskStatus.NOT_STARTED)

        set_process_group()

        for command in self.commands:
            proc = DelayedPopen(command)
            proc_meta = ProcessMetadata(process=proc,)
            task.processes.append(proc_meta)

            # spawn a thread that will monitor this process
            monitor = threading.Thread(target=self._monitor_process, args=(proc_meta,))
            self.monitors.append(monitor)
            monitor.start()

        # it's possible that some processes finished already and the status has switched
        # to TERMINATING/TERMINATED, in that case we don't want to go back to RUNNING
        task.set_status(TaskStatus.RUNNING, expected_status=TaskStatus.STARTING)

    def _monitor_process(self, proc_meta: ProcessMetadata):
        """Monitor the given process for completion.

        :param proc_meta:
            The process to monitor.
        """

        bin_name = proc_meta.bin_name()
        try:
            # Wait for the process to complete
            # [note](https://docs.python.org/3/library/asyncio-subprocess.html#asyncio.subprocess.Process)
            #   the Process.wait() method is asynchronous,
            #   whereas subprocess.Popen.wait() is implemented as a blocking busy loop;
            task_status = self.task.status
            if task_status == TaskStatus.STARTING:
                logger.debug(f"starting {bin_name}")
                proc_meta.process.start()
                proc_meta.process.wait(timeout=(self.config.timeout_seconds or None))
            else:
                logger.debug(f"not starting {bin_name}, task is {task_status}")
        except TimeoutExpired:
            logger.debug(f"timeout expired for {bin_name}")
            proc_meta.has_timed_out = True

            # spawn a killer thread
            threading.Thread(target=self._kill_process, args=(proc_meta,)).start()
        finally:
            # Close the output and error files
            if stdout := proc_meta.process.stdout:
                stdout.close()

            if stderr := proc_meta.process.stderr:
                stderr.close()

            # notify the controller that the process has finished
            if proc_meta.done():
                self._on_process_finished(proc_meta)

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

        for proc_meta in task.processes:
            bin_name = proc_meta.bin_name()
            if not proc_meta.process.is_started():
                logger.debug(f"not killing unstarted process {bin_name}")
                continue

            if proc_meta.on_kill_list:
                logger.debug(f"{bin_name} already marked for killing")
                continue

            if proc_meta.done():
                logger.debug(f"{bin_name} already terminated")
                continue

            logger.debug(f"terminating {bin_name}")
            proc_meta.on_kill_list = True
            killer = threading.Thread(target=self._kill_process, args=(proc_meta,))
            pool.append(killer)
            killer.start()

        if pool:
            logger.debug("waiting for all killers to finish")

        for killer in pool:
            killer.join()

        task.status = TaskStatus.TERMINATED
        return True

    def _kill_process(self, proc_meta: ProcessMetadata):
        process = proc_meta.process
        if process.poll() is None:
            process.terminate()

        # Wait for process to terminate gracefully
        grace_period_seconds = 1
        try:
            process.wait(timeout=grace_period_seconds)
        except TimeoutExpired:
            if process.poll() is None:
                logger.debug(
                    f"process {process!r} still running after {grace_period_seconds}s"
                )
                process.kill()

    def _on_process_finished(self, proc_meta: ProcessMetadata):
        # Update the end_time in proc_meta
        proc_meta.end_time = time.time()

        elapsed = proc_meta.end_time - proc_meta.start_time
        exitcode = proc_meta.process.returncode

        # only log "natural" exits, ignore kills
        if not proc_meta.on_kill_list:
            logger.info(
                f"{proc_meta.bin_name()} returned {exitcode} in {elapsed:.2f}s"
            )

        if (
            proc_meta.ok()
            and self.config.early_exit
            and self.task.status.value < TaskStatus.TERMINATED.value
        ):
            self.kill()
            logger.debug(f"setting result to {proc_meta.result()}")
            self.task.result = proc_meta.result()
