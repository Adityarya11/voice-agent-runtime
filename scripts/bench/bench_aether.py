import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AETHER_ROOT = os.path.abspath(os.path.join(REPO_ROOT, "..", "AetherRTC"))
RESULT_DIR = os.path.join(REPO_ROOT, "docs", "benchmarks", "latest")
BENCH_SRC = os.path.join(os.path.dirname(__file__), "aether")

FIX_COMMIT = "632fa47"
DNS_TARGET_LINE = 'return "passthrough:///" + addr'


def run(cmd, cwd=None, timeout=300, env=None):
    out = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
    )
    return out.returncode, (out.stdout + out.stderr).strip()


def git(*args, cwd=AETHER_ROOT):
    code, out = run(["git", *args], cwd=cwd)
    if code != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out}")
    return out


def stats(values):
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
    }


def add_worktree(path, rev):
    git("worktree", "add", "--detach", path, rev)


def copy_bench_sources(worktree):
    open_dir = os.path.join(worktree, "cmd", "bench_bridge_open")
    os.makedirs(open_dir, exist_ok=True)
    shutil.copy(
        os.path.join(BENCH_SRC, "bridge_open", "main.go"),
        os.path.join(open_dir, "main.go"),
    )
    shutil.copy(
        os.path.join(BENCH_SRC, "speaking_gate", "bench_speaking_gate_test.go"),
        os.path.join(worktree, "internal", "webrtc", "bench_speaking_gate_test.go"),
    )


def make_dns_control(worktree):
    path = os.path.join(worktree, "internal", "bridge", "client.go")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if DNS_TARGET_LINE not in src:
        raise RuntimeError("control arm: dialTarget line not found; fix has changed")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src.replace(DNS_TARGET_LINE, "return addr"))


def build_open_binary(worktree):
    exe = os.path.join(worktree, "bridge_open.exe")
    code, out = run(
        ["go", "build", "-o", exe, "./cmd/bench_bridge_open"], cwd=worktree
    )
    if code != 0:
        raise RuntimeError(f"go build failed in {worktree}: {out}")
    return exe


def measure_open(exe, host):
    code, out = run([exe, "-host", host], timeout=120)
    line = out.splitlines()[-1] if out else ""
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"error": f"unparseable output (exit {code}): {out[:200]}"}


def dns_diagnostics():
    timing = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "$sw=[Diagnostics.Stopwatch]::StartNew();"
            "try { Resolve-DnsName -Name _grpc_config.localhost -Type TXT "
            "-DnsOnly -ErrorAction Stop | Out-Null; $r='answered' } "
            "catch { $r=$_.Exception.GetType().Name };"
            "$sw.Stop(); '{0}|{1}' -f $sw.Elapsed.TotalSeconds, $r",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    servers = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-DnsClientServerAddress -AddressFamily IPv4 | "
            "Where-Object { $_.ServerAddresses } | "
            "ForEach-Object { $_.InterfaceAlias + ': ' + ($_.ServerAddresses -join ',') }",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    seconds, _, outcome = timing.stdout.strip().partition("|")
    return {
        "txt_lookup_seconds": float(seconds) if seconds else None,
        "txt_lookup_outcome": outcome,
        "dns_servers": [s for s in servers.stdout.splitlines() if s],
    }


def run_gate_test(worktree, out_path, reps):
    env = dict(os.environ, AETHER_BENCH_OUT=out_path, AETHER_BENCH_REPS=str(reps))
    code, out = run(
        ["go", "test", "-count=1", "-run", "TestSpeakingGateSweep", "-v", "./internal/webrtc"],
        cwd=worktree,
        timeout=900,
        env=env,
    )
    if code != 0 or not os.path.exists(out_path):
        return {"error": out[-1500:]}
    with open(out_path, encoding="utf-8") as f:
        rows = json.load(f)
    by_gap = {}
    for gap in sorted({r["gap_ms"] for r in rows}):
        subset = [r for r in rows if r["gap_ms"] == gap]
        by_gap[str(gap)] = {
            "reps": len(subset),
            "unmuted_mid_reply": sum(1 for r in subset if r["unmuted_mid_reply"]),
            "mid_reply_open_ms": stats(
                [r["mid_reply_open_ms"] for r in subset if r["unmuted_mid_reply"]]
            ),
            "release_after_delivery_ms": stats(
                [r["release_after_delivery_ms"] for r in subset]
            ),
        }
    return {"by_gap": by_gap, "raw": rows}


