import enum

sat, unsat, error, unknown, timeout, killed = (
    "sat",
    "unsat",
    "error",
    "unknown",
    "timeout",
    "killed",
)


class TaskResult(enum.Enum):
    SAT = sat
    UNSAT = unsat
    ERROR = error
    UNKNOWN = unknown
    TIMEOUT = timeout
    KILLED = killed


class TaskStatus(enum.Enum):
    RUNNING = 1
    TERMINATING = 2
    TERMINATED = 3


def solve(smt: str) -> TaskResult:
    return TaskResult.UNKNOWN
