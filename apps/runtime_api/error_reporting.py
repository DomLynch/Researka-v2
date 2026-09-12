"""Capture unexpected ASGI errors without supplying request state to the SDK."""
from starlette.types import ASGIApp, Receive, Scope, Send

from runtime_core.error_reporting import configure_error_reporting, report_error


class ErrorReportingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        configure_error_reporting("api")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            report_error(exc, stage="api")
            raise
