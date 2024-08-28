import click
import pytest

from jsi.__main__ import main


def test_basic():
    assert True


def test_main_file_does_not_exist():
    with pytest.raises(click.exceptions.BadParameter):
        main(["does-not-exist.smt2"], standalone_mode=False)
