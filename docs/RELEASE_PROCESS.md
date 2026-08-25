# Release Process

[中文](RELEASE_PROCESS_ZH.md) | English

This checklist prepares and publishes AgentBC without allowing a local smoke
test, a mutable branch, or an existing artifact name to stand in for a release.
The current internal candidate is **1.0.3A** (Python package `1.0.3a1`), while
the published release procedure below remains the immutable 1.0.2A record.
For `1.0.2A`, the fixed mapping is:

```text
Product tag:    v1.0.2A
Python package: 1.0.2a1
```

## 1. Freeze The Release Commit

- Finish the changelog entry and replace `Unreleased` with the publication date.
- Require a clean, attached release branch and review every file in the release diff.
- Confirm `pyproject.toml` and `agent_bridge_connect.__version__` both contain
  `1.0.2a1`.
- Confirm the public remote has no existing `v1.0.2A` tag and PyPI has no
  existing `agentbc==1.0.2a1` file. Published tags and package files are immutable.

## 2. Run The Release Matrix

The release-check workflow is authoritative for Python 3.10, 3.11, and 3.14.
Each job runs source tests, Ruff, compileall, shell syntax, package build, Twine,
distribution-name validation, manifest generation, wheel installation, and the
package-only smoke test.

Before tagging, also complete the release-specific manual gates recorded in the
1.0.2A checklist: MacBook synchronization, retain-on session behavior, clean
install/rollback, and real-executor checks. A green Runner or one smoke test does
not replace the other gates.

## 3. Build A Local Candidate

Use an isolated output directory and a clean checkout. Generate build identity
before building so the wheel and sdist name their exact source commit:

```bash
python3 scripts/build_provenance.py print-package-version
python3 scripts/build_provenance.py print-product-version
python3 scripts/build_provenance.py generate-build-info --build-source release-candidate
python3 -m build
python3 -m twine check dist/*.whl dist/*.tar.gz
python3 scripts/build_provenance.py validate-dists
python3 scripts/build_provenance.py generate-manifest
```

Verify every SHA-256 in `dist/release-manifest.json`. Install the wheel in a
fresh virtual environment and run `agentbc --version` plus the package-only
smoke test. Candidate artifacts are disposable and must not be uploaded as the
final release if the source commit changes.

Build the macOS local-alpha bundle from the same commit as a separate candidate:

```bash
./scripts/build_local_alpha_bundle.sh /tmp/agentbc-v1.0.2A-release
shasum -a 256 -c /tmp/agentbc-v1.0.2A-release/agentbc-v1.0.2A-macos-local-alpha.tar.gz.sha256
```

Extract it once and run `shasum -a 256 -c SHA256SUMS` inside the bundle. The
archive, archive checksum, `install-agentbc-alpha.sh`, and
`uninstall-agentbc-alpha.sh` are required GitHub Release assets; the PyPI
workflow does not build these macOS assets.

## 4. Tag And Publish

After every gate passes, create the immutable annotated tag on the reviewed
release commit and validate the tag/version/commit relationship:

```bash
git tag -a v1.0.2A -m "AgentBC 1.0.2A"
python3 scripts/build_provenance.py validate --tag v1.0.2A
git push public <release-branch>
git push public refs/tags/v1.0.2A
```

Create a **draft** GitHub Release from `v1.0.2A` using the matching changelog
section. Upload and verify these four macOS assets before publication:

```text
agentbc-v1.0.2A-macos-local-alpha.tar.gz
agentbc-v1.0.2A-macos-local-alpha.tar.gz.sha256
install-agentbc-alpha.sh
uninstall-agentbc-alpha.sh
```

Only then publish the draft. Publishing triggers
`.github/workflows/publish-pypi.yml`, which rebuilds from the tag, validates
provenance, uploads wheel/sdist/manifest to the GitHub Release, and publishes
only wheel/sdist to PyPI through Trusted Publishing. Do not upload Python
distributions from a developer credential or from an untagged tree.

## 5. Verify And Recover

After publication:

- compare GitHub Release and PyPI artifact hashes with the generated manifest;
- install `agentbc==1.0.2a1` in a clean environment;
- verify `agentbc --version`, `agentbc setup --show`, Runner identity, Skill
  manifests, and `agentbc doctor`;
- verify both Apple Silicon and Intel installation paths before announcing the release.

If the publication job fails after the tag exists, fix the workflow rather than
moving or recreating the tag. The guarded recovery path is a manual workflow
dispatch with `release_tag=v1.0.2A` and `publish=true`; it checks out and validates
the existing tag before rebuilding. Never overwrite an already published PyPI
file with the same version.
