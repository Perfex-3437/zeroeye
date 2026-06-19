#!/usr/bin/env python3
"""Mock health check server for testing retry behavior.

Usage:
    python3 tests/mock_health_server.py [--port PORT] [--fail N]
    
    --fail N makes the first N requests fail with 500, then succeed.
    Default: --fail 2 (fails twice, succeeds on 3rd attempt)
"""

import argparse
import json
from http.server import HTTPServer, BaseHTTPRequestHandler


class MockHealthHandler(BaseHTTPRequestHandler):
    fail_count = 0
    request_num = 0

    def do_GET(self):
        type(self).request_num += 1
        if self.request_num <= type(self).fail_count:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "error",
                "error": f"Simulated failure {self.request_num}/{type(self).fail_count}",
            }).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "service": "mock",
            }).encode())

    def log_message(self, format, *args):
        print(f"  [{self.request_num}] {args[0]} {args[1]} {args[2]}")


def main():
    parser = argparse.ArgumentParser(description="Mock health check server")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--fail", type=int, default=2)
    args = parser.parse_args()

    MockHealthHandler.fail_count = args.fail
    MockHealthHandler.request_num = 0

    server = HTTPServer(("", args.port), MockHealthHandler)
    print(f"Mock health server on :{args.port}, failing {args.fail} times then succeeding")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
        server.server_close()


if __name__ == "__main__":
    main()
