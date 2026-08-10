# 06 - Visual direction and UI

Agent 6 deliverable. Aesthetic direction, token system, layout, signature element,
state design, and the Basecoat/shadcn override surface.

Written 2026-08-09. Planning only, no code was written to the Mini.

---

## Decisions

1. **The signature element is the provenance plate**: one fixed-width rectangle in a
   right-hand gutter, present on every row, with seven states. Wanted rows show
   *what the store promises* (`UP TO 24/96`). Owned rows show *what actually landed*
   (`24/96 FLAC`, store, date), rendered as a stamp. Promised versus delivered is the
   whole product thesis compressed into one object that changes at the moment of purchase.
2. **Not cards, a rack.** Flat rows on one surface, divided by hairlines, with sticky
   group headers. 160 floating shadowed cards is 160 pieces of visual noise and is
   actively hostile with visual snow. No shadows in the list at all.
3. **Palette is six values**: cool grey-green card stock, a lighter sleeve surface,
   blue-black ink, a fade grey, one accent (`#5A2A7A` aniline violet, the color of a
   rubber stamp pad) and one flag red. Violet means provenance and nothing else, ever.
   It is deliberately kept out of the shadcn token namespace so no Basecoat component
   can borrow it.
4. **Type is Archivo alone (variable, two widths).** Expanded Archivo for the masthead
   and group headers because expanded grotesques are record-label typography; the same
   family, narrower and smaller, for every plate, price, spec and timestamp. A second
   monospaced face was tried for those and cut: it read as a terminal rather than as a
   sleeve, and `tabular-nums` already gives the column alignment that was the reason
   for it.
5. **`--radius: 0.125rem` (2px).** This single override does more than any other to stop
   the result reading as a Tailwind admin panel. Not 0px, because hairline rules plus
   zero radius is its own recognisable template.
6. **New tracks arriving over SSE never move the page.** They buffer behind a sticky
   "3 new loves, show" control unless the user is at the top and idle. Existing rows are
   patched in place and never re-sorted.
7. **The claim reports named stages, not a spinner**, because every failure mode is
   stage-specific and the user needs to know which one broke.
8. **The confidence refusal is a first-class UI state**, not an error toast. It names the
   candidate it rejected and its score, states plainly that nothing was downloaded, and
   requires an explicit second click to override. This is the 2026-08-02 incident turned
   into a permanent interface contract.
9. **No stagger, no shimmer, no grain, no noise, no gradient mesh, anywhere.** The only
   orchestrated motion in the entire app is the stamp press, which fires once per purchase.

---

## What the baseline gets wrong

The current UI (one `PAGE` string, `app.py:13-121`) is competent Apple Music Store
pastiche. Four specific problems worth naming, because the redesign is answering them:

| Problem | Where | Why it matters |
|---|---|---|
| Provenance is absent | nowhere in `card()` | The app's entire reason to exist is that you own a file at a known quality. Nothing on screen says what quality. |
| The Buy spinner is a lie about system state | `buy()`, "waiting for purchase…" | Nothing is being awaited server side. A spinner that spins forever teaches the user to distrust every spinner. |
| Full re-render on a 60s timer | `setInterval(()=>{load()},60000)` | Rewrites the DOM under a user mid-interaction, loses audio state, loses scroll. |
| The ignore control is a 15px glyph | `.nvm` | Roughly a 23x23px hit area sitting next to a 40px button. Miss it and you claim a track you did not buy. |

Also: `box-shadow` on every one of 160 cards, a `#0071e3` accent lifted straight from
Apple, and the system font stack. All three are why it reads as pastiche.

---

## Pass 1: the token system

### Color

Six named values. Everything else is derived with `color-mix`, so there is no
hand-picked eleventh grey.

| Token | Light | Role |
|---|---|---|
| `--stock` | `#E7E8E2` | Page ground. Card stock: cool, faintly green-grey. |
| `--sleeve` | `#F7F7F3` | The raised surface the rows sit on. |
| `--ink` | `#1E2226` | Primary text, primary button fill. Blue-black, not `#000`. |
| `--fade` | `#6A7078` | Secondary text, rules, the ghost plate. |
| `--stamp` | `#5A2A7A` | Provenance only. Plate rule, spec text, the owned stamp. |
| `--flag` | `#9E2B25` | Failure only. Refusals, dead credentials, download errors. |

Dark theme remaps the same six:

| Token | Dark |
|---|---|
| `--stock` | `#15181B` |
| `--sleeve` | `#1D2125` |
| `--ink` | `#E9EAE4` |
| `--fade` | `#98A0A8` |
| `--stamp` | `#C79BEE` |
| `--flag` | `#E8837B` |

