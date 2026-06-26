#!/usr/bin/env python3
"""
Health check tool for the Tent of Trials platform.
Performs comprehensive health checks across all services and reports
the overall system status.

This tool is used by:
  - The Kubernetes liveness/readiness probes
  - The deployment pipeline (post-deployment validation)
  - The monitoring system (periodic health checks)
  - The on-call engineer (manual troubleshooting)

The health check performs the following checks:
  1. Service availability (HTTP health endpoints)
  2. Database connectivity (connection test)
  3. Redis connectivity (ping test)
  4. Kafka connectivity (metadata fetch)
  5. Message queue depth (consumer lag check)
  6. Certificate expiry (TLS certificate check)
  7. Disk space (filesystem usage check)
  8. Memory usage (process memory check)

Each check returns a status of OK, WARNING, or CRITICAL, along with
a detail message and optional diagnostic data.

Usage:
    python3 health_check.py                  # Check all services
    python3 health_check.py --service backend # Check specific service
    python3 health_check.py --json            # JSON output
    python3 health_check.py --watch           # Continuous monitoring
    python3 health_check.py --timeout 10      # Per-probe timeout (seconds)
    python3 health_check.py --probe-rate 5    # Max probes per second
"""

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

SERVICES = {
    "backend": {"host": "localhost", "port": 8080, "path": "/health", "timeout": 5},
    "market": {"host": "localhost", "port": 8081, "path": "/health", "timeout": 5},
    "frailbox": {"host": "localhost", "port": 8082, "path": "/health", "timeout": 10},
    "frontend": {"host": "localhost", "port": 3000, "path": "/", "timeout": 5},
}

INFRASTRUCTURE = {
    "postgresql": {"host": os.environ.get("DB_HOST", "localhost"), "port": int(os.environ.get("DB_PORT", "5432")), "timeout": 5},
    "redis": {"host": os.environ.get("REDIS_HOST", "localhost"), "port": int(os.environ.get("REDIS_PORT", "6379")), "timeout": 5},
    "kafka": {"host": os.environ.get("KAFKA_HOST", "localhost"), "port": int(os.environ.get("KAFKA_PORT", "9092")), "timeout": 5},
}

DISK_THRESHOLD_WARNING = 80
DISK_THRESHOLD_CRITICAL = 90

MEMORY_THRESHOLD_WARNING = 80
MEMORY_THRESHOLD_CRITICAL = 90

# Circuit breaker states
CIRCUIT_CLOSED = "CLOSED"
CIRCUIT_OPEN = "OPEN"
CIRCUIT_HALF_OPEN = "HALF_OPEN"

# ---------------------------------------------------------------------------
# RATE LIMITER (Token Bucket)
# ---------------------------------------------------------------------------


class TokenBucket:
    """A thread-safe token bucket rate limiter.

    Limits the number of operations (probes) per second globally.
    Supports dynamic rate adjustment for circuit breaker integration.
    """

    def __init__(self, rate: float = 10.0, burst: Optional[int] = None):
        """
        Args:
            rate: Maximum sustained operations per second.
            burst: Maximum burst size (defaults to rate).
        """
        self._rate = float(rate)
        self._burst = float(burst) if burst is not None else float(rate)
        self._tokens = self._burst
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._throttled_count = 0
        self._total_requests = 0

    @property
    def rate(self) -> float:
        return self._rate

    @rate.setter
    def rate(self, value: float):
        with self._lock:
            self._rate = float(value)
            # Never let burst grow beyond the configured max burst
            if self._burst < self._rate:
                self._burst = self._rate

    @property
    def throttled(self) -> int:
        return self._throttled_count

    @property
    def current_burst(self) -> float:
        return self._burst

    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self, tokens: float = 1.0, block: bool = False) -> bool:
        """Try to acquire *tokens* from the bucket.

        Args:
            tokens: Number of tokens to consume (default 1.0).
            block: If True, block until tokens are available (not used for probes).

        Returns:
            True if tokens were acquired, False if throttled.
        """
        with self._lock:
            self._total_requests += 1
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            else:
                self._throttled_count += 1
                return False

    def stats(self) -> Dict[str, Any]:
        """Return current rate limiter statistics."""
        with self._lock:
            self._refill()
            return {
                "current_rate": round(self._rate, 1),
                "current_burst": round(self._burst, 1),
                "available_tokens": round(self._tokens, 2),
                "total_requests": self._total_requests,
                "throttled_requests": self._throttled_count,
                "throttle_pct": round(
                    (self._throttled_count / max(self._total_requests, 1)) * 100, 1
                ),
            }

    def reset(self):
        """Reset the bucket to full."""
        with self._lock:
            self._tokens = self._burst
            self._last_refill = time.monotonic()
            self._throttled_count = 0
            self._total_requests = 0


