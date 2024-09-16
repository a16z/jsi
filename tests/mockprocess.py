import sys
import time

import click


@click.command()
@click.option("--sleep-ms", type=int, default=0)
@click.option("--stdout", type=str, default="")
@click.option("--stderr", type=str, default="")
@click.option("--exit-code", type=int, default=0)
def main(sleep_ms: int, stdout: str, stderr: str, exit_code: int):
    sleep_s = sleep_ms / 1000
    if sleep_s > 0:
        time.sleep(sleep_s)

    if stdout:
        print(stdout)

    if stderr:
        print(stderr, file=sys.stderr)

    sys.exit(exit_code)


if __name__ == "__main__":
    sys.exit(main())
