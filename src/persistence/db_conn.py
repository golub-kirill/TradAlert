"""
Shared MySQL connect helper .

Three call sites previously had near-identical ``_connect`` implementations:
``src/persistence/db.py``, ``src/core/position_manager.py``, and
``backtest/db.py``. The first two added a ``_DB_OPTIONAL_KEYS`` check after
 so a missing DB_USER/DB_PASSWORD/DB_NAME degrades gracefully via
``ConfigError``; ``backtest/db.py`` was forgotten and still raised
``KeyError`` straight to the worker, crashing the sweep.

This module is the single canonical implementation. All three call sites
import from here.
"""

from __future__ import annotations

import logging
import os

import mysql.connector
from mysql.connector.abstracts import MySQLConnectionAbstract
from mysql.connector.pooling import PooledMySQLConnection

from exceptions import ConfigError

logger = logging.getLogger(__name__)

# keys whose absence must produce ConfigError, not KeyError —
# callers catch (MySQLError, ConfigError) to degrade gracefully.
_DB_OPTIONAL_KEYS: tuple[str, ...] = ("DB_USER", "DB_PASSWORD", "DB_NAME")


def connect() -> MySQLConnectionAbstract | PooledMySQLConnection:
    """Open a MySQL connection from environment variables.

    Env vars consulted:
    DB_HOST (default ``localhost``)
    DB_PORT (default ``3306``)
    DB_USER (required)
    DB_PASSWORD (required)
    DB_NAME (required)

    Raises
    ------
    ConfigError
    When any required env var is unset.
    mysql.connector.Error
    On connection failure.
    """
    missing = [k for k in _DB_OPTIONAL_KEYS if not os.environ.get(k)]
    if missing:
        raise ConfigError(
            "DB_" + "/DB_".join(m.replace("DB_", "") for m in missing),
            reason=f"environment variable(s) not set: {missing}",
        )
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        connect_timeout=5,
    )
