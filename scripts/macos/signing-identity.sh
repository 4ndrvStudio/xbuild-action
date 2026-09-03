#!/usr/bin/env bash

# Select only by the code-signing identity class required by the export method.
# Do not treat the parenthesized suffix in a certificate Common Name as Team ID:
# an Apple Development certificate issued to a user can contain Team Member ID
# there. Profile selection already locks the build to a Team ID, and Xcode
# validates that the selected certificate is authorized by those profiles.
xbuild_select_signing_identity() {
  local export_method="${1:?Usage: xbuild_select_signing_identity <export-method> <identities-file>}"
  local identities_file="${2:?Usage: xbuild_select_signing_identity <export-method> <identities-file>}"
  local identity

  while IFS= read -r identity; do
    case "$export_method:$identity" in
      development:Apple\ Development:*|development:iPhone\ Developer:*)
        printf '%s\n' "$identity"
        return 0
        ;;
      ad-hoc:Apple\ Distribution:*|ad-hoc:iPhone\ Distribution:*|app-store:Apple\ Distribution:*|app-store:iPhone\ Distribution:*)
        printf '%s\n' "$identity"
        return 0
        ;;
    esac
  done < <(sed -n 's/^[[:space:]]*[0-9][0-9]*) [0-9A-F]* "\(.*\)"$/\1/p' "$identities_file")

  return 1
}
