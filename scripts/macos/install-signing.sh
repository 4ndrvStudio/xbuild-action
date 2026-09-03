#!/usr/bin/env bash

set -euo pipefail
umask 077
source scripts/macos/signing-identity.sh

bundle_ids_file="${1:?Usage: install-signing.sh <bundle-ids-file> <github-env-file>}"
github_env_file="${2:?Usage: install-signing.sh <bundle-ids-file> <github-env-file>}"
signing_dir="${XBUILD_SIGNING_DIR:?XBUILD_SIGNING_DIR is required}"
export_method="${XBUILD_EXPORT_METHOD:?XBUILD_EXPORT_METHOD is required}"
signing_set="${XBUILD_SIGNING_SET:-}"
upload_testflight="${XBUILD_UPLOAD_TESTFLIGHT:-false}"

mkdir -p "$signing_dir"

case "$export_method" in
  development) mode_suffix="DEVELOPMENT" ;;
  ad-hoc) mode_suffix="ADHOC" ;;
  app-store) mode_suffix="APPSTORE" ;;
  *)
    echo "::error title=Invalid export method::Expected development, ad-hoc, or app-store."
    exit 32
    ;;
esac

if [[ "$upload_testflight" == "true" ]]; then
  if [[ "$export_method" != "ad-hoc" ]]; then
    echo "::error title=Invalid dual signing::TestFlight mode must use ad-hoc as the primary export method."
    exit 32
  fi
  if [[ -z "$signing_set" || ! "$signing_set" =~ ^[A-Z0-9][A-Z0-9_]{0,31}$ ]]; then
    echo "::error title=Named signing set required::TestFlight mode requires a valid named signing set."
    exit 32
  fi
  secret_prefix="XBUILD_SIGNING_${signing_set}"
  certificate_base64="${XBUILD_DUAL_ADHOC_CERTIFICATE_BASE64:-}"
  certificate_password="${XBUILD_DUAL_ADHOC_CERTIFICATE_PASSWORD:-}"
  profiles_base64="${XBUILD_DUAL_ADHOC_PROFILES_BASE64:-}"
  appstore_certificate_base64="${XBUILD_DUAL_APPSTORE_CERTIFICATE_BASE64:-}"
  appstore_certificate_password="${XBUILD_DUAL_APPSTORE_CERTIFICATE_PASSWORD:-}"
  appstore_profiles_base64="${XBUILD_DUAL_APPSTORE_PROFILES_BASE64:-}"
  team_id_override="${XBUILD_SET_TEAM_ID_OVERRIDE:-}"
  certificate_hint="${secret_prefix}_CERTIFICATE_ADHOC_BASE64 and ${secret_prefix}_CERTIFICATE_APPSTORE_BASE64"
  profiles_hint="${secret_prefix}_PROVISIONING_PROFILES_ADHOC_BASE64 and ${secret_prefix}_PROVISIONING_PROFILES_APPSTORE_BASE64"
  if [[ -z "$appstore_certificate_base64" ]]; then
    echo "::error title=App Store certificate missing::Configure $certificate_hint."
    exit 32
  fi
  if [[ -z "$appstore_profiles_base64" ]]; then
    echo "::error title=App Store profiles missing::Configure $profiles_hint."
    exit 32
  fi
  if [[ "$certificate_base64" != "$appstore_certificate_base64" ||
        "$certificate_password" != "$appstore_certificate_password" ]]; then
    echo "::error title=Distribution certificate mismatch::Ad Hoc and App Store must use the same Distribution P12 and password for a one-archive build. Re-save this Apple Team in XBuild."
    exit 32
  fi
  echo "Using named signing set '$signing_set' for Ad Hoc and App Store exports."
elif [[ -n "$signing_set" ]]; then
  if [[ ! "$signing_set" =~ ^[A-Z0-9][A-Z0-9_]{0,31}$ ]]; then
    echo "::error title=Invalid signing set::Signing set keys must match [A-Z0-9][A-Z0-9_]{0,31}."
    exit 32
  fi
  secret_prefix="XBUILD_SIGNING_${signing_set}"
  certificate_base64="${XBUILD_SET_MODE_CERTIFICATE_BASE64:-${XBUILD_SET_COMMON_CERTIFICATE_BASE64:-}}"
  certificate_password="${XBUILD_SET_MODE_CERTIFICATE_PASSWORD:-${XBUILD_SET_COMMON_CERTIFICATE_PASSWORD:-}}"
  profiles_base64="${XBUILD_SET_MODE_PROFILES_BASE64:-${XBUILD_SET_MODE_PROFILE_BASE64:-${XBUILD_SET_COMMON_PROFILES_BASE64:-${XBUILD_SET_COMMON_PROFILE_BASE64:-}}}}"
  team_id_override="${XBUILD_SET_TEAM_ID_OVERRIDE:-}"
  certificate_hint="${secret_prefix}_CERTIFICATE_${mode_suffix}_BASE64 or ${secret_prefix}_CERTIFICATE_BASE64"
  profiles_hint="${secret_prefix}_PROVISIONING_PROFILES_${mode_suffix}_BASE64 (or a singular/common profile secret in the same set)"
  echo "Using named signing set '$signing_set'."
