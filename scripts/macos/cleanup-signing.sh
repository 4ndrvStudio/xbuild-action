#!/usr/bin/env bash

set -euo pipefail

signing_dir="${XBUILD_SIGNING_DIR:-}"
if [[ -z "$signing_dir" ]]; then
  echo "No signing directory was configured."
  exit 0
fi

manifest="$signing_dir/installed-profiles.txt"
legacy_profiles_home="$HOME/Library/MobileDevice/Provisioning Profiles"
xcode_profiles_home="$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"
if [[ -f "$manifest" ]]; then
  while IFS= read -r installed_profile; do
    case "$installed_profile" in
      "$legacy_profiles_home"/*.mobileprovision|"$xcode_profiles_home"/*.mobileprovision)
        rm -f -- "$installed_profile"
        ;;
      *)
        echo "::warning title=Profile cleanup skipped::Unexpected profile path: $installed_profile"
        ;;
    esac
  done < "$manifest"
fi

keychain_path="$signing_dir/xbuild.keychain-db"
if [[ -f "$keychain_path" ]]; then
  security delete-keychain "$keychain_path" >/dev/null 2>&1 || true
fi

# Restore exactly the keychain search list that existed before this build.
original_keychains="$signing_dir/original-keychains.txt"
if [[ -f "$original_keychains" ]]; then
  keychain_search_list=()
  while IFS= read -r existing_keychain; do
    [[ -n "$existing_keychain" ]] && keychain_search_list+=("$existing_keychain")
  done < "$original_keychains"
  if (( ${#keychain_search_list[@]} > 0 )); then
    security list-keychains -d user -s "${keychain_search_list[@]}" || true
  fi
fi

case "$signing_dir" in
  "${RUNNER_TEMP%/}"/xbuild-*/signing) rm -rf -- "$signing_dir" ;;
  *) echo "::warning title=Signing cleanup skipped::Unexpected signing directory: $signing_dir" ;;
esac

echo "Removed temporary keychain and provisioning profiles."
