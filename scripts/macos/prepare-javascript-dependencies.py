#!/usr/bin/env python3
"""Restore the selected React Native app before CocoaPods evaluates its Podfile."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


COREPACK_VERSION = "0.34.1"
LOCKFILES = {
    "npm": ("package-lock.json", "npm-shrinkwrap.json"),
    "yarn": ("yarn.lock",),
    "pnpm": ("pnpm-lock.yaml",),
}


def ancestors(directory, root):
    while directory == root or root in directory.parents:
        yield directory
        if directory == root:
            break
        directory = directory.parent


def run(command, directory, environment, capture=False):
    print(f"Running in {directory}: {shlex.join(command)}", flush=True)
    return subprocess.run(command, cwd=directory, env=environment, check=True,
                          text=True, stdout=subprocess.PIPE if capture else None)


def prepare(source_root, podfile):
    pod_dir = podfile.parent
    pod_text = "\n".join(line for line in podfile.read_text(encoding="utf-8-sig").splitlines()
                         if not line.lstrip().startswith("#"))
    uses_expo = bool(re.search(r"expo/package\.json|use_expo_modules!?", pod_text))
    uses_react_native = uses_expo or bool(re.search(
        r"react-native|react_native_pods|use_react_native!?|use_native_modules!?", pod_text))
    app_dir = None
    manifest = None
    for directory in ancestors(pod_dir, source_root):
        package_file = directory / "package.json"
        if not package_file.is_file():
            continue
        try:
            candidate = json.loads(package_file.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as error:
            if uses_react_native:
                raise ValueError(f"Cannot read {package_file}: {error}") from error
            continue
        if not isinstance(candidate, dict):
            if uses_react_native:
                raise ValueError(f"{package_file} must contain a JSON object.")
            continue
        dependencies = {}
        for field in ("dependencies", "devDependencies"):
            value = candidate.get(field, {})
            if isinstance(value, dict):
                dependencies.update(value)
        if "react-native" in dependencies or "expo" in dependencies:
            app_dir, manifest = directory, candidate
            uses_expo = uses_expo or "expo" in dependencies
            uses_react_native = True
            break

    # Native exports retain their existing local-Gemfile behavior.
    gemfile = pod_dir / "Gemfile"
    if not uses_react_native:
        print("Native Podfile selected; no React Native JavaScript dependencies are required.")
        return {"projectRoot": None, "gemfile": str(gemfile) if gemfile.is_file() else None}
    if app_dir is None:
        raise ValueError(
            "This React Native/Expo Podfile needs JavaScript dependencies, but no containing "
            "package.json declaring react-native or expo was uploaded. Select the full app root "
            "containing package.json, the lockfile, JavaScript source, and ios/, not just ios/. "
            "XBuild requires an existing iOS project; it does not run Expo prebuild.")

    node = shutil.which("node")
    if not node:
        raise ValueError("Node.js is required for React Native/Expo. Configure setup-node before CocoaPods.")
    declared = manifest.get("packageManager")
    if declared is not None and (not isinstance(declared, str) or not re.fullmatch(
            r"(?:npm|yarn|pnpm)@\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+sha(?:224|256|384|512)\.[A-Za-z0-9+/=]+)?",
            declared)):
        raise ValueError("Unsupported packageManager. Set package.json packageManager to an exact "
                         "npm, yarn, or pnpm version (for example yarn@4.9.2).")
    locked_managers = [manager for manager, names in LOCKFILES.items()
                       if any((app_dir / name).is_file() for name in names)]
    if not declared and len(locked_managers) > 1:
        raise ValueError("Multiple JavaScript package-manager lockfiles were uploaded. Set packageManager "
                         "in package.json or keep only the lockfile used by this app.")
    manager = declared.split("@", 1)[0] if declared else next(iter(locked_managers), "npm")
    has_lock = manager in locked_managers
    if declared and locked_managers and not has_lock:
        raise ValueError(f"packageManager selects {manager}, but only another manager's lockfile exists. "
                         "Upload the matching lockfile or correct packageManager.")
    if (app_dir / "bun.lock").is_file() or (app_dir / "bun.lockb").is_file():
        if not declared and not locked_managers:
            raise ValueError("Bun dependency installs are not supported. Supply an npm, yarn, or pnpm lockfile "
                             "and set the corresponding packageManager in package.json.")

    environment = os.environ.copy()
    environment.pop("YARN_PRODUCTION", None)
    environment.update({"NODE_ENV": "development", "NPM_CONFIG_PRODUCTION": "false",
                        "COREPACK_ENABLE_AUTO_PIN": "0",
                        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", "COREPACK_DEFAULT_TO_LATEST": "0"})
    if manager == "npm" and not declared:
        executable = shutil.which("npm")
        if not executable:
            raise ValueError("npm is missing from the runner's Node.js installation.")
        command = [executable]
    else:
        corepack = shutil.which("corepack")
        if corepack:
            command = [corepack, manager]
        else:
            npx = shutil.which("npx")
            if not npx:
                raise ValueError(f"Corepack or npx is required to run the selected {manager} version.")
            command = [npx, "--yes", f"--package=corepack@{COREPACK_VERSION}", "corepack", manager]
    if not has_lock:
        print(f"::warning title=JavaScript lockfile missing::No {manager} lockfile found in {app_dir}; "
              "dependency versions may change. Commit and upload the app's lockfile for reproducible builds.")
    print(f"Preparing {'Expo / React Native' if uses_expo else 'React Native'} dependencies from {app_dir} "
          f"using {declared or manager}.", flush=True)
    if manager == "npm":
        arguments = ["ci" if has_lock else "install", "--include=dev"]
    elif manager == "pnpm":
        arguments = ["install", "--frozen-lockfile" if has_lock else "--no-frozen-lockfile", "--prod=false"]
    else:
        version = run(command + ["--version"], app_dir, environment, capture=True).stdout.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].*)?", version):
            raise ValueError(f"Could not determine the selected Yarn version: {version!r}")
        if int(version.split(".")[0]) == 1:
            arguments = ["install", "--non-interactive", "--production=false"]
            if has_lock:
                arguments.append("--frozen-lockfile")
        else:
            # RN native scripts resolve files from node_modules. PnP alone cannot satisfy them.
            environment["YARN_NODE_LINKER"] = "node-modules"
            environment["YARN_ENABLE_IMMUTABLE_INSTALLS"] = "true" if has_lock else "false"
            arguments = ["install"] + (["--immutable"] if has_lock else [])
    run(command + arguments, app_dir, environment)

    for package in (["react-native", "expo"] if uses_expo else ["react-native"]):
        try:
            run([node, "--print", f"require.resolve('{package}/package.json')"], pod_dir, environment)
        except subprocess.CalledProcessError as error:
            raise ValueError(f"Installed dependencies cannot resolve {package}/package.json from {pod_dir}. "
                             "Check the app's dependency declarations and upload its full project root.") from error
    node_binary = run([node, "--print", "process.execPath"], pod_dir, environment, capture=True).stdout.strip()
    if not node_binary or "\n" in node_binary or "\r" in node_binary:
        raise ValueError("Node.js returned an invalid executable path.")
    local_env = pod_dir / ".xcode.env.local"
    original = local_env.read_text(encoding="utf-8-sig") if local_env.exists() else ""
    retained = [line for line in original.splitlines()
                if not re.match(r"^\s*(?:export\s+)?NODE_BINARY\s*=", line)
                and line != "# XBuild: use the Node.js executable installed on this runner."]
    retained.extend(["# XBuild: use the Node.js executable installed on this runner.",
                     f"export NODE_BINARY={shlex.quote(node_binary)}"])
    local_env.write_text("\n".join(retained) + "\n", encoding="utf-8")
    print(f"Configured runner Node.js in {local_env}; .xcode.env is preserved.")
    gemfile = next((directory / "Gemfile" for directory in ancestors(pod_dir, source_root)
                    if (directory / "Gemfile").is_file()), None)
    return {"projectRoot": str(app_dir), "gemfile": str(gemfile) if gemfile else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("podfile", type=Path)
    parser.add_argument("metadata_file", type=Path)
    args = parser.parse_args()
    root, podfile = args.source_root.resolve(), args.podfile.resolve()
    try:
        if root not in podfile.parents:
            raise ValueError("The selected Podfile must be inside the uploaded source.")
        metadata = prepare(root, podfile)
        args.metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error title=JavaScript dependency preparation failed::{error}", file=sys.stderr)
        return 21
    return 0


if __name__ == "__main__":
    sys.exit(main())
