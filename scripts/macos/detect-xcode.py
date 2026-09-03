#!/usr/bin/env python3
"""Resolve a buildable Xcode container and its application scheme.

The source is deliberately inspected on macOS after `pod install`: a generated
workspace always wins over its project, and xcodebuild itself is the source of
truth for schemes, configurations, and expanded bundle identifiers.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Any, Iterable


def fail(title: str, message: str, code: int = 20) -> "NoReturn":
    print(f"::error title={title}::{message}")
    raise SystemExit(code)


def safe_value(label: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        fail("Invalid build override", f"{label} must be a single-line value.")
    return value.strip()


def excluded(path: pathlib.Path) -> bool:
    lowered = {part.lower() for part in path.parts}
    if lowered.intersection({"pods", "deriveddata", "__macosx", ".git"}):
        return True
    return path.name == "project.xcworkspace" and path.parent.suffix == ".xcodeproj"


def load_json_output(command: list[str], log_path: pathlib.Path) -> Any:
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "$ " + " ".join(command) + "\n\n" + proc.stdout + "\n" + proc.stderr,
        encoding="utf-8",
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"xcodebuild exited with {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"xcodebuild returned invalid JSON: {exc}") from exc


def container_args(kind: str, path: pathlib.Path) -> list[str]:
    return [f"-{kind}", str(path)]


def container_score(path: pathlib.Path) -> tuple[int, int, str]:
    score = 0
    stem = path.stem.lower()
    if (path.parent / "Podfile").is_file():
        score += 5_000
    if stem == "unity-iphone":
        score += 4_000
    if (path.parent / f"{path.stem}.xcodeproj").is_dir():
        score += 2_000
    if "unity" in stem:
        score += 500
    return (-score, len(path.parts), str(path).lower())


def workspace_references_project(
    workspace: pathlib.Path, hinted_project: pathlib.Path
) -> bool:
    contents = workspace / "contents.xcworkspacedata"
    try:
        root = ET.parse(contents).getroot()
    except (OSError, ET.ParseError):
        return False

    expected = hinted_project.resolve(strict=False).as_posix().casefold()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "FileRef":
            continue
        location = str(element.attrib.get("location", ""))
        reference_type, separator, value = location.partition(":")
        if not separator or reference_type not in {"absolute", "container", "group", "self"}:
            continue
        reference = pathlib.PurePosixPath(value.replace("\\", "/"))
        if reference_type == "absolute":
            candidate = pathlib.Path(*reference.parts)
        else:
            candidate = workspace.parent.joinpath(*reference.parts)
        if candidate.resolve(strict=False).as_posix().casefold() == expected:
            return True
    return False


def hinted_container_rank(
    kind: str, path: pathlib.Path, source_root: pathlib.Path, project_hint: str
) -> tuple[int, tuple[int, int, str]]:
    if not project_hint:
        return (0 if kind == "workspace" else 1, container_score(path))

    relative = path.relative_to(source_root).as_posix().lower()
    hint = pathlib.PurePosixPath(project_hint)
    hint_text = hint.as_posix().lower()
    hint_parent = hint.parent.as_posix().lower()
    hint_stem = hint.stem.lower()

    if hint.suffix.lower() == ".xcworkspace" and kind == "workspace" and relative == hint_text:
        return (0, container_score(path))
    if hint.suffix.lower() == ".xcodeproj":
        same_parent = pathlib.PurePosixPath(relative).parent.as_posix().lower() == hint_parent
        same_stem = pathlib.PurePosixPath(relative).stem.lower() == hint_stem
        hinted_project = source_root.joinpath(*hint.parts)
        podfile_ancestor = (path.parent / "Podfile").is_file() and (
            path.parent == hinted_project.parent or path.parent in hinted_project.parents
        )
        if kind == "workspace":
            if same_parent and same_stem:
                return (0, container_score(path))
            if podfile_ancestor and workspace_references_project(path, hinted_project):
                return (0, container_score(path))
        if kind == "project" and relative == hint_text:
            return (1, container_score(path))
    return (2 if kind == "workspace" else 3, container_score(path))


def scheme_score(name: str, container_stem: str) -> tuple[int, str]:
    lower = name.lower()
    score = 0
    if lower == "unity-iphone":
        score += 10_000
    if lower == container_stem.lower():
        score += 8_000
    if "unity-iphone" in lower:
        score += 3_000
    if "pods" in lower:
        score -= 10_000
    if "test" in lower:
        score -= 4_000
    if "framework" in lower:
        score -= 2_000
    return (-score, lower)


def strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def list_details(data: Any) -> tuple[list[str], list[str]]:
    if not isinstance(data, dict):
        return [], []
    for key in ("workspace", "project"):
        section = data.get(key)
        if isinstance(section, dict):
            return strings(section.get("schemes")), strings(section.get("configurations"))
    return [], []


def unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def configuration_candidates(requested: str, available: list[str]) -> list[str]:
    if requested:
        if available and requested not in available:
            lowered = {item.lower(): item for item in available}
            if requested.lower() in lowered:
                return [lowered[requested.lower()]]
            raise RuntimeError(
                f"configuration {requested!r} was not found; available: {', '.join(available)}"
            )
        return [requested]
    ordered = [candidate for candidate in available if candidate.lower() == "release"]
    ordered.extend(
        candidate
        for candidate in available
        if "release" in candidate.lower() or "production" in candidate.lower()
    )
    ordered.extend(candidate for candidate in available if candidate.lower() != "debug")
    ordered.extend(available)
    return unique(ordered) if ordered else ["Release"]


def signable_settings(settings_json: Any) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    if not isinstance(settings_json, list):
        return results
    for entry in settings_json:
        if not isinstance(entry, dict):
            continue
        settings = entry.get("buildSettings")
        if not isinstance(settings, dict):
            continue
        bundle_id = str(settings.get("PRODUCT_BUNDLE_IDENTIFIER", "")).strip()
        wrapper = str(settings.get("WRAPPER_EXTENSION", "")).lower()
        product_type = str(settings.get("PRODUCT_TYPE", "")).lower()
        if not bundle_id or "$(" in bundle_id:
            continue
        if wrapper not in {"app", "appex"} and not any(
            marker in product_type
            for marker in ("product-type.application", "application-extension", "watchkit")
        ):
            continue
        if any(marker in product_type for marker in ("unit-test", "ui-testing")):
            continue
        results.append(
            {
                "target": str(entry.get("target", "")),
                "bundle_identifier": bundle_id,
                "project_file_path": str(settings.get("PROJECT_FILE_PATH", "")).strip(),
                "wrapper_extension": wrapper,
                "product_type": product_type,
                "skip_install": str(settings.get("SKIP_INSTALL", "")).upper(),
                "product_name": str(settings.get("PRODUCT_NAME", "")),
            }
        )
    return results


def main_target_score(target: dict[str, str], scheme: str) -> tuple[int, str]:
    score = 0
    if target["wrapper_extension"] == "app":
        score += 5_000
    if target["skip_install"] == "NO":
        score += 3_000
    if target["target"].lower() == scheme.lower():
        score += 2_000
    if target["target"].lower() == "unity-iphone":
        score += 1_000
    if "watch" in target["product_type"]:
        score -= 2_000
    return (-score, target["target"].lower())


def append_env(path: pathlib.Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                fail("Unsafe detected value", f"{key} contains a line break.")
            handle.write(f"{key}={value}\n")


def main() -> int:
    if len(sys.argv) != 4:
        fail("Invalid invocation", "detect-xcode.py needs source-root, env-file, and work-dir.", 2)

    source_root = pathlib.Path(sys.argv[1]).resolve()
    env_file = pathlib.Path(sys.argv[2])
    work_dir = pathlib.Path(sys.argv[3]).resolve()
    log_dir = pathlib.Path(os.environ.get("XBUILD_LOG_DIR", work_dir / "logs"))
    requested_scheme = safe_value("scheme", os.environ.get("XBUILD_REQUESTED_SCHEME", ""))
    requested_configuration = safe_value(
        "configuration", os.environ.get("XBUILD_REQUESTED_CONFIGURATION", "")
    )
    requested_bundle = safe_value(
        "bundle_identifier", os.environ.get("XBUILD_REQUESTED_BUNDLE_ID", "")
    )
    project_hint = safe_value("project_hint", os.environ.get("XBUILD_PROJECT_HINT", "")).replace(
        "\\", "/"
    )
    if project_hint:
        hint_path = pathlib.PurePosixPath(project_hint)
        if hint_path.is_absolute() or ".." in hint_path.parts or pathlib.PureWindowsPath(project_hint).is_absolute():
            fail("Invalid project hint", "project_hint must stay inside the uploaded source directory.")

    workspaces = sorted(
        (path for path in source_root.rglob("*.xcworkspace") if path.is_dir() and not excluded(path)),
        key=container_score,
    )
    projects = sorted(
        (path for path in source_root.rglob("*.xcodeproj") if path.is_dir() and not excluded(path)),
        key=container_score,
    )
    containers = [("workspace", path) for path in workspaces] + [("project", path) for path in projects]
    containers.sort(
        key=lambda item: hinted_container_rank(item[0], item[1], source_root, project_hint)
    )
    if not containers:
        fail("No Xcode project", "No usable .xcworkspace or .xcodeproj directory was found.")

    # Workspace listings do not expose configurations, so collect them from the
    # user projects once and use the same Release-first policy for every scheme.
    all_configurations: list[str] = []
    for index, project in enumerate(projects):
        try:
            data = load_json_output(
                ["xcodebuild", "-project", str(project), "-list", "-json"],
                log_dir / f"xcode-list-project-{index + 1}.log",
            )
            _, configurations = list_details(data)
            all_configurations.extend(configurations)
        except RuntimeError as exc:
            print(f"::warning title=Project inspection failed::{project.name}: {exc}")
    all_configurations = unique(all_configurations)

    diagnostics: list[str] = []
    discovered_schemes: list[str] = []
    selected: dict[str, Any] | None = None

    for container_index, (kind, container) in enumerate(containers):
        try:
            listing = load_json_output(
                ["xcodebuild", *container_args(kind, container), "-list", "-json"],
                log_dir / f"xcode-list-container-{container_index + 1}.log",
            )
            schemes, local_configurations = list_details(listing)
        except RuntimeError as exc:
            diagnostics.append(f"{container}: {exc}")
            continue

        discovered_schemes.extend(schemes)
        if requested_scheme:
            candidates = [scheme for scheme in schemes if scheme == requested_scheme]
            if not candidates:
                candidates = [
                    scheme for scheme in schemes if scheme.lower() == requested_scheme.lower()
                ]
        else:
            candidates = sorted(schemes, key=lambda item: scheme_score(item, container.stem))

        configurations = unique(local_configurations + all_configurations)
        try:
            candidate_configurations = configuration_candidates(
                requested_configuration, configurations
            )
        except RuntimeError as exc:
            diagnostics.append(f"{container}: {exc}")
            continue

        for scheme_index, scheme in enumerate(candidates):
            for configuration_index, configuration in enumerate(candidate_configurations):
                show_log = log_dir / (
                    f"xcode-settings-{container_index + 1}-{scheme_index + 1}-"
                    f"{configuration_index + 1}.log"
                )
                command = [
                    "xcodebuild",
                    *container_args(kind, container),
                    "-scheme",
                    scheme,
                    "-configuration",
                    configuration,
                    "-sdk",
                    "iphoneos",
                    "-destination",
                    "generic/platform=iOS",
                    "-showBuildSettings",
                    "-json",
                ]
                try:
                    settings_json = load_json_output(command, show_log)
                except RuntimeError as exc:
                    diagnostics.append(f"{container.name} / {scheme} / {configuration}: {exc}")
                    continue
                targets = signable_settings(settings_json)
                if not targets:
                    diagnostics.append(
                        f"{container.name} / {scheme} / {configuration}: no iOS application target"
                    )
                    continue

                if requested_bundle:
                    main_matches = [
                        target for target in targets if target["bundle_identifier"] == requested_bundle
                    ]
                    if not main_matches:
                        diagnostics.append(
                            f"{container.name} / {scheme}: bundle ID is not {requested_bundle}"
                        )
                        continue
                    main_target = sorted(
                        main_matches, key=lambda item: main_target_score(item, scheme)
                    )[0]
                else:
                    main_target = sorted(
                        targets, key=lambda item: main_target_score(item, scheme)
                    )[0]

                selected = {
                    "container_kind": kind,
                    "container_path": str(container),
                    "scheme": scheme,
                    "configuration": configuration,
                    "bundle_identifier": main_target["bundle_identifier"],
                    "main_target": main_target,
                    "signable_targets": targets,
                }
                break
            if selected:
                break
        if selected:
            break

    if not selected:
        available = ", ".join(unique(discovered_schemes)) or "none"
        detail = " | ".join(diagnostics[-6:])
        if requested_scheme:
            fail(
                "Scheme is not buildable",
                f"Requested scheme {requested_scheme!r} could not be used. Shared schemes: {available}. {detail}",
            )
        fail(
            "Could not detect an app scheme",
            f"Shared schemes: {available}. Set the scheme override if needed. {detail}",
        )

    bundle_ids = unique(
        [selected["bundle_identifier"]]
        + [target["bundle_identifier"] for target in selected["signable_targets"]]
    )
    bundle_ids_file = work_dir / "bundle-identifiers.txt"
    bundle_ids_file.write_text("\n".join(bundle_ids) + "\n", encoding="utf-8")
    detected_file = work_dir / "detected-build.json"
    detected_file.write_text(json.dumps(selected, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    append_env(
        env_file,
        {
            "XBUILD_CONTAINER_KIND": selected["container_kind"],
            "XBUILD_CONTAINER_PATH": selected["container_path"],
            "XBUILD_SCHEME": selected["scheme"],
            "XBUILD_CONFIGURATION": selected["configuration"],
            "XBUILD_BUNDLE_IDENTIFIER": selected["bundle_identifier"],
            "XBUILD_BUNDLE_IDS_FILE": str(bundle_ids_file),
            "XBUILD_DETECTED_BUILD_FILE": str(detected_file),
        },
    )

    relative_container = pathlib.Path(selected["container_path"]).relative_to(source_root)
    print(f"Selected {selected['container_kind']}: {relative_container}")
    print(f"Selected shared scheme: {selected['scheme']}")
    print(f"Selected configuration: {selected['configuration']}")
    print(f"Detected main bundle identifier: {selected['bundle_identifier']}")
    if len(bundle_ids) > 1:
        print("Signing will also cover embedded targets: " + ", ".join(bundle_ids[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
