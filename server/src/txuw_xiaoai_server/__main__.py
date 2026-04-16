from __future__ import annotations

import uvicorn

from .config import settings
from .logging import configure_logging


def main() -> None:
    configure_logging(settings.log_level)

    uvicorn.run(
        "txuw_xiaoai_server.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,
    )


if __name__ == "__main__":
    main()
