"""Adaptive probing around edgebuild.py: same command line, same RESULT / FAIL output, so the webapp calls it in place.

The default build runs first. If it misses the webapp's acceptance check (one valid closed solid, no free edges, volume
within 1 %, p95 surface distance within 0.005 of the diagonal), variants picked by the failure class run in parallel
and the first that passes is served. Tightening / construction variants go first; loosening rungs (corner residual,
sewing tolerance) only after them (user decision 2026-09-15). The acceptance check itself never changes. Every probe
appends one JSON line to EB_PROBE_LOG so the variants that keep winning can become the defaults.
"""
import ast
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

EB = Path(__file__).with_name("edgebuild.py")
MAX_DV_PCT, MAX_P95_REL = 1.0, 0.005          # the webapp gate (server.py EDGE_MAX_DV_PCT / EDGE_MAX_P95_REL)
JOBS = int(os.environ.get("EB_PROBE_JOBS", "4"))
BUDGET = float(os.environ.get("EB_PROBE_BUDGET", "840"))
LOG = Path(os.environ.get("EB_PROBE_LOG", str(Path.home() / ".local/share/mesh2step/probe_log.jsonl")))
LOOSENING_KEYS = {"EB_CORNER_TOL", "EB_SEW_TOL", "EB_ABSORB_AREA"}
# raced beside the default build from the start (owner, 2026-09-29: four cores on the rebuild): the three tightening
# variants that won most often in the probe log (unlabelled 23, corners 15 and 10 wins of 105). The default still wins
# whenever it passes; a raced variant is only used where the default's own failure ladder would have run it.
PRE_RACE = [{"EB_SPLIT_UNLABELLED": "1"},
            {"EB_REFIT_GUARD": "1", "EB_CURVED_UNLABEL": "1", "EB_SPLIT_MIXED": "1", "EB_ISO_SNAP": "1",
             "EB_LEFTOVER_AXIS": "1"},
            {"EB_REFIT_GUARD": "1", "EB_CURVED_UNLABEL": "1", "EB_SPLIT_MIXED": "1", "EB_ISO_SNAP": "1"}]
# a raced variant that passed waits for the default only this long: the default wins when it passes, and of 41 logged
# default passes the slowest took 96 s (probe log, 2026-09-29)
# ponytail: from 41 samples; raise it if the log ever shows a later default pass
DEFAULT_PASS_S = 120.0
LIVE = {}                  # candidate path -> running Popen, so a winner can stop the others
STOPPED = set()            # candidate paths killed on purpose: not a failure class, never a rung to build on
_lock = threading.Lock()


def jobs():
    """Builds at once: up to JOBS, fewer when the host is loaded (six conversions may probe together)."""
    # ponytail: 1-minute load average; a cgroup CPU-pressure reading if this ever oversubscribes
    return max(1, min(JOBS, int((os.cpu_count() or 1) - os.getloadavg()[0])))


def stop(path):
    with _lock:
        p = LIVE.get(str(path))
        STOPPED.add(str(path))
    if p is not None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass

# per failure class: (tightening / construction variants, loosening variants)
LADDERS = {
    "open": ([{"EB_WELD": "1e-5"}, {"EB_WELD": "1e-6"}], []),
    # mechparts/20: a cylinder merged with its tangent round is split back (SPLIT_MIXED), refits may not get worse
    # (REFIT_GUARD), and a round's side edge along one torus iso line is that exact circle (ISO_SNAP)
    "corners": ([{"EB_RIM_CYL": "1"},
                 {"EB_REFIT_GUARD": "1", "EB_CURVED_UNLABEL": "1", "EB_SPLIT_MIXED": "1", "EB_ISO_SNAP": "1"},
                 # mechparts/17: the same, plus leftover curved groups fitted about a known axis (a R 200.66 bore in 5
                 # fragments); a separate rung because on mechparts/20 it builds an invalid solid
                 {"EB_REFIT_GUARD": "1", "EB_CURVED_UNLABEL": "1", "EB_SPLIT_MIXED": "1", "EB_ISO_SNAP": "1",
                  "EB_LEFTOVER_AXIS": "1"},
                 {"EB_STRIP_SPLIT": "1"}, {"EB_CURVED_UNLABEL": "1"}, {"EB_RCOND": "1e-3"}, {"EB_CYL_CYL": "0"},
                 {"EB_CONE_PLANE": "1"}, {"EB_CYL_CYL": "0", "EB_RCOND": "1e-3"}],
                [{"EB_CORNER_TOL": "1e-5"}, {"EB_CORNER_TOL": "1e-4"}]),
    "chain": ([{"EB_CYL_CYL": "0"}, {"EB_RCOND": "1e-3"}, {"EB_NO_PLANE_SPLIT": "1"}], [{"EB_CORNER_TOL": "1e-5"}]),
    "unlabelled": ([{"EB_SPLIT_UNLABELLED": "1"}, {"EB_PLANE_RELABEL": "1"}, {"EB_NO_MIXED": "1"}, {"EB_UNION_MIXED": "1"},
                    {"EB_KINDS4": "1"}],
                   [{"EB_ABSORB_AREA": "0.05"}, {"EB_ABSORB_AREA": "0.05", "EB_CORNER_TOL": "1e-5"}]),
    "unsound": ([{"EB_NO_CYL_BAND": "1"}, {"EB_CYL_CYL": "0"}, {"EB_NO_PLANE_SPLIT": "1"}, {"EB_WELD": "1e-5"}],
                [{"EB_SEW_TOL": "5e-5"}, {"EB_SEW_TOL": "2e-4"}]),
}


