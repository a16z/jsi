import humanize
from rich.status import Status
from rich.table import Table
from rich.text import Text

from jsi.core import Command, ProcessController, Task, TaskResult, TaskStatus
from jsi.utils import file_loc, stderr


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


_status_message = "waiting for solvers (press ^C to stop)"
status = Status(_status_message, spinner="noise", console=stderr)