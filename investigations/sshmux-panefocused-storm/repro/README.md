# Reproduction: WezTerm PaneFocused notification storm (#4390 / PR #7763)

Two harnesses here:

| File | Path it exercises | Result |
|------|-------------------|--------|
| `local_repro.py` | **single host**, plain unix domain | **Reliable runaway.** Use this. |
| `repro.py` | Mac GUI ↔ Linux SSHMUX (kill proxy mid-churn) | Only a *finite* burst (SSH round-trips throttle the in-flight notifications). Kept for reference. |

## The mechanism (why local works)

PR #7763 fixes three compounding bugs. The one that makes the loop **self-sustaining**
is problem #2, *redundant re-notification*: every focus change fires
`MuxNotification::PaneFocused` **twice** (once from `advise_focus_change`, once from the
call site). Two rapid switches → the doubled events interleave and the reconcile queue
never drains. That needs no SSH and no second machine — a local unix domain reproduces it.

(The original SSHMUX incident was the same core bug, amplified by problem #1 — a detach
firing `PaneFocused` for destroyed pane IDs — and problem #3 — the client echoing the
server's notification back as a fresh `SetFocusedPane`.)

## Run it

```bash
# buggy baseline (installed binaries):
./local_repro.py

# a specific build (e.g. patched PR #7763):
./local_repro.py \
  --gui /projects/external-repo/wezterm/target/debug/wezterm-gui \
  --mux /projects/external-repo/wezterm/target/debug/wezterm-mux-server \
  --cli /projects/external-repo/wezterm/target/debug/wezterm
```

Needs a display (`DISPLAY`/`WAYLAND_DISPLAY`). The harness is fully isolated — it uses its
own `XDG_RUNTIME_DIR=/tmp/wezrepro/xrd` and a throwaway `repro` unix domain, so it cannot
disturb your real mux/sessions.

### What you'll see

- **Buggy:** after the drive phase stops, focus keeps flipping with zero input
  (observed ~7–8 flips/s by a 20 Hz sampler; true internal rate far higher) and GUI CPU
  climbs monotonically and never settles. `VERDICT: RUNAWAY`. Exit code 0.
- **Fixed:** focus quiesces the moment input stops; CPU returns to idle.
  `VERDICT: SETTLED`. Exit code 1.

### Measured baseline (installed build 20260117-154428-05343b38)

```
PHASE 1: 365 activate-pane calls in 4s -> gui cpu 0.3% -> 7.2%
PHASE 2 (zero input):
  t= 0.0s cpu=39.0% flips=1
  t= 3.0s cpu=41.5% flips=22
  t= 6.1s cpu=44.0% flips=46
  t= 9.1s cpu=46.2% flips=64
  t=12.0s cpu=48.3% flips=86
  t=15.0s cpu=50.3% flips=112
VERDICT: RUNAWAY (queue never drains)
```
