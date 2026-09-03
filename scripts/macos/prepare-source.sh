#!/usr/bin/env bash

set -euo pipefail

archive="${1:?Usage: prepare-source.sh <archive.zip> <destination>}"
destination="${2:?Usage: prepare-source.sh <archive.zip> <destination>}"

if [[ ! -s "$archive" ]]; then
  echo "::error title=Invalid source archive::The uploaded archive does not exist or is empty."
  exit 10
fi

mkdir -p "$destination"
shopt -s nullglob dotglob
existing_entries=("$destination"/*)
shopt -u nullglob dotglob
if (( ${#existing_entries[@]} > 0 )); then
  echo "::error title=Source directory is not empty::Refusing to extract over an existing directory."
  exit 10
fi

# Inspect every member before using ditto. Besides producing a clearer corrupt-ZIP
# error, this prevents an uploaded archive from escaping the runner temp folder.
python3 - "$archive" <<'PY'
import os
import pathlib
import shutil
import stat
import sys
import zipfile

archive = sys.argv[1]
try:
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if not entries:
            raise ValueError("archive has no entries")

        total = 0
        for entry in entries:
            normalized = entry.filename.replace("\\", "/")
            path = pathlib.PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or normalized.startswith("/"):
                raise ValueError(f"unsafe archive entry: {entry.filename!r}")
            mode = (entry.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                target_text = source.read(entry).decode("utf-8", errors="strict").replace("\\", "/")
                target = pathlib.PurePosixPath(target_text)
                if target.is_absolute() or ".." in target.parts:
                    raise ValueError(
                        f"unsafe symbolic link: {entry.filename!r} -> {target_text!r}"
                    )
            total += entry.file_size

        free = shutil.disk_usage(os.path.dirname(archive)).free
        if total > free * 0.9:
            raise ValueError(
                f"archive needs about {total / 1024**3:.1f} GiB but the runner has "
                f"only {free / 1024**3:.1f} GiB free"
            )

        print(f"Validated {len(entries)} archive entries ({total / 1024**2:.1f} MiB uncompressed).")
except (OSError, ValueError, zipfile.BadZipFile) as exc:
    print(f"::error title=Invalid source ZIP::{exc}")
    raise SystemExit(10)
PY

# ditto is the native macOS ZIP extractor and correctly handles macOS metadata
# when the uploader preserved it.
if ! ditto -x -k "$archive" "$destination"; then
  echo "::error title=Extraction failed::macOS could not extract the uploaded ZIP."
  exit 10
fi

rm -rf -- "$destination/__MACOSX"

# Unity exports created on Windows can contain CRLF shell scripts and do not carry
# Unix executable bits. Normalize only text shell scripts; Xcode build phases often
# invoke these scripts directly.
python3 - "$destination" <<'PY'
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
mach_o_magics = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
}
for path in root.rglob("*"):
    if not path.is_file():
        continue
    try:
        with path.open("rb") as handle:
            header = handle.read(4096)
        is_script = path.suffix.lower() in {".sh", ".command"} or header.startswith(b"#!")
        should_execute = is_script
        if is_script:
            data = path.read_bytes()
            if b"\x00" not in data and b"\r\n" in data:
                path.write_bytes(data.replace(b"\r\n", b"\n"))
        else:
            should_execute = header[:4] in mach_o_magics
        if should_execute:
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        print(f"::warning title=File-mode normalization skipped::{path}: {exc}")
PY

container_count="$(find "$destination" -type d \( -name '*.xcodeproj' -o -name '*.xcworkspace' \) \
  ! -path '*/Pods/*' \
  ! -path '*.xcodeproj/project.xcworkspace' \
  -print | wc -l | tr -d ' ')"

if [[ "$container_count" == "0" ]]; then
  echo "::error title=No Xcode project::The ZIP does not contain a .xcodeproj or .xcworkspace directory."
  exit 11
fi

podfile_count="$(find "$destination" -type f -name Podfile ! -path '*/Pods/*' -print | wc -l | tr -d ' ')"
echo "Source is ready: found $container_count Xcode container(s) and $podfile_count Podfile(s)."
