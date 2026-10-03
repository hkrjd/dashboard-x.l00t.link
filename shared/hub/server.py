"""Entry point for uvicorn: `uvicorn --factory hub.server:build`."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .app import create_app
from .config import load_settings


def build() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app(load_settings())
