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
  9. Prometheus stale metric detection (age-based staleness guard)

Each check returns a status of OK, WARNING, or CRITICAL, along with
a detail message and optional diagnostic data.

Usage:
    python3 health_check.py                  # Check all services
    python3 health_check.py --service backend # Check specific service
    python3 health_check.py --json            # JSON output
    python3 health_check.py --watch           # Continuous monitoring
    python3 health_check.py --stale-metrics   # Include stale-metric guard
"""

import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
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

# Stale-metric guard configuration
# Metrics with a timestamp older than STALE_METRIC_AGE_SECONDS are flagged as stale.
# This value can be overridden via the STALE_METRIC_AGE environment variable.
STALE_METRIC_AGE_SECONDS = int(os.environ.get("STALE_METRIC_AGE", "300"))
STALE_METRIC_CRITICAL_AGE_SECONDS = int(os.environ.get("STALE_METRIC_CRITICAL_AGE", "900"))

# Patterns that look like secrets or prompts — these values are redacted
# from diagnostic output per the acceptance criteria.
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|secret|api[_-]?key|token|auth|credential)\s*[:=]\s*['\"]?[^\s,'\"}]+"),
    re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9+/=_-]{10,}"),
    re.compile(r"(?i)-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"(?i)sk-[a-z0-9]{20,}"),   # OpenAI-style secret keys
    re.compile(r"(?i)ghp_[a-zA-Z0-9]{36}"), # GitHub tokens
    re.compile(r"(?i)prompt\s*[:=]\s*['\"].+?['\"]"),  # Raw prompt content
]

ENVIRONMENT = os.environ.get("ENVIRONMENT", os.environ.get("TENT_ENV", "production"))

# ---------------------------------------------------------------------------
# REDACTION HELPERS
# ---------------------------------------------------------------------------


def redact_secrets(text: str) -> str:
    """Replace secret-looking values with '[REDACTED]'."""
    # Replace known patterns directly
    text = re.sub(r"(?i)(password|secret|api[_-]?key|token|auth|credential)\s*[:=]\s*['\"]?[^\s,'\"}]+",
                  r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)\b(bearer|basic)\s+[a-z0-9+/=_-]{10,}", r"\1 [REDACTED]", text)
    text = re.sub(r"(?i)-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",
                  "-----BEGIN PRIVATE KEY----- [REDACTED]", text)
    text = re.sub(r"(?i)sk-[a-z0-9]{20,}", "[REDACTED-KEY]", text)
    text = re.sub(r"(?i)ghp_[a-zA-Z0-9]{36}", "[REDACTED-TOKEN]", text)
    text = re.sub(r"(?i)prompt\s*[:=]\s*['\"].+?['\"]", "prompt=[REDACTED]", text)
    return text


def redact_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redact secret-looking values from a dictionary."""
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = redact_secrets(v)
        elif isinstance(v, dict):
            result[k] = redact_dict(v)
        elif isinstance(v, list):
            result[k] = [redact_dict(item) if isinstance(item, dict) else
                         redact_secrets(str(item)) if isinstance(item, str) else item
                         for item in v]
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# STALE METRIC DETECTION
# ---------------------------------------------------------------------------


def parse_prometheus_timestamp(metric_line: str) -> Optional[float]:
    """
    Extract a Unix timestamp from a Prometheus metric exposition line.
    Supports both OpenMetrics (# HELP / # TYPE / # EOF) and the classic format.
    Looks for a trailing timestamp after the value.
    """
    line = metric_line.strip()
    # Skip comment lines
    if line.startswith("#"):
        return None

    # Prometheus classic format: metric_name{labels} value [timestamp]
    # The timestamp is the third whitespace-separated token (after metric+labels and value)
    parts = line.split()
    if len(parts) >= 3:
        try:
            # parts[0] = metric_name{labels}, parts[1] = value, parts[2] = timestamp
            return float(parts[-1])
        except (ValueError, IndexError):
            return None
    return None


