#!/usr/bin/env python3
"""
Reproduce the WezTerm PaneFocused reconcile storm (upstream #4390 / PR #7763).

Runs on the Linux mux host. Orchestrates:
  1. (Mac) rapid focus churn between two panes over the SSHMUX domain
  2. (Linux) abrupt kill of the SSHMUX proxy mid-churn -> domain detach
  3. (Mac) measure wezterm-gui-log for the "pane N not found" storm + CPU

Trigger hypothesis: a PaneFocused notification in flight for a pane that the
domain-detach then tears down -> frontend spawns a failing reconcile task per
notification, faster than the UI thread drains them.

Usage: repro.py [--churn-secs N] [--detach-delay N] [--no-detach] [--no-churn]
The harness does NOT auto-recover the GUI; use --recover to kill+relaunch the
wedged gui afterwards.
"""
import argparse, json, subprocess, sys, time

MAC = "m2book"
WEZ = "/Applications/WezTerm.app/Contents/MacOS/wezterm"


def sh(*a, timeout=15):
    return subprocess.run(a, capture_output=True, text=True, timeout=timeout)


def mac(cmd, timeout=15):
    return sh("ssh", MAC, cmd, timeout=timeout)


def gui_pid():
    return mac(f'pgrep -n -f "WezTerm.app/Contents/MacOS/wezterm-gui"').stdout.strip()


def gui_log(pid):
    return f"$HOME/.local/share/wezterm/wezterm-gui-log-{pid}.txt"


def reconcile_count(pid):
    out = mac(f'grep -c "reconciling PaneFocused" {gui_log(pid)} 2>/dev/null || echo 0').stdout.strip()
    try:
        return int(out.splitlines()[-1])
    except Exception:
        return -1


def gui_cpu(pid):
    return mac(f"ps -o %cpu= -p {pid}").stdout.strip()


def two_panes():
    """Pick two pane ids in the same tab from the Mac gui's view."""
    out = mac(f"{WEZ} cli list --format json").stdout
    panes = json.loads(out)
    from collections import defaultdict
    bytab = defaultdict(list)
    for p in panes:
        bytab[p["tab_id"]].append(p["pane_id"])
    for tab, ids in bytab.items():
        if len(ids) >= 2:
            return tab, ids[0], ids[1]
    # fall back: any two panes
    ids = [p["pane_id"] for p in panes]
    return None, ids[0], ids[1]


def linux_proxy_pid():
    out = sh("pgrep", "-f", "wezterm cli --prefer-mux proxy").stdout.strip()
    return out.splitlines()[0] if out else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--churn-secs", type=float, default=3.0)
    ap.add_argument("--detach-delay", type=float, default=1.0, help="churn for this long before detach")
    ap.add_argument("--no-detach", action="store_true")
    ap.add_argument("--no-churn", action="store_true")
    ap.add_argument("--watch-secs", type=float, default=6.0)
    args = ap.parse_args()

    pid = gui_pid()
    print(f"[*] Mac gui pid={pid}")
    base = reconcile_count(pid)
    print(f"[*] baseline reconcile errors={base} cpu={gui_cpu(pid)}%")

    tab, a, b = two_panes()
    print(f"[*] churn panes {a} <-> {b} (tab {tab})")

    churn_proc = None
    if not args.no_churn:
        # background a tight activate-pane alternation on the Mac
        churn_cmd = (
            f'end=$(($(date +%s)+{int(args.churn_secs)+1})); '
            f'while [ $(date +%s) -lt $end ]; do '
            f'{WEZ} cli activate-pane --pane-id {a} >/dev/null 2>&1; '
            f'{WEZ} cli activate-pane --pane-id {b} >/dev/null 2>&1; done'
        )
        churn_proc = subprocess.Popen(["ssh", MAC, churn_cmd])
        print(f"[*] churn started for ~{args.churn_secs}s")

    if not args.no_detach:
        time.sleep(args.detach_delay)
        proxy = linux_proxy_pid()
        print(f"[*] killing Linux proxy pid={proxy} -> domain detach")
        if proxy:
            sh("kill", "-9", proxy)

    # let churn finish
    if churn_proc:
        try:
            churn_proc.wait(timeout=args.churn_secs + 3)
        except Exception:
            churn_proc.kill()

    # measure storm
    print(f"[*] watching gui log for {args.watch_secs}s ...")
    t0 = time.monotonic()
    prev = reconcile_count(pid)
    samples = []
    while time.monotonic() - t0 < args.watch_secs:
        time.sleep(1.0)
        c = reconcile_count(pid)
        cpu = gui_cpu(pid)
        rate = c - prev
        prev = c
        samples.append((round(time.monotonic() - t0, 1), c, rate, cpu))
        print(f"    t={samples[-1][0]:4}s  errors={c}  (+{rate}/s)  cpu={cpu}%")

    total = reconcile_count(pid) - base
    pinned = any(float(s[3] or 0) > 50 for s in samples if s[3] not in ("", None))
    storm = total > 100 or pinned
    print()
    print(f"[=] RESULT: {'REPRODUCED' if storm else 'no storm'}  "
          f"(+{total} reconcile errors; cpu_pinned={pinned})")
    return 0 if storm else 2


if __name__ == "__main__":
    sys.exit(main())
