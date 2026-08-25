import logging
import logging.handlers
from unittest import mock
import pytest

import orizon_logging
from orizon_logging import setup_logging, get_logger

@pytest.fixture
def reset_logging_state():
    """Reset global state and clear handlers before and after each test."""
    def _reset():
        orizon_logging._CONFIGURED = False
        logger = logging.getLogger(orizon_logging._ROOT_NAME)
        # Remove all handlers safely
        for handler in list(logger.handlers):
            # Do not touch LogCaptureHandler added by pytest
            if type(handler).__name__ == "LogCaptureHandler":
                continue
            logger.removeHandler(handler)
            handler.close()

    _reset()
    yield
    _reset()

@pytest.fixture
def mock_log_dir(tmp_path):
    """Mock _log_dir to use a temporary directory."""
    with mock.patch("orizon_logging._log_dir", return_value=tmp_path):
        yield tmp_path

def get_my_console_handler(logger):
    """Helper to find our StreamHandler ignoring pytest LogCaptureHandler."""
    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.handlers.RotatingFileHandler) and type(h).__name__ != "LogCaptureHandler":
            return h
    return None

def test_setup_logging_default(reset_logging_state, mock_log_dir):
    """Test setup_logging with default arguments (INFO console level, file logging enabled)."""
    logger = setup_logging()

    assert logger.name == orizon_logging._ROOT_NAME
    assert logger.level == logging.DEBUG
    assert not logger.propagate

    # Expecting our 2 handlers + possibly pytest handlers
    # So we should just assert our handlers are present
    console_handler = get_my_console_handler(logger)
    file_handler = next((h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)), None)

    assert console_handler is not None
    assert console_handler.level == logging.INFO

    assert file_handler is not None
    assert file_handler.level == logging.DEBUG
    assert (mock_log_dir / "orizon.log").exists()

def test_setup_logging_verbose(reset_logging_state, mock_log_dir):
    """Test setup_logging with verbose=True."""
    logger = setup_logging(verbose=True)

    console_handler = get_my_console_handler(logger)
    assert console_handler is not None
    assert console_handler.level == logging.DEBUG

def test_setup_logging_quiet(reset_logging_state, mock_log_dir):
    """Test setup_logging with quiet=True."""
    logger = setup_logging(quiet=True)

    console_handler = get_my_console_handler(logger)
    assert console_handler is not None
    assert console_handler.level == logging.WARNING

def test_setup_logging_idempotent(reset_logging_state, mock_log_dir):
    """Test that multiple calls return the same logger and do not add duplicate handlers."""
    logger1 = setup_logging()
    initial_handler_count = len(logger1.handlers)

    logger2 = setup_logging()

    assert logger1 is logger2
    assert len(logger2.handlers) == initial_handler_count

def test_setup_logging_oserror(reset_logging_state, mock_log_dir):
    """Test graceful degradation to console-only if file handler fails."""
    with mock.patch("logging.handlers.RotatingFileHandler", side_effect=OSError("Disk full")):
        logger = setup_logging()

    console_handler = get_my_console_handler(logger)
    file_handler = next((h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)), None)

    # Should only have the StreamHandler (console) and no RotatingFileHandler
    assert console_handler is not None
    assert file_handler is None

def test_get_logger():
    """Test getting child loggers."""
    child1 = get_logger("test")
    assert child1.name == f"{orizon_logging._ROOT_NAME}.test"

    # Calling it with prefix already there
    child2 = get_logger(f"{orizon_logging._ROOT_NAME}.test2")
    assert child2.name == f"{orizon_logging._ROOT_NAME}.test2"
