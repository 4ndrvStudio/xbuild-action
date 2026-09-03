#!/usr/bin/env bash

set -euo pipefail

source_root="${1:?Usage: configure-signing.sh <source-root> <signing-map> [detected-build-json]}"
signing_map="${2:?Usage: configure-signing.sh <source-root> <signing-map> [detected-build-json]}"
detected_build="${3:-${XBUILD_DETECTED_BUILD_FILE:-}}"

if ! ruby -e 'require "xcodeproj"' >/dev/null 2>&1; then
  echo "Installing the xcodeproj Ruby gem used to configure target-level signing..."
  sudo gem install xcodeproj --no-document
fi

ruby scripts/macos/configure-signing.rb "$source_root" "$signing_map" "$detected_build"
