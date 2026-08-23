# AgentBC Homebrew packaging

The release workflow generates `Formula/agentbc.rb` from the same
`release-manifest.json` used by `agentbc update`:

```bash
python3 scripts/generate_homebrew_formula.py \
  dist/release-manifest.json \
  --output packaging/homebrew/Formula/agentbc.rb
```

The formula pins the release sdist SHA-256, installs into Homebrew's isolated
Python virtualenv, exposes the Runner as a service, and never owns AgentBC
configuration, records, reports, artifacts, customer projects, or executor
skills. `agentbc setup` remains the explicit skill/config refresh step.
It depends on Homebrew's unversioned `python` Formula; AgentBC's minimum Python
version remains defined only by `requires-python` in `pyproject.toml`, so the
Homebrew package does not create a conflicting interpreter-version contract.

`agentbc update` never replaces a Homebrew Cellar link. Homebrew installations
upgrade only through `brew upgrade agentbc`; this keeps Homebrew's receipt and
service ownership authoritative.

Release acceptance must run `brew install`, `brew upgrade`, all-version
`brew uninstall --force agentbc`, `brew services start/stop`,
PyPI/local-bundle migration, and PATH-conflict checks on both Apple Silicon
and Intel macOS before publishing the tap update.

Use `scripts/run_homebrew_rc_e2e.py` for the destructive RC phase. Its default
mode is read-only preflight; a real run additionally requires `--execute` and
`AGENTBC_HOMEBREW_RC_RUN=1`. The driver fails before tapping or installing when
Homebrew Doctor, Xcode/CLT, architecture, disk space, preinstalled Formula
dependencies, HTTPS CA/server-certificate extensions, existing Cellar
ownership, or Runner/service isolation do not meet the gate. A private RC feed
must supply the CA and server certificate together: the CA requires SKI/AKI,
while the server certificate requires SAN/SKI/AKI. Neither certificate is
written to the system keychain.

Every Homebrew command is executed with automatic update, install cleanup, and
autoremove disabled. The driver snapshots Formula versions, taps, services,
Formula-level trust, PATH, AgentBC configuration, all three Skill roots,
workspace data, and stable RunLease identity. Homebrew 6 receives temporary
trust for the exact RC Formula only; whole-tap trust and trust-check bypasses
are forbidden, and the original trust state must be restored. Heartbeat
timestamps may advance while an existing Runner stays online, but
task/run/owner/process identity must not drift.

Example preflight:

```bash
python3 scripts/run_homebrew_rc_e2e.py \
  --old-formula /tmp/agentbc-1.0.2a1.rb \
  --new-formula /tmp/agentbc-1.0.3a1.rb \
  --feed-url https://mac-mini.local:8443/v1.0.3A/ \
  --ca-cert /tmp/agentbc-rc-ca.pem \
  --server-cert /tmp/agentbc-rc-server.pem \
  --evidence /tmp/agentbc-homebrew-preflight.json
```

Intel service execution is allowed only after the host preflight passes and
the existing non-Homebrew Runner has been stopped explicitly. The driver never
updates Xcode/CLT, writes the system keychain, runs `brew autoremove`, or
approximates restoration by installing a newer dependency version.
