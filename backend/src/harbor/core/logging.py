import json
import logging
from typing import Any


class GuardPayloadFormatter(logging.Formatter):
    """Append the structured ``guard`` payload to the rendered line.

    ``log_guard_event`` passes its whitelisted fields as ``extra={"guard": …}``;
    the default formatter ignores extras, so without this the guard events
    would reach the log as a bare "guard.event" with no fields at all.
    """

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        payload: Any = getattr(record, "guard", None)
        if payload is None:
            return rendered
        try:
            return f"{rendered} {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        except (TypeError, ValueError):
            return f"{rendered} {payload!r}"


def setup_logging(level: str = "INFO") -> None:
    resolved = level.upper()
    if resolved not in logging.getLevelNamesMapping():
        resolved = "INFO"

    handler = logging.StreamHandler()
    handler.setFormatter(
        GuardPayloadFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=resolved, handlers=[handler], force=True)
    logging.getLogger("uvicorn").setLevel(resolved)
    logging.getLogger("uvicorn.access").setLevel(resolved)
