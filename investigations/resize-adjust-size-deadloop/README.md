# WezTerm resize freeze — `adjust_x_size`/`adjust_y_size` dead loop

**Date:** 2026-06-13
**Status:** Matched to upstream issue **#7765**, **reproduced locally**, and upstream fix
commit **`47f402a`** verified to resolve it.
**Affected build:** `20260331-040028-577474d8` (and current `main` `891bed31b` — the fix is
**not yet merged**).

## Symptom (as reported by the user)

Dragging the divider between two **side-by-side** split panes — or resizing the window in
general, even with a single tab — can make WezTerm **freeze / lock up**, pinning a CPU core.
Sometimes after a resize the text "jumps around constantly" or the window "keeps cycling
between two sizes."

## Root cause — upstream issue #7765

`mux/src/tab.rs` has two recursive helpers, `adjust_x_size` (width) and `adjust_y_size`
(height), that shrink/grow a split tree to fit a new terminal size. They run on the **GUI
main thread** inside the resize handler:

```
TermWindow::set_window_size -> apply_dimensions -> mux::tab::Tab::resize
    -> TabInner::resize -> adjust_x_size(...)   // (or adjust_y_size)
```

When a resize asks the split tree to shrink **below its minimum size (1 cell per pane)**, the
shrinking branch can make **no progress** (every pane already at 1 col/row) while the
remaining adjustment is still non-zero. The enclosing `while` loop then **spins forever** on
the main thread → window frozen, one core at 100%.

`adjust_x_size` is the **width** path, so it triggers with **side-by-side** (horizontal)
splits — exactly the divider-drag case. `adjust_y_size` is the vertical counterpart.
(Issue #3694 — "jumpy / wrong dimensions on resize over mux" — and the "cycling between two
sizes" symptom are the non-converging-resize cousins of the same sizing code.)

### The buggy code (build 577474d8, `mux/src/tab.rs`)

The `SplitDirection::Horizontal` shrink branch decrements `data.first.cols` / `data.second.cols`
only while they are `> 1`; once both are at the floor it changes nothing but the outer loop
keeps iterating because `x_adjust != 0`. No progress check → infinite loop.

### The fix — commit `47f402a` ("mux: fix dead loop in adjust_x_size/adjust_y_size")

By Toshihiro Suzuki, 2026-06-12, `refs #7765`. Adds a progress guard to both functions:

```rust
let remaining = x_adjust;
// ... shrink branch ...
if x_adjust == remaining {
    // No pane can shrink any further; stop rather than looping forever.
    return;
}
```

Plus two regression tests (`adjust_{x,y}_size_terminates_when_tree_cannot_shrink`). **Not yet
on `main`** as of 891bed31b.

## Reproduction (DONE) — `repro/resize_repro.py`

Fully local, isolated wezterm instance on X11 (`DISPLAY=:0`), own `XDG_RUNTIME_DIR` +
throwaway unix domain. It opens a roomy window, creates **24 side-by-side panes**, then
progressively **shrinks the window width with `xdotool`** and samples the GUI process CPU
(`/proc/<pid>/stat` jiffies). The dead loop shows as the GUI pinned ~100% of one core that
**does not settle** after the resize (and the main thread is the only one in state `R`).

```bash
./repro/resize_repro.py \
  --gui /projects/external-repo/wezterm/target/debug/wezterm-gui \
  --mux /projects/external-repo/wezterm/target/debug/wezterm-mux-server \
  --cli /usr/bin/wezterm --splits 24
# exit 0 = dead loop reproduced; exit 1 = settled/fixed
```

## Fix verification (DONE)

Built the buggy baseline `577474d8` and the same tree with `47f402a` cherry-picked on top
(`mux/src/tab.rs` only), same flags (`--no-default-features --features vendored-fonts` debug,
X11), and ran the identical repro:

| Build (same tree, 24 side-by-side panes) | Resize to 800px wide | Full shrink sweep → 40px |
|---|---|---|
| `577474d8` — **BUGGY** | **STUCK 100–101%, frozen** (dead loop, never settles) | never gets past 800px |
| `577474d8 + 47f402a` — **FIXED** | 71% → 57% (**settles**) | every step settles, CPU → **0%**, window keeps resizing |

**Conclusion:** issue #7765 reproduced deterministically; commit `47f402a` fixes it. With the
guard, the resize CPU decays after each step instead of pinning a core; without it the main
thread spins forever and the window freezes.

### Notes

- The loop is in the **GUI** process (the embedded mux copy), so the standalone
  `wezterm-mux-server` stays responsive — the tell is the **GUI** process at a sustained 100%
  of one core, and only its **main thread** in state `R`. (`gdb` backtrace was blocked by
  `ptrace_scope=1`; the thread-state + CPU + source evidence is conclusive.)
- Reproduces more easily with many panes stacked in one direction (less total room → easier to
  ask a sub-tree below minimum). It can also trigger with few panes if the window is shrunk
  enough.

## For the real setup (the Mac)

Both the Mac `wezterm-gui` and the Linux `wezterm-mux-server` should carry `47f402a` (build
from it, or wait for it to merge + upgrade). Until then, if it freezes: the split/session
state lives in the Linux mux, so killing the frozen Mac `wezterm-gui` and relaunching it loses
nothing (same recovery as the PaneFocused loop — see the sibling doc
`2026-06-13-wezterm-sshmux-panefocused-loop/`).

## Links

- Upstream issue: https://github.com/wezterm/wezterm/issues/7765
- Fix commit: `47f402a070b59c398fdfd131cfb6238d6e92e178` (`mux: fix dead loop in adjust_x_size/adjust_y_size`)
- Related: #3694 (jumpy/wrong dims on mux resize), #5142 (resizing in mux domains), #4084 (closed; resize+font freeze)
- WezTerm source: Linux `/projects/external-repo/wezterm`; Mac `/Users/ankit/Documents/wezterm`
