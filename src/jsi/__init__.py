"""just solve it - a command line tool to SMT solvers in parallel."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cli import main  # for type checkers only

__all__ = ["main"]


def __getattr__(name: str) -> Any:
    if name == "main":
        from .cli import main

        return main
    raise AttributeError(name)
