"""Run multiple SMT solvers in parallel and compare their results.

When running on an input file (typically a .smt2 file), jsi:
- runs all available solvers at the same time
- waits for the first solver to finish
- if a solver finds a solution (sat) or proves no solution exists (unsat), jsi:
   - stops all other solvers
   - prints the result from the successful solver on stdout

jsi can be interrupted (with Ctrl+C) and it will kill all running solvers.
It also supports a `--timeout` option to limit the runtime of each solver.

To find available solvers:
- jsi loads the solver definitions from a config file (~/.jsi/definitions.json)
- for each defined solver, jsi looks for the executable on your PATH
- found solvers are cached in ~/.jsi/solvers.json

Note: solvers are not included with jsi and must be built/installed separately.

Usage: jsi [OPTIONS] FILE

Common options:
  --timeout FLOAT     timeout in seconds (can also use unit suffixes: "ms", "s")
  --interval FLOAT    interval in seconds between starting solvers (default: 0s)
  --full-run          run all solvers to completion (don't stop on first result)
  --sequence CSV      run only specified solvers, in the given order (e.g. a,c,b)
  --model             generate a model for satisfiable instances

Less common options:
  --output DIRECTORY  directory where solver output files will be written
  --reaper            run a reaper process that kills orphaned solvers when jsi exits
  --debug             enable debug logging
  --verbose           enable verbose output
  --csv               print solver results in CSV format (<output>/<input>.csv)
  --perf              print performance timers

Miscellaneous:
  --version           show the version and exit
  --versions          show the versions of all available and enabledsolvers and exit
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
import time
from functools import partial

from jsi.config.loader import (
    Config,
    find_solvers,
    load_definitions,
    load_solvers,
    save_solvers,
)
from jsi.core import (
    Command,
    ProcessController,
    Task,
    TaskResult,
    TaskStatus,
    base_commands,
    set_input_output,
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


def get_process_callbacks():
    """
    Return a tuple of two callbacks (process start and process exit)
    from the appropriate output module.
    """

    if is_terminal(sys.stderr):
        from jsi.output.fancy import on_proc_exit, on_proc_start, status

        return tuple(partial(f, status=status) for f in (on_proc_start, on_proc_exit))

    else:
        from jsi.output.basic import on_proc_exit, on_proc_start

        return (on_proc_start, on_proc_exit)


def get_status():
    if is_terminal(sys.stderr):
        from jsi.output.fancy import status

        return status
    else:
        from jsi.output.basic import NoopStatus

        return NoopStatus()


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


def is_in_container() -> bool:
    """Check if we're running inside a container."""

    if os.path.exists("/.dockerenv"):
        return True
    if os.path.exists("/run/.containerenv"):
        return True

    proc1_cgroup = "/proc/1/cgroup"
    if os.path.exists(proc1_cgroup):
        with open(proc1_cgroup) as f:
            return any(
                "docker" in line or "container" in line for line in f.readlines()
            )

    return False


def reaper_thread():
    """Monitor the parent process and exit if it dies or changes."""
    parent_pid = os.getppid()

    # Skip PID 1 check if we're running in a container
    # (when running interactively, the parent shell can have PID 1)
    skip_pid1 = is_in_container()
    if skip_pid1:
        logger.debug("containerized environment detected, skipping PID 1 check")

    def check_parent():
        while True:
            try:
                current_ppid = os.getppid()

                if current_ppid != parent_pid and (not skip_pid1 or current_ppid != 1):
                    stderr.print("parent process died, exiting...")
                    os.kill(os.getpid(), signal.SIGTERM)
                    break
                time.sleep(1)
            except ProcessLookupError:
                os.kill(os.getpid(), signal.SIGTERM)
                break

    monitor_thread = threading.Thread(target=check_parent, daemon=True)
    monitor_thread.start()


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


def get_version():
    from importlib.metadata import version

    return version("just-solve-it")


def parse_args(args: list[str]) -> Config:
    config = Config()
    args_iter = iter(args)

    for arg in args_iter:
        match arg:
            case "--version":
                raise SystemExit(f"jsi {get_version()}")
            case "--versions":
                config.solver_versions = True
            case "--help":
                raise SystemExit(__doc__)
            case "--perf":
                logger.enable(console=stderr, level=LogLevel.TRACE)
            case "--debug":
                config.debug = True
            case "--verbose":
                config.verbose = True
            case "--full-run":
                config.early_exit = False
            case "--model":
                config.model = True
            case "--csv":
                config.csv = True
            case "--reaper":
                config.reaper = True
            case "--daemon":
                config.daemon = True
            case "--output" | "--timeout" | "--interval" | "--sequence" as flag:
                value = next(args_iter, None)
                if value is None:
                    raise BadParameterError(f"missing value after {flag}")
                match flag:
                    case "--output":
                        config.output_dir = value
                    case "--timeout":
                        config.timeout_seconds = parse_time(value)
                    case "--interval":
                        config.interval_seconds = parse_time(value)
                    case "--sequence":
                        config.sequence = value.split(",")
            case _:
                if arg.startswith("--"):
                    raise BadParameterError(f"unknown argument: {arg}")

                if config.input_file:
                    raise BadParameterError("multiple input files provided")

                config.input_file = arg

    # some options don't require an input file
    if not config.daemon and not config.solver_versions:
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
    if config.output_dir is None and config.input_file:
        config.output_dir = os.path.dirname(config.input_file)

    return config


