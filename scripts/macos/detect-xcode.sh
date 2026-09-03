#!/usr/bin/env bash

set -euo pipefail

source_root="${1:?Usage: detect-xcode.sh <source-root> <github-env-file> <work-dir>}"
github_env_file="${2:?Usage: detect-xcode.sh <source-root> <github-env-file> <work-dir>}"
work_dir="${3:?Usage: detect-xcode.sh <source-root> <github-env-file> <work-dir>}"

python3 scripts/macos/detect-xcode.py "$source_root" "$github_env_file" "$work_dir"