```css
:root{
  --stock:#E7E8E2; --sleeve:#F7F7F3; --ink:#1E2226;
  --fade:#6A7078;  --stamp:#5A2A7A;  --flag:#9E2B25;

  /* derived, never hand-picked */
  --rule:        color-mix(in srgb, var(--fade) 22%, transparent);
  --rule-firm:   color-mix(in srgb, var(--fade) 38%, transparent);
  --hover:       color-mix(in srgb, var(--ink)   5%, transparent);
  --plate-ghost: color-mix(in srgb, var(--fade) 55%, transparent);
  --img-edge:    oklch(0 0 0 / 0.10);
}
:root:not([data-theme="light"]){
  @media (prefers-color-scheme: dark){
    --stock:#15181B; --sleeve:#1D2125; --ink:#E9EAE4;
    --fade:#98A0A8;  --stamp:#C79BEE;  --flag:#E8837B;
    --img-edge: oklch(1 0 0 / 0.10);
  }
}
:root[data-theme="dark"]{ /* same six as above, so the toggle wins both ways */ }
```

Deliberate non-choices, stated so they are not re-litigated later:

- **Not near-white on near-black, and not pure `#000` on pure `#FFF`.** Maximum contrast
  shimmers with visual snow. Body text lands around 13:1, meta around 5:1. High enough to
  scan, short of the range that vibrates.
- **No store brand colors.** Bandcamp teal and Qobuz black are in the baseline
  (`--bc`, `--qz`). Store identity belongs in the plate's text, not in the palette,
  otherwise adding 7digital means inventing a seventh color.
- **Light is the default.** Dark makes cover art pop and every music app knows it, but
  the primary user here has visual snow syndrome, and a large dark field with bright type
  is the worst case for that. Dark is fully supported, not default.

### Type

One file, three roles.

| Role | Face | Setting |
|---|---|---|
| Display | **Archivo Expanded** (variable, `wdth 115`) | 700, tracking `-0.015em`, uppercase. Masthead, group headers. |
| Body | **Archivo** (same file, `wdth 100`) | 400/500/600. Track titles, artist names, buttons, prose. |
| Utility | **Archivo** (same file) | Plates, prices, sample rates, timestamps, paths, stage labels. Smaller, tracked out, uppercase, `tabular-nums`. |

Archivo's variable width axis means all three roles are one 40KB woff2 that reads as more
than one typeface. Subset to latin, **self-hosted in the image**. No Google Fonts: this is
a LAN app that may have no outbound internet and should not phone home.

Fallbacks matter because the fonts can fail to load on a first paint:

```css
--font-sans: Archivo, "Helvetica Neue", Arial, sans-serif;
```

Why an expanded grotesque and not a serif: high-contrast serif display on a warm ground is
the single most recognisable machine-generated look right now. Expanded grotesque is also
the actual vernacular of record label identity (Blue Note, Factory, ECM sleeves), so it is
both less defaulted and more correct for the subject.

### Scale

Six steps. The list view is allowed to show at most four of them at once, because scan
speed collapses when a page has more than four type sizes competing.

| Token | Size / line | Face | Used for |
|---|---|---|---|
| `--t-display` | 28px / 1.0 | Archivo Exp 700 | Masthead only |
| `--t-group` | 12px / 1.0 | Archivo Exp 700, `+0.10em`, upper | Sticky group headers |
| `--t-title` | 15px / 1.35 | Archivo 600 | Track title |
| `--t-body` | 14px / 1.5 | Archivo 400 | Prose, empty states |
| `--t-sub` | 13px / 1.4 | Archivo 400, `--fade` | Artist, source, time |
| `--t-plate` | 11px / 1.25 | Archivo 600, `+0.06em`, upper | Plate lines, stage labels |
| `--t-micro` | 10px / 1.2 | Archivo 400, `+0.10em`, upper | Stamp date, paths |

### Space, radius, motion

```css
--space: 4px;            /* everything is a multiple: 4 8 12 16 24 32 48 */
--radius: 2px;           /* plates, buttons, inputs, the masthead rule */
--radius-sleeve: 4px;    /* cover art, the one nested element */
--row-h: 76px;           /* desktop row, 2 lines of meta + 12px padding */
--plate-w: 132px;        /* fixed, so plates form a column down the page */

--ease: cubic-bezier(0.2, 0, 0, 1);
--t-fast: 120ms;         /* hover, focus, color */
--t-mid:  180ms;         /* row enter, plate state change */
--t-press: 220ms;        /* the stamp, and nothing else */
```

Concentric radii check: cover art is `--radius-sleeve` 4px inside a row with 0 radius and
12px padding, which is correct (a square container never fights a rounded child). The plate
is 2px with a 3px inset inner rule, so the inner rule is square (2 minus 3 is negative).
Stated explicitly so nobody "fixes" it to 2px later and pinches the corner.

### Signature

**The provenance plate.** A `132px` wide, 2px-radius rectangle in a fixed right-hand gutter
on every row. Two tracked-out lines plus a four-segment measure rule. It is the same
rectangle in all seven of its states, which is what makes it teachable: the user learns one
object, not seven badges.

The tier ladder maps to something physically real, so the segments are information rather
than decoration:

| Segments | Condition | Plate label |
|---|---|---|
| 1 | Lossy, any bitrate | `MP3 320` |
| 2 | 16-bit, up to 48 kHz | `16/44 FLAC` |
| 3 | 24-bit, 44.1 to 96 kHz | `24/96 FLAC` |
| 4 | 24-bit above 96 kHz, or DSD | `24/192 FLAC`, `DSD64` |

