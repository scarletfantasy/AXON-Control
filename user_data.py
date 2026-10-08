"""User-owned state is independent of the executable location."""
import json
import os
from pathlib import Path
import shutil


def prepare_data(legacy, destination=None):
    target = Path(destination or os.environ.get('AXON_CONTROL_DATA_DIR') or
                  Path(os.environ['LOCALAPPDATA']) / 'AXONControl')
    target.mkdir(parents=True, exist_ok=True)
    legacy = Path(legacy)
    # Local delivery can point at the previous installation without copying or
    # freezing a session while that older version is still running.
    link = legacy / 'legacy-data-locations.json'
    if link.exists():
        locations = json.loads(link.read_text(encoding='utf-8'))
        sources = [Path(p) for p in locations if isinstance(p, str) and Path(p).is_dir()]
        sources.sort(key=lambda p: max((q.stat().st_mtime for q in p.glob('*session.json')), default=0), reverse=True)
        for source in sources:
            if source.resolve() != legacy.resolve() and not (source/'legacy-data-locations.json').exists():
                prepare_data(source, target)
    if legacy.resolve() != target.resolve():
        for name in ('last-session.json', 'recovery-session.json', 'desktop-settings.json',
                     'preset-library.json', 'backups', 'drafts'):
            source = legacy / name
            if source.is_dir():
                for path in source.rglob('*.json'):
                    out = target / name / path.relative_to(source)
                    if not out.exists():
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, out)
            elif source.is_file() and not (target / name).exists():
                shutil.copy2(source, target / name)
    return target


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
