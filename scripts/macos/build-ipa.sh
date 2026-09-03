#!/usr/bin/env bash

set -euo pipefail

container_kind="${XBUILD_CONTAINER_KIND:?XBUILD_CONTAINER_KIND is required}"
container_path="${XBUILD_CONTAINER_PATH:?XBUILD_CONTAINER_PATH is required}"
scheme="${XBUILD_SCHEME:?XBUILD_SCHEME is required}"
configuration="${XBUILD_CONFIGURATION:?XBUILD_CONFIGURATION is required}"
signing_map="${XBUILD_SIGNING_MAP:?XBUILD_SIGNING_MAP is required}"
work_dir="${XBUILD_WORK_DIR:?XBUILD_WORK_DIR is required}"
output_dir="${XBUILD_OUTPUT_DIR:?XBUILD_OUTPUT_DIR is required}"
log_dir="${XBUILD_LOG_DIR:?XBUILD_LOG_DIR is required}"
dual_export="${XBUILD_EXPORT_APPSTORE:-${XBUILD_UPLOAD_TESTFLIGHT:-false}}"
export_method="${XBUILD_EXPORT_METHOD:?XBUILD_EXPORT_METHOD is required}"
export_compliance="${XBUILD_EXPORT_COMPLIANCE:-preserve}"
export_compliance_code="${XBUILD_EXPORT_COMPLIANCE_CODE:-}"

case "$container_kind" in
  workspace) container_args=(-workspace "$container_path") ;;
  project) container_args=(-project "$container_path") ;;
  *)
    echo "::error title=Invalid Xcode container::Unknown container kind '$container_kind'."
    exit 40
    ;;
esac

mkdir -p "$output_dir" "$log_dir"
archive_path="$work_dir/app.xcarchive"
result_bundle="$work_dir/archive.xcresult"
archive_overrides=(COMPILER_INDEX_STORE_ENABLE=NO)

redact_export_compliance_code() {
  python3 -u -c '
import os
import sys

value = os.environ.get("XBUILD_EXPORT_COMPLIANCE_CODE", "")
for line in sys.stdin:
    sys.stdout.write(line.replace(value, "[REDACTED]") if value else line)
'
}

compliance_applies=false
if [[ "$export_method" == "app-store" || "$dual_export" == "true" ]]; then
  compliance_applies=true
  source_dir="${XBUILD_SOURCE_DIR:?XBUILD_SOURCE_DIR is required for App Store export compliance}"
  detected_build="${XBUILD_DETECTED_BUILD_FILE:?XBUILD_DETECTED_BUILD_FILE is required for App Store export compliance}"
  python3 scripts/macos/configure-export-compliance.py patch \
    --source-root "$source_dir" \
    --detected-build "$detected_build" \
    --mode "$export_compliance" \
    --compliance-code "$export_compliance_code"
  case "$export_compliance" in
    exempt)
      archive_overrides+=(
        "INFOPLIST_KEY_ITSAppUsesNonExemptEncryption=NO"
        "INFOPLIST_KEY_ITSEncryptionExportComplianceCode="
      )
      ;;
    non-exempt)
      archive_overrides+=(
        "INFOPLIST_KEY_ITSAppUsesNonExemptEncryption=YES"
        "INFOPLIST_KEY_ITSEncryptionExportComplianceCode=$export_compliance_code"
      )
      ;;
    preserve) ;;
    *)
      echo "::error title=Invalid export compliance mode::Expected exempt, non-exempt, or preserve."
      exit 45
      ;;
  esac
fi

