# e-KHNP Automation - Next Steps

Last updated: 2026-03-09

## Done
- Login automation works.
- Navigate to `My학습포털 > 나의 학습현황` via click flow (no direct URL goto fallback).
- Open first course from course list.
- Click the bottom `학습하기` inside `학습진행현황`.
- Learning popup open confirmed: `https://www.e-khnp.com/learning/simple/popup.do`.

## In Progress
- Auto-complete lesson steps in popup:
  - Read dynamic step text like `01/06`, `03/20`, `01/01`.
  - Click `다음` with ~5s delay.
  - If early click causes red step, click red step to recover and continue.

Current blocker:
- Player UI is split across popup page + iframe(s).
- Step text and next button are not always detected from same scope.

## Next Actions
1. Detect step progress from popup frames reliably.
2. Bind `다음` click to the correct popup frame/button region.
3. Re-run full flow and verify finish condition `current == total`.
4. Add final success log and screenshot when lesson completes.

## Deferred (Server/Remote Access)
- Goal: no local install for users, access by URL.
- Approach chosen: Cloudflare Tunnel (Option A).
- Status: deferred for later.

Planned steps:
1. Run Streamlit on Mac mini (`127.0.0.1:8501`).
2. Install `cloudflared` and open temporary URL first.
3. Move to fixed domain tunnel later (optional).
4. Add auto-start for Streamlit + tunnel.
5. Decide minimal access control policy (or open access if intentionally public).

## Debug Artifacts
- Folder: `artifacts/player_debug/`
- Used to inspect popup/page/frame text and screenshots on failure.
