#!/usr/bin/env python3
"""
diagnostic_diff.py - Compare two diagnostic metadata JSON files.

Compares diagnostic/build-XXX.json files produced by build.py and
prints a human-readable diff showing added/removed modules, status
changes, duration deltas, and command/output differences.

Usage:
    python3 tools/diagnostic_diff.py old.json new.json
    python3 tools/diagnostic_diff.py old.json new.json --json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_metadata(path: str) -> dict[str, Any]:
    """Load and validate a diagnostic metadata JSON file."""
    p = Path(path)
    if not p.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    if not p.is_file():
        print(f"Error: not a file: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"Error: expected a JSON object (dict) in {path}, got {type(data).__name__}", file=sys.stderr)
        sys.exit(1)
    return data


def build_module_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract modules dict keyed by name from a report."""
    modules = report.get("modules", [])
    if not isinstance(modules, list):
        return {}
    return {m["name"]: m for m in modules if isinstance(m, dict) and "name" in m}


def compute_diff(
    old_report: dict[str, Any],
    new_report: dict[str, Any],
) -> dict[str, Any]:
    """Compute a structured diff between two diagnostic reports."""
    old_mods = build_module_map(old_report)
    new_mods = build_module_map(new_report)

    old_names = set(old_mods.keys())
    new_names = set(new_mods.keys())

    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)
    common = sorted(old_names & new_names)

    changed: list[dict[str, Any]] = []
    for name in common:
        om = old_mods[name]
        nm = new_mods[name]
        changes: dict[str, Any] = {"name": name}

        old_status = om.get("status", "UNKNOWN")
        new_status = nm.get("status", "UNKNOWN")
        if old_status != new_status:
            changes["status"] = {"old": old_status, "new": new_status}

        old_elapsed = om.get("elapsed_seconds", 0)
        new_elapsed = nm.get("elapsed_seconds", 0)
        if abs(old_elapsed - new_elapsed) > 0.001:
            changes["elapsed_delta_seconds"] = round(new_elapsed - old_elapsed, 3)

        old_artifact = om.get("artifact")
        new_artifact = nm.get("artifact")
        if old_artifact != new_artifact:
            changes["artifact"] = {"old": old_artifact, "new": new_artifact}

        old_output = om.get("output", "")
        new_output = nm.get("output", "")
        if old_output != new_output:
            changes["output_changed"] = True

        if len(changes) > 1:  # has at least 'name' plus one real change
            changed.append(changes)

    # Compute summary stats
    old_total = old_report.get("total_modules", len(old_mods))
    new_total = new_report.get("total_modules", len(new_mods))

    regression_count = sum(
        1 for c in changed
        if c.get("status", {}).get("old") == "PASS" and c.get("status", {}).get("new") == "FAIL"
    )
    improvement_count = sum(
        1 for c in changed
        if c.get("status", {}).get("old") == "FAIL" and c.get("status", {}).get("new") == "PASS"
    )

    diff: dict[str, Any] = {
        "old_file": old_report.get("commit", "unknown"),
        "new_file": new_report.get("commit", "unknown"),
        "old_generated_at": old_report.get("generated_at", ""),
        "new_generated_at": new_report.get("generated_at", ""),
        "old_total_modules": old_total,
        "new_total_modules": new_total,
        "added_modules": added,
        "removed_modules": removed,
        "common_modules": len(common),
        "changed_modules": changed,
        "regressions": regression_count,
        "improvements": improvement_count,
    }
    return diff


def format_human(diff: dict[str, Any]) -> str:
    """Format a diff dict as human-readable text."""
    lines: list[str] = []

    # Header
    lines.append("=" * 60)
    lines.append("  Diagnostic Metadata Diff")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"  Old: commit={diff['old_file']}  ({diff['old_generated_at']})")
    lines.append(f"  New: commit={diff['new_file']}  ({diff['new_generated_at']})")
    lines.append("")

    # Summary
    lines.append(f"  Modules: {diff['old_total_modules']} → {diff['new_total_modules']}")
    if diff["regressions"] > 0:
        lines.append(f"  ⚠ Regressions: {diff['regressions']}")
    if diff["improvements"] > 0:
        lines.append(f"  ✓ Improvements: {diff['improvements']}")
    lines.append("")

    # Added modules
    if diff["added_modules"]:
        lines.append(f"  Added modules ({len(diff['added_modules'])}):")
        for name in diff["added_modules"]:
            lines.append(f"    + {name}")
        lines.append("")

    # Removed modules
    if diff["removed_modules"]:
        lines.append(f"  Removed modules ({len(diff['removed_modules'])}):")
        for name in diff["removed_modules"]:
            lines.append(f"    - {name}")
        lines.append("")

    # Changed modules
    if diff["changed_modules"]:
        lines.append(f"  Changed modules ({len(diff['changed_modules'])}):")
        for mod in diff["changed_modules"]:
            name = mod["name"]
            lines.append(f"    {name}:")
            if "status" in mod:
                old_s = mod["status"]["old"]
                new_s = mod["status"]["new"]
                if old_s == "PASS" and new_s == "FAIL":
                    lines.append(f"      status: {old_s} → {new_s}  ⚠ REGRESSION")
                elif old_s == "FAIL" and new_s == "PASS":
                    lines.append(f"      status: {old_s} → {new_s}  ✓ IMPROVEMENT")
                else:
                    lines.append(f"      status: {old_s} → {new_s}")
            if "elapsed_delta_seconds" in mod:
                delta = mod["elapsed_delta_seconds"]
                direction = "↑" if delta > 0 else "↓"
                lines.append(f"      duration: {direction} {abs(delta):.3f}s")
            if "artifact" in mod:
                lines.append(f"      artifact: {mod['artifact']['old']} → {mod['artifact']['new']}")
            if mod.get("output_changed"):
                lines.append(f"      output: changed")
        lines.append("")

    lines.append("=" * 60)
    if diff["regressions"] > 0:
        lines.append("  Result: REGRESSIONS DETECTED")
    elif diff["improvements"] > 0:
        lines.append("  Result: improvements found, no regressions")
    else:
        lines.append("  Result: no regressions")
    lines.append("=" * 60)

    return "\n".join(lines)


def format_json(diff: dict[str, Any]) -> str:
    """Format a diff dict as pretty-printed JSON."""
    return json.dumps(diff, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two diagnostic metadata JSON files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python3 tools/diagnostic_diff.py diagnostic/build-abc123.json diagnostic/build-def456.json
  python3 tools/diagnostic_diff.py old.json new.json --json
""",
    )
    parser.add_argument("old_file", help="Path to the older diagnostic metadata JSON file")
    parser.add_argument("new_file", help="Path to the newer diagnostic metadata JSON file")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON instead of human-readable text")

    args = parser.parse_args()

    old_report = load_metadata(args.old_file)
    new_report = load_metadata(args.new_file)

    diff = compute_diff(old_report, new_report)

    if args.json:
        print(format_json(diff))
    else:
        print(format_human(diff))

    # Exit non-zero when regressions are detected
    if diff["regressions"] > 0:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
