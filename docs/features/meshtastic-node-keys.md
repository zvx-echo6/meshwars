---
title: Meshtastic Node Keys
status: shipped (August 2026)
---

# Meshtastic Node Keys

## The problem

Meshtastic 2.8 derives a node's ID from its key material rather than from hardware. An ID is therefore no longer a stable identity: it can change under a node (a key regeneration, a factory reset), and two nodes can collide on one. The public key is the stable thing.

## The constraint that shapes everything

Position packets — which is what scoring reads — carry only a node ID. Only NodeInfo packets (portnum 4) carry the public key. So attribution cannot key on the public key, and does not: it still looks up the node ID exactly as before.

## What was built, in two parts

**First**, a table `mt_node_key` accumulating node-ID-to-public-key pairs passively from NodeInfo packets. Its primary key is the PAIR, not the node alone — keying on the node would overwrite the old row the moment a key changed, destroying exactly the evidence of drift the table exists to capture. It cannot be backfilled, because a key is only learned when a node broadcasts NodeInfo and that reaches an MQTT feeder, which is why it had to start filling before anything could depend on it.

**Second**, a `public_key` column on `player_node`, captured at registration. It is optional, and filled in automatically from the accumulated map when the node is already known, so nobody types 64 characters unless they have to. When two different keys are on record for one node, none is stored — that is the drift case, and picking one would be inventing an answer.

## Why the key is metadata rather than the lookup

Scoring stays exactly as it was. The key rides alongside so that when a changed ID eventually arrives carrying a key already on file, the binding can be re-pointed without the player doing anything.

## Two counters worth watching

- A node with two keys on record means an ID changed under someone.
- One key on two nodes means a collision.

Both read zero at the time of writing.

## A related fix shipped alongside

The Meshtastic roster listed a player once per radio, so someone with ten radios appeared ten times — 19 entries for 6 actual players. The `/teams` route joined players to radios and returned a row per radio; it now returns one per player. The MeshCore roster never had the bug.