else
  certificate_base64="${XBUILD_MODE_CERTIFICATE_BASE64:-${XBUILD_COMMON_CERTIFICATE_BASE64:-}}"
  certificate_password="${XBUILD_MODE_CERTIFICATE_PASSWORD:-${XBUILD_COMMON_CERTIFICATE_PASSWORD:-}}"
  profiles_base64="${XBUILD_MODE_PROFILES_BASE64:-${XBUILD_MODE_PROFILE_BASE64:-${XBUILD_COMMON_PROFILES_BASE64:-${XBUILD_COMMON_PROFILE_BASE64:-}}}}"
  team_id_override="${XBUILD_LEGACY_TEAM_ID_OVERRIDE:-${XBUILD_TEAM_ID_OVERRIDE:-}}"
  certificate_hint="the mode-specific IOS_CERTIFICATE_*_BASE64 secret or IOS_CERTIFICATE_BASE64"
  profiles_hint="IOS_PROFILE_*_BASE64, IOS_PROVISIONING_PROFILES_*_BASE64, or a common profile secret"
  echo "Using legacy IOS_* signing secrets."
fi

if [[ -z "$certificate_base64" ]]; then
  echo "::error title=Signing certificate missing::Configure $certificate_hint."
  exit 32
fi
if [[ -z "$profiles_base64" ]]; then
  echo "::error title=Provisioning profile missing::Configure $profiles_hint."
  exit 32
fi

if [[ "$upload_testflight" == "true" ]]; then
  mkdir -p "$signing_dir/raw-profiles-ad-hoc" "$signing_dir/profile-metadata-ad-hoc" "$signing_dir/raw-profiles-app-store" "$signing_dir/profile-metadata-app-store"
  raw_profiles_dir="$signing_dir/raw-profiles-ad-hoc"
  metadata_dir="$signing_dir/profile-metadata-ad-hoc"
else
  mkdir -p "$signing_dir/raw-profiles" "$signing_dir/profile-metadata"
  raw_profiles_dir="$signing_dir/raw-profiles"
  metadata_dir="$signing_dir/profile-metadata"
fi

certificate_path="$signing_dir/certificate.p12"
profiles_blob="$signing_dir/profiles.payload"
appstore_profiles_blob="$signing_dir/profiles-app-store.payload"

# Decode with Python so both wrapped and unwrapped Base64 produced by Windows
# setup tools are accepted without writing a secret to the command line.
XBUILD_DECODE_VALUE="$certificate_base64" XBUILD_DECODE_PATH="$certificate_path" python3 - <<'PY'
import base64
import os
import pathlib

value = "".join(os.environ["XBUILD_DECODE_VALUE"].split())
try:
    payload = base64.b64decode(value, validate=True)
except Exception as exc:
    print(f"::error title=Certificate decode failed::{exc}")
    raise SystemExit(32)
pathlib.Path(os.environ["XBUILD_DECODE_PATH"]).write_bytes(payload)
PY

decode_profile_payload() {
  local encoded="$1"
  local destination="$2"
  XBUILD_DECODE_VALUE="$encoded" XBUILD_DECODE_PATH="$destination" python3 - <<'PY'
import base64
import os
import pathlib

value = "".join(os.environ["XBUILD_DECODE_VALUE"].split())
try:
    payload = base64.b64decode(value, validate=True)
except Exception as exc:
    print(f"::error title=Profile decode failed::{exc}")
    raise SystemExit(32)
pathlib.Path(os.environ["XBUILD_DECODE_PATH"]).write_bytes(payload)
PY
}

# The plural secret is normally a ZIP, while singular secrets may be a raw
# .mobileprovision. Extract only provisioning-profile entries.
extract_profiles() {
  local payload="$1"
  local destination="$2"
  python3 - "$payload" "$destination" <<'PY'
import pathlib
import shutil
import sys
import zipfile

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
if zipfile.is_zipfile(source):
    count = 0
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            if entry.is_dir() or pathlib.PurePosixPath(entry.filename).suffix.lower() != ".mobileprovision":
                continue
            count += 1
            with archive.open(entry) as source_file, (destination / f"{count:03d}.mobileprovision").open("wb") as target:
                shutil.copyfileobj(source_file, target)
    if count == 0:
        print("::error title=Profile ZIP is empty::No .mobileprovision file was found in the profile secret.")
        raise SystemExit(32)
else:
    shutil.copyfile(source, destination / "001.mobileprovision")
PY
}

