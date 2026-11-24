"""Delete bare clones of mined repos that yielded no instance (component < 5)."""
import json, shutil, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
MIN_K = int(sys.argv[1]) if len(sys.argv) > 1 else 5
# optional 2nd arg: candidates file -> only touch clones of those repos
ONLY = ({json.loads(l)["full_name"].replace("/", "__") for l in open(sys.argv[2])}
        if len(sys.argv) > 2 else None)
freed = 0
for f in (ROOT / "data" / "mined").glob("*.json"):
    if ONLY is not None and f.stem not in ONLY:
        continue
    clone = ROOT / "data" / "repos" / f.stem
    if not clone.exists():
        continue
    r = json.loads(f.read_text())
    keep = any(g["largest_conflict_component"] >= MIN_K for g in r.get("groups", []))
    if not keep:
        shutil.rmtree(clone, ignore_errors=True)
        freed += 1
print(f"removed {freed} clones")
