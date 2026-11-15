# 02 Scout report - Block Blast web versions

## 1. Official browser version?
- blockblast.com (Hungry Studio official site, checked): news/download page only, links to Google Play and App Store. **No official browser game.** Apple Arcade "Block Blast!+" (Sep 2026) is iOS only.
- No official web version found. All web versions are clones/ports.

## Candidates
| Site | Type | Notes |
|---|---|---|
| https://blockblastgamefree.pages.dev/ (game iframe: https://blockblastgamefree.pages.dev/game/) | Third-party Unity WebGL clone (companyName "reun", productName "Block Blast Puzzle", build BlockBlast1.1-8) | No login, no cookie banner, no ads seen during load. 8x8 grid, 3-piece tray (tutorial shows 1 piece), crown/best score + score visible, version text "V-1.1.8-..." bottom-right. Best candidate found. |
| https://blockblastgame.io | Listed on Lutris as browser release | Navigating redirected to spam site (ttbd-xoilac-7.online). AVOID. |
| blockblast.free, block-blast.online, block-blastonline.com, playblockblastunblocked.github.io, sites.google.com/view/ubg66/block-blast, pizzaedition.com/g/blockblast | Unverified clones | Not visited (search results only); some reportedly 10x10 or ad-laden. |

## 2. Technical setup (blockblastgamefree.pages.dev/game/)
- Engine: **Unity WebGL (IL2CPP wasm)**, standard Unity loader template. Rendering: single `<canvas id="unity-canvas">` (WebGL), 1024x768. No DOM/a11y representation of board; `body.innerText` is empty. **State only readable by screenshots** (or by reverse engineering wasm).
- Files (relative to /game/Build/):
  - BlockBlast1.1-8.loader.js (brotli-encoded)
  - BlockBlast1.1-8.framework.js.unityweb (~75.8 KB)
  - BlockBlast1.1-8.wasm.unityweb (size not reported by HEAD; content-encoding header absent)
  - BlockBlast1.1-8.data.unityweb (~2.61 MB; contains Il2Cpp global-metadata likely)
  - Page wrapper scripts: /assets/js/app.js?v2.0, /assets/js/vendor.min.js?v2.0 (site chrome only). Cloudflare Insights beacon. Service worker /sw.js registered.
- JS globals: only `createUnityInstance`, `unityShowBanner`. The `unityInstance` is NOT stored globally (it is a local in the `.then` callback), so `SendMessage`/`Module` are not reachable without modifying the page. No scene tree in JS (Unity, not Cocos).
- Implication for Code-Analyst: client logic is C# compiled to wasm via IL2CPP; scoring/piece generation would need dump of `data.unityweb` global-metadata.dat + wasm (Il2CppDumper) - offline analysis of downloaded files (downloads need user permission). Note this is a clone ("reun"), so its algorithm may differ from the official game; the official client is not available on web.
- Direct game URL works standalone (no iframe needed): https://blockblastgamefree.pages.dev/game/. Wrapper page uses same-origin iframe 1016x700.

## 3. Controls trial (INCOMPLETE)
- Viewport (CSS) 1024x768, dpr 1. Screenshot/coordinate frame given to the tool is 800x600 (scale 0.78125; tool maps correctly, e.g. 400,493 -> 512,631 CSS).
- Layout in 800x600 coordinates: score at (400,83); best-score crown (265,83); grid 8x8 spans x 244..556, y 125..437, cell pitch ~39px; cell centres x = 263 + 39*col, y = 144 + 39*row; piece tray centre ~ (400,493) (a single piece shown in tutorial; 3 pieces expected spread horizontally below grid).
- Game opens in a forced tutorial: board pre-filled with 3-wide colored rows, one blue 1x3 horizontal bar in tray, target is the empty row 3 (y~261, cols 3-5). A looping tutorial hand animation overlays.
- `left_click_drag` is delivered properly (verified by logging events: pointerdown, 2 pointermoves, pointerup, i.e. only coarse motion) but the piece was NOT placed in 5 attempts (several start/end offsets, plus click-click). Score stayed 0. Likely causes: (a) the browser pane was reported **hidden**, so the page runs as background page with throttled frames, and Unity polls input per frame, or (b) the drag has too few intermediate moves / zero hold time, or (c) the piece lifts with an offset above the pointer. Not resolved. No score changes observed; no combo messages seen.
- Recommendation: make sure the browser window is visible before playing, then retry `left_click_drag` from the tray piece centre to the target cell (aim for the pointer slightly below the target if the piece floats above the finger); if it still fails, try another browser automation tool or desktop control (real mouse with press-and-hold) instead.

## Obstacles
- Redirect/spam on blockblastgame.io.
- Forced tutorial must be completed first.
- No machine-readable state; no official source.
- Third-party Unity clone, fidelity to official scoring unknown.
