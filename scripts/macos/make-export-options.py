#!/usr/bin/env python3
"""Generate an ExportOptions.plist from the selected signing map."""

from __future__ import annotations

import json
import pathlib
import plistlib
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: make-export-options.py <signing-map.json> <ExportOptions.plist>")
        return 2
    signing = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    semantic_method = signing["export_method"]
    export_methods = {
        "development": "debugging",
        "ad-hoc": "release-testing",
        "app-store": "app-store-connect",
    }
    if semantic_method not in export_methods:
        print(f"Unsupported export method: {semantic_method}")
        return 2
    options = {
        "destination": "export",
        "manageAppVersionAndBuildNumber": False,
        "method": export_methods[semantic_method],
        "provisioningProfiles": {
            bundle_id: profile["name"]
            for bundle_id, profile in signing["profiles"].items()
        },
        "signingCertificate": signing["certificate_identity"],
        "signingStyle": "manual",
        "stripSwiftSymbols": True,
        "teamID": signing["team_id"],
        "thinning": "<none>",
    }
    with pathlib.Path(sys.argv[2]).open("wb") as handle:
        plistlib.dump(options, handle, fmt=plistlib.FMT_XML, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
