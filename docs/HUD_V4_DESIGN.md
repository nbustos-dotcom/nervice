# HUD v4 — Design Document

Target: `app/static/index_v3.html` only. Single file, zero external resources, offline over
Tailscale. Theme law: **purple `#b07cff` = identity/structure, cyan `#19e3e3` = live data**.
Hard rules: no glassmorphism, no fake readouts — every pixel is a real signal or supports one.

## Sources studied (Phase 0, real web research)

- **Arwes** — arwes.dev + github.com/arwes/arwes at source level: `frames/src` (Corners,
  Octagon, Kranox, Nefrex, Underline generators), `animateFrameAssembler`, `bgs/src`
  (Dots/Puffs/GridLines/MovingLines), `animator/src` (4-state machine, stagger/sequence managers).
- **eDEX-UI** — github.com/GitSquared/edex-ui: boot sequence structure, module CSS (zero
  steady-state animation), tiered pollers, typography density, tron-theme color discipline.
- **ENCOM globe / boardroom** — github.com/arscan/encom-globe: z-as-coupled-parameters,
  fog-toward-background tinting, TextureAnimator spritesheets, pooling (SmokeProvider),
  boot's longitude-wave spatial stagger.
- **hudsandguis.com** — FUI principles: motion-with-meaning, one-hero focal hierarchy,
  fidelity tiering, decorative-vs-informative discipline (principles only; no film assets).
- **Self-found** — arc-reactor pure-CSS builds (dual-shadow emissive rings, transform-origin
  orbits), a vanilla JARVIS-HUD repo (corner-cut panels, scanline texture — and its
  anti-example: "simulated metrics"), GROK starfield canvas write-up (phase-offset twinkle,
  gradient streak trails, no-shadowBlur rule).

## Core principles (ranked, deduped)

1. **Every pixel is real signal; motion means state change.** Boot (once) → quiet loop
   (near-still) → action fired only by a real data event. One synthetic number poisons trust.
2. **One clock drives everything.** A single rAF loop owns all canvases; every animation is a
   pure function of `t`. Pause-on-hidden, throttle, and a deterministic boot come free.
3. **Two-hue semantic channel + locked alpha ramp.** Purple chrome, cyan data. Hierarchy
   within a hue is alpha only: 1.0 values / .5 labels / .3 borders / ~.08 grid.
4. **One hero.** The reactor gets the brightest glow, the most motion, the only additive
   blending. If a panel out-glows it, dim the panel.
5. **Steady-state stillness.** After boot, chrome is static; only data, the orb, and
   event-driven sweeps move. Restraint is the believability lever.
6. **Broken geometry is the frame language.** Never a closed 1px box: open corner brackets
   with unequal arms (~16px/2px strokes), asymmetric decoration on secondary panels.
7. **Depth from three cheap signals.** Per-particle z drives size + alpha + fog-tint toward
   `#04070b`; occlusion ordering (back half → core → front half); per-layer parallax rates.
8. **Pre-render everything expensive.** Glow = pre-baked radial-gradient sprites blitted with
   `drawImage`. `shadowBlur`/`ctx.filter` are banned inside rAF.
9. **Allocate once.** Flat arrays, index-phase offsets (`sin(t·ω + i)`), no per-frame objects.
10. **Boot is computed choreography.** Segment timeline `[start, dur, ease]` against one
    clock; stagger keyed by distance-from-orb (radial power-on), never random delays.
11. **Honest, tiered cadence.** Pollers keep their natural rates; sweeps fire on actual fetch
    resolution; values never tick unless data changed.
12. **Text lives in DOM** (`tabular-nums`); canvas is for grid, dust, and orb only.

## What changes per phase

