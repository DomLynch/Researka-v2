"""Capture unexpected ASGI errors without supplying request state to the SDK."""
from functools import wraps
from typing import Callable, ParamSpec, TypeVar

from starlette.types import ASGIApp, Receive, Scope, Send

from runtime_core.error_reporting import configure_error_reporting, flush_error_reporting, report_error

P = ParamSpec("P")
T = TypeVar("T")


def report_api_startup(factory: Callable[P, T]) -> Callable[P, T]:
    @wraps(factory)
    def build(*args: P.args, **kwargs: P.kwargs) -> T:
        configure_error_reporting("api")
        try:
            return factory(*args, **kwargs)
        except Exception as exc:
            report_error(exc, stage="api_startup")
            flush_error_reporting()
            raise
    return build


class ErrorReportingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            report_error(exc, stage="api")
            raise
        finally:
            if scope["type"] == "lifespan":
                flush_error_reporting()
