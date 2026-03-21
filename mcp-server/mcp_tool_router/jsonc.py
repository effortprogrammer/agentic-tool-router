from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_opencode_config_path() -> str:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    json_path = base / "opencode" / "opencode.json"
    jsonc_path = json_path.with_suffix(".jsonc")
    return str(jsonc_path if jsonc_path.exists() else json_path)


def load_jsonc_file(path: str | Path) -> Any:
    expanded = Path(path).expanduser()
    return parse_jsonc(expanded.read_text(encoding="utf-8"))


def parse_jsonc(raw: str) -> Any:
    normalized = _strip_trailing_commas(_strip_json_comments(raw.lstrip("\ufeff")))
    return json.loads(normalized)


def _strip_json_comments(raw: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    in_line_comment = False
    in_block_comment = False
    i = 0

    while i < len(raw):
        char = raw[i]
        nxt = raw[i + 1] if i + 1 < len(raw) else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                result.append(char)
            else:
                result.append(" ")
            i += 1
            continue

        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                result.extend([" ", " "])
                i += 2
                continue
            result.append("\n" if char == "\n" else " ")
            i += 1
            continue

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            i += 1
            continue

        if char == "/" and nxt == "/":
            in_line_comment = True
            result.extend([" ", " "])
            i += 2
            continue

        if char == "/" and nxt == "*":
            in_block_comment = True
            result.extend([" ", " "])
            i += 2
            continue

        result.append(char)
        i += 1

    return "".join(result)


def _strip_trailing_commas(raw: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    i = 0

    while i < len(raw):
        char = raw[i]

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            i += 1
            continue

        if char == ",":
            j = i + 1
            while j < len(raw) and raw[j].isspace():
                j += 1
            if j < len(raw) and raw[j] in "}]":
                i += 1
                continue

        result.append(char)
        i += 1

    return "".join(result)
