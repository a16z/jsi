"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import contextlib
import pathlib
import sys
import time

import click
from loguru import logger

from jsi.core import TaskResult, solve


@contextlib.contextmanager
def timer(description: str):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    logger.trace(f"{description}: {elapsed:.3f}s")


@click.command()
@click.version_option()
@click.argument("file", type=click.Path(exists=True, dir_okay=False), required=True)
def main(file: pathlib.Path) -> int:
    with timer("solve"):
        result, output = solve(file)
        click.echo(result.value)
        click.echo(output)
        return 0 if result in (TaskResult.SAT, TaskResult.UNSAT) else 1


if __name__ == "__main__":
    sys.exit(main())
