"""`migrate` entrypoint: `alembic upgrade head` against DATABASE_URL.

Runs as the Kubernetes Job (orders image, command `["migrate"]`) and the compose one-off before services start.
"""

import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from orders.settings import Settings
from shopflow_common.log import configure_logging

# Source checkouts (editable install) use services/orders/alembic.ini; images set ALEMBIC_CONFIG to their copy.
_SOURCE_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

log = logging.getLogger("migrate")


def alembic_config(database_url: str | None = None) -> Config:
    config = Config(os.environ.get("ALEMBIC_CONFIG", str(_SOURCE_INI)))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def upgrade(database_url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def main() -> None:
    settings = Settings()
    configure_logging("orders", settings.log_level)
    log.info("applying migrations", extra={"target": "head"})
    upgrade(settings.database_url.get_secret_value())
    log.info("migrations applied")
