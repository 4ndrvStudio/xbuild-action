#!/usr/bin/env python3
"""Configure and verify Apple's encryption export-compliance plist keys."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import plistlib
import stat
from typing import Any, NoReturn


USES_NON_EXEMPT_KEY = "ITSAppUsesNonExemptEncryption"
COMPLIANCE_CODE_KEY = "ITSEncryptionExportComplianceCode"
MODES = ("exempt", "non-exempt", "preserve")


def fail(title: str, message: str, code: int = 45) -> NoReturn:
    print(f"::error title={title}::{message}")
    raise SystemExit(code)


def validate(mode: str, compliance_code: str) -> None:
    if mode not in MODES:
        fail("Invalid export compliance mode", f"Expected one of: {', '.join(MODES)}.")
    if len(compliance_code) > 256:
        fail("Invalid export compliance code", "The App Store Connect compliance code is too long.")
    if any(ord(character) < 32 or ord(character) == 127 for character in compliance_code):
        fail(
            "Invalid export compliance code",
            "The App Store Connect compliance code must not contain control characters.",
        )
    if mode == "non-exempt" and not compliance_code.strip():
        fail(
            "Export compliance code required",
            "Non-exempt encryption requires the code Apple provided after approving the app's export documentation.",
        )


def load_detected_build(path: pathlib.Path) -> dict[str, Any]:
    try:
        detected = json.loads(path.read_text(encoding="utf-8"))
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
        "XBuild could not identify the top-level iOS app target for export compliance.",
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


def read_plist(path: pathlib.Path, title: str) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        data = plistlib.loads(raw)
    except (OSError, plistlib.InvalidFileException) as exc:
        fail(title, f"Could not read {path}: {exc}")
    if not isinstance(data, dict):
        fail(title, f"{path} is not a dictionary plist.")
    return raw, data


def write_plist(path: pathlib.Path, raw: bytes, data: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".xbuild.tmp")
    mode = stat.S_IMODE(path.stat().st_mode)
    try:
        with temporary.open("wb") as handle:
            plistlib.dump(
                data,
                handle,
                fmt=plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML,
                sort_keys=False,
            )
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        fail("Export compliance update failed", str(exc))
    finally:
        temporary.unlink(missing_ok=True)


def patch_source(
    source_root: pathlib.Path,
    detected_path: pathlib.Path,
    mode: str,
    compliance_code: str,
) -> None:
    validate(mode, compliance_code)
    if mode == "preserve":
        print("Preserving export-compliance keys from the Xcode project.")
        return

    source_root = source_root.resolve()
    main_target = select_main_target(load_detected_build(detected_path))
    target_name = str(main_target.get("target", "main app")) or "main app"
    if str(main_target.get("generate_info_plist_file", "")).upper() == "YES":
        print(
            f"{target_name} generates Info.plist; export compliance will be supplied "
            "through Xcode archive build settings."
        )
        return

    plist_path = resolve_info_plist(source_root, main_target)
    if plist_path is None or not plist_path.is_file():
        print(
            f"::warning title=Main app Info.plist unresolved::Could not update {target_name} "
            "before archive; the archived app will be checked after the Xcode build."
        )
        return

    raw, data = read_plist(plist_path, "Export compliance update failed")
    data[USES_NON_EXEMPT_KEY] = mode == "non-exempt"
    if mode == "non-exempt":
        data[COMPLIANCE_CODE_KEY] = compliance_code
    else:
        data.pop(COMPLIANCE_CODE_KEY, None)
    write_plist(plist_path, raw, data)
    print(
        f"Configured {USES_NON_EXEMPT_KEY} as a Boolean for "
        f"{plist_path.relative_to(source_root)}."
    )


def archive_main_app(archive: pathlib.Path) -> pathlib.Path:
    applications = archive / "Products" / "Applications"
    apps = sorted(path for path in applications.glob("*.app") if path.is_dir())
    if len(apps) != 1:
        fail(
            "Export compliance verification failed",
            f"Expected one top-level iOS app in the archive, found {len(apps)}.",
        )
    return apps[0]


def verify_archive(archive: pathlib.Path, mode: str, compliance_code: str) -> None:
    validate(mode, compliance_code)
    if mode == "preserve":
        print("Preserved the archived app's export-compliance keys without validation.")
        return

    main_app = archive_main_app(archive)
    _, data = read_plist(main_app / "Info.plist", "Export compliance verification failed")
    actual = data.get(USES_NON_EXEMPT_KEY)
    expected = mode == "non-exempt"
    if type(actual) is not bool:  # bool is required; strings such as "NO" are invalid here.
        fail(
            "Invalid archived export compliance value",
            f"{main_app.name} must contain Boolean {USES_NON_EXEMPT_KEY}; found {type(actual).__name__}.",
        )
    if actual is not expected:
        fail(
            "Archived export compliance mismatch",
            f"{main_app.name} does not contain the requested export-compliance declaration.",
        )

    if mode == "non-exempt":
        actual_code = data.get(COMPLIANCE_CODE_KEY)
        if not isinstance(actual_code, str) or actual_code != compliance_code:
            fail(
                "Archived export compliance code mismatch",
                f"{main_app.name} does not contain the requested App Store Connect compliance code.",
            )
    elif data.get(COMPLIANCE_CODE_KEY) not in (None, ""):
        fail(
            "Stale export compliance code",
            f"{main_app.name} still contains a non-empty {COMPLIANCE_CODE_KEY} in exempt mode.",
        )
    print(f"Verified archived {USES_NON_EXEMPT_KEY} Boolean ({mode}).")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    patch_parser = subparsers.add_parser("patch")
    patch_parser.add_argument("--source-root", type=pathlib.Path, required=True)
    patch_parser.add_argument("--detected-build", type=pathlib.Path, required=True)
    patch_parser.add_argument("--mode", choices=MODES, required=True)
    patch_parser.add_argument("--compliance-code", default="")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--archive", type=pathlib.Path, required=True)
    verify_parser.add_argument("--mode", choices=MODES, required=True)
    verify_parser.add_argument("--compliance-code", default="")

    args = parser.parse_args()
    if args.command == "patch":
        patch_source(args.source_root, args.detected_build, args.mode, args.compliance_code)
    else:
        verify_archive(args.archive, args.mode, args.compliance_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