decode_profile_metadata() {
  local raw_directory="$1"
  local metadata_directory="$2"
  local decoded_count=0
  while IFS= read -r -d '' profile; do
    decoded_count=$((decoded_count + 1))
    stem="$(basename "$profile" .mobileprovision)"
    if ! security cms -D -i "$profile" > "$metadata_directory/$stem.plist"; then
      echo "::error title=Invalid provisioning profile::Could not decode provisioning profile number $decoded_count."
      return 33
    fi
  done < <(find "$raw_directory" -type f -name '*.mobileprovision' -print0)
}

decode_profile_payload "$profiles_base64" "$profiles_blob"
extract_profiles "$profiles_blob" "$raw_profiles_dir"
decode_profile_metadata "$raw_profiles_dir" "$metadata_dir"

if [[ "$upload_testflight" == "true" ]]; then
  decode_profile_payload "$appstore_profiles_base64" "$appstore_profiles_blob"
  extract_profiles "$appstore_profiles_blob" "$signing_dir/raw-profiles-app-store"
  decode_profile_metadata "$signing_dir/raw-profiles-app-store" "$signing_dir/profile-metadata-app-store"

  adhoc_signing_map="$signing_dir/signing-map-ad-hoc.json"
  appstore_signing_map="$signing_dir/signing-map-app-store.json"
  python3 scripts/macos/select-signing.py \
    --metadata-dir "$metadata_dir" \
    --raw-dir "$raw_profiles_dir" \
    --bundle-ids "$bundle_ids_file" \
    --method ad-hoc \
    --team-id "$team_id_override" \
    --output "$adhoc_signing_map"
  python3 scripts/macos/select-signing.py \
    --metadata-dir "$signing_dir/profile-metadata-app-store" \
    --raw-dir "$signing_dir/raw-profiles-app-store" \
    --bundle-ids "$bundle_ids_file" \
    --method app-store \
    --team-id "$team_id_override" \
    --output "$appstore_signing_map"
  adhoc_team_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["team_id"])' "$adhoc_signing_map")"
  appstore_team_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["team_id"])' "$appstore_signing_map")"
  if [[ "$adhoc_team_id" != "$appstore_team_id" ]]; then
    echo "::error title=Signing team mismatch::Ad Hoc team $adhoc_team_id does not match App Store team $appstore_team_id."
    exit 33
  fi
  signing_map="$appstore_signing_map"
  team_id="$appstore_team_id"
else
  signing_map="$signing_dir/signing-map.json"
  python3 scripts/macos/select-signing.py \
    --metadata-dir "$metadata_dir" \
    --raw-dir "$raw_profiles_dir" \
    --bundle-ids "$bundle_ids_file" \
    --method "$export_method" \
    --team-id "$team_id_override" \
    --output "$signing_map"
  team_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["team_id"])' "$signing_map")"
fi
keychain_path="$signing_dir/xbuild.keychain-db"
keychain_password="$(openssl rand -base64 36 | tr -d '\r\n')"
original_keychains="$signing_dir/original-keychains.txt"
security list-keychains -d user | sed -e 's/^[[:space:]]*"//' -e 's/"[[:space:]]*$//' > "$original_keychains"

security create-keychain -p "$keychain_password" "$keychain_path"
security set-keychain-settings -lut 21600 "$keychain_path"
security unlock-keychain -p "$keychain_password" "$keychain_path"
security import "$certificate_path" \
  -k "$keychain_path" \
  -P "$certificate_password" \
  -A \
  -t cert \
  -f pkcs12
security set-key-partition-list \
  -S apple-tool:,apple:,codesign: \
  -s \
  -k "$keychain_password" \
  "$keychain_path" >/dev/null
keychain_search_list=("$keychain_path")
while IFS= read -r existing_keychain; do
  [[ -n "$existing_keychain" && "$existing_keychain" != "$keychain_path" ]] && keychain_search_list+=("$existing_keychain")
done < "$original_keychains"
security list-keychains -d user -s "${keychain_search_list[@]}"

identities_file="$signing_dir/identities.txt"
security find-identity -v -p codesigning "$keychain_path" > "$identities_file"
if ! certificate_identity="$(xbuild_select_signing_identity "$export_method" "$identities_file")"; then
  expected="Apple Development"
  [[ "$export_method" != "development" ]] && expected="Apple Distribution"
  echo "::error title=Signing identity missing::The P12 does not contain a valid $expected identity."
  exit 34
fi

maps_to_install=("$signing_map")
if [[ "$upload_testflight" == "true" ]]; then
  maps_to_install=("$adhoc_signing_map" "$appstore_signing_map")
fi

