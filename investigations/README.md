# WezTerm bug investigations (ankitson fork)

Root-cause writeups, reproductions, and fix verifications for several WezTerm
bugs hit on a Mac-GUI ↔ Linux-mux (SSHMUX) setup. Each folder is self-contained
(README + `repro/`). Fixes are carried on the `fix/*` branches of this fork.

| Folder | Issue(s) | Status | Branch |
|--------|----------|--------|--------|
| [`sshmux-panefocused-storm`](sshmux-panefocused-storm/) | #4390 PaneFocused notification storm (SSHMUX focus flip loop) | reproduced + fix verified; PR #7763 review addressed | `fix/panefocused-reconcile-storm` |
| [`resize-adjust-size-deadloop`](resize-adjust-size-deadloop/) | #7765 `adjust_x_size`/`adjust_y_size` dead loop on resize | reproduced + fix verified | `fix/adjust-size-deadloop` |
| [`mux-resize-sync`](mux-resize-sync/) | #5117 attach undersize (fixed); #5142/#3694 drag desync (analyzed) | #5117 fix deployed; #5142 tractability assessed | `fix/activate-tab-resize` |

All three fixes are deployed locally (Mac `wezterm-gui` bundle + Linux
`wezterm-mux-server`). The fixes are unmerged upstream.

`_scratch/` (untracked) holds the ad-hoc integration-testing scripts used during
the investigations (isolated-instance launchers, repro drivers, the gdb trigger,
pane-size probe, the activate-tab fix patch).