Wanted rows render the ladder **outlined** and prefix the label with `UP TO`, because before
you buy, the number is a claim the store is making. Owned rows render it **filled** with no
prefix, because after you buy, the number is a fact about a file on your disk. If the
delivered tier is lower than the promised one, the stamp says so in a `--fade` line
underneath: `listed 24/96, file is 16/44`. No other music software discloses that, and it is
exactly what a self-hosted, you-own-the-file app should be honest about.

---

## Pass 2: critique of the plan, and what changed

Run against the brief before writing any markup, as the skill requires.

| Concern | Verdict | Change made |
|---|---|---|
| Grey-green stock plus a violet accent is one step off the cream + serif + terracotta default | Adjacent but distinct on all three axes: the ground is cool not warm, the accent is cool not warm, the display face is an expanded grotesque not a serif | Kept, check recorded |
| Mono spec plates could read as generic developer aesthetic | Justified by content: sample rates, prices and dates are tabular data that must align vertically and must not shift width when they change. Confined to the gutter, not sprayed across the UI | Kept |
| A header row of stat tiles ("160" big, "wanted" small) is the template answer to "show the numbers" | It is | **Revised twice.** First to one running line of text rather than tiles, then to nothing at all: the tabs below carry a count each beside the view it describes, so a masthead ledger repeated three numbers the eye already had. The masthead is now `LIBRARY WISHLIST` in a plate-shaped rule and nothing else |
| Grouping by love-date is arbitrary if you love 40 tracks in one sitting | Partly true, and grouping by *what you would get* is more useful for the actual task of buying | **Revised.** Grouping is a control. Default is date, second option is tier (`24-bit available` / `CD quality` / `Bandcamp only` / `no store found`), which makes provenance structural a second time |
| The rotated stamp risks kitsch | Real risk | Constrained: `-1.5deg` only, no drop shadow, no texture, no distress marks. Fallback if it still reads cheap is `0deg` with a doubled inner rule |
| Staggered row entrance | Would be actively bad at 160 rows | **Cut entirely.** No stagger anywhere in the app. This is the "remove one accessory" cut |
| A hi-res share chart in the header | Nice, not needed | **Cut.** The `61 listed in 24-bit` in the masthead line carries it |
| Showing a price on every plate | Stores do not reliably return one | Price renders only when the store actually supplied it. Never fabricate, never show `--` |
| Violet as the focus ring | Would break the "violet means provenance" rule | **Revised.** Focus ring is `--ink`, 2px, with a 2px `--stock` offset. Roughly 13:1 on the sleeve, unmistakable, and violet stays purely semantic |

The one deliberate risk: **aniline violet as the only accent in a category that is uniformly
black, red, green and orange**, paired with near-square 2px corners. If it fails it will fail
by reading as "library stationery" rather than "record shop". That is a defensible failure
and it is the direction's whole point, so it is the risk being taken rather than hedged.

---

## The provenance plate: seven states

One rectangle, seven states. This table is the spine of the whole interface and should be
treated as the contract between this document and Agent 4 (matching) and Agent 5 (jobs/SSE).

| # | State | Line 1 | Line 2 | Ladder | Rule / text color |
|---|---|---|---|---|---|
| 1 | `RESOLVING` | `RESOLVING` | (blank) | none | `--plate-ghost` |
| 2 | `NO STORE` | `NO STORE` | `SEARCH ↗` | none | `--plate-ghost` |
| 3 | `AVAILABLE` | `UP TO 24/96` | `QOBUZ 1.79` | outlined | `--plate-ghost` rule, `--fade` text |
| 4 | `AWAITING` | `UP TO 24/96` | `OPENED 14:02` | outlined | `--fade`, rule at `--rule-firm` |
| 5 | `WORKING` | stage name | `3 OF 5` | outlined | `--ink` |
| 6 | `REFUSED` / `FAILED` | `MATCH REFUSED` | `0.42` | outlined | `--flag` |
| 7 | `OWNED` | `24/96 FLAC` | `QOBUZ 07.08.26` | filled | `--stamp`, 1.5px rule, `rotate(-1.5deg)` |

State 1 is easy to forget and matters: a track lands from Last.fm before enrichment has
found a store, so for a few seconds the plate has nothing to say. It says `RESOLVING` rather
than rendering an empty box, and the row is not actionable until it resolves.

```
   AVAILABLE (wanted)              OWNED (stamped)

  ┌────────────────┐             ╭────────────────╮
  │ UP TO 24/96    │             │ 24/96  FLAC    │
  │ QOBUZ     1.79 │             │ QOBUZ 07.08.26 │
  │ ▯▯▯▭           │             │ ▮▮▮▭     OWNED │
  └────────────────┘             ╰────────────────╯
   1px --plate-ghost              1.5px --stamp, -1.5deg
```

---

## Layout

Single centered column, `max-width: 1080px`, 24px gutters. Rows are flat, divided by
`--rule` hairlines, sitting on `--sleeve` over a `--stock` page. Nothing in the list is
elevated, so nothing in the list has a shadow.

### Wanted view, desktop

