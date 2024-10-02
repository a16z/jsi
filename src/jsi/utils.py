import contextlib
import io
import os
import signal
import sys
import time
from datetime import datetime
from enum import Enum


class Closeable:
    def close(self) -> None: ...


class Printable:
    def print(self, msg: object | None = None, style: object | None = None) -> None:  # type: ignore
        pass


class SimpleConsole(Printable):
    def __init__(self, file: object):
        self.file = file

    def print(self, msg: object | None = None, style: object | None = None) -> None:
        if msg is None:
            print(file=self.file)  # type: ignore
        else:
            print(msg, file=self.file)  # type: ignore

    @property
    def is_terminal(self) -> bool:
        return False


def is_terminal() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def get_consoles() -> tuple[Printable, Printable]:
    if is_terminal():
        # only pay for cost of import if we're in an interactive terminal
        from rich.console import Console

        return (Console(file=sys.stdout), Console(file=sys.stderr))  # type: ignore
    else:
        return (simple_stdout, simple_stderr)


class LogLevel(Enum):
    DISABLED = 0
    TRACE = 1
    DEBUG = 2
    INFO = 3
    WARNING = 4
    ERROR = 5


class SimpleLogger:
    level: LogLevel
    console: object | None

    def __init__(self, level: LogLevel = LogLevel.INFO):
        self.level = level
        self.console = None

    def _log(self, level: LogLevel, message: object):
        if not self.console:
            return

        if self.level == LogLevel.DISABLED:
            return

        if level.value >= self.level.value:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self.console.print(f"[{timestamp}]\t{level.name}\t{message}")  # type: ignore

    def trace(self, message: object):
        self._log(LogLevel.TRACE, message)

    def debug(self, message: object):
        self._log(LogLevel.DEBUG, message)

    def info(self, message: object):
        self._log(LogLevel.INFO, message)

    def warning(self, message: object):
        self._log(LogLevel.WARNING, message)

    def error(self, message: object):
        self._log(LogLevel.ERROR, message)

    def disable(self):
        self.level = LogLevel.DISABLED
        self.console = None

    def enable(self, console: object, level: LogLevel = LogLevel.INFO):
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


def readable_size(num: int | float) -> str:
    match num:
        case n if n < 1024:
            return f"{n:.1f}B"
        case n if n < 1024 * 1024:
            return f"{n/1024:.1f}KiB"
        case _:
            return f"{num/(1024*1024):.1f}MiB"


logger = SimpleLogger()
simple_stdout = SimpleConsole(file=sys.stdout)
simple_stderr = SimpleConsole(file=sys.stderr)
