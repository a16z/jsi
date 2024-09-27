import contextlib
import io
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


null_console = NullConsole()
stdout, stderr = get_consoles()
logger = SimpleLogger()
