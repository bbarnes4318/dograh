"""ASGI entry: ``uvicorn hopstt.app:app`` (prefer ``python -m hopstt``,
which drains on SIGTERM)."""

from .server import _app_from_env, create_app

app = _app_from_env()

__all__ = ["app", "create_app"]
