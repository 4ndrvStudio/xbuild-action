#!/usr/bin/env python3

"""Normalize the legacy public CocoaPods Specs source in one selected Podfile."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


CANONICAL_SOURCE_URL = "https://cdn.cocoapods.org/"
LOCKFILE_TRUNK_SOURCE = "trunk"
SOURCE_DECLARATION = re.compile(
    r"""
    ^(?P<indent>[ \t]*)
    source[ \t]*
    (?P<open_paren>\([ \t]*)?
    (?P<quote>['"])
    (?P<url>[^'"\r\n]+)
    (?P=quote)
    (?(open_paren)[ \t]*\))
    [ \t]*;?[ \t]*
    (?P<comment>\#[^\r\n]*)?
    (?P<newline>\r\n|\n|\r)?
    $
    """,
    re.VERBOSE,
)
LOCKFILE_SOURCE_KEY = re.compile(
    r"^(?P<indent>[ \t]+)(?P<key>.+):[ \t]*$"
)


def _parse_exact_url(url: str):
    if url != url.strip():
        return None
    try:
        parsed = urlsplit(url)
        # Accessing hostname/port also validates malformed bracketed hosts and ports.
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed, hostname.lower()


def _is_legacy_specs_url(url: str) -> bool:
    result = _parse_exact_url(url)
    if result is None:
        return False
    parsed, hostname = result
    if parsed.scheme.lower() not in {"http", "https", "git"} or hostname != "github.com":
        return False

    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path == "/CocoaPods/Specs"


def _is_canonical_specs_url(url: str) -> bool:
    result = _parse_exact_url(url)
    if result is None:
        return False
    parsed, hostname = result
    return (
        parsed.scheme.lower() == "https"
        and hostname == "cdn.cocoapods.org"
        and parsed.path in {"", "/"}
    )


def _resolve_selected_podfile(source_root_arg: str, podfile_arg: str) -> tuple[Path, Path]:
    source_root = Path(source_root_arg).resolve(strict=True)
    podfile = Path(podfile_arg).resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError("the source root is not a directory")
    if podfile.name != "Podfile" or not podfile.is_file():
        raise ValueError("the selected file is not a Podfile")

    try:
        common_path = os.path.commonpath((str(source_root), str(podfile)))
    except ValueError as error:
        raise ValueError("the selected Podfile is outside the extracted source") from error
    if common_path != str(source_root):
        raise ValueError("the selected Podfile is outside the extracted source")
    return source_root, podfile


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".xbuild-podfile-", dir=path.parent, delete=False
        ) as temporary_file:
            temporary_path = temporary_file.name
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _line_body(line: str) -> str:
    return line.rstrip("\r\n")


def _line_ending(line: str) -> str:
    return line[len(_line_body(line)) :]


def _decode_lockfile_source_key(raw_key: str) -> str | None:
    key = raw_key.strip()
    if len(key) >= 2 and key[0] == key[-1] == "'":
        return key[1:-1].replace("''", "'")
    if len(key) >= 2 and key[0] == key[-1] == '"':
        inner = key[1:-1]
        # CocoaPods emits these URL keys unquoted. Only accept an unescaped quoted
        # form so an unusual YAML scalar is never rewritten by approximation.
        return None if "\\" in inner else inner
    return key


def _lockfile_source_kind(raw_key: str) -> str | None:
    key = _decode_lockfile_source_key(raw_key)
    if key is None:
        return None
    if key == LOCKFILE_TRUNK_SOURCE:
        return "trunk"
    if _is_legacy_specs_url(key):
        return "legacy"
    if _is_canonical_specs_url(key):
        return "canonical"
    return None


def _normalize_adjacent_lockfile(podfile: Path) -> tuple[int, int]:
    lockfile = podfile.with_name("Podfile.lock")
    if not lockfile.is_file():
        print("Podfile.lock source normalization: no adjacent lockfile found.")
        return 0, 0

    original_bytes = lockfile.read_bytes()
    has_utf8_bom = original_bytes.startswith(b"\xef\xbb\xbf")
    text_bytes = original_bytes[3:] if has_utf8_bom else original_bytes
    text = text_bytes.decode("utf-8", errors="surrogateescape")
    lines = text.splitlines(keepends=True)

    section_start = next(
        (index for index, line in enumerate(lines) if _line_body(line) == "SPEC REPOS:"),
        None,
    )
    if section_start is None:
        print("Podfile.lock source normalization: no SPEC REPOS section found.")
        return 0, 0

    section_end = len(lines)
    for index in range(section_start + 1, len(lines)):
        body = _line_body(lines[index])
        if body and not body[0].isspace():
            section_end = index
            break

    content_end = section_end
    while content_end > section_start + 1 and not _line_body(lines[content_end - 1]).strip():
        content_end -= 1

    candidates: list[tuple[int, re.Match[str], int]] = []
    for index in range(section_start + 1, content_end):
        match = LOCKFILE_SOURCE_KEY.fullmatch(_line_body(lines[index]))
        if match is None or match.group("key").lstrip().startswith("-"):
            continue
        indentation_width = len(match.group("indent").expandtabs(8))
        candidates.append((index, match, indentation_width))

    if not candidates:
        print("Podfile.lock source normalization: SPEC REPOS has no source keys.")
        return 0, 0

    source_indentation = min(item[2] for item in candidates)
    source_starts = [item for item in candidates if item[2] == source_indentation]
    entries: list[tuple[int, int, re.Match[str], str | None]] = []
    for position, (start, match, _) in enumerate(source_starts):
        end = source_starts[position + 1][0] if position + 1 < len(source_starts) else content_end
        entries.append((start, end, match, _lockfile_source_kind(match.group("key"))))

    legacy_entries = [entry for entry in entries if entry[3] == "legacy"]
    if not legacy_entries:
        print(
            "Podfile.lock source normalization: no exact legacy public CocoaPods "
            "Specs key found; left unchanged."
        )
        return 0, 0

    public_entries = [
        entry for entry in entries if entry[3] in {"legacy", "canonical", "trunk"}
    ]
    first_public_start = public_entries[0][0]
    first_public_match = public_entries[0][2]
    merged_children: list[str] = []
    seen_pod_items: set[str] = set()
    for start, end, _, _ in public_entries:
        for line in lines[start + 1 : end]:
            stripped = _line_body(line).strip()
            if not stripped:
                if start == first_public_start:
                    merged_children.append(line)
                continue
            if stripped.startswith("- "):
                if stripped in seen_pod_items:
                    continue
                seen_pod_items.add(stripped)
            merged_children.append(line)

    replacement = [
        f"{first_public_match.group('indent')}{LOCKFILE_TRUNK_SOURCE}:"
        f"{_line_ending(lines[first_public_start])}",
        *merged_children,
    ]
    public_starts = {entry[0] for entry in public_entries}
    rewritten = lines[: section_start + 1]
    cursor = section_start + 1
    for start, end, _, _ in entries:
        rewritten.extend(lines[cursor:start])
        if start == first_public_start:
            rewritten.extend(replacement)
        elif start not in public_starts:
            rewritten.extend(lines[start:end])
        cursor = end
    rewritten.extend(lines[cursor:])

    rewritten_bytes = "".join(rewritten).encode("utf-8", errors="surrogateescape")
    if has_utf8_bom:
        rewritten_bytes = b"\xef\xbb\xbf" + rewritten_bytes
    if rewritten_bytes != original_bytes:
        mode = lockfile.stat().st_mode & 0o777
        _atomic_write(lockfile, rewritten_bytes, mode)

    duplicates_removed = len(public_entries) - 1
    print(
        f"Normalized {len(legacy_entries)} exact legacy public CocoaPods Specs "
        f"Podfile.lock key(s) to {LOCKFILE_TRUNK_SOURCE}; merged "
        f"{duplicates_removed} duplicate public Specs source key(s). Locked pod "
        "versions and checksums were preserved."
    )
    return len(legacy_entries), duplicates_removed


def normalize(source_root_arg: str, podfile_arg: str) -> tuple[int, int, int, int]:
    _, podfile = _resolve_selected_podfile(source_root_arg, podfile_arg)
    original_bytes = podfile.read_bytes()
    has_utf8_bom = original_bytes.startswith(b"\xef\xbb\xbf")
    text_bytes = original_bytes[3:] if has_utf8_bom else original_bytes
    text = text_bytes.decode("utf-8", errors="surrogateescape")
    lines = text.splitlines(keepends=True)

    declarations: list[tuple[int, re.Match[str], str]] = []
    all_source_declarations: dict[int, re.Match[str]] = {}
    legacy_count = 0
    for index, line in enumerate(lines):
        match = SOURCE_DECLARATION.fullmatch(line)
        if match is None:
            continue
        all_source_declarations[index] = match
        url = match.group("url")
        if _is_legacy_specs_url(url):
            declarations.append((index, match, "legacy"))
            legacy_count += 1
        elif _is_canonical_specs_url(url):
            declarations.append((index, match, "canonical"))

    safe_preamble_indexes: set[int] = set()
    in_source_preamble = True
    for index, line in enumerate(lines):
        stripped = _line_body(line).strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = all_source_declarations.get(index)
        if in_source_preamble and match is not None and not match.group("indent"):
            safe_preamble_indexes.add(index)
            continue
        in_source_preamble = False

    safe_public_declarations = [
        declaration
        for declaration in declarations
        if declaration[0] in safe_preamble_indexes
    ]
    safe_preamble_has_legacy = any(
        kind == "legacy" for _, _, kind in safe_public_declarations
    )
    deduplicated_declarations = (
        safe_public_declarations if safe_preamble_has_legacy else []
    )
    duplicates_removed = max(0, len(deduplicated_declarations) - 1)

    # Do not rewrite a modern Podfile merely because it repeats the CDN source.
    if legacy_count == 0:
        print(
            "Podfile source normalization: no exact legacy public CocoaPods Specs "
            "source URL found; left unchanged."
        )
    else:
        first_deduplicated_index = (
            deduplicated_declarations[0][0] if deduplicated_declarations else None
        )
        deduplicated_indexes = {
            index for index, _, _ in deduplicated_declarations
        }
        declarations_by_index = {
            index: (match, kind) for index, match, kind in declarations
        }
        rewritten: list[str] = []
        for index, line in enumerate(lines):
            declaration = declarations_by_index.get(index)
            if index == first_deduplicated_index:
                match, _ = declaration
                comment = match.group("comment")
                suffix = f" {comment}" if comment else ""
                rewritten.append(
                    f"{match.group('indent')}source '{CANONICAL_SOURCE_URL}'"
                    f"{suffix}{match.group('newline') or ''}"
                )
            elif index in deduplicated_indexes:
                match, _ = declaration
                comment = match.group("comment")
                if comment:
                    rewritten.append(
                        f"{match.group('indent')}{comment}{match.group('newline') or ''}"
                    )
            elif declaration is not None and declaration[1] == "legacy":
                match, _ = declaration
                rewritten.append(
                    line[: match.start("url")]
                    + CANONICAL_SOURCE_URL
                    + line[match.end("url") :]
                )
            else:
                rewritten.append(line)

        rewritten_bytes = "".join(rewritten).encode("utf-8", errors="surrogateescape")
        if has_utf8_bom:
            rewritten_bytes = b"\xef\xbb\xbf" + rewritten_bytes
        mode = podfile.stat().st_mode & 0o777
        _atomic_write(podfile, rewritten_bytes, mode)

        print(
            f"Normalized {legacy_count} exact legacy public CocoaPods Specs source "
            f"declaration(s) to {CANONICAL_SOURCE_URL}; removed "
            f"{duplicates_removed} duplicate top-level public Specs source "
            "declaration(s). Conditional declarations were preserved."
        )

    lock_legacy_count, lock_duplicates_removed = _normalize_adjacent_lockfile(podfile)
    return (
        legacy_count,
        duplicates_removed,
        lock_legacy_count,
        lock_duplicates_removed,
    )


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "Usage: normalize-podfile-sources.py <source-root> <selected-podfile>",
            file=sys.stderr,
        )
        return 2
    try:
        normalize(sys.argv[1], sys.argv[2])
    except (OSError, UnicodeError, ValueError) as error:
        print(
            f"::error title=Podfile source normalization failed::{error}",
            file=sys.stderr,
        )
        return 19
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
