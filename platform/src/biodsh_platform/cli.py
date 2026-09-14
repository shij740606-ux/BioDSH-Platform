import uvicorn

from biodsh_platform.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "biodsh_platform.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
