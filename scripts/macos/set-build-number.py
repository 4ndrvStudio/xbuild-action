#!/usr/bin/env python3
"""Preserve the project's TestFlight build number and verify the archive."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import plistlib
import re
import stat
from typing import Any


BUILD_NUMBER = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")
BUILD_SETTING_REFERENCE = re.compile(r"\$\(([^)]+)\)|\$\{([^}]+)\}")


def fail(title: str, message: str, code: int = 43) -> "NoReturn":
    print(f"::error title={title}::{message}")
    raise SystemExit(code)


def validate_build_number(value: str) -> str:
    if not BUILD_NUMBER.fullmatch(value):
        fail(
            "Invalid project build number",
            f"The Unity-exported app resolves CFBundleVersion to {value!r}. "
            "TestFlight requires one to three dot-separated integers, for example 42 or 1.2.3. "
            "Update Player Settings > iOS > Build in Unity and export the Xcode project again.",
        )
    return value


def load_detected_build(detected_path: pathlib.Path) -> dict[str, Any]:
    try:
        detected = json.loads(detected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail("Build metadata unavailable", str(exc))
    if not isinstance(detected, dict):
        fail("Build metadata unavailable", "The detected build metadata is not an object.")
    return detected


def select_main_target(detected: dict[str, Any]) -> dict[str, Any]:
    main_target = detected.get("main_target")
    if isinstance(main_target, dict):
        return main_target

    targets = detected.get("signable_targets")
    if isinstance(targets, list):
        app_targets = [
            target
            for target in targets
            if isinstance(target, dict)
            and (
                str(target.get("wrapper_extension", "")).lower() == "app"
                or "product-type.application"
                in str(target.get("product_type", "")).lower()
            )
        ]
        if len(app_targets) == 1:
            return app_targets[0]
    fail(
        "Main app target unavailable",
        "XBuild could not identify the main iOS app target while reading its build number. "
        "Reinstall the latest workflow and run the build again.",
    )


def load_plist_value(plist_path: pathlib.Path) -> str:
    try:
        raw = plist_path.read_bytes()
        data = plistlib.loads(raw)
    except (OSError, plistlib.InvalidFileException) as exc:
        fail("Build number read failed", f"Could not read {plist_path}: {exc}")
    if not isinstance(data, dict):
        fail("Build number read failed", f"{plist_path} is not a dictionary plist.")
    value = data.get("CFBundleVersion")
    return "" if value is None else str(value).strip()


def expand_build_settings(value: str, target: dict[str, Any]) -> str:
    settings = {
        "CURRENT_PROJECT_VERSION": str(
            target.get("current_project_version", "")
        ).strip().strip('"'),
        "INFOPLIST_KEY_CFBundleVersion": str(
            target.get("info_plist_bundle_version", "")
        ).strip().strip('"'),
    }
    expanded = value.strip().strip('"')
    for _ in range(4):
        updated = BUILD_SETTING_REFERENCE.sub(
            lambda match: settings.get(match.group(1) or match.group(2), match.group(0)),
            expanded,
        )
        if updated == expanded:
            break
        expanded = updated
    return expanded.strip()


def resolve_project_build_number(
    source_root: pathlib.Path, detected_path: pathlib.Path
) -> str:
    source_root = source_root.resolve()
    detected = load_detected_build(detected_path)
    main_target = select_main_target(detected)
    target_name = str(main_target.get("target", "main app")) or "main app"

    plist_path = resolve_info_plist(source_root, main_target)
    plist_value = ""
    if plist_path is not None and plist_path.is_file():
        plist_value = load_plist_value(plist_path)

    build_setting_candidates = [
        (
            "INFOPLIST_KEY_CFBundleVersion",
            str(main_target.get("info_plist_bundle_version", "")).strip(),
        ),
        (
            "CURRENT_PROJECT_VERSION",
            str(main_target.get("current_project_version", "")).strip(),
        ),
    ]
    if str(main_target.get("generate_info_plist_file", "")).upper() == "YES":
        # A source plist path can still appear in build settings for generated
        # targets, but Xcode does not use that file to produce the bundle plist.
        candidates = build_setting_candidates
    else:
        candidates = [("CFBundleVersion", plist_value), *build_setting_candidates]
    for source_name, raw_value in candidates:
        if not raw_value:
            continue
        resolved = expand_build_settings(raw_value, main_target)
        if BUILD_SETTING_REFERENCE.search(resolved):
            fail(
                "Project build number unresolved",
                f"{target_name} uses {source_name}={raw_value!r}, but its Xcode build setting "
                "could not be resolved. Set a numeric iOS Build value in Unity and export again.",
            )
        if not resolved:
            continue
        build_number = validate_build_number(resolved)
        print(
            f"Using Unity-exported build number {build_number} "
            f"from {target_name} ({source_name})."
        )
        return build_number

    fail(
        "Project build number missing",
        f"{target_name} has no resolvable CFBundleVersion or CURRENT_PROJECT_VERSION. "
        "Set Player Settings > iOS > Build in Unity and export the Xcode project again.",
    )


def resolve_info_plist(
    source_root: pathlib.Path, target: dict[str, Any]
) -> pathlib.Path | None:
    raw = str(target.get("info_plist_file", "")).strip().strip('"')
    if not raw:
        return None
    project_dir_text = str(target.get("project_dir", "")).strip()
    project_file_text = str(target.get("project_file_path", "")).strip()
    if project_dir_text:
        project_dir = pathlib.Path(project_dir_text)
    elif project_file_text:
        project_dir = pathlib.Path(project_file_text).parent
    else:
        return None

    for marker in ("$(SRCROOT)", "${SRCROOT}", "$(PROJECT_DIR)", "${PROJECT_DIR}"):
        raw = raw.replace(marker, str(project_dir))
    if "$(" in raw or "${" in raw:
        return None

    candidate = pathlib.Path(raw)
    if not candidate.is_absolute():
        candidate = project_dir / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(source_root)
    except ValueError:
        fail(
            "Unsafe Info.plist path",
            f"Refusing to modify a plist outside the uploaded source: {candidate}",
        )
    return candidate


def write_plist(path: pathlib.Path, data: dict[str, Any], binary: bool) -> None:
    temporary = path.with_name(path.name + ".xbuild.tmp")
    mode = stat.S_IMODE(path.stat().st_mode)
    try:
        with temporary.open("wb") as handle:
            plistlib.dump(
                data,
                handle,
                fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML,
                sort_keys=False,
            )
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def patch_source(
    source_root: pathlib.Path, detected_path: pathlib.Path, build_number: str
) -> None:
    source_root = source_root.resolve()
    detected = load_detected_build(detected_path)

    targets = detected.get("signable_targets")
    if not isinstance(targets, list) or not targets:
        fail("Build metadata unavailable", "No signable targets were detected.")

    patched: set[pathlib.Path] = set()
    generated_targets = 0
    unresolved: list[str] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        target_name = str(target.get("target", "unknown target"))
        if str(target.get("generate_info_plist_file", "")).upper() == "YES":
            generated_targets += 1
        plist_path = resolve_info_plist(source_root, target)
        if plist_path is None:
            if str(target.get("generate_info_plist_file", "")).upper() != "YES":
                unresolved.append(target_name)
            continue
        if plist_path in patched:
            continue
        if not plist_path.is_file():
            unresolved.append(target_name)
            continue
        try:
            raw = plist_path.read_bytes()
            data = plistlib.loads(raw)
        except (OSError, plistlib.InvalidFileException) as exc:
            fail("Info.plist update failed", f"{plist_path}: {exc}")
        if not isinstance(data, dict):
            fail("Info.plist update failed", f"{plist_path} is not a dictionary plist.")
        data["CFBundleVersion"] = build_number
        write_plist(plist_path, data, raw.startswith(b"bplist"))
        patched.add(plist_path)
        print(f"Stamped CFBundleVersion {build_number}: {plist_path.relative_to(source_root)}")

    if not patched and generated_targets == 0:
        fail(
            "Build number update failed",
            "No source or generated Info.plist could be associated with the detected app targets.",
        )
    for target_name in sorted(set(unresolved)):
        print(
            f"::warning title=Build number source unresolved::"
            f"{target_name} will rely on the CURRENT_PROJECT_VERSION archive override."
        )


def load_bundle_version(bundle: pathlib.Path) -> str:
    info = bundle / "Info.plist"
    try:
        with info.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        fail("Archive verification failed", f"Could not read {info}: {exc}")
    return str(data.get("CFBundleVersion", "")).strip()


def verify_archive(archive: pathlib.Path, build_number: str) -> None:
    applications = archive / "Products" / "Applications"
    apps = sorted(path for path in applications.glob("*.app") if path.is_dir())
    if len(apps) != 1:
        fail(
            "Archive verification failed",
            f"Expected one top-level iOS app in the archive, found {len(apps)}.",
        )

    main_app = apps[0]
    actual = load_bundle_version(main_app)
    if actual != build_number:
        fail(
            "Archive build number mismatch",
            f"{main_app.name} has CFBundleVersion {actual!r}, but the Unity project value is "
            f"{build_number!r}. Check for a build phase or plugin that overrides the build number.",
        )

    for extension in sorted(path for path in main_app.rglob("*.appex") if path.is_dir()):
        extension_version = load_bundle_version(extension)
        if extension_version != build_number:
            fail(
                "Extension build number mismatch",
                f"{extension.name} has CFBundleVersion {extension_version!r}, "
                f"but the main app uses {build_number!r}. Check that extension's version settings.",
            )
    print(f"Verified archived CFBundleVersion: {build_number}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--source-root", type=pathlib.Path, required=True)
    resolve_parser.add_argument("--detected-build", type=pathlib.Path, required=True)
    resolve_parser.add_argument("--output", type=pathlib.Path, required=True)

    patch_parser = subparsers.add_parser("patch")
    patch_parser.add_argument("--source-root", type=pathlib.Path, required=True)
    patch_parser.add_argument("--detected-build", type=pathlib.Path, required=True)
    patch_parser.add_argument("--build-number", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--archive", type=pathlib.Path, required=True)
    verify_parser.add_argument("--build-number", required=True)

    args = parser.parse_args()
    if args.command == "resolve":
        build_number = resolve_project_build_number(args.source_root, args.detected_build)
        try:
            args.output.write_text(build_number + "\n", encoding="utf-8")
        except OSError as exc:
            fail("Build number output failed", str(exc))
    elif args.command == "patch":
        build_number = validate_build_number(args.build_number)
        patch_source(args.source_root, args.detected_build, build_number)
    else:
        build_number = validate_build_number(args.build_number)
        verify_archive(args.archive, build_number)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