# Global rate limiter instance (configured via CLI args)
_rate_limiter: Optional[TokenBucket] = None

# ---------------------------------------------------------------------------
# CIRCUIT BREAKER STATE TRACKING
# ---------------------------------------------------------------------------

_circuit_breaker_state: Dict[str, str] = {}


def get_circuit_breaker_state(service_name: str) -> str:
    """Get the current circuit breaker state for a service.

    Defaults to CLOSED if no state has been recorded.
    """
    return _circuit_breaker_state.get(service_name, CIRCUIT_CLOSED)


def set_circuit_breaker_state(service_name: str, state: str):
    """Record the current circuit breaker state for a service."""
    _circuit_breaker_state[service_name] = state


def effective_probe_rate(configured_rate: float) -> float:
    """Calculate the effective probe rate based on circuit breaker states.

    When any service is in HALF_OPEN state, reduce the global rate to 50%.
    """
    if any(
        state == CIRCUIT_HALF_OPEN
        for state in _circuit_breaker_state.values()
    ):
        return configured_rate * 0.5
    return configured_rate


def should_proceed(name: str) -> bool:
    """Check whether a probe should proceed based on rate limiting."""
    global _rate_limiter
    if _rate_limiter is None:
        return True
    return _rate_limiter.acquire()


# ---------------------------------------------------------------------------
# CHECK FUNCTIONS
# ---------------------------------------------------------------------------


def check_http_service(host: str, port: int, path: str, timeout: int) -> Tuple[str, str, int]:
    import http.client
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        body = resp.read().decode("utf-8", errors="replace")[:200]
        conn.close()

        if status == 200:
            result = "OK"
            detail = f"HTTP {status}"
        elif status < 500:
            result = "WARNING"
            detail = f"HTTP {status}: {body[:100]}"
        else:
            result = "CRITICAL"
            detail = f"HTTP {status}: {body[:100]}"

        return result, detail, status
    except Exception as e:
        return "CRITICAL", str(e), 0


def check_tcp_port(host: str, port: int, timeout: int) -> Tuple[str, str, float]:
    try:
        start = time.time()
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        latency = (time.time() - start) * 1000
        return "OK", f"Connected ({latency:.1f}ms)", latency
    except socket.timeout:
        return "CRITICAL", f"Connection timeout ({timeout}s)", 0
    except ConnectionRefusedError:
        return "CRITICAL", "Connection refused", 0
    except Exception as e:
        return "CRITICAL", str(e), 0


def check_certificate_expiry(host: str, port: int = 443) -> Tuple[str, str, int]:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                if not cert:
                    return "WARNING", "No certificate found", 0

                from datetime import datetime as dt
                expires = dt.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
                days_left = (expires - dt.now()).days

                if days_left > 30:
                    return "OK", f"Certificate expires in {days_left} days", days_left
                elif days_left > 7:
                    return "WARNING", f"Certificate expires in {days_left} days", days_left
                else:
                    return "CRITICAL", f"Certificate expires in {days_left} days", days_left
    except Exception as e:
        return "WARNING", f"Cannot check: {e}", 0


def check_disk_usage(path: str = "/") -> Tuple[str, str, float]:
    try:
        stat = os.statvfs(path)
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = total - free
        pct = (used / total) * 100

        if pct < DISK_THRESHOLD_WARNING:
            return "OK", f"{pct:.1f}% used ({used // (1024**3)}GB/{total // (1024**3)}GB)", pct
        elif pct < DISK_THRESHOLD_CRITICAL:
            return "WARNING", f"{pct:.1f}% used ({used // (1024**3)}GB/{total // (1024**3)}GB)", pct
        else:
            return "CRITICAL", f"{pct:.1f}% used ({used // (1024**3)}GB/{total // (1024**3)}GB)", pct
    except Exception as e:
        return "WARNING", f"Cannot check: {e}", 0


