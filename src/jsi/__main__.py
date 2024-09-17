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

from jsi.core import (
    SOLVERS,
    Command,
    Config,
    ProcessController,
    Task,
    TaskResult,
    TaskStatus,
)
from jsi.utils import Supervisor

logger.disable("jsi")


@click.command()
@click.version_option()
@click.option("--timeout", type=float, help="timeout in seconds", default=0)
@click.option("--debug", type=bool, help="enable debug logging", is_flag=True)
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
    required=True,
)
def main(file: pathlib.Path, timeout: float, debug: bool) -> int:
    if debug:
        logger.enable("jsi")

    config = Config(timeout_seconds=timeout, debug=debug)
    task = Task(name=str(file))

    # TODO: stdout, stderr redirects
    commands = [
        Command(executable=cmd[0], args=cmd[1:], input_file=file)
        for cmd in SOLVERS.values()
    ]

    controller = ProcessController(task, commands, config)
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
        child_pids = [command.pid for command in controller.commands]
        supervisor = Supervisor(os.getpid(), child_pids, debug=config.debug)
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
