# Nocturne — Design System v1.0.0
**Dark-first visual language for the chat app.** Sleek, calm, premium, fast.
This document is the contract: build UI from these tokens, don't invent new colors/sizes.

> Files in this system
> | File | Purpose |
> |---|---|
> | `DESIGN-SYSTEM.md` | This spec — rationale, rules, component recipes |
> | `nocturne.tokens.json` | Machine-readable source of truth (W3C design-token shape) |
> | `nocturne.css` | Drop-in CSS custom properties + base components (`.btn`, `.bubble`, `.card`, …) |
> | `tailwind.config.js` | Tailwind v3 preset mapped 1:1 to the tokens |
> | `nocturne-preview.html` | Self-contained living preview: open in a browser to verify |

---

## 1. Principles

1. **Dark by design, not inverted.** Surfaces are deep blue-tinted charcoals (`#08090D → #1B1E2A`), never pure black. Light is implied by *lighter* surfaces plus a 1px inner top highlight.
2. **Elevation = lightness + shadow.** Raise a component by moving it up the surface ramp (`raised-1 → raised-4`) and adding shadow — never by adding a white border.
3. **One loud accent per view.** `brand-500 #7C5CFF` is the only saturated fill competing for attention. Cyan/magenta are *seasoning* (read receipts, reactions, data viz), not fills.
4. **Content first, chrome dissolves.** Panels are flat and quiet; bubbles, avatars and presence are where contrast and glow live.
5. **Tactile, fast, quiet motion.** 140–200 ms, spring for things that "pop", no animation longer than 480 ms. Everything collapses under `prefers-reduced-motion`.
6. **Contrast is not negotiable.** Body text ≥ 4.5:1 on the surface it sits on. `text-400` is for meta, never prose.

---

## 2. Color

### 2.1 Surface ramp (backgrounds)
| Token | Hex | Use |
|---|---|---|
| `sunken` | `#05060A` | App chrome behind panels, scroll wells |
| `base` | `#08090D` | **App canvas** — window background |
| `raised-1` | `#0E1017` | Sidebar, header, composer bar, cards |
| `raised-2` | `#14161F` | Incoming bubble, hovered card, popover body |
| `raised-3` | `#1B1E2A` | Dropdown, modal, hovered list row |
| `raised-4` | `#2B303C` | Tooltip, dragged item, pressed row |
| `glass` | `rgba(20,22,31,.72)` + `blur(24px) saturate(140%)` | Sticky header & composer |
| `overlay` | `rgba(5,6,10,.72)` + `blur(2px)` | Modal scrim |

### 2.2 Text
| Token | Hex | Contrast on `base` | Use |
|---|---|---|---|
| `text-primary` | `#F4F6FA` | 17.1:1 | Message text, headings, sender names |
| `text-secondary` | `#A8AFC0` | 8.4:1 | Chat previews, labels, timestamps |
| `text-tertiary` | `#7E8798` | 4.9:1 | Placeholders, idle icons, meta |
| `text-disabled` | `#5C6474` | — | Non-interactive only |
| `text-brand` | `#A794FF` | 6.6:1 | Links, @mentions, unread counts |
| `text-on-brand` | `#0B0714` | 12:1 on brand | Text/icons on gradient or brand fills |

### 2.3 Brand & accents
| Token | Hex | Use |
|---|---|---|
| `brand-500` | `#7C5CFF` | **Primary**: buttons, active nav, focus ring, own bubble |
| `brand-400` | `#8F75FF` | Hover / light text |
| `brand-600` | `#6A48F0` | Pressed |
| `brand-800` | `#3D2596` | Deep end of gradients |
| `accent-cyan` | `#22D3EE` | Read receipts, gradient end, data viz |
| `accent-magenta` | `#F472B6` | Reactions, mentions, delight moments |
| `accent-lime` | `#A3E635` | Rare: active voice waveform |

### 2.4 Semantic & presence
`success #34D399` · `warning #FBBF24` · `danger #FB7185` · `info #38BDF8`
Presence dots: `online #34D399` · `away #FBBF24` · `busy #FB7185` · `offline #5C6474`
Presence is always an avatar-attached 10px dot with a 2px `base` ring — never a color-only indicator (pair with text on hover/labels).

### 2.5 Borders (all translucent white, never grey hex)
`subtle rgba(255,255,255,.06)` · `default .10` · `strong .16` · `brand rgba(124,92,255,.55)`
Dividers are **always 1px**. 2px is reserved for tab underlines and focus.

---

## 3. Gradients

