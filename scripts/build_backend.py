"""PEP 517 backend: generate WebUI once per artifact, ship self-contained wheels."""

from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from setuptools import build_meta as backend

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    target = ROOT / "src/agent_bridge_connect/assets/webui"
    if (ROOT / "PKG-INFO").is_file():
        # Published sdists carry the frontend built by the release pipeline.
        # Installing them (including Homebrew) must not need Node or npm access.
        if not (target / "index.html").is_file() or not list(
            (target / "assets").glob("*.js")
        ):
            raise RuntimeError("Source distribution is missing packaged WebUI assets")
        return
    frontend = ROOT / "webui"
    subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=frontend, check=True)
    subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)
    assets = frontend / "dist"
    if not (assets / "index.html").is_file() or not list(
        (assets / "assets").glob("*.js")
    ):
        raise RuntimeError("WebUI build did not produce complete assets")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(assets, target)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    prepare()
    return backend.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    prepare()
    name = backend.build_sdist(sdist_directory, config_settings)
    path = Path(sdist_directory) / name
    with tempfile.TemporaryDirectory() as directory:
        replacement = Path(directory) / name
        with (
            tarfile.open(path) as original,
            tarfile.open(replacement, "w:gz") as output,
        ):
            members = original.getmembers()
            prefix = members[0].name.split("/")[0]
            included = {m.name for m in members}
            for member in members:
                output.addfile(
                    member, original.extractfile(member) if member.isfile() else None
                )
            for relative in (
                "scripts/build_backend.py",
                "webui/package.json",
                "webui/package-lock.json",
                "webui/tsconfig.json",
                "webui/index.html",
                "webui/vite.config.ts",
            ):
                source = ROOT / relative
                if source.is_file() and f"{prefix}/{relative}" not in included:
                    output.add(source, arcname=f"{prefix}/{relative}")
            output.add(ROOT / "webui/src", arcname=f"{prefix}/webui/src")
        shutil.copyfile(replacement, path)
    return name


get_requires_for_build_wheel = backend.get_requires_for_build_wheel
get_requires_for_build_sdist = backend.get_requires_for_build_sdist
prepare_metadata_for_build_wheel = backend.prepare_metadata_for_build_wheel
build_editable = backend.build_editable
get_requires_for_build_editable = backend.get_requires_for_build_editable
prepare_metadata_for_build_editable = backend.prepare_metadata_for_build_editable
