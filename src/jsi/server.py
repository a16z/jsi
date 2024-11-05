"""
A daemon that listens for requests on a unix socket.

Can be started with:

    # with a command line interface to parse the config:
    jsi [options] --daemon

    # or with a default config:
    python -m jsi.server

These commands return immediately, as a detached daemon runs in the background.

The daemon:
- checks if there is an existing daemon (as indicated by ~/.jsi/daemon/server.pid)
- it kills the existing daemon if found
- it writes its own pid to ~/.jsi/daemon/server.pid
- it outputs logs to ~/.jsi/daemon/server.{err,out}
- it listens for requests on a unix domain socket (by default ~/.jsi/daemon/server.sock)
- each request is a single line of text, the path to a file to solve
- for each request, it runs the sequence of solvers defined in the config
- it returns the output of the solvers, based on the config
- it runs until terminated by the user or another daemon
"""

import asyncio
import contextlib
import os
import signal
import sys
import threading
from pathlib import Path

import daemon  # type: ignore

from jsi.config.loader import (
    Config,
    SolverDefinition,
    find_available_solvers,
    load_definitions,
)
from jsi.core import (
    Command,
    ProcessController,
    Task,
    base_commands,
    set_input_output,
)
from jsi.utils import get_console, logger, pid_exists, unexpand_home

SERVER_HOME = Path.home() / ".jsi" / "daemon"
SOCKET_PATH = SERVER_HOME / "server.sock"
STDOUT_PATH = SERVER_HOME / "server.out"
STDERR_PATH = SERVER_HOME / "server.err"
PID_PATH = SERVER_HOME / "server.pid"
CONN_BUFFER_SIZE = 1024


unexpanded_pid = unexpand_home(PID_PATH)
server_usage = f"""[bold white]starting daemon...[/]

- tail logs:
    [green]tail -f {unexpand_home(STDERR_PATH)[:-4]}.{{err,out}}[/]

- view pid of running daemon:
    [green]cat {unexpanded_pid}[/]

- display useful info about current daemon:
    [green]ps -o pid,etime,command -p $(cat {unexpanded_pid})[/]

- terminate daemon (gently, with SIGTERM):
    [green]kill $(cat {unexpanded_pid})[/]

- terminate daemon (forcefully, with SIGKILL):
    [green]kill -9 $(cat {unexpanded_pid})[/]

(use the commands above to monitor the daemon, this process will exit immediately)"""


class ResultListener:
    def __init__(self):
        self.event = threading.Event()
        self._result: str | None = None

    def exit_callback(self, command: Command, task: Task):
        name, result, elapsed = command.name, command.result(), command.elapsed()
        logger.info(f"{name} returned {result} in {elapsed:.03f}s")

        if self.event.is_set():
            return

        if command.done() and command.ok() and (stdout_text := command.stdout_text):
            self.event.set()
            self._result = f"{stdout_text.strip()}\n; (result from {command.name})"

    @property
    def result(self) -> str:
        self.event.wait()

        assert self._result is not None
        return self._result


class PIDFile:
    def __init__(self, path: Path):
        self.path = path
        # don't get the pid here, as it may not be the current pid anymore
        # by the time we enter the context manager

    def __enter__(self):
        path_str = unexpand_home(self.path)
        try:
            with open(self.path) as fd:
                logger.info(f"pid file already exists: {path_str}")
                other_pid = fd.read()

            other_pid_int = int(other_pid) if other_pid.isdigit() else None
            if other_pid_int and pid_exists(other_pid_int):
                logger.info(f"killing existing daemon (other_pid={other_pid_int})")
                os.kill(other_pid_int, signal.SIGKILL)

            else:
                logger.info(f"pid file points to dead daemon ({other_pid=})")
        except FileNotFoundError:
            # pid file doesn't exist, we're good to go
            pass

        # overwrite the file if it already exists
        pid = os.getpid()
        with open(self.path, "w") as fd:
            fd.write(str(pid))

        logger.info(f"created pid file: {path_str} ({pid=})")
        return self.path

    def __exit__(self, exc_type, exc_value, traceback):  # type: ignore
        logger.info(f"removing pid file: {self.path}")

        # ignore if the file was already removed
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.path)


def start_logger(command: Command, task: Task):
    logger.info(f"command started: {command.parts()}")


class Server:
    solver_definitions: dict[str, SolverDefinition]
    available_solvers: dict[str, str]
    config: Config

    def __init__(self, config: Config):
        self.config = config
        self.solver_definitions = load_definitions(config)
        self.available_solvers = find_available_solvers(self.solver_definitions, config)

    async def start(self):
        server = await asyncio.start_unix_server(
            self.handle_client, path=str(SOCKET_PATH)
        )

        async with server:
            logger.info(f"server listening on {unexpand_home(SOCKET_PATH)}")
            await server.serve_forever()

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        try:
            data: bytes = await reader.read(1024)
            if data:
                message: str = data.decode()
                print(f"received request: {message}")
                result = await self.solve(message)
                writer.write(result.encode())
                await writer.drain()
        except Exception as e:
            logger.info(f"Error handling client: {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    async def solve(self, file: str) -> str:
        # Assuming solve is CPU-bound, we use run_in_executor
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self.sync_solve, file)
        return result

    def sync_solve(self, file: str) -> str:
        # initialize the controller
        task = Task(name=str(file))

        # FIXME: don't mutate the config
        self.config.input_file = file
        self.config.output_dir = os.path.dirname(file)

        defs = self.solver_definitions
        enabled_solvers = [solver for solver in defs if defs[solver].enabled]

        commands = base_commands(
            self.config.sequence or enabled_solvers,
            self.solver_definitions,
            self.available_solvers,
            self.config,
        )
        set_input_output(commands, self.config)

        listener = ResultListener()
        controller = ProcessController(
            task,
            commands,
            self.config,
            start_callback=start_logger,
            exit_callback=listener.exit_callback,
        )
        controller.start()

        result = listener.result
        controller.kill()

        return result


def daemonize(config: Config):
    stdout = get_console(sys.stdout)
    stdout.print(server_usage)

    async def run_server():
        server = Server(config)
        await server.start()

    stdout_file = open(STDOUT_PATH, "w+")  # noqa: SIM115
    stderr_file = open(STDERR_PATH, "w+")  # noqa: SIM115

    with daemon.DaemonContext(
        stdout=stdout_file,
        stderr=stderr_file,
        pidfile=PIDFile(PID_PATH),
    ):
        asyncio.run(run_server())


if __name__ == "__main__":
    daemonize(Config())