```
┌─ max 1080px, centered ─────────────────────────────────────────────────────┐
│                                                                            │
│  ┌──────────────────┐                                                      │
│  │ LIBRARY WISHLIST │  160 wanted · 2 owned · 61 listed in 24-bit          │
│  └──────────────────┘                                                      │
│                                                                            │
│   Wanted   Owned   Ignored              group: date ▾      ⌕ filter ______ │
│  ════════                                                                  │
│                                                                            │
│  THU 7 AUG · 4 ─────────────────────────────────────────────────  (sticky) │
│                                                                            │
│  ┌────┐  Lies                              ┌────────────────┐              │
│  │▶ ▨ │  CHVRCHES                          │ UP TO 24/96    │ [ Buy ▸ ]  ✕ │
│  │    │  Last.fm · 14:02                   │ QOBUZ     1.79 │              │
│  └────┘                                    │ ▯▯▯▭           │              │
│  ──────────────────────────────────────────────────────────────────────────│
│  ┌────┐  Hesitating Beauty                 ┌────────────────┐              │
│  │▶ ▨ │  Billy Bragg                       │ UP TO 16/44    │ [ Buy ▸ ]  ✕ │
│  │    │  ListenBrainz · 09:40              │ BANDCAMP       │              │
│  └────┘                                    │ ▯▯▭▭           │              │
│  ──────────────────────────────────────────────────────────────────────────│
│  ┌────┐  Souvlaki Space Station            ┌────────────────┐              │
│  │▶ ▨ │  Slowdive                          │ NO STORE       │  Search ↗  ✕ │
│  │    │  Navidrome · 08:51                 │ SEARCH ↗       │              │
│  └────┘                                    │                │              │
│  ──────────────────────────────────────────────────────────────────────────│
│                                                                            │
│  WED 6 AUG · 11 ────────────────────────────────────────────────  (sticky) │
```

The plate gutter is fixed width and right-aligned, so the plates form a readable column
down the page. Scanning "which of these can I get in 24-bit" is a single vertical sweep of
the ladders, with no reading required. That is the layout doing the work instead of a badge.

### Row anatomy

```
  ├─12─┤              ├────────── flexible ──────────┤ ├─132─┤ ├─ auto ─┤├40┤
  ┌────────┐
  │ 64x64  │  Title            15px Archivo 600            plate    Buy    ✕
  │ cover  │  Artist           13px Archivo 400 --fade
  │        │  Source · time    10px Archivo, tracked, upper, --fade
  └────────┘
  4px radius, 1px --img-edge outline inset
```

Row height 76px. Hover changes background to `--hover` and nothing else moves. The `✕`
ignore control is a 16px glyph with a 40x40 hit area extended by a pseudo-element, with an
8px gap so it never overlaps the Buy button's hit area (the baseline's does).

### Owned view

The payoff view, which the baseline does not have at all. Same rows, stamped plates, and a
library path instead of actions.

```
│  ┌────┐  Lies                          ╭────────────────╮                  │
│  │  ▨ │  CHVRCHES                      │ 24/96  FLAC    │  Music/CHVRCHES… │
│  │    │  bought 7 Aug via Qobuz        │ QOBUZ 07.08.26 │                  │
│  └────┘                                │ ▮▮▮▭     OWNED │                  │
│                                        ╰────────────────╯                  │
│  ──────────────────────────────────────────────────────────────────────────│
│  ┌────┐  Wichita Lineman               ╭────────────────╮                  │
│  │  ▨ │  Glen Campbell                 │ 16/44  FLAC    │  Music/Glen_Camp…│
│  │    │  bought 2 Aug via Qobuz        │ QOBUZ 02.08.26 │                  │
│  └────┘  listed 24/96, file is 16/44   │ ▮▮▭▭     OWNED │                  │
│                                        ╰────────────────╯                  │
```

### Mobile, under 640px

The gutter cannot survive at phone width, so the plate becomes a full-width strip under the
title. It stays the same object with the same states.

```
┌────────────────────────────────┐
│ ┌────┐ Lies                    │
│ │▶ ▨ │ CHVRCHES                │
│ └────┘ Last.fm · 14:02         │
│                                │
│ ┌────────────────────────────┐ │
│ │ UP TO 24/96    QOBUZ  1.79 │ │
│ │ ▯▯▯▭                       │ │
│ └────────────────────────────┘ │
│                                │
│ [       Buy at Qobuz ▸      ]  │
│ [  I bought it  ]        [ ✕ ] │
│ ────────────────────────────── │
```

Cover drops to 48px, every control is at least 44x44, the sticky group header stays.

### First run

The screen most people actually see first, and the one the baseline does not handle at all.
Numbered, because unlike decorative `01 / 02 / 03` eyebrows this genuinely is a sequence:
you cannot buy before a source has given you something to buy.

