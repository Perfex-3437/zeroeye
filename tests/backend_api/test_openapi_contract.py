"""Tests for the documented backend API contract."""

from __future__ import annotations

import socket

import pytest


def test_spec_loads_without_network(monkeypatch, spec_path, api_operations):
    def blocked_socket(*_args, **_kwargs):
        raise AssertionError("API contract tests must run without network access")

    monkeypatch.setattr(socket, "socket", blocked_socket)

    assert spec_path.exists()
    assert api_operations


def test_every_operation_has_success_response(api_operations):
    assert all(operation.success_statuses for operation in api_operations)


def test_get_and_post_operations_are_covered(api_operations):
    methods = {operation.method for operation in api_operations}

    assert "GET" in methods
    assert "POST" in methods


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_at_least_one_operation_per_http_verb(api_operations, method):
    assert any(operation.method == method for operation in api_operations)


def test_success_cases_for_all_documented_operations(api_client, api_operations, valid_payloads, auth_token):
    for operation in api_operations:
        token = None if operation.path.startswith("/auth/") else auth_token
        payload = valid_payloads.get((operation.method, operation.path))

        response = api_client.request(operation.method, operation.path, payload=payload, token=token)

        assert response.status_code in operation.success_statuses
        assert response.body["operation_id"] == operation.operation_id


def test_missing_auth_token_returns_documented_auth_error(api_client, api_operations):
    protected_operations = [operation for operation in api_operations if not operation.path.startswith("/auth/")]

    for operation in protected_operations:
        response = api_client.request(operation.method, operation.path)

        assert response.status_code in {401, 403, 400}
        assert response.body["code"] == 4002


def test_unknown_endpoint_returns_not_found(api_client):
    response = api_client.request("GET", "/missing-resource")

    assert response.status_code == 404
    assert response.body["code"] == 4004


def test_missing_required_payload_returns_validation_error(api_client, api_operations):
    body_operations = [operation for operation in api_operations if operation.request_body_required]

    for operation in body_operations:
        response = api_client.request(operation.method, operation.path, payload=None)

        assert response.status_code in {400, 409, 422}
        assert response.body["code"] == 4001


def test_internal_error_path_is_mocked_without_external_dependencies(api_client):
    response = api_client.request(
        "POST",
        "/auth/login",
        payload={"__force_internal_error__": True},
    )

    assert response.status_code == 500
    assert response.body["code"] == 5001


def test_edge_case_payloads_are_accepted_by_contract_mock(api_client, api_operations, auth_token):
    operation = next(operation for operation in api_operations if operation.method == "POST")
    payload = {
        "email": "unicode-\\u2603@example.com",
        "password": "x" * 4096,
        "mfa_code": "",
        "client_fingerprint": "edge-case",
    }

    response = api_client.request(operation.method, operation.path, payload=payload, token=auth_token)

    assert response.status_code in operation.success_statuses


@pytest.mark.asyncio
async def test_async_request_wrapper(api_client, auth_token):
    response = await api_client.request_async("GET", "/users", token=auth_token)

    assert response.status_code == 200
    assert response.body["path"] == "/users"

