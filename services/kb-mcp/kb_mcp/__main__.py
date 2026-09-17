import os

import uvicorn

from .app import build_http_app


def main() -> None:
    port = int(os.environ.get("PORT", "7878"))
    app = build_http_app()
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