```
│  ┌──────────────────┐                                                      │
│  │ LIBRARY WISHLIST │                                                      │
│  └──────────────────┘                                                      │
│                                                                            │
│  Nothing is connected yet. Three steps.                                    │
│                                                                            │
│  1  Where your loves come from                      [ Connect a source ]   │
│     Last.fm, ListenBrainz, Navidrome, Deezer                               │
│  ────────────────────────────────────────────────────────────────────────  │
│  2  Where you buy                                    [ Connect a store ]   │
│     Qobuz, Bandcamp, 7digital                                              │
│  ────────────────────────────────────────────────────────────────────────  │
│  3  Where files land                          /music            [ Change ] │
│     Navidrome rescans after each claim                                     │
```

---

## Basecoat and shadcn: which tokens to override

Basecoat reads shadcn's CSS variable names. Left untouched, they produce the exact
grey-white-rounded admin panel this design is trying not to be. These are the overrides
that matter, in rough order of how much each one changes the read.

```css
:root{
  /* 1. The single highest-impact override. shadcn ships 0.5-0.625rem on
        everything, which is the look. 2px reads as printed matter.
        Not 0px: hairline rules + zero radius is its own known template. */
  --radius: 0.125rem;

  /* 2. Surfaces. Default shadcn is white cards on near-white background.
        Here the card is only ~4% lighter than the ground. */
  --background: var(--stock);
  --foreground: var(--ink);
  --card: var(--sleeve);
  --card-foreground: var(--ink);
  --popover: var(--sleeve);
  --popover-foreground: var(--ink);

  /* 3. Primary stays ink, NOT the violet. The violet is reserved. */
  --primary: var(--ink);
  --primary-foreground: var(--sleeve);
  --secondary: color-mix(in srgb, var(--ink) 8%, var(--sleeve));
  --secondary-foreground: var(--ink);

  /* 4. In shadcn, --accent is the hover background, not a brand color.
        Naming it after the brand accent is how the violet would leak into
        every dropdown row. Point it at the neutral hover tint. */
  --accent: var(--hover);
  --accent-foreground: var(--ink);

  --muted: color-mix(in srgb, var(--ink) 5%, var(--sleeve));
  --muted-foreground: var(--fade);

  --destructive: var(--flag);
  --destructive-foreground: var(--sleeve);

  /* 5. Borders. Default is a mid grey that boxes everything. Ours is a
        22% hairline, and the list uses rules between rows rather than
        borders around rows. */
  --border: var(--rule);
  --input: var(--rule-firm);

  /* 6. Focus. Default ring is primary-at-alpha, which is invisible-ish
        and generic. Solid ink, 2px, 2px offset. */
  --ring: var(--ink);

  /* 7. Fonts. The default is the system stack, which is precisely the
        Apple pastiche in the current PAGE string. */
  --font-sans: Archivo, "Helvetica Neue", Arial, sans-serif;
}
```

Three rules that go beyond variable values:

- **`--stamp` is not in the shadcn namespace.** It has no `--chart-N`, no `--accent`, no
  alias. Basecoat components physically cannot pick it up, so violet cannot drift into a
  badge or a tab underline by accident.
- **Strip `shadow-sm` from `.card` in the list.** Basecoat's card ships elevation. Rows are
  not elevated. Shadows are permitted on exactly three things: popovers, dialogs, and the
  sticky "new loves" pill, all of which genuinely float.
- **Override `.btn` height and radius.** Basecoat buttons are pill-adjacent; here they are
  2px radius, 40px tall on desktop, 44px on touch, 13px Archivo 600, no shadow,
  `active { scale: 0.96 }`.

---

## Live refresh over SSE

The governing rule: **an arriving track must never move content under a reading user.**
Unexpected motion is the single worst thing this interface can do to someone with ADHD or
visual snow, and a wishlist that reorders itself while you are deciding whether to buy
something is worse than one that updates on a 60s timer.

### Insertion policy

| Condition | Behaviour |
|---|---|
| Scrolled to top, nothing focused, no claim running | Row inserts. 180ms height plus opacity enter, `--ease`. No slide, no bounce. A 2px `--stamp` left edge marker holds 2s then fades over 400ms. |
| Scrolled below the fold, or any row focused, or a claim running | **Do not insert.** Increment a sticky pill under the tab bar: `3 new loves` with a `Show` action. User-triggered. |
| `prefers-reduced-motion: reduce` | No enter transition, row appears. The left edge marker holds 4s instead of 2s, because with motion removed the static cue has to carry the whole signal. |

The edge marker is the one place violet appears outside a plate, and it is the same meaning:
this row is new provenance entering the system. Motion is never the only channel here, per
the restraint rule: there is always the marker and the counter.

### Patching, not re-rendering

Existing rows are never re-rendered wholesale and the list is never re-sorted on a tick.
HTMX out-of-band swaps keyed on `#row-{id}`, and within a row the plate is its own swap
target `#plate-{id}` so a resolution result repaints 132px, not the row. Audio playback,
focus, and the open state of any row survive every update.

### Events the UI needs

Agent 5 owns the transport and the real payload shapes. The interface needs these to exist,
by whatever names that agent chooses:

