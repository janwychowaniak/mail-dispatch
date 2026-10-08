"""`python -m mail_dispatch`: read the configuration, then serve.

A configuration that cannot be used stops startup before the port is opened [D28]. One
process, no workers: a configuration error must end the container visibly.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import create_app
from .jsonlog import configure_logging, log_event
from .settings import ConfigError, load_settings


def main() -> int:
    configure_logging("INFO")
    try:
        settings = load_settings()
    except ConfigError as error:
        log_event("config_error", level=logging.ERROR, message=f"invalid configuration: {error}")
        return 2
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        log_config=None,
        # The service writes its own line per request (§9).
        access_log=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
