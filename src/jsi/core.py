# postpone the evaluation of annotations, treating them as strings at runtime
from __future__ import annotations

import enum
import io
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import click

sat, unsat, error, unknown, timeout, killed = (
    "sat",
    "unsat",
    "error",
    "unknown",
    "timeout",
    "killed",
)

SOLVERS = {
    "bitwuzla": "bitwuzla --produce-models".split(),
    "boolector": "boolector --model-gen --output-number-format=hex".split(),
    "cvc4": "cvc4 --produce-models".split(),
    "cvc5": "cvc5 --produce-models".split(),
    "stp": "stp --print-counterex --SMTLIB2".split(),
    "yices-smt2": "yices-smt2".split(),  # needs to include (get-model) in the SMT2 file to generate the model
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
    RUNNING = 2
    TERMINATING = 3
    TERMINATED = 4


@dataclass(frozen=True)
class Config:
    early_exit: bool = False
    timeout_seconds: float | None = None


@dataclass
class ProcessMetadata:
    solver_name: str
    task_name: str
    output_file: io.TextIOWrapper
    error_file: io.TextIOWrapper | None
    process: subprocess.Popen[bytes]
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    timeout_expired: float = False
    _result: str | None = None

    def done(self):
        return self.process.poll() is not None

    def _ensure_finished(self):
        if not self.done():
            raise ValueError(f"process {self.process!r} is still running")

    def ok(self):
        self._ensure_finished()

        # unfortunately can't just use returncode == 0 here because:
        # - stp can return 0 when it fails to parse the input file
        # - boolector returns non 0 even when it's happy
        # - ...

        return self.result() in (sat, unsat)

    def _get_result(self):
        if self.process.returncode == -15:
            return timeout if self.timeout_expired else killed

        with open(self.output_file.name, "r") as f:
            line = f.readline()
            if line == "sat\n":
                return sat
            elif line == "unsat\n":
                return unsat
            elif "error" in line:
                return error
            elif "ASSERT(" in line:
                return sat  # stp may not return sat as the first line when there is a counterexample
            elif self.timeout_expired:
                return timeout
            else:
                return unknown

    def result(self):
        self._ensure_finished()
        if self._result is None:
            self._result = self._get_result()
        return self._result


@dataclass
class Task:
    name: str
    status: TaskStatus = TaskStatus.NOT_STARTED
    processes: list[ProcessMetadata] = field(default_factory=list)
    result: TaskResult | None = None
    output: str | None = None


@dataclass(frozen=True)
class ProcessController:
    config: Config
    solvers: list[str]
    task: Task


    def join(self):
        # TODO: keep or kill?
        ...


    def start(self):
        task = self.task

        if task.status != TaskStatus.NOT_STARTED:
            raise ValueError(f"already processing task {task.name!r}")

        task.status = TaskStatus.RUNNING

        smt_file = task.name
        for solver in self.solvers:
            logger.debug(f"starting {solver}")
            command = SOLVERS[solver] + [smt_file]
            output_file = open(f"{smt_file}.{solver}.out", "w")

            # most solvers don't write to stderr, so avoid creating the extra files
            error_file = None
            proc = subprocess.Popen(command, stdout=output_file, stderr=error_file)

            proc_meta = ProcessMetadata(
                solver_name=solver,
                task_name=smt_file,
                output_file=output_file,
                error_file=error_file,
                process=proc,
            )

            task.processes.append(proc_meta)

            # spawn a thread that will monitor this process
            threading.Thread(target=self._monitor_process, args=(proc_meta,)).start()


    def kill_task(self) -> bool:
        """Kill all processes associated with the current task.

        :return:
            True if the task was killed, False otherwise.
        """

        task = self.task
        if task.status != TaskStatus.RUNNING:
            logger.debug(f"can not kill task {task.name!r} with status {task.status!r}")
            return False

        logger.debug(f"killing {task.name!r}")
        task.status = TaskStatus.TERMINATING
        pool: list[threading.Thread] = []
        for proc_meta in task.processes:
            if proc_meta.process.poll() is None:
                killer = threading.Thread(target=self._kill_process, args=(proc_meta,))
                pool.append(killer)
                killer.start()

        for killer in pool:
            killer.join()

        task.status = TaskStatus.TERMINATED
        return True


    def _on_task_finished(self, task: Task):
        """Called when the given task has finished.

        :param task:
            The task that has finished.
        """

        pass


    def _monitor_process(self, proc_meta: ProcessMetadata):
        """Monitor the given process for completion.

        :param proc_meta:
            The process to monitor.
        """
        try:
            # Wait for the process to complete
            # [note](https://docs.python.org/3/library/asyncio-subprocess.html#asyncio.subprocess.Process)
            #   the Process.wait() method is asynchronous,
            #   whereas subprocess.Popen.wait() method is implemented as a blocking busy loop;
            proc_meta.process.wait(timeout=(self.config.timeout_seconds or None))
        except subprocess.TimeoutExpired:
            logger.debug(
                f"timeout expired for {proc_meta.solver_name} on {proc_meta.task_name}"
            )
            proc_meta.timeout_expired = True

            # spawn a killer thread
            threading.Thread(target=self._kill_process, args=(proc_meta,)).start()
        finally:
            # Close the output and error files
            proc_meta.output_file.close()

            if proc_meta.error_file:
                proc_meta.error_file.close()

            # notify the controller that the process has finished
            self._on_process_finished(proc_meta)

    def _kill_process(self, proc_meta: ProcessMetadata):
        process = proc_meta.process
        if process.poll() is None:
            logger.debug(f"terminating {process!r}")
            process.terminate()

        # Wait for process to terminate gracefully
        grace_period_seconds = 1
        try:
            process.wait(timeout=grace_period_seconds)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                logger.debug(
                    f"process {process!r} still running after {grace_period_seconds} seconds, killing it"
                )
                process.kill()

    def _on_process_finished(self, proc_meta: ProcessMetadata):
        # Update the end_time in proc_meta
        proc_meta.end_time = time.time()
        elapsed = proc_meta.end_time - proc_meta.start_time

        exitcode = proc_meta.process.returncode
        logger.info(f"{proc_meta.solver_name} returned {exitcode} in {elapsed:.2f}s")

        task = self.task
        assert task is not None
        if self.config.early_exit:
            if task.status == TaskStatus.RUNNING and proc_meta.ok():
                self.kill_task()



def solve(smtfile: click.Path) -> tuple[TaskResult, str]:
    return TaskResult.UNKNOWN, ""
