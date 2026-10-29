import uvicorn

from harbor.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "harbor.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
