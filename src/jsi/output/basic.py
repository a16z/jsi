import csv
import io

from jsi.core import Command, ProcessController, Task, TaskResult, TaskStatus
from jsi.utils import file_loc
from jsi.utils import simple_stderr as stderr


def get_results_csv(controller: ProcessController) -> str:
    output = io.StringIO()
    writer = csv.writer(output)

    # Write header
    writer.writerow(["solver", "result", "exit", "time", "output file", "size"])

    commands = controller.commands
    for command in sorted(commands, key=lambda x: (not x.maybe_ok(), x.elapsed() or 0)):
        command_output = command.stdout_loc or command.stdout

        writer.writerow(
            [
                command.name,
                command.result().value if command.result() else "-",
                str(command.returncode) if command.returncode is not None else "-",
                f"{command.elapsed():.2f}s" if command.elapsed() else "-",
                file_loc(command_output) or "-",
                len(command.stdout_text) if command.stdout_text else 0,
            ]
        )

    return output.getvalue()


class NoopStatus:
    def start(self):
        pass

    def update(self):
        pass

    def stop(self):
        pass


def on_proc_start(command: Command, task: Task):
    pass


def on_proc_exit(command: Command, task: Task):
    if task.status > TaskStatus.RUNNING:
        return

    # would be unexpected
    if not command.done():
        return

    if command.result() == TaskResult.TIMEOUT:
        return

    stderr.print(f"{command.name} returned {command.result().value}")
