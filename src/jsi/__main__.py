"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import atexit
import os
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import click
from loguru import logger
from rich.console import Console

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

console = Console()


def qprint(*args: Any) -> None:
    """quiet print, only print if in interactive terminal"""
    if console.is_terminal:
        console.print(*args)


def find_available_solvers() -> list[str]:
    qprint("checking for solvers available on PATH:")
    available: list[str] = []
    for solver in SOLVERS:
        if shutil.which(solver) is not None:
            available.append(solver)
            qprint(f"{solver:>12} [green]OK[/green]")
        else:
            qprint(f"{solver:>12} not found")

    qprint()
    return available


@click.command()
@click.version_option()
@click.option("--timeout", type=float, help="Timeout in seconds.", show_default=True)
@click.option("--debug", type=bool, help="Enable debug logging.", is_flag=True)
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--output",
    type=click.Path(exists=True, dir_okay=True, file_okay=False, path_type=Path),
    required=False,
    help="Directory where solver output files will be written.",
)
def main(file: Path, timeout: float, debug: bool, output_dir: Path | None) -> int:
    if debug:
        logger.enable("jsi")

    solvers = find_available_solvers()
    if not solvers:
        console.print("[red]No solvers found on PATH[/red]")
        return 1

    config = Config(timeout_seconds=timeout, debug=debug)
    task = Task(name=str(file))

    if not output_dir:
        output_dir = file.parent

    # TODO: stdout, stderr redirects
    commands: list[Command] = []
    for solver in solvers:
        command = Command(
            id=solver,
            args=SOLVERS[solver],
            input_file=file,
        )

        stdout_file = str(output_dir / f"{file.stem}.{solver}.out")

        # TODO: handle output file creation failure
        if not stdout_file:
            raise RuntimeError(f"failed to create output file for {command}")

        command.stdout = open(stdout_file, "w")  # noqa: SIM115
        commands.append(command)

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
