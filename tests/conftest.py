"""Shared fixtures for backend API contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.api_contract import (
    DEFAULT_SPEC_PATH,
    MockBackendApiClient,
    build_valid_payload,
    iter_operations,
    load_openapi_spec,
)


@pytest.fixture(scope="session")
def spec_path() -> Path:
    # Centralize the public API spec location so tests do not duplicate paths.
    return DEFAULT_SPEC_PATH


@pytest.fixture(scope="session")
def api_spec(spec_path: Path):
    # Load the OpenAPI document once; no test fixture performs network I/O.
    return load_openapi_spec(spec_path)


@pytest.fixture(scope="session")
def api_operations(api_spec):
    # Expose every documented operation as normalized method/path metadata.
    return iter_operations(api_spec)


@pytest.fixture(scope="session")
def api_client(api_spec, api_operations):
    # Use a deterministic offline mock so API behavior can be tested without external services.
    return MockBackendApiClient(api_spec, api_operations)


@pytest.fixture(scope="session")
def auth_token() -> str:
    # Representative bearer token for endpoints that require authentication.
    return "test-access-token"


@pytest.fixture(scope="session")
def valid_payloads(api_spec, api_operations):
    # Build minimal request bodies from schema-required fields for POST endpoints.
    return {
        (operation.method, operation.path): build_valid_payload(api_spec, operation)
        for operation in api_operations
        if operation.request_body_required
    }
