# Backend API Test Suite

This suite validates the documented public backend API contract in
`docs/openapi/v3.yaml`. The runtime backend is implemented in Rust, so these
pytest tests use a small offline contract helper in `backend/api_contract.py`
instead of starting network services.

Coverage includes:

- one success-path assertion for every documented GET and POST operation
- authentication, not-found, request-validation, and internal-error cases
- empty, large, and unicode payload edge cases
- async request handling through pytest-asyncio