### Phase 1 — Depth field (commit 1)
- `#bg` becomes a live 2-layer canvas + 1 CSS layer:
  - **Far**: perspective floor+ceiling grid converging behind the reactor, purple alpha
    .04–.14 with distance-fade `(1−z/zMax)²`; drifts slowly (z-scroll, wraps modulo spacing);
    redrawn every 2nd frame.
  - **Mid**: particle dust pool (60 desktop / 30 phone), z from a continuum driving size,
    alpha, drift, parallax; pre-baked sprites in 3 depth bands (mostly purple, ~1-in-8 cyan);
    triangular opacity envelope on respawn; phase-offset twinkle.
  - **Near**: CSS vignette div, `radial-gradient` off-center toward the orb — painted once.
- Pointer parallax (desktop, `hover:hover` only): one eased vector, layer multipliers
  far ×.15 / dust ×.5; clamped ≤8px. No gyro (iOS permission prompt not worth it).
- Drift speed × reactor state: idle 1.0, listening 1.25, speaking 1.6, thinking 2.2.
- **FX core** (built here, reused by all phases): single rAF clock; `visibilitychange`
  pauses everything; DPR cap (2 desktop / 1.5 narrow); rolling-avg frame-time auto-throttle
  ladder (dust 60→30→15, grid every 1→2→3 frames, corona `lighter` pass last) with
  hysteresis; persisted toggle `localStorage.nervice_fx` (`off` = current flat static bg,
  one-time draw) via header FX button; `prefers-reduced-motion` ⇒ flat mode.

### Phase 2 — Reactor (commit 2)
- New orb canvas inside `.reactor` (pointer-events:none), composited under the existing SVG
  tick rings + amp arc (both kept):
  - **Corona**: 3 pre-baked sprites (hot core→cyan mid→purple outer), drawn `lighter` —
    the app's only additive pass; outer breathes on slow sine, mid alpha rides REAL `--amp`.
  - **Core**: offset-highlight radial gradient (upper-left hotspot) + limb-darkening rim —
    cached gradients, two fills/frame; state-tinted (idle purple / listening cyan /
    thinking violet / speaking warm) via color lerp on state change.
  - **Two orbiting particle rings**: tilted ellipses (tilt .28/.38, opposite directions),
    `z=(sinθ+1)/2` → scale .6–1.3, alpha .3–1, sharp vs pre-blurred sprite swap; back half
    drawn before the core, front half after (real occlusion). Speed rides `--amp`.
- Center column recomposition: reactor grows to `clamp(260px, min(44vw, 38vh), 430px)`;
  clock/reactor/controls flex to consume the dead vertical space; reactor visibly dominant.
- State language preserved exactly: same `setState` machine, same labels, same tap targets.

### Phase 3 — Panel language (commit 3)
- Kill uniform 1px boxes. Per-panel: very-low-alpha fill, hairline edges at purple .3,
  open corner brackets with unequal arms (16px short / 2px stroke), title tag knocked out of
  the top hairline. Hierarchy: center panels = 4 brackets + stronger alpha; left SYSTEM =
  medium; right rail = 2 opposing corners only (Kranox-style asymmetry).
- **Data-refresh scanline**: one shared keyframes rule; thin cyan gradient bar
  (transform-only translateY sweep, ~380ms, ease-out) fired ONLY when a panel's fetch
  resolves; cleanup on `animationend`. Panels `overflow:hidden` so sweeps respect frames.

### Phase 4 — Boot sequence (commit 4)
- ≤1.6s, one segment timeline on the FX clock: 0–300ms grid/vignette ramp → 200–700ms
  reactor ignition (core scale .85→1, corona 0→1) → 500–1300ms panel cascade staggered by
  distance-from-reactor (bracket strokes draw via dashoffset where cheap, content fades
  second half) → 1300–1600ms VER stamps + one sweep per panel that has data.
- Plays once per tab session (`sessionStorage.nervice_booted`); reconnect/re-auth/modal
  cycles never replay it. `prefers-reduced-motion` or FX-off ⇒ jump to final state.

## Performance playbook (hard budget)

