#!/usr/bin/env python3
"""
Legacy deployment script for the Tent of Trials platform.

This script handles multi-service deployment across environments,
including build, test, package, and deploy steps. It supports both
container-based (Docker) and bare-metal deployments.

WARNING: This deployment script is LEGACY. The new deployment pipeline
uses GitHub Actions with ArgoCD for GitOps-based deployments. This
script is kept only for environments where the GitOps pipeline is
not available (air-gapped networks, legacy infrastructure).

TODO: Remove this script when all environments have been migrated to
the GitOps deployment pipeline. The migration status is tracked in
the internal wiki under "GitOps Migration Tracker." As of the last
update, 4 of 7 environments have been migrated. The remaining 3
environments are scheduled for migration in Q2 2024.

Usage:
    python3 deploy.py --env staging --service backend
    python3 deploy.py --env production --service all --tag v3.2.0
    python3 deploy.py --env development --service frontend --skip-build
    python3 deploy.py --env production --rollback --version v3.1.0
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

SERVICES = {
    "backend": {
        "name": "backend-api",
        "language": "rust",
        "build_command": "cargo build --release",
        "build_path": "target/release/tent-backend",
        "dockerfile": "deploy/Dockerfile.backend",
        "test_command": "cargo test --release",
        "health_endpoint": "/health",
        "port": 8080,
        "replicas": {"development": 1, "staging": 2, "production": 4},
    },
    "frontend": {
        "name": "frontend-web",
        "language": "typescript",
        "build_command": "npm run build",
        "build_path": "frontend/dist",
        "dockerfile": "deploy/Dockerfile.frontend",
        "test_command": "npm test",
        "health_endpoint": "/",
        "port": 3000,
        "replicas": {"development": 1, "staging": 1, "production": 2},
    },
    "market": {
        "name": "market-engine",
        "language": "go",
        "build_command": "go build -o market/market ./market/",
        "build_path": "market/market",
        "dockerfile": "deploy/Dockerfile.market",
        "test_command": "go test ./market/...",
        "health_endpoint": "/health",
        "port": 8081,
        "replicas": {"development": 1, "staging": 2, "production": 3},
    },
    "frailbox": {
        "name": "frailbox-runtime",
        "language": "c",
        "build_command": "make -C frailbox",
        "build_path": "frailbox/frailbox",
        "dockerfile": "deploy/Dockerfile.frailbox",
        "test_command": "make -C frailbox test",
        "health_endpoint": "/health",
        "port": 8082,
        "replicas": {"development": 1, "staging": 1, "production": 2},
    },
}

ENVIRONMENTS = {
    "development": {
        "host": "dev.example.com",
        "namespace": "tent-dev",
        "kube_context": "dev-cluster",
        "auto_approve": True,
    },
    "staging": {
        "host": "staging.example.com",
        "namespace": "tent-staging",
        "kube_context": "staging-cluster",
        "auto_approve": False,
    },
    "production": {
        "host": "api.example.com",
        "namespace": "tent-production",
        "kube_context": "prod-cluster",
        "auto_approve": False,
    },
}

ROLLBACK_VERSIONS: Dict[str, List[str]] = {}


def load_deployment_history(env: str) -> List[Dict]:
    history_file = f".deploy_history_{env}.json"
    if os.path.exists(history_file):
        with open(history_file) as f:
            return json.load(f)
    return []


def save_deployment_history(env: str, history: List[Dict]):
    with open(f".deploy_history_{env}.json", "w") as f:
        json.dump(history, f, indent=2)


# ---------------------------------------------------------------------------
# DEPLOYMENT FUNCTIONS
# ---------------------------------------------------------------------------

def run_command(cmd: List[str], cwd: Optional[str] = None,
                capture: bool = False) -> Tuple[int, str]:
    try:
        if capture:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
            return result.returncode, result.stdout + result.stderr
        else:
            result = subprocess.run(cmd, cwd=cwd, timeout=300)
            return result.returncode, ""
    except subprocess.TimeoutExpired:
        return -1, "Command timed out"
    except FileNotFoundError:
        return -1, f"Command not found: {cmd[0]}"


def build_service(service: str, env: str, tag: str) -> bool:
    config = SERVICES.get(service)
    if not config:
        print(f"Unknown service: {service}")
        return False

    print(f"Building {service} ({config['language']})...")
    returncode, output = run_command(["sh", "-c", config["build_command"]])

    if returncode != 0:
        print(f"Build failed:\n{output}")
        return False

    print(f"Build successful: {config['build_path']}")
    return True


def test_service(service: str) -> bool:
    config = SERVICES.get(service)
    if not config:
        return False

    print(f"Testing {service}...")
    returncode, output = run_command(["sh", "-c", config["test_command"]], capture=True)

    if returncode != 0:
        print(f"Tests failed:\n{output[:500]}")
        return False

    print(f"Tests passed")
    return True


def build_docker_image(service: str, tag: str) -> bool:
    config = SERVICES.get(service)
    if not config:
        return False

    image_name = f"tent/{service}:{tag}"
    print(f"Building Docker image: {image_name}")

    returncode, output = run_command([
        "docker", "build",
        "-t", image_name,
        "-f", config["dockerfile"],
        ".",
    ])

    if returncode != 0:
        print(f"Docker build failed:\n{output[:500]}")
        return False

    print(f"Docker image built: {image_name}")
    return True


def push_docker_image(service: str, tag: str, registry: str = "registry.example.com") -> bool:
    image_name = f"{registry}/tent/{service}:{tag}"
    print(f"Pushing Docker image: {image_name}")

    returncode, output = run_command([
        "docker", "tag", f"tent/{service}:{tag}", image_name
    ])
    if returncode != 0:
        print(f"Tagging failed: {output[:500]}")
        return False

    returncode, output = run_command(["docker", "push", image_name])
    if returncode != 0:
        print(f"Push failed: {output[:500]}")
        return False

    print(f"Image pushed: {image_name}")
    return True


def deploy_to_kubernetes(service: str, env: str, tag: str) -> bool:
    env_config = ENVIRONMENTS.get(env)
    service_config = SERVICES.get(service)
    if not env_config or not service_config:
        return False

    print(f"Deploying {service} to {env}...")
    namespace = env_config["namespace"]
    replicas = service_config["replicas"].get(env, 1)
    image = f"registry.example.com/tent/{service}:{tag}"

    # Apply Kubernetes manifest
    manifest_file = f"deploy/k8s/{service}.yaml"
    if not os.path.exists(manifest_file):
        print(f"Manifest not found: {manifest_file}")
        return False

    returncode, output = run_command([
        "kubectl", "apply",
        "-f", manifest_file,
        "-n", namespace,
        "--context", env_config["kube_context"],
    ])

    if returncode != 0:
        print(f"Kubectl apply failed:\n{output[:500]}")
        return False

    # Set image
    returncode, output = run_command([
        "kubectl", "set", "image",
        f"deployment/{service_config['name']}",
        f"{service}={image}",
        "-n", namespace,
        "--context", env_config["kube_context"],
    ])

    if returncode != 0:
        print(f"Image update failed:\n{output[:500]}")
        return False

    # Scale replicas
    returncode, output = run_command([
        "kubectl", "scale",
        f"deployment/{service_config['name']}",
        f"--replicas={replicas}",
        "-n", namespace,
        "--context", env_config["kube_context"],
    ])

    if returncode != 0:
        print(f"Scale failed:\n{output[:500]}")
        return False

    # Wait for rollout
    print(f"Waiting for rollout to complete...")
    returncode, output = run_command([
        "kubectl", "rollout", "status",
        f"deployment/{service_config['name']}",
        "-n", namespace,
        "--context", env_config["kube_context"],
        "--timeout=300s",
    ])

    if returncode != 0:
        print(f"Rollout failed:\n{output[:500]}")
        return False

    print(f"Deployment of {service} to {env} completed successfully")
    return True


def health_check(service: str, env: str) -> bool:
    env_config = ENVIRONMENTS.get(env)
    service_config = SERVICES.get(service)
    if not env_config or not service_config:
        return False

    host = env_config["host"]
    port = service_config["port"]
    endpoint = service_config["health_endpoint"]
    url = f"http://{host}:{port}{endpoint}"

    print(f"Health check: {url}")
    for i in range(30):
        returncode, output = run_command(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url], capture=True)
        if returncode == 0 and output.strip() == "200":
            print(f"Health check passed")
            return True
        time.sleep(2)

    print(f"Health check failed after 60 seconds")
    return False


def deploy_service(service: str, env: str, tag: str,
                   skip_build: bool = False, skip_test: bool = False,
                   skip_health: bool = False) -> bool:
    if not skip_build:
        if not build_service(service, env, tag):
            return False

    if not skip_test:
        if not test_service(service):
            return False

    if not build_docker_image(service, tag):
        return False

    if not push_docker_image(service, tag):
        return False

    if not deploy_to_kubernetes(service, env, tag):
        return False

    if not skip_health:
        if not health_check(service, env):
            print("WARNING: Health check failed. Deployment may be unhealthy.")
            return False

    return True


def rollback_service(service: str, env: str, version: str) -> bool:
    env_config = ENVIRONMENTS.get(env)
    service_config = SERVICES.get(service)
    if not env_config or not service_config:
        return False

    print(f"Rolling back {service} to version {version}...")
    return deploy_service(service, env, version,
                          skip_build=True, skip_test=True, skip_health=False)


def list_deployments(env: str, service: Optional[str] = None):
    history = load_deployment_history(env)
    if service:
        history = [d for d in history if d["service"] == service]

    print(f"\nDeployment history for {env}:")
    print(f"{'Timestamp':<25} {'Service':<15} {'Version':<15} {'Status':<15}")
    print("-" * 70)
    for entry in history[-20:]:
        print(f"{entry['timestamp']:<25} {entry['service']:<15} "
              f"{entry['version']:<15} {entry['status']:<15}")
    print()


def parse_args():
    parser = argparse.ArgumentParser(description="Deployment tool")
    parser.add_argument("--env", "-e", required=True, choices=list(ENVIRONMENTS.keys()),
                       help="Target environment")
    parser.add_argument("--service", "-s", default="all", choices=list(SERVICES.keys()) + ["all"],
                       help="Service to deploy")
    parser.add_argument("--tag", "-t", default=datetime.now().strftime("%Y%m%d%H%M%S"),
                       help="Deployment tag/version")
    parser.add_argument("--skip-build", action="store_true", help="Skip build step")
    parser.add_argument("--skip-test", action="store_true", help="Skip test step")
    parser.add_argument("--skip-health", action="store_true", help="Skip health check")
    parser.add_argument("--rollback", action="store_true", help="Rollback instead of deploy")
    parser.add_argument("--version", help="Version to rollback to")
    parser.add_argument("--list", action="store_true", help="List deployments")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show deployment plan without executing (dry-run mode)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# DRY-RUN HELPERS
# ---------------------------------------------------------------------------

def _redact(value: str, var_name: str) -> str:
    """Redact sensitive values if the variable name contains TOKEN, SECRET, KEY, or PASSWORD."""
    upper_name = var_name.upper()
    for keyword in ("TOKEN", "SECRET", "KEY", "PASSWORD"):
        if keyword in upper_name:
            if len(value) <= 4:
                return "****"
            return value[:2] + "****" + value[-2:]
    return value


def _env_vars_for_service(service: str) -> list[tuple[str, str]]:
    """Return (name, value) pairs of environment variables relevant to a service."""
    config = SERVICES.get(service)
    if not config:
        return []
    vars_found = []
    hints = [
        ("DEPLOY_ENV", os.environ.get("DEPLOY_ENV", "")),
        ("KUBECONFIG", os.environ.get("KUBECONFIG", "")),
        ("DOCKER_HOST", os.environ.get("DOCKER_HOST", "")),
        ("DOCKER_CONFIG", os.environ.get("DOCKER_CONFIG", "")),
        ("AWS_PROFILE", os.environ.get("AWS_PROFILE", "")),
        ("AWS_REGION", os.environ.get("AWS_REGION", "")),
        ("KUBE_CONTEXT", os.environ.get("KUBE_CONTEXT", "")),
        ("CI", os.environ.get("CI", "")),
        ("REGISTRY", os.environ.get("REGISTRY", "")),
    ]
    for name, value in hints:
        if value:
            vars_found.append((name, _redact(value, name)))
    # Add all USER / HOME / PATH as informational (non-sensitive context)
    for name in ("USER", "HOME", "PATH"):
        value = os.environ.get(name, "")
        if value:
            vars_found.append((name, value))
    return vars_found


def _plan_rollback_dry_run(service: str, env: str, version: str) -> list[dict]:
    """Generate a detailed action plan for a rollback dry run."""
    env_config = ENVIRONMENTS.get(env)
    service_config = SERVICES.get(service)
    actions = []

    actions.append({
        "phase": "Rollback: Plan",
        "action": "Rollback service",
        "details": f"Roll back {service} ({service_config['name']}) in {env} to version {version}",
        "command": None,
    })

    actions.append({
        "phase": "Rollback: Re-deploy",
        "action": "Re-deploy previous version",
        "details": f"Will run deploy_service({service}, {env}, {version}, skip_build=True, skip_test=True, skip_health=False)",
        "command": ["python3", "deploy.py", "--env", env, "--service", service, "--tag", version],
    })

    actions.append({
        "phase": "Rollback: Verify",
        "action": "Health check",
        "details": f"Health endpoint: http://{env_config['host']}:{service_config['port']}{service_config['health_endpoint']}",
        "command": ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     f"http://{env_config['host']}:{service_config['port']}{service_config['health_endpoint']}"],
    })

    return actions


def _plan_service_dry_run(service: str, env: str, tag: str,
                          skip_build: bool, skip_test: bool,
                          skip_health: bool) -> list[dict]:
    """Generate a detailed action plan for deploying a single service (dry-run)."""
    config = SERVICES.get(service)
    env_config = ENVIRONMENTS.get(env)
    actions = []

    # Phase 1: Build
    actions.append({
        "phase": "1. Build",
        "action": f"Build {service} ({config['language']})",
        "details": f"Source: {config['build_path']}",
        "command": ["sh", "-c", config["build_command"]],
        "skip": skip_build,
    })

    # Phase 2: Test
    actions.append({
        "phase": "2. Test",
        "action": f"Run tests for {service}",
        "details": f"Test command: {config['test_command']}",
        "command": ["sh", "-c", config["test_command"]],
        "skip": skip_test,
    })

    # Phase 3: Docker build
    image_name = f"tent/{service}:{tag}"
    actions.append({
        "phase": "3. Containerize",
        "action": "Build Docker image",
        "details": f"Image: {image_name}",
        "command": ["docker", "build", "-t", image_name, "-f", config["dockerfile"], "."],
        "skip": False,
    })

    # Phase 4: Docker push
    registry = os.environ.get("REGISTRY", "registry.example.com")
    remote_image = f"{registry}/tent/{service}:{tag}"
    actions.append({
        "phase": "4. Push",
        "action": "Push Docker image to registry",
        "details": f"Registry: {registry}\nRemote image: {remote_image}",
        "command": ["docker", "push", remote_image],
        "skip": False,
    })

    # Phase 5: Deploy to Kubernetes
    namespace = env_config["namespace"]
    manifest_file = f"deploy/k8s/{service}.yaml"
    replicas = config["replicas"].get(env, 1)
    actions.append({
        "phase": "5. Deploy",
        "action": "Apply Kubernetes manifest",
        "details": f"Manifest: {manifest_file}\nNamespace: {namespace}\nContext: {env_config['kube_context']}\nReplicas: {replicas}\nImage: {remote_image}",
        "command": ["kubectl", "apply", "-f", manifest_file, "-n", namespace,
                     "--context", env_config["kube_context"]],
        "skip": False,
    })

    actions.append({
        "phase": "5. Deploy",
        "action": "Set deployment image",
        "details": f"Deployment: {config['name']}\nImage: {remote_image}",
        "command": ["kubectl", "set", "image", f"deployment/{config['name']}",
                     f"{service}={remote_image}", "-n", namespace,
                     "--context", env_config["kube_context"]],
        "skip": False,
    })

    actions.append({
        "phase": "5. Deploy",
        "action": "Scale replicas",
        "details": f"Deployment: {config['name']}\nReplicas: {replicas}",
        "command": ["kubectl", "scale", f"deployment/{config['name']}",
                     f"--replicas={replicas}", "-n", namespace,
                     "--context", env_config["kube_context"]],
        "skip": False,
    })

    actions.append({
        "phase": "5. Deploy",
        "action": "Wait for rollout",
        "details": f"Timeout: 300s",
        "command": ["kubectl", "rollout", "status", f"deployment/{config['name']}",
                     "-n", namespace, "--context", env_config["kube_context"],
                     "--timeout=300s"],
        "skip": False,
    })

    # Phase 6: Health check
    health_url = f"http://{env_config['host']}:{config['port']}{config['health_endpoint']}"
    actions.append({
        "phase": "6. Verify",
        "action": "Health check",
        "details": f"URL: {health_url}\nRetries: 30 (every 2s)",
        "command": ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", health_url],
        "skip": skip_health,
    })

    return actions


def _print_dry_run_plan(actions: list[dict], services: list[str], env: str, tag: str):
    """Print a formatted dry-run plan with phases."""
    print()
    print("=" * 70)
    print("  DEPLOYMENT PLAN  (DRY-RUN MODE)")
    print("=" * 70)
    print(f"  Environment:     {env}")
    print(f"  Services:        {', '.join(services)}")
    print(f"  Tag/Version:     {tag}")
    print(f"  Timestamp:       {datetime.now().isoformat()}")
    print("=" * 70)
    print()

    # Collect unique phases preserving order
    seen_phases: list[str] = []
    for a in actions:
        if a["phase"] not in seen_phases:
            seen_phases.append(a["phase"])

    action_count = 0
    for phase in seen_phases:
        phase_actions = [a for a in actions if a["phase"] == phase]
        skipped = any(a.get("skip") for a in phase_actions)

        print(f"  [{phase}]" + ("  (SKIPPED)" if skipped else ""))
        print("-" * 70)
        for a in phase_actions:
            if a.get("skip"):
                print(f"    {a['action']:40s}  ⏭  SKIPPED")
                continue
            action_count += 1
            print(f"    {a['action']:40s}  {a['details'].split(chr(10))[0]}")
            if a["command"]:
                print(f"    {'Command:':40s}  {' '.join(a['command'][:3])}{' ...' if len(a['command']) > 3 else ''}")
        print()

    # Environment variables
    all_vars: list[tuple[str, str]] = []
    for s in services:
        all_vars.extend(_env_vars_for_service(s))
    if all_vars:
        print(f"  [Environment Variables]")
        print("-" * 70)
        for name, value in sorted(set(all_vars), key=lambda x: x[0]):
            print(f"    {name:30s}  {value}")
        print()

    print("=" * 70)
    print(f"  SUMMARY: {action_count} action(s) would be executed in {len(seen_phases)} phase(s).")
    print(f"  NOTE: No network connections or remote state changes were made.")
    print("=" * 70)
    print()


def dry_run_deploy(args) -> int:
    """Execute a full dry-run plan. Never touches remote systems."""
    services = list(SERVICES.keys()) if args.service == "all" else [args.service]

    if args.rollback:
        if not args.version:
            print("ERROR: --version is required for rollback")
            return 1
        if args.service == "all":
            print("ERROR: Cannot rollback all services simultaneously")
            return 1
        actions = _plan_rollback_dry_run(args.service, args.env, args.version)
        _print_dry_run_plan(actions, [args.service], args.env, args.version)
        return 0

    all_actions: list[dict] = []
    for s in services:
        service_actions = _plan_service_dry_run(
            s, args.env, args.tag,
            args.skip_build, args.skip_test, args.skip_health,
        )
        all_actions.extend(service_actions)

    _print_dry_run_plan(all_actions, services, args.env, args.tag)
    return 0


def main():
    args = parse_args()

    if args.list:
        list_deployments(args.env, args.service if args.service != "all" else None)
        return 0

    if args.dry_run:
        return dry_run_deploy(args)

    if args.rollback:
        if not args.version:
            print("ERROR: --version is required for rollback")
            return 1

        if args.service == "all":
            print("ERROR: Cannot rollback all services simultaneously")
            return 1

        success = rollback_service(args.service, args.env, args.version)
        return 0 if success else 1

    services = list(SERVICES.keys()) if args.service == "all" else [args.service]

    all_successful = True
    for service in services:
        print(f"\n{'='*60}")
        print(f"  Deploying {service} to {args.env}")
        print(f"  Tag: {args.tag}")
        print(f"  Time: {datetime.now().isoformat()}")
        print(f"{'='*60}\n")

        success = deploy_service(service, args.env, args.tag,
                                 args.skip_build, args.skip_test, args.skip_health)

        # Record deployment
        history = load_deployment_history(args.env)
        history.append({
            "timestamp": datetime.now().isoformat(),
            "service": service,
            "version": args.tag,
            "status": "success" if success else "failed",
            "deployed_by": os.environ.get("USER", "unknown"),
        })
        save_deployment_history(args.env, history)

        if success:
            print(f"✓ {service} deployed successfully to {args.env}")
        else:
            print(f"✗ {service} deployment FAILED")
            all_successful = False
            if args.service != "all":
                break

    return 0 if all_successful else 1


if __name__ == "__main__":
    main()
