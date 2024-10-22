import io
import subprocess
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from typing import Any

import pytest

from jsi.cli import BadParameterError, parse_args
from jsi.config.loader import Config, load_definitions


def capture_stdout(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> tuple[Any, str]:
    f = io.StringIO()
    with redirect_stdout(f):
        result: Any = func(*args, **kwargs)
    output = f.getvalue()
    return result, output


def test_cli_file_does_not_exist():
    with pytest.raises(BadParameterError) as excinfo:
        parse_args(["does-not-exist.smt2"])
    assert "does not exist" in str(excinfo.value)


def test_cli_file_exists_but_is_directory():
    with pytest.raises(BadParameterError) as excinfo:
        parse_args(["src/"])
    assert "not a file" in str(excinfo.value)


def test_cli_file_is_not_stdin():
    with pytest.raises(BadParameterError) as excinfo:
        parse_args(["-"])
    assert "does not exist" in str(excinfo.value)


def test_cli_version():
    result = subprocess.run(
        [sys.executable, "-m", "jsi", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "jsi v" in result.stdout


def test_load_definitions():
    definitions = load_definitions(Config())
    assert "z3" in definitions
    assert "bitwuzla" in definitions
    assert "cvc4" in definitions
    assert "stp" in definitions
    assert "yices" in definitions
    assert "boolector" in definitions
    assert "cvc5" in definitions
