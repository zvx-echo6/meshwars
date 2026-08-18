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
    season_days: int = 30
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


    # Fortress scoring constants
    score_per_packet: float = 0.5          # effort bonus per qualifying paint
    score_per_unique_node: float = 1.0     # one-time bonus per new painter
    score_decay_per_day: float = 0.25      # decay rate, applied to all scores
    defense_window_seconds: int = 900      # 15 minutes after capture, no flip

    # MeshCore ingest: wardriving batches pushed by the MeshMapper app
    mc_ingest_enabled: bool = True
    mc_queue_max: int = 10000
    mc_max_batch_pings: int = 50
    mc_key_cache_seconds: int = 60
    mc_max_speed_mps: float = 89.0          # about 200 mph
    mc_max_clock_skew_seconds: int = 3600
    mc_ping_retention_hours: int = 48
    mc_stat_retention_days: int = 30

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

    # MeshCore scoring: a separate scoreboard from the Meshtastic fortress
    # game above, keyed on flat grid cells and players instead of
    # geohashes and radios. Up to seven teams instead of two.
    teams: str = "RED,GREEN,BLUE,PURPLE,YELLOW,ORANGE,PINK"
    mc_season_days: int = 30
    mc_score_per_ping: float = 0.5             # effort bonus per qualifying paint
    mc_score_per_unique_player: float = 1.0    # one-time bonus per new painter
    mc_score_decay_per_day: float = 0.25       # decay rate, applied to all scores
    mc_defense_window_seconds: int = 900       # 15 minutes after capture, no flip
    mc_cooldown_seconds: int = 300             # a player can't repaint the same cell inside this window

    # Which board the map opens on by default. Valid values are "meshcore"
    # or "meshtastic". Configurable per the owner's requirement, not hardcoded.
    mc_default_view: str = "meshcore"

    @property
    def teams_list(self) -> list[str]:
        return [t.strip().upper() for t in self.teams.split(",") if t.strip()]

    @property
    def meshview_url(self) -> str:
        return self.meshview_base_url.rstrip("/")


settings = Settings()
