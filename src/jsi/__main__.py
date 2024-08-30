"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import atexit
import os
import pathlib
import signal
import sys
import threading
from typing import Any

import click
from loguru import logger

from jsi.core import SOLVERS, Config, ProcessController, Task, TaskResult, TaskStatus
from jsi.utils import Supervisor


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
        cleanup()

    def cleanup():
        controller.kill()

    # register the signal listener
    for signum in [
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGQUIT,
        signal.SIGHUP,
    ]:
        signal.signal(signum, signal_listener)

    # start a signal handling thread in daemon mode so that it does not block
    # the program from exiting
    signal_handler_thread = threading.Thread(target=signal_handler, daemon=True)
    signal_handler_thread.start()

    # also register the cleanup function to be called on exit
    atexit.register(cleanup)

    try:
        # start the solver processes
        controller.start()

        # wait for the solver processes to start, we need the PIDs for the supervisor
        while controller.task.status.value < TaskStatus.RUNNING.value:
            pass

        # start a supervisor process in daemon mode so that it does not block
        # the program from exiting
        child_pids = [proc_meta.process.pid for proc_meta in controller.task.processes]
        supervisor = Supervisor(os.getpid(), child_pids)
        supervisor.daemon = True
        supervisor.start()

        # wait for the solver processes to finish
        controller.join()

        click.echo(task.result.value)
        return 0 if task.result in (TaskResult.SAT, TaskResult.UNSAT) else 1
    except KeyboardInterrupt:
        controller.kill()
        return 1


if __name__ == "__main__":
    sys.exit(main())
