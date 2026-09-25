"""Shared fixtures: a fully isolated app rooted in a temp directory."""

from __future__ import annotations

import pytest

from app import create_app
from config import AppConfig, ensure_dirs


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig(root=tmp_path)
    ensure_dirs(c)
    return c


@pytest.fixture
def app(cfg):
    return create_app(cfg)


@pytest.fixture
def client(app):
    return app.test_client()
