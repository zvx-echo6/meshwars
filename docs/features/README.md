---
title: Feature Design Notes
status: index
---

# Feature Design Notes

This folder holds design notes for game features: why a thing works the way it does, not how the code is laid out. That's what `docs/meshcore/` is for — this is the reasoning behind decisions, including the ones that were tried and reversed. Each note states plainly whether it is shipped or unbuilt, because a design note that reads as a promise is worse than none.

| Note | What it covers | Status |
|---|---|---|
| [The Long Season](long-season.md) | Why seasons went from 30 days to 180, monthly results, check-in streaks, and the honors awarded each month | Shipped (August 2026) |
| [Meshtastic Node Keys](meshtastic-node-keys.md) | Why node IDs stopped being stable identities under Meshtastic 2.8, and how key drift is tracked without changing how scoring attributes traffic | Shipped (August 2026) |
| [Places Worth Going](places.md) | Scoring named destinations — summits, parks, landmarks — instead of treating every map square the same | Shipped (August 2026) |
| [Board Colors](board-colors.md) | Why purple/pink and orange/yellow are hard to tell apart on the map, and the two hex values, fill opacity and outline changes that fix it | Proposed, not built |
