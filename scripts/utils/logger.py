"""Structured logging for the india-d2c pipeline."""
import logging
import sys
from pathlib import Path

# Log file lives at <repo-root>/logs/run.log (NOT scripts/logs/) per convention.
# logs/ is gitignored.
LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "run.log"


def get_logger(name: str = "india-d2c") -> logging.Logger:
    """Return a configured logger that writes to both stderr and logs/run.log."""
    LOG_PATH.parent.mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Console (stderr)
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    # File
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger
