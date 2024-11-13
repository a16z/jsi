import json
import os
from collections.abc import Sequence
from importlib.abc import Traversable
from importlib.resources import files

from jsi.utils import Printable, get_consoles, logger, simple_stderr, simple_stdout


class Config:
    jsi_home: str
    definitions_file: str
    path_cache: str
    definitions_default_path: Traversable

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
        reaper: bool = False,
        sequence: Sequence[str] | None = None,
        model: bool = False,
        csv: bool = False,
        daemon: bool = False,
        verbose: bool = False,
    ):
        self.early_exit = early_exit
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.debug = debug
        self.input_file = input_file
        self.output_dir = output_dir
        self.reaper = reaper
        self.sequence = sequence
        self.model = model
        self.csv = csv
        self.daemon = daemon
        self.verbose = verbose
        self.stdout = simple_stdout
        self.stderr = simple_stderr
        self.solver_versions = False

        # global defaults
        self.jsi_home = os.path.expanduser("~/.jsi")
        self.path_cache = os.path.join(self.jsi_home, "cache.json")
        self.definitions_file = os.path.join(self.jsi_home, "solvers.json")
        self.definitions_default_path = files("jsi.config").joinpath("solvers.json")

    def setup_consoles(self):
        self.stdout, self.stderr = get_consoles()


class SolverDefinition:
    executable: str
    model: str | None
    args: list[str]

    def __init__(
        self,
        executable: str,
        model: str | None,
        args: list[str],
        enabled: bool = True,
    ):
        self.executable = executable
        self.model = model
        self.args = args
        self.enabled = enabled

    @classmethod
    def from_dict(cls, data: dict[str, str | None | list[str]]) -> "SolverDefinition":
        return cls(
            executable=data["executable"],  # type: ignore
            model=data["model"],  # type: ignore
            args=data["args"],  # type: ignore
            enabled=data.get("enabled", True),  # type: ignore
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


def load_definitions(config: Config) -> dict[str, SolverDefinition]:
    _, stderr = get_consoles()

    custom_path = config.definitions_file
    if os.path.exists(custom_path):
        logger.debug(f"Loading definitions from {custom_path}")
        with open(custom_path) as f:
            return parse_definitions(json.load(f))

    default_path = config.definitions_default_path

    if config.verbose:
        stderr.print(f"no custom definitions file found ('{custom_path}')")
        stderr.print(f"loading defaults ('{default_path}')")

    data = default_path.read_text()
    return parse_definitions(json.loads(data))


def load_solvers(
    solver_definitions: dict[str, SolverDefinition],
    config: Config,
) -> dict[str, str]:
    path_cache = config.path_cache
    if not os.path.exists(path_cache):
        return {}

    if config.verbose:
        stderr = config.stderr
        stderr.print(f"loading solver paths from cache ('{path_cache}')")

    import json

    with open(path_cache) as f:
        try:
            paths = json.load(f)
            return paths
        except json.JSONDecodeError as err:
            logger.error(f"error loading solver cache: {err}")
            return {}


def find_solvers(
    solver_definitions: dict[str, SolverDefinition],
    config: Config,
) -> dict[str, str]:
    stderr = config.stderr
    stderr.print("looking for solvers available on PATH:")

    paths: dict[str, str] = {}

    import shutil

    executables = set(d.executable for d in solver_definitions.values())
    for executable in executables:
        path = shutil.which(executable)  # type: ignore

        if path is None:
            stderr.print(f"[yellow]{'N/A':>6}[/yellow] {executable}")
            continue

        paths[executable] = path
        stderr.print(f"[green]{'OK':>6}[/green] {executable}")

    stderr.print()
    return paths


def save_solvers(
    paths: dict[str, str],
    config: Config,
):
    if not paths:
        return

    import json

    if not os.path.exists(config.jsi_home):
        os.makedirs(config.jsi_home)

    with open(config.path_cache, "w") as f:
        json.dump(paths, f)
