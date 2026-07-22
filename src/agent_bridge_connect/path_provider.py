from __future__ import annotations

import os
import shutil
import subprocess
from glob import glob
from pathlib import Path
from typing import Callable


_MANAGER_TIMEOUT_S = 3


def find_binary(name: str, extra_paths: list[str] | None = None) -> dict:
    """Search for an agent binary across supported install locations."""
    candidates: list[tuple[Path, str]] = []
    env_var = _env_var_for(name)
    env_path = os.environ.get(env_var)
    if env_path:
        candidates.append((Path(env_path).expanduser(), "env_override"))

    for raw_path in extra_paths or []:
        candidates.extend(_candidate_from_extra_path(raw_path, name))

    candidates.extend(_path_candidates(name))
    candidates.extend(_common_user_bin_candidates(name))
    candidates.extend(_package_manager_candidates(name))
    candidates.extend(_version_manager_candidates(name))
    candidates.extend(_macos_candidates(name))
    candidates.extend(_ide_bundled_runtime_candidates(name))

    unique = _unique_candidates(candidates)
    for candidate, source in unique:
        if candidate.is_file():
            return {
                "name": name,
                "found": True,
                "path": str(candidate.resolve()),
                "source": source,
                "searched_paths": [str(path) for path, _ in unique],
                "env_var": env_var,
                "manual_override": _manual_override(name, env_var),
            }

    return {
        "name": name,
        "found": False,
        "path": "",
        "source": "not_found",
        "searched_paths": [str(path) for path, _ in unique],
        "env_var": env_var,
        "manual_override": _manual_override(name, env_var),
    }


def _env_var_for(name: str) -> str:
    normalized = "".join(char if char.isalnum() else "_" for char in name).upper()
    return f"AGENTBC_{normalized}_BIN"


def _manual_override(name: str, env_var: str) -> str:
    return f"{env_var}=/your/path/{name} agentbc setup"


def _candidate_from_extra_path(raw_path: str, name: str) -> list[tuple[Path, str]]:
    path = Path(raw_path).expanduser()
    if path.is_file() or path.name in _executable_names(name):
        return [(path, "extra_path")]
    return [(path / executable, "extra_path") for executable in _executable_names(name)]


def _path_candidates(name: str) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    found = shutil.which(name)
    if found:
        candidates.append((Path(found).expanduser(), "path"))
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory:
            candidates.extend(
                (Path(directory).expanduser() / executable, "path")
                for executable in _executable_names(name)
            )
    return candidates


def _common_user_bin_candidates(name: str) -> list[tuple[Path, str]]:
    home = Path.home()
    directories = [
        (home / ".local" / "bin", "common_user_bin"),
        (home / ".npm-global" / "bin", "npm"),
        (home / ".bun" / "bin", "bun"),
    ]
    extra_paths = os.environ.get("AGENTBC_EXTRA_BIN_PATHS", "")
    for directory in extra_paths.split(os.pathsep):
        if directory:
            directories.append((Path(directory).expanduser(), "extra_path"))
    return _named_candidates(directories, name)


def _package_manager_candidates(name: str) -> list[tuple[Path, str]]:
    manager_commands: list[
        tuple[str, tuple[str, ...], Callable[[str], list[Path]], str]
    ] = [
        ("npm", ("prefix", "-g"), lambda output: [Path(output) / "bin"], "npm"),
        ("pnpm", ("bin", "-g"), lambda output: [Path(output)], "pnpm"),
        ("yarn", ("global", "bin"), lambda output: [Path(output)], "yarn"),
        ("bun", ("pm", "bin", "-g"), lambda output: [Path(output)], "bun"),
    ]
    candidates: list[tuple[Path, str]] = []
    for manager, arguments, to_directories, source in manager_commands:
        manager_path = shutil.which(manager)
        if not manager_path:
            continue
        result = _run_manager(Path(manager_path), *arguments)
        if result is None or result.returncode != 0:
            continue
        output = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if not output:
            continue
        for directory in to_directories(output):
            candidates.extend(
                (directory.expanduser() / executable, source)
                for executable in _executable_names(name)
            )
    return candidates


