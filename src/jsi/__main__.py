"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import contextlib
import pathlib
import sys
import signal
import threading
import time
from typing import Any

import click
from loguru import logger

from jsi.core import Config, ProcessController, SOLVERS, Task, TaskResult


@contextlib.contextmanager
def timer(description: str):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    logger.trace(f"{description}: {elapsed:.3f}s")


@click.command()
@click.version_option()
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
    required=True,
)
def main(file: pathlib.Path) -> int:
    config = Config()
    task = Task(name=str(file))
    controller = ProcessController(config, list(SOLVERS.keys()), task)

    event = threading.Event()

    def signal_listener(signum: int, frame: Any | None = None):
        event.set()
        thread_name = threading.current_thread().name
        logger.debug(f"Signal {signum} received in thread: {thread_name}")

    def signal_handler():
        event.wait()
        controller.kill_task()
        sys.exit(1)

    # register the signal listener
    for signum in [
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGQUIT,
        signal.SIGHUP,
    ]:
        signal.signal(signum, signal_listener)

    # start a signal handling thread
    thread = threading.Thread(target=signal_handler)
    thread.start()

    try:
        controller.start()
        controller.join()

        click.echo(task.result.value)
        # click.echo(task.result.output)
        return 0 if task.result in (TaskResult.SAT, TaskResult.UNSAT) else 1
    except KeyboardInterrupt:
        controller.kill_task()
        return 1


if __name__ == "__main__":
    sys.exit(main())
