"""Local worker inventory; does not launch or send on import."""
import json,time,msvcrt
from pathlib import Path
BASE=Path(__file__).resolve().parent
def directory(group):return BASE/'group-workers'/str(int(group))
def locked(path):
    if not path.exists():return False
    with path.open('r+b') as handle:
        try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:return True
        handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1);return False
def any_active():return locked(BASE/'runner.lock') or any(locked(p) for p in (BASE/'group-workers').glob('*/runner.lock'))
def statuses():
    result=[]
    for path in (BASE/'group-workers').glob('*/status.json'):
        try:
            value=json.loads(path.read_text(encoding='utf-8'));value['fresh']=time.time()-path.stat().st_mtime<90 and locked(path.parent/'runner.lock');result.append(value)
        except (ValueError,OSError):continue
    return sorted(result,key=lambda row:row.get('group',0))