def classify(rc, stdout):
    """(class, metrics or None) of one build's output."""
    res = next((ln for ln in reversed(stdout.splitlines()) if ln.startswith("RESULT ")), None)
    if res is not None:
        m = ast.literal_eval(res[7:].split(" radii ")[0])
        ok = (m["valid"] and m["solids"] == 1 and m["free_edges"] == 0 and abs(m["dv_pct"]) <= MAX_DV_PCT
              and m["dist_p95"] <= MAX_P95_REL * m["diag"])
        return ("pass" if ok else "unsound"), m
    fail = next((ln for ln in reversed(stdout.splitlines()) if ln.startswith("FAIL ")), "")
    if rc is None:
        return "timeout", None
    for key, cls in (("open or non-manifold", "open"), ("corners where", "corners"), ("do not meet there", "chain"),
                     ("lie on no fitted surface", "unlabelled"), ("free edges after sewing", "unsound")):
        if key in fail:
            return cls, None
    return "unsound", None


def build(src, out, env_extra, deadline):
    """(rc or None on timeout / stop, stdout, seconds) of one edgebuild run that must end by `deadline` (epoch s)."""
    t0 = time.time()
    timeout = deadline - t0
    if timeout < 1.0 or str(out) in STOPPED:
        return None, "", 0.0           # queued past the deadline, or stopped before it started
    p = subprocess.Popen([sys.executable, str(EB), str(src), str(out)], env=dict(os.environ, EB_FAST_FAIL="1", **env_extra),
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    with _lock:                    # registered and checked under stop()'s lock: a stop never misses a starting build
        LIVE[str(out)] = p         # (mechparts/20: a queued variant started as the winner stopped it, ran 5 min on)
        if str(out) in STOPPED:
            os.killpg(p.pid, signal.SIGKILL)
    try:
        stdout, _ = p.communicate(timeout=timeout)
        rc = None if str(out) in STOPPED else p.returncode
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        stdout, _ = p.communicate()
        rc = None
    finally:
        with _lock:
            LIVE.pop(str(out), None)
    return rc, stdout or "", time.time() - t0


def main():
    if "--combine" in sys.argv or os.environ.get("EB_COUNT_BODIES"):
        os.execv(sys.executable, [sys.executable, str(EB), *sys.argv[1:]])      # nothing to probe
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    deadline = time.time() + BUDGET
    cand = lambda i: out.with_name(f"{out.stem}.probe{i}{out.suffix}")  # noqa: E731
    key = lambda v: json.dumps(v, sort_keys=True)  # noqa: E731
    pool = ThreadPoolExecutor(max_workers=JOBS)
    n_pre = jobs() - 1
    pre = {key(v): (pool.submit(build, src, cand(i), v, deadline), i, v) for i, v in enumerate(PRE_RACE[:n_pre])}
    dfut, t_d, early = pool.submit(build, src, out, {}, deadline), time.time(), None
    while not dfut.done():
        time.sleep(0.5)
        if early is None:
            early = next(((i, v, f_) for f_, i, v in pre.values() if f_.done() and not f_.cancelled()
                          and classify(*f_.result()[:2])[0] == "pass"), None)
        if early is not None and time.time() - t_d > DEFAULT_PASS_S:
            stop(out)                                  # a default still running now fails (none passed past 96 s)
            break
    rc, stdout, sec = dfut.result()
    if early is not None and str(out) in STOPPED:
        i, v, f_ = early
        rc_v, out_v, sec_v = f_.result()
        record = {"mesh": str(src), "body": os.environ.get("EB_BODY"), "default": "stopped",
                  "default_sec": round(sec, 1), "raced": n_pre,
                  "tried": [{"env": v, "class": "pass", "sec": round(sec_v, 1), "metrics": None, "i": i}], "winner": None}
        _finish(pool, record, [cand(j) for _f, j, _v in pre.values()], cand(i), out)
        print(f"PROBE WINNER {json.dumps(v)} raced, default stopped after {sec:.0f} s", flush=True)
        sys.stdout.write(out_v)
        sys.exit(rc_v)
    cls, m = classify(rc, stdout)
    # default_sec: how long the default build took, pass or fail (2026-09-28: 1482 default wins had no time, so no
    # early give-up could be set from data)
    record = {"mesh": str(src), "body": os.environ.get("EB_BODY"), "default": cls, "default_sec": round(sec, 1),
              "raced": n_pre, "tried": [], "winner": None}
    ladder_keys = {key(v) for v in LADDERS.get(cls, ([], []))[0]}
    for k, (_f, i, _v) in pre.items():
        if cls == "pass" or k not in ladder_keys:
            stop(cand(i))                              # not a rung this failure would run: free its core
    if cls == "pass" or cls not in LADDERS:
        record["winner"] = {} if cls == "pass" else None
        _finish(pool, record, [cand(i) for _f, i, _v in pre.values()], None, out)
        sys.stdout.write(stdout)
        sys.exit(rc if rc is not None else 3)
    winner = None
    tried_envs = set()
    next_i = len(PRE_RACE)
    # (class to fix, settings kept from an earlier rung): a rung that turns one failure into ANOTHER class has fixed
    # its own problem (mechparts/31: absorbing leftovers turns "unlabelled" into "corners"), so its settings stay on
    # and the new class's ladder runs on top of them, up to EB_PROBE_DEPTH levels
    frontier = [(cls, {})]
    for _depth in range(int(os.environ.get("EB_PROBE_DEPTH", "2"))):
        nxt = []
        for cls_f, base in frontier:
            tight, loose = LADDERS.get(cls_f, ([], []))
            for stage in (tight, loose):
                stage = [dict(base, **v) for v in stage]
                stage = [v for v in stage if key(v) not in tried_envs]
                if winner or not stage or time.time() >= deadline:
                    continue
                tried_envs.update(key(v) for v in stage)
                futs = {}
                for v in stage:
                    if key(v) in pre:                  # raced since the start: its result, or its run, is reused
                        f_, i, _v = pre.pop(key(v))
                    else:
                        i = next_i; next_i += 1
                        f_ = pool.submit(build, src, cand(i), v, deadline)
                    futs[f_] = (i, v)
                for fut in as_completed(futs):
                    i, v = futs[fut]
                    if fut.cancelled() or str(cand(i)) in STOPPED:
                        continue                       # queued or running behind a winner: stopped, not failed
                    rc_v, out_v, sec_v = fut.result(); cls_v, m_v = classify(rc_v, out_v)
                    short_ = ({k: m_v[k] for k in ("valid", "solids", "free_edges", "dv_pct", "dist_p95", "cylinders")}
                              if m_v else None)
                    record["tried"].append({"env": v, "class": cls_v, "sec": round(sec_v, 1), "metrics": short_, "i": i})
                    print(f"PROBE {json.dumps(v)} -> {cls_v} ({sec_v:.0f} s){' ' + json.dumps(short_) if short_ else ''}",
                          flush=True)
                    if cls_v == "pass" and winner is None:
                        winner = (i, v, rc_v, out_v)
                        for f_, (j, _v) in futs.items():
                            if j != i:
                                stop(cand(j)); f_.cancel()  # the others' cores go back now, not when they end
                    elif cls_v != cls_f and cls_v in LADDERS:
                        nxt.append((cls_v, v))
        # build on the least-loosened bases only: on mechparts/31 the absorb-leftovers base spent 12 variants that the
        # exact chamfer split (no loosening) had already made pointless
        n_loose = lambda v: sum(1 for k in v if k in LOOSENING_KEYS)  # noqa: E731
        if nxt:
            fewest = min(n_loose(v) for _, v in nxt)
            nxt = [(c, v) for c, v in nxt if n_loose(v) == fewest]
        frontier = nxt
        if winner or not frontier:
            break
    _finish(pool, record, [cand(i) for i in range(next_i)], winner and cand(winner[0]), out)
    if winner:
        print(f"PROBE WINNER {json.dumps(winner[1])} after default {cls}", flush=True)
        sys.stdout.write(winner[3])
        sys.exit(winner[2])
    print(f"PROBE no variant passed (default {cls})", flush=True)
    sys.stdout.write(stdout)
    sys.exit(rc if rc is not None else 3)


def _finish(pool, record, cands, win, out):
    """Stop what still runs, keep the winner's STEP as `out`, drop the other candidates, log the probe."""
    for c in cands:
        if c != win:
            stop(c)
    pool.shutdown(wait=True, cancel_futures=True)      # every child is killed above: this returns at once
    for c in cands:
        if win is not None and c == win and c.exists():
            c.replace(out)
        else:
            c.unlink(missing_ok=True)
    if win is not None:
        record["winner"] = next(t["env"] for t in record["tried"] if cands and t.get("i") is not None
                                and out.with_name(f"{out.stem}.probe{t['i']}{out.suffix}") == win)
    _log(record)


def _log(record):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
