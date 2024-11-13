import contextlib
import io
import os
import signal
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path


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


def unexpand_home(path: str | Path) -> str:
    return str(path).replace(str(Path.home()), "~")


def is_terminal(file: object) -> bool:
    return hasattr(file, "isatty") and file.isatty()  # type: ignore


def get_console(file: object) -> Printable:
    if is_terminal(file):
        from rich.console import Console

        return Console(file=file)  # type: ignore

    # if not a terminal, use a simple console
    match file:
        case sys.stdout:
            return simple_stdout
        case sys.stderr:
            return simple_stderr
        case _:
            return SimpleConsole(file=file)


# always return a simple console for stdout, and optionally a rich console for stderr
def get_consoles() -> tuple[Printable, Printable]:
    return (simple_stdout, get_console(sys.stderr))


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
            return f"{n}B"
        case n if n < 1024 * 1024:
            return f"{n/1024:.1f}KiB"
        case _:
            return f"{num/(1024*1024):.1f}MiB"


def num_solvers_str(num: int) -> str:
    return "1 solver" if num == 1 else f"{num} solvers"


logger = SimpleLogger()
simple_stdout = SimpleConsole(file=sys.stdout)
simple_stderr = SimpleConsole(file=sys.stderr)
