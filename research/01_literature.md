# Block Blast! literature review (Researcher agent)

Date: 2026-11-16. Method: web searches + page fetches. No official documentation was found; most pages are SEO/fan content that cites nothing. Confidence tags: OFFICIAL / COMMUNITY-VERIFIED / SPECULATION. Nothing found reaches OFFICIAL.

## 1. Scoring
- No official statement from Hungry Studio exists in anything found. [no OFFICIAL]
- Reverse-engineered candidate A (snickrscodes/Block-Blast-AI README; says "reverse-engineered from observed high-combo behavior in the live game"; C++ env is authoritative). Confidence: SPECULATION / weak COMMUNITY (single author, unreplicated).
  - Placement: +1 per cell of placed piece.
  - Base B(l): 0 for none, 10 for 1 line, 10*l*(l-1) for l>=2 (2 lines=20, 3=60, 4=120).
  - If any line cleared: combo c' = c+1 first, reward = cells + c' * B(l). Full-board clear: +300.
  - Combo survival: 3-slot counter. Starts at 3, decrements per placement. A clear raises combo and resets counter to 3 + pieces still in hand (3 with 0 left, 4 with 1, 5 with 2). If counter is 1 and no clear, combo resets to 0. So a combo survives roughly one full 3-piece hand without a clear (up to ~3 placements).
  - Its page says scoring varied across versions/sessions.
- Candidate B (unsupo/Block-Blast-Solver README): +1 per cell; line score = 10 * (lines)^2 * combo; combo increments on consecutive clearing moves and resets to 0 on any non-clearing placement (no grace window). README says it is ambiguous whether combo is read before or after increment. SPECULATION.
- Candidate C (another repo per search snippet, not fetched): +1 per cell, +10 per line * (1+combo).
- Vineeth14 solver blog: "+50 bonus per completed row/column" used as a heuristic only, no verification.
- Generic fan pages: 10 pts per line/cell, multiplier tables (2 lines x3, 3 lines x6, 4+ x10, +0.5x per consecutive clear), flat combo bonuses +20..+100: mutually contradictory, unsourced. Ignore. One page (blockblast-free-nocms wiki) explicitly says no verified values and rules vary by version.
- "Three-move combo window" and "10 x combo" appear in a guide about a different game (Blocky Blast Puzzle), not Block Blast.
- "Excellent/Unbelievable"-type message thresholds: not found anywhere. Open.
- Whole-board clear bonus: only candidate A (+300). Unverified.

## 2. Piece set
- Counts disagree: 27 shapes (one repo), 38 (another), 43 entries (unsupo; includes 2 duplicate 1x1 entries), 15 canonical shapes -> 41 distinct oriented poses (snickrscodes). Probably consistent with ~41 poses under no rotation/reflection duplicates.
- Families (unsupo): 1x1; bars 1x2..1x5 both orientations; 2x2, 3x3, 2x3, 3x2 rectangles; L-triominoes (4 orientations); L-tetrominoes/3x2 L shapes; T; Z/S; large 3x3 corner L (5-cell, 4 orientations); diagonal 2- and 3-cell steps. Full list not verified against the game.
- Note: pieces are fixed-orientation to the player but orientation variants appear as separate pieces.

## 3. Piece generation
- Most important finding: dev.to "Reverse Engineering Block Blast" (Ayo Omolewa; decompiled Android 10.8.2 / iOS 7.4.9 with jadx and Ghidra; logic mostly in server-delivered "traits" and encrypted JS). Claims the generator is ADAPTIVE through named traits:
  - PersonalMarginBreakTrait: once player is ~10% past personal best, 20% chance each deal repeats the same hand.
  - FastHandPunishTrait: hard pieces if last six hands averaged <4.2 s and score > 93% of high score.
  - MaturityDevelopmentV2Trait: swaps hard puzzles for easy fills when an internal score is < 65.
  - "Lethal puzzle" generator ZHI_SI_TI: two pieces that fit plus one that does not, kept only if no order/placement survives (i.e. deliberately unsolvable triple). MABXiBaiTrait gates when it may fire (never, or after 4, 6, 10, ~17, 30 min: an A/B test with six arms).
  - Author says decoding was validated by an ML port reproducing outputs; Hungry Studio did not reply; no independent verification. COMMUNITY single-source / SPECULATION.
- unsupo docs mention a "hasLiveWay" near-miss guarantee and entropy tuning (docs not read).
- Blog author (vineeth14): early-game pieces seem "suspiciously perfect" for completing lines. Anecdote.
- Playgama posts: pure random, no solvability check. Unsourced.
- snickrscodes sim: uniform over 15 canonical shapes then uniform orientation. This is the simulator's assumption, explicitly not the live game's.
- No GDC talks, patents or dev interviews found.

## 4. Modes
- Classic: endless, score-chasing until no piece fits. Adventure: level-based with objectives (collect gems, target score, obstacles). Scoring formulas for Adventure: not found; a fan blog claims focus shifts to objective lines. SPECULATION.

## 5. Web versions
- No official browser version identified. blockblast.io and many "unblocked"/portal pages (playgama, solitaired, itch.io BlockBoom, etc.) are clones or third-party hosts; fidelity unverified. Clone scoring should not be assumed to equal the app.

## 6. Community tools
- Solvers: vineeth14.github.io/BlockBlastSolver (brute force over 3! orders x positions, heuristic scoring), unsupo/Block-Blast-Solver (bitboards, 43 shapes), snickrscodes/Block-Blast-AI (RL + C++ bitboard, beam search), Botkraker/block-blast-AI, rshk941/block-blast-solver, tokaa1/blockerino (clone), durocodes/block-blast-solver, bryan7231/CSFMLBlockBlast, dev.to "From Greedy to Smart" scoring-engine post. Their scoring assumptions are the candidates listed in section 1.

## Sources
- https://dev.to/om_shree_0709/reverse-engineering-block-blast-5eop
- https://github.com/snickrscodes/Block-Blast-AI
- https://github.com/unsupo/Block-Blast-Solver
- https://vineeth14.github.io/BlockBlastSolver
- https://blockblast-free-nocms-publish.pages.dev/wiki/scoring-and-combos
- https://playgama.com/blog/2025/09/12/how-do-combos-work-and-how-to-get-them-in-block-blast/
- https://playgama.com/blog/?p=11361 , https://playgama.com/blog/?p=11177
- https://github.com/Botkraker/block-blast-AI , https://github.com/rshk941/block-blast-solver , https://github.com/tokaa1/blockerino , https://github.com/bryan7231/CSFMLBlockBlast
- https://dev.to/augsutss/from-greedy-to-smart-optimizing-the-block-blast-solvers-scoring-engine-395i
- https://www.mumuglobal.com/blog/block-blast-guide-to-high-scores.html
- https://block-blast-solver-ei9.pages.dev/block-blast-high-score/
