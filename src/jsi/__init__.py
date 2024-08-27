"""just solve it - a command line tool to SMT solvers in parallel."""

import click

from loguru import logger

@click.command()
@click.version_option()
def main() -> int:
    logger.info("Hello, world!")
    return 0