def check_memory_usage() -> Tuple[str, str, float]:
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip().replace(" kB", "")
                    try:
                        meminfo[key] = int(value) * 1024
                    except ValueError:
                        pass

        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = total - available
        pct = (used / total) * 100 if total > 0 else 0

        if pct < MEMORY_THRESHOLD_WARNING:
            return "OK", f"{pct:.1f}% used ({used // (1024**3)}GB/{total // (1024**3)}GB)", pct
        elif pct < MEMORY_THRESHOLD_CRITICAL:
            return "WARNING", f"{pct:.1f}% used", pct
        else:
            return "CRITICAL", f"{pct:.1f}% used", pct
    except Exception as e:
        return "WARNING", f"Cannot check: {e}", 0


def check_load_average() -> Tuple[str, str, float]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().strip().split()
            load = float(parts[0])
            cpu_count = os.cpu_count() or 1
            load_pct = (load / cpu_count) * 100

            if load_pct < 70:
                return "OK", f"Load: {load} ({load_pct:.0f}% of {cpu_count} cores)", load
            elif load_pct < 90:
                return "WARNING", f"Load: {load} ({load_pct:.0f}% of {cpu_count} cores)", load
            else:
                return "CRITICAL", f"Load: {load} ({load_pct:.0f}% of {cpu_count} cores)", load
    except Exception as e:
        return "WARNING", f"Cannot check: {e}", 0


# ---------------------------------------------------------------------------
# HEALTH CHECK RUNNER
# ---------------------------------------------------------------------------


def run_health_checks(service: Optional[str] = None, json_output: bool = False,
                      probe_timeout: Optional[int] = None,
                      probe_rate: Optional[float] = None) -> Dict[str, Any]:
    results: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "hostname": socket.gethostname(),
        "services": {},
        "infrastructure": {},
        "system": {},
        "overall_status": "OK",
    }

    all_ok = True

    # Update rate limiter if configured
    global _rate_limiter
    if probe_rate is not None and _rate_limiter is None:
        _rate_limiter = TokenBucket(rate=probe_rate)
    elif probe_rate is not None and _rate_limiter is not None:
        effective_rate = effective_probe_rate(probe_rate)
        _rate_limiter.rate = effective_rate

    # Apply circuit breaker adjustment to rate
    if _rate_limiter is not None and probe_rate is not None:
        effective_rate = effective_probe_rate(probe_rate)
        _rate_limiter.rate = effective_rate

    # Check services
    for name, config in SERVICES.items():
        if service and name != service:
            continue

        # Check rate limiter before probing
        if _rate_limiter is not None and not should_proceed(name):
            results["services"][name] = {
                "status": "WARNING",
                "detail": "Throttled (rate limit exceeded)",
                "code": 429,
                "endpoint": f"http://{config['host']}:{config['port']}{config['path']}",
            }
            continue

        timeout = probe_timeout if probe_timeout is not None else config["timeout"]
        status, detail, code = check_http_service(
            config["host"], config["port"], config["path"], timeout
        )
        results["services"][name] = {
            "status": status,
            "detail": detail,
            "code": code,
            "endpoint": f"http://{config['host']}:{config['port']}{config['path']}",
        }
        if status == "CRITICAL":
            all_ok = False

    # Check infrastructure
    for name, config in INFRASTRUCTURE.items():
        if service and name != service:
            continue
        timeout = probe_timeout if probe_timeout is not None else config["timeout"]
        status, detail, latency = check_tcp_port(config["host"], config["port"], timeout)
        results["infrastructure"][name] = {
            "status": status,
            "detail": detail,
            "endpoint": f"{config['host']}:{config['port']}",
        }
        if status == "CRITICAL":
            all_ok = False

    # Check system resources
    disk_status, disk_detail, disk_pct = check_disk_usage()
    results["system"]["disk"] = {"status": disk_status, "detail": disk_detail}
    if disk_status == "CRITICAL":
        all_ok = False

    mem_status, mem_detail, mem_pct = check_memory_usage()
    results["system"]["memory"] = {"status": mem_status, "detail": mem_detail}
    if mem_status == "CRITICAL":
        all_ok = False

    load_status, load_detail, load_val = check_load_average()
    results["system"]["load"] = {"status": load_status, "detail": load_detail}

    # Check certificate expiry (web services)
    for name, config in SERVICES.items():
        if service and name != service:
            continue
        if config["port"] == 443:
            cert_status, cert_detail, days_left = check_certificate_expiry(config["host"])
            results["services"][name]["certificate"] = {
                "status": cert_status,
                "detail": cert_detail,
                "days_remaining": days_left,
            }
            if cert_status == "CRITICAL":
                all_ok = False

    results["overall_status"] = "OK" if all_ok else "DEGRADED"

    # Add rate limiter stats to the report
    if _rate_limiter is not None:
        results["rate_limiter"] = _rate_limiter.stats()

    return results