if [[ "$dual_export" == "true" ]]; then
  source_dir="${XBUILD_SOURCE_DIR:?XBUILD_SOURCE_DIR is required for dual export}"
  detected_build="${XBUILD_DETECTED_BUILD_FILE:?XBUILD_DETECTED_BUILD_FILE is required for dual export}"
  build_number_file="$work_dir/project-build-number.txt"
  python3 scripts/macos/set-build-number.py resolve \
    --source-root "$source_dir" \
    --detected-build "$detected_build" \
    --output "$build_number_file"
  build_number="$(tr -d '\r\n' < "$build_number_file")"
  export XBUILD_EFFECTIVE_BUILD_NUMBER="$build_number"
  python3 scripts/macos/set-build-number.py patch \
    --source-root "$source_dir" \
    --detected-build "$detected_build" \
    --build-number "$build_number"
  archive_overrides+=(
    "CURRENT_PROJECT_VERSION=$build_number"
    "INFOPLIST_KEY_CFBundleVersion=$build_number"
  )
fi

echo "::group::xcodebuild archive"
echo "Archiving scheme '$scheme' ($configuration) from $(basename "$container_path")..."
set +e
NSUnbufferedIO=YES xcodebuild \
  "${container_args[@]}" \
  -scheme "$scheme" \
  -configuration "$configuration" \
  -sdk iphoneos \
  -destination 'generic/platform=iOS' \
  -archivePath "$archive_path" \
  -resultBundlePath "$result_bundle" \
  "${archive_overrides[@]}" \
  archive 2>&1 | redact_export_compliance_code | tee "$log_dir/xcodebuild-archive.log"
archive_status=${PIPESTATUS[0]}
set -e
echo "::endgroup::"

if (( archive_status != 0 )); then
  echo "::error title=Xcode archive failed::xcodebuild archive exited with status $archive_status. See xcodebuild-archive.log."
  exit "$archive_status"
fi
if [[ ! -d "$archive_path" ]]; then
  echo "::error title=Archive missing::xcodebuild succeeded but did not create an .xcarchive."
  exit 41
fi

if [[ "$dual_export" == "true" ]]; then
  python3 scripts/macos/set-build-number.py verify \
    --archive "$archive_path" \
    --build-number "$build_number"
fi

if [[ "$compliance_applies" == "true" ]]; then
  python3 scripts/macos/configure-export-compliance.py verify \
    --archive "$archive_path" \
    --mode "$export_compliance" \
    --compliance-code "$export_compliance_code"
fi

