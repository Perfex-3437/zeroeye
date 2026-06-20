"""
Pytest configuration for the Tent of Trials API test suite.

Comprehensive fixtures, mock data, and auth tokens for testing
all backend API tools with mocked external dependencies.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

# Add the tools directory to sys.path so we can import the tools
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

# ---------------------------------------------------------------------------
# Mock Data Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_health_report():
    """A realistic health check report dictionary."""
    return {
        "timestamp": "2025-06-18T03:30:00",
        "hostname": "test-host",
        "services": {
            "backend": {"status": "OK", "detail": "HTTP 200", "code": 200,
                        "endpoint": "http://localhost:8080/health"},
            "market": {"status": "OK", "detail": "HTTP 200", "code": 200,
                       "endpoint": "http://localhost:8081/health"},
            "frailbox": {"status": "OK", "detail": "HTTP 200", "code": 200,
                         "endpoint": "http://localhost:8082/health"},
            "frontend": {"status": "OK", "detail": "HTTP 200", "code": 200,
                         "endpoint": "http://localhost:3000/"},
        },
        "infrastructure": {
            "postgresql": {"status": "OK", "detail": "Connected (5.2ms)", "endpoint": "localhost:5432"},
            "redis": {"status": "OK", "detail": "Connected (1.1ms)", "endpoint": "localhost:6379"},
            "kafka": {"status": "OK", "detail": "Connected (3.7ms)", "endpoint": "localhost:9092"},
        },
        "system": {
            "disk": {"status": "OK", "detail": "45.2% used (23GB/50GB)"},
            "memory": {"status": "OK", "detail": "34.5% used (16GB/32GB)"},
            "load": {"status": "OK", "detail": "Load: 1.2 (15% of 8 cores)", "load": 1.2},
        },
        "overall_status": "OK",
    }


@pytest.fixture
def sample_health_report_degraded():
    """A health check report with some failures."""
    return {
        "timestamp": "2025-06-18T03:30:00",
        "hostname": "test-host",
        "services": {
            "backend": {"status": "CRITICAL", "detail": "Connection refused", "code": 0,
                        "endpoint": "http://localhost:8080/health"},
            "market": {"status": "OK", "detail": "HTTP 200", "code": 200,
                       "endpoint": "http://localhost:8081/health"},
        },
        "infrastructure": {
            "postgresql": {"status": "OK", "detail": "Connected (5.2ms)", "endpoint": "localhost:5432"},
        },
        "system": {
            "disk": {"status": "WARNING", "detail": "82.1% used"},
            "memory": {"status": "OK", "detail": "45% used"},
            "load": {"status": "OK", "detail": "Load: 0.5", "load": 0.5},
        },
        "overall_status": "DEGRADED",
    }


@pytest.fixture
def sample_deploy_history():
    """Sample deployment history data."""
    return [
        {
            "timestamp": "2025-06-17T12:00:00",
            "service": "backend",
            "version": "v3.2.0",
            "status": "success",
            "deployed_by": "ci-bot",
        },
        {
            "timestamp": "2025-06-17T13:00:00",
            "service": "frontend",
            "version": "v3.2.0",
            "status": "success",
            "deployed_by": "ci-bot",
        },
        {
            "timestamp": "2025-06-17T14:00:00",
            "service": "market",
            "version": "v3.1.0",
            "status": "failed",
            "deployed_by": "manual",
        },
    ]


@pytest.fixture
def sample_alert_rules():
    """Sample alert rules for monitoring setup."""
    return [
        {
            "name": "HighErrorRate",
            "expr": "sum(rate(http_errors_total[5m])) / sum(rate(http_requests_total[5m])) > 0.05",
            "duration": "5m",
            "severity": "critical",
            "summary": "High HTTP error rate",
            "description": "Error rate is above 5% for 5 minutes",
        },
        {
            "name": "ServiceDown",
            "expr": "up == 0",
            "duration": "1m",
            "severity": "critical",
            "summary": "Service is down",
            "description": "Instance has been unreachable for 1 minute",
        },
    ]


@pytest.fixture
def sample_deployment_config():
    """Sample deployment configuration dictionary."""
    return {
        "service": "backend",
        "env": "staging",
        "tag": "v3.2.0",
        "skip_build": False,
        "skip_test": False,
        "skip_health": False,
    }


@pytest.fixture
def sample_db_migrations():
    """Sample migration records."""
    return [
        {"version": "20210101000000", "description": "Initial schema", "type": "sql", "applied": True},
        {"version": "20210102000000", "description": "Add user profiles", "type": "sql", "applied": True},
        {"version": "20210103000000", "description": "Create audit logs", "type": "sql", "applied": False},
    ]


# ---------------------------------------------------------------------------
# Auth Token Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_auth_token():
    """A mock JWT-like auth token for API authentication."""
    return "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0LXVzZXIiLCJpYXQiOjE3NTAwMDAwMDB9.test-signature"


@pytest.fixture
def mock_grafana_api_key():
    """A mock Grafana API key."""
    return "glsa_mock_api_key_for_testing_purposes_1234567890"


@pytest.fixture
def mock_slack_webhook():
    """A mock Slack webhook URL."""
    return "https://hooks.slack.com/services/T00/B00/mock_webhook_token"


@pytest.fixture
def mock_pagerduty_key():
    """A mock PagerDuty integration key."""
    return "pd_key_mock_integration_key_for_testing"


# ---------------------------------------------------------------------------
# Temporary File / Directory Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workspace():
    """Create a temporary workspace directory for tests."""
    with tempfile.TemporaryDirectory(prefix="zeroeye_test_") as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def tmp_config_file(tmp_workspace):
    """Create a temporary config file."""
    path = tmp_workspace / "test_config.yaml"
    yield path


@pytest.fixture
def tmp_output_dir(tmp_workspace):
    """Create a temporary output directory."""
    out_dir = tmp_workspace / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    yield out_dir


# ---------------------------------------------------------------------------
# Mock System State Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_env_vars():
    """Set mock environment variables for testing."""
    env_patches = {
        "DB_HOST": "mock-db-host",
        "DB_PORT": "15432",
        "DB_NAME": "test_db",
        "DB_USER": "test_user",
        "DB_PASSWORD": "test_pass",
        "REDIS_HOST": "mock-redis-host",
        "REDIS_PORT": "16379",
        "KAFKA_HOST": "mock-kafka-host",
        "KAFKA_PORT": "19092",
        "USER": "test-user",
    }
    with patch.dict(os.environ, env_patches, clear=False):
        yield


# ---------------------------------------------------------------------------
# Mock Patch Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_http_connection():
    """Mock http.client.HTTPConnection for health check tests."""
    with patch("http.client.HTTPConnection") as mock:
        conn_instance = MagicMock()
        mock.return_value = conn_instance
        yield conn_instance


@pytest.fixture
def mock_socket():
    """Mock socket operations."""
    with patch("socket.create_connection") as mock:
        mock_sock = MagicMock()
        mock.return_value = mock_sock
        yield mock, mock_sock


@pytest.fixture
def mock_subprocess_run():
    """Mock subprocess.run for deploy and migration tests."""
    with patch("subprocess.run") as mock:
        yield mock


@pytest.fixture
def mock_urllib_request():
    """Mock urllib.request for monitoring tests."""
    with patch("urllib.request.urlopen") as mock_urlopen, \
         patch("urllib.request.Request") as mock_request, \
         patch("urllib.request.build_opener") as mock_builder:
        yield mock_urlopen, mock_request


@pytest.fixture
def mock_open_files():
    """Allow mocking open() for file read/write operations in tests."""
    mock_data = {}

    def custom_open(filepath, mode="r", *args, **kwargs):
        from io import StringIO
        if "r" in mode:
            content = mock_data.get(str(filepath), "")
            return StringIO(content)
        elif "w" in mode:
            mock_obj = MagicMock()
            mock_data[str(filepath)] = ""
            return mock_obj
        return MagicMock()

    with patch("builtins.open", side_effect=custom_open) as mock_open:
        mock_open.mock_data = mock_data
        yield mock_open
