# Windows installer ACL fix — 2026-10-08

Root cause reproduced in the original candidate: docker_install_bundle.install created root/bundle with mkdir(mode=0700), then private_json required a protected current-user-SID-only DACL. The Windows-specific ACL initializer had not run for this new directory. Replaced mkdir with ensure_private_directory; no validation weakened.

Superseding candidate on Windows:
D:\agentbc-tests\win-formal-20261008-aclfix\output\agentbc-1.0.4a1-docker-windows-amd64.zip

48 installation tests passed. Fresh candidate real Windows transaction passed bundle checks, image loading/identity, managed venv creation and offline wheel installation. A deliberate DIAGNOSTIC_SETUP_BOUNDARY exception stopped before interactive setup and triggered normal rollback. Post-check: receipt=None, managed root absent, historical workspace preserved. No credentials imported or business services started. Full interactive installation remains pending.

Also includes the preview backup fix excluding the currently held preview-setup.lock from copy/hash on Windows. Preview uninstall was completed with 14 historical tasks preserved in workspace-preview-backup-20261008-201519.
