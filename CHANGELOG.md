# Changelog

Notable, player-facing changes. Plain language, newest first.

## 2026-09-05 — Sign in to MeshWars

**You can sign in now, instead of only holding on to an API key.** From
the account page, connect GitHub, Google, Discord, a magic-link email,
or a password — any one of them gets you in, and you can connect more
than one to the same account, so losing access to one doesn't lock you
out as long as another still works. Signing in links to exactly one
player. If you never want an account, the original flow — join with an
invite code, get an API key, paste it in when you need to check
anything — still works exactly as it always has.

**Signing in turns the account page into your whole player dashboard.**
Your radios (add or remove any time), a full setup-check and
diagnostics without pasting a key, your stats, your check-in history,
your honors, your team, and a Security section covering your sign-in
methods, your password, your contact email, your API key, and every
device you're currently signed in on, with sign-out available for one
device or all of them at once.

**Joining from inside the account page skips the invite code.** The
invite-code join page is still there for anyone new. If you're already
signed in, you join from the account page instead — straight into
name, team, and radio type, no code needed, since being signed in
already clears a higher bar than the code does.

**If you join as Meshtastic while signed in, you no longer get an API
key.** Nothing on the Meshtastic side — FreqMapper, check-ins — has
ever used one, so a signed-in Meshtastic join now mints nothing to
lose or misplace. The original anonymous join is unchanged and still
issues one, since it's the only way an anonymous player can ever come
back and claim their own player later. Either way, you can generate a
key later from Security if you want one for something else.

## 2026-09-05 — Confirm your node directly

**A new way to prove a radio is yours, instead of waiting for it to
line up on its own.** Net check-ins are normally matched automatically
— by your registered node ID on Meshtastic, or by your registered
contact's name on MeshCore — but that breaks the moment a radio's name
or identity drifts from what MeshWars has on file. "Confirm my node,"
on the account page, proves a specific radio is yours directly instead
of waiting on that.

**MeshCore proves it with an advert.** Type the name your radio is
currently posting under, press Confirm my node, then trigger an advert
from that same radio — a long-press of the side button, or "Send
Advert"/"Flood Advert," on most devices. MeshWars watches the mesh for
five minutes and shows you every node it heard advertising under that
name; pick yours, and its public key is bound to your account, so a
later name change on the radio won't break it again.

**Meshtastic proves it with a one-time code.** No name to type — select
Meshtastic and press Confirm my node, and you're handed a short code
like `mw-3h7fpk4`. Send that exact text as a message on any channel on
your mesh (it's fine buried in a longer sentence); MeshWars watches for
it for five minutes and shows you which node it came from. Confirm
it's yours, and that node ID is bound to your account.

Either way, a radio already bound to someone else's account can't be
claimed this way.

## 2026-09-05 — Two-factor authentication, and deleting your own account

**Two-factor authentication is optional, and lives in Security on the
account page.** It adds an authenticator-app code on top of your
password or magic-link email — the two sign-in doors MeshWars issues
itself. GitHub, Discord and Google already have their own two-factor
options on their side, so this only ever guards the two doors it can
actually protect. You get ten recovery codes the one time you enable
it — save them, since a lost authenticator with no recovery code left
behind is a lost door.

**You can delete your own account, player, and data.** "Delete my
account," at the bottom of Security, permanently destroys every way
you sign in — password, two-factor, and any connected provider —
along with your session, your API key, and any radios linked to your
player. It's confirmed by typing your own display name (plus a
two-factor code if you have one enabled), and it can't be undone. What
it doesn't touch is the shared game record: the squares your team
holds stay with the team, and your past check-ins, honors and
captures stay in the record — they just no longer say your name. A new
Privacy page, linked from every page's footer, spells out exactly what
survives and what doesn't.

## 2026-09-05 — Looking up a player by name now requires signing in

**Finding where a specific player is on the map now requires a
session.** The score panel's player-search box, and the API behind it,
used to answer "where is this person right now" for anyone —
unauthenticated, with no login of any kind required. It now asks you
to sign in first; a logged-out search says so plainly instead of just
failing. Nothing else about the map changed — squares, teams and
territory are exactly as public as they've always been. What required
signing in was specifically the link between a name and a location.

**Signed-in sessions now remember which browser and OS you used, never
your IP address.** The account page's Sessions list already showed
this; what changed is what's actually stored behind it — a label like
"Chrome on Windows" rather than a raw device fingerprint, and no IP
address at all, not even a hashed one. Existing sessions were rewritten
to match, not just new ones going forward.