| Event | Drives |
|---|---|
| `track.added` | Insertion policy above |
| `track.updated` | Plate swap, state 1 to state 2 or 3 |
| `track.removed` | Row exits, 140ms opacity plus 4px `translateY`, softer than the enter |
| `claim.progress` | Plate state 5, stage name and `n of 5`, progress rule |
| `claim.result` | Plate state 6 or 7 |
| `provider.status` | The credential-died banner |
| `heartbeat` | Drives the disconnected banner when it stops |

---

## The claim: progress, failure, refusal

A claim authenticates, enumerates purchases, matches, downloads, verifies and files. It is
slow and every stage fails differently, so a single spinner throws away the only information
the user needs. Five named stages, rendered in the plate with the row's progress rule
underneath.

```
│  ┌────┐  Lies                              ┌────────────────┐              │
│  │  ▨ │  CHVRCHES                          │ MATCHING       │  [ Cancel ]  │
│  │    │  Last.fm · 14:02                   │ 3 OF 5         │              │
│  └────┘                                    │ ▯▯▯▭           │              │
│         Matching your purchases to "Lies" by CHVRCHES                      │
│  ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
```

| Stage | Plate label | Sub-line |
|---|---|---|
| 1 | `SESSION` | Checking your Qobuz session |
| 2 | `FINDING` | Looking through your Qobuz purchases |
| 3 | `MATCHING` | Matching your purchases to "Lies" by CHVRCHES |
| 4 | `DOWNLOAD` | Downloading, 8.1 of 12.4 MB |
| 5 | `VERIFY` | Checking the file is what it claims to be |

Stage 4 is determinate. The others are indeterminate and render as a 20% segment traversing
slowly, not a barber pole. Under reduced motion the rule is static and only the byte count
updates, in `tabular-nums`.

### Success

The plate flips to state 7 over `--t-press` 220ms: opacity 0 to 1, scale 1.04 to 1, rotate 0
to `-1.5deg`, `will-change: transform, opacity` applied only for the duration. The row holds
stamped for 1.2s, then collapses to a single 28px line that persists until reload:

```
│  ✓  Lies · CHVRCHES  ·  24/96 FLAC  ·  filed in Music/CHVRCHES/         ↗ │
```

The row does not simply vanish. A 160-item list that silently loses an item gives no
confirmation that the thing you just paid for actually arrived. Under reduced motion the
plate appears already stamped, with no press and no rotation.

### Failure

The row stays, always. This is the validate-before-remove guarantee already in
`fetch_purchase` made visible: nothing leaves the queue without a confirmed success. The
plate goes `--flag` and the sub-line names the stage and the fix, in the interface's voice,
without apologising and without being vague:

| Stage | Message | Action |
|---|---|---|
| 1 | Your Qobuz session expired. Re-seed your cookies, then claim again. | `Open auth` |
| 2 | No matching purchase in your Qobuz account. If you bought this as part of an album, claim the album instead. | `Retry`, `Find album` |
| 4 | Download stopped at 8.1 of 12.4 MB. | `Retry` |
| 5 | The downloaded file is not FLAC. Nothing was added to your library. | `Retry`, `Report` |

### Refusal

The most important state in the application, and the reason it exists as a designed screen
rather than an error string. On 2026-08-02 a sibling job resolved `CHVRCHES - Lies` to
`Such Great Heights (From "Tell Me Lies Season 3")` and downloaded the wrong file. The
correct behaviour on a doubtful match is to refuse, say so, and show its work.

```
│  ┌────┐  Lies                              ┌────────────────┐              │
│  │  ▨ │  CHVRCHES                          │ MATCH REFUSED  │  [ Retry ] ✕ │
│  │    │  Last.fm · 14:02                   │ 0.42           │              │
│  └────┘                                    │ ▯▯▯▭           │              │
│                                                                            │
│    Nothing was downloaded. The closest purchase in your Qobuz account was   │
│    "Such Great Heights (From "Tell Me Lies Season 3")" by The Postal        │
│    Service. Confidence 0.42, and a claim needs 0.90.                        │
│                                                                            │
│    [ That is the right track, download it ]   [ Not it, keep waiting ]      │
│                                                                            │
```

Design requirements on this state, which are contractual rather than cosmetic:

- The rejected candidate's **full title and artist are printed verbatim**, not truncated. In the real incident, seeing the string `(From "Tell Me Lies Season 3")` is
  what makes the failure instantly legible.
- Both the achieved score and the threshold are shown, in `tabular-nums`. A bare "low
  confidence" teaches nothing.
- "Nothing was downloaded" is stated explicitly, first, before the explanation.
- The override is a **separate, explicitly worded button**, never the primary, never the
  default focus target, and never reachable by pressing Enter on the row.
- Every refusal writes a log line with the query, the candidate, the score and the
  threshold, and the UI links to it. Agent 4 owns the scoring; this view owns making a wrong
  decision diagnosable after the fact.

### The Buy button

After Buy opens the store in a new tab, the plate enters state 4 `AWAITING` and the actions
become `I bought it` (primary) plus `Cancel`, with `OPENED 14:02` on the plate's second line.
No spinner. Nothing is being awaited server side, and a spinner that spins forever is a lie
about system state that costs the user trust in every other spinner in the app.

---

## Empty, loading and error states

### Empty

