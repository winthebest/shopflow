"""Run a service under uvicorn with the shared runtime contract (docs/contracts/services.md)."""

import uvicorn
from fastapi import FastAPI

# SIGTERM: uvicorn closes the listening socket at once, then waits this long for in-flight requests.
GRACEFUL_SHUTDOWN_S = 20


def serve(app: FastAPI, *, port: int) -> None:
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - container port, exposed only through the Service/compose network
        port=port,
        log_config=None,  # keep the JSON handler from configure_logging()
        access_log=False,  # AccessLogMiddleware writes access lines with trace ids
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
