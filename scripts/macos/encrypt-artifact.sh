#!/usr/bin/env bash

set -euo pipefail
umask 077

input_path="${1:-}"
output_path="${2:-}"
key_base64="${XBUILD_TRANSPORT_KEY_BASE64:-}"

if [[ ! -f "$input_path" || ! -s "$input_path" || -z "$output_path" ]]; then
  echo "::error title=Private artifact input missing::Cannot encrypt an empty result."
  exit 2
fi
if [[ -z "$key_base64" ]]; then
  echo "::error title=Private transport key missing::Reinstall the workflow from the current XBuild version."
  exit 2
fi

key_file="$(mktemp "${RUNNER_TEMP%/}/xbuild-key.XXXXXX")"
cipher_file="${output_path}.cipher"
header_file="${output_path}.header"
trap 'rm -f -- "$key_file" "$cipher_file" "$header_file"' EXIT

if ! printf '%s' "$key_base64" | base64 --decode > "$key_file" 2>/dev/null; then
  # macOS ships a BSD base64 variant whose portable decode flag is -D.
  if ! printf '%s' "$key_base64" | base64 -D > "$key_file" 2>/dev/null; then
    echo "::error title=Private transport key invalid::The per-run transport key is malformed."
    exit 2
  fi
fi
if [[ "$(wc -c < "$key_file" | tr -d ' ')" != "64" ]]; then
  echo "::error title=Private transport key invalid::The per-run transport key is malformed."
  exit 2
fi

enc_key="$(dd if="$key_file" bs=1 count=32 2>/dev/null | xxd -p -c 64)"
mac_key="$(dd if="$key_file" bs=1 skip=32 count=32 2>/dev/null | xxd -p -c 64)"
iv="$(openssl rand -hex 16)"
openssl enc -aes-256-cbc -K "$enc_key" -iv "$iv" -in "$input_path" -out "$cipher_file"

printf 'XBUILD01' > "$header_file"
printf '%s' "$iv" | xxd -r -p >> "$header_file"
cat "$cipher_file" >> "$header_file"
openssl dgst -sha256 -mac HMAC -macopt "hexkey:$mac_key" -binary "$header_file" >> "$header_file"
mv -f -- "$header_file" "$output_path"

unset key_base64 enc_key mac_key iv
echo "Encrypted private XBuild output."
