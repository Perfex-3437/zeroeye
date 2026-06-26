"""Unit tests for health_check rate limiter and timeout features."""
import socket
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Add parent directory to path so we can import health_check
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from health_check import (
    TokenBucket,
    effective_probe_rate,
    set_circuit_breaker_state,
    get_circuit_breaker_state,
    CIRCUIT_CLOSED,
    CIRCUIT_OPEN,
    CIRCUIT_HALF_OPEN,
    _rate_limiter,
    should_proceed,
    check_http_service,
    check_tcp_port,
)


class TestTokenBucketRateLimiter(unittest.TestCase):
    """Test the Token Bucket rate limiter logic."""

    def setUp(self):
        self.bucket = TokenBucket(rate=10.0, burst=5)

    def test_initial_state(self):
        """Test that the bucket starts with burst tokens available."""
        stats = self.bucket.stats()
        self.assertEqual(stats["current_rate"], 10.0)
        self.assertEqual(stats["available_tokens"], 5.0)
        self.assertEqual(stats["total_requests"], 0)
        self.assertEqual(stats["throttled_requests"], 0)

    def test_acquire_success(self):
        """Test that acquire returns True when tokens are available."""
        result = self.bucket.acquire()
        self.assertTrue(result)
        stats = self.bucket.stats()
        self.assertEqual(stats["total_requests"], 1)
        self.assertEqual(stats["throttled_requests"], 0)
        # Should have consumed 1 token
        self.assertAlmostEqual(stats["available_tokens"], 4.0, places=1)

    def test_acquire_throttled(self):
        """Test that acquire returns False when tokens are exhausted."""
        # Drain the bucket (burst=5)
        for _ in range(5):
            self.bucket.acquire()
        # Next acquire should fail
        result = self.bucket.acquire()
        self.assertFalse(result)
        stats = self.bucket.stats()
        self.assertEqual(stats["throttled_requests"], 1)
        self.assertEqual(stats["total_requests"], 6)

    def test_token_refill_over_time(self):
        """Test that tokens refill over time."""
        # Drain the bucket
        for _ in range(5):
            self.bucket.acquire()
        # Wait a bit for refill (rate=10/s = 0.01 per ms)
        time.sleep(0.15)  # ~1.5 tokens should refill
        stats = self.bucket.stats()
        self.assertGreater(stats["available_tokens"], 1.0)
        # Should be able to acquire again
        result = self.bucket.acquire()
        self.assertTrue(result)

    def test_reset(self):
        """Test that reset restores the bucket."""
        for _ in range(5):
            self.bucket.acquire()
        self.bucket.reset()
        stats = self.bucket.stats()
        self.assertEqual(stats["available_tokens"], 5.0)
        self.assertEqual(stats["throttled_requests"], 0)
        self.assertEqual(stats["total_requests"], 0)

    def test_rate_adjustment(self):
        """Test dynamic rate adjustment."""
        self.bucket.rate = 5.0
        self.assertEqual(self.bucket.rate, 5.0)
        stats = self.bucket.stats()
        self.assertEqual(stats["current_rate"], 5.0)