def _version_manager_candidates(name: str) -> list[tuple[Path, str]]:
    home = Path.home()
    directories: list[tuple[Path, str]] = [
        (home / ".volta" / "bin", "node_manager"),
        (home / ".asdf" / "shims", "version_manager"),
        (home / ".mise" / "shims", "version_manager"),
    ]
    directories.extend(
        (directory, "node_manager")
        for directory in _glob_directories(home / ".nvm" / "versions" / "node" / "*" / "bin")
    )
    directories.extend(
        (directory, "node_manager")
        for directory in _glob_directories(home / ".fnm" / "node-versions" / "*" / "installation" / "bin")
    )
    return _named_candidates(directories, name)


def _macos_candidates(name: str) -> list[tuple[Path, str]]:
    candidates = _named_candidates(
        [
            (Path("/opt/homebrew/bin"), "macos_dir"),
            (Path("/usr/local/bin"), "macos_dir"),
        ],
        name,
    )
    if name == "codex":
        candidates.extend(
            [
                (
                    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
                    "chatgpt_desktop",
                ),
                (
                    Path("/Applications/Codex.app/Contents/Resources/codex"),
                    "codex_desktop_legacy",
                ),
            ]
        )
    return candidates


def _ide_bundled_runtime_candidates(name: str) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for extension_dir, editor in _ide_extension_candidates(name):
        source = f"{editor}_extension_bundled"
        preferred = [
            extension_dir / "bin" / "macos-aarch64" / name,
            extension_dir / "bin" / "darwin-arm64" / name,
            extension_dir / "bin" / "darwin-x64" / name,
            extension_dir / "bin" / name,
            extension_dir / name,
        ]
        candidates.extend((path, source) for path in preferred)
        try:
            for path in extension_dir.rglob(name):
                candidates.append((path, source))
        except OSError:
            continue
    return candidates


def _ide_extension_candidates(name: str) -> list[tuple[Path, str]]:
    home = Path.home()
    bases = [
        (home / ".vscode" / "extensions", "vscode"),
        (home / ".cursor" / "extensions", "cursor"),
        (home / ".windsurf" / "extensions", "windsurf"),
        (home / "Library" / "Application Support" / "Code" / "User" / "extensions", "vscode"),
        (home / "Library" / "Application Support" / "Cursor" / "User" / "extensions", "cursor"),
        (home / "Library" / "Application Support" / "Windsurf" / "User" / "extensions", "windsurf"),
    ]
    prefixes_by_name = {
        "codex": ("openai.chatgpt-", "openai.codex-", "codex-"),
        "claude": ("anthropic.claude-code-", "claude-code-"),
    }
    prefixes = prefixes_by_name.get(name, (f"{name}-",))
    candidates: list[tuple[Path, str]] = []
    for base, editor in bases:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir(), reverse=True):
            if child.is_dir() and child.name.startswith(prefixes):
                candidates.append((child, editor))
    return candidates


def _named_candidates(directories: list[tuple[Path, str]], name: str) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for directory, source in directories:
        candidates.extend((directory / executable, source) for executable in _executable_names(name))
    return candidates


def _executable_names(name: str) -> list[str]:
    names = [name]
    if os.name == "nt":
        names.extend([f"{name}.cmd", f"{name}.exe", f"{name}.bat"])
    return names


def _glob_directories(pattern: Path) -> list[Path]:
    try:
        return [Path(path) for path in glob(str(pattern.expanduser())) if Path(path).is_dir()]
    except OSError:
        return []


def _run_manager(path: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [str(path), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=_MANAGER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _unique_candidates(candidates: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    unique: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, source in candidates:
        marker = str(path.expanduser())
        if marker in seen:
            continue
        seen.add(marker)
        unique.append((path.expanduser(), source))
    return unique
