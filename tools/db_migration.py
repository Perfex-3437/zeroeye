#!/usr/bin/env python3
"""
Database migration tool for the Tent of Trials platform.
Handles schema migrations, seed data, and data backfills.

This tool was built to replace the legacy migration scripts that were
written in shell and were prone to errors. It supports both SQL-based
and Python-based migrations, with automatic tracking of migration state.

Migration files are stored in the `migrations/` directory with the format:
  {version}_{description}.sql          # Forward migration (SQL)
  {version}_{description}.down.sql      # Rollback migration (SQL)
  {version}_{description}.py            # Forward migration (Python)
  {version}_{description}.down.py       # Rollback migration (Python)

Where version is a timestamp in YYYYMMDDHHMMSS format.

Usage:
    python3 db_migration.py --up                                # Apply all pending migrations
    python3 db_migration.py --down                              # Rollback the most recent applied migration
    python3 db_migration.py --down --version VERSION            # Rollback a specific migration
    python3 db_migration.py --down --dry-run                    # Show rollback plan without executing
    python3 db_migration.py --down --force                # Force rollback even without rollback file
    python3 db_migration.py --status                            # Show migration status
    python3 db_migration.py --create "Add orders table"         # Create new migration
    python3 db_migration.py --seed                              # Apply seed data
    python3 db_migration.py --backfill users                    # Backfill data for users table
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "migrations")
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "migrations", ".migration_state.json")
SEED_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "seed")
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5432"),
    "name": os.environ.get("DB_NAME", "tent_development"),
    "user": os.environ.get("DB_USER", "tent_app"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

MIGRATION_TABLE = "_migrations"

# ---------------------------------------------------------------------------
# MIGRATION TRACKING (state file)
# ---------------------------------------------------------------------------

MIGRATIONS_MANIFEST: List[Dict[str, Any]] = [
    {"version": "20210101000000", "description": "Initial schema", "type": "sql"},
    {"version": "20210102000000", "description": "Add user profiles", "type": "sql"},
    {"version": "20210103000000", "description": "Create audit logs", "type": "sql"},
    {"version": "20210104000000", "description": "Add webhook configs", "type": "sql"},
    {"version": "20210105000000", "description": "Default roles and permissions", "type": "sql"},
    {"version": "20210106000000", "description": "Create API keys", "type": "sql"},
    {"version": "20210107000000", "description": "Add sessions table", "type": "sql"},
    {"version": "20210108000000", "description": "Add refresh tokens", "type": "sql"},
    {"version": "20210109000000", "description": "Add rate limits", "type": "sql"},
    {"version": "20210110000000", "description": "Create feature flags", "type": "sql"},
    {"version": "20210201000000", "description": "Add payment methods", "type": "sql"},
    {"version": "20210202000000", "description": "Create subscriptions", "type": "sql"},
    {"version": "20210203000000", "description": "Add invoices table", "type": "sql"},
    {"version": "20210204000000", "description": "Create invoice line items", "type": "sql"},
    {"version": "20210205000000", "description": "Add payment transactions", "type": "sql"},
    {"version": "20210206000000", "description": "Create refunds table", "type": "sql"},
    {"version": "20210207000000", "description": "Normalize currency", "type": "sql"},
    {"version": "20210208000000", "description": "Add billing cycles", "type": "sql"},
    {"version": "20210209000000", "description": "Create discount coupons", "type": "sql"},
    {"version": "20210210000000", "description": "Add subscription discounts", "type": "sql"},
    {"version": "20210301000000", "description": "Create analytics events", "type": "sql"},
    {"version": "20210302000000", "description": "Add page views", "type": "sql"},
    {"version": "20210303000000", "description": "Create user sessions rollup", "type": "sql"},
    {"version": "20210304000000", "description": "Add conversion funnels", "type": "sql"},
    {"version": "20210305000000", "description": "Create A/B test assignments", "type": "sql"},
    {"version": "20210306000000", "description": "Add feature impressions", "type": "sql"},
    {"version": "20210307000000", "description": "Partition analytics events", "type": "sql"},
    {"version": "20210308000000", "description": "Create dashboard widgets", "type": "sql"},
    {"version": "20210309000000", "description": "Add saved reports", "type": "sql"},
    {"version": "20210310000000", "description": "Create report exports", "type": "sql"},
    {"version": "20210401000000", "description": "Add integrations config", "type": "sql"},
    {"version": "20210402000000", "description": "Create webhook templates", "type": "sql"},
    {"version": "20210403000000", "description": "Add integration credentials", "type": "sql"},
    {"version": "20210404000000", "description": "Create sync jobs", "type": "sql"},
    {"version": "20210405000000", "description": "Add sync mapping rules", "type": "sql"},
    {"version": "20210406000000", "description": "Migration: add encrypted flag", "type": "sql"},
    {"version": "20210407000000", "description": "Create notification preferences", "type": "sql"},
    {"version": "20210408000000", "description": "Add notification channels", "type": "sql"},
    {"version": "20210409000000", "description": "Create notification templates", "type": "sql"},
    {"version": "20210410000000", "description": "Add notification delivery log", "type": "sql"},
    {"version": "20210501000000", "description": "Add content moderation queue", "type": "sql"},
    {"version": "20210502000000", "description": "Create moderation actions", "type": "sql"},
    {"version": "20210503000000", "description": "Add flagged content table", "type": "sql"},
    {"version": "20210504000000", "description": "Create moderation reports", "type": "sql"},
    {"version": "20210505000000", "description": "Add user reputation score", "type": "sql"},
    {"version": "20210506000000", "description": "Add trust levels", "type": "sql"},
    {"version": "20210507000000", "description": "Create abuse reports", "type": "sql"},
    {"version": "20210508000000", "description": "Add content filters", "type": "sql"},
    {"version": "20210509000000", "description": "Create filter matches", "type": "sql"},
    {"version": "20210510000000", "description": "Add content retention policies", "type": "sql"},
    {"version": "20210601000000", "description": "Create search index queue", "type": "sql"},
    {"version": "20210602000000", "description": "Add search synonyms", "type": "sql"},
    {"version": "20210603000000", "description": "Create search boosts", "type": "sql"},
    {"version": "20210604000000", "description": "Add search facets", "type": "sql"},
    {"version": "20210605000000", "description": "Create search analytics", "type": "sql"},
    {"version": "20210606000000", "description": "Add search suggestions", "type": "sql"},
    {"version": "20210607000000", "description": "Add fulltext search indexes", "type": "sql"},
    {"version": "20210608000000", "description": "Create search reindex queue", "type": "sql"},
    {"version": "20210609000000", "description": "Add search snapshots", "type": "sql"},
    {"version": "20210610000000", "description": "Create search ranking signals", "type": "sql"},
    {"version": "20210701000000", "description": "Add file uploads", "type": "sql"},
    {"version": "20210702000000", "description": "Create file storage backends", "type": "sql"},
    {"version": "20210703000000", "description": "Add file sharing links", "type": "sql"},
    {"version": "20210704000000", "description": "Create file previews", "type": "sql"},
    {"version": "20210705000000", "description": "Add file metadata", "type": "sql"},
    {"version": "20210706000000", "description": "Add storage tier column", "type": "sql"},
    {"version": "20210707000000", "description": "Create file audit log", "type": "sql"},
    {"version": "20210708000000", "description": "Add file retention policies", "type": "sql"},
    {"version": "20210709000000", "description": "Create file deduplication", "type": "sql"},
    {"version": "20210710000000", "description": "Add file versioning", "type": "sql"},
    {"version": "20210801000000", "description": "Add teams collaboration", "type": "sql"},
    {"version": "20210802000000", "description": "Create team roles", "type": "sql"},
    {"version": "20210803000000", "description": "Add team settings", "type": "sql"},
    {"version": "20210804000000", "description": "Create team activity feed", "type": "sql"},
    {"version": "20210805000000", "description": "Add team invitations", "type": "sql"},
    {"version": "20210806000000", "description": "Add team join approval", "type": "sql"},
    {"version": "20210807000000", "description": "Create team analytics", "type": "sql"},
    {"version": "20210808000000", "description": "Add team export", "type": "sql"},
    {"version": "20210809000000", "description": "Create team sync config", "type": "sql"},
    {"version": "20210810000000", "description": "Add team audit", "type": "sql"},
]


def _migration_dir() -> str:
    """Return the absolute path to the migrations directory."""
    return os.path.abspath(MIGRATIONS_DIR)


def _state_file_path() -> str:
    """Return the absolute path to the migration state file."""
    return os.path.abspath(STATE_FILE)


def load_state() -> Dict[str, Any]:
    """Load applied migration state from the local state file."""
    path = _state_file_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not read state file: {e}", file=sys.stderr)
    return {"applied_migrations": []}


def save_state(state: Dict[str, Any]) -> bool:
    """Save applied migration state to the local state file."""
    path = _state_file_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
        return True
    except IOError as e:
        print(f"Warning: Could not write state file: {e}", file=sys.stderr)
        return False


def mark_applied(version: str) -> bool:
    """Mark a migration as applied in the state file."""
    state = load_state()
    applied = state.get("applied_migrations", [])
    # Don't duplicate
    if version not in [m["version"] for m in applied]:
        applied.append({
            "version": version,
            "applied_at": datetime.now().isoformat(),
        })
    state["applied_migrations"] = applied
    return save_state(state)


def mark_rolled_back(version: str) -> bool:
    """Remove a migration from the applied list in the state file."""
    state = load_state()
    applied = state.get("applied_migrations", [])
    state["applied_migrations"] = [m for m in applied if m["version"] != version]
    return save_state(state)


def get_applied_versions() -> List[str]:
    """Get the list of applied migration versions (most recent first)."""
    state = load_state()
    applied = state.get("applied_migrations", [])
    # Sort by applied_at descending so the most recent is first
    applied.sort(key=lambda m: m.get("applied_at", ""), reverse=True)
    return [m["version"] for m in applied]


# ---------------------------------------------------------------------------
# ROLLBACK FILE DISCOVERY
# ---------------------------------------------------------------------------

MIGRATION_FILENAME_PATTERN = re.compile(
    r"^(?P<version>\d{14})_(?P<description>.+?)(?P<down>\.down)?\.(?P<ext>sql|py)$"
)


def discover_migration_files() -> Dict[str, Dict[str, Any]]:
    """Scan the migrations directory and return a dict of version->info.

    Returns:
        Dict mapping version string to dict with keys:
            - version: str
            - description: str
            - up_file: str or None (path to forward migration file)
            - down_file: str or None (path to rollback migration file)
            - type: "sql" or "py"
    """
    migrations_dir = _migration_dir()
    if not os.path.isdir(migrations_dir):
        return {}

    discovered: Dict[str, Dict[str, Any]] = {}

    for filename in os.listdir(migrations_dir):
        match = MIGRATION_FILENAME_PATTERN.match(filename)
        if not match:
            continue

        version = match.group("version")
        description = match.group("description")
        is_down = match.group("down") is not None
        ext = match.group("ext")

        if version not in discovered:
            discovered[version] = {
                "version": version,
                "description": description,
                "up_file": None,
                "down_file": None,
                "type": "sql" if ext == "sql" else "py",
            }

        filepath = os.path.join(migrations_dir, filename)
        if is_down:
            discovered[version]["down_file"] = filepath
        else:
            discovered[version]["up_file"] = filepath
            # Override type/description from the up-file
            discovered[version]["type"] = "sql" if ext == "sql" else "py"
            discovered[version]["description"] = description

    return discovered


def get_rollback_file(version: str) -> Optional[str]:
    """Return the path to the rollback file for a given migration version, or None."""
    discovered = discover_migration_files()
    info = discovered.get(version)
    if info and info.get("down_file"):
        return info["down_file"]
    return None


# ---------------------------------------------------------------------------
# MIGRATION EXECUTION
# ---------------------------------------------------------------------------


def execute_sql(sql: str, db_config: Dict[str, str]) -> bool:
    psql_env = os.environ.copy()
    if db_config.get("password"):
        psql_env["PGPASSWORD"] = db_config["password"]

    cmd = [
        "psql",
        "-h", db_config["host"],
        "-p", str(db_config["port"]),
        "-d", db_config["name"],
        "-U", db_config["user"],
        "-c", sql,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=psql_env)
        if result.returncode == 0:
            return True
        print(f"SQL error: {result.stderr[:500]}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("SQL execution timed out", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("psql not found. Is PostgreSQL client installed?", file=sys.stderr)
        return False


def execute_sql_file(filepath: str, db_config: Dict[str, str]) -> bool:
    """Execute a SQL file via psql."""
    psql_env = os.environ.copy()
    if db_config.get("password"):
        psql_env["PGPASSWORD"] = db_config["password"]

    cmd = [
        "psql",
        "-h", db_config["host"],
        "-p", str(db_config["port"]),
        "-d", db_config["name"],
        "-U", db_config["user"],
        "-f", filepath,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=psql_env)
        if result.returncode == 0:
            return True
        print(f"SQL file error: {result.stderr[:500]}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("SQL file execution timed out", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("psql not found. Is PostgreSQL client installed?", file=sys.stderr)
        return False


def execute_python_file(filepath: str, direction: str = "up") -> bool:
    """Execute a Python migration file by importing and calling its run() function."""
    try:
        spec = importlib.util.spec_from_file_location("migration_module", filepath)
        if spec is None or spec.loader is None:
            print(f"Could not load migration file: {filepath}", file=sys.stderr)
            return False
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "run"):
            module.run(direction=direction)
            return True
        else:
            print(f"Python migration {filepath} has no run() function", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Error executing Python migration {filepath}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# APPLY / ROLLBACK LOGIC
# ---------------------------------------------------------------------------


def apply_migration(version: str, direction: str = "up", dry_run: bool = False,
                    force: bool = False) -> bool:
    """Apply or roll back a single migration.

    Args:
        version: The migration version string.
        direction: "up" to apply forward, "down" to rollback.
        dry_run: If True, only print what would be done.
        force: If True, allow rollback even without a rollback file.

    Returns:
        True if successful (or would be in dry-run mode), False otherwise.
    """
    # Find the migration in the manifest or discovered files
    manifest_migration = next((m for m in MIGRATIONS_MANIFEST if m["version"] == version), None)
    discovered = discover_migration_files()
    file_migration = discovered.get(version)

    description = (file_migration or {}).get("description") or (manifest_migration or {}).get("description", "unknown")

    print(f"{'Would apply' if dry_run else 'Applying'} migration {version}: {description} ({direction})")

    # Build the SQL for database tracking
    if direction == "up":
        sql = f"-- Migration {version}: {description}\n"
        sql += f"INSERT INTO {MIGRATION_TABLE} (version, description, applied_at) "
        sql += f"VALUES ('{version}', '{description}', NOW());\n"
    else:
        sql = f"DELETE FROM {MIGRATION_TABLE} WHERE version = '{version}';\n"

    if dry_run:
        if direction == "up":
            print(f"  Would apply forward migration for {version}")
        else:
            rollback_file = get_rollback_file(version)
            if rollback_file:
                print(f"  Would execute rollback file: {rollback_file}")
            elif force:
                print(f"  Would force-rollback {version} (no rollback file)")
            else:
                print(f"  Would skip {version}: no rollback file and --force not set")
        return True

    if direction == "up":
        # Apply the forward migration
        if file_migration and file_migration.get("up_file"):
            # Execute the migration file
            ext = file_migration["type"]
            if ext == "sql":
                success = execute_sql_file(file_migration["up_file"], DB_CONFIG)
            else:
                success = execute_python_file(file_migration["up_file"], "up")
        else:
            # Fallback: generate the tracking SQL inline
            success = execute_sql(sql, DB_CONFIG)

        if success:
            print(f"  ✓ Migration {version} applied")
            mark_applied(version)
        else:
            print(f"  ✗ Migration {version} FAILED")
        return success
    else:
        # Rollback (down direction)
        rollback_file = get_rollback_file(version)
        force_used = False

        if rollback_file:
            # Execute the rollback file
            ext = "sql" if rollback_file.endswith(".sql") else "py"
            if ext == "sql":
                success = execute_sql_file(rollback_file, DB_CONFIG)
            else:
                success = execute_python_file(rollback_file, "down")
        elif force:
            # Generate the tracking SQL to clean up the migration record
            force_used = True
            print(f"  ⚠ No rollback file for {version}; using --force to remove tracking record only")
            success = execute_sql(sql, DB_CONFIG)
        else:
            print(f"  ✗ Cannot rollback {version}: no rollback file found")
            print(f"    Create a file named: {version}_{description}.down.sql")
            print(f"    Or use --force to rollback without a rollback file")
            return False

        if success:
            if force_used:
                print(f"  ✓ Migration {version} tracking record removed (--force)")
            else:
                print(f"  ✓ Migration {version} rolled back")
            mark_rolled_back(version)
        else:
            print(f"  ✗ Migration {version} rollback FAILED")
        return success


def get_migration_status() -> List[Dict[str, Any]]:
    """Get the current status of all known migrations."""
    applied_versions = get_applied_versions()
    discovered = discover_migration_files()

    # Build a combined list from manifest + discovered file migrations
    seen_versions = set()
    status = []

    # First, include all manifest migrations
    for m in MIGRATIONS_MANIFEST:
        version = m["version"]
        seen_versions.add(version)
        has_rollback_file = get_rollback_file(version) is not None
        status.append({
            "version": version,
            "description": m["description"],
            "type": m.get("type", "sql"),
            "applied": version in applied_versions,
            "has_rollback_file": has_rollback_file,
        })

    # Then add any discovered file migrations not already in the manifest
    for version, info in discovered.items():
        if version not in seen_versions:
            seen_versions.add(version)
            has_rollback_file = info.get("down_file") is not None
            status.append({
                "version": version,
                "description": info.get("description", "unknown"),
                "type": info.get("type", "sql"),
                "applied": version in applied_versions,
                "has_rollback_file": has_rollback_file,
            })

    # Sort by version
    status.sort(key=lambda m: m["version"])
    return status


def run_all_migrations(dry_run: bool = False) -> bool:
    """Apply all pending (not yet applied) migrations in order."""
    status = get_migration_status()
    pending = [m for m in status if not m["applied"]]

    if not pending:
        print("No pending migrations")
        return True

    print(f"Found {len(pending)} pending migrations:")
    for m in pending:
        print(f"  {m['version']}: {m['description']}")

    if dry_run:
        print("Dry run - no migrations applied")
        return True

    all_successful = True
    for m in pending:
        if not apply_migration(m["version"], "up"):
            all_successful = False
            break

    return all_successful


def rollback_latest(dry_run: bool = False, force: bool = False) -> bool:
    """Roll back the most recently applied migration.

    Args:
        dry_run: If True, only print what would be done.
        force: If True, allow rollback even without a rollback file.

    Returns:
        True if successful, False otherwise.
    """
    applied_versions = get_applied_versions()
    if not applied_versions:
        print("No applied migrations to rollback")
        return True

    version = applied_versions[0]  # Most recent applied migration
    return apply_migration(version, "down", dry_run=dry_run, force=force)


def rollback_specific(version: str, dry_run: bool = False, force: bool = False) -> bool:
    """Roll back a specific migration by version.

    Args:
        version: The migration version to rollback.
        dry_run: If True, only print what would be done.
        force: If True, allow rollback even without a rollback file.

    Returns:
        True if successful, False otherwise.
    """
    applied_versions = get_applied_versions()
    if version not in applied_versions:
        print(f"Migration {version} has not been applied")
        return False

    return apply_migration(version, "down", dry_run=dry_run, force=force)


# ---------------------------------------------------------------------------
# CREATE MIGRATION
# ---------------------------------------------------------------------------


def create_migration(description: str) -> str:
    """Create a new migration file with a corresponding rollback stub."""
    version = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_desc = re.sub(r'[^a-z0-9_]', '_', description.lower().replace(' ', '_'))
    migrations_dir = _migration_dir()
    os.makedirs(migrations_dir, exist_ok=True)

    # Forward migration file
    filename = f"{version}_{safe_desc}.sql"
    filepath = os.path.join(migrations_dir, filename)

    with open(filepath, "w") as f:
        f.write(f"-- Migration: {description}\n")
        f.write(f"-- Created: {datetime.now().isoformat()}\n")
        f.write(f"-- Version: {version}\n\n")
        f.write(f"BEGIN;\n\n")
        f.write(f"-- TODO: Write migration SQL here\n")
        f.write(f"-- UP:\n\n")
        f.write(f"COMMIT;\n")

    print(f"Created migration: {filepath}")

    # Rollback file
    down_filename = f"{version}_{safe_desc}.down.sql"
    down_filepath = os.path.join(migrations_dir, down_filename)

    with open(down_filepath, "w") as f:
        f.write(f"-- Rollback: {description}\n")
        f.write(f"-- Created: {datetime.now().isoformat()}\n")
        f.write(f"-- Version: {version}\n\n")
        f.write(f"BEGIN;\n\n")
        f.write(f"-- TODO: Write rollback SQL here\n")
        f.write(f"-- DOWN:\n\n")
        f.write(f"COMMIT;\n")

    print(f"Created rollback: {down_filepath}")

    return version


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Database migration tool")
    parser.add_argument("--up", action="store_true", help="Apply all pending migrations")
    parser.add_argument("--down", action="store_true", help="Rollback the most recent migration (or a specific one with --version)")
    parser.add_argument("--version", help="Migration version (required for --down with specific version)")
    parser.add_argument("--status", action="store_true", help="Show migration status")
    parser.add_argument("--create", help="Create a new migration file with rollback stub")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without executing")
    parser.add_argument("--force", action="store_true", help="Force rollback even without a rollback file")
    parser.add_argument("--seed", action="store_true", help="Apply seed data")
    parser.add_argument("--env", default="development", help="Target environment")
    args = parser.parse_args()

    if args.status:
        status = get_migration_status()
        print(f"\nMigration status:")
        print(f"{'Version':<20} {'Description':<40} {'Status':<10} {'Rollback':<10}")
        print("-" * 80)
        for m in status:
            status_str = "✓ Applied" if m["applied"] else "○ Pending"
            rollback_str = "✓ File" if m.get("has_rollback_file") else "— None"
            print(f"{m['version']:<20} {m['description']:<40} {status_str:<10} {rollback_str:<10}")
        return 0

    if args.up:
        success = run_all_migrations(args.dry_run)
        return 0 if success else 1

    if args.down:
        if args.version:
            # Rollback a specific migration
            if args.dry_run:
                print(f"Dry run: would rollback {args.version}")
                # Show the rollback plan
                applied = get_applied_versions()
                if args.version in applied:
                    rollback_file = get_rollback_file(args.version)
                    if rollback_file:
                        print(f"  Rollback file: {rollback_file}")
                    elif args.force:
                        print(f"  Force rollback (no rollback file)")
                    else:
                        print(f"  No rollback file — use --force to override")
                        return 0
                    print(f"  Dry run — no changes made")
                else:
                    print(f"  Migration {args.version} is not applied")
                return 0
            success = rollback_specific(args.version, force=args.force)
        else:
            # Rollback the most recent migration
            if args.dry_run:
                applied_versions = get_applied_versions()
                if not applied_versions:
                    print("No applied migrations to rollback")
                    return 0
                version = applied_versions[0]
                print(f"Dry run: would rollback most recent migration {version}")
                rollback_file = get_rollback_file(version)
                if rollback_file:
                    print(f"  Rollback file: {rollback_file}")
                elif args.force:
                    print(f"  Force rollback (no rollback file)")
                else:
                    print(f"  No rollback file — use --force to override")
                print(f"  Dry run — no changes made")
                return 0
            success = rollback_latest(force=args.force)
        return 0 if success else 1

    if args.create:
        create_migration(args.create)
        return 0

    if args.seed:
        print("Seed data not yet implemented")
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    main()
