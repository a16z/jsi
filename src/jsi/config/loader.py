import json
import os
from collections.abc import Sequence
from importlib.resources import files

from jsi.utils import Printable, get_consoles, logger, simple_stderr, simple_stdout


class Config:
    stdout: Printable
    stderr: Printable

    def __init__(
        self,
        early_exit: bool = True,
        timeout_seconds: float = 0,
        interval_seconds: float = 0,
        debug: bool = False,
        input_file: str | None = None,
        output_dir: str | None = None,
        supervisor: bool = False,
        sequence: Sequence[str] | None = None,
        model: bool = False,
        csv: bool = False,
    ):
        self.early_exit = early_exit
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.debug = debug
        self.input_file = input_file
        self.output_dir = output_dir
        self.supervisor = supervisor
        self.sequence = sequence
        self.model = model
        self.csv = csv
        self.stdout = simple_stdout
        self.stderr = simple_stderr

    def setup_consoles(self):
        self.stdout, self.stderr = get_consoles()


class SolverDefinition:
    executable: str
    model: str | None
    args: list[str]

    def __init__(self, executable: str, model: str | None, args: list[str]):
        self.executable = executable
        self.model = model
        self.args = args

    @classmethod
    def from_dict(cls, data: dict[str, str | None | list[str]]) -> "SolverDefinition":
        return cls(
            executable=data["executable"],  # type: ignore
            model=data["model"],  # type: ignore
            args=data["args"],  # type: ignore
        )


def parse_definitions(data: dict[str, object]) -> dict[str, SolverDefinition]:
    """Go from unstructured definitions data to a structured format.

    Input: a dict from some definitions file (e.g. json)
    Output: dict that maps solver names to SolverDefinition objects.
    """
    return {
        name: SolverDefinition.from_dict(definitions)  # type: ignore
        for name, definitions in data.items()  # type: ignore
    }


def load_definitions() -> dict[str, SolverDefinition]:
    _, stderr = get_consoles()

    custom_path = os.path.expanduser("~/.jsi/definitions.json")
    if os.path.exists(custom_path):
        logger.debug(f"Loading definitions from {custom_path}")
        with open(custom_path) as f:
            return parse_definitions(json.load(f))

    stderr.print(f"no custom definitions file found ({custom_path}), loading default")
    data = files("jsi.config").joinpath("definitions.json").read_text()
    return parse_definitions(json.loads(data))
