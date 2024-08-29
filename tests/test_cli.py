import io
from collections.abc import Callable
from contextlib import redirect_stdout
from typing import Any

import click
import pytest

from jsi.__main__ import main


def capture_stdout(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> tuple[Any, str]:
    f = io.StringIO()
    with redirect_stdout(f):
        result: Any = func(*args, **kwargs)
    output = f.getvalue()
    return result, output


def test_cli_file_does_not_exist():
    with pytest.raises(click.exceptions.BadParameter) as excinfo:
        main(["does-not-exist.smt2"], standalone_mode=False)
    assert "does not exist" in str(excinfo.value)


def test_cli_file_exists_but_is_directory():
    with pytest.raises(click.exceptions.BadParameter) as excinfo:
        main(["src/"], standalone_mode=False)
    assert "is a directory" in str(excinfo.value)


def test_cli_file_is_not_stdin():
    with pytest.raises(click.exceptions.BadParameter) as excinfo:
        main(["-"], standalone_mode=False)
    assert "does not exist" in str(excinfo.value)


def test_cli_version():
    (result, output) = capture_stdout(main, ["--version"], standalone_mode=False)
    assert result == 0
    assert "version 0.1.dev" in output
