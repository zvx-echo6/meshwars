---
title: Places Worth Going
status: not built — design note only
---

# Places Worth Going

This is unbuilt: no schema, no code, no deploy. What follows is the design conversation written down so it does not have to be had again.

## The idea

Every square is currently worth the same; the map is graph paper with no features. Named places — summits, parks, landmarks — are destinations worth more than the cell they sit in. None of the lists need inventing: Summits on the Air and Parks on the Air are curated ham-radio programmes, and landmarks come from OpenStreetMap.

## Values and the cap

| Reference type | Source | Points |
|---|---|---|
| Landmark | OpenStreetMap | 5 |
| Park | POTA | 25 |
| Summit | SOTA | 100 |

A park is deliberately worth exactly one check-in — that is the anchor. Twenty landmarks, four parks, or one summit all reach the same weekly ceiling of 100 points, so the choice is how you would rather spend the week rather than which nets more.

## Rules

- One credit per reference, per person, per week.
- 100 points per person per week, whatever the mix.
- The week resets Wednesday just before the net, so the game has one clock — the gathering and the reset are the same moment.
- Activating requires a scoring ping; being there is not enough, exactly as it is not enough anywhere else.
- Points go to a personal Explorer Score AND to the team total, the same shape check-ins already have.
- Aircraft are excluded.

## Why per person rather than per team

It matches the existing rule that the first new person to paint a square earns extra, because more people beats one person going back and forth.

## Why the weekly cap does the work

- It keeps check-ins relevant: a full week of places is four check-ins, not ten.
- It stops landmark density deciding anything, because a city grinder and a mountaineer reach the same ceiling.
- It keeps the summit attractive, since one trip beats twenty town halls.

## Seeding is the real balance knob, not the point values

A narrow OpenStreetMap tag list, not every point of interest.

**In:** town hall, courthouse, museum, library, fire station, post office, historic marker, monument, viewpoint, trailhead, visitor centre.

**Out:** schools, hospitals, churches, playgrounds, anything on private land, anything you would not tell a stranger to drive to at night.

The test is permanent, publicly accessible, distinctive. Narrow is the recoverable direction — adding tags later gives people new places, pruning later takes credits from people who already earned them.

## Seed first, submissions maybe never

The lesson from Ingress and Pokémon Go is that Niantic seeded from existing databases first and opened player submission years later, with players reviewing each other. User submission makes the operator the referee: someone submits their own driveway, or a real place on private land, and the game starts telling people to trespass.

## What it changes about the honors

Explorer becomes most Explorer points that month, instead of most squares nobody had ever claimed. Frontier keeps counting squares beyond city limits but drops its virgin-ground restriction, which only existed so Frontier would be a strict subset of Explorer. The two then measure different things: Frontier counts ground out past the towns, Explorer counts destinations reached.

## Considered and dropped

- An "infrastructure" award for distinct repeaters personally heard.
- Keeping Explorer points out of the team total entirely.
- Making the check-in a multiplier on the week's places — rejected because a bonus for showing up is a penalty for not, and the person out of signal on Wednesday is the one who played hardest.
- Scaling park credit by park size — rejected because SOTA and POTA themselves do not; a pocket park and a wilderness both count as one activation.
