"""
Shared pytest configuration.

Sets asyncio mode to auto so every async test function is automatically
treated as a coroutine test without requiring explicit @pytest.mark.asyncio
on each one (already present, but this makes it mode-consistent).
"""
import pytest


def pytest_configure(config):
    """Register custom marks to suppress PytestUnknownMarkWarning."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
