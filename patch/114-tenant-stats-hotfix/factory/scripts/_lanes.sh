# shellcheck shell=bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# One definition of "which compiled lanes exist", sourced by every driver.
# No shebang: this file is sourced, never executed, so the directive above tells shellcheck which
# shell to assume.
#
# Why this file exists: the lane list was hard-coded in four scripts. Adding the DynamoDB lane
# meant editing all four, and the API Gateway lane would have meant editing them again — with
# nothing catching the one that was missed. A driver that does not know about a lane does not
# fail; it silently treats the kit as a host-config kit and goes looking for a host snapshot the
# kit does not have.
#
# Sourced, not executed: it defines functions and sets no traps or options.

# Directory-name prefix -> lane. The prefix is what the compiler emits under lib/compiled/.
OC_LANE_PREFIXES=('fn-*' 'ddbnew-*' 'ddb-*' 'apigw-*')

# Print the compiled entry directory for a kit, or the host-config directory when no
# control-plane lane matches.
oc_kit_entry() {
  local kit="$1" dir pattern
  for pattern in "${OC_LANE_PREFIXES[@]}"; do
    dir="$(find "$kit/lib/compiled" -maxdepth 1 -type d -name "$pattern" 2>/dev/null \
      | head -1 || true)"
    [[ -n "$dir" ]] && { printf '%s' "$dir"; return 0; }
  done
  printf '%s' "$kit/lib/compiled"
}

# Print the lane name for a kit: lambda | ddb | apigw | host-config.
oc_kit_lane() {
  local entry
  entry="$(oc_kit_entry "$1")"
  case "$(basename "$entry")" in
    fn-*)     printf 'lambda' ;;
    ddbnew-*) printf 'ddbnew' ;;
    ddb-*)    printf 'ddb' ;;
    apigw-*)  printf 'apigw' ;;
    *)        printf 'host-config' ;;
  esac
}

# The manifest key each lane declares its target in, so a driver can validate the right one
# instead of reading lambda_functions[0] for every non-host kit (which yielded null and failed a
# perfectly appliable DynamoDB kit).
oc_lane_manifest_key() {
  case "$1" in
    lambda) printf 'lambda_functions' ;;
    ddb)    printf 'ddb_settings' ;;
    ddbnew) printf 'ddb_tables' ;;
    apigw)  printf 'api_routes' ;;
    *)      printf '' ;;
  esac
}

# A host-config stage takes environment.json (it needs the host snapshot and the lease bucket);
# a control-plane stage takes its target from environment variables.
oc_lane_needs_envjson() {
  [[ "$(oc_kit_lane "$1")" == "host-config" ]]
}