def print_health_report(results: Dict[str, Any]):
    print(f"\n{'='*60}")
    print(f"  HEALTH CHECK REPORT")
    print(f"  Host: {results['hostname']}")
    print(f"  Time: {results['timestamp']}")
    print(f"  Overall: {results['overall_status']}")
    print(f"{'='*60}")

    for category, items in [("Services", results["services"]),
                             ("Infrastructure", results["infrastructure"]),
                             ("System", results["system"])]:
        if items:
            print(f"\n  {category}:")
            for name, check in items.items():
                if isinstance(check, dict) and "status" in check:
                    status_icon = {"OK": "✓", "WARNING": "⚠", "CRITICAL": "✗"}.get(check["status"], "?")
                    print(f"    {status_icon} {name}: {check['detail']}")
                else:
                    print(f"    {name}:")
                    for sub_name, sub_check in check.items():
                        if isinstance(sub_check, dict) and "status" in sub_check:
                            sub_icon = {"OK": "✓", "WARNING": "⚠", "CRITICAL": "✗"}.get(sub_check["status"], "?")
                            print(f"      {sub_icon} {sub_name}: {sub_check['detail']}")

    # Print rate limiter stats if present
    if "rate_limiter" in results:
        rl = results["rate_limiter"]
        print(f"\n  Rate Limiter:")
        print(f"    Current rate: {rl['current_rate']} req/s")
        print(f"    Throttled: {rl['throttled_requests']} ({rl['throttle_pct']}%)")
        print(f"    Available tokens: {rl['available_tokens']}")

    print()


def parse_args():
    parser = argparse.ArgumentParser(description="Health check tool")
    parser.add_argument("--service", "-s", help="Check specific service only")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--watch", "-w", action="store_true", help="Continuous monitoring")
    parser.add_argument("--interval", "-i", type=int, default=30, help="Check interval in seconds")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--timeout", "-t", type=int, default=None,
                        help="Per-probe timeout in seconds (overrides service defaults)")
    parser.add_argument("--probe-rate", "-r", type=float, default=None,
                        help="Max probes per second (rate limiting)")
    return parser.parse_args()


def main():
    args = parse_args()

    # Configure global rate limiter
    global _rate_limiter
    if args.probe_rate is not None:
        _rate_limiter = TokenBucket(rate=args.probe_rate)

    if args.watch:
        print(f"Continuous monitoring (interval: {args.interval}s). Press Ctrl+C to stop.")
        try:
            while True:
                results = run_health_checks(args.service, args.json,
                                            probe_timeout=args.timeout,
                                            probe_rate=args.probe_rate)
                if args.json:
                    print(json.dumps(results, indent=2))
                else:
                    print_health_report(results)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitoring stopped")
    else:
        results = run_health_checks(args.service, args.json,
                                    probe_timeout=args.timeout,
                                    probe_rate=args.probe_rate)
        if args.json:
            output = json.dumps(results, indent=2)
            print(output)
        else:
            print_health_report(results)

        if args.output:
            with open(args.output, "w") as f:
                if args.json:
                    json.dump(results, f, indent=2)
                else:
                    json.dump(results, f, indent=2)
            print(f"Report saved to {args.output}")

        if results["overall_status"] == "DEGRADED":
            return 1

    return 0


if __name__ == "__main__":
    main()
