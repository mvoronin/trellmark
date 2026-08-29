#!/usr/bin/env bash
set -euo pipefail

readonly GSD_VERSION="${GSD_VERSION:-1.11.0}"
readonly GSD_NODE_PACKAGE="${GSD_NODE_PACKAGE:-node@24}"

repo_root="$(git rev-parse --show-toplevel)"
cache_root="${TMPDIR:-/tmp}/trellmark-npm-cache"

cd "$repo_root"

env npm_config_cache="$cache_root" \
  npx --yes \
  --package="$GSD_NODE_PACKAGE" \
  --package="@opengsd/gsd-core@$GSD_VERSION" \
  gsd-core --codex --local

node scripts/normalize_gsd_hooks.cjs

while IFS= read -r hook_file; do
  node --check "$hook_file"
done < <(find .codex/hooks -type f \( -name '*.js' -o -name '*.cjs' \) -print)

node .codex/gsd-core/bin/gsd-tools.cjs --help >/dev/null

printf 'GSD Core %s is ready. Restart Codex from %s.\n' "$GSD_VERSION" "$repo_root"