def main():
    parser = argparse.ArgumentParser(description="Reproduce the AetherRTC 632fa47 claims")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--gate-reps", type=int, default=5)
    parser.add_argument("--out", default=RESULT_DIR)
    parser.add_argument("--skip-gate", action="store_true")
    parser.add_argument("--skip-open", action="store_true")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    before_rev = git("rev-parse", f"{FIX_COMMIT}^")
    after_rev = git("rev-parse", FIX_COMMIT)
    tmp = tempfile.mkdtemp(prefix="aether_bench_")
    arms = {
        "before": (os.path.join(tmp, "before"), before_rev),
        "after": (os.path.join(tmp, "after"), after_rev),
        "after_dns_control": (os.path.join(tmp, "control"), after_rev),
    }

    result = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "before_commit": before_rev,
        "after_commit": after_rev,
        "aether_head_at_run": git("rev-parse", "HEAD"),
        "go_version": run(["go", "version"])[1],
        "dns": dns_diagnostics(),
    }

    created = []
    try:
        for name, (path, rev) in arms.items():
            add_worktree(path, rev)
            created.append(path)
            copy_bench_sources(path)
        make_dns_control(arms["after_dns_control"][0])

        if not args.skip_open:
            exes = {n: build_open_binary(p) for n, (p, _) in arms.items()}
            samples = {n: [] for n in arms}
            print(f"== bridge open: {args.runs} runs per arm, interleaved ==", flush=True)
            for i in range(1, args.runs + 1):
                for name in arms:
                    res = measure_open(exes[name], "localhost")
                    samples[name].append(res)
                    print(
                        f" run {i:2d} {name:<18} "
                        f"server_received_ms={res.get('server_received_ms', float('nan')):.1f}"
                        f"{'  ERROR ' + res['error'] if res.get('error') else ''}",
                        flush=True,
                    )
            result["bridge_open"] = {
                name: {
                    "construct_ms": stats([s["construct_ms"] for s in rows if "construct_ms" in s]),
                    "open_return_ms": stats([s["open_return_ms"] for s in rows if not s.get("error")]),
                    "server_received_ms": stats(
                        [s["server_received_ms"] for s in rows if not s.get("error")]
                    ),
                    "errors": [s["error"] for s in rows if s.get("error")],
                    "raw": rows,
                }
                for name, rows in samples.items()
            }

        if not args.skip_gate:
            result["speaking_gate"] = {}
            for name in ("before", "after"):
                print(f"== speaking-state gate sweep: {name} ==", flush=True)
                out_path = os.path.join(args.out, f"speaking_gate_{name}.json")
                result["speaking_gate"][name] = run_gate_test(
                    arms[name][0], out_path, args.gate_reps
                )
    finally:
        for path in created:
            subprocess.run(
                ["git", "worktree", "remove", "--force", path],
                cwd=AETHER_ROOT,
                capture_output=True,
            )
        subprocess.run(["git", "worktree", "prune"], cwd=AETHER_ROOT, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    path = os.path.join(args.out, "aether.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"wrote {path}")

    report = os.path.join(os.path.dirname(__file__), "make_report.py")
    command = [sys.executable, report, "--data", args.out]
    if os.path.abspath(args.out) != os.path.abspath(RESULT_DIR):
        command += ["--out", os.path.join(args.out, "benchmarks.md")]
    subprocess.run(command, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
