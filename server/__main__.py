"""Run the web app: `python -m server`."""

from __future__ import annotations

import uvicorn

from .config import SETTINGS


def main() -> None:
    print(f"Relative value charts -> http://{SETTINGS.host}:{SETTINGS.port}")
    uvicorn.run("server.api:app", host=SETTINGS.host, port=SETTINGS.port, reload=False)


if __name__ == "__main__":
    main()
