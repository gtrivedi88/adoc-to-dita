#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export BUNDLE_GEMFILE="$project_dir/Gemfile"
export BUNDLE_PATH="$project_dir/vendor/bundle"
export BUNDLE_USER_CACHE="$project_dir/.cache/bundle"
export XDG_CACHE_HOME="$project_dir/.cache"
bundle install
printf '\nReady. Run ./adoc-dita serve and open http://127.0.0.1:8765\n'
