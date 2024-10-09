import asyncio
import contextlib
import os
import signal
import socket
import threading

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
from jsi.utils import pid_exists

SERVER_HOME = os.path.expanduser("~/.jsi/daemon")
SOCKET_PATH = os.path.join(SERVER_HOME, "server.sock")
STDOUT_PATH = os.path.join(SERVER_HOME, "server.out")
STDERR_PATH = os.path.join(SERVER_HOME, "server.err")
PID_PATH = os.path.join(SERVER_HOME, "server.pid")
CONN_BUFFER_SIZE = 1024

# TODO: handle signal.SIGCHLD (received when a child process exits)
# TODO: check if there is an existing daemon


class ResultListener:
    def __init__(self):
        self.event = threading.Event()
        self._result: str | None = None

    def exit_callback(self, command: Command, task: Task):
        if self.event.is_set():
            return

        if command.done() and command.ok() and (stdout_text := command.stdout_text):
            self.event.set()
            self._result = f"{stdout_text.strip()}\n; (result from {command.name})"
            name, result = command.name, command.result()
            print(f"{name} returned {result} in {command.elapsed():.03f}s")

    @property
    def result(self) -> str:
        self.event.wait()

        assert self._result is not None
        return self._result


class PIDFile:
    def __init__(self, path: str):
        self.path = path
        self.pid = os.getpid()

    def __enter__(self):
        try:
            with open(self.path) as fd:
                print(f"pid file already exists: {self.path}")
                other_pid = fd.read()

            if pid_exists(int(other_pid)):
                print(f"killing existing daemon ({other_pid=})")
                os.kill(int(other_pid), signal.SIGKILL)
        except FileNotFoundError:
            # pid file doesn't exist, we're good to go
            pass

        # overwrite the file if it already exists
        with open(self.path, "w") as fd:
            fd.write(str(self.pid))

        print(f"created pid file: {self.path} ({self.pid=})")
        return self.path

    def __exit__(self, exc_type, exc_value, traceback):
        print(f"removing pid file: {self.path}")

        # ignore if the file was already removed
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.path)


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
            self.handle_client, path=SOCKET_PATH
        )

        async with server:
            await server.serve_forever()

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        try:
            data: bytes = await reader.read(1024)
            if data:
                message: str = data.decode()
                result = await self.solve(message)
                writer.write(result.encode())
                await writer.drain()
        except Exception as e:
            print(f"Error handling client: {e}")
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

        commands = base_commands(
            self.config.sequence or list(self.available_solvers.keys()),
            self.solver_definitions,
            self.available_solvers,
            self.config,
        )
        set_input_output(commands, self.config)

        listener = ResultListener()
        controller = ProcessController(
            task, commands, self.config, exit_callback=listener.exit_callback
        )
        controller.start()

        return listener.result

    # def start(self, detach_process: bool | None = None):
    #     if not os.path.exists(SERVER_HOME):
    #         print(f"creating server home: {SERVER_HOME}")
    #         os.makedirs(SERVER_HOME)

    #     stdout_file = open(STDOUT_PATH, "w+")  # noqa: SIM115
    #     stderr_file = open(STDERR_PATH, "w+")  # noqa: SIM115

    #     print(f"daemonizing... (`tail -f {STDOUT_PATH[:-4]}.{{err,out}}` to view logs)")
    #     with daemon.DaemonContext(
    #         stdout=stdout_file,
    #         stderr=stderr_file,
    #         detach_process=detach_process,
    #         pidfile=PIDFile(PID_PATH),
    #     ):
    #         if os.path.exists(SOCKET_PATH):
    #             print(f"removing existing socket: {SOCKET_PATH}")
    #             os.remove(SOCKET_PATH)

    #         print(f"binding socket: {SOCKET_PATH}")
    #         with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
    #             server.bind(SOCKET_PATH)
    #             server.listen(1)

    #             while True:
    #                 try:
    #                     conn, _ = server.accept()
    #                     with conn:
    #                         try:
    #                             data = conn.recv(CONN_BUFFER_SIZE).decode()
    #                             if not data:
    #                                 continue
    #                             print(f"solving: {data}")
    #                             conn.sendall(self.solve(data).encode())
    #                         except ConnectionError as e:
    #                             print(f"connection error: {e}")
    #                 except SystemExit as e:
    #                     print(f"system exit: {e}")
    #                     return e.code


if __name__ == "__main__":

    async def run_server():
        server = Server(Config())
        await server.start()

    stdout_file = open(STDOUT_PATH, "w+")  # noqa: SIM115
    stderr_file = open(STDERR_PATH, "w+")  # noqa: SIM115

    with daemon.DaemonContext(stdout=stdout_file, stderr=stderr_file):
        asyncio.run(run_server())
