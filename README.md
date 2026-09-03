# CodeQL Organization Scan Report

A command-line utility that inventories GitHub Code Scanning/CodeQL coverage across repositories in an organization and writes the results to a CSV file.

## What it does

For each selected repository, the script:

1. Checks whether GitHub Code Scanning **default setup** is configured.
2. If default setup is not configured, looks for **advanced setup** by checking for:
   - a CodeQL analysis uploaded within the last 90 days, or
   - an active GitHub Actions workflow that uses `github/codeql-action/init` or `github/codeql-action/analyze`.
3. Retrieves CodeQL analyses and selects the latest analysis for each language.
4. Records any scan error reported by that latest analysis.
5. Writes all results to a CSV report.

Repositories without Code Scanning are included in the report. API failures are also retained as `UNKNOWN` rows, while a warning is printed to standard error.

## Requirements

- Python 3.11 or newer
- [GitHub CLI](https://cli.github.com/) (`gh`)
- GitHub CLI authentication with access to the target organization and enough repository permissions to read repository metadata, Actions workflows, and Code Scanning data

The Python application uses only the standard library, so [requirements.txt](requirements.txt) contains no PyPI dependencies.

## Setup

### 1. Install Python and GitHub CLI

On macOS with Homebrew:

```shell
brew install python gh
```

### 2. Authenticate GitHub CLI

```shell
gh auth login
```

Follow the prompts for `github.com`. On macOS, GitHub CLI can store the credential securely in Keychain. If access to organization repositories is governed by SAML SSO, authorize the credential for that organization as well.

Check authentication with:

```shell
gh auth status
```

### 3. Optional virtual environment

No Python packages need to be installed, but a virtual environment can still be used:

```shell
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Usage

```shell
python3 walk_org_repos.py ORG [options]
```

### Scan every repository in an organization

```shell
python3 walk_org_repos.py octo-org
```

This writes `codeql_report.csv` in the current directory.

### Scan a limited number of repositories

```shell
python3 walk_org_repos.py octo-org --max-repos 25
```

Short form:

```shell
python3 walk_org_repos.py octo-org -n 25
```

### Scan one repository

Pass the organization as the positional argument and the repository name without its owner to `--repo`:

```shell
python3 walk_org_repos.py octo-org --repo example-repo
```

Short form:

```shell
python3 walk_org_repos.py octo-org -r example-repo
```

### Choose the report path

```shell
python3 walk_org_repos.py octo-org --output reports/codeql.csv
```

The parent directory is created automatically when needed.

### Show all options

```shell
python3 walk_org_repos.py --help
```

`--max-repos` and `--repo` are mutually exclusive.

## CSV columns

| Column | Meaning |
| --- | --- |
| `repo_name` | Full repository name in `owner/repository` form. |
| `codeql_setup` | `YES`, `NO`, or `UNKNOWN`. |
| `setup_type` | `DEFAULT`, `ADVANCED`, `NONE`, or `UNKNOWN`. |
| `scan_error` | Whether the latest analysis for the language contains an error: `YES`, `NO`, or `UNKNOWN`. |
| `error_message` | Error text returned with the analysis, when present. |
| `language` | CodeQL language/category detected from the analysis. |
| `last_scan_date` | Creation timestamp of the latest analysis for the language. |

A configured repository can produce multiple rows—one for each scanned language. A repository with no setup, no analyses, or an API error produces a single summary row.

## How setup detection works

- **Default setup:** The repository's Code Scanning default-setup endpoint reports the state as `configured`.
- **Advanced setup:** Default setup is absent and either a recent CodeQL analysis exists or an active Actions workflow contains a CodeQL init/analyze action.
- **No setup:** Neither default nor advanced setup is detected.

The 90-day analysis window helps avoid treating a stale historical analysis as active advanced setup. An active CodeQL workflow can still establish advanced setup when no recent analysis is available.

## Output and errors

Progress and non-fatal repository warnings are written to standard error. The final report location and row count are written to standard output.

The command exits with status `1` when authentication fails, repositories cannot initially be listed, or the CSV cannot be written. Per-repository API failures do not stop the overall inventory; they appear as `UNKNOWN` in the report.

## Security

The script does not read or store a token directly. All GitHub API requests are delegated to GitHub CLI, which manages authentication. Do not add credentials or exported report data containing sensitive repository details to source control.
