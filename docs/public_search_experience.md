# ArtSearch Public Search Experience

Status: alpha product and visual specification

## Design Thesis

ArtSearch should feel like a well-preserved late-1990s creative workstation,
not a themed arcade cabinet and not an AI marketing page. The interface is
compact, legible, quick, and image-led. Retrofuture references appear in
proportion, typography, state changes, and control geometry rather than in
scanlines, star fields, fake terminals, or neon gradients.

The first screen is the working search product. There is no landing-page hero.

## Information Architecture

| Public route | Responsibility |
| --- | --- |
| `/search` | Text/reference search, filters, result grid, and result inspector |
| `/compare` | Positive/negative exemplars and side-by-side refinement |
| `/saved` | User collections and saved artworks |
| `/artists` | Artist browsing, attribution, and source links |
| `/account` | Invite identity, privacy, exports, and deletion controls |

Operations remain a separate authenticated product under `/ops`. Intake,
review, model metrics, raw ranking evidence, and provider health never appear as
casual public navigation tabs.

## Desktop Workbench

```text
+--------------------------------------------------------------------------------+
| ARTSEARCH / SIGNAL DESK       SEARCH  COMPARE  SAVED  ARTISTS      [ALEX  02] |
+--------------------------------------------------------------------------------+
| QUERY INPUT                          | RESULTS  01-40       MODE: FUSED         |
| +----------------------------------+ | +-------+ +-------+ +-------+ +-------+ |
| | drop reference or paste image   | | |       | |       | |       | |       | |
| |                                  | | |  art  | |  art  | |  art  | |  art  | |
| +----------------------------------+ | |       | |       | |       | |       | |
| [ describe subject, mood, medium ]  | +-------+ +-------+ +-------+ +-------+ |
|                                      | +-------+ +-------+ +-------+ +-------+ |
| SIGNAL MIX                           | |       | |       | |       | |       | |
| SUBJECT  [=====-----] 0.55           | |  art  | |  art  | |  art  | |  art  | |
| VISUAL   [=======---] 0.72           | |       | |       | |       | |       | |
| DETAIL   [===-------] 0.34           | +-------+ +-------+ +-------+ +-------+ |
|                                      |                                             |
| FILTERS                              | SELECTED / 08                               |
| artist [any]  media [any]            | artist, source, save, like, dislike         |
| [x] diverse artists  [x] reviewed    | subject .81  visual .74  detail .52         |
|                                      | "why this result" evidence for evaluators  |
| [ SEARCH ]  [ RESET ]                |                                             |
+--------------------------------------------------------------------------------+
| QUERY 8F21  INDEX 0042  312 ms      40 results                  feedback saved  |
+--------------------------------------------------------------------------------+
```

The left rail is a tool panel, not a card. The result grid uses stable tracks
and bounded aspect-ratio boxes so images never shift while loading. Selecting a
result opens an inline or right-edge inspector without covering neighboring
results. Advanced model evidence appears only for authorized evaluators.

## Mobile Search

```text
+----------------------------------+
| ARTSEARCH             [02] [=]  |
+----------------------------------+
| [ reference image / upload ]     |
| [ describe what you want      ]  |
| [ SEARCH ]       [ FILTERS 3 ]   |
+----------------------------------+
| FUSED / 40 RESULTS               |
| +-------------+ +-------------+  |
| |             | |             |  |
| |     art     | |     art     |  |
| +-------------+ +-------------+  |
| +-------------+ +-------------+  |
| |     art     | |     art     |  |
| +-------------+ +-------------+  |
+----------------------------------+
| SEARCH  COMPARE  SAVED  ACCOUNT  |
+----------------------------------+
```

Signal weights and filters move into a bottom sheet. Mobile keeps search,
selection, save, and feedback complete; operations and dense diagnostics remain
desktop-first.

## Visual System

### Color

```text
paper          #f2f3ef   application background
surface        #ffffff   controls and inspectors
graphite       #171b1d   primary text
steel          #687178   secondary text
rule           #c6ccd0   ordinary 1px boundaries
cobalt         #1557d6   active selection and primary command
cyan           #00a6b2   query/visual signal marker
signal green   #15805a   saved, healthy, accepted
amber          #c57916   pending and review state
red            #b43a35   destructive or failed state only
```

No large gradients are part of the visual language. Dark mode, when added,
should be charcoal rather than navy and must preserve artwork color accuracy.

### Typography and Geometry

- IBM Plex Sans or an equivalent neutral grotesk for interface text.
- IBM Plex Mono for IDs, ranks, versions, scores, and timing.
- A custom or licensed geometric wordmark may reference Eurostile/Microgramma;
  it is not used for paragraphs or controls.
- Body text is 14-16 px; compact metadata is never below 11 px.
- Corners are 0, 4, or 6 px. Cards do not nest.
- Controls use one-pixel graphite/rule borders, visible focus rings, and clear
  pressed states. Shadows are rare and no blur is used as structural contrast.

### Interaction Character

- Segmented controls and tab strips can feel like physical instrument panels,
  but use familiar icons and labels.
- A small status strip may show query ID, index version, result count, and total
  latency. It must never imply certainty or intelligence.
- Loading uses reserved image boxes and compact progress states, not a full-page
  spectacle.
- Empty, offline, rate-limited, model-unavailable, and no-result states explain
  the actual condition and preserve the user query.

## Alpha Feedback

Every impression can generate click, like, dislike, save, follow-artist, hide,
and reformulation events. Feedback controls remain quiet until selection or
hover; they do not cover artwork. Users can inspect and delete their stored
query history from the account surface.

The evaluator view may reveal component scores, stage ranks, model/index
versions, and latency breakdown. Ordinary users see a concise explanation such
as subject, visual structure, or detail match without raw model internals.

## Anti-Patterns

- No giant AI claim, chatbot greeting, or prompt-engineering instructions.
- No purple/blue glow, floating orb, glass panel stack, chrome robot, or stock
  generative imagery.
- No fake DOS commands, CRT distortion, scanline overlay, or pixel font for body
  copy.
- No masonry layout that jumps during image loading.
- No unexplained sliders, unlabeled icon-only modes, or model names as the main
  user vocabulary.
- No operations dashboard embedded inside the public search experience.
