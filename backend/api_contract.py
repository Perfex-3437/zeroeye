"""Utilities for pytest-based validation of the documented backend API.

The runtime backend in this repository is Rust, while the public HTTP API
contract is documented in `docs/openapi/v3.yaml`. These helpers load that
contract, expose its operations in a test-friendly shape, and provide an
offline mock client for success, error, and edge-case tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
DEFAULT_SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi" / "v3.yaml"


@dataclass(frozen=True)
class ApiOperation:
    """A public API operation extracted from the OpenAPI path map."""

    method: str
    path: str
    operation_id: str
    responses: tuple[int, ...]
    request_body_required: bool
    required_fields: tuple[str, ...]
    parameters: tuple[str, ...]

    @property
    def success_statuses(self) -> tuple[int, ...]:
        """Return documented 2xx response codes."""

        return tuple(code for code in self.responses if 200 <= code < 300)

    @property
    def error_statuses(self) -> tuple[int, ...]:
        """Return documented 4xx/5xx response codes."""

        return tuple(code for code in self.responses if code >= 400)


@dataclass(frozen=True)
class MockResponse:
    """Small response object used by offline API contract tests."""

    status_code: int
    body: Mapping[str, Any]


def load_openapi_spec(path: Path = DEFAULT_SPEC_PATH) -> Mapping[str, Any]:
    """Load the repository OpenAPI document from disk without network access."""

    with path.open(encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)
    if not isinstance(spec, Mapping):
        raise ValueError(f"OpenAPI spec at {path} did not parse to a mapping")
    return spec


def iter_operations(spec: Mapping[str, Any]) -> tuple[ApiOperation, ...]:
    """Return every public HTTP operation declared in the OpenAPI spec."""

    paths = spec.get("paths", {})
    if not isinstance(paths, Mapping):
        raise ValueError("OpenAPI spec is missing a paths mapping")

    operations: list[ApiOperation] = []
    for path, path_item in sorted(paths.items()):
        if not isinstance(path_item, Mapping):
            continue
        for method, operation in sorted(path_item.items()):
            if method not in HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            operations.append(_build_operation(spec, path, method, operation))
    return tuple(operations)


def operation_key(method: str, path: str) -> tuple[str, str]:
    """Normalize a method/path pair for dictionary lookups."""

    return method.lower(), path


def build_valid_payload(spec: Mapping[str, Any], operation: ApiOperation) -> dict[str, Any]:
    """Build a minimal payload for an operation with a required request body."""

    schema = _request_schema(spec, operation.path, operation.method)
    required_fields = operation.required_fields
    if not required_fields:
        return {"request_id": "test-request"}

    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    payload: dict[str, Any] = {}
    for field in required_fields:
        field_schema = properties.get(field, {}) if isinstance(properties, Mapping) else {}
        payload[field] = _sample_value(field, field_schema)
    return payload


class MockBackendApiClient:
    """Offline client that returns responses from the OpenAPI contract shape."""

    def __init__(self, spec: Mapping[str, Any], operations: Iterable[ApiOperation]):
        self.spec = spec
        self.operations = {operation_key(op.method, op.path): op for op in operations}

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> MockResponse:
        """Return a deterministic mock response for a documented operation."""

        operation = self.operations.get(operation_key(method, path))
        if operation is None:
            return MockResponse(404, {"code": 4004, "message": "Resource not found"})

        if payload and payload.get("__force_internal_error__"):
            return MockResponse(500, {"code": 5001, "message": "Internal server error"})

        if self._requires_authorization(operation) and not token:
            status_code = _first_available(operation.responses, (401, 403), default=400)
            return MockResponse(status_code, {"code": 4002, "message": "Authentication required"})

        if operation.request_body_required and not payload:
            status_code = _first_available(operation.responses, (422, 400, 409), default=422)
            return MockResponse(status_code, {"code": 4001, "message": "Invalid request parameters"})

        status_code = min(operation.success_statuses or (200,))
        return MockResponse(
            status_code,
            {"operation_id": operation.operation_id, "path": operation.path, "ok": True},
        )

    async def request_async(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> MockResponse:
        """Async wrapper used by pytest-asyncio tests."""

        return self.request(method, path, payload=payload, token=token)

    @staticmethod
    def _requires_authorization(operation: ApiOperation) -> bool:
        return not operation.path.startswith("/auth/")


def _build_operation(
    spec: Mapping[str, Any],
    path: str,
    method: str,
    operation: Mapping[str, Any],
) -> ApiOperation:
    responses = tuple(sorted(_response_codes(operation.get("responses", {}))))
    request_body = operation.get("requestBody", {})
    request_required = bool(isinstance(request_body, Mapping) and request_body.get("required"))
    schema = _request_schema(spec, path, method)
    required_fields = tuple(schema.get("required", ())) if isinstance(schema, Mapping) else ()
    parameters = tuple(_parameter_names(operation.get("parameters", ())))
    operation_id = str(operation.get("operationId") or f"{method}_{path}".replace("/", "_"))
    return ApiOperation(
        method=method.upper(),
        path=path,
        operation_id=operation_id,
        responses=responses,
        request_body_required=request_required,
        required_fields=required_fields,
        parameters=parameters,
    )


def _request_schema(spec: Mapping[str, Any], path: str, method: str) -> Mapping[str, Any]:
    operation = spec.get("paths", {}).get(path, {}).get(method.lower(), {})
    request_body = operation.get("requestBody", {}) if isinstance(operation, Mapping) else {}
    content = request_body.get("content", {}) if isinstance(request_body, Mapping) else {}
    json_content = content.get("application/json", {}) if isinstance(content, Mapping) else {}
    schema = json_content.get("schema", {}) if isinstance(json_content, Mapping) else {}
    if isinstance(schema, Mapping) and "$ref" in schema:
        try:
            return _resolve_ref(spec, str(schema["$ref"]))
        except KeyError:
            return {}
    return schema if isinstance(schema, Mapping) else {}


def _resolve_ref(spec: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"Only local OpenAPI refs are supported: {ref}")
    current: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(current, Mapping) or part not in current:
            raise KeyError(f"OpenAPI ref segment not found: {ref}")
        current = current[part]
    if not isinstance(current, Mapping):
        raise ValueError(f"OpenAPI ref does not resolve to a mapping: {ref}")
    return current


def _response_codes(responses: Any) -> set[int]:
    if not isinstance(responses, Mapping):
        return set()
    codes: set[int] = set()
    for code in responses:
        try:
            codes.add(int(code))
        except (TypeError, ValueError):
            continue
    return codes


def _parameter_names(parameters: Any) -> list[str]:
    if not isinstance(parameters, list):
        return []
    names: list[str] = []
    for parameter in parameters:
        if isinstance(parameter, Mapping) and "name" in parameter:
            names.append(str(parameter["name"]))
    return names


def _first_available(responses: tuple[int, ...], candidates: tuple[int, ...], *, default: int) -> int:
    for candidate in candidates:
        if candidate in responses:
            return candidate
    return default


def _sample_value(field: str, schema: Any) -> Any:
    if not isinstance(schema, Mapping):
        return f"test-{field}"

    field_type = schema.get("type")
    if field == "email":
        return "user@example.com"
    if field == "password":
        return "correct-horse-battery-staple"
    if field_type == "boolean":
        return True
    if field_type == "integer":
        return 1
    if field_type == "number":
        return 1.0
    if field_type == "array":
        return []
    if field_type == "object":
        return {}
    return f"test-{field}"
