import os

from .server import mcp


def main() -> None:
    port = int(os.environ.get("PORT", "7878"))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
