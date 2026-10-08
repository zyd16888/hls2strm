import uvicorn

from .app import create_app
from .config import BootConfig


def main() -> None:
    boot = BootConfig.from_env()
    uvicorn.run(
        create_app(boot),
        host=boot.host,
        port=boot.port,
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