def extract_version(output: str) -> str:
    """Extract version number from solver output.

    Handles both standalone version numbers (e.g. "2.3.4-dev\n") and
    version numbers embedded in text (e.g. "Z3 version 4.12.2 - 64 bit\n").
    """

    output = output.strip()
    words = output.split()

    # try to find word containing only version-like characters
    for word in words:
        if all(c.isdigit() or c == "." for c in word):
            return word

    # try to find version after the word "version"
    try:
        version_idx = words.index("version") + 1
        return words[version_idx]
    except (ValueError, IndexError):
        # If we can't parse it, return the whole string
        return output


def main(args: list[str] | None = None) -> int:
    global stdout
    global stderr

    # kick off the parent monitor in the background as early as possible
    reaper_thread()

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

    if config.daemon:
        import jsi.server

        # this detaches the server from the current shell,
        # this returns immediately, leaving the server running in the background
        jsi.server.daemonize(config)
        return 0

    with timer("load_config"):
        solver_definitions = load_definitions(config)

    if not solver_definitions:
        stderr.print("error: no solver definitions found")
        return 1

    with timer("find_available_solvers"):
        # maps executable name to executable path
        available_solvers = load_solvers(solver_definitions, config)

        cache_version = available_solvers.get("__version__")
        stale_cache = cache_version != get_version()
        if stale_cache:
            stderr.print(f"warning: ignoring stale solver cache ({cache_version=})")

        if not available_solvers or stale_cache:
            available_solvers = find_solvers(solver_definitions, config)

            # stamp the current jsi version in the cache before saving
            available_solvers["__version__"] = get_version()
            save_solvers(available_solvers, config)

    if not available_solvers:
        stderr.print("error: no solvers found on PATH")
        stderr.print("see https://github.com/a16z/jsi?tab=readme-ov-file#tips for help")
        return 1

    # build the commands to run the solvers
    # run the solvers in the specified sequence, or fallback to the default order
    defs = solver_definitions
    enabled_solvers = [solver for solver in defs if defs[solver].enabled]

    if not enabled_solvers:
        n = len(available_solvers)
        stderr.print(f"error: found {n} solver(s) but none are enabled")
        return 1

    if config.solver_versions:
        import subprocess

        done: set[str] = set()
        for solver_name in enabled_solvers:
            executable_name = solver_definitions[solver_name].executable
            if executable_name not in available_solvers:
                continue

            executable_path = available_solvers[executable_name]
            if executable_name in done:
                continue

            done.add(executable_name)

            output = subprocess.run(
                [executable_path, "--version"],
                capture_output=True,
                text=True,
            ).stdout
            print(f"{executable_name}: {extract_version(output)}")

        return 0

    commands: list[Command] = base_commands(
        config.sequence or enabled_solvers,
        solver_definitions,
        available_solvers,
        config,
    )

    set_input_output(commands, config)

    # initialize the controller
    task = Task(name=str(config.input_file))
    start_callback, exit_callback = get_process_callbacks()
    controller = ProcessController(
        task,
        commands,
        config,
        start_callback=start_callback,
        exit_callback=exit_callback,
    )

    setup_signal_handlers(controller)

    if config.verbose:
        stderr.print(f"starting {len(commands)} solvers")
        stderr.print(f"output will be written to: {config.output_dir}{os.sep}")

    status = get_status()
    try:
        # all systems go
        controller.start()
        status.start()

        if config.reaper:
            from jsi.reaper import Reaper

            # wait for the subprocesses to start, we need the PIDs for the supervisor
            while controller.task.status.value < TaskStatus.RUNNING.value:
                pass

            # start a supervisor process in daemon mode so that it does not block
            # the program from exiting
            child_pids = [command.pid for command in controller.commands]
            reaper = Reaper(os.getpid(), child_pids, config.debug)
            reaper.daemon = True
            reaper.start()

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

            assert config.input_file is not None
            assert config.output_dir is not None

            basename = os.path.basename(config.input_file)
            csv_file = os.path.join(config.output_dir, f"{basename}.csv")
            stderr.print(f"writing results to: {csv_file}")
            with open(csv_file, "w") as f:
                f.write(csv)
