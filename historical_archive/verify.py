"""Read-only verification of the retired source snapshot; never runs its code."""
import hashlib
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent
for manifest in sorted(root.glob('*source-manifest.json')):
    archive = root / ('legacy-source-20260907.tar.gz' if manifest.name == 'legacy-source-manifest.json' else 'extra-source-20260907.tar.gz')
    records = json.loads(manifest.read_text())
    with tarfile.open(archive) as bundle:
        assert set(bundle.getnames()) == {item['path'] for item in records}
        for item in records:
            data = bundle.extractfile(item['path']).read()
            assert len(data) == item['bytes']
            assert hashlib.sha256(data).hexdigest() == item['sha256'], item['path']
    print(archive.name, len(records), hashlib.sha256(archive.read_bytes()).hexdigest())
