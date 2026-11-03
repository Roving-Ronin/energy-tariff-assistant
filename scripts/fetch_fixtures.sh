#!/usr/bin/env bash
# Fetch the energy-manager-tariffs plan files used as importer test fixtures.
#
# They are not committed here because that repository has no licence file.
# Run this before the tests; CI runs it too.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
target="$repo_root/tests/fixtures/energy_manager"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

git clone --quiet --depth 1 https://github.com/ha-energymanager/energy-manager-tariffs "$work/src"
mkdir -p "$target"
find "$target" -name '*.json' -delete
find "$work/src/tariffs" -name '*.json' ! -name 'index.json' -exec cp {} "$target/" \;
echo "fetched $(find "$target" -name '*.json' | wc -l) fixture files into tests/fixtures/energy_manager"
