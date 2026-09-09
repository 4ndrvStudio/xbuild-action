#!/usr/bin/env bash

set -euo pipefail

source_root="${1:?Usage: install-cocoapods.sh <source-root> <log-dir> [project-hint]}"
log_dir="${2:?Usage: install-cocoapods.sh <source-root> <log-dir> [project-hint]}"
project_hint="${3:-${XBUILD_PROJECT_HINT:-}}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source_root="$(cd "$source_root" && pwd -P)"
mkdir -p "$log_dir"

podfiles=()
while IFS= read -r -d '' file; do
  podfiles+=("$file")
done < <(find "$source_root" -type f -name Podfile \
  ! -path '*/Pods/*' \
  ! -path '*/vendor/*' \
  ! -path '*/.bundle/*' \
  -print0)

if (( ${#podfiles[@]} == 0 )); then
  echo "No Podfile was found; CocoaPods is not required for this export."
  exit 0
fi

selected_podfiles=()
if [[ -n "$project_hint" ]]; then
  normalized_hint="$(printf '%s' "$project_hint" | tr '\\' '/')"
  case "$normalized_hint" in
    /*|[A-Za-z]:*|../*|*/../*|*/..)
      echo "::error title=Invalid project hint::Cannot select a Podfile outside the uploaded source."
      exit 19
      ;;
  esac

  hint_parent="$(dirname "$source_root/$normalized_hint")"
  if [[ ! -d "$hint_parent" ]]; then
    echo "::error title=Project hint not found::Cannot locate the selected Xcode container's directory."
    exit 19
  fi
  current_dir="$(cd "$hint_parent" && pwd -P)"
  while :; do
    for podfile in "${podfiles[@]}"; do
      pod_dir="$(cd "$(dirname "$podfile")" && pwd -P)"
      if [[ "$pod_dir" == "$current_dir" ]]; then
        selected_podfiles+=("$podfile")
        break 2
      fi
    done

    [[ "$current_dir" == "$source_root" ]] && break
    parent_dir="$(dirname "$current_dir")"
    if [[ "$parent_dir" != "$source_root" && "$parent_dir" != "$source_root/"* ]]; then
      break
    fi
    current_dir="$parent_dir"
  done
elif (( ${#podfiles[@]} == 1 )); then
  selected_podfiles=("${podfiles[0]}")
else
  for podfile in "${podfiles[@]}"; do
    [[ "$(cd "$(dirname "$podfile")" && pwd -P)" == "$source_root" ]] &&
      selected_podfiles+=("$podfile")
  done
  if (( ${#selected_podfiles[@]} != 1 )); then
    echo "::error title=Multiple Podfiles::Set project_hint so XBuild can choose the Podfile associated with the selected project."
    exit 19
  fi
fi

if (( ${#selected_podfiles[@]} == 0 )); then
  echo "Found ${#podfiles[@]} Podfile(s), but none belongs to the selected project; CocoaPods is not required for this build."
  exit 0
fi

if (( ${#podfiles[@]} > ${#selected_podfiles[@]} )); then
  echo "Ignoring $((${#podfiles[@]} - ${#selected_podfiles[@]})) unrelated nested Podfile(s)."
fi
podfiles=("${selected_podfiles[@]}")
echo "Selected ${#podfiles[@]} Podfile(s). CocoaPods will run only on the macOS runner."

minimum_cocoapods_version="1.7.2"

cocoapods_version_supported() {
  local version="$1"
  ruby -rrubygems -e \
    'exit(Gem::Version.new(ARGV.fetch(0)) >= Gem::Version.new(ARGV.fetch(1)) ? 0 : 1)' \
    "$version" "$minimum_cocoapods_version" >/dev/null 2>&1
}

install_cocoapods_version() {
  local required_version="$1"
  if [[ -n "$required_version" ]]; then
    if gem list --installed --exact cocoapods --version "$required_version" 2>/dev/null | grep -q '^true$'; then
      return
    fi
    echo "Installing CocoaPods $required_version from Podfile.lock..."
    sudo gem install cocoapods --version "$required_version" --no-document
  elif ! command -v pod >/dev/null 2>&1; then
    echo "Installing the current CocoaPods release..."
    sudo gem install cocoapods --no-document
  else
    local installed_version
    installed_version="$(pod --version 2>/dev/null | awk 'NF { version=$0 } END { print version }' | tr -d '\r')"
    if ! cocoapods_version_supported "$installed_version"; then
      echo "Runner CocoaPods $installed_version is older than the CDN minimum $minimum_cocoapods_version; installing the current release..."
      sudo gem install cocoapods --no-document
    fi
  fi
}

gemfile_cocoapods_status() {
  local pod_dir="$1"
  local lockfile=""
  [[ -f "$pod_dir/Gemfile.lock" ]] && lockfile="$pod_dir/Gemfile.lock"

  ruby -rbundler - "$pod_dir/Gemfile" "$lockfile" <<'RUBY'
begin
  gemfile = ARGV.fetch(0)
  lockfile = ARGV.fetch(1)
  lockfile = nil if lockfile.empty?
  definition = Bundler::Definition.build(gemfile, lockfile, nil)
  has_cocoapods = definition.dependencies.any? do |dependency|
    dependency.name.casecmp('cocoapods').zero?
  end
  # Keep the "not declared" result distinct from Ruby/Bundler startup errors,
  # which can exit before this script body runs (commonly with status 1).
  exit(has_cocoapods ? 0 : 10)
rescue StandardError, ScriptError => e
  warn "Could not inspect Gemfile: #{e.class}: #{e.message}"
  exit 11
end
RUBY
}

index=0
for podfile in "${podfiles[@]}"; do
  index=$((index + 1))
  pod_dir="$(cd "$(dirname "$podfile")" && pwd -P)"
  pod_log="$log_dir/cocoapods-$index.log"
  required_version=""

  echo "::group::CocoaPods $index/${#podfiles[@]} — $pod_dir"

  if [[ -f "$pod_dir/Podfile.lock" ]]; then
    required_version="$(awk '/^COCOAPODS: / { print $2; exit }' "$pod_dir/Podfile.lock" | tr -d '\r')"
  fi

  pod_command=()
  use_bundler=false
  if [[ -f "$pod_dir/Gemfile" ]]; then
    if ! command -v bundle >/dev/null 2>&1; then
      sudo gem install bundler --no-document
    fi
    if gemfile_cocoapods_status "$pod_dir"; then
      use_bundler=true
    else
      gemfile_status=$?
      if (( gemfile_status == 10 )); then
        echo "Gemfile does not declare CocoaPods; using Podfile.lock or the runner CocoaPods instead."
      else
        echo "::endgroup::"
        echo "::error title=Gemfile inspection failed::Could not safely determine whether the Gemfile pins CocoaPods. Fix the Gemfile/Gemfile.lock or remove it from the export before building."
        exit 20
      fi
    fi
  fi

  if [[ "$use_bundler" == "true" ]]; then
    echo "Gemfile declares CocoaPods; using its bundled version."
    if ! command -v bundle >/dev/null 2>&1; then
      sudo gem install bundler --no-document
    fi
    bundle_dir="${XBUILD_WORK_DIR:-${RUNNER_TEMP:-/tmp}/xbuild}/bundle-$index"
    (
      cd "$pod_dir"
      bundle config set --local path "$bundle_dir"
      bundle install --jobs 4 --retry 3
    ) 2>&1 | tee "$log_dir/bundle-$index.log"
    pod_command=(bundle exec pod)
  else
    install_cocoapods_version "$required_version"
    if [[ -n "$required_version" ]]; then
      pod_command=(pod "_${required_version}_")
    else
      pod_command=(pod)
    fi
  fi

  if [[ -n "$required_version" && "$use_bundler" != "true" ]]; then
    echo "Podfile.lock requests CocoaPods $required_version."
  fi

  pod_version_output="$(
    cd "$pod_dir"
    "${pod_command[@]}" --version
  )"
  printf '%s\n' "$pod_version_output"
  resolved_pod_version="$(printf '%s\n' "$pod_version_output" | awk 'NF { version=$0 } END { print version }' | tr -d '\r')"
  if ! cocoapods_version_supported "$resolved_pod_version"; then
    if [[ "$use_bundler" == "true" ]]; then
      echo "::warning title=Legacy bundled CocoaPods::Gemfile/Gemfile.lock resolves CocoaPods $resolved_pod_version, which predates CDN support. XBuild will honor the pin and keep the legacy public Specs source for this build."
      normalize_public_specs=false
    elif [[ -n "$required_version" ]]; then
      echo "::warning title=Legacy locked CocoaPods::Podfile.lock requests CocoaPods $resolved_pod_version, which predates CDN support. XBuild will honor that version and keep the legacy public Specs source for this build."
      normalize_public_specs=false
    else
      echo "::endgroup::"
      echo "::error title=CocoaPods is too old::Resolved CocoaPods $resolved_pod_version, but the normalized CDN source requires $minimum_cocoapods_version or newer."
      exit 20
    fi
  else
    normalize_public_specs=true
  fi

  if [[ "$normalize_public_specs" == "true" ]]; then
    python3 "$script_dir/normalize-podfile-sources.py" "$source_root" "$podfile"
  else
    echo "Podfile source normalization skipped to remain compatible with explicitly selected CocoaPods $resolved_pod_version."
  fi

  set +e
  (
    cd "$pod_dir"
    "${pod_command[@]}" install
  ) 2>&1 | tee "$pod_log"
  pod_status=${PIPESTATUS[0]}
  set -e

  if (( pod_status != 0 )); then
    echo "The first pod install failed. Retrying once with --repo-update..." | tee -a "$pod_log"
    set +e
    (
      cd "$pod_dir"
      "${pod_command[@]}" install --repo-update
    ) 2>&1 | tee -a "$pod_log"
    pod_status=${PIPESTATUS[0]}
    set -e
  fi

  echo "::endgroup::"
  if (( pod_status != 0 )); then
    echo "::error title=CocoaPods failed::pod install failed in '$pod_dir'. Expand the CocoaPods step or open $pod_log for the complete error."
    exit "$pod_status"
  fi
done

workspace_count="$(find "$source_root" -type d -name '*.xcworkspace' \
  ! -path '*/Pods/*' \
  ! -path '*.xcodeproj/project.xcworkspace' \
  -print | wc -l | tr -d ' ')"
echo "CocoaPods completed successfully; $workspace_count build workspace(s) are now available."
