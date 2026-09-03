#!/usr/bin/env python3
"""Report Code Scanning setup for one or more organization repositories.

Authentication is delegated to the GitHub CLI (`gh`), which can keep a PAT in
macOS Keychain rather than in this script or a plaintext configuration file.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote


def positive_integer(value: str) -> int:
    """Parse a strictly positive integer for an argparse option."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def run_gh(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run GitHub CLI without reading or printing its stored credential."""
    return subprocess.run(
        ["gh", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def ensure_authenticated() -> None:
    """Fail with a useful message unless GitHub CLI is installed and logged in."""
    if shutil.which("gh") is None:
        raise RuntimeError(
            "GitHub CLI is not installed. Install it with Homebrew: brew install gh"
        )

    result = run_gh("auth", "status", "--hostname", "github.com")
    if result.returncode != 0:
        detail = result.stderr.strip() or "no active GitHub CLI login"
        raise RuntimeError(
            f"GitHub CLI authentication failed: {detail}\n"
            "Run 'gh auth login' and choose the macOS Keychain when prompted."
        )


def fetch_repositories(
    org: str, maximum: int | None = None
) -> list[dict[str, Any]]:
    """Fetch every repository, or stop at `maximum` when one is provided."""
    repositories: list[dict[str, Any]] = []
    page = 1

    while maximum is None or len(repositories) < maximum:
        page_size = 100 if maximum is None else min(100, maximum - len(repositories))
        endpoint = f"/orgs/{quote(org, safe='')}/repos"
        result = run_gh(
            "api",
            "--method",
            "GET",
            endpoint,
            "-f",
            f"per_page={page_size}",
            "-f",
            f"page={page}",
            "-f",
            "sort=full_name",
            "-f",
            "direction=asc",
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "unknown GitHub API error"
            raise RuntimeError(f"Could not list repositories for '{org}': {detail}")

        try:
            batch = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("GitHub CLI returned invalid JSON") from error

        if not isinstance(batch, list):
            raise RuntimeError("GitHub API returned an unexpected response")

        repositories.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    return repositories if maximum is None else repositories[:maximum]


def repository_endpoint(full_name: str, suffix: str) -> str:
    """Build a safely quoted REST endpoint for a repository."""
    owner, repo = full_name.split("/", maxsplit=1)
    return (
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/{suffix.lstrip('/')}"
    )


def response_says_code_scanning_is_unavailable(detail: str) -> bool:
    """Recognize GitHub responses that explicitly say scanning is unavailable."""
    if re.search(r"\bHTTP 404\b", detail, flags=re.IGNORECASE):
        return True
    return (
        re.search(r"\bHTTP 403\b", detail, flags=re.IGNORECASE) is not None
        and "must be enabled" in detail.lower()
        and ("code scanning" in detail.lower() or "advanced security" in detail.lower())
    )


def default_setup_is_configured(full_name: str) -> bool:
    """Read the repository's explicit default-setup state."""
    endpoint = repository_endpoint(full_name, "code-scanning/default-setup")
    result = run_gh("api", "--method", "GET", endpoint)

    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown GitHub API error"
        if response_says_code_scanning_is_unavailable(detail):
            return False
        raise RuntimeError(
            f"Could not check default setup for '{full_name}': {detail}"
        )

    try:
        configuration = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"GitHub returned invalid default-setup JSON for '{full_name}'"
        ) from error

    if not isinstance(configuration, dict):
        raise RuntimeError(
            f"GitHub returned an unexpected default-setup response for '{full_name}'"
        )
    return configuration.get("state") == "configured"


def has_recent_code_scanning_analysis(full_name: str) -> bool:
    """Detect an active advanced setup from a recent uploaded analysis."""
    endpoint = repository_endpoint(full_name, "code-scanning/analyses")
    result = run_gh(
        "api",
        "--method",
        "GET",
        endpoint,
        "-f",
        "tool_name=CodeQL",
        "-f",
        "per_page=1",
    )

    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown GitHub API error"
        if response_says_code_scanning_is_unavailable(detail):
            return False
        raise RuntimeError(
            f"Could not check analyses for '{full_name}': {detail}"
        )

    try:
        analyses = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"GitHub returned invalid analyses JSON for '{full_name}'"
        ) from error

    if not isinstance(analyses, list) or not analyses:
        return False

    created_at = analyses[0].get("created_at")
    if not isinstance(created_at, str):
        return False

    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return created >= datetime.now(UTC) - timedelta(days=90)


def fetch_latest_codeql_analysis_by_language(
    full_name: str,
) -> dict[str, dict[str, Any]]:
    """Return the newest CodeQL analysis found for every scanned language."""
    endpoint = repository_endpoint(full_name, "code-scanning/analyses")
    latest_by_language: dict[str, dict[str, Any]] = {}
    page = 1

    while True:
        result = run_gh(
            "api",
            "--method",
            "GET",
            endpoint,
            "-f",
            "tool_name=CodeQL",
            "-f",
            "per_page=100",
            "-f",
            f"page={page}",
            "-f",
            "sort=created",
            "-f",
            "direction=desc",
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "unknown GitHub API error"
            if response_says_code_scanning_is_unavailable(detail):
                return {}
            raise RuntimeError(
                f"Could not list CodeQL analyses for '{full_name}': {detail}"
            )

        try:
            batch = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"GitHub returned invalid analyses JSON for '{full_name}'"
            ) from error

        if not isinstance(batch, list):
            raise RuntimeError(
                f"GitHub returned an unexpected analyses response for '{full_name}'"
            )

        for analysis in batch:
            if not isinstance(analysis, dict):
                continue
            language = codeql_analysis_language(analysis)
            latest_by_language.setdefault(language, analysis)

        if len(batch) < 100:
            break
        page += 1

    return latest_by_language


def codeql_analysis_language(analysis: dict[str, Any]) -> str:
    """Extract a readable language/category from a CodeQL analysis record."""
    category = analysis.get("category")
    if isinstance(category, str):
        match = re.search(r"(?:^|/)language:([^/]+)", category)
        if match:
            return match.group(1)

    environment = analysis.get("environment")
    if isinstance(environment, str):
        try:
            environment = json.loads(environment)
        except json.JSONDecodeError:
            environment = None
    if isinstance(environment, dict) and isinstance(environment.get("language"), str):
        return environment["language"]

    return str(category or "unknown")


def has_active_code_scanning_workflow(full_name: str) -> bool:
    """Find an active Actions workflow that runs a CodeQL analysis."""
    endpoint = repository_endpoint(full_name, "actions/workflows")
    result = run_gh(
        "api", "--method", "GET", endpoint, "-f", "per_page=100"
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown GitHub API error"
        if re.search(r"\bHTTP 404\b", detail, flags=re.IGNORECASE):
            return False
        raise RuntimeError(
            f"Could not check Actions workflows for '{full_name}': {detail}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"GitHub returned invalid workflows JSON for '{full_name}'"
        ) from error

    workflows = payload.get("workflows") if isinstance(payload, dict) else None
    if not isinstance(workflows, list):
        raise RuntimeError(
            f"GitHub returned an unexpected workflows response for '{full_name}'"
        )

    code_scanning_action = re.compile(
        r"github/codeql-action/(?:init|analyze)@", re.IGNORECASE
    )
    for workflow in workflows:
        if not isinstance(workflow, dict) or workflow.get("state") != "active":
            continue
        path = workflow.get("path")
        if not isinstance(path, str):
            continue

        content_endpoint = repository_endpoint(
            full_name, f"contents/{quote(path, safe='/')}"
        )
        content_result = run_gh(
            "api",
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github.raw+json",
            content_endpoint,
        )
        if content_result.returncode != 0:
            continue
        if code_scanning_action.search(content_result.stdout):
            return True

    return False


def detect_code_scanning_setup(full_name: str) -> tuple[bool, bool]:
    """Return whether default setup or active advanced setup is configured."""
    default = default_setup_is_configured(full_name)
    if default:
        return True, False

    advanced = has_recent_code_scanning_analysis(full_name)
    if not advanced:
        advanced = has_active_code_scanning_workflow(full_name)
    return False, advanced


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report default or advanced Code Scanning setup for repositories."
    )
    parser.add_argument("org", help="GitHub organization login, for example: github")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--max-repos",
        "-n",
        type=positive_integer,
        default=None,
        help="maximum repositories to evaluate; omitted means all repositories",
    )
    selection.add_argument(
        "--repo",
        "-r",
        help="evaluate only this repository name, for example: docs",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="codeql_report.csv",
        help="CSV output path (default: codeql_report.csv)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        ensure_authenticated()
        if args.repo:
            repositories = [{"full_name": f"{args.org}/{args.repo}"}]
        else:
            repositories = fetch_repositories(args.org, args.max_repos)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    rows: list[dict[str, str]] = []
    print(f"Evaluating {len(repositories)} repositories...", file=sys.stderr)
    for index, repository in enumerate(repositories, start=1):
        full_name = repository.get("full_name")
        if not isinstance(full_name, str) or "/" not in full_name:
            print(
                f"Warning: repository {index}/{len(repositories)} has no valid name",
                file=sys.stderr,
            )
            continue

        print(
            f"Evaluating {index}/{len(repositories)}: {full_name}",
            file=sys.stderr,
        )
        try:
            default, advanced = detect_code_scanning_setup(full_name)
        except RuntimeError as error:
            print(f"Warning: {error}", file=sys.stderr)
            rows.append(
                {
                    "repo_name": full_name,
                    "codeql_setup": "UNKNOWN",
                    "setup_type": "UNKNOWN",
                    "scan_error": "UNKNOWN",
                    "error_message": "",
                    "language": "",
                    "last_scan_date": "",
                }
            )
            continue

        configured = default or advanced
        if not configured:
            rows.append(
                {
                    "repo_name": full_name,
                    "codeql_setup": "NO",
                    "setup_type": "NONE",
                    "scan_error": "NO",
                    "error_message": "",
                    "language": "",
                    "last_scan_date": "",
                }
            )
            continue

        try:
            latest_by_language = fetch_latest_codeql_analysis_by_language(full_name)
        except RuntimeError as error:
            print(f"Warning: {error}", file=sys.stderr)
            rows.append(
                {
                    "repo_name": full_name,
                    "codeql_setup": "YES",
                    "setup_type": "DEFAULT" if default else "ADVANCED",
                    "scan_error": "UNKNOWN",
                    "error_message": "",
                    "language": "",
                    "last_scan_date": "",
                }
            )
            continue

        if not latest_by_language:
            rows.append(
                {
                    "repo_name": full_name,
                    "codeql_setup": "YES",
                    "setup_type": "DEFAULT" if default else "ADVANCED",
                    "scan_error": "NO",
                    "error_message": "",
                    "language": "",
                    "last_scan_date": "",
                }
            )
            continue

        for language, analysis in sorted(latest_by_language.items()):
            analysis_error = analysis.get("error")
            failed = isinstance(analysis_error, str) and bool(analysis_error)
            rows.append(
                {
                    "repo_name": full_name,
                    "codeql_setup": "YES",
                    "setup_type": "DEFAULT" if default else "ADVANCED",
                    "scan_error": "YES" if failed else "NO",
                    "error_message": str(analysis_error) if failed else "",
                    "language": language,
                    "last_scan_date": str(analysis.get("created_at", "")),
                }
            )

    output_path = Path(args.output).expanduser()
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8") as output_file:
            writer = csv.DictWriter(
                output_file,
                fieldnames=[
                    "repo_name",
                    "codeql_setup",
                    "setup_type",
                    "scan_error",
                    "error_message",
                    "language",
                    "last_scan_date",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
    except OSError as error:
        print(f"Error: could not write CSV report: {error}", file=sys.stderr)
        return 1

    print(f"CSV report written to {output_path.resolve()} ({len(rows)} rows)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
