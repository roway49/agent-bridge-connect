"""Fetch a hash-pinned public runtime asset during image build; no credentials."""
import hashlib
import json
from pathlib import Path
import sys
import shutil
import tarfile
import tempfile
import urllib.request

lock = json.loads(Path(sys.argv[1]).read_text())
asset = lock['assets'][sys.argv[2]]['linux/' + sys.argv[3]]
destination = Path(sys.argv[4])
destination.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as folder:
    archive = Path(folder) / 'runtime.tar.gz'
    cached = Path('/cache') / asset['url'].rsplit('/', 1)[-1]
    if cached.is_file():
        shutil.copyfile(cached, archive)
    else:
        with urllib.request.urlopen(asset['url'], timeout=120) as response, archive.open('wb') as output:
            while block := response.read(1024 * 1024):
                output.write(block)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != asset['sha256']:
        raise SystemExit('Runtime asset SHA256 mismatch')
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter='data')
for path in destination.rglob(sys.argv[2]):
    if path.is_file():
        path.chmod(path.stat().st_mode | 0o555)
print('Verified', sys.argv[2], 'linux/' + sys.argv[3])
