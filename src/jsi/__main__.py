"""Usage: jsi [OPTIONS] FILE

Options:
  --timeout FLOAT     Timeout in seconds.
  --debug             Enable debug logging.
  --full-run          Run all solvers to completion even if one succeeds.
  --output DIRECTORY  Directory where solver output files will be written.
  --supervisor        Run a supervisor process to avoid orphaned subprocesses.
  --version           Show the version and exit.
  --help              Show this message and exit.
"""

import atexit
import os
import signal
import sys
import threading
from functools import partial

from jsi.core import (
    SOLVERS,
    Command,
    Config,
    ProcessController,
    Task,
    TaskResult,
    TaskStatus,
)
from jsi.utils import (
    LogLevel,
    is_terminal,
    logger,
    simple_stderr,
    simple_stdout,
)

stdout, stderr = simple_stdout, simple_stderr
jsi_home = os.path.expanduser("~/.jsi")
solver_paths = os.path.join(jsi_home, "solvers.json")


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


def get_results_table(controller: ProcessController) -> str | object:
    if is_terminal():
        from jsi.output.fancy import get_results_table

        return get_results_table(controller)
    else:
        from jsi.output.basic import get_results_csv

        return get_results_csv(controller)


def find_available_solvers() -> list[str]:
    if os.path.exists(solver_paths):
        import json
        with open(solver_paths) as f:
            paths = json.load(f)

        available = list(paths.keys())
        if available:
            return available

    stderr.print("checking for solvers available on PATH:")
    available: list[str] = []
    paths: dict[str, str] = {}

    import shutil
    for solver in SOLVERS:
        path = shutil.which(solver)  # type: ignore

        if path is None:
            stderr.print(f"{solver:>12} not found")
            continue

        paths[solver] = path
        available.append(solver)
        stderr.print(f"{solver:>12} [green]OK[/green]")

    stderr.print()

    # save the paths to the solver_paths file
    if paths:
        import json
        if not os.path.exists(jsi_home):
            os.makedirs(jsi_home)

        with open(solver_paths, "w") as f:
            json.dump(paths, f)

    stderr.print(f"found {len(available)} solvers")
    return available


def setup_signal_handlers(controller: ProcessController):
    event = threading.Event()

    def signal_listener(signum: int, frame: object | None = None):
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


def parse_args(args: list[str]) -> Config:
    config = Config()

    for arg in args:
        match arg:
            case "--version":
                stdout.print("jsi v0.1.0")
                sys.exit(0)
            case "--help":
                stdout.print(__doc__)
                sys.exit(0)
            case "--debug":
                config.debug = True
            case "--full-run":
                config.early_exit = False
            case "--output":
                config.output_dir = arg
            case "--supervisor":
                config.supervisor = True
            case "--timeout":
                config.timeout_seconds = float(arg)
            case _:
                config.input_file = arg

    if not config.input_file:
        stderr.print("error: no input file provided", style="red")
        sys.exit(1)

    # output directory defaults to the parent of the input file
    if config.output_dir is None:
        config.output_dir = os.path.dirname(config.input_file)

    # potentially replace with rich consoles if we're in an interactive terminal
    # (only after arg parsing so we don't pay for the import if we're not using it)
    config.setup_consoles()

    return config


def main(args: list[str]) -> int:
    global stdout
    global stderr
    config = parse_args(args)

    stdout, stderr = config.stdout, config.stderr

    if config.debug:
        logger.enable(console=stderr, level=LogLevel.DEBUG)

    solvers = find_available_solvers()
    if not solvers:
        stderr.print("No solvers found on PATH", style="red")
        return 1

    # build the commands to run the solvers
    file = config.input_file
    output = config.output_dir

    assert file is not None
    assert output is not None

    commands: list[Command] = []
    for solver in solvers:
        command = Command(
            name=solver,
            args=SOLVERS[solver],
            input_file=file,
        )

        stdout_file = os.path.join(output, f"{os.path.basename(file)}.{solver}.out")

        # TODO: handle output file creation failure
        if not stdout_file:
            raise RuntimeError(f"failed to create output file for {command}")

        command.stdout = open(stdout_file, "w")  # noqa: SIM115
        commands.append(command)

    # initialize the controller
    task = Task(name=str(file))
    controller = ProcessController(task, commands, config, get_exit_callback())

    setup_signal_handlers(controller)

    stderr.print(f"Starting {len(commands)} solvers")
    stderr.print(f"Output will be written to: {output}{os.sep}")
    status = get_status()
    try:
        # all systems go
        controller.start()
        status.start()

        if config.supervisor:
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
    sys.exit(main(sys.argv[1:]))
