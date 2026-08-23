"""Configuration loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # The only required setting
    meshview_base_url: str

    # Storage
    db_path: str = "/data/game.db"

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
    # counting toward the exploration awards (Explorer/Frontier) -- those
    # reward reach and effort, and a plane trivialises both. Was 200 mph,
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
    # revoke a key or disable a player. There is no other authentication
    # anywhere in this application -- this token is the whole of it, so
    # an empty value disables the admin interface entirely rather than
    # leaving it open. Empty must mean off, never open, same reasoning
    # as join_invite_code above.
    admin_token: str = ""

    # Rate limit on the status-check endpoint (/api/mc/status), keyed
    # per client address rather than per key -- a caller with no key,
    # or the wrong one, still costs us a request, and this is the same
    # bounded per-address limiter pattern app/join_api.py already uses
    # for /api/join, not a second mechanism.
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

    # Quick Fingers needs more than one check-in to average, or a single
    # lucky night wins it.
    quick_fingers_min_checkins: int = 2

    # Automation guard, applied to Quick Fingers ONLY. A player whose
    # check-in lands within this many seconds of the same offset every
    # week, over at least this many nets, is almost certainly a cron job
    # and is skipped for that one award -- silently, never penalised
    # elsewhere. A human posting from a phone scatters over minutes.
    automation_stdev_seconds: float = 2.0
    automation_min_samples: int = 3
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

    @property
    def teams_list(self) -> list[str]:
        return [t.strip().upper() for t in self.teams.split(",") if t.strip()]

    @property
    def meshview_url(self) -> str:
        return self.meshview_base_url.rstrip("/")


settings = Settings()
