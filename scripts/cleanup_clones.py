"""Delete bare clones of mined repos that yielded no instance (component < 5)."""
import json, shutil, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
MIN_K = int(sys.argv[1]) if len(sys.argv) > 1 else 5
freed = 0
for f in (ROOT / "data" / "mined").glob("*.json"):
    clone = ROOT / "data" / "repos" / f.stem
    if not clone.exists():
        continue
    r = json.loads(f.read_text())
    keep = any(g["largest_conflict_component"] >= MIN_K for g in r.get("groups", []))
    if not keep:
        shutil.rmtree(clone, ignore_errors=True)
        freed += 1
print(f"removed {freed} clones")
