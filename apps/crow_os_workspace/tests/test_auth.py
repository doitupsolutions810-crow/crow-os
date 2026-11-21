from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crow_os.api import create_app


def test_required_auth_protects_ui_and_mutating_api_but_keeps_minimal_health(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("CROW_AUTH_REQUIRED", "true")
    monkeypatch.setenv("CROW_AUTH_USERNAME", "operator")
    monkeypatch.setenv("CROW_AUTH_PASSWORD", "test-password-42")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    app = create_app(repo, tmp_path / "auth.sqlite", tmp_path / "worktrees", ingest_on_startup=False)

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"ok": True}
        assert health.headers["x-content-type-options"] == "nosniff"

        for path in ("/", "/static/app.css", "/api/overview", "/api/rounds"):
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.headers["www-authenticate"].startswith("Basic realm=")

        assert client.get("/api/overview", auth=("operator", "wrong")).status_code == 401
        assert client.get("/api/overview", auth=("other", "test-password-42")).status_code == 401

        allowed = client.get("/api/overview", auth=("operator", "test-password-42"))
        assert allowed.status_code == 200
        assert allowed.headers["cache-control"] == "no-store"


def test_auth_required_fails_closed_without_a_password(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CROW_AUTH_REQUIRED", "true")
    monkeypatch.setenv("CROW_AUTH_USERNAME", "operator")
    monkeypatch.delenv("CROW_AUTH_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="CROW_AUTH_REQUIRED"):
        create_app(repo, tmp_path / "missing-password.sqlite", tmp_path / "worktrees", ingest_on_startup=False)


def test_auth_can_be_disabled_for_loopback_development(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CROW_AUTH_REQUIRED", "false")
    monkeypatch.delenv("CROW_AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    app = create_app(repo, tmp_path / "local.sqlite", tmp_path / "worktrees", ingest_on_startup=False)
    with TestClient(app) as client:
        assert client.get("/api/overview").status_code == 200
