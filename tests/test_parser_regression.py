"""Regression tests for log aggregator parsers (JSON, Text, Nginx).

Tests use hand-written sample log lines covering valid, malformed, and
edge-case inputs. No parser-generated fixtures or network resources.
"""

import json
import sys
import os
import unittest
from datetime import datetime, timezone

# Add repo root to path and import via module file
_test_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.join(_test_dir, "..")
sys.path.insert(0, _repo_root)

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "la_mod", os.path.join(_repo_root, "tools", "log_aggregator.py")
)
_la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_la)

JSONLogParser = _la.JSONLogParser
TextLogParser = _la.TextLogParser
NginxLogParser = _la.NginxLogParser


class TestJSONLogParser(unittest.TestCase):
    """Tests for JSONLogParser with hand-written sample lines."""

    def setUp(self):
        self.parser = JSONLogParser()

    def test_valid_json(self):
        line = '{"timestamp": "2026-06-20T10:00:00Z", "level": "error", "service": "backend", "message": "DB timeout"}'
        result = self.parser.parse(line)
        assert result is not None
        assert result["level"] == "error"
        assert result["service"] == "backend"
        assert "DB timeout" in result["message"]

    def test_json_with_alternative_fields(self):
        line = '{"time": "2026-06-20T10:00:00Z", "severity": "warn", "logger": "market", "event": "Order rejected"}'
        result = self.parser.parse(line)
        assert result is not None
        assert result["level"] == "warn"
        assert result["service"] == "market"
        assert "Order rejected" in result["message"]

    def test_malformed_json(self):
        line = '{this is not json}'
        result = self.parser.parse(line)
        assert result is None, "malformed JSON should return None"

    def test_empty_json_object(self):
        line = '{}'
        result = self.parser.parse(line)
        assert result is not None
        assert result["level"] == "info"
        assert result["service"] is None

    def test_json_non_dict(self):
        line = '["array", "not", "dict"]'
        result = self.parser.parse(line)
        assert result is None, "JSON array should return None"


class TestTextLogParser(unittest.TestCase):
    """Tests for TextLogParser with hand-written sample lines."""

    def setUp(self):
        self.parser = TextLogParser()

    def test_empty_line(self):
        result = self.parser.parse("")
        assert result is None, "empty line should return None"

    def test_blank_line(self):
        result = self.parser.parse("   ")
        assert result is None, "whitespace-only line should return None"

    def test_plain_message(self):
        result = self.parser.parse("Server started on port 8080")
        assert result is not None
        assert "Server started" in result["message"]
        assert result["format"] == "text"

    def test_malformed_input(self):
        result = self.parser.parse("\x00\x01\x02binary garbage\x1f\x8b")
        assert result is not None
        assert result["format"] == "text"


class TestNginxLogParser(unittest.TestCase):
    """Tests for NginxLogParser with hand-written sample lines."""

    def setUp(self):
        self.parser = NginxLogParser()

    def test_valid_nginx_200(self):
        line = '192.168.1.1 - - [20/Jun/2026:10:00:00 +0000] "GET /api/health HTTP/1.1" 200 1234 "-" "curl/7.68"'
        result = self.parser.parse(line)
        assert result is not None
        assert result["level"] == "info"
        assert result["service"] == "nginx"
        assert result["fields"]["status"] == 200
        assert result["fields"]["remote_addr"] == "192.168.1.1"

    def test_nginx_404_is_warn(self):
        line = '10.0.0.1 - admin [20/Jun/2026:11:30:00 +0000] "GET /missing HTTP/1.1" 404 0 "-" "Mozilla/5.0"'
        result = self.parser.parse(line)
        assert result is not None
        assert result["level"] == "warn"
        assert result["fields"]["status"] == 404

    def test_nginx_500_is_error(self):
        line = '10.0.0.1 - - [20/Jun/2026:12:00:00 +0000] "POST /api/order HTTP/1.1" 500 0 "-" "python-requests/2.28"'
        result = self.parser.parse(line)
        assert result is not None
        assert result["level"] == "error"
        assert result["fields"]["status"] == 500

    def test_corrupt_nginx_line(self):
        line = 'this is not an nginx log line'
        result = self.parser.parse(line)
        assert result is None, "corrupt nginx line should return None"

    def test_malformed_nginx_timestamp(self):
        line = '192.168.1.1 - - [BAD-DATE] "GET / HTTP/1.1" 200 123 "-" "-"'
        result = self.parser.parse(line)
        assert result is not None
        assert result["timestamp"] is None, "unparseable timestamp should be None"

    def test_empty_nginx_line(self):
        result = self.parser.parse("")
        assert result is None
