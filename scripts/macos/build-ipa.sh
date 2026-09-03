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
export_options="$work_dir/ExportOptions.plist"
result_bundle="$work_dir/archive.xcresult"

python3 scripts/macos/make-export-options.py "$signing_map" "$export_options"

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
  COMPILER_INDEX_STORE_ENABLE=NO \
  archive 2>&1 | tee "$log_dir/xcodebuild-archive.log"
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

echo "::group::xcodebuild exportArchive"
set +e
NSUnbufferedIO=YES xcodebuild \
  -exportArchive \
  -archivePath "$archive_path" \
  -exportPath "$output_dir" \
  -exportOptionsPlist "$export_options" \
  2>&1 | tee "$log_dir/xcodebuild-export.log"
export_status=${PIPESTATUS[0]}
set -e
echo "::endgroup::"

if (( export_status != 0 )); then
  echo "::error title=IPA export failed::xcodebuild -exportArchive exited with status $export_status. See xcodebuild-export.log."
  exit "$export_status"
fi

ipa_count="$(find "$output_dir" -type f -name '*.ipa' -print | wc -l | tr -d ' ')"
if [[ "$ipa_count" == "0" ]]; then
  echo "::error title=IPA missing::Export completed without producing an .ipa file."
  exit 42
fi

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

manifest = {
    "schema_version": 1,
    "build_id": os.environ.get("XBUILD_BUILD_ID", ""),
    "github_run_id": os.environ.get("GITHUB_RUN_ID", ""),
    "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    "source_sha256": os.environ.get("XBUILD_SOURCE_SHA256", ""),
    "container": os.environ.get("XBUILD_CONTAINER_PATH", ""),
    "container_kind": os.environ.get("XBUILD_CONTAINER_KIND", ""),
    "scheme": os.environ.get("XBUILD_SCHEME", ""),
    "configuration": os.environ.get("XBUILD_CONFIGURATION", ""),
    "bundle_identifier": os.environ.get("XBUILD_BUNDLE_IDENTIFIER", ""),
    "export_method": signing["export_method"],
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