export_archive() {
  local label="$1"
  local slug="$2"
  local map_path="$3"
  local destination="$4"
  local log_name="$5"
  local suffix="$6"
  local env_name="$7"
  local export_options="$work_dir/ExportOptions-$slug.plist"

  mkdir -p "$destination"
  python3 scripts/macos/make-export-options.py "$map_path" "$export_options"
  echo "::group::xcodebuild exportArchive ($label)"
  set +e
  NSUnbufferedIO=YES xcodebuild \
    -exportArchive \
    -archivePath "$archive_path" \
    -exportPath "$destination" \
    -exportOptionsPlist "$export_options" \
    2>&1 | redact_export_compliance_code | tee "$log_dir/$log_name"
  local export_status=${PIPESTATUS[0]}
  set -e
  echo "::endgroup::"

  if (( export_status != 0 )); then
    echo "::error title=IPA export failed::$label export exited with status $export_status. See $log_name."
    exit "$export_status"
  fi

  local ipas=()
  while IFS= read -r -d '' ipa; do
    ipas+=("$ipa")
  done < <(find "$destination" -type f -name '*.ipa' -print0)
  if (( ${#ipas[@]} == 0 )); then
    echo "::error title=IPA missing::$label export completed without producing an .ipa file."
    exit 42
  fi

  if [[ -n "$suffix" ]]; then
    local renamed=()
    local target
    for ipa in "${ipas[@]}"; do
      target="${ipa%.ipa}-$suffix.ipa"
      mv -- "$ipa" "$target"
      renamed+=("$target")
    done
    ipas=("${renamed[@]}")
  fi

  if [[ -n "$env_name" ]]; then
    if (( ${#ipas[@]} != 1 )); then
      echo "::error title=Ambiguous IPA export::$label export produced ${#ipas[@]} IPA files; dual export requires exactly one."
      exit 42
    fi
    if [[ -z "${GITHUB_ENV:-}" ]]; then
      echo "::error title=GitHub environment unavailable::GITHUB_ENV is required for dual export."
      exit 42
    fi
    printf '%s=%s\n' "$env_name" "${ipas[0]}" >> "$GITHUB_ENV"
  fi
}

if [[ "$dual_export" == "true" ]]; then
  adhoc_signing_map="${XBUILD_SIGNING_MAP_ADHOC:?XBUILD_SIGNING_MAP_ADHOC is required}"
  appstore_signing_map="${XBUILD_SIGNING_MAP_APPSTORE:?XBUILD_SIGNING_MAP_APPSTORE is required}"
  export_archive "Ad Hoc" "ad-hoc" "$adhoc_signing_map" "$output_dir/ad-hoc" "xcodebuild-export-ad-hoc.log" "AdHoc" "XBUILD_ADHOC_IPA"
  export_archive "App Store" "app-store" "$appstore_signing_map" "$output_dir/app-store" "xcodebuild-export-app-store.log" "AppStore" "XBUILD_APPSTORE_IPA"
else
  export_archive "$XBUILD_EXPORT_METHOD" "single" "$signing_map" "$output_dir" "xcodebuild-export.log" "" ""
fi

ipa_count="$(find "$output_dir" -type f -name '*.ipa' -print | wc -l | tr -d ' ')"

python3 - "$output_dir" "$signing_map" <<'PY'
import hashlib
import json
import os
import pathlib
import subprocess
import sys

output_dir = pathlib.Path(sys.argv[1])
signing = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
ipas = []
for path in sorted(output_dir.rglob("*.ipa")):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    ipas.append(
        {
            "file": str(path.relative_to(output_dir)),
            "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    )

try:
    xcode_version = subprocess.check_output(["xcodebuild", "-version"], text=True).strip()
except Exception:
    xcode_version = "unknown"

dual_export_value = (
    os.environ.get("XBUILD_EXPORT_APPSTORE")
    or os.environ.get("XBUILD_UPLOAD_TESTFLIGHT", "false")
)
dual_export = dual_export_value == "true"
app_store_exported = dual_export or os.environ.get("XBUILD_EXPORT_METHOD") == "app-store"

manifest = {
    "schema_version": 3 if dual_export else 1,
    "build_id": os.environ.get("XBUILD_BUILD_ID", ""),
    "github_run_id": os.environ.get("GITHUB_RUN_ID", ""),
    "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    "source_sha256": os.environ.get("XBUILD_SOURCE_SHA256", ""),
    "container": os.environ.get("XBUILD_CONTAINER_PATH", ""),
    "container_kind": os.environ.get("XBUILD_CONTAINER_KIND", ""),
    "scheme": os.environ.get("XBUILD_SCHEME", ""),
    "configuration": os.environ.get("XBUILD_CONFIGURATION", ""),
    "bundle_identifier": os.environ.get("XBUILD_BUNDLE_IDENTIFIER", ""),
    "export_method": (
        "ad-hoc+app-store"
        if dual_export
        else signing["export_method"]
    ),
    "build_number": os.environ.get("XBUILD_EFFECTIVE_BUILD_NUMBER", ""),
    "app_store_exported": app_store_exported,
    "testflight_requested": False,
    "testflight_uploaded": False,
    "testflight_upload_deferred": app_store_exported,
    "export_compliance": (
        os.environ.get("XBUILD_EXPORT_COMPLIANCE", "preserve")
        if app_store_exported
        else "not-applicable"
    ),
    "team_id": signing["team_id"],
    "xcode_version": xcode_version,
    "ipas": ipas,
}
(output_dir / "build-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

echo "Successfully exported $ipa_count IPA file(s):"
find "$output_dir" -type f -name '*.ipa' -print
