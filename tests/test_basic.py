import click
import pytest

from jsi.__main__ import main


def test_basic():
    assert True


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