## 2026-09-05 — Colorado Mesh joins the roster

**Colorado Mesh is now a configured MeshCore community**, alongside
Mountain West Mesh — another net you can check in with. See Where It's
Played on the About page for the current list.

## 2026-09-02 — Switch teams, once a month

**Picked the wrong team, or just want to play somewhere else?** You can
now switch teams yourself, once per calendar month, from the
setup-check panel at the bottom of the Join page — paste your API key,
then use the new team control right below your name and team.

**Your points and streak come with you. Your ground doesn't.** Every
check-in point, exploration point, and streak you've built stays
attached to you, not your old team, so none of it is lost by switching.
The squares you currently hold are the one thing that stays behind —
they remain with the team that held them, exactly as if someone else
on that team had painted them. The confirmation screen spells out both
halves of that before you commit, along with the date you'll be able
to switch again.

## 2026-08-27 — The Layers box gets out of the way

**You can put the Layers panel away.** The layer switcher in the bottom
left corner of the map now has a tab on its right-hand edge. Press it
and the panel slides off the left side of the screen, leaving just the
tab behind; press the tab again and it slides back. Worth the most on a
phone, where the panel was covering a real share of the map and could
not be dismissed at all.

Your choice is remembered on that device, so a collapsed panel stays
collapsed the next time you open the map rather than making you put it
away again on every visit.

## 2026-08-27 — Places no longer stack

**One square, one place, one credit.** A square that carries more than
one place — a landmark standing inside a large park is the usual case —
used to pay out for every live place on it from a single ping, most
valuable first. That was never intended: one trip to one square is one
errand, and it should pay once. Only the most valuable place on a
square scores now. The lesser one is dropped rather than paid alongside
it, and it is not a fallback either: if you already collected the
valuable place this week, the square pays nothing rather than handing
you the cheaper one. If the valuable place is worth more than what is
left of your weekly hundred, it still pays — just for the remainder,
capping you at the hundred instead of turning you away. Two places of
exactly equal value on one square always resolve to the same winner,
for every player and every week.

**The weekly cap caps, it doesn't refuse.** This applies whether or not
a square has a runner-up sitting on it: reach a place worth more than
what's left of your hundred for the week and it no longer turns you
away empty-handed — it pays out the rest of the budget and leaves you
at the ceiling. A player at 50 points who then reaches a 100-point peak
is credited the remaining 50, not zero. That place has used its one
credit for the week either way.

Nothing already earned changes. Past credits, past scores and frozen
months stay exactly as they are — this is forward behaviour only.

**Explorer, said properly.** Explorer is not a monthly honor and never
was resettable: it is the name of an activity and of a rankings tab
(alongside Wardrivers and NetOps under Top Operators), a season-long
points figure that counts toward your total score. The rules said so in
passing; they say so plainly now. The monthly honors built on places
count visits, not points — Tourist, Park Hopper and Peak Tagger.

## 2026-08-25 — Places Worth Going, a new map, and self-hosted terrain

**Scoring changed.** Reaching a summit, a park, or a landmark now earns
points on top of the usual square-claiming game — "Places Worth
Going." Points are effort-based, not category-based: anything inside a
town's limits (a city park, a summit you can drive to) is worth 5,
wherever it sits outside one a landmark is worth 10, a park 25, and a
summit 100. A weekly rotation cycles landmarks and small parks through
so there's always something new nearby (raised from a 5-place, 3-mile
quota to 15 places, 1-mile spacing, measured against real town
coverage rather than guessed). Trailheads and lookouts were dropped
from the landmark list — a trailhead is where a walk starts, not a
destination, and a lookout on a peak is the summit you already scored.

**Explorer and Frontier mean something different now.** Explorer used
to track squares nobody had ever claimed, which mostly duplicated
Frontier. Explorer now counts points earned from places; Frontier keeps
tracking ground taken out past the towns. Months already frozen keep
the numbers they were frozen with.

**The map changed.** The MapLibre map, previously a side-by-side
preview at `/map2`, is now the front page. The old Leaflet map is still
there for comparison at `/map-legacy`, marked so it doesn't compete
with the real page in search. Terrain, roads, and public-land overlays
are now served from the game's own archives instead of fetched live
from another project's tileset — that other project rebuilds its
archives often enough that the map was breaking (blank regions, stale
byte ranges) most times it did. Contour lines were tried and dropped:
they cost most of a map pan's frame budget to draw lines over terrain
the hillshade already shows.

## Older history

Everything before this file existed is in the commit log.
