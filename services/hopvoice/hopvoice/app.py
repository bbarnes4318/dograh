"""Compatibility import for the Hopvoice ASGI server."""

from .server import app, create_app

__all__ = ["app", "create_app"]
