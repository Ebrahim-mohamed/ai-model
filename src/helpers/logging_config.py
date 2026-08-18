import logging
from pathlib import Path


def configure_logging(log_file_path: str, level_name: str) -> None:
    """One-time, process-wide logging setup for step-by-step RAG pipeline
    tracing. Attaches a single FileHandler to the ROOT logger — every
    existing `self.logger = logging.getLogger(__name__)` already used
    throughout controllers/stores automatically inherits it via Python's
    standard logger hierarchy, so no per-file handler wiring is needed
    anywhere else, and no existing logger-acquisition code changes.

    Production-safe by construction, not by extra guarding here:
    `logging.Handler` already isolates a handler-level failure (e.g. a
    disk-full write error) from the calling code — it prints to stderr
    via `Handler.handleError` and returns, it does not raise into
    application logic. Wrapping every `self.logger.debug(...)` call in
    this codebase in its own try/except would duplicate a guarantee the
    standard library already provides.

    Idempotent: safe to call more than once (e.g. under uvicorn --reload
    re-executing this module) without stacking duplicate FileHandlers,
    which would otherwise write every log line N times.
    """
    resolved_path = str(Path(log_file_path).resolve())
    root_logger = logging.getLogger()

    already_configured = any(
        isinstance(handler, logging.FileHandler) and handler.baseFilename == resolved_path
        for handler in root_logger.handlers
    )
    if already_configured:
        return

    level = getattr(logging, level_name.upper(), logging.INFO)
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(resolved_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
