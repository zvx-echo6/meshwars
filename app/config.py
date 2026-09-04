"""Configuration loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # The only required setting
    meshview_base_url: str

    # Storage
    db_path: str = "/data/game.db"

    # Terrain/overlay PMTiles archives (USFS roads+trails, public lands)
    # served under /tiles -- see mount() in app/api.py. A bind mount
    # rather than the meshwars-data volume: these are large, static, and
    # copied in from outside docker, so they should survive a container
    # or volume rebuild without being re-copied. If the directory isn't
    # there, the /tiles mount is simply skipped (same pattern as the
    # /static mount's frontend_dir.exists() check).
    tiles_dir: str = "/tiles-data"

    # CARTO basemap tiles (frontend/map2.js). CARTO began serving
    # "API KEY REQUIRED" watermark tiles for keyless usage; this key
    # removes them. It is NOT a secret in the usual sense -- a basemap
    # key is visible to every browser that loads the map and cannot be
    # hidden from users -- but it stays out of the repo all the same, so
    # it is set per-deployment via the environment (see .env.example and
    # the CARTO_API_KEY line in docker-compose.yml). Empty is a fully
    # supported state: the map still renders, just watermarked, so a
    # fresh clone with no key works out of the box.
    carto_api_key: str = ""

    # Polling
    poll_interval_seconds: int = 45
    upstream_rate_per_sec: float = 5.0  # global rate cap to upstream meshview
    upstream_concurrency: int = 5  # max concurrent packets_seen fetches

    # Game timing
    # Six months. Long enough that ground is worth building on, and the
    # reason it can be this long is that score decay already stops a
    # season calcifying -- a square painted once and never revisited
    # falls to zero within weeks, so held territory always means
    # recently active. A monthly result (see the /results page) provides
    # the recognition a short season used to, without the wipe.
    season_days: int = 180
    winner_banner_hours: int = 72
    history_max: int = 12

    # Tile rules
    max_hops: int = 99  # accept positions where hop_start - hop_limit <= max_hops

    # Server
    listen_host: str = "0.0.0.0"
    listen_port: int = 8090

    # Reverse proxy trust (app/client_ip.py). This app is always reached
    # through a reverse proxy in every deployment known to run it --
    # docker-compose.yml publishes a plain host:container port, not
    # network_mode: host, so request.client.host is the PROXY's address,
    # never the real caller's. Every per-address rate limiter in this
    # codebase (app/join_api.py, app/nodes_api.py, app/checkin_api.py,
    # app/clientlog_api.py, app/mc_api.py, app/public_api.py) keyed on
    # that value, which silently collapsed every caller on the internet
    # onto the proxy's one address.
    #
    # X-Forwarded-For carries the real chain, but it is only as
    # trustworthy as whoever last touched it -- so it is read back ONLY
    # when the peer that actually connected to us appears in this set,
    # comma-separated bare IPs and/or CIDR ranges (e.g.
    # "203.0.113.10,10.0.0.0/8"). Empty is the safe default and means
    # exactly what it means everywhere else in this file (join_invite_code,
    # admin_token, ...): OFF, not "trust everyone" -- a fresh clone with
    # no reverse proxy named here must never let an untrusted caller's
    # own header pick its own rate-limit bucket. A deployment that sits
    # behind a reverse proxy MUST set this to that proxy's real
    # address(es) via the environment (see .env.example), or every
    # limiter above stays exactly as broken as it is without this
    # setting. Left unset here (rather than defaulting to this
    # deployment's own proxy address) because that address is private
    # infrastructure and this is a public repository -- see
    # .env.example for where it's actually configured.
    trusted_proxies: str = ""

    @property
    def trusted_proxies_set(self) -> set[str]:
        return {p.strip() for p in self.trusted_proxies.split(",") if p.strip()}

    # Meshtastic portnum constant
    position_app_portnum: int = 3

    # On startup, backfill this many hours of position history from upstream
    backfill_hours: int = 24

    # Node roles excluded from the territory game (infrastructure, not players)
    excluded_roles: str = "ROUTER,ROUTER_LATE,CLIENT_BASE"

    @property
    def excluded_roles_set(self) -> set[str]:
        return {r.strip().upper() for r in self.excluded_roles.split(",") if r.strip()}

    # MeshCore ingest: wardriving batches pushed by the MeshMapper app
    mc_ingest_enabled: bool = True
    # How long a serialized board response is reused before it is built
    # again (0 disables the cache entirely). The board only changes when
    # a ping paints a square, and every open map tab re-fetches it on
    # its own 30s timer -- without this, N viewers cost N queries, N
    # bounds passes and N JSON serializations of the same bytes. The map
    # is already up to 30s stale by design, so a few seconds more costs
    # a reader nothing and takes viewer load off the box entirely.
    board_cache_seconds: int = 10

    mc_queue_max: int = 10000
    mc_max_batch_pings: int = 50
    mc_key_cache_seconds: int = 60
    # About 100 mph. Two jobs: it logs an implausible-speed warning, and
    # it is the line above which a claim is marked by_air and stops
    # counting toward Places Worth Going (app/place_scoring.py, and so
    # toward the Tourist/Park Hopper/Peak Tagger honours and the
    # season-long Explorer ranking built on it) and toward the Frontier
    # award -- those reward reach and effort, and a plane trivialises
    # both. Was 200 mph,
    # which a light aircraft at cruise sails straight under; an interstate
    # at 90 mph is only 40 m/s, so this separates the two cleanly.
    # Territory is NOT affected -- the radio really did hear the repeater.
    mc_max_speed_mps: float = 45.0
    mc_max_clock_skew_seconds: int = 3600
    mc_ping_retention_hours: int = 48
    mc_stat_retention_days: int = 30

    # Per-key rate limit on the ingest endpoint. The endpoint is public
    # and keys are handed out to players, so nothing else stops a key
    # from being replayed as fast as the caller likes. A wardriving
    # session sends a batch every 15-30 seconds, so this default ceiling
    # is far above legitimate use while still stopping a key from being
    # used to hammer the service.
    mc_ingest_rate_limit_batches: int = 20        # max batches per key per window
    mc_ingest_rate_limit_window_seconds: int = 60  # window length, seconds

    # MeshCore raw batch diagnostic log: writes each received batch
    # verbatim (including real GPS positions) so real MeshMapper payloads
    # can be inspected before the ingest thresholds above are tuned, and
    # so the MeshMapper grid-origin assumption can be checked against
    # real data. This records real GPS tracks of real people -- meant to
    # be switched on briefly for tuning and switched back off, not left
    # running.
    mc_raw_log_enabled: bool = False
    mc_raw_log_path: str = "/data/mc_raw.log"  # where the raw batch log is written
    mc_raw_log_max_bytes: int = 10_000_000     # rotate after this many bytes
    mc_raw_log_backups: int = 3                # rotated files kept, beyond the active one

    # Team roster: shared by both boards now that Meshtastic runs on the
    # same player model as MeshCore, keyed on flat grid cells and players
    # rather than the retired geohashes-and-radios game. mc_season_days
    # and the MeshCore-specific ping-scoring settings below it are
    # board-scoped (see the mt_* settings further down for Meshtastic's
    # equivalents) -- the roster itself is not.
    teams: str = "RED,GREEN,BLUE,PURPLE,YELLOW,ORANGE,PINK"
    mc_season_days: int = 180   # see season_days above
    # Points per repeater a ping heard, and the cap on points a single
    # ping can earn -- see app/mc_scoring.py and app/mc_ingest.py for how
    # these turn a heard-repeater count into points.
    mc_points_per_repeater: float = 0.1        # points earned per distinct repeater heard
    mc_max_points_per_ping: float = 1.0        # ceiling on points from any one ping
    mc_score_per_unique_player: float = 0.5    # one-time bonus per new painter
    mc_score_decay_per_day: float = 0.25       # decay rate, applied to all scores
    mc_defense_window_seconds: int = 900       # 15 minutes after capture, no flip
    mc_cooldown_seconds: int = 300             # a player can't repaint the same cell inside this window

    # Which board the map opens on by default. Valid values are "meshcore"
    # or "meshtastic". Configurable per the owner's requirement, not hardcoded.
    mc_default_view: str = "meshcore"

    # Meshtastic scoring: same idea as MeshCore's mc_points_per_repeater /
    # mc_max_points_per_ping above, inverted. MeshCore scores by how many
    # repeaters the PLAYER heard (parsed from the radio's own
    # heard_repeats/repeater_id fields); meshview gives us no such thing
    # for a Meshtastic position packet -- it only tells us which MQTT
    # feeders (meshview's gateway nodes that heard the packet over the air
    # and republished it to MQTT) heard THE PLAYER. That is the only
    # direction meshview can observe, so Meshtastic scores on the distinct
    # feeder count instead of a repeater count. Kept as separate knobs
    # from the mc_ ones (not reused) so the two boards can be tuned
    # independently -- see app/ingest.py for where a ping's distinct
    # feeder count turns into points.
    mt_points_per_feeder: float = 0.1    # points earned per distinct MQTT feeder that heard a ping
    mt_max_points_per_ping: float = 1.0  # ceiling on points from any one ping

    # ---- FreqMapper paint source (app/freqmapper_ingest.py) --------------
    # FreqMapper is a third-party, independently-operated Meshtastic
    # coverage-mapping service -- an alternative source of "who painted
    # which cell" evidence to meshview's own position-packet feed
    # (app/ingest.py, above). It exposes one read-only endpoint
    # (GET /api/v1/integrations/verified-coverage) that reports verified
    # reception events, but deliberately does not say how many stations
    # heard each one, so it cannot feed the same feeder-count scoring
    # model meshview does -- see freqmapper_points_per_event below.
    freqmapper_enabled: bool = False
    freqmapper_base_url: str = "https://dev.freqmapper.net:8443"
    # Empty means off, regardless of freqmapper_enabled -- same contract
    # admin_token and mc_checkin_base_url already use: a blank secret
    # must never be read as "authenticate with nothing," so an empty key
    # disables the connector outright. Never logged, never returned from
    # any route.
    freqmapper_api_key: str = ""
    freqmapper_poll_interval_seconds: int = 60
    freqmapper_page_limit: int = 200
    # Flat award per verified coverage event, replacing the feeder-count
    # model above (mt_points_per_feeder) -- FreqMapper does not report a
    # feeder/repeater count for an event, so there is nothing to count;
    # see apply_paint()'s flat_points parameter in app/mc_scoring.py.
    freqmapper_points_per_event: float = 0.5
    # One-time bonus the first time a player paints a given cell for
    # their team via FreqMapper -- same mechanic mc_score_per_unique_player
    # drives for MeshCore (mc_tile_unique_painter / is_first_paint_for_player()),
    # kept as its own independent knob (via apply_paint()'s
    # unique_player_bonus parameter) rather than reusing
    # mc_score_per_unique_player, so the two can be tuned independently.
    freqmapper_unique_painter_bonus: float = 0.5

    # Lower bound on which verified-coverage event can paint, as a local
    # YYYY-MM-DD date -- compared against the event's verified_at,
    # reduced to a local calendar date, never the raw timestamp. Exactly
    # the same contract checkin_net_start_date above already uses (see
    # its comment for the full reasoning): FreqMapper's own feed hands
    # back history on every request, so a freshly enabled connector
    # would otherwise repaint the board with everything the feed can
    # still reach in one pass, on behalf of whichever players happen to
    # be registered today. Empty is deliberately treated as "block every
    # event," not "no lower bound," the same contract mc_checkin_base_url,
    # join_invite_code, and checkin_net_start_date all use for "empty
    # means off, never open." Set this to the date FreqMapper should
    # actually start counting from before relying on it.
    freqmapper_paint_from: str = ""

    # Which upstream source(s) paint the Meshtastic board: "meshview"
    # (app/ingest.py's position-packet poll and backfill score, exactly
    # as before this switch existed), "freqmapper" (app/ingest.py's
    # position-painting paths -- the portnum=3 poll and _backfill -- are
    # gated off entirely; roster and nodeinfo keep running either way,
    # since those are identity/roster concerns, not scoring), or "both"
    # (both painters run, each exactly as if it were the sole selected
    # source). This is the single switch both app/ingest.py and
    # app/freqmapper_ingest.py read, so the two can never disagree about
    # which source(s) are currently allowed to paint.
    #
    # Default is "both". Two sources touching the same cell is not a
    # conflict that needs resolving here: app/mc_scoring.py's existing
    # capture/defense window and per-repeater cooldown already absorb a
    # cell being credited from more than one direction, the same as they
    # would for two different players painting it. This is deliberately
    # NOT a preference or priority between the two ingest paths -- both
    # simply paint, unconditionally, with no dedupe or arbitration
    # between them. ("FreqMapper is recommended" is guidance given to
    # PLAYERS in the account page's Meshtastic setup copy, about which
    # radio-side integration to use; it has no bearing on this switch.)
    #
    # app/freqmapper_ingest.py's poll loop still runs (and dedupes)
    # whenever freqmapper_enabled is true regardless of this value, so
    # switching it later never replays history -- only the actual
    # score/write is gated on it. Changing this default does not rewrite
    # any already-seeded freqmapper_config row (see
    # seed_freqmapper_config_from_env in app/freqmapper_ingest.py, only
    # ever applied while that row's updated_at is still 0) -- an
    # existing deployment's stored choice is never overridden by a code
    # upgrade.
    mt_paint_source: str = "both"

    # Minimum Meshtastic position precision a packet must carry to score.
    # A Meshtastic node can report its position at reduced precision
    # (Channel settings -> Precision, or a firmware default): the device
    # transmits latitude_i/longitude_i as signed 1e-7-degree integers, but
    # at reduced precision only the top `precision_bits` of that 32-bit
    # value are real -- the low `32 - precision_bits` bits are zeroed
    # before transmission, so the true position can be anywhere in a box
    # 2**(32-precision_bits) raw units (1e-7 degree each) on a side. A
    # grid cell here is ~300m (app/grid.py's CELL_LAT_DEG=0.0027 ~= 300m);
    # accepting a box bigger than that lets a radio that has truncated
    # away a kilometre or more of uncertainty paint a specific square it
    # may never have been near.
    #
    # Box size in meters, converting via ~111,320 m per degree of
    # LATITUDE (not longitude -- longitude is narrower everywhere off the
    # equator, and inside this play area, ~40-44 N, a degree of longitude
    # is only ~74% of a degree of latitude; using the wider figure is the
    # conservative choice for a security gate, since it never understates
    # the box):
    #   precision_bits=17 -> 2**15 * 1e-7 * 111320 =~ 365 m (still >= the
    #                        300 m cell -- not good enough)
    #   precision_bits=18 -> 2**14 * 1e-7 * 111320 =~ 182 m (well under
    #                        the 300 m cell -- the floor chosen below)
    #   precision_bits=19 -> 2**13 * 1e-7 * 111320 =~  91 m
    # 18 is the minimum: the first value whose box sits safely (roughly
    # 40% smaller than the cell) under 300 m, rather than merely under it.
    mt_min_precision_bits: int = 18

    # A packet whose payload never carries precision_bits at all (older
    # firmware, or a meshview fork that omits it) cannot be scored
    # against the check above -- there is nothing to check. Investigated
    # against 8,000 live position packets pulled straight from this
    # deployment's meshview (2026-08-25): every single one that carried a
    # usable latitude_i/longitude_i also carried precision_bits; the only
    # packets missing it had no position fields at all (already caught by
    # pings_bad_coord, upstream of this check). So "missing precision on
    # an otherwise-real fix" is not something current firmware in this
    # deployment actually does -- there is no population of legitimate
    # older-firmware players this would silently shut out today. Given
    # that, and that silently accepting a missing field would hand any
    # future gap in what meshview reports right back to whoever wants to
    # exploit it, missing precision_bits is treated as failing this gate
    # (rejected, not accepted) -- see app/ingest.py's precision check.

    # Speed gate for the Meshtastic path -- app/mc_ingest.py has had this
    # for MeshCore (settings.mc_max_speed_mps) since that board shipped;
    # this closes the same gap here, where live data has shown implied
    # speeds of 220-1300 mph between a player's own consecutive fixes.
    #
    # Deliberately NOT the same 45 m/s (~100 mph) MeshCore uses, and not
    # for lack of trying to reuse it: crossing MeshCore's threshold only
    # marks a claim `by_air` (a soft label the exploration awards read --
    # see mc_max_speed_mps's own comment) and never costs a square, so it
    # can afford to be trigger-happy. Crossing this one REJECTS the fix
    # outright and credits nothing, so it needs real headroom above
    # anything a genuine player can do on the ground: this play area's
    # interstates top out around 80 mph (~36 m/s), and two consecutive
    # fixes landing in adjacent ~300 m cells a few seconds apart can
    # already read as a deceptively high "speed" from cell-center
    # geometry alone, before anyone drives anywhere. 90 m/s (~201 mph) is
    # exactly double MeshCore's floor -- comfortable headroom over any
    # real drive plus that grid-quantization noise -- and still sits well
    # under the slowest impossible jump actually observed (220 mph =~ 98
    # m/s), so nothing this gate is meant to catch gets missed either.
    mt_max_speed_mps: float = 90.0

    # Play-area bounding box: Ontario, Oregon (NW) to Provo, Utah (SE).
    # Pings outside this box are rejected before they touch anything else.
    # Setting play_area_north == play_area_south disables the check
    # entirely, for an unbounded deployment.
    play_area_north: float = 44.10   # northern edge, degrees latitude
    play_area_south: float = 40.20   # southern edge, degrees latitude
    play_area_west: float = -117.00  # western edge, degrees longitude
    play_area_east: float = -111.60  # eastern edge, degrees longitude

    # Public self-registration (/api/join). Empty invite code disables
    # registration entirely -- empty must mean off, never open, since we
    # never want a blank submitted code to register as a "match" against
    # a blank configured one. See the check at the top of join() in
    # app/join_api.py.
    join_invite_code: str = ""          # empty disables registration entirely
    # When true, the invite code above is shown on the join page so a
    # person can read it and type it in themselves, instead of being
    # told it separately. Defaults to false so a fresh install never
    # reveals its own code by accident -- this is a deliberate
    # per-deployment choice, not a code change.
    join_invite_code_public: bool = False
    join_rate_limit_attempts: int = 5
    join_rate_limit_window_seconds: int = 600
    public_host: str = "meshwars.com"   # used to build the config link

    # Google Search Console site-ownership token: rendered as
    # <meta name="google-site-verification" content="..."> in the head of
    # every public page (see app/api.py's _inject_head/_templated_html_page)
    # when set, and omitted entirely when empty -- an empty tag with no content is
    # not a safe stand-in for "not verifying," so empty must mean the
    # meta tag is absent from the page, not present with nothing in it.
    # Same off-by-default reasoning as join_invite_code/admin_token
    # above: a fresh install has not been claimed in Search Console and
    # must not emit a verification tag nobody asked for. Set from Search
    # Console's own HTML-tag verification method and requires no rebuild
    # to take effect, just an .env edit and a restart.
    google_site_verification: str = ""

    # Registering a Meshtastic node (protocol "mt") is what puts it on the
    # Meshtastic board -- a node nobody has registered is read by the
    # meshview poller and discarded, the same way an unregistered MeshCore
    # contact never reaches a square. This flag opens or closes that
    # registration path for a deployment. It defaults to false so a fresh
    # install doesn't open Meshtastic registration until it decides to run
    # that board -- the same deliberate per-deployment choice as
    # join_invite_code_public above, not a stale placeholder.
    join_meshtastic_enabled: bool = False

    # Admin door (/admin, /api/admin/*): lists players and keys, and can
    # revoke a key or disable a player. Empty must mean off, never open,
    # same reasoning as join_invite_code above.
    #
    # CLAIM-ONLY (privacy-hardening pass): this token used to authenticate
    # every /api/admin/* request directly (the X-Admin-Token header,
    # compared with secrets.compare_digest -- see app/admin_api.py's own
    # module docstring for the full history). It no longer does. Every
    # real admin/operator action now goes through a signed-in account
    # holding a role (account.role -- see app/db.py's own MIGRATIONS
    # comment on that column, and app/admin_api.py's _role_guard()); this
    # token's ONLY remaining power is POST /api/admin/roles/claim, which
    # lets an already signed-in account with active two-factor
    # authentication grant ITSELF the operator role. That is a single
    # auditable event (admin_action_log), not an anonymous bypass -- the
    # difference the whole redesign exists to make.
    #
    # Left set after bootstrap, this token remains a way to mint an
    # ADDITIONAL operator (a second person, or recovery from a fresh
    # account if every existing operator is unreachable) -- see that
    # route's own docstring for why several accounts claiming with the
    # same token is intentional, not a bug. Cleared once no more
    # claiming is wanted; every already-granted role keeps working
    # regardless (see _admin_surface_enabled()'s own comment for exactly
    # what stays reachable once this is blank again).
    admin_token: str = ""

    # Address-keyed rate limit on POST /api/admin/roles/claim -- same
    # "without one this is a token-guessing oracle" reasoning
    # account_link_key_rate_limit_attempts/window_seconds gives for its
    # own endpoint just below, and the same independent-_BoundedHits-
    # instance-per-call-site convention (app/auth.py's module docstring).
    # This is the highest-value guessing target the whole roles feature
    # adds -- a correct guess grants the OPERATOR role outright, not
    # merely a player -- so it gets its own budget rather than sharing
    # link-key's.
    admin_claim_operator_rate_limit_attempts: int = 5
    admin_claim_operator_rate_limit_window_seconds: int = 60

    # Rate limit on the status-check endpoint (/api/mc/status), keyed
    # per client address rather than per key -- a caller with no key,
    # or the wrong one, still costs us a request, and this is the same
    # bounded per-address limiter pattern app/join_api.py already uses
    # for /api/join, not a second mechanism.
    # Public read API (app/public_api.py). Generous on purpose: a bot
    # polling the capture feed every few seconds is the expected caller,
    # not an abusive one. It is still a bound, and it is per address.
    public_api_rate_limit_requests: int = 120
    public_api_rate_limit_window_seconds: int = 60

    mc_status_rate_limit_attempts: int = 30
    mc_status_rate_limit_window_seconds: int = 60

    # Rate limit on the key-authenticated node-management routes
    # (GET/POST/DELETE /api/nodes, app/nodes_api.py), keyed per API key
    # rather than per address -- the caller is already authenticated by
    # the time this is checked, same as McIngestor.rate_limit_ok in
    # app/mc_ingest.py, so limiting by key (not IP) is both simpler and
    # more accurate. A player clicking "add radio" a handful of times
    # in a sitting is nowhere near this ceiling.
    node_api_rate_limit_attempts: int = 30
    node_api_rate_limit_window_seconds: int = 60

    # Address-keyed rate limit on GET /find and GET /api/mc/find
    # (app/api.py, app/mc_api.py) -- the "where is this named player"
    # lookup, now gated behind app/sessions.py's require_session (see
    # that dependency's own privacy-hardening docstring) but still
    # unthrottled at the address level before this setting existed.
    # Requiring a session bounds WHO can ask, not how often -- a single
    # signed-in account could otherwise script this into exactly the
    # people-finder the session gate exists to prevent, one display_name
    # guess at a time. Same shape and same per-address budget as
    # mc_status_rate_limit_*/node_api_rate_limit_* above; sized the same
    # (30/60s) since this is the same "a human repeats it a few times,
    # a script does not get to hammer it" polling budget, not the
    # tighter one-time-action budget account_link_key_rate_limit_* uses.
    find_rate_limit_attempts: int = 30
    find_rate_limit_window_seconds: int = 60

    # Client-side failure reporting (POST /api/clientlog,
    # app/clientlog_api.py). Public and unauthenticated -- reached by
    # frontend/map2.js's window.onerror/unhandledrejection hooks and its
    # map-failure paths (construction throw, map.on('error'), load
    # timeout, webglcontextlost) -- so it is rate-limited per address,
    # same shape as join_rate_limit_attempts above. A real failing page
    # load fires a small handful of these at once (a thrown error plus
    # the unhandledrejection it can trigger, say); 20 per minute covers
    # that with room to spare without giving a flood a free log-filler.
    clientlog_rate_limit_attempts: int = 20
    clientlog_rate_limit_window_seconds: int = 60

    # Net check-ins (app/checkin.py): a second way to earn points,
    # alongside squares held. A weekly net runs Wednesday evenings;
    # checking in on either board's feed earns a registered player's
    # team checkin_points once per player per net. Off by default -- a
    # fresh install has not configured either upstream feed and must
    # not start polling a third-party service (live.mwmesh.com,
    # meshview) it was never told about.
    checkin_enabled: bool = False
    checkin_points: float = 25.0              # points a check-in earns; stored on the award row itself, so a later change here never rewrites history

    # Streak bonus: turning up to the net every week is worth more than
    # turning up once. Paid from the SECOND consecutive net onward --
    # bonus = min(streak_bonus * (streak - 1), streak_bonus_max) -- so a
    # first check-in is still worth exactly checkin_points and there is
    # no such thing as a bonused streak of one. With the defaults that
    # runs 25/30/35/40/45/50 and caps from the sixth net on. Like
    # checkin_points, the total is copied onto the award row when it is
    # earned, so changing these never rewrites what someone already won.
    checkin_streak_bonus: float = 5.0
    checkin_streak_bonus_max: float = 25.0

    # ---- monthly results (app/results.py) ----------------------------
    # How far beyond the nearest town's edge a square must be to qualify
    # for Frontier. Twenty miles out is where mesh coverage runs out, so
    # a qualifying square still has to hear a repeater -- expect months
    # with no winner, which is the point of a prestige award.
    frontier_miles: float = 20.0

    # Quick Fingers averages however many timed check-ins a player has,
    # down to one. It was two, so that a single lucky night could not win
    # it -- but that assumed every net carries timings, and the first one
    # ever run did not: message_ts shipped 2026-08-22, between the
    # 2026-08-19 and 2026-08-26 nets, so August had exactly one timed net
    # and the award could not be won by anyone at all. An award that
    # silently cannot fire is worse than one a lucky night can win, and
    # the anti-automation guard below is what actually protects it.
    quick_fingers_min_checkins: int = 1

    # Automation guard, applied to Quick Fingers ONLY. A player whose
    # check-in lands within this many seconds of the same offset every
    # week, over at least this many nets, is almost certainly a cron job
    # and is skipped for that one award -- silently, never penalised
    # elsewhere. A human posting from a phone scatters over minutes.
    automation_stdev_seconds: float = 2.0
    automation_min_samples: int = 3

    # PREVIEW HOSTS ONLY. Off in production, deliberately: a month is
    # judged once, when it is over, and a standings table that reshuffles
    # under people mid-month is not a result. Turning this on makes
    # /results additionally render the month currently being played,
    # computed live and labelled provisional. It is a read-only display
    # switch -- the in-progress month is never frozen and nothing extra
    # is ever written to month_result/month_standing/month_award.
    results_preview_current_month: bool = False

    # Longest Road is only worth winning if it is hard to hold. A chain
    # this long crosses most of a valley, and ANY rival square landing in
    # the middle of it cuts it in two -- so a leader can be denied the
    # award outright by one well-placed capture, and nobody wins it that
    # month. That is the point: it is the one honor another team can take
    # off you without out-scoring you anywhere.
    #
    # 300 for its first month, lowered to 200 on 2026-08-31. It worked
    # exactly as designed, and that was the problem: RED held a 330-square
    # run in the morning, it was cut during the day, and by evening their
    # best was 237 with nobody clearing the floor -- so the award nobody
    # had ever seen was going to go unawarded in the month it launched.
    longest_road_min_squares: int = 200
    checkin_poll_interval_seconds: int = 30   # tight -- MeshCore's feed returns only its newest 100 messages, no pagination, and a busy net can approach that

    # The net window. Weekday follows Python's datetime.weekday()
    # (Monday=0 .. Sunday=6), so Wednesday=2. Hours are LOCAL to
    # checkin_net_timezone and inclusive at both ends (checked as
    # start_hour <= local_hour <= end_hour), so the defaults 17..23
    # cover 17:00:00 through 23:59:59. checkin_net_timezone must be a
    # real IANA zone name, resolved through zoneinfo at call time, never
    # a fixed UTC offset -- a fixed offset would drift an hour off the
    # intended local window every time America/Boise crosses a
    # daylight-saving transition.
    checkin_net_weekday: int = 2
    checkin_net_start_hour: int = 17
    checkin_net_end_hour: int = 23
    checkin_net_timezone: str = "America/Boise"

    # Lower bound on which net a check-in can be awarded for, as a local
    # YYYY-MM-DD net date -- compared against the value net_date_for_ts()
    # returns, never the raw message timestamp, since those two differ
    # for a message sent late in the net window. Both check-in feeds
    # carry history (live.mwmesh.com's weekly-net channel returns its
    # newest 100 messages regardless of age; meshview keeps its own
    # backlog), so the first poll after this feature goes live would
    # otherwise retroactively award every past net still visible
    # upstream -- for whichever players happen to be registered today,
    # never for anyone else who was actually at those same nets. Empty
    # is deliberately treated as "block every net" (see
    # net_date_for_ts), not "no lower bound" -- the same contract
    # mc_checkin_base_url and join_invite_code already use for "empty
    # means off, never open," extended here because an unset bound must
    # never silently become an unbounded one. Set this to the date of
    # the first net that should actually count before relying on it.
    checkin_net_start_date: str = ""

    # MeshCore weekly-net feed: a live.mwmesh.com channel-messages
    # endpoint, entirely separate from the wardriving ingest path in
    # app/mc_ingest.py. Empty disables this half of check-ins even when
    # checkin_enabled is true -- empty means off, never open, same
    # contract as join_invite_code.
    mc_checkin_base_url: str = ""
    mc_checkin_channel: str = "#weekly-net"

    # Meshtastic check-in feed reuses meshview_base_url (the SAME
    # meshview instance app/ingest.py already polls for position
    # packets) with portnum=1 (text) instead of 3 (position), filtered
    # by this hashtag -- there is only one meshview instance configured
    # for this deployment, so no separate base URL setting is needed.
    mt_checkin_hashtag: str = "#freq51"

    # MeshCore public-key directory bridge (live.mwmesh.com/api/nodes):
    # resolves a player's already-bound MeshCore radio contact (the
    # first 8 hex characters of its public key -- see app/mc_ingest.py's
    # auto-bind, or app/nodes_api.py) to that radio's current display
    # name in the directory, so a player who has never typed a check-in
    # registration command can still earn credit under whatever name
    # their own radio currently advertises. See app/checkin.py's module
    # docstring for why this is trusted (it starts from a public key we
    # already know independently, not from the name) and for the
    # ambiguity rules that make it refuse rather than guess. The
    # directory changes slowly -- radios don't rename themselves often
    # -- and a net only produces a few dozen messages, so it is cached
    # and refreshed on its own interval, never fetched per message.
    mc_checkin_directory_limit: int = 5000
    mc_checkin_directory_refresh_seconds: int = 900

    # MQTT connector kind (app/mqtt_subscriber.py): a persistent broker
    # subscription, not a poll -- see that module's docstring for why it
    # runs as its own background task rather than inside CheckinPoller's
    # 30-second cycle. mqtt_buffer_retention_hours bounds
    # mqtt_message_buffer (app/db.py) the same way mc_ping_retention_hours
    # bounds player_cell_ping above -- a decoded message only has to
    # survive long enough for CheckinPoller's next cycle to read and
    # settle it, so 48h is generous margin, not a real requirement.
    # mqtt_reconcile_interval_seconds is how often the subscriber re-reads
    # checkin_net for enabled mqtt nets and connects/disconnects/
    # resubscribes to match -- independent of checkin_poll_interval_seconds,
    # since broker connections are the subscriber's own concern, not the
    # poller's.
    mqtt_buffer_retention_hours: int = 48
    mqtt_reconcile_interval_seconds: int = 30

    # ---- Account layer (app/sessions.py, app/account_api.py) -------------
    # A login session sitting above the existing hashed-API-key player
    # model -- see app/db.py's "Account layer" SCHEMA comment for the
    # full design. Nothing here touches the key-only path at all.

    # Sliding-expiry lifetime: how long a session stays valid AFTER its
    # most recent touch, not from creation. 30 days is generous on
    # purpose -- this is a browser session cookie for a casual
    # territory-game site, not a banking app, and the cost of getting
    # logged out unexpectedly (having to sign in again) is a much worse
    # experience here than the cost of a long-lived cookie.
    account_session_lifetime_seconds: int = 60 * 60 * 24 * 30  # 30 days

    # How stale last_seen_at has to be before a verify actually writes a
    # fresh value, rather than treating the session as "seen recently
    # enough already." Bumping last_seen_at (and, with it, expires_at --
    # see account_session's own comment in app/db.py) on literally every
    # authenticated request would mean a write, through the same global
    # WriteSession lock every other write in this app already
    # serializes through (app/db.py), on every single page a logged-in
    # visitor loads -- directly contending with the check-in poller's
    # own periodic writes for no real benefit, since "logged in 4
    # seconds ago" and "logged in 3 minutes ago" are indistinguishable
    # to a human reading their own session list. A few minutes of slack
    # costs nothing observable and removes nearly all of that write
    # volume.
    account_session_touch_threshold_seconds: int = 300  # 5 minutes

    # Secure flag on the session cookie -- browsers refuse to send a
    # Secure cookie back over plain http. True is the correct default
    # for every real deployment (this app is always reached through a
    # TLS-terminating reverse proxy -- see app/client_ip.py's own
    # docstring), so a fresh clone is safe by default; a developer
    # running the server bare over http on localhost sets this to false
    # in their own .env to be able to log in at all, the same escape
    # hatch trusted_proxies and every other empty-means-off setting in
    # this file gives a local dev loop.
    account_session_cookie_secure: bool = True

    # Address-keyed rate limit on POST /api/account/link-key
    # (app/account_api.py). That endpoint takes an arbitrary API key in
    # its request body and reports back whether it was valid -- with no
    # limit at all, a logged-in attacker could use it to brute-force
    # other players' keys (which are otherwise only ever transmitted,
    # never guessed at) simply by trying candidates until one links.
    # Reuses the same _BoundedHits shape app/auth.py's
    # require_api_key_principal() already uses for its own pre-auth
    # limiter, as its own independent budget (see that module's
    # docstring for why each call site keeps its own instance rather
    # than sharing a pool) -- sized separately from
    # node_api_rate_limit_*/mc_status_rate_limit_* because this is a
    # session-authenticated, occasional, one-time-per-account action,
    # not a routine polling or ingest budget.
    account_link_key_rate_limit_attempts: int = 5
    account_link_key_rate_limit_window_seconds: int = 60

    # ---- OAuth sign-in (app/oauth.py, app/oauth_api.py) -------------------
    # Provider login on top of the account layer above -- GET
    # /auth/{provider}/start and /auth/{provider}/callback exchange a
    # provider's authorization code for an account_identity row and a
    # session, the same session app/sessions.py already knows how to
    # mint. See app/oauth.py's module docstring for the full provider
    # table and the callback decision tree.
    #
    # Every provider follows the same "empty means off" contract as
    # join_invite_code/admin_token above: BOTH client_id and
    # client_secret must be set for a provider to be reachable at all
    # (app/oauth.py's provider_enabled()) -- a half-configured provider
    # (one set, one blank) is treated as fully off, never as "trust an
    # empty secret." app/oauth_api.py returns 404 for a disabled
    # provider's routes, the same "indistinguishable from not existing"
    # contract app/admin_api.py's _api_guard already uses for the admin
    # door.
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = ""

    # Same "empty means off" contract as the GitHub pair above, for
    # Discord's OAuth2 app credentials -- see app/oauth.py's DISCORD
    # Provider(...) entry and _discord_extract_identity for how these
    # are used.
    oauth_discord_client_id: str = ""
    oauth_discord_client_secret: str = ""

    # Same "empty means off" contract as the GitHub/Discord pairs
    # above, for Google's OAuth 2.0 client credentials -- see
    # app/oauth.py's GOOGLE Provider(...) entry and
    # _google_extract_identity for how these are used. Registered as a
    # Google Cloud "OAuth client ID" (Web application type), not an API
    # key -- Google's own console terminology for this pair.
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""

    # Base URL (scheme + host, no trailing slash, e.g.
    # "https://meshwars.com") this deployment is publicly reachable at,
    # used to build the exact redirect_uri
    # ({oauth_public_base_url}/auth/{provider}/callback) every enabled
    # provider is sent on the authorize request and must match, to the
    # byte, whatever redirect URI is registered in that provider's own
    # OAuth app settings -- provider consoles reject a mismatch outright,
    # so this has to be the real public URL, not settings.public_host
    # (which is bare hostname, no scheme, meant only for the
    # meshmapper:// custom-URL-scheme link app/join_api.py builds, a
    # different consumer with a different format). Left empty here for
    # the same reason trusted_proxies is: it names this deployment's own
    # public address, which is private-deployment information that does
    # not belong in a public repository's default. Empty means no
    # provider can be started at all, regardless of client id/secret --
    # app/oauth_api.py's start route treats a blank base URL as "not
    # configured," the same as a disabled provider.
    oauth_public_base_url: str = ""

    # How long the state/PKCE-verifier cookies app/oauth_api.py sets on
    # /auth/{provider}/start survive before the browser drops them --
    # just long enough to cover a real login through a provider's
    # consent screen (a person choosing an account, maybe entering 2FA)
    # without leaving a long-lived cookie sitting around doing nothing
    # once the flow either completes or is abandoned. Shorter than
    # account_pending_identity's own 15-minute TTL below, since this
    # only has to survive the redirect round-trip, not a person deciding
    # whether to create an account afterward.
    oauth_state_cookie_lifetime_seconds: int = 600  # 10 minutes

    # How long a pending identity (app/db.py's account_pending_identity
    # -- a brand-new provider identity with no matching account yet,
    # case 4 of app/oauth_api.py's callback decision tree) stays
    # redeemable via POST /api/account/pending/create or a follow-up
    # login. Long enough for a person to read the choice screen and
    # decide, short enough that an abandoned pending row is not a
    # meaningful standing liability -- the same reasoning join_token's
    # 15-minute TTL already applies to a different kind of single-use
    # ticket.
    account_pending_identity_lifetime_seconds: int = 900  # 15 minutes

    # ---- Email sign-in (magic link) (app/email_login.py, app/oauth_api.py) --
    # Passwordless sign-in by a one-time link sent to an address --
    # POST /auth/email/start mails the link, GET /auth/email/callback
    # redeems it and feeds the exact same identity-resolution decision
    # tree resolve_oauth_callback() above already implements for every
    # OAuth provider, as provider="email". Not a Provider(...) table
    # entry in app/oauth.py -- there is no authorize/token/userinfo
    # round trip here, no client id/secret, no PKCE; it only shares the
    # account model and the callback decision tree those providers do.
    #
    # Same "empty means off" contract as every optional feature in this
    # file: an empty smtp_host means email sign-in is not offered at
    # all -- GET /auth/providers omits it and both routes below 404,
    # the same "indistinguishable from not existing" contract
    # app/oauth.py's provider_enabled() already gives a disabled OAuth
    # provider. See app/email_login.py's email_login_enabled() for the
    # exact check (it also requires oauth_public_base_url below to be
    # set, since that setting already names this deployment's own
    # public base address -- the same fact the magic link itself has to
    # be built from -- rather than this feature inventing a second
    # setting for the identical concept).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_address: str = "admin@meshwars.com"

    # "starttls" (default -- connect on the plain-text port, typically
    # 587, then upgrade the connection via STARTTLS before sending
    # anything else) or "implicit" (TLS from the very first byte,
    # typically port 465). Anything else is treated as "starttls" -- see
    # app/email_login.py's _send_sync() for exactly how each is dialed.
    # This deliberately does not configure or verify SPF/DKIM/DNS for
    # smtp_from_address's own domain -- that is a mail-server-side
    # concern handled separately from this application.
    smtp_tls_mode: str = "starttls"

    # How long a magic-link token (app/db.py's email_login_token) stays
    # redeemable via GET /auth/email/callback before it expires -- same
    # 15-minute default and reasoning as
    # account_pending_identity_lifetime_seconds above: long enough to
    # find the mail and click it, short enough that an unopened link is
    # not a meaningful standing liability.
    email_login_token_lifetime_seconds: int = 900  # 15 minutes

    # Rate limits on POST /auth/email/start -- the most abusable surface
    # this feature adds: an unauthenticated endpoint that triggers an
    # outbound mail send for whatever address is posted to it. TWO
    # independent budgets, both enforced -- per SOURCE IP (stop one
    # client from flooding sign-in mail at arbitrary addresses) and per
    # TARGET EMAIL ADDRESS (stop one address from being mail-bombed by
    # many different source IPs). Reuses app/auth.py's
    # new_rate_limit_bucket()/_BoundedHits, the same bounded-dictionary
    # counter every other rate limit in this app already uses, as its
    # own independent budget per the reasoning app/auth.py's module
    # docstring gives for why call sites never share one.
    email_login_start_ip_rate_limit_attempts: int = 5
    email_login_start_ip_rate_limit_window_seconds: int = 300
    email_login_start_address_rate_limit_attempts: int = 3
    email_login_start_address_rate_limit_window_seconds: int = 600

    # ---- Player-facing key rotation (app/account_api.py) -------------------
    # Session-keyed rate limit on POST /api/account/rotate-key -- same
    # reasoning as account_link_key_rate_limit_* above (an occasional,
    # one-time-per-sitting, session-authenticated action, not a routine
    # polling budget), kept as its own separate setting rather than
    # reused so the two can be tuned independently -- rotate-key has no
    # guessing-oracle shape at all (it takes no attacker-supplied
    # secret), the risk here is closer to "an automated script mashing
    # the button" than key brute-forcing, so this can run a little
    # tighter without the same justification link-key's limit needs.
    account_rotate_key_rate_limit_attempts: int = 5
    account_rotate_key_rate_limit_window_seconds: int = 60

    # ---- Account password (app/password_login.py, app/oauth_api.py) -------
    # The fifth sign-in door: hashlib.scrypt (stdlib, memory-hard) set/
    # changed via POST /api/account/password, checked at sign-in via
    # POST /auth/password/start. See app/password_login.py's own module
    # docstring for the full reasoning on why scrypt and not
    # app/mc_ingest.py's hash_secret().
    #
    # scrypt cost parameters. n=2**14 (16384), r=8, p=1 is RFC 7914's
    # own "interactive login" recommendation -- the standard baseline
    # for a password checked on every sign-in request, not a
    # backup-encryption key checked once in a while (which is where
    # RFC 7914 suggests a much heavier n). Memory cost is 128*n*r*p
    # bytes = 128*16384*8*1 = 16 MiB, comfortably under Python's
    # hashlib.scrypt default 32 MiB maxmem ceiling, so no maxmem
    # override is needed. dklen=32 (256 bits) matches every other
    # derived-key/digest length already used in this codebase
    # (secrets.token_urlsafe(32) for every raw token, hash_secret's own
    # 32-byte SHA-256 digest). These parameters are stored ALONGSIDE
    # each password hash (app/db.py's account_password table), not
    # read fresh from these settings at verify time, so raising them
    # later never invalidates a password hashed under today's values --
    # see that table's own comment for why.
    account_password_scrypt_n: int = 2 ** 14
    account_password_scrypt_r: int = 8
    account_password_scrypt_p: int = 1
    account_password_scrypt_dklen: int = 32

    # Minimum password length -- a length floor only, deliberately no
    # character-class rules (no forced uppercase/digit/symbol): NIST
    # SP 800-63B's own current guidance is that length is the dominant
    # factor in resisting an offline guessing attack and that composition
    # rules mostly just push people toward predictable substitutions
    # ("Password1!") without adding real entropy. 8 is that guidance's
    # own floor.
    account_password_min_length: int = 8

    # Rate limits on POST /auth/password/start -- same two-budget shape
    # as email_login_start_*_rate_limit_* above and the same reasoning
    # (per SOURCE IP, per TARGET address), since this is an
    # unauthenticated endpoint taking a guessable secret (a password,
    # unlike a magic-link token) and reporting back success/failure.
    account_password_start_ip_rate_limit_attempts: int = 10
    account_password_start_ip_rate_limit_window_seconds: int = 300
    account_password_start_address_rate_limit_attempts: int = 5
    account_password_start_address_rate_limit_window_seconds: int = 600

    # Escalating backoff on repeated failed password attempts for one
    # TARGET address, on top of (not instead of) the flat address
    # window limit just above -- a flat window alone lets an attacker
    # spend the whole budget in a burst right at the start of every
    # window, forever, at a steady rate; escalating backoff instead
    # makes each successive failure against the same address cost more
    # wall-clock time than the last, which degrades a sustained
    # guessing campaign much faster than a brute-force script expects.
    # base_seconds is the lockout after the FIRST failure, doubled
    # (factor) for each failure after that, capped at max_seconds so a
    # very long failure streak does not lock the address out
    # effectively forever. Reset to nothing on the next SUCCESSFUL
    # sign-in for that address (see app/oauth_api.py's
    # _PasswordBackoff.record_success()).
    account_password_backoff_base_seconds: float = 2.0
    account_password_backoff_factor: float = 2.0
    account_password_backoff_max_seconds: float = 900.0  # 15 minutes

    # ---- Contact email (app/account_api.py, app/oauth_api.py) -------------
    # A user-editable address on the account, for contact purposes
    # ONLY -- see account.contact_email's own MIGRATIONS comment in
    # app/db.py for exactly why this can never sign anyone in or
    # auto-link a new provider identity. Verified via a mailed,
    # single-use link, the same hashed-single-use-ticket shape as
    # email_login_token but its OWN table
    # (account_contact_email_token) -- see that table's own comment for
    # why it is not a reuse of email_login_token.
    account_contact_email_token_lifetime_seconds: int = 900  # 15 minutes
    account_contact_email_rate_limit_attempts: int = 3
    account_contact_email_rate_limit_window_seconds: int = 600

    # ---- TOTP two-factor authentication (app/totp.py, app/totp_api.py) ----
    # An OPTIONAL second factor layered on top of the two LOCAL sign-in
    # doors above (password, magic-link email) -- see app/totp_api.py's
    # module docstring for exactly which doors this guards and why the
    # three OAuth providers (GitHub/Google/Discord) are deliberately
    # NOT guarded (each already enforces whatever second factor its own
    # user configured with it).
    #
    # The symmetric key (cryptography.fernet.Fernet, urlsafe-base64,
    # 32 raw bytes) that encrypts a TOTP secret at rest
    # (app/totp.py's encrypt_secret/decrypt_secret) -- held in the
    # ENVIRONMENT, never the database, so a stolen database file alone
    # yields no working second factors (see app/totp.py's own "secret
    # at rest" docstring section for the full reasoning). This is the
    # one setting in this file where "empty means off" is not quite
    # right: every other optional feature here just does nothing when
    # unconfigured, but this one FAILS CLOSED -- app/totp.py's
    # totp_encryption_available() returns False when this is unset (or
    # not a valid Fernet key), and app/totp_api.py's enrollment route
    # refuses to start enrollment at all rather than ever risk storing
    # an unencrypted secret. Generate one with:
    #   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    account_totp_encryption_key: str = ""

    # otpauth:// issuer name (app/totp.py's provisioning_uri()) -- shown
    # by every authenticator app next to the account label, so a person
    # enrolling several MeshWars-adjacent tools in one app can still
    # tell this entry apart. A plain constant, not deployment-configurable:
    # unlike oauth_public_base_url (a real, deployment-specific address),
    # this is just a display string, and every deployment of this
    # codebase is still "MeshWars."
    account_totp_issuer: str = "MeshWars"

    # How long the intermediate "credential verified, second factor not
    # yet supplied" state (app/db.py's account_totp_challenge -- the
    # short-lived, single-use ticket a successful password or
    # magic-link sign-in hands off to POST /auth/totp/verify, following
    # the exact same hashed-single-use-ticket shape as
    # account_pending_identity/email_login_token) stays redeemable.
    # Short on purpose: this is never meant to survive more than the
    # few seconds it takes to switch to an authenticator app and read a
    # code off it, and -- unlike a pending OAuth identity someone might
    # want to think over -- there is nothing to decide here, only to
    # type in.
    account_totp_challenge_lifetime_seconds: int = 300  # 5 minutes

    # How many recovery codes app/totp_api.py's activation route mints,
    # shown to the account holder exactly once. 10 is the number every
    # mainstream 2FA implementation (GitHub, Google, ...) converges on
    # -- enough to cover realistic loss-of-device scenarios (a phone
    # replaced every year or two, an app reinstalled after a factory
    # reset) across a game's typical lifetime, without generating (and
    # asking a person to safely store) an excessive list.
    account_totp_recovery_code_count: int = 10

    # Rate limits on POST /api/account/totp/activate (proving
    # enrollment) and DELETE /api/account/totp (disabling) -- both take
    # an attacker-guessable 6-digit code from an ALREADY-authenticated
    # session, so these are account-keyed (like
    # account_contact_email_rate_limit_* above), not address-keyed:
    # there is no anonymous-caller enumeration risk here, only "how many
    # guesses can one hijacked/logged-in session throw before a code it
    # doesn't actually know verifies."
    account_totp_activate_rate_limit_attempts: int = 10
    account_totp_activate_rate_limit_window_seconds: int = 300
    account_totp_disable_rate_limit_attempts: int = 10
    account_totp_disable_rate_limit_window_seconds: int = 300

    # Rate limits on POST /auth/totp/verify -- the actual sign-in second
    # factor, reached with NO session at all (only the short-lived
    # challenge cookie/token above), which makes this the single
    # highest-value guessing target this feature adds: a 6-digit code
    # is only 1,000,000 possibilities, and the skew window
    # (app/totp.py's DEFAULT_SKEW_STEPS) accepts three of them at once.
    # Two independent budgets, the same "per IP, per the specific thing
    # being attacked" shape POST /auth/password/start already uses (per
    # source IP, and per the challenge being guessed against, rather
    # than per email address -- there is no email here, only a
    # challenge ticket).
    account_totp_verify_ip_rate_limit_attempts: int = 10
    account_totp_verify_ip_rate_limit_window_seconds: int = 300
    account_totp_verify_challenge_rate_limit_attempts: int = 8
    account_totp_verify_challenge_rate_limit_window_seconds: int = 300

    @property
    def teams_list(self) -> list[str]:
        return [t.strip().upper() for t in self.teams.split(",") if t.strip()]

    @property
    def meshview_url(self) -> str:
        return self.meshview_base_url.rstrip("/")


settings = Settings()
