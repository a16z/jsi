"""main module for jsi.

Usage:
    python -m jsi [options] <path/to/query.smt2>
"""

import sys
from contextlib import contextmanager
from time import perf_counter
from typing import TextIO

import click
from loguru import logger

from jsi.logic import solve


@contextmanager
def timer(description: str):
    start = perf_counter()
    yield
    elapsed = perf_counter() - start
    logger.trace(f"{description}: {elapsed:.3f} seconds")


@click.command()
@click.version_option()
@click.argument("file", type=click.File(), required=True)
def main(file: TextIO) -> int:
    with timer("read file"):
        smt = file.read()

    result = solve(smt)
    click.echo(result)
    return 0


sys.exit(main())
