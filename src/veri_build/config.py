#!/usr/bin/env python3
"""Project configuration for Veri (`.veri/veri.toml`).

A project (e.g. an existing Rust crate) opts into Veri by creating a `.veri/`
directory at its root containing a `veri.toml` file. That file declares:

  * `[keys]`      — paths to model access key files (read and exported so the
                    pipeline doesn't have to be handed `ANTHROPIC_API_KEY=…` by
                    hand on every invocation).
  * `[[modules]]` — one table per spec, mapping a named `*.veri.md` spec to the
                    output directory its clean artifacts should land in.

Example `.veri/veri.toml`:

    [keys]
    anthropic = "~/.veri-api-key"

    [[modules]]
    name   = "trust_engine"
    spec   = "src/trust_engine.veri.md"
    output = "src/trust_engine/kernel/"
    # optional: target, module, timeout, agent

All `spec`/`output` paths are relative to the project root (the directory that
contains `.veri/`).
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

CONFIG_DIRNAME = ".veri"
CONFIG_FILENAME = "veri.toml"


class ConfigError(Exception):
    """Raised when a project config is missing, malformed, or inconsistent."""


@dataclass
class ModuleConfig:
    """A single spec → output mapping from `[[modules]]`."""

    name: str
    spec: Path  # absolute, resolved against the project root
    output: Path  # absolute, resolved against the project root
    target: Optional[str] = None  # e.g. "dafny-rust"; default: read from spec
    module_name: Optional[str] = None  # override; default: derived from filename
    timeout: Optional[int] = None  # agent timeout seconds; default: pipeline default
    agent: Optional[str] = None  # "claude" | "openclaw"; default: pipeline default


@dataclass
class VeriProjectConfig:
    """Parsed `.veri/veri.toml` for a project."""

    root: Path  # the directory containing `.veri/`
    keys: Dict[str, str] = field(default_factory=dict)  # provider → key file path
    modules: List[ModuleConfig] = field(default_factory=list)


def find_project_root(start: Optional[Path] = None) -> Path:
    """Walk up from ``start`` (default: cwd) until a `.veri/veri.toml` is found.

    Returns the directory that *contains* `.veri/` (the project root).

    Raises:
        ConfigError: if no `.veri/veri.toml` is found in any ancestor.
    """
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        if (directory / CONFIG_DIRNAME / CONFIG_FILENAME).is_file():
            return directory
    raise ConfigError(
        f"no {CONFIG_DIRNAME}/{CONFIG_FILENAME} found in {start} or any parent. "
        f"Create one at your project root to use `veri build`."
    )


def load_config(root: Path) -> VeriProjectConfig:
    """Load and validate `<root>/.veri/veri.toml`.

    Paths in the config are expanded (``~``) and resolved against ``root``.

    Raises:
        ConfigError: on a missing/malformed file, missing required fields,
            duplicate module names, or a spec path that does not exist.
    """
    root = root.resolve()
    config_path = root / CONFIG_DIRNAME / CONFIG_FILENAME
    if not config_path.is_file():
        raise ConfigError(f"config not found: {config_path}")

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"could not parse {config_path}: {e}") from e

    keys_raw = data.get("keys", {})
    if not isinstance(keys_raw, dict):
        raise ConfigError(f"{config_path}: [keys] must be a table")
    keys = {str(k): str(v) for k, v in keys_raw.items()}

    modules_raw = data.get("modules", [])
    if not isinstance(modules_raw, list):
        raise ConfigError(f"{config_path}: [[modules]] must be an array of tables")

    modules: List[ModuleConfig] = []
    seen: set = set()
    for i, entry in enumerate(modules_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"{config_path}: modules[{i}] must be a table")
        for required in ("name", "spec", "output"):
            if required not in entry:
                raise ConfigError(
                    f"{config_path}: modules[{i}] is missing required field '{required}'"
                )
        name = str(entry["name"])
        if name in seen:
            raise ConfigError(f"{config_path}: duplicate module name '{name}'")
        seen.add(name)

        spec = _resolve(root, str(entry["spec"]))
        if not spec.is_file():
            raise ConfigError(
                f"{config_path}: module '{name}' spec not found: {spec}"
            )
        output = _resolve(root, str(entry["output"]))

        timeout = entry.get("timeout")
        if timeout is not None and not isinstance(timeout, int):
            raise ConfigError(
                f"{config_path}: module '{name}' timeout must be an integer"
            )

        modules.append(
            ModuleConfig(
                name=name,
                spec=spec,
                output=output,
                target=_opt_str(entry.get("target")),
                module_name=_opt_str(entry.get("module")),
                timeout=timeout,
                agent=_opt_str(entry.get("agent")),
            )
        )

    return VeriProjectConfig(root=root, keys=keys, modules=modules)


def lookup_module(config: VeriProjectConfig, name: str) -> ModuleConfig:
    """Return the module named ``name``.

    Raises:
        ConfigError: if no module with that name is configured.
    """
    for mod in config.modules:
        if mod.name == name:
            return mod
    available = ", ".join(m.name for m in config.modules) or "(none)"
    raise ConfigError(
        f"no module named '{name}' in config. Available modules: {available}"
    )


def resolve_key(config: VeriProjectConfig, provider: str) -> Optional[str]:
    """Read the key file configured for ``provider`` and return its contents.

    Returns ``None`` if no key is configured for the provider. Whitespace
    (including trailing newlines) is stripped.

    Raises:
        ConfigError: if a key path is configured but the file is unreadable.
    """
    raw = config.keys.get(provider)
    if not raw:
        return None
    key_path = Path(raw).expanduser()
    if not key_path.is_file():
        raise ConfigError(
            f"key file for '{provider}' not found: {key_path}"
        )
    try:
        return key_path.read_text().strip()
    except OSError as e:
        raise ConfigError(f"could not read key file {key_path}: {e}") from e


def _resolve(root: Path, raw: str) -> Path:
    """Expand ``~`` and resolve ``raw`` against ``root`` if relative."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _opt_str(value) -> Optional[str]:
    return None if value is None else str(value)
