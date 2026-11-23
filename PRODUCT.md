# Product

## Platform

web

## Users

One collector, buying graded and raw trading cards (Final Fantasy and other Magic sets, Union
Arena, Dragon Ball, Pokémon). The main moment is a phone: an alert or the morning summary email
arrives, one tap opens the deal, and the question is whether to buy, offer or let it go. Setting
up wants, ceilings and exports happens at a desk.

## Product Purpose

Sleeper sweeps eBay for every card on the wants list several times a day, lands each listing at
the forwarding address, rates it against sold comps and the ceiling set for the card, and says
which ones are worth buying. Success: no underpriced copy of a wanted card goes unseen, and every
deal shows why it is one.

## Capabilities and Constraints

- Pages: Deals, Ended, Wants and one want, Add card, Export, Summary, Runs.
- Server-rendered FastAPI and Jinja, one CSS file, one small script for search, the theme and the
  price slider; every page works without it.
- Sleeper never buys on its own: a purchase, an offer or a Gixen snipe starts from the collector's
  own tap.

## Product Principles

1. The price shown is the landed price, never the sticker.
2. Every deal says why: its comps, its ceiling and how the art matched.
3. Say plainly when a sweep failed or a price is unknown.