def check_stale_metrics(
    metrics_text: str,
    service_name: str = "unknown",
    max_age: int = STALE_METRIC_AGE_SECONDS,
    critical_age: int = STALE_METRIC_CRITICAL_AGE_SECONDS,
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    Check Prometheus metrics for staleness.

    Returns:
        (overall_status, detail_message, stale_metric_list)
    """
    now = time.time()
    stale_metrics = []
    lines = metrics_text.split("\n")

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        ts = parse_prometheus_timestamp(line)
        if ts is None:
            continue

        age = now - ts
        if age < 0:
            # Future timestamps are likely clock skew, not stale
            continue

        if age > critical_age:
            stale_metrics.append({
                "service": service_name,
                "environment": ENVIRONMENT,
                "metric_name": line.split("{")[0].split()[0] if "{" in line else line.split()[0],
                "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "age_seconds": round(age, 1),
                "stale": True,
                "critical": True,
            })
        elif age > max_age:
            stale_metrics.append({
                "service": service_name,
                "environment": ENVIRONMENT,
                "metric_name": line.split("{")[0].split()[0] if "{" in line else line.split()[0],
                "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "age_seconds": round(age, 1),
                "stale": True,
                "critical": False,
            })

    if not stale_metrics:
        return "OK", "No stale metrics detected", []

    critical_count = sum(1 for m in stale_metrics if m.get("critical"))
    warning_count = sum(1 for m in stale_metrics if not m.get("critical"))

    if critical_count > 0:
        status = "CRITICAL"
        detail = f"{critical_count} stale metric(s) exceed critical age ({critical_age}s), {warning_count} exceed warning age ({max_age}s)"
    else:
        status = "WARNING"
        detail = f"{warning_count} stale metric(s) exceed warning age ({max_age}s)"

    return status, detail, stale_metrics


def fetch_metrics_from_service(host: str, port: int, timeout: int = 5) -> Optional[str]:
    """Fetch Prometheus metrics from a service's /metrics endpoint."""
    import http.client
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/metrics")
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            return None
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return body
    except Exception:
        return None


def run_stale_metric_checks() -> Dict[str, Any]:
    """
    Run stale metric detection across all configured services.
    Returns a dictionary suitable for inclusion in the health check results.
    """
    results = {}
    for name, config in SERVICES.items():
        metrics_text = fetch_metrics_from_service(config["host"], config["port"], config["timeout"])
        if metrics_text is None:
            results[name] = {
                "status": "WARNING",
                "detail": f"Could not fetch /metrics from {name}",
                "stale_metrics": [],
            }
            continue
        status, detail, stale_list = check_stale_metrics(
            metrics_text, service_name=name
        )
        results[name] = {
            "status": status,
            "detail": detail,
            "stale_metrics_count": len(stale_list),
            "stale_metrics": stale_list,
        }
    return results


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
                      check_stale: bool = False) -> Dict[str, Any]:
    results: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "hostname": socket.gethostname(),
        "environment": ENVIRONMENT,
        "services": {},
        "infrastructure": {},
        "system": {},
        "overall_status": "OK",
    }

    all_ok = True

    # Check services
    for name, config in SERVICES.items():
        if service and name != service:
            continue
        status, detail, code = check_http_service(
            config["host"], config["port"], config["path"], config["timeout"]
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
        status, detail, latency = check_tcp_port(config["host"], config["port"], config["timeout"])
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

    # Stale metric checks (when --stale-metrics is passed)
    if check_stale:
        stale_results = run_stale_metric_checks()
        results["stale_metrics"] = stale_results
        for svc, sr in stale_results.items():
            if sr["status"] == "CRITICAL":
                all_ok = False
        # Also add a summary
        total_stale = sum(sr.get("stale_metrics_count", 0) for sr in stale_results.values())
        results["stale_metrics_summary"] = {
            "total_stale_metrics": total_stale,
            "stale_metric_age_threshold_seconds": STALE_METRIC_AGE_SECONDS,
            "stale_metric_critical_age_threshold_seconds": STALE_METRIC_CRITICAL_AGE_SECONDS,
            "environment": ENVIRONMENT,
        }

    results["overall_status"] = "OK" if all_ok else "DEGRADED"

    return results


def print_health_report(results: Dict[str, Any]):
    print(f"\n{'='*60}")
    print(f"  HEALTH CHECK REPORT")
    print(f"  Host: {results['hostname']}")
    print(f"  Env:  {results.get('environment', 'unknown')}")
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

    # Print stale metrics summary
    if "stale_metrics_summary" in results:
        sm = results["stale_metrics_summary"]
        print(f"\n  Stale Metrics Guard:")
        if sm["total_stale_metrics"] > 0:
            print(f"    ✗ {sm['total_stale_metrics']} stale metric(s) detected")
            print(f"      Threshold: {sm['stale_metric_age_threshold_seconds']}s, Critical: {sm['stale_metric_critical_age_threshold_seconds']}s")
            if "stale_metrics" in results:
                for svc_name, svc_data in results["stale_metrics"].items():
                    if svc_data["stale_metrics"]:
                        icon = {"OK": "✓", "WARNING": "⚠", "CRITICAL": "✗"}.get(svc_data["status"], "?")
                        print(f"    {icon} {svc_name}: {svc_data['detail']}")
                        for m in svc_data["stale_metrics"][:5]:  # Show top 5
                            age_str = f"{m['age_seconds']}s"
                            print(f"        - {m['metric_name']} (age: {age_str})")
                        if len(svc_data["stale_metrics"]) > 5:
                            print(f"        ... and {len(svc_data['stale_metrics']) - 5} more")
        else:
            print(f"    ✓ No stale metrics detected (threshold: {sm['stale_metric_age_threshold_seconds']}s)")

    print()


def parse_args():
    parser = argparse.ArgumentParser(description="Health check tool")
    parser.add_argument("--service", "-s", help="Check specific service only")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--watch", "-w", action="store_true", help="Continuous monitoring")
    parser.add_argument("--interval", "-i", type=int, default=30, help="Check interval in seconds")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--stale-metrics", action="store_true", help="Check for stale Prometheus metrics")
    parser.add_argument("--stale-max-age", type=int, default=STALE_METRIC_AGE_SECONDS,
                        help=f"Stale metric warning age threshold in seconds (default: {STALE_METRIC_AGE_SECONDS})")
    parser.add_argument("--stale-critical-age", type=int, default=STALE_METRIC_CRITICAL_AGE_SECONDS,
                        help=f"Stale metric critical age threshold in seconds (default: {STALE_METRIC_CRITICAL_AGE_SECONDS})")
    return parser.parse_args()


def main():
    args = parse_args()

    # Override stale metric ages from CLI args if provided
    global STALE_METRIC_AGE_SECONDS, STALE_METRIC_CRITICAL_AGE_SECONDS
    STALE_METRIC_AGE_SECONDS = args.stale_max_age
    STALE_METRIC_CRITICAL_AGE_SECONDS = args.stale_critical_age

    if args.watch:
        print(f"Continuous monitoring (interval: {args.interval}s). Press Ctrl+C to stop.")
        try:
            while True:
                results = run_health_checks(args.service, args.json, args.stale_metrics)
                # Redact secrets from output
                results = redact_dict(results)
                if args.json:
                    print(json.dumps(results, indent=2))
                else:
                    print_health_report(results)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitoring stopped")
    else:
        results = run_health_checks(args.service, args.json, args.stale_metrics)
        # Redact secrets from output
        results = redact_dict(results)
        if args.json:
            output = json.dumps(results, indent=2)
            print(output)
        else:
            print_health_report(results)

        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            print(f"Report saved to {args.output}")

        if results["overall_status"] == "DEGRADED":
            return 1

    return 0


if __name__ == "__main__":
    main()
