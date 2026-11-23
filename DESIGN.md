# Design

A grading desk: cards sit in slab-like frames with a label band, as on a PSA holder, and the
interface stays quiet around the card art. Light is toploader grey, dark is binder navy. The one
loud thing is the foil edge on a steal. `sleeper/static/app.css` is the source of truth for values.

## Palette

| Token     | Light     | Dark      | Job                                                           |
| --------- | --------- | --------- | ------------------------------------------------------------- |
| `bg`      | `#eef0f2` | `#0f1620` | Page ground                                                   |
| `panel`   | `#ffffff` | `#172231` | Tiles, menus, the phone's tab bar                             |
| `well`    | `#e3e7eb` | `#0c121a` | Segmented controls, the art well behind a card                |
| `line`    | `#d5dae0` | `#263345` | Hairlines and tile edges, 1px                                 |
| `ink`     | `#15202b` | `#e6ebf1` | Text                                                          |
| `muted`   | `#525d69` | `#93a1b1` | Secondary text and labels                                     |
| `accent`  | `#0a6870` | `#3fb5bf` | Links, the primary button, the current tab's icon, focus ring |
| `great`   | `#14703d` | `#45c27a` | A great deal                                                  |
| `good`    | `#36702a` | `#8cc56b` | A good deal                                                   |
| `fair`    | `#835800` | `#e0a63a` | A fair price; warnings                                        |
| `high`    | `#b02e18` | `#f0705a` | Over the ceiling; danger                                      |
| `foil`    | conic     | conic     | The edge of a steal and the moon in the mark, nothing else    |

The deal colours are states, never decoration.

## Type

Archivo (bundled variable font, OFL), one family. Display headings use the wide cut at 900 with
tight tracking, like the type on a slab label; body at 16px; tabular numerals for prices and counts.

## Components

- **Mark**: a slab drawn in outline, its label band with a name line and a grade box, and a foil
  crescent moon in the card window: the sleeper, a card nobody has noticed yet. It sits beside the
  name (`mark.svg`) and is the app icon on navy (`icon.svg`, the PNGs drawn from it). The manifest
  lets a phone add sleeper to its home screen.
- **Header**: the mark and name, the pages, then Search (Cmd/Ctrl-K) and the theme switch as an
  icon button. From 601px the pages are words in the bar, the current one underlined.
- **Phone tab bar**: at 600px and below the pages move to a bar fixed to the foot: Deals, Ended,
  Wants, Add card, and More, which opens Export, Summary and Runs above it. The current tab's
  icon takes the accent.
- **Filters**: segmented controls of plain links, so every filter works without script. On a
  phone only Deals/All live and Graded/Raw show; buying format, sort, grouping and set fold behind
  a More filters row, which stays open and counts what is on whenever one of them is set.
- **Slab tile**: the art in a well, then the label band with name and set on the left and the
  landed price in the grade box on the right, the deal word under the price.
- **Market meter**: where the landed price sits against market, red to green.

## Rules

- Radius 14px on tiles, 8px inside them, pills on controls.
- Icons are Lucide (ISC, `static/icons/LICENSE`), drawn as CSS masks so they take the text colour.
- Motion is hover, press and the tile lift, 90 to 200ms, and off under reduced motion.
- Every control is at least 40px tall, 44px or more on a phone.
- Nothing loads from elsewhere: font, icons and the mark come from this app.
