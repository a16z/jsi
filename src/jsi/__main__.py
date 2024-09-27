"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import argparse
import atexit
import os
import shutil
import signal
import sys
import threading
from functools import partial
from pathlib import Path
from typing import Any

from jsi.core import (
    SOLVERS,
    Command,
    Config,
    ProcessController,
    Task,
    TaskResult,
    TaskStatus,
)
from jsi.utils import LogLevel, is_terminal, logger, stderr


def get_exit_callback():
    if is_terminal():
        from jsi.output.fancy import on_process_exit, status

        return partial(on_process_exit, status=status)
    else:
        from jsi.output.basic import on_process_exit

        return on_process_exit


def get_status():
    if is_terminal():
        from jsi.output.fancy import status

        return status
    else:
        from jsi.output.basic import NoopStatus

        return NoopStatus()


def get_results_table(controller: ProcessController) -> Any:
    if is_terminal():
        from jsi.output.fancy import get_results_table

        return get_results_table(controller)
    else:
        from jsi.output.basic import get_results_csv

        return get_results_csv(controller)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Just Solve It - SMT solver runner")
    parser.add_argument("file", type=Path, help="Path to the SMT2 file to solve")
    parser.add_argument("--timeout", type=float, help="Timeout in seconds")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--full-run",
        action="store_true",
        help="Run all solvers to completion, even after one succeeds",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Directory where solver output files will be written",
    )
    parser.add_argument("--version", action="version", version="jsi v0.1.0")
    parser.add_argument(
        "--supervisor",
        action="store_true",
        help="Enable supervisor process to avoid orphaned processes.",
    )
    args = parser.parse_args(sys.argv[1:])

    file = args.file
    timeout = args.timeout
    debug = args.debug
    full_run = args.full_run
    output = args.output
    supervisor = args.supervisor
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
    controller = ProcessController(task, commands, config, get_exit_callback())

    setup_signal_handlers(controller)

    stderr.print(f"Starting {len(commands)} solvers")
    stderr.print(f"Output will be written to: {output}")
    status = get_status()
    try:
        # all systems go
        controller.start()
        status.start()

        if supervisor:
            from jsi.supervisor import Supervisor

            # wait for the subprocesses to start, we need the PIDs for the supervisor
            while controller.task.status.value < TaskStatus.RUNNING.value:
                pass

            # start a supervisor process in daemon mode so that it does not block
            # the program from exiting
            child_pids = [command.pid for command in controller.commands]
            sv = Supervisor(os.getpid(), child_pids, debug=config.debug)
            sv.daemon = True
            sv.start()

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