| Token | Value | Use |
|---|---|---|
| `aurora` | `linear-gradient(135deg,#7C5CFF,#22D3EE)` | Avatar fallbacks, unread badges, onboarding art |
| `brand` | `linear-gradient(135deg,#8F75FF,#6A48F0)` | Primary buttons (with `glow-brand`) |
| `brand-deep` | `linear-gradient(160deg,#6A48F0,#3D2596)` | Large brand surfaces, pressed hero |
| `own-bubble` | `linear-gradient(160deg,#7C5CFF,#6244E8)` | **Outgoing message bubble** |
| `text` | `linear-gradient(92deg,#FFF,#A8AFC0)` | Display headings ≥24px only |
| `text-brand` | `linear-gradient(92deg,#A794FF,#22D3EE)` | Wordmark, upgrade CTA |
| `glow-top` | `radial-gradient(120% 80% at 50% -20%, rgba(124,92,255,.28), transparent 60%)` | Ambient light behind header/modals |
| `glow-corner` | `radial-gradient(60% 60% at 100% 0%, rgba(34,211,238,.16), transparent 70%)` | Secondary corner warmth |
| `mesh` | 3 radial blobs (violet 20%, cyan 14%, magenta 12%) | Empty states, auth screens |
| `shimmer` | transparent → `rgba(255,255,255,.06)` → transparent | Skeleton sweep, 1.4s linear infinite |
| `scrim` | base-opaque fade | Fade under sticky header/footer |

**Rules:** gradients run 135° (fills) / 160° (bubbles) / 92° (text). Max **two** gradient-bearing elements per message row. Never place `text-primary` grey-on-gradient — on brand fills use `text-on-brand`.

---

## 4. Typography

**Inter** (UI + body, variable) · **Inter Tight** (≥24px display) · **Space Grotesk** (wordmark) · **JetBrains Mono** (code, invite codes). Fallback stack always ends in `sans-serif`.

| Token | Size / LH | Weight | Use |
|---|---|---|---|
| `micro` | 11 / 16, `+0.06em` caps | 500 | Caps labels, compact timestamps |
| `caption` | 12 / 16 | 500 | Timestamps, read receipts, meta |
| `body-sm` | 13 / 20 | 400 | Sidebar previews, dense settings |
| `body` | **15 / 24** | 400 | **Default** UI text & messages |
| `body-strong` | 15 / 24 | 600 | Sender names, row titles |
| `title-md` | 17 / 24 | 600 | Chat header name, modal title |
| `title-lg` | 20 / 28 | 600 | Panel titles, empty-state headline |
| `display-sm…xl` | 24 → 48, `-0.02em` | 600 | Onboarding & marketing heroes |

**Rules:** measure 720px for the message column, 64ch for prose. Never <13px for interactive text. `tabular-nums` for every timestamp/counter/receipt. Two weights per screen (400 + 600); 700 is wordmark-only.

---

## 5. Space, radius, shadow, blur, motion

- **Space**: 4px base — `4 8 12 16 20 24 32 40 48 64`. Gutter 16 · panel padding 20 · section gap 24 · bubble gap 8 (2 when grouped).
- **Radius**: xs 6 · sm 10 · md 14 · lg 18 · xl 24 · 2xl 32 · full. Bubbles 18 with the sender-side corner collapsed to **6**.
- **Shadow**: `sm 0 2px 8px rgba(0,0,0,.36)` → `xl 0 32px 72px rgba(0,0,0,.60)`; `glow-brand 0 8px 28px rgba(124,92,255,.36)`; `inner-top inset 0 1px 0 rgba(255,255,255,.08)`. **Recipe:** `shadow.md + innerTop + 1px border` ≈ every elevated surface in dark mode.
- **Blur**: `sm 8` · `md 16` · `lg 24 + saturate(140%)` (glass) · `xl 48 + saturate(160%)` (ambient blobs).
- **Motion**: 80 / 140 / 200 / 320 / 480 ms. Easings: `standard cubic-bezier(.2,0,0,1)`, `enter (0,0,.2,1)`, `exit (.4,0,1,1)`, `spring (.34,1.56,.64,1)`.
  - message in: fade + `translateY(6px)`→0, 200 ms `enter`
  - send / reaction: `scale(.96)`→1 fade, 140 ms `spring`
  - press: `scale(.97)`, 80 ms · hover lift: `translateY(-1px)` + shadow up, 140 ms
  - typing dots: 1.1 s bounce, 160 ms stagger · skeleton: 1.4 s sweep
  - `prefers-reduced-motion`: keep opacity, drop transforms.

---

## 6. Layout

```
┌ rail 72 ┐┌ sidebar 320 ─────┐┌ header 64 (glass) ──────────────┐
│  icon   ││ search           ││ avatar · name · presence · ⚙    │
│  stack  ││ ───────────────  │├─────────────────────────────────┤
│         ││ chat list item   ││  messages, max 720px column     │
│         ││ 68px, r-md      ││  incoming: raised-2, r 18/18/18/6│
│         ││ active: raised-3 ││  outgoing: gradient, r 18/18/6/18│
│         ││ + 2px brand bar  ││  8px gap, 2px when grouped      │
│         ││ unread: aurora   │├─────────────────────────────────┤
│  avatar ││ badge pill       ││ composer 56 (glass)             │
└─────────┘└──────────────────┘└─────────────────────────────────┘
```
Mobile: rail + sidebar collapse to a hamburger drawer (`shadow.lg`, 240 ms slide); composer becomes `sticky bottom` with safe-area padding; message column → 100% − 32px.

