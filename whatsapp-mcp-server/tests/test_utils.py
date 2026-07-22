"""Tests for lib/utils.py utility functions."""

import logging

from lib.utils import setup_logging


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_default(self):
        """Test default logging setup (INFO level)."""
        logger = setup_logging(debug=False)

        assert logger.name == "whatsapp-mcp"
        assert logger.level <= logging.INFO

    def test_setup_logging_debug(self):
        """Test debug logging setup."""
        logger = setup_logging(debug=True)

        assert logger.level <= logging.DEBUG
