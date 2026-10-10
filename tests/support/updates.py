"""Shared immutable update fixture used by release-contract tests."""


def available_release() -> dict:
    return {
        "state": "update_available",
        "code": "",
        "channel": "alpha",
        "current": "1.0.3a1",
        "latest": "1.0.4a1",
        "update_available": True,
        "source": "github_release_manifest",
        "release_url": "https://example.test/release",
        "summary": "fixes",
        "wheel": {
            "filename": "agentbc-1.0.4a1-py3-none-any.whl",
            "sha256": "a" * 64,
            "url": "https://example.test/wheel",
        },
    }
