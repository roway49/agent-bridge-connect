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

`agentbc update` never replaces a Homebrew Cellar link. Homebrew installations
upgrade only through `brew upgrade agentbc`; this keeps Homebrew's receipt and
service ownership authoritative.

Release acceptance must run `brew install`, `brew upgrade`, `brew uninstall`,
`brew services start/stop`, PyPI/local-bundle migration, and PATH-conflict
checks on both Apple Silicon and Intel macOS before publishing the tap update.
