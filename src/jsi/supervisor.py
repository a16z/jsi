import multiprocessing
import time

from jsi.utils import LogLevel, kill_process, logger, pid_exists, stderr


class Supervisor(multiprocessing.Process):
    """Supervisor process that monitors the parent process and its children."""

    def __init__(self, parent_pid: int, child_pids: list[int], debug: bool = False):
        super().__init__()
        self.parent_pid = parent_pid
        self.child_pids = child_pids
        self.debug = debug

    def run(self):
        if self.debug:
            logger.enable(console=stderr, level=LogLevel.DEBUG)

        logger.debug(f"supervisor started (PID: {self.pid})")
        logger.debug(f"watching parent (PID: {self.parent_pid})")
        logger.debug(f"watching children (PID: {self.child_pids})")

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