Empty screens are invitations to act, and they must branch on why they are empty. Copy is
written out because generic empty-state copy makes a design feel as templated as generic
layout does.

| Condition | Copy | Action |
|---|---|---|
| No sources connected (first run) | Nothing is connected yet. Three steps. | The numbered setup panel above |
| Sources connected, queue empty | Nothing on the want list. Love a track on Last.fm, ListenBrainz or Navidrome and it lands here within a minute. | `Check sources now` |
| Filter matches nothing | No wanted tracks in 24-bit. Six are available at CD quality. | `Clear filter` |
| Owned empty | Nothing claimed yet. Buy something, claim it here, and the file lands in your library with its quality stamped on it. | none |
| Ignored empty | Nothing ignored. | none |

Note that the "sources connected, queue empty" case is a **good** state, not a failure, and
the copy should not read as an apology. It means you own everything you love, which is the
end state the entire app is aiming at.

### Loading

Static skeletons, never spinners, for the list. The skeleton is the row hairlines plus a
64px cover block and a 132px plate block at `--fade` 8%. **No shimmer.** A shimmer sweep
across 12 skeleton rows is close to worst-case for visual snow, and it buys nothing a static
block does not.

The masthead count renders as `···` rather than `0` while loading, because a `0` that flips
to `160` reads as data loss for the half second it is wrong.

### Error

| Error | Presentation |
|---|---|
| SSE dropped | Persistent bar under the masthead, 2px `--flag` top rule: `Live updates disconnected. Retrying in 8s.` Countdown in `tabular-nums`. Not a toast: toasts vanish before an ADHD reader has finished the sentence. |
| A source credential died | Persistent row in the tab bar area: `Last.fm needs reconnecting. New loves are not arriving.` plus `Reconnect`. Names the consequence, not just the fault. |
| A store credential died | Same pattern, but the affected rows' plates also drop to state 2 `NO STORE` so the list stays truthful. |
| Server unreachable entirely | The page keeps rendering the last known list, greyed at 70%, with the disconnected bar. Never a blank page. |

---

## Accessibility, and the specific constraints here

The primary user has visual snow syndrome and ADHD. These are not generic a11y bullets, they
are the reasons behind several decisions above.

**Visual snow**

- No grain, noise overlay, film texture, gradient mesh, or animated background. Anywhere.
- No shimmer skeletons, no barber-pole progress, no pulsing.
- No dashed or dotted rules. High-frequency edges vibrate. Every rule is 1px solid.
- Contrast is high but short of maximum: `--ink` on `--sleeve` is about 13:1, `--fade` on
  `--sleeve` about 5:1. Pure black on pure white is avoided deliberately.
- Large saturated fields are avoided. The violet appears only in objects under about
  132x44px.

**ADHD**

- Sticky group headers give the eye a landmark every screenful, so scroll position is never
  ambiguous in a 160-row list.
- At most four type sizes visible at once in the list.
- Nothing moves under the reader. See the SSE insertion policy.
- Errors persist until dismissed. Nothing important is communicated by something that
  disappears on a timer.
- One primary action per row. Buy is primary, claim is secondary until Buy has been pressed,
  ignore is tertiary and out of the way.

**Motion**

```css
@media (prefers-reduced-motion: reduce){
  *, *::before, *::after{
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
  /* opacity fades up to 120ms are kept: they prevent flicker rather than
     causing motion, and removing them makes state changes harder to notice */
  .fade-ok{ transition-duration: 120ms !important; }
}
```

The stamp appears instantly and unrotated. The progress rule stops traversing and only the
byte counter moves.

**Keyboard**

- Every row is a focusable list item. Focus ring is 2px `--ink` with a 2px `--stock` offset,
  never removed, never `outline: none` without a replacement.
- `Enter` on a focused row triggers the primary action for that row's state, and never the
  refusal override.
- `c` claims, `x` ignores, `/` focuses the filter, `Escape` clears it.
- Roving tabindex within the list so `Tab` does not require 480 presses to leave it.
- The audio preview is a real `button` with `aria-pressed`, not a `div` with `onclick`
  (the baseline uses a `div`, `app.py:80`).

**Mobile**

Layout above. Every hit target at least 44x44 with no overlaps, the plate becomes a
full-width strip, actions stack, and the filter collapses into the tab bar.

---

## Polish rules, applied

From the `make-interfaces-feel-better` pass, as concrete rules for whoever builds this
rather than as a review of code that does not exist yet.