class TestHalfOpenRateReduction(unittest.TestCase):
    """Test that HALF_OPEN circuit breaker state reduces probe rate."""

    def setUp(self):
        set_circuit_breaker_state("test_service", CIRCUIT_CLOSED)

    def test_closed_rate(self):
        """Test that CLOSED state uses full configured rate."""
        rate = effective_probe_rate(10.0)
        self.assertEqual(rate, 10.0)

    def test_half_open_rate_reduction(self):
        """Test that HALF_OPEN state reduces rate to 50%."""
        set_circuit_breaker_state("test_service", CIRCUIT_HALF_OPEN)
        rate = effective_probe_rate(10.0)
        self.assertEqual(rate, 5.0)

    def test_open_state_no_reduction(self):
        """Test that OPEN state does not reduce rate (already blocked)."""
        set_circuit_breaker_state("test_service", CIRCUIT_OPEN)
        rate = effective_probe_rate(10.0)
        self.assertEqual(rate, 10.0)

    def test_multiple_services_half_open(self):
        """Test that one HALF_OPEN among many services triggers reduction."""
        set_circuit_breaker_state("backend", CIRCUIT_CLOSED)
        set_circuit_breaker_state("market", CIRCUIT_HALF_OPEN)
        set_circuit_breaker_state("frailbox", CIRCUIT_OPEN)
        rate = effective_probe_rate(10.0)
        self.assertEqual(rate, 5.0)

    def tearDown(self):
        set_circuit_breaker_state("test_service", CIRCUIT_CLOSED)
        set_circuit_breaker_state("backend", CIRCUIT_CLOSED)
        set_circuit_breaker_state("market", CIRCUIT_CLOSED)
        set_circuit_breaker_state("frailbox", CIRCUIT_CLOSED)


class TestTimeoutHandling(unittest.TestCase):
    """Test that timeout values are properly passed to check functions."""

    @patch("http.client.HTTPConnection")
    def test_http_service_timeout_passed(self, mock_conn):
        """Test that the timeout parameter reaches HTTPConnection."""
        mock_instance = MagicMock()
        mock_conn.return_value = mock_instance
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"status": "ok"}'
        mock_instance.getresponse.return_value = mock_response

        result, detail, code = check_http_service("localhost", 8080, "/health", timeout=30)

        # Verify timeout was passed to HTTPConnection
        call_args, call_kwargs = mock_conn.call_args
        self.assertIn("timeout", call_kwargs)
        self.assertEqual(call_kwargs["timeout"], 30)

    @patch("socket.create_connection")
    def test_tcp_port_timeout_passed(self, mock_socket):
        """Test that the timeout parameter reaches socket.create_connection."""
        mock_sock = MagicMock()
        mock_socket.return_value = mock_sock
        mock_sock.__enter__.return_value = mock_sock

        result, detail, latency = check_tcp_port("localhost", 5432, timeout=15)

        # Verify timeout was passed to create_connection
        call_args, call_kwargs = mock_socket.call_args
        self.assertIn("timeout", call_kwargs)
        self.assertEqual(call_kwargs["timeout"], 15)

    @patch("socket.create_connection")
    def test_tcp_port_timeout_short(self, mock_socket):
        """Test with a very short timeout."""
        mock_socket.side_effect = socket.timeout("timed out")
        result, detail, latency = check_tcp_port("localhost", 5432, timeout=0.1)
        self.assertEqual(result, "CRITICAL")
        self.assertIn("timeout", detail)


class TestCircuitBreakerStateTracking(unittest.TestCase):
    """Test circuit breaker state tracking functions."""

    def setUp(self):
        set_circuit_breaker_state("test_svc", CIRCUIT_CLOSED)

    def test_default_state(self):
        """Test that unknown services default to CLOSED."""
        state = get_circuit_breaker_state("nonexistent_service")
        self.assertEqual(state, CIRCUIT_CLOSED)

    def test_set_and_get(self):
        """Test setting and getting circuit breaker state."""
        set_circuit_breaker_state("test_svc", CIRCUIT_OPEN)
        self.assertEqual(get_circuit_breaker_state("test_svc"), CIRCUIT_OPEN)

        set_circuit_breaker_state("test_svc", CIRCUIT_HALF_OPEN)
        self.assertEqual(get_circuit_breaker_state("test_svc"), CIRCUIT_HALF_OPEN)

        set_circuit_breaker_state("test_svc", CIRCUIT_CLOSED)
        self.assertEqual(get_circuit_breaker_state("test_svc"), CIRCUIT_CLOSED)

    def tearDown(self):
        set_circuit_breaker_state("test_svc", CIRCUIT_CLOSED)


if __name__ == "__main__":
    unittest.main()
