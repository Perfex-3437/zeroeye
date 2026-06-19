# Health Check Retry Test

This directory contains a mock server for testing the health check retry behavior.

## Quick Test

1. Start the mock server (fails first 2 requests, succeeds on 3rd):
```bash
python3 tests/mock_health_server.py --port 9999 --fail 2
```

2. In another terminal, run health check against the mock server:
```bash
python3 tools/health_check.py --json --retries 3 --backoff-secs 0.5
```

The output will show that the specified service endpoint attempts the
connection with retry, and the `retry_config` and per-attempt details
are recorded in the JSON output.

## Parameters

- `--retries N`: Max retry attempts (default: 3)
- `--timeout-secs N`: Per-attempt timeout override
- `--backoff-secs N`: Base backoff between retries, doubled each attempt (default: 2.0)
- `--json`: Output in JSON format with per-attempt details

## Retry Behavior

- Retries only network timeouts, connection errors, and HTTP 5xx responses
- HTTP 4xx responses are NOT retried
- Each attempt records elapsed milliseconds and failure reason
- Exponential backoff: backoff_secs × (2 ^ attempt)