| Principle | Rule for this app |
|---|---|
| Concentric radii | Cover art 4px inside a square row. Plate 2px, inner rule square. Do not "fix" the inner rule to 2px. |
| Shadows for elevation, rules for structure | Zero shadows in the list. Shadows only on popover, dialog, and the new-loves pill. |
| Tabular numbers | Mandatory on: masthead counts, sample rates, prices, download MB, confidence scores, retry countdown, all timestamps. Every one of these changes live. |
| Text wrapping | `text-wrap: balance` on the masthead and group headers, `pretty` on all empty-state and error prose. Track titles single-line ellipsis with a `title` attribute. |
| Font smoothing | `-webkit-font-smoothing: antialiased` on root. This ships to Macs first. |
| Image outlines | Cover art gets `outline: 1px solid var(--img-edge); outline-offset: -1px`. Pure black at 10% in light, pure white at 10% in dark, never a tinted neutral. Load-bearing here: a lot of cover art is near-white and dissolves into `--sleeve` without it. |
| Never `transition: all` | Always enumerate: `transition-property: opacity, background-color, border-color, transform`. |
| Interruptible animations | Hover, focus, and plate state changes are CSS transitions. Keyframes only for the stamp press and the indeterminate progress segment. |
| Icon animations | Play and pause both stay in the DOM, one absolutely positioned, cross-faded with `--ease`. Never a `visibility` toggle. Scale 0.25 to 1, opacity 0 to 1, blur 4px to 0. |
| Icon stroke weight | One set (Lucide). 1.5px beside 400 text, 2px beside 600. Never two icon libraries on one surface. |
| One SVG per icon | `currentColor` only, states from CSS color and opacity. Outline is default, fill marks active. |
| Scale on press | `active { scale: 0.96 }` on buttons. Exactly 0.96. Never on the row itself. |
| Hit areas | 44x44 touch, 40x40 dense desktop, extended by pseudo-element where the glyph is smaller. The `✕` and the Buy button get an 8px gap so their hit areas cannot overlap. |
| Motion restraint | No custom animation on hover, filter typing, tab switching, or preview play. Those are high frequency and the attention cost repeats. |
| Skip animation on load | No entrance animation on first paint. 160 rows animating in is unusable. |
| `will-change` | Only on the stamp, only during its 220ms press, removed after. |
| Exit softer than enter | Row removal is 140ms opacity plus 4px `translateY`, against a 180ms enter. |

---

## Implementation notes

- **Fonts are vendored**, subset to latin, served from `/static/fonts/` with
  `font-display: swap` and a `<link rel="preload">` for the two files actually used above
  the fold. No external font CDN: the app may run with no outbound internet, and a LAN app
  should not announce itself to Google on every page load.
- **Tailwind standalone CLI at build time**, per the locked stack. The token block above
  lives in `@theme` / `:root` in the input CSS; Basecoat's stylesheet is imported after it so
  the overrides win without `!important`.
- **The plate is one Jinja macro** taking `(state, tier, format, store, price, date, note)`
  and rendering all seven states. One macro, one place to change, no divergence between the
  wanted view, the owned view and the mobile strip.
- **Rows are `#row-{id}` and plates are `#plate-{id}`**, both HTMX oob-swap targets, so a
  resolution result repaints 132px rather than the row and never disturbs focus or audio.
- **Alpine holds only per-row ephemeral state** (which store is selected, whether the row is
  expanded). Everything durable comes from the server.
- The current `PAGE` string in `app.py` is replaced entirely by Jinja templates. None of it
  survives except the API shapes.

---

## Open questions / risks

1. **Where does the promised tier come from before purchase?** The `UP TO 24/96` claim on
   the wanted plate is the design's central conceit, and it depends on the store provider
   returning available formats at search time. Qobuz's product pages list them; Bandcamp
   generally does not commit to a format until download. If Bandcamp cannot supply this, the
   plate needs an eighth state (`TIER UNKNOWN`, ladder blank) rather than guessing, and the
   scan-the-column-for-hi-res behaviour degrades for Bandcamp-only rows. **Question for
   Agent 2:** can each store provider return a `promised_tier` at resolve time, even coarsely?
2. **Who owns the confidence threshold shown in the refusal state?** The mock says
   "needs 0.90". Agent 4 sets the real number and may want it per-store or per-source. The UI
   requires it to be a single number it can print, and requires the score and threshold to
   arrive together in `claim.result`.
3. **The rotated stamp may still read as kitsch on a real screen.** It cannot be settled on
   paper. Build it at `-1.5deg` and at `0deg` with a doubled inner rule, look at both, keep
   one. Documented so the fallback is not a regression.
4. **Sticky group headers plus a 160-row list plus HTMX oob swaps** is the one place this
   design could get technically awkward: inserting a row into a group whose header is
   currently stuck. Likely fine with `position: sticky`, but worth a spike before committing
   to grouping.
5. **Aniline violet is a taste bet.** It is defensible (stamp ink, provenance, uncommon in
   the category) but it is the piece most likely to be wrong for this particular user. It is
   isolated to `--stamp` and appears in exactly three places (plate rule and text, the new
   row edge marker, the stamp), so swapping it is a one-value change. That isolation is
   deliberate insurance.
6. **No objection to any locked decision in section 4.** Basecoat plus Tailwind plus HTMX
   plus Alpine supports everything described here. The only thing worth flagging is that
   Basecoat's defaults are strongly opinionated toward the shadcn look, so the override block
   above is not optional polish, it is load-bearing. Skipping it produces exactly the
   Tailwind admin panel the brief asked to avoid.
7. **Not designed here, deliberately:** the settings and provider-connection screens beyond
   the first-run panel (they depend on Agent 3's auth flows, especially the OAuth redirect
   decision), and the album-versus-track claim flow implied by the stage-2 failure message.