- One rAF; layered internal rates (grid ½-rate, decorative ≤12fps, charts untouched).
- `document.hidden` → cancel rAF (existing pollers already independent; they stay).
- DPR cap 2 (1.5 under 700px width); geometry/gradients/sprites rebuilt only on debounced
  resize.
- Auto-throttle: rolling 60-frame avg; >22ms steps a tier down, <14ms sustained steps up
  (hysteresis); tiers shrink loop bounds on flat arrays — no reallocation.
- Banned in any per-frame path: `shadowBlur`, `ctx.filter`, CSS `filter`/`backdrop-filter`,
  animated `box-shadow`/`background-position`, per-frame allocation, canvas text.
- `lighter` compositing scoped to the orb box only.
- Effects never block data: fetch handlers and WS/audio code paths are untouched by FX; FX
  reads state (`orbState`, `ampSmooth`), never writes it.

## Anti-pattern wall

No simulated metrics, no cosmetic-timer sweeps, no persistent CRT/scanline texture, no
glassmorphism/backdrop-filter, no idle pulsing chrome, no third accent hue, no closed boxes,
no randomized boot delays, no chained setTimeout choreography, no canvas text, no uniform
glow on every panel.

## Information layer grammar (v4.1)

Sources: jayse.tv (Avengers/IM3 portfolio + interviews), hudsandguis.com Iron Man/JARVIS
entries, Cantina Creative / Territory breakdowns (principles only — zero asset copying).

1. **Function-only rule** — every arc, tick, digit, and micro-label binds to a real telemetry value or it does not exist; no decorative geometry, no lorem noise — audiences feel randomness.
2. **No containers, ever** — the void is the box: a cluster = bright value + small tracked-caps label + one 1px hairline rule (purple ~35% alpha) it sits on or hangs from; separation is empty space, belonging is shared alignment edge + proximity.
3. **Two hues, two jobs** — purple #b07cff exclusively for structure (rules, arcs, ticks, leader lines, labels); cyan #19e3e3 exclusively for live data (values, pointers, sweep heads); one rationed hot accent appears only during alerts and vanishes after.
4. **Leader-line callouts** — three parts: origin node on the subject (2-3px dot/ring), a 1px elbow line at 0/45/90 degrees with max two segments (never curved), a horizontal landing rule the label sits on, text justified away from the subject; line dimmer than label, drawn on via stroke-dashoffset on spawn, retracted on dismiss.
5. **Open arcs, graduated ticks** — never close a circle: gauges are 90-270 degree arc fragments with the readout floating in the gap; radial ticks follow avionics major/minor rhythm; bright cyan partial arc over dim purple track = value-over-range; orient arc openings toward the focal subject.
6. **Hierarchy = brightness x scale** — exactly three planes: T1 focus (large cyan tabular numerals, max 2-3 on screen), T2 structure (small letterspaced caps, mid-alpha purple), T3 ambient micro-text at 15-25% alpha read as pattern; promote/demote by animating opacity+scale, never by adding frames.
7. **Motion is status, never decoration** — IDLE = slow breathing alpha (4-8s); LIVE = steady pulse / rolling digits; ACTIVATING = build-out (rule -> arc sweep -> ticks -> line draws -> label last); ALERT = brightness step + 1-2Hz pulse confined to the cluster; DISMISS = reverse collapse; never reuse one motion for two meanings.
8. **Event-driven presence; absence = silence** — sparse seeds by default; elements bloom only when relevant (scale 115%->100%, 200-400ms fast-out, no blur) and collapse when resolved; appearance itself is the status display.
9. **One stage anchors the field** — a single master reference geometry (the reactor's radial axis) every floating cluster aligns to by radius, tangent, or baseline; protected central exclusion zone kept empty; clusters in the annular mid-periphery point the eye inward.
10. **One hand, one budget** — lock tokens (1px structure / 2px emphasis, 3/6/10px ticks, one letterspacing, one easing, 30-60ms stagger) and cap attention: one T1 focal cluster + 5-7 mid-tier elements lit at once; new data buys brightness by being new, then decays a tier.
