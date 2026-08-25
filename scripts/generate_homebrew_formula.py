#!/usr/bin/env python3
"""Generate the pinned AgentBC Homebrew formula from a release manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def generate_formula(manifest: dict, base_url: str) -> str:
    version = str(manifest.get("package_version") or "")
    tag = str(manifest.get("tag") or "")
    tag_match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)A", tag)
    if tag_match is None or version != ".".join(tag_match.groups()) + "a1":
        raise ValueError("manifest version/tag is not an AgentBC Alpha release")
    if not base_url.startswith("https://"):
        raise ValueError("formula release URL must use https")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest artifacts must be a list")
    sdists = [
        item
        for item in artifacts
        if isinstance(item, dict) and str(item.get("filename") or "").endswith(".tar.gz")
    ]
    if len(sdists) != 1:
        raise ValueError("manifest must declare exactly one sdist")
    sdist = sdists[0]
    filename = str(sdist.get("filename") or "")
    sha256 = str(sdist.get("sha256") or "")
    if not re.fullmatch(r"agentbc-[A-Za-z0-9_.-]+\.tar\.gz", filename):
        raise ValueError("sdist filename is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("sdist sha256 is invalid")
    url = f"{base_url.rstrip('/')}/{filename}"
    return f'''class Agentbc < Formula
  include Language::Python::Virtualenv

  desc "Local-first task control plane for Codex, Claude Code, and Hermes"
  homepage "https://github.com/roway49/agent-bridge-connect"
  url "{url}"
  version "{version}"
  sha256 "{sha256}"
  license "MIT"

  depends_on "python"

  def install
    virtualenv_install_with_resources
  end

  service do
    run [opt_bin/"agentbc", "runner", "serve"]
    keep_alive true
    log_path var/"log/agentbc-runner.log"
    error_log_path var/"log/agentbc-runner.log"
  end

  def caveats
    <<~EOS
      Run `agentbc setup` after install or upgrade to discover executors and
      install the version-matched Codex, Claude, and Hermes skills.
      Start the managed Runner with `brew services start agentbc`.
    EOS
  end

  test do
    assert_match "agentbc {version}", shell_output("#{{bin}}/agentbc --version")
    system bin/"agentbc", "--help"
  end
end
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    base_url = args.base_url or (
        "https://github.com/roway49/agent-bridge-connect/releases/download/"
        + str(manifest.get("tag") or "")
    )
    formula = generate_formula(manifest, base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(formula, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
