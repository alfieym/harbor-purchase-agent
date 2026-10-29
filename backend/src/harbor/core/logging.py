import logging


def setup_logging(level: str = "INFO") -> None:
    resolved = level.upper()
    if resolved not in logging.getLevelNamesMapping():
        resolved = "INFO"

    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("uvicorn").setLevel(resolved)
    logging.getLogger("uvicorn.access").setLevel(resolved)