python3 - "$certificate_identity" "${maps_to_install[@]}" <<'PY'
import json
import pathlib
import sys

identity = sys.argv[1]
for argument in sys.argv[2:]:
    path = pathlib.Path(argument)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["certificate_identity"] = identity
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

legacy_profiles_home="$HOME/Library/MobileDevice/Provisioning Profiles"
xcode_profiles_home="$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles"
mkdir -p "$legacy_profiles_home" "$xcode_profiles_home"
installed_manifest="$signing_dir/installed-profiles.txt"
python3 - "${maps_to_install[@]}" <<'PY' | while IFS=$'\t' read -r uuid raw_path; do
import json
import sys

seen = set()
for argument in sys.argv[1:]:
    data = json.load(open(argument, encoding="utf-8"))
    for profile in data["profiles"].values():
        if profile["uuid"] not in seen:
            seen.add(profile["uuid"])
            print(f"{profile['uuid']}\t{profile['raw_path']}")
PY
  # Xcode 16+ manages profiles under UserData, while older Xcode releases and
  # some command-line tooling still discover the legacy MobileDevice cache.
  # Install into both locations so runner/Xcode overrides remain compatible.
  for profiles_home in "$legacy_profiles_home" "$xcode_profiles_home"; do
    installed_path="$profiles_home/$uuid.mobileprovision"
    cp "$raw_path" "$installed_path"
    printf '%s\n' "$installed_path" >> "$installed_manifest"
  done
done

printf 'XBUILD_KEYCHAIN_PATH=%s\n' "$keychain_path" >> "$github_env_file"
printf 'XBUILD_SIGNING_MAP=%s\n' "$signing_map" >> "$github_env_file"
if [[ "$upload_testflight" == "true" ]]; then
  printf 'XBUILD_SIGNING_MAP_ADHOC=%s\n' "$adhoc_signing_map" >> "$github_env_file"
  printf 'XBUILD_SIGNING_MAP_APPSTORE=%s\n' "$appstore_signing_map" >> "$github_env_file"
fi
printf 'XBUILD_TEAM_ID=%s\n' "$team_id" >> "$github_env_file"
printf 'XBUILD_CERTIFICATE_IDENTITY=%s\n' "$certificate_identity" >> "$github_env_file"

# The imported identity and installed profiles are all Xcode needs. Delete the
# decoded P12, its password-independent payload, and duplicate profile copies
# before any project-provided build phase can run.
rm -f -- "$certificate_path" "$profiles_blob" "$appstore_profiles_blob"
rm -rf -- \
  "$signing_dir/raw-profiles" \
  "$signing_dir/profile-metadata" \
  "$signing_dir/raw-profiles-ad-hoc" \
  "$signing_dir/profile-metadata-ad-hoc" \
  "$signing_dir/raw-profiles-app-store" \
  "$signing_dir/profile-metadata-app-store"

echo "Imported signing identity: $certificate_identity"
echo "Installed provisioning profiles for team $team_id."

# Make accidental later environment dumps harmless.
unset certificate_base64 certificate_password profiles_base64
unset XBUILD_MODE_CERTIFICATE_BASE64 XBUILD_MODE_CERTIFICATE_PASSWORD
unset XBUILD_MODE_PROFILES_BASE64 XBUILD_MODE_PROFILE_BASE64
unset XBUILD_COMMON_CERTIFICATE_BASE64 XBUILD_COMMON_CERTIFICATE_PASSWORD
unset XBUILD_COMMON_PROFILES_BASE64 XBUILD_COMMON_PROFILE_BASE64
unset XBUILD_SET_MODE_CERTIFICATE_BASE64 XBUILD_SET_MODE_CERTIFICATE_PASSWORD
unset XBUILD_SET_MODE_PROFILES_BASE64 XBUILD_SET_MODE_PROFILE_BASE64
unset XBUILD_SET_COMMON_CERTIFICATE_BASE64 XBUILD_SET_COMMON_CERTIFICATE_PASSWORD
unset XBUILD_SET_COMMON_PROFILES_BASE64 XBUILD_SET_COMMON_PROFILE_BASE64
unset XBUILD_DUAL_ADHOC_CERTIFICATE_BASE64 XBUILD_DUAL_ADHOC_CERTIFICATE_PASSWORD
unset XBUILD_DUAL_ADHOC_PROFILES_BASE64
unset XBUILD_DUAL_APPSTORE_CERTIFICATE_BASE64 XBUILD_DUAL_APPSTORE_CERTIFICATE_PASSWORD
unset XBUILD_DUAL_APPSTORE_PROFILES_BASE64
unset XBUILD_SET_TEAM_ID_OVERRIDE XBUILD_LEGACY_TEAM_ID_OVERRIDE XBUILD_TEAM_ID_OVERRIDE
