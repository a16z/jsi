import contextlib
import multiprocessing
import os
import signal
import time

import psutil
from loguru import logger


@contextlib.contextmanager
def timer(description: str):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    logger.trace(f"{description}: {elapsed:.3f}s")


def kill_process(pid: int):
    try:
        os.kill(pid, signal.SIGTERM)
        logger.debug(f"sent SIGTERM to process {pid}")
    except ProcessLookupError:
        logger.debug(f"skipping SIGTERM for process {pid} -- not found")


class Supervisor(multiprocessing.Process):
    """Supervisor process that monitors the parent process and its children."""

    def __init__(self, parent_pid: int, child_pids: list[int]):
        super().__init__()
        self.parent_pid = parent_pid
        self.child_pids = child_pids

    def run(self):
        logger.debug(f"supervisor started (PID: {self.pid})")
        logger.debug(f"watching parent (PID: {self.parent_pid})")

        last_message_time = time.time()
        try:
            while True:
                current_time = time.time()
                if current_time - last_message_time >= 60:
                    logger.debug(f"supervisor still running (PID: {self.pid})")
                    last_message_time = current_time

                if psutil.pid_exists(self.parent_pid):
                    time.sleep(1)
                    continue

                logger.debug(f"parent (PID {self.parent_pid} has died)")
                for pid in self.child_pids:
                    kill_process(pid)

                logger.debug("all children terminated, supervisor exiting.")
                break
        except KeyboardInterrupt:
            logger.debug("supervisor interrupted")

        logger.debug(f"supervisor exiting (PID: {self.pid})")
