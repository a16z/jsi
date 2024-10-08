import os
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

SERVER_HOME = os.path.expanduser("~/.jsi/daemon")
SOCKET_PATH = os.path.join(SERVER_HOME, "server.sock")
STDOUT_PATH = os.path.join(SERVER_HOME, "server.out")
STDERR_PATH = os.path.join(SERVER_HOME, "server.err")

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

        # controller.join()
        # return get_results_csv(controller)

    def start(self):
        if os.path.exists(SOCKET_PATH):
            print(f"removing existing socket: {SOCKET_PATH}")
            os.remove(SOCKET_PATH)

        print(f"binding socket: {SOCKET_PATH}")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(SOCKET_PATH)

            print(f"listening on {SOCKET_PATH}")
            server.listen(1)

            while True:
                conn, _ = server.accept()
                # print(f"accepted connection from {conn}")

                with conn:
                    try:
                        data = conn.recv(CONN_BUFFER_SIZE).decode()
                        if not data:
                            continue
                        conn.sendall(self.solve(data).encode())
                    except ConnectionError as e:
                        print(f"connection error: {e}")



if __name__ == "__main__":
    if not os.path.exists(SERVER_HOME):
        print(f"creating server home: {SERVER_HOME}")
        os.makedirs(SERVER_HOME)

    stdout_file = open(STDOUT_PATH, "w+")  # noqa: SIM115
    stderr_file = open(STDERR_PATH, "w+")  # noqa: SIM115

    print("daemonizing...")
    with daemon.DaemonContext(stdout=stdout_file, stderr=stderr_file):
        Server(Config()).start()
