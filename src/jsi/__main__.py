"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import sys
from contextlib import contextmanager
from time import perf_counter

import click
from loguru import logger

from jsi.core import TaskResult, solve


@contextmanager
def timer(description: str):
    start = perf_counter()
    yield
    elapsed = perf_counter() - start
    logger.trace(f"{description}: {elapsed:.3f}s")


@click.command()
@click.version_option()
@click.argument("file", type=click.Path(exists=True), required=True)
def main(file: click.Path) -> int:
    with timer("solve"):
        result = solve(file)
        click.echo(result.value)
        return 0 if result in (TaskResult.SAT, TaskResult.UNSAT) else 1


sys.exit(main())