---

## 7. Component specs

**Buttons** — heights 32/38/44, radius `sm`, 600 weight, `body-sm`.
`primary`: `gradient.brand` + `glow-brand` + inner-top; hover `brightness(1.08)` + `translateY(-1px)`; active `scale(.98)`.
`secondary`: `raised-3` + `border.default`; hover `raised-4`. `ghost`: transparent → `raised-2`. `danger`: `danger` fill, dark text `#2A0710`. Icon-only = 32/38px circle, ghost.

**Message bubble** — padding 10×14, max `min(72ch, 68%)`, radius 18 with sender-side corner 6.
Incoming `raised-2` + `border.subtle`. Outgoing `gradient.own-bubble` + `shadow.sm` + inner-top.
Meta: `caption`/`text-tertiary`, bottom-aligned, 6px gap. Same sender within 5 min → 2px gap, only last bubble keeps the tail.

**Composer** — `glass` + `blur(24px)`, 56px; field `raised-2`, radius `md`, min 44 / max 200px then scroll, focus `border.brand` + `shadow.focus`. Actions: attach + emoji (ghost), **send** = 36px circle, `gradient.brand` + `glow-brand`, appears when input non-empty. Enter sends, Shift+Enter newline.

**Avatar** — 20/28/36/48/96, full radius, `gradient.aurora` + initials (700, white) as fallback, 2px `base` ring when stacked, 10px presence dot bottom-right.

**Chat list item** — 68px tall, `radius.md`, rest transparent → hover `raised-2` → active `raised-3` + 2px brand left bar. Unread: title 600, preview promoted to `text-primary`, `aurora` badge pill. Muted: dot instead of badge.

**Input** — 38/44px, `raised-2`, `border.default`, focus brand border + ring, error = danger border + `caption` danger helper. Label `caption/500 text-secondary`, 6px gap.

**Modal** — `raised-3`, `radius.xl`, `shadow.xl`, 24px padding, widths 400/560/760, scrim + blur(2px), enter = fade + `scale(.98)`→1, 200 ms.

**Tooltip** — `raised-4`, `caption`, 6×10 padding, `radius.xs`, 400 ms delay in / 0 out.
**Toast** — `raised-3` + 3px semantic left bar, `shadow.lg`, 4 s, slide-up 12px + fade 240 ms.
**Reaction pill** — `raised-2`, full radius, `caption`, 3×10 padding; active `rgba(124,92,255,.16)` + brand border + `pop-in`.
**Typing indicator** — `raised-2` bubble, three 6px `text-tertiary` dots, 160 ms stagger.
**Read receipts** — 16px, 1.5px stroke: sent ✓ svg-tertiary, delivered ✓✓ tertiary, **read ✓✓ cyan**.
**Scrollbar** — 10px, thumb `rgba(255,255,255,.14)` full radius w/ 2px inset, hover `.22`, auto-hide after 1 s idle.
**Skeleton** — `raised-2` + `shimmer` sweep 1.4 s.
**Empty state** — `mesh` blob 320px `blur(80px)`, `title-lg`, `body-sm secondary` max 42ch, primary CTA.

**Icons** — outline, 1.5px stroke, round caps/joins, 16/20(default)/24, Lucide (MIT) recommended.

---

## 8. Accessibility

- Body text ≥ 4.5:1, large text ≥ 3:1, interactive borders ≥ 3:1 — verified in the color tables above.
- Focus is always visible: `0 0 0 2px #08090D, 0 0 0 4px rgba(124,92,255,.75)`. Never `outline:none` without a replacement.
- Hit areas ≥ 32px (44px on mobile primaries) — grow with padding, not visible size.
- Never encode state with color alone: presence, unread, error all get icon/weight/text reinforcement.
- Honor `prefers-reduced-motion` (transforms off, opacity fades kept).

**Don't:** pure `#000` bg with pure `#FFF` text · grey below `#7E8798` for body copy · glow shadows on more than two elements per view · two competing gradient fills in one message row.

---

## 9. Usage snippets

```html
<!-- install -->
<link rel="stylesheet" href="nocturne.css">
<!-- Inter / Inter Tight / Space Grotesk / JetBrains Mono via Google Fonts -->
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Inter+Tight:wght@600&family=JetBrains+Mono:wght@400;500&family=Space+Grotesk:wght@600&display=swap" rel="stylesheet">
```

```html
<button class="btn btn--primary">Send message</button>
<div class="bubble bubble--out">Okay, shipping the dark theme tonight 🚀</div>
```

```js
// tokens in JS/TS (no build step)
const { color, gradient, typography } = require('./nocturne.tokens.json');
```

```js
// Tailwind
module.exports = { presets: [require('./tailwind.config.js')], content: ['./src/**/*.{tsx,jsx,html}'] };
// then: bg-base text-content-primary shadow-elevated bg-own-bubble font-display text-display-sm
```

**Name:** *Nocturne* — the palette is named after night light: violet aurora over deep charcoal.
