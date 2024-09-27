import contextlib
import io
import multiprocessing
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


@dataclass
class NullConsole:
    def print(self, *args: Any, **kwargs: Any):
        pass

    @property
    def is_terminal(self) -> bool:
        return False


@dataclass
class SimpleConsole:
    file: Any

    def print(self, *args: Any, **kwargs: Any):
        print(*args, **kwargs, file=self.file)

    @property
    def is_terminal(self) -> bool:
        return False


def is_terminal() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def get_consoles() -> tuple[Any, Any]:
    if is_terminal():
        # only pay for cost of import if we're in an interactive terminal
        from rich.console import Console
        return (Console(file=sys.stdout), Console(file=sys.stderr))
    else:
        return (SimpleConsole(file=sys.stdout), SimpleConsole(file=sys.stderr))


class LogLevel(Enum):
    DISABLED = 0
    TRACE = 1
    DEBUG = 2
    INFO = 3
    WARNING = 4
    ERROR = 5


@dataclass
class SimpleLogger:
    level: LogLevel = LogLevel.INFO
    console: Any | None = None

    def _log(self, level: LogLevel, message: str):
        if not self.console:
            return

        if self.level == LogLevel.DISABLED:
            return

        if level.value >= self.level.value:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self.console.print(f"[{timestamp}]\t{level.name}\t{message}")

    def trace(self, message: str):
        self._log(LogLevel.TRACE, message)

    def debug(self, message: str):
        self._log(LogLevel.DEBUG, message)

    def info(self, message: str):
        self._log(LogLevel.INFO, message)

    def warning(self, message: str):
        self._log(LogLevel.WARNING, message)

    def error(self, message: str):
        self._log(LogLevel.ERROR, message)

    def disable(self):
        self.level = LogLevel.DISABLED
        self.console = None

    def enable(self, console: Any, level: LogLevel = LogLevel.INFO):
        self.level = level
        self.console = console




@contextlib.contextmanager
def timer(description: str):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    logger.trace(f"{description}: {elapsed:.3f}s")


def kill_process(pid: int):
    try:
        os.kill(pid, signal.SIGTERM)
        logger.debug(f"sent SIGTERM to process {pid}")
    except ProcessLookupError:
        logger.debug(f"skipping SIGTERM for process {pid} -- not found")


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def file_loc(iowrapper: io.TextIOWrapper | int | None) -> str:
    return iowrapper.name if isinstance(iowrapper, io.TextIOWrapper) else ""


class Supervisor(multiprocessing.Process):
    """Supervisor process that monitors the parent process and its children."""

    def __init__(self, parent_pid: int, child_pids: list[int], debug: bool = False):
        super().__init__()
        self.parent_pid = parent_pid
        self.child_pids = child_pids
        self.debug = debug

    def run(self):
        if self.debug:
            logger.level = LogLevel.DEBUG

        logger.debug(f"supervisor started (PID: {self.pid})")
        logger.debug(f"watching parent (PID: {self.parent_pid})")
        logger.debug(f"watching children (PID: {self.child_pids})")

        last_message_time = time.time()
        try:
            while True:
                current_time = time.time()
                if current_time - last_message_time >= 60:
                    logger.debug(f"supervisor still running (PID: {self.pid})")
                    last_message_time = current_time

                if pid_exists(self.parent_pid):
                    time.sleep(1)
                    continue

                logger.debug(f"parent (PID {self.parent_pid} has died)")
                for pid in self.child_pids:
                    kill_process(pid)

                logger.debug("all children terminated, supervisor exiting.")
                break
        except KeyboardInterrupt:
            logger.debug("supervisor interrupted")

        logger.debug(f"supervisor exiting (PID: {self.pid})")


null_console = NullConsole()
stdout, stderr = get_consoles()
logger = SimpleLogger()
