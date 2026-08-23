#!/usr/bin/env python3
"""Guarded two-version Homebrew RC driver for PKG-103-001.

The default mode is read-only preflight.  A real Homebrew mutation requires
both ``--execute`` and ``AGENTBC_HOMEBREW_RC_RUN=1``.  The driver refuses to
start when Xcode/CLT/Homebrew health is unsupported, Formula dependencies are
not already installed, AgentBC is already Homebrew-owned, or the service test
would collide with an existing AgentBC Runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


GATE_ENV = "AGENTBC_HOMEBREW_RC_RUN"
FORMULA = "agentbc"
DEFAULT_TAP = "roway49/agentbc-rc"
TEMP_PREFIX = "agentbc-homebrew-rc-"
MIN_FREE_GIB = 5
BREW_GUARD_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
    "HOMEBREW_NO_AUTOREMOVE": "1",
    "HOMEBREW_NO_ANALYTICS": "1",
}
VOLATILE_NAMES = frozenset({"run_lease.json"})
VOLATILE_SUFFIXES = (".run.temp",)
LEASE_STABLE_FIELDS = (
    "run_id",
    "task_id",
    "executor_id",
    "pid",
    "pgid",
    "work_dir",
    "started_at",
    "cleanup_strategy",
    "state",
)
_VERSION_RE = re.compile(r'^\s*version\s+"([^"]+)"', re.MULTILINE)
_DEPENDENCY_RE = re.compile(r'^\s*depends_on\s+"([^"]+)"', re.MULTILINE)
_VERSION_VALUE_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$"
)


class RcError(RuntimeError):
    """The guarded RC flow could not proceed safely."""


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    def evidence(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass
class CommandRunner:
    environment: dict[str, str]
    commands: list[dict[str, Any]] = field(default_factory=list)

    def run(
        self,
        argv: Iterable[str | Path],
        *,
        check: bool = False,
        timeout: int = 300,
        input_text: str | None = None,
    ) -> CommandResult:
        command = [str(part) for part in argv]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
            env=self.environment,
        )
        result = CommandResult(
            argv=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        self.commands.append(result.evidence())
        if check and completed.returncode != 0:
            raise RcError(
                f"command failed ({completed.returncode}): " + " ".join(command)
            )
        return result


def brew_environment(
    base: dict[str, str] | None = None,
    *,
    ca_bundle: Path | None = None,
    curlrc: Path | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    environment.update(BREW_GUARD_ENV)
    if ca_bundle is not None:
        environment["SSL_CERT_FILE"] = str(ca_bundle)
    if curlrc is not None:
        environment["HOMEBREW_CURLRC"] = str(curlrc)
    return environment


def formula_version(path: Path) -> str:
    matched = _VERSION_RE.search(path.read_text(encoding="utf-8"))
    if matched is None:
        raise RcError(f"Formula has no pinned version: {path}")
    return matched.group(1)


def formula_dependencies(path: Path) -> list[str]:
    content = path.read_text(encoding="utf-8")
    return sorted(set(_DEPENDENCY_RE.findall(content)))


def version_key(value: str) -> tuple[int, ...]:
    matched = _VERSION_VALUE_RE.fullmatch(value)
    if matched is None:
        raise RcError(f"Unsupported Formula version: {value}")
    pre = matched.group(4)
    return (
        int(matched.group(1)),
        int(matched.group(2)),
        int(matched.group(3)),
        {"a": 0, "b": 1, "rc": 2, None: 3}[pre],
        int(matched.group(5) or 0),
    )


def _volatile(relative: Path) -> bool:
    return relative.name in VOLATILE_NAMES or relative.name.endswith(VOLATILE_SUFFIXES)


def stable_tree_sha256(root: Path) -> str:
    """Hash stable bytes without treating Runner heartbeat files as user drift."""
    digest = hashlib.sha256()
    root = root.expanduser()
    if not root.exists():
        digest.update(b"missing\0")
        return digest.hexdigest()
    if root.is_symlink():
        digest.update(f"link\0{os.readlink(root)}\0".encode())
        return digest.hexdigest()
    if root.is_file():
        digest.update(root.read_bytes())
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _volatile(relative):
            continue
        name = relative.as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + name + b"\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + name + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def lease_semantics(record_root: Path) -> dict[str, dict[str, Any]]:
    """Capture stable RunLease identity while allowing heartbeat timestamps to move."""
    result: dict[str, dict[str, Any]] = {}
    if not record_root.is_dir():
        return result
    for path in sorted(record_root.rglob("run_lease.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            result[path.relative_to(record_root).as_posix()] = {"invalid": True}
            continue
        if not isinstance(value, dict):
            result[path.relative_to(record_root).as_posix()] = {"invalid": True}
            continue
        result[path.relative_to(record_root).as_posix()] = {
            key: value.get(key) for key in LEASE_STABLE_FIELDS
        }
    return result


def snapshot_preserved(paths: Iterable[Path], record_root: Path) -> dict[str, Any]:
    return {
        "stable_paths": {
            str(path.expanduser()): stable_tree_sha256(path) for path in paths
        },
        "run_leases": lease_semantics(record_root.expanduser()),
    }


def snapshot_brew(runner: CommandRunner, brew: Path) -> dict[str, Any]:
    formulae = runner.run([brew, "list", "--formula", "--versions"])
    taps = runner.run([brew, "tap"])
    services = runner.run([brew, "services", "list", "--json"])
    trust = runner.run([brew, "trust", "--json=v1"])
    return {
        "formulae": sorted(line.strip() for line in formulae.stdout.splitlines() if line.strip()),
        "taps": sorted(line.strip() for line in taps.stdout.splitlines() if line.strip()),
        "services": _json_or_text(services.stdout),
        "trust": _json_or_text(trust.stdout),
    }


def _json_or_text(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip()


def ca_extensions_present(text: str) -> bool:
    required = (
        "X509v3 Subject Key Identifier",
        "X509v3 Authority Key Identifier",
    )
    return all(item in text for item in required)


def server_extensions_present(text: str) -> bool:
    required = (
        "X509v3 Subject Alternative Name",
        "X509v3 Subject Key Identifier",
        "X509v3 Authority Key Identifier",
    )
    return all(item in text for item in required)


def create_combined_ca_bundle(test_ca: Path, output: Path) -> Path:
    defaults = ssl.get_default_verify_paths()
    candidates = [Path(defaults.cafile)] if defaults.cafile else []
    candidates.extend((Path("/etc/ssl/cert.pem"), Path("/private/etc/ssl/cert.pem")))
    system_ca = next((path for path in candidates if path.is_file()), None)
    if system_ca is None:
        raise RcError("No readable system CA bundle is available")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(system_ca.read_bytes().rstrip() + b"\n" + test_ca.read_bytes())
    return output


def create_curlrc(bundle: Path, output: Path) -> Path:
    output.write_text(f'cacert = "{bundle}"\n', encoding="utf-8")
    return output


def default_preserve_paths(home: Path) -> list[Path]:
    return [
        home / ".abc",
        home / ".codex" / "skills" / "agentbc",
        home / ".claude" / "skills" / "agentbc",
        home / ".hermes" / "skills" / "agentbc",
        home / "Documents" / "AgentBC" / "workspace",
    ]


def command_plan(tap: str, *, service: bool) -> list[list[str]]:
    formula_ref = f"{tap}/{FORMULA}"
    commands = [
        ["brew", "tap", tap, "file://<TEMP_TAP_SOURCE>"],
        ["brew", "trust", "--formula", formula_ref],
        ["brew", "install", formula_ref],
        ["brew", "test", formula_ref],
        ["<BREW_AGENTBC>", "--version"],
        ["<BREW_AGENTBC>", "--help"],
        ["git", "-C", "<TAP_CHECKOUT>", "pull", "--ff-only"],
        ["brew", "upgrade", formula_ref],
        ["<BREW_AGENTBC>", "update"],
    ]
    if service:
        commands.extend(
            (
                ["brew", "services", "start", FORMULA],
                ["<BREW_AGENTBC>", "doctor", "--json"],
                ["brew", "services", "stop", FORMULA],
            )
        )
    commands.extend(
        (
            ["brew", "uninstall", "--force", FORMULA],
            ["brew", "untrust", "--formula", formula_ref],
            ["brew", "untap", tap],
        )
    )
    return commands


def _probe_feed(runner: CommandRunner, feed_url: str, python: Path) -> None:
    curl = ["/usr/bin/curl"]
    curlrc = runner.environment.get("HOMEBREW_CURLRC")
    if curlrc:
        curl.extend(("--config", curlrc))
    curl.extend(("--fail", "--silent", "--show-error", "--head", feed_url))
    runner.run(curl, check=True, timeout=30)
    runner.run(
        [
            python,
            "-c",
            (
                "import os,ssl,urllib.request; "
                "cafile=os.environ.get('SSL_CERT_FILE'); "
                "context=ssl.create_default_context(cafile=cafile) if cafile "
                "else ssl.create_default_context(); "
                f"urllib.request.urlopen({feed_url!r}, context=context, "
                "timeout=20).read(1)"
            ),
        ],
        check=True,
        timeout=30,
    )


def _preflight(
    runner: CommandRunner,
    *,
    brew: Path,
    old_formula: Path,
    new_formula: Path,
    feed_url: str,
    ca_cert: Path | None,
    server_cert: Path | None,
    service: bool,
    min_free_gib: int,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    def checked(name: str, argv: list[str | Path], *, strict: bool = True) -> CommandResult:
        result = runner.run(argv)
        checks.append({"name": name, "returncode": result.returncode})
        if strict and result.returncode != 0:
            blockers.append(name)
        return result

    version = checked("brew_version", [brew, "--version"])
    config = checked("brew_config", [brew, "config"])
    doctor = checked("brew_doctor", [brew, "doctor"])
    prefix_result = checked("brew_prefix", [brew, "--prefix"])
    checked("xcode_select", ["/usr/bin/xcode-select", "-p"])
    checked(
        "clt_receipt",
        ["/usr/sbin/pkgutil", "--pkg-info=com.apple.pkg.CLTools_Executables"],
    )
    arch = checked("architecture", ["/usr/bin/uname", "-m"])
    if arch.stdout.strip() not in {"arm64", "x86_64"}:
        blockers.append("unsupported_architecture")

    if version_key(formula_version(old_formula)) >= version_key(formula_version(new_formula)):
        blockers.append("formula_version_order")
    dependencies = sorted(set(formula_dependencies(old_formula) + formula_dependencies(new_formula)))
    for dependency in dependencies:
        result = checked(
            f"dependency:{dependency}",
            [brew, "list", "--versions", dependency],
            strict=False,
        )
        if not result.stdout.strip():
            blockers.append(f"dependency_missing:{dependency}")

    existing = checked(
        "agentbc_not_homebrew_owned",
        [brew, "list", "--versions", FORMULA],
        strict=False,
    )
    if existing.returncode == 0 and existing.stdout.strip():
        blockers.append("agentbc_already_homebrew_owned")

    prefix = Path(prefix_result.stdout.strip()) if prefix_result.returncode == 0 else Path("/")
    if prefix.exists() and shutil.disk_usage(prefix).free < min_free_gib * 1024**3:
        blockers.append("insufficient_disk")

    if (ca_cert is None) != (server_cert is None):
        blockers.append("test_tls_material_incomplete")
    if ca_cert is not None:
        certificate = checked(
            "test_ca_extensions",
            ["/usr/bin/openssl", "x509", "-in", ca_cert, "-noout", "-text"],
        )
        if not ca_extensions_present(certificate.stdout):
            blockers.append("test_ca_missing_extensions")
    if server_cert is not None:
        certificate = checked(
            "test_server_extensions",
            ["/usr/bin/openssl", "x509", "-in", server_cert, "-noout", "-text"],
        )
        if not server_extensions_present(certificate.stdout):
            blockers.append("test_server_missing_extensions")
    if not feed_url.startswith("https://"):
        blockers.append("feed_not_https")
    else:
        try:
            _probe_feed(runner, feed_url, Path(sys.executable))
            checks.append({"name": "https_feed_curl_and_python", "returncode": 0})
        except RcError:
            blockers.append("https_feed_probe")
            checks.append({"name": "https_feed_curl_and_python", "returncode": 1})

    if service:
        current_runner = checked(
            "existing_runner_stopped",
            ["agentbc", "runner", "status"],
            strict=False,
        )
        if current_runner.returncode == 0 and '"status": "ready"' in current_runner.stdout:
            blockers.append("existing_runner_active")

    return {
        "ok": not blockers,
        "blockers": sorted(set(blockers)),
        "checks": checks,
        "brew_version": version.stdout.splitlines()[0] if version.stdout else "",
        "brew_config_sha256": hashlib.sha256(config.stdout.encode()).hexdigest(),
        "brew_doctor_sha256": hashlib.sha256(doctor.stdout.encode()).hexdigest(),
        "dependencies": dependencies,
    }


def _write_formula(source: Path, formula: Path) -> None:
    destination = source / "Formula" / f"{FORMULA}.rb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(formula, destination)


def _git_commit_formula(runner: CommandRunner, source: Path, message: str) -> None:
    runner.run(["/usr/bin/git", "-C", source, "add", "--", f"Formula/{FORMULA}.rb"], check=True)
    runner.run(["/usr/bin/git", "-C", source, "commit", "-m", message], check=True)


def _init_tap_source(runner: CommandRunner, source: Path, old_formula: Path) -> None:
    source.mkdir(parents=True)
    runner.run(["/usr/bin/git", "init", source], check=True)
    runner.run(["/usr/bin/git", "-C", source, "config", "user.name", "AgentBC RC"], check=True)
    runner.run(["/usr/bin/git", "-C", source, "config", "user.email", "rc@agentbc.invalid"], check=True)
    _write_formula(source, old_formula)
    _git_commit_formula(runner, source, "test: add old AgentBC formula")


def _brew_cli(runner: CommandRunner, brew: Path) -> Path:
    prefix = runner.run([brew, "--prefix", FORMULA], check=True).stdout.strip()
    path = Path(prefix) / "bin" / FORMULA
    if not path.is_file():
        raise RcError("Homebrew AgentBC CLI is missing from the Cellar prefix")
    return path


def _assert_version(runner: CommandRunner, cli: Path, version: str) -> None:
    result = runner.run([cli, "--version"], check=True)
    if version not in result.stdout:
        raise RcError(f"Homebrew CLI did not report expected version {version}")


def _assert_brew_update_guidance(runner: CommandRunner, cli: Path) -> None:
    result = runner.run([cli, "update"])
    output = result.stdout + result.stderr
    if "brew upgrade agentbc" not in output:
        raise RcError("Homebrew-owned agentbc update did not return brew upgrade guidance")


def _assert_path_order(preexisting_cli: str | None, brew_cli: Path, original_path: str) -> None:
    brew_dir = str(brew_cli.parent)
    without_brew = os.pathsep.join(
        part for part in original_path.split(os.pathsep) if part and part != brew_dir
    )
    brew_first = os.pathsep.join((brew_dir, without_brew))
    if shutil.which(FORMULA, path=brew_first) != str(brew_cli):
        raise RcError("Homebrew-first PATH did not resolve the Cellar-owned CLI")
    if preexisting_cli and Path(preexisting_cli) != brew_cli:
        local_dir = str(Path(preexisting_cli).parent)
        local_first = os.pathsep.join((local_dir, brew_dir, without_brew))
        if shutil.which(FORMULA, path=local_first) != preexisting_cli:
            raise RcError("Local-alpha-first PATH did not preserve the existing CLI")


def _write_evidence(path: Path | None, evidence: dict[str, Any]) -> None:
    document = json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    if path is None:
        print(document, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    print(f"evidence: {path}")


def run(args: argparse.Namespace) -> int:
    old_formula = args.old_formula.expanduser().resolve()
    new_formula = args.new_formula.expanduser().resolve()
    brew = args.brew.expanduser().resolve()
    ca_cert = args.ca_cert.expanduser().resolve() if args.ca_cert is not None else None
    server_cert = (
        args.server_cert.expanduser().resolve()
        if args.server_cert is not None
        else None
    )
    home = Path.home()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "task": "PKG-103-001",
        "old_formula": {
            "version": formula_version(old_formula),
            "sha256": hashlib.sha256(old_formula.read_bytes()).hexdigest(),
        },
        "new_formula": {
            "version": formula_version(new_formula),
            "sha256": hashlib.sha256(new_formula.read_bytes()).hexdigest(),
        },
        "service": args.service,
        "plan": command_plan(args.tap, service=args.service),
    }

    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as temporary:
        root = Path(temporary)
        ca_bundle = None
        curlrc = None
        if ca_cert is not None:
            ca_bundle = create_combined_ca_bundle(ca_cert, root / "combined-ca.pem")
            curlrc = create_curlrc(ca_bundle, root / "curlrc")
        runner = CommandRunner(brew_environment(ca_bundle=ca_bundle, curlrc=curlrc))
        preflight = _preflight(
            runner,
            brew=brew,
            old_formula=old_formula,
            new_formula=new_formula,
            feed_url=args.feed_url,
            ca_cert=ca_cert,
            server_cert=server_cert,
            service=args.service,
            min_free_gib=args.min_free_gib,
        )
        evidence["preflight"] = preflight
        evidence["commands"] = runner.commands
        if not preflight["ok"]:
            evidence["state"] = "environment_blocked"
            _write_evidence(args.evidence, evidence)
            return 2
        if not args.execute:
            evidence["state"] = "preflight_ready"
            _write_evidence(args.evidence, evidence)
            return 0
        if os.environ.get(GATE_ENV) != "1":
            raise RcError(f"real Homebrew RC requires {GATE_ENV}=1")

        preserved = default_preserve_paths(home) + list(args.preserve_path)
        record_root = home / "Documents" / "AgentBC" / "workspace" / "record"
        before = {
            "brew": snapshot_brew(runner, brew),
            "data": snapshot_preserved(preserved, record_root),
            "path": os.environ.get("PATH", ""),
        }
        tap_source = root / "tap-source"
        _init_tap_source(runner, tap_source, old_formula)
        formula_ref = f"{args.tap}/{FORMULA}"
        trust_state = before["brew"].get("trust")
        trusted_formulae = (
            trust_state.get("formulae", []) if isinstance(trust_state, dict) else []
        )
        trust_was_present = formula_ref in trusted_formulae
        cleanup_needed = False
        failure = ""
        try:
            runner.run([brew, "tap", args.tap, tap_source.as_uri()], check=True)
            cleanup_needed = True
            if not trust_was_present:
                runner.run([brew, "trust", "--formula", formula_ref], check=True)
            runner.run([brew, "install", formula_ref], check=True, timeout=900)
            runner.run([brew, "test", formula_ref], check=True)
            old_cli = _brew_cli(runner, brew)
            _assert_version(runner, old_cli, evidence["old_formula"]["version"])
            runner.run([old_cli, "--help"], check=True)
            _assert_path_order(shutil.which(FORMULA), old_cli, before["path"])
            old_python = old_cli.parent.parent / "libexec" / "bin" / "python"
            if not old_python.is_file():
                raise RcError("Homebrew AgentBC virtualenv Python is missing")
            _probe_feed(runner, args.feed_url, old_python)

            _write_formula(tap_source, new_formula)
            _git_commit_formula(runner, tap_source, "test: update AgentBC formula")
            tap_checkout = runner.run([brew, "--repository", args.tap], check=True).stdout.strip()
            runner.run(["/usr/bin/git", "-C", tap_checkout, "pull", "--ff-only"], check=True)
            runner.run([brew, "upgrade", formula_ref], check=True, timeout=900)
            new_cli = _brew_cli(runner, brew)
            _assert_version(runner, new_cli, evidence["new_formula"]["version"])
            _assert_brew_update_guidance(runner, new_cli)
            _assert_path_order(shutil.which(FORMULA), new_cli, before["path"])
            new_python = new_cli.parent.parent / "libexec" / "bin" / "python"
            if not new_python.is_file():
                raise RcError("Upgraded Homebrew AgentBC virtualenv Python is missing")
            _probe_feed(runner, args.feed_url, new_python)

            if args.service:
                runner.run([brew, "services", "start", FORMULA], check=True)
                doctor = runner.run([new_cli, "doctor", "--json"], check=True)
                report = json.loads(doctor.stdout)
                runner_identity = report.get("runner", {}) if isinstance(report, dict) else {}
                if runner_identity.get("identity") != "match":
                    raise RcError("Homebrew service Runner identity did not match")
                if "Cellar" not in str(runner_identity.get("module_path") or ""):
                    raise RcError("Homebrew service Runner did not execute from Cellar")
                runner.run([brew, "services", "stop", FORMULA], check=True)

            runner.run([brew, "uninstall", "--force", FORMULA], check=True)
            if not trust_was_present:
                runner.run([brew, "untrust", "--formula", formula_ref], check=True)
            runner.run([brew, "untap", args.tap], check=True)
            cleanup_needed = False
        except (OSError, ValueError, RcError, subprocess.SubprocessError) as exc:
            failure = str(exc)
        finally:
            if cleanup_needed:
                try:
                    if args.service:
                        runner.run([brew, "services", "stop", FORMULA])
                    runner.run([brew, "uninstall", "--force", FORMULA])
                    if not trust_was_present:
                        runner.run([brew, "untrust", "--formula", formula_ref])
                    runner.run([brew, "untap", args.tap])
                except (OSError, subprocess.SubprocessError) as exc:
                    failure = failure or f"cleanup failed: {exc}"

        after = {
            "brew": snapshot_brew(runner, brew),
            "data": snapshot_preserved(preserved, record_root),
            "path": os.environ.get("PATH", ""),
        }
        evidence["before"] = before
        evidence["after"] = after
        evidence["commands"] = runner.commands
        if failure:
            evidence["state"] = "failed"
            evidence["failure"] = failure
            _write_evidence(args.evidence, evidence)
            return 1
        evidence["state"] = "accepted" if before == after else "state_drift"
        _write_evidence(args.evidence, evidence)
        return 0 if before == after else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded AgentBC Homebrew RC E2E driver")
    parser.add_argument("--old-formula", type=Path, required=True)
    parser.add_argument("--new-formula", type=Path, required=True)
    parser.add_argument("--feed-url", required=True)
    parser.add_argument("--ca-cert", type=Path)
    parser.add_argument("--server-cert", type=Path)
    parser.add_argument(
        "--brew",
        type=Path,
        default=Path(shutil.which("brew") or "/opt/homebrew/bin/brew"),
    )
    parser.add_argument("--tap", default=DEFAULT_TAP)
    parser.add_argument("--service", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--preserve-path", type=Path, action="append", default=[])
    parser.add_argument("--min-free-gib", type=int, default=MIN_FREE_GIB)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except (OSError, ValueError, RcError, subprocess.SubprocessError) as exc:
        print(f"homebrew_rc_error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
