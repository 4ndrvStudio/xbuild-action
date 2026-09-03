#!/usr/bin/env python3
"""Choose provisioning profiles for every application bundle in an archive."""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import pathlib
import plistlib
import sys
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Profile:
    name: str
    uuid: str
    team_id: str
    app_pattern: str
    method: str
    expires: dt.datetime
    raw_path: pathlib.Path


def profile_method(data: dict[str, Any]) -> str:
    entitlements = data.get("Entitlements") or {}
    get_task_allow = bool(entitlements.get("get-task-allow", False))
    has_devices = bool(data.get("ProvisionedDevices"))
    if data.get("ProvisionsAllDevices"):
        return "enterprise"
    if has_devices and get_task_allow:
        return "development"
    if has_devices:
        return "ad-hoc"
    if get_task_allow:
        return "development"
    return "app-store"


def load_profiles(metadata_dir: pathlib.Path, raw_dir: pathlib.Path) -> list[Profile]:
    profiles: list[Profile] = []
    for plist_path in sorted(metadata_dir.glob("*.plist")):
        try:
            with plist_path.open("rb") as handle:
                data = plistlib.load(handle)
            platforms = data.get("Platform") or []
            if isinstance(platforms, str):
                platforms = [platforms]
            if platforms and "iOS" not in platforms:
                raise ValueError(f"profile is for {', '.join(map(str, platforms))}, not iOS")
            entitlements = data.get("Entitlements") or {}
            application_identifier = str(
                entitlements.get("application-identifier")
                or entitlements.get("com.apple.application-identifier")
                or ""
            )
            if "." not in application_identifier:
                raise ValueError("application-identifier entitlement is missing")
            _, app_pattern = application_identifier.split(".", 1)
            teams = data.get("TeamIdentifier") or []
            if isinstance(teams, str):
                teams = [teams]
            team_id = str(
                teams[0]
                if teams
                else entitlements.get("com.apple.developer.team-identifier", "")
            )
            expires = data.get("ExpirationDate")
            if not isinstance(expires, dt.datetime):
                raise ValueError("ExpirationDate is missing")
            if expires.tzinfo is not None:
                expires = expires.astimezone(dt.timezone.utc).replace(tzinfo=None)
            raw_path = raw_dir / f"{plist_path.stem}.mobileprovision"
            if not raw_path.is_file():
                raise ValueError("decoded profile payload is missing")
            profiles.append(
                Profile(
                    name=str(data.get("Name", "")),
                    uuid=str(data.get("UUID", "")),
                    team_id=team_id,
                    app_pattern=app_pattern,
                    method=profile_method(data),
                    expires=expires,
                    raw_path=raw_path,
                )
            )
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            print(f"::warning title=Provisioning profile ignored::{plist_path.name}: {exc}")
    return [profile for profile in profiles if profile.name and profile.uuid and profile.team_id]


def match_score(profile: Profile, bundle_id: str) -> tuple[int, int, float] | None:
    if not fnmatch.fnmatchcase(bundle_id, profile.app_pattern):
        return None
    exact = int(profile.app_pattern == bundle_id)
    specificity = len(profile.app_pattern.replace("*", ""))
    return (exact, specificity, profile.expires.timestamp())


def select_for_team(
    profiles: list[Profile], bundle_ids: list[str], method: str, team_id: str
) -> tuple[dict[str, Profile], tuple[int, int, float]] | None:
    result: dict[str, Profile] = {}
    exact_total = 0
    specificity_total = 0
    earliest_expiry = float("inf")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for bundle_id in bundle_ids:
        candidates: list[tuple[tuple[int, int, float], Profile]] = []
        for profile in profiles:
            if profile.team_id != team_id or profile.method != method or profile.expires <= now:
                continue
            score = match_score(profile, bundle_id)
            if score is not None:
                candidates.append((score, profile))
        if not candidates:
            return None
        score, chosen = max(candidates, key=lambda item: item[0])
        result[bundle_id] = chosen
        exact_total += score[0]
        specificity_total += score[1]
        earliest_expiry = min(earliest_expiry, score[2])
    return result, (exact_total, specificity_total, earliest_expiry)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-dir", type=pathlib.Path, required=True)
    parser.add_argument("--raw-dir", type=pathlib.Path, required=True)
    parser.add_argument("--bundle-ids", type=pathlib.Path, required=True)
    parser.add_argument("--method", choices=("development", "ad-hoc", "app-store"), required=True)
    parser.add_argument("--team-id", default="")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()

    bundle_ids = [
        line.strip()
        for line in args.bundle_ids.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not bundle_ids:
        print("::error title=Signing detection failed::No application bundle identifiers were supplied.")
        return 30

    profiles = load_profiles(args.metadata_dir, args.raw_dir)
    if not profiles:
        print("::error title=No valid profiles::No provisioning profile secret could be decoded.")
        return 30

    teams = [args.team_id] if args.team_id else sorted({profile.team_id for profile in profiles})
    selections: list[tuple[tuple[int, int, float], str, dict[str, Profile]]] = []
    for team_id in teams:
        selection = select_for_team(profiles, bundle_ids, args.method, team_id)
        if selection:
            selected, score = selection
            selections.append((score, team_id, selected))

    if not selections:
        available = sorted(
            {
                f"{profile.name} [{profile.method}, {profile.app_pattern}, team {profile.team_id}]"
                for profile in profiles
            }
        )
        team_note = f" for team {args.team_id}" if args.team_id else ""
        print(
            "::error title=No matching provisioning profiles::"
            f"Could not sign {', '.join(bundle_ids)} as {args.method}{team_note}. "
            f"Available profiles: {'; '.join(available)}"
        )
        return 31

    _, team_id, selected = max(selections, key=lambda item: item[0])
    payload = {
        "team_id": team_id,
        "export_method": args.method,
        "bundle_identifier": bundle_ids[0],
        "profiles": {
            bundle_id: {
                "name": profile.name,
                "uuid": profile.uuid,
                "team_id": profile.team_id,
                "application_identifier_pattern": profile.app_pattern,
                "method": profile.method,
                "expires": profile.expires.isoformat() + "Z",
                "raw_path": str(profile.raw_path),
            }
            for bundle_id, profile in selected.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Selected provisioning profiles for {len(selected)} bundle identifier(s), team {team_id}.")
    for bundle_id, profile in selected.items():
        print(f"  {bundle_id} -> {profile.name} (expires {profile.expires.date().isoformat()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
