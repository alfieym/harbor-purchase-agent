"""HTTP correlation middleware for S13.

Every request becomes a trace with a stable id: the incoming ``X-Request-ID``
when the caller supplies one, otherwise a generated id. The id goes back on
the response header so a log line, a trace and the buyer's report can be
joined. The SSE stream is skipped — an open stream is not a request that
finishes, so tracing it would leave a span open for the connection's lifetime.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from harbor.observability import get_observability
from harbor.observability.trace import current_trace_id

_SKIP_SUFFIX = "/observability/events/stream"


async def observability_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # Prefer the app's own instance so the middleware and the /observability
    # routes always read and write the same store.
    if hasattr(request.app.state, "observability"):
        instance = request.app.state.observability
    else:
        instance = get_observability()
    if instance is None or request.url.path.endswith(_SKIP_SUFFIX):
        return await call_next(request)

    # A client-supplied request id is a correlation input, never a store key:
    # a gateway that repeats one id must not collapse every request into the
    # first trace. The generated trace id is what the store is keyed by.
    attributes = {"method": request.method, "path": request.url.path}
    request_id = request.headers.get("x-request-id")
    if request_id:
        attributes["request_id"] = request_id

    with instance.tracer.trace_scope("http.request", **attributes) as trace_span:
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled error never yields a response, so the accounting
            # below would be skipped — and a 500 is exactly what the metrics
            # line exists to surface. There is no response to stamp, so the
            # trace id cannot go back on the header in this path.
            instance.metrics.incr("http.requests")
            instance.metrics.incr("http.status.500")
            trace_span.attributes["status_code"] = 500
            trace_span.fail("http_500")
            raise
        trace_span.attributes["status_code"] = response.status_code
        if response.status_code >= 500:
            trace_span.fail(f"http_{response.status_code}")
        trace_id = current_trace_id()
        if trace_id is not None:
            response.headers["X-Trace-ID"] = trace_id
        instance.metrics.incr("http.requests")
        instance.metrics.incr(f"http.status.{response.status_code}")
        return response
