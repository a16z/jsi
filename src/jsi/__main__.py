"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import atexit
import io
import os
import shutil
import signal
import sys
import threading
from functools import partial
from pathlib import Path
from typing import Any

import click
import humanize
from rich.status import Status
from rich.table import Table
from rich.text import Text

from jsi.core import (
    SOLVERS,
    Command,
    Config,
    ProcessController,
    Task,
    TaskResult,
    TaskStatus,
)
from jsi.utils import LogLevel, Supervisor, logger, stderr


def file_loc(iowrapper: io.TextIOWrapper | int | None) -> str:
    return iowrapper.name if isinstance(iowrapper, io.TextIOWrapper) else ""


def find_available_solvers() -> list[str]:
    stderr.print("checking for solvers available on PATH:")
    available: list[str] = []
    for solver in SOLVERS:
        if shutil.which(solver) is not None:
            available.append(solver)
            stderr.print(f"{solver:>12} [green]OK[/green]")
        else:
            stderr.print(f"{solver:>12} not found")

    stderr.print()
    return available


def result_color(result: TaskResult) -> str:
    if result in (TaskResult.SAT, TaskResult.UNSAT):
        return "green"

    if result in (TaskResult.ERROR,):
        return "red"

    if result in (TaskResult.TIMEOUT, TaskResult.KILLED):
        return "yellow"

    return "white"


def styled_result(result: TaskResult) -> Text:
    return Text(result.value, style=result_color(result))


def styled_size(size: int) -> Text:
    return Text(humanize.naturalsize(size, gnu=True))


def styled_output(command: Command) -> Text:
    return Text(file_loc(command.stdout), style="magenta")


def get_results_table(controller: ProcessController) -> Table:
    table = Table(title="Results")
    table.add_column("solver", style="cyan")
    table.add_column("result")
    table.add_column("exit", style="magenta", justify="right")
    table.add_column("time", justify="right", style="yellow")
    table.add_column("output file", justify="left", style="magenta", overflow="fold")
    table.add_column("size", justify="right")

    commands = controller.commands
    for command in sorted(commands, key=lambda x: (not x.ok(), x.elapsed() or 0)):
        table.add_row(
            command.name,
            styled_result(command.result()),
            str(command.returncode) if command.returncode is not None else "N/A",
            f"{command.elapsed():.2f}s" if command.elapsed() else "N/A",
            styled_output(command) if command.stdout else "N/A",
            styled_size(len(command.stdout_text) if command.stdout_text else 0),
        )

    return table


def on_process_exit(command: Command, task: Task, status: Status):
    if task.status > TaskStatus.RUNNING:
        return

    # would be unexpected
    if not command.done():
        return

    if command.result() == TaskResult.TIMEOUT:
        return

    message = Text.assemble(
        (command.name, "cyan bold"),
        " returned ",
        styled_result(command.result()),
    )
    stderr.print(message)

    not_done = sum(1 for command in task.processes if not command.done())
    status.update(f"{not_done} solvers still running (Ctrl-C to stop)")


def setup_signal_handlers(controller: ProcessController):
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


@click.command()
@click.version_option()
@click.option("--timeout", type=float, help="Timeout in seconds.", show_default=True)
@click.option("--debug", type=bool, help="Enable debug logging.", is_flag=True)
@click.option(
    "--full-run",
    type=bool,
    help=(
        "Run all solvers to completion (by default, the first solver to finish will"
        " cause the others to be terminated)."
    ),
    is_flag=True,
    default=False,
    show_default=True,
)
@click.option(
    "--output",
    type=click.Path(exists=True, dir_okay=True, file_okay=False, path_type=Path),
    required=False,
    help="Directory where solver output files will be written.",
)
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
def main(
    file: Path, timeout: float, debug: bool, output: Path | None, full_run: bool
) -> int:
    if debug:
        logger.enable(console=stderr, level=LogLevel.DEBUG)


    solvers = find_available_solvers()
    if not solvers:
        stderr.print("No solvers found on PATH", style="red")
        return 1

    # output directory defaults to the parent of the input file
    if not output:
        output = file.parent

    # build the commands to run the solvers
    commands: list[Command] = []
    for solver in solvers:
        command = Command(
            name=solver,
            args=SOLVERS[solver],
            input_file=file,
        )

        stdout_file = str(output / f"{file.stem}.{solver}.out")

        # TODO: handle output file creation failure
        if not stdout_file:
            raise RuntimeError(f"failed to create output file for {command}")

        command.stdout = open(stdout_file, "w")  # noqa: SIM115
        commands.append(command)

    # initialize the controller
    task = Task(name=str(file))
    config = Config(timeout_seconds=timeout, debug=debug, early_exit=not full_run)
    status_message = "waiting for solvers (press ^C to stop)"
    status = Status(status_message, spinner="noise", console=stderr)
    exit_callback = partial(on_process_exit, status=status)
    controller = ProcessController(task, commands, config, exit_callback)

    setup_signal_handlers(controller)

    stderr.print(f"Starting {len(commands)} solvers")
    stderr.print(f"Output will be written to: {output}")
    try:
        # all systems go
        controller.start()
        status.start()

        # wait for the solver processes to start, we need the PIDs for the supervisor
        while controller.task.status.value < TaskStatus.RUNNING.value:
            pass

        # start a supervisor process in daemon mode so that it does not block
        # the program from exiting
        child_pids = [command.pid for command in controller.commands]
        supervisor = Supervisor(os.getpid(), child_pids, debug=config.debug)
        supervisor.daemon = True
        supervisor.start()

        # wait for the solvers to finish
        controller.join()

        return 0 if task.result in (TaskResult.SAT, TaskResult.UNSAT) else 1
    except KeyboardInterrupt:
        controller.kill()
        return 1
    finally:
        status.stop()
        for command in sorted(controller.commands, key=lambda x: x.elapsed() or 0):
            if command.done() and command.ok():
                if stdout_text := command.stdout_text:
                    print(stdout_text.strip())
                    print(f"; (showing result for {command.name})")
                break

        table = get_results_table(controller)
        stderr.print(table)


if __name__ == "__main__":
    sys.exit(main())
