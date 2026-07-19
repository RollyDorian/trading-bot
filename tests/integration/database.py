import os

import pytest

from trading_bot.config import integration_test_database_url


def require_test_database_url() -> str:
    if os.getenv("TEST_DATABASE_URL") is None:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return integration_test_database_url()
