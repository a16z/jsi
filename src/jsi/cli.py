"""Run multiple SMT solvers in parallel and compare their results.

When running on an input file (typically a .smt2 file), jsi:
- runs all available solvers at the same time
- waits for the first solver to finish
- if a solver finds a solution (sat) or proves no solution exists (unsat), jsi:
   - stops all other solvers
   - prints the result from the successful solver on stdout

jsi can be interrupted (with Ctrl+C) and it will kill all running solvers. It also
supports a `--timeout` option to limit the runtime of each solver.

To find available solvers:
- jsi loads the solver definitions from a config file (~/.jsi/definitions.json)
- for each solver defined in the config, jsi looks for its executable on your PATH
- found solvers are cached in ~/.jsi/solvers.json

Note: solvers are not included with jsi and must be built/installed separately.

Usage: jsi [OPTIONS] FILE

Common options:
  --timeout FLOAT     timeout in seconds (can also use suffix "ms", "s", "m")
  --interval FLOAT    interval in seconds between starting solvers (default: 0s)
  --full-run          run all solvers to completion (don't stop on first result)
  --sequence CSV      run only specified solvers, in the given order (e.g. a,c,b)
  --model             generate a model for satisfiable instances

Less common options:
  --output DIRECTORY  directory where solver output files will be written
  --supervisor        run a supervisor process to avoid orphaned subprocesses
  --debug             enable debug logging
  --csv               print solver results in CSV format (<output-dir>/<input-file>.csv)
  --perf              print performance timers

Miscellaneous:
  --version           show the version and exit
  --help              show this message and exit

Examples:
- Run all available solvers to completion on a file with a 2.5s timeout:
    jsi --timeout 2.5s --full-run file.smt2

- Run specific solvers in sequence on a file, with some interval between solver starts:
    jsi --sequence yices,bitwuzla,z3 --interval 100ms file.smt2

- Redirect stderr to a file to disable rich output (only prints winning solver output):
    jsi --csv file.smt2 2> jsi.err
"""

import atexit
import os
import signal
import sys
import threading
from functools import partial

from jsi.config.loader import Config, SolverDefinition, load_definitions
from jsi.core import (
    Command,
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
    timer,
)

stdout, stderr = simple_stdout, simple_stderr
jsi_home = os.path.expanduser("~/.jsi")
solver_paths = os.path.join(jsi_home, "solvers.json")


def get_exit_callback():
    if is_terminal(sys.stderr):
        from jsi.output.fancy import on_process_exit, status

        return partial(on_process_exit, status=status)
    else:
        from jsi.output.basic import on_process_exit

        return on_process_exit


def get_status():
    if is_terminal(sys.stderr):
        from jsi.output.fancy import status

        return status
    else:
        from jsi.output.basic import NoopStatus

        return NoopStatus()


def find_available_solvers(
    solver_definitions: dict[str, SolverDefinition],
) -> dict[str, str]:
    if os.path.exists(solver_paths):
        stderr.print(f"loading solver paths from cache ({solver_paths})")
        import json

        with open(solver_paths) as f:
            try:
                paths = json.load(f)
            except json.JSONDecodeError as err:
                logger.error(f"error loading solver cache: {err}")
                paths = {}

        if paths:
            return paths

    stderr.print("looking for solvers available on PATH:")
    paths: dict[str, str] = {}

    import shutil

    for solver_name, solver_def in solver_definitions.items():
        path = shutil.which(solver_def.executable)  # type: ignore

        if path is None:
            stderr.print(f"{solver_name:>12} not found")
            continue

        paths[solver_name] = path
        stderr.print(f"{solver_name:>12} [green]OK[/green]")

    stderr.print()

    # save the paths to the solver_paths file
    if paths:
        import json

        if not os.path.exists(jsi_home):
            os.makedirs(jsi_home)

        with open(solver_paths, "w") as f:
            json.dump(paths, f)

    return paths


def setup_signal_handlers(controller: ProcessController):
    event = threading.Event()

    def signal_listener(signum: int, frame: object | None = None):
        event.set()
        thread_name = threading.current_thread().name
        logger.debug(f"signal {signum} received in thread: {thread_name}")

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


class BadParameterError(Exception):
    pass


def parse_time(arg: str) -> float:
    if arg.endswith("ms"):
        return float(arg[:-2]) / 1000
    elif arg.endswith("s"):
        return float(arg[:-1])
    elif arg.endswith("m"):
        return float(arg[:-1]) * 60
    else:
        return float(arg)


