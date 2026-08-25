import logging
from orizon_logging import get_logger

def test_get_logger_with_root_prefix():
    """Test get_logger when the name already starts with the root prefix."""
    logger = get_logger("orizon.test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "orizon.test"

def test_get_logger_without_root_prefix():
    """Test get_logger when the name doesn't start with the root prefix."""
    logger = get_logger("test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "orizon.test"

def test_get_logger_with_partial_root_prefix():
    """Test get_logger when the name has 'orizon' but not 'orizon.'."""
    logger = get_logger("orizon_test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "orizon.orizon_test"

def test_get_logger_empty_name():
    """Test get_logger with an empty name."""
    logger = get_logger("")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "orizon."
