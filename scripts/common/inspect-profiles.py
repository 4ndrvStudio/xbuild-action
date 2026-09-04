#!/usr/bin/env python3
"""Extract a deliberately non-secret index from provisioning-profile secrets."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import plistlib
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


MAX_SECRET_BYTES = 4 * 1024 * 1024
MAX_PROFILE_BYTES = 2 * 1024 * 1024


def decode_secret(value: str, method: str) -> list[tuple[str, bytes]]:
    if not value:
        return []
    try:
        raw = base64.b64decode("".join(value.split()), validate=True)
    except Exception as exc:
        raise ValueError(f"{method} profile secret is not valid Base64") from exc
    if not raw or len(raw) > MAX_SECRET_BYTES:
        raise ValueError(f"{method} profile secret has an invalid decoded size")

    stream = io.BytesIO(raw)
    if not zipfile.is_zipfile(stream):
        return [(f"{method}.mobileprovision", raw)]

    profiles: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(stream) as archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            if info.is_dir() or not name.lower().endswith(".mobileprovision"):
                continue
            if info.file_size <= 0 or info.file_size > MAX_PROFILE_BYTES:
                raise ValueError(f"{method} profile '{name}' has an invalid size")
            data = archive.read(info)
            if len(data) != info.file_size:
                raise ValueError(f"{method} profile '{name}' was truncated")
            profiles.append((name, data))
    if not profiles:
        raise ValueError(f"{method} profile archive contains no .mobileprovision files")
    return profiles


def decode_cms(data: bytes, name: str) -> dict:
    if b"<plist" in data:
        start = data.index(b"<?xml") if b"<?xml" in data else data.index(b"<plist")
        end = data.find(b"</plist>", start)
        if end >= 0:
            return plistlib.loads(data[start : end + len(b"</plist>")])

    path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="xbuild-profile-", suffix=".mobileprovision", delete=False) as handle:
            handle.write(data)
            path = handle.name
        for command in (
            ["openssl", "cms", "-verify", "-inform", "DER", "-noverify", "-in", path],
            ["openssl", "smime", "-verify", "-inform", "DER", "-noverify", "-in", path],
        ):
            completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if completed.returncode == 0:
                return plistlib.loads(completed.stdout)
    finally:
        if path:
            Path(path).unlink(missing_ok=True)
    raise ValueError(f"profile '{name}' is not a readable CMS provisioning profile")


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def profile_metadata(data: bytes, file_name: str, method: str, expected_team: str) -> dict:
    plist = decode_cms(data, file_name)
    name = str(plist.get("Name", "")).strip()
    uuid = str(plist.get("UUID", "")).strip().upper()
    expiration = plist.get("ExpirationDate")
    teams = string_list(plist.get("TeamIdentifier")) or string_list(plist.get("ApplicationIdentifierPrefix"))
    prefixes = string_list(plist.get("ApplicationIdentifierPrefix"))
    entitlements = plist.get("Entitlements")
    if not isinstance(entitlements, dict):
        entitlements = {}
    application_id = str(
        entitlements.get("application-identifier")
        or entitlements.get("com.apple.application-identifier")
        or ""
    ).strip()
    if not name or not uuid or not teams or not application_id or not isinstance(expiration, dt.datetime):
        raise ValueError(f"profile '{file_name}' is missing required metadata")
    if expected_team.upper() not in {team.upper() for team in teams}:
        raise ValueError(f"profile '{file_name}' belongs to a different Apple Team")

    bundle_id = application_id
    for prefix in sorted(set(prefixes + teams), key=len, reverse=True):
        marker = prefix + "."
        if application_id.lower().startswith(marker.lower()):
            bundle_id = application_id[len(marker) :]
            break
    if not bundle_id:
        raise ValueError(f"profile '{file_name}' has an empty bundle identifier")

    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=dt.timezone.utc)
    expiration = expiration.astimezone(dt.timezone.utc)
    return {
        "exportMethod": method,
        "name": name,
        "bundleIdentifier": bundle_id,
        "uuid": uuid,
        "expirationDate": expiration.isoformat().replace("+00:00", "Z"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    team = args.team.strip().upper()
    if len(team) != 10 or not team.isalnum() or not team.isascii():
        raise ValueError("signing_set must be a 10-character Apple Team ID")

    configured_team = os.environ.get("SIGNING_TEAM_ID", "").strip().upper()
    if configured_team and configured_team != team:
        raise ValueError("selected Team ID does not match the stored Team ID secret")

    profiles: list[dict] = []
    for method, environment_name in (
        ("ad-hoc", "ADHOC_PROFILES_BASE64"),
        ("app-store", "APPSTORE_PROFILES_BASE64"),
        ("development", "DEVELOPMENT_PROFILES_BASE64"),
    ):
        for file_name, data in decode_secret(os.environ.get(environment_name, ""), method):
            profiles.append(profile_metadata(data, file_name, method, team))

    unique = {
        (item["exportMethod"].lower(), item["uuid"].upper()): item
        for item in profiles
    }
    profiles = sorted(
        unique.values(),
        key=lambda item: (
            {"ad-hoc": 0, "app-store": 1, "development": 2}.get(item["exportMethod"], 3),
            item["bundleIdentifier"].lower(),
            item["name"].lower(),
        ),
    )
    output = {
        "schemaVersion": 1,
        "teamId": team,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "profiles": profiles,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
    print(f"Indexed {len(profiles)} provisioning profile(s) for Team {team}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
