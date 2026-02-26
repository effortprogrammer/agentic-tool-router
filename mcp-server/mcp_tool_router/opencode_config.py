from __future__ import annotations

import argparse
import json
import os
import warnings
from pathlib import Path
from typing import Any

_WELL_KNOWN_REMOTE_MCPS: dict[str, dict[str, Any]] = {
    "context7": {
        "type": "remote",
        "url": "https://mcp.context7.com/mcp",
        "enabled": False,
    },
    "grep_app": {
        "type": "remote",
        "url": "https://mcp.grep.app",
        "enabled": False,
    },
    "websearch": {
        "type": "remote",
        "url": "https://mcp.exa.ai/mcp?tools=web_search_exa",
        "enabled": False,
    },
}


def apply_router_config(
    config_path: str,
    router_id: str = "router",
    router_command: list[str] | None = None,
    disable_others: bool = True,
    create_backup: bool = True,
) -> dict[str, Any]:
    warnings.warn(
        "apply_router_config() is deprecated. Router MCP registration is removed; "
        "this function now cleans up legacy router MCP entries.",
        DeprecationWarning,
        stacklevel=2,
    )
    del router_command

    payload, path = _load_config(config_path)
    mcp = _ensure_mcp(payload)

    _cleanup_router_entry(mcp, router_id)

    for remote_id, remote_entry in _WELL_KNOWN_REMOTE_MCPS.items():
        if remote_id not in mcp:
            mcp[remote_id] = dict(remote_entry)

    if disable_others:
        for entry in mcp.values():
            if isinstance(entry, dict):
                entry["enabled"] = False

    _write_config(path, payload, create_backup=create_backup)
    _disable_oh_my_opencode_mcps(path.parent, create_backup=create_backup)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "[DEPRECATED] Cleanup legacy router MCP entries in OpenCode config. "
            "Gateway injection is now used instead."
        )
    )
    parser.add_argument(
        "--config",
        default="~/.config/opencode/opencode.json",
        help="Path to opencode.json",
    )
    parser.add_argument(
        "--router-id", default="router", help="Legacy router MCP id to remove"
    )
    parser.add_argument(
        "--disable-others",
        dest="disable_others",
        action="store_true",
        help="Disable all other MCP entries (default)",
    )
    parser.add_argument(
        "--keep-others",
        dest="disable_others",
        action="store_false",
        help="Keep existing enabled flags for other MCP entries",
    )
    parser.set_defaults(disable_others=True)
    parser.add_argument(
        "--no-backup",
        dest="create_backup",
        action="store_false",
        help="Do not create a .bak backup",
    )
    parser.set_defaults(create_backup=True)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print changes without writing"
    )

    args = parser.parse_args()

    warnings.warn(
        "opencode_config.py CLI is deprecated. It now only removes legacy router MCP "
        "entries and preserves gateway-compatible config.",
        DeprecationWarning,
        stacklevel=2,
    )

    payload, path = _load_config(args.config)
    mcp = _ensure_mcp(payload)

    _cleanup_router_entry(mcp, args.router_id)

    for remote_id, remote_entry in _WELL_KNOWN_REMOTE_MCPS.items():
        if remote_id not in mcp:
            mcp[remote_id] = dict(remote_entry)

    if args.disable_others:
        for entry in mcp.values():
            if isinstance(entry, dict):
                entry["enabled"] = False

    if args.dry_run:
        _print_payload(payload)
        return 0

    _write_config(path, payload, create_backup=args.create_backup)
    _disable_oh_my_opencode_mcps(path.parent, create_backup=args.create_backup)
    return 0


_OH_MY_OPENCODE_BUILTIN_MCPS = ["context7", "grep_app", "websearch"]


def _cleanup_router_entry(mcp: dict[str, Any], router_id: str) -> None:
    mcp.pop("router", None)
    if router_id != "router":
        mcp.pop(router_id, None)


def _disable_oh_my_opencode_mcps(config_dir: Path, *, create_backup: bool) -> None:
    omo_path = config_dir / "oh-my-opencode.json"

    omo_payload: dict[str, Any] = {}
    if omo_path.exists():
        try:
            raw = json.loads(omo_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                omo_payload = raw
        except (json.JSONDecodeError, OSError):
            return

    existing: list[str] = (
        omo_payload.get("disabled_mcps", [])
        if isinstance(omo_payload.get("disabled_mcps"), list)
        else []
    )
    merged = list(dict.fromkeys(existing + _OH_MY_OPENCODE_BUILTIN_MCPS))

    if merged == existing:
        return

    omo_payload["disabled_mcps"] = merged
    if create_backup and omo_path.exists():
        backup = omo_path.with_suffix(omo_path.suffix + ".bak")
        backup.write_text(omo_path.read_text(encoding="utf-8"), encoding="utf-8")
    config_dir.mkdir(parents=True, exist_ok=True)
    omo_path.write_text(json.dumps(omo_payload, indent=2), encoding="utf-8")
    print(
        f"Disabled oh-my-opencode built-in MCPs ({', '.join(_OH_MY_OPENCODE_BUILTIN_MCPS)})"
        " - now routed through the gateway."
    )


def _load_config(config_path: str) -> tuple[dict[str, Any], Path]:
    path = Path(os.path.expanduser(config_path))
    if not path.exists():
        return {}, path
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("OpenCode config must be a JSON object.")
    return payload, path


def _ensure_mcp(payload: dict[str, Any]) -> dict[str, Any]:
    if "mcp" not in payload or payload["mcp"] is None:
        payload["mcp"] = {}
    if not isinstance(payload["mcp"], dict):
        raise ValueError("OpenCode config 'mcp' field must be an object.")
    return payload["mcp"]


def _write_config(path: Path, payload: dict[str, Any], create_backup: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_backup and path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _print_payload(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
