import multiprocessing
import sys
import time

from jsi.utils import LogLevel, get_console, kill_process, logger, pid_exists


class Reaper(multiprocessing.Process):
    """Reaper process that monitors the parent process and its children."""

    parent_pid: int
    child_pids: list[int]
    debug: bool

    def __init__(self, parent_pid: int, child_pids: list[int], debug: bool = False):
        super().__init__()
        self.parent_pid = parent_pid
        self.child_pids = child_pids
        self.debug = debug

    def run(self):
        level = LogLevel.DEBUG if self.debug else LogLevel.INFO
        logger.enable(console=get_console(sys.stderr), level=level)

        logger.info(f"reaper started (PID: {self.pid})")
        logger.info(f"watching parent (PID: {self.parent_pid})")
        logger.info(f"watching children (PID: {self.child_pids})")

        last_message_time = time.time()
        try:
            while True:
                current_time = time.time()
                if current_time - last_message_time >= 60:
                    logger.debug(f"supervisor still running (PID: {self.pid})")
                    last_message_time = current_time

                if pid_exists(self.parent_pid):
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
