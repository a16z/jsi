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
import contextlib

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

    def __enter__(self):
        if os.path.exists(self.path):
            print(f"pid file already exists: {self.path}")

            with open(self.path) as fd:
                other_pid = fd.read()

            if pid_exists(int(other_pid)):
                print(f"killing existing daemon ({other_pid=})")
                os.kill(int(other_pid), signal.SIGKILL)

            # the file may have been removed on termination by another instance
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.path)

        pid = os.getpid()
        print(f"creating pid file: {self.path} ({pid=})")
        with open(self.path, "w") as fd:
            fd.write(str(pid))

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

    def solve(self, file: str) -> str:
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


    def start(self, detach_process: bool | None = None):
        if not os.path.exists(SERVER_HOME):
            print(f"creating server home: {SERVER_HOME}")
            os.makedirs(SERVER_HOME)

        stdout_file = open(STDOUT_PATH, "w+")  # noqa: SIM115
        stderr_file = open(STDERR_PATH, "w+")  # noqa: SIM115

        print(f"daemonizing... (`tail -f {STDOUT_PATH[:-4]}.{{err,out}}` to view logs)")
        with daemon.DaemonContext(
            stdout=stdout_file,
            stderr=stderr_file,
            detach_process=detach_process,
            pidfile=PIDFile(PID_PATH),
        ):
            if os.path.exists(SOCKET_PATH):
                print(f"removing existing socket: {SOCKET_PATH}")
                os.remove(SOCKET_PATH)

            print(f"binding socket: {SOCKET_PATH}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(SOCKET_PATH)
                server.listen(1)

                while True:
                    try:
                        conn, _ = server.accept()
                        with conn:
                            try:
                                data = conn.recv(CONN_BUFFER_SIZE).decode()
                                if not data:
                                    continue
                                conn.sendall(self.solve(data).encode())
                            except ConnectionError as e:
                                print(f"connection error: {e}")
                    except SystemExit as e:
                        print(f"system exit: {e}")
                        return e.code


if __name__ == "__main__":
    Server(Config()).start()
