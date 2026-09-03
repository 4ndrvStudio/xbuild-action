#!/usr/bin/env bash

set -euo pipefail
umask 077

ipa_path="${1:?Usage: upload-testflight.sh <app-store-ipa> <log-directory>}"
log_dir="${2:?Usage: upload-testflight.sh <app-store-ipa> <log-directory>}"
signing_dir="${XBUILD_SIGNING_DIR:?XBUILD_SIGNING_DIR is required}"
output_dir="${XBUILD_OUTPUT_DIR:?XBUILD_OUTPUT_DIR is required}"
key_id="${XBUILD_ASC_KEY_ID:?App Store Connect Key ID is required}"
issuer_id="${XBUILD_ASC_ISSUER_ID:?App Store Connect Issuer ID is required}"
private_key_base64="${XBUILD_ASC_PRIVATE_KEY_BASE64:?App Store Connect private key is required}"

if [[ ! "$key_id" =~ ^[A-Z0-9]{10}$ ]]; then
  echo "::error title=Invalid App Store Connect Key ID::Expected 10 uppercase letters or digits."
  exit 44
fi
if [[ ! "$issuer_id" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
  echo "::error title=Invalid App Store Connect Issuer ID::Expected a UUID."
  exit 44
fi

ipa_path="$(python3 - "$ipa_path" "$output_dir" <<'PY'
import pathlib
import sys

ipa = pathlib.Path(sys.argv[1]).resolve(strict=True)
output = pathlib.Path(sys.argv[2]).resolve(strict=True)
try:
    ipa.relative_to(output)
except ValueError:
    print("::error title=Unsafe TestFlight path::The App Store IPA is outside XBuild's output directory.")
    raise SystemExit(44)
if ipa.suffix.lower() != ".ipa" or not ipa.is_file():
    print("::error title=App Store IPA missing::The TestFlight upload input is not an IPA file.")
    raise SystemExit(44)
print(ipa)
PY
)"

credentials_root="$signing_dir/appstoreconnect"
private_keys_dir="$credentials_root/private_keys"
key_file="$private_keys_dir/AuthKey_${key_id}.p8"
public_key_file="$credentials_root/appstoreconnect-public-key.pem"
mkdir -p "$private_keys_dir" "$log_dir"
chmod 700 "$credentials_root" "$private_keys_dir"

cleanup_private_key() {
  rm -f -- "$key_file" "$public_key_file"
  rmdir "$private_keys_dir" "$credentials_root" >/dev/null 2>&1 || true
}
trap cleanup_private_key EXIT

XBUILD_DECODE_VALUE="$private_key_base64" XBUILD_DECODE_PATH="$key_file" python3 - <<'PY'
import base64
import os
import pathlib

value = "".join(os.environ["XBUILD_DECODE_VALUE"].split())
try:
    payload = base64.b64decode(value, validate=True)
except Exception as exc:
    print(f"::error title=App Store Connect key decode failed::{exc}")
    raise SystemExit(44)
pathlib.Path(os.environ["XBUILD_DECODE_PATH"]).write_bytes(payload)
PY
chmod 600 "$key_file"

if ! openssl pkey -in "$key_file" -noout </dev/null; then
  echo "::error title=Invalid App Store Connect private key::The .p8 secret is not a readable unencrypted private key."
  exit 44
fi
if ! openssl ec -in "$key_file" -pubout -out "$public_key_file" >/dev/null 2>&1 ||
   ! openssl ec -pubin -in "$public_key_file" -text -noout 2>/dev/null |
     grep -E 'ASN1 OID: prime256v1|NIST CURVE: P-256' >/dev/null; then
  rm -f -- "$public_key_file"
  echo "::error title=Invalid App Store Connect private key::The .p8 must use the EC P-256 curve."
  exit 44
fi
rm -f -- "$public_key_file"

# Do not pass the Base64 secret to altool or any project-controlled process.
unset private_key_base64 XBUILD_ASC_PRIVATE_KEY_BASE64
unset XBUILD_ASC_KEY_ID XBUILD_ASC_ISSUER_ID

echo "::group::Upload App Store IPA to TestFlight"
echo "Uploading $(basename "$ipa_path") to App Store Connect..."
set +e
(
  cd "$credentials_root"
  xcrun altool \
    --upload-app \
    --type ios \
    --file "$ipa_path" \
    --apiKey "$key_id" \
    --apiIssuer "$issuer_id"
) 2>&1 | tee "$log_dir/testflight-upload.log"
upload_status=${PIPESTATUS[0]}
set -e
echo "::endgroup::"

if (( upload_status != 0 )); then
  echo "::error title=TestFlight upload failed::altool exited with status $upload_status. The Ad Hoc IPA remains in the GitHub artifact."
  exit "$upload_status"
fi

echo "::notice title=TestFlight upload accepted::Apple received the build. Processing in App Store Connect continues asynchronously."
