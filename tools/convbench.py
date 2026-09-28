"""Converter benchmark: the live /api/convert path (engine=trueform, feature=true) run in-process from this
checkout, one part at a time, with a timer around every stage. No service is touched.

usage: convbench.py <out_dir> <stl>...        (one JSON line per part on stdout; the STEPs kept in out_dir)
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.update({"MESH2STEP_NATIVE": os.path.expanduser("~/.local/share/mesh2step-native-v1.8.2-fcaee701f2db/run.sh"),
                   "MESH2STEP_ENGINE_FALLBACK": "1", "MESH2STEP_FEATURE": "1",
                   "MESH2STEP_EDGEBUILD_TIMEOUT_S": "900", "MESH2STEP_SLOTS": "1"})
os.environ.pop("MESH2STEP_RECON", None)                # the paid AI rebuild never runs here
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
# the feature pass runs as "python -m mesh2step.feature": without this its subprocess imports the INSTALLED
# (production) package, not this checkout (s2 measured production's serial builders on parts 12, 23, 39)
os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT), os.environ.get("PYTHONPATH", "")])
os.chdir(ROOT)

import mesh2step                                        # noqa: E402
assert str(ROOT) in mesh2step.__file__, mesh2step.__file__   # this checkout, never the live one
import webapp.server as S                               # noqa: E402
from fastapi.testclient import TestClient               # noqa: E402

CALLS = []


def timed(name, fn):
    def w(*a, **k):
        t0 = time.time()
        print(f"MARK {name} start {t0:.2f}", flush=True)
        try:
            return fn(*a, **k)
        finally:
            t1 = time.time()
            print(f"MARK {name} end {t1:.2f}", flush=True)
            CALLS.append((name, round(t1 - t0, 1)))
    return w


for n in ("convert_native", "_feature_upgrade", "_edgebuild_upgrade", "_retry_broken_trueform",
          "_edgebuild_build", "_edgebuild_apply"):     # the last two exist only where edgebuild runs beside the engine
    if hasattr(S, n):
        setattr(S, n, timed(n, getattr(S, n)))

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
c = TestClient(S.app)
for stl in sys.argv[2:]:
    CALLS.clear()
    t0 = time.time()
    with open(stl, "rb") as fh:
        d = c.post("/api/convert", data={"engine": "trueform", "feature": "true"},
                   files={"file": (Path(stl).name, fh, "application/octet-stream")}).json()
    while d.get("pending"):
        time.sleep(2)
        d = c.get(f"/api/job/{d['job']}").json()
    rec = {"part": Path(stl).stem, "seconds": round(time.time() - t0, 1), "stages": list(CALLS),
           "ok": bool(d.get("download_token"))}
    st = d.get("stats", {})
    rec.update({k: st.get(k) for k in ("backend", "faces", "surface_types", "engine_seconds", "tris", "cylinders")
                if k in st})
    if d.get("download_token"):
        (out / f"{Path(stl).stem}.step").write_bytes(c.get(f"/api/download/{d['download_token']}").content)
    else:
        rec["detail"] = str(d)[:300]
    print(json.dumps(rec), flush=True)