def parse_args(args: list[str]) -> Config:
    config = Config()

    i = 0
    while i < len(args):
        arg = args[i]
        i += 1

        match arg:
            case "--version":
                raise SystemExit("jsi v0.1.0")
            case "--help":
                raise SystemExit(__doc__)
            case "--perf":
                logger.enable(console=stderr, level=LogLevel.TRACE)
            case "--debug":
                config.debug = True
            case "--full-run":
                config.early_exit = False
            case "--output":
                config.output_dir = arg
            case "--model":
                config.model = True
            case "--csv":
                config.csv = True
            case "--supervisor":
                config.supervisor = True
            case "--timeout":
                config.timeout_seconds = parse_time(args[i])
                i += 1
            case "--interval":
                config.interval_seconds = parse_time(args[i])
                i += 1
            case "--sequence":
                config.sequence = args[i].split(",")
                i += 1
            case _:
                if arg.startswith("--"):
                    raise BadParameterError(f"unknown argument: {arg}")

                if config.input_file:
                    raise BadParameterError("multiple input files provided")

                config.input_file = arg

    if not config.input_file:
        raise BadParameterError("no input file provided")

    if not os.path.exists(config.input_file):
        raise BadParameterError(f"input file does not exist: {config.input_file}")

    if not os.path.isfile(config.input_file):
        raise BadParameterError(f"input file is not a file: {config.input_file}")

    if config.output_dir and not os.path.exists(config.output_dir):
        raise BadParameterError(f"output directory does not exist: {config.output_dir}")

    if config.output_dir and not os.path.isdir(config.output_dir):
        raise BadParameterError(f"output path is not a directory: {config.output_dir}")

    if config.timeout_seconds < 0:
        raise BadParameterError(f"invalid timeout value: {config.timeout_seconds}")

    if config.interval_seconds < 0:
        raise BadParameterError(f"invalid interval value: {config.interval_seconds}")

    # output directory defaults to the parent of the input file
    if config.output_dir is None:
        config.output_dir = os.path.dirname(config.input_file)

    return config


def main(args: list[str] | None = None) -> int:
    global stdout
    global stderr

    if args is None:
        args = sys.argv[1:]

    try:
        with timer("parse_args"):
            config = parse_args(args)
    except BadParameterError as err:
        stderr.print(f"error: {err}")
        return 1
    except IndexError:
        stderr.print(f"error: missing argument after {args[-1]}")
        return 1
    except ValueError as err:
        stderr.print(f"error: invalid argument: {err}")
        return 1
    except SystemExit as err:
        stdout.print(err)
        return 0

    # potentially replace with rich consoles if we're in an interactive terminal
    # (only after arg parsing so we don't pay for the import if we're not using it)
    with timer("setup_consoles"):
        config.setup_consoles()

    stdout, stderr = config.stdout, config.stderr
    logger.console = stderr

    if config.debug:
        logger.enable(console=stderr, level=LogLevel.DEBUG)

    with timer("load_config"):
        solver_definitions = load_definitions()

    if not solver_definitions:
        stderr.print("error: no solver definitions found", style="red")
        return 1

    with timer("find_available_solvers"):
        # maps solver name to executable path
        available_solvers: dict[str, str] = find_available_solvers(solver_definitions)

    if not available_solvers:
        stderr.print("error: no solvers found on PATH", style="red")
        return 1

    # build the commands to run the solvers
    file = config.input_file
    output = config.output_dir

    assert file is not None
    assert output is not None

    commands: list[Command] = []
    basename = os.path.basename(file)

    # run the solvers in the specified sequence, or fallback to the default order
    for solver_name in config.sequence or available_solvers:
        solver_def: SolverDefinition | None = solver_definitions.get(solver_name)
        if not solver_def:
            stderr.print(f"error: unknown solver: {solver_name}", style="red")
            return 1

        executable_path = available_solvers[solver_name]
        args = [executable_path]

        # append the model option if requested
        if config.model:
            if model_arg := solver_def.model:
                args.append(model_arg)
            else:
                stderr.print(f"warn: solver {solver_name} has no model option")

        # append solver-specific extra arguments
        args.extend(solver_def.args)

        command = Command(
            name=solver_name,
            args=args,
            input_file=file,
        )

        stdout_file = os.path.join(output, f"{basename}.{solver_name}.out")
        stderr_file = os.path.join(output, f"{basename}.{solver_name}.err")
        command.stdout = open(stdout_file, "w")  # noqa: SIM115
        command.stderr = open(stderr_file, "w")  # noqa: SIM115
        commands.append(command)

    # initialize the controller
    task = Task(name=str(file))
    controller = ProcessController(task, commands, config, get_exit_callback())

    setup_signal_handlers(controller)

    stderr.print(f"starting {len(commands)} solvers")
    stderr.print(f"output will be written to: {output}{os.sep}")
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
            sv = Supervisor(os.getpid(), child_pids, config)
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
                    print(f"; (result from {command.name})")
                break

        if is_terminal(sys.stderr):
            # don't pay for the cost of importing rich (~40ms) if we're not using it
            from jsi.output.fancy import get_results_table

            table = get_results_table(controller)
            stderr.print()
            stderr.print(table)

        if config.csv:
            from jsi.output.basic import get_results_csv

            csv = get_results_csv(controller)
            csv_file = os.path.join(output, f"{basename}.csv")
            stderr.print(f"writing results to: {csv_file}")
            with open(csv_file, "w") as f:
                f.write(csv)
