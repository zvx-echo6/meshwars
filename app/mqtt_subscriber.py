"""MQTT connector kind: a persistent broker subscription feeding
app/checkin.py's mqtt_message_buffer table, distinct from every other
connector kind's 30-second HTTP poll.

Architecture (see also app/checkin.py's KIND_MQTT header comment):

    MqttSubscriber holds one persistent paho-mqtt connection per
    DISTINCT connector_url across every enabled 'mqtt' net (shared by
    nets on the same broker, the same pooling rule every other kind's
    HTTP client already follows -- see app/checkin.py's _mc_client_for/
    _mt_client_for). Each connection subscribes broadly to that
    broker's Meshtastic uplink topics and, for every text message it
    can decode, writes one row into mqtt_message_buffer -- NOT into
    mc_checkin_award directly. app/checkin.py's CheckinPoller reads that
    table on its own next cycle exactly the way it reads an HTTP
    response for the other three kinds (_fetch_mqtt_messages/
    _process_mqtt_message there), so a broker outage never loses
    anything: whatever was already buffered is still there whenever the
    poller (or a reconnected subscriber) next looks, and a poller cycle
    is never blocked waiting on broker I/O.

    A background asyncio task (start/stop below) periodically
    reconciles which brokers should be connected against
    checkin_net -- added, removed, disabled, or edited (credentials,
    topic_root) nets take effect within one reconcile interval, no
    restart, the same no-restart promise every other admin-edited knob
    in this feature already keeps (see app/checkin.py's module
    docstring). A net whose channel_key alone changed does NOT force a
    reconnect -- see _BrokerConnection.update_nets -- only credentials
    or topic_root do, since only those affect the broker connection
    itself.

Threading: paho-mqtt's loop_start() runs every callback (on_connect,
on_message, ...) on ITS OWN background thread, one per Client, not on
this process's asyncio event loop. sqlite3 connections are not safely
shared across threads (app/db.py's connect() sets
check_same_thread=False, which silences the error but does not make
concurrent use from two threads correct) -- so _BrokerConnection opens
its OWN sqlite connection lazily, the first time it actually needs to
write, from INSIDE that callback thread, and never touches (or is
touched by) any connection the asyncio side holds. Status writes
(last_poll_at/last_poll_error, connection-state driven) use a fresh,
short-lived connection per write instead of a cached one, since they
fire rarely (on connect/disconnect, not per message) and a cached
handle would be one more thing to get wrong across the same thread
boundary for no real benefit.

Message formats -- both detected purely by which topic a message
arrives on, per the Meshtastic MQTT integration
(https://meshtastic.org/docs/software/integrations/mqtt/):

    <topic_root>/2/json/<channel-name>/<node>   -- plain JSON
    <topic_root>/2/e/<channel-name>/<node>      -- encrypted ServiceEnvelope protobuf

JSON shape verified against Meshtastic firmware's own serializer
(MeshPacketSerializer::JsonSerialize,
https://github.com/meshtastic/firmware/blob/master/src/serialization/MeshPacketSerializer.cpp),
not assumed: for a text message it publishes
{"id":<int>,"timestamp":<epoch s>,"to":<int>,"from":<int>,"channel":<int>,
"type":"text","sender":"!<gateway node hex>","payload":{"text":"..."}}
-- "sender" here is the GATEWAY (uplink) node, not the message's actual
author, so it is never used for identity; "from" is. `payload` is
occasionally the message's own parsed JSON (if the text itself happens
to be valid JSON) rather than the {"text": ...} wrapper -- see
decode_json_message for how that edge case is handled.

Encrypted-protobuf decode is hand-rolled (no protobuf runtime, no
Meshtastic protobuf package -- see requirements.txt) against the exact
field numbers/wire types in the authoritative .proto sources:
    https://github.com/meshtastic/protobufs/blob/master/meshtastic/mqtt.proto  (ServiceEnvelope)
    https://github.com/meshtastic/protobufs/blob/master/meshtastic/mesh.proto  (MeshPacket, Data)
    https://github.com/meshtastic/protobufs/blob/master/meshtastic/portnums.proto  (PortNum.TEXT_MESSAGE_APP = 1)
See _parse_protobuf_fields/_parse_service_envelope_packet/_parse_mesh_packet/_parse_data below.

AES-CTR decryption (nonce construction, default-key expansion) is
verified against Meshtastic FIRMWARE source, not a third-party writeup
-- see _NONCE construction in decrypt_payload and _expand_channel_key
for the exact files and lines this mirrors:
    https://github.com/meshtastic/firmware/blob/master/src/mesh/CryptoEngine.cpp   (initNonce, encryptAESCtr)
    https://github.com/meshtastic/firmware/blob/master/src/mesh/CryptoEngine.h     (initNonce's own doc comment)
    https://github.com/meshtastic/firmware/blob/master/src/mesh/Channels.h        (defaultpsk bytes)
    https://github.com/meshtastic/firmware/blob/master/src/mesh/Channels.cpp      (getKey() shorthand expansion)

A message that fails to decrypt (wrong/unknown channel key) or fails to
parse (garbage, or a portnum we don't care about) is skipped quietly at
debug level, never logged as a warning/error and never marks a net
failed -- a public broker carrying channels we hold no key for is the
NORMAL case, not something wrong. See decode_encrypted_envelope.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import settings
from .db import connect

log = logging.getLogger("mqtt_subscriber")

KIND_MQTT = "mqtt"

# ---- minimal protobuf wire-format decoding --------------------------------
#
# Just enough of the protobuf wire format (varints and length-delimited
# fields -- see https://protobuf.dev/programming-guides/encoding/) to
# pull a handful of known field numbers out of a ServiceEnvelope/
# MeshPacket/Data message, without depending on Meshtastic's own
# generated protobuf Python package (explicitly out of scope -- see
# requirements.txt's comment on why). Not a general protobuf decoder:
# it does not handle groups (wire type 3/4, removed from proto3 and
# never used by these messages) and treats an unrecognized wire type as
# a parse failure for the whole message, which is exactly the "skip
# quietly" behavior every caller wants for a message this decoder was
# never meant to handle in the first place.


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode one base-128 varint starting at buf[pos]; return
    (value, new_pos). Raises IndexError/ValueError on truncated or
    absurdly long input -- callers catch broadly and treat that as
    "not a valid message," never let it propagate.
    """
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _parse_protobuf_fields(buf: bytes) -> dict[int, list]:
    """Every top-level field in `buf`, keyed by field number, each value
    a list (a field can legally repeat; the messages we read never
    intentionally repeat one, but a malformed/adversarial broker could
    send one twice -- callers that care take the LAST entry, matching
    real protobuf decoders' "last one wins" rule, rather than choking on
    it). Each entry is an int for wire types 0/1/5 (varint/64-bit/32-bit)
    or raw bytes for wire type 2 (length-delimited -- a nested message,
    a string, or `bytes`, indistinguishable at this layer and not our
    job to tell apart; the caller with more context does that).
    """
    fields: dict[int, list] = {}
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        field_no = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:  # varint
            val, pos = _read_varint(buf, pos)
        elif wire_type == 1:  # 64-bit fixed
            val = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(buf, pos)
            val = bytes(buf[pos:pos + length])
            pos += length
        elif wire_type == 5:  # 32-bit fixed
            val = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        else:
            raise ValueError("unsupported protobuf wire type %d" % wire_type)
        fields.setdefault(field_no, []).append(val)
    if pos != n:
        raise ValueError("trailing bytes in protobuf message")
    return fields


def _parse_service_envelope_packet(raw: bytes) -> bytes | None:
    """ServiceEnvelope.packet (field 1, embedded MeshPacket message) --
    see mqtt.proto. None if the field is absent or malformed.
    """
    fields = _parse_protobuf_fields(raw)
    packets = fields.get(1)
    if not packets:
        return None
    return packets[-1]


def _parse_mesh_packet(raw: bytes) -> dict | None:
    """MeshPacket's `from` (field 1, fixed32), `id` (field 6, fixed32),
    and whichever of the oneof payload_variant is present -- `decoded`
    (field 4, embedded Data, used when the channel carries no
    encryption) or `encrypted` (field 5, bytes) -- see mesh.proto. None
    if `from`/`id` are missing or neither payload variant is present;
    every other MeshPacket field (to, channel, rx_time, ...) is simply
    not read, since nothing downstream of this module needs them.
    """
    fields = _parse_protobuf_fields(raw)
    from_vals = fields.get(1)
    id_vals = fields.get(6)
    if not from_vals or not id_vals:
        return None
    return {
        "from": from_vals[-1],
        "id": id_vals[-1],
        "decoded": fields.get(4, [None])[-1],
        "encrypted": fields.get(5, [None])[-1],
    }


def _parse_data(raw: bytes) -> dict | None:
    """Data's `portnum` (field 1, varint enum) and `payload` (field 2,
    bytes) -- see mesh.proto. None if either is missing.
    """
    fields = _parse_protobuf_fields(raw)
    portnum_vals = fields.get(1)
    payload_vals = fields.get(2)
    if not portnum_vals or not payload_vals:
        return None
    return {"portnum": portnum_vals[-1], "payload": payload_vals[-1]}


PORTNUM_TEXT_MESSAGE_APP = 1  # meshtastic/portnums.proto

# ---- channel-key expansion + AES-CTR decryption ----------------------------
#
# Meshtastic's "16 bytes of random PSK for our _public_ default channel
# that all devices power up on (AES128)" -- verified byte-for-byte
# against firmware source (see module docstring for the exact file/line
# this mirrors), not a base64 string copied from a blog post.
_DEFAULT_PSK = bytes([
    0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
    0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01,
])


def _expand_channel_key(raw_b64: str) -> bytes | None:
    """A checkin_net.channel_key value (base64) -> raw AES key bytes,
    expanding Meshtastic's single-byte PSK shorthand exactly the way
    firmware's Channels::getKey() does (Channels.cpp): a decoded key of
    length 1 is not a literal 1-byte AES key (not a valid size for
    either cipher) -- it is an INDEX. Index 0 means encryption is
    explicitly off for that channel (returns None: nothing to decrypt
    with). Any other index N means "the default PSK, with its last byte
    incremented by (N-1)" -- so index 1 (the common "AQ==" shorthand)
    reproduces the default PSK completely unmodified.

    '' (checkin_net's blank/default) is treated exactly like index 1 --
    "the Meshtastic default key" is this column's own documented
    meaning for blank (see app/db.py's checkin_net comment), and index 1
    IS the default key, so this is not a special case, just the same
    shorthand expansion with an implicit index of 1.

    Returns None (skip, never raise) for anything that cannot be a real
    key: bad base64, or a decoded length that is none of 1 (shorthand),
    16 (AES-128), or 32 (AES-256) -- see CryptoEngine.h's setKey() doc
    comment for why only those three lengths are ever meaningful.
    """
    if not raw_b64:
        raw = bytes([1])
    else:
        try:
            raw = base64.b64decode(raw_b64, validate=True)
        except Exception:
            return None
    if len(raw) == 1:
        index = raw[0]
        if index == 0:
            return None
        key = bytearray(_DEFAULT_PSK)
        key[-1] = (key[-1] + index - 1) & 0xFF
        return bytes(key)
    if len(raw) in (16, 32):
        return raw
    return None


def _build_nonce(packet_id: int, from_node: int) -> bytes:
    """The 16-byte AES-CTR nonce/initial-counter-block Meshtastic
    firmware builds for a channel-key-encrypted packet -- verified
    against CryptoEngine::initNonce (see module docstring for the exact
    source): a 64-bit packet id (little-endian; MeshPacket.id is a
    32-bit field, so this is that value zero-extended -- bytes 4..7 are
    always zero) followed by a 32-bit sending node number
    (little-endian), followed by 4 zero bytes (firmware's `extraNonce`,
    always 0 for ordinary channel-key traffic -- it is only ever
    nonzero on the separate Curve25519 direct-message path, which does
    not use this AES-CTR routine at all).
    """
    return (
        (packet_id & 0xFFFFFFFF).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + (from_node & 0xFFFFFFFF).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
    )


def decrypt_payload(key: bytes, packet_id: int, from_node: int, ciphertext: bytes) -> bytes:
    """AES-CTR is its own inverse (see CryptoEngine::decrypt's own
    comment: "For CTR, the implementation is the same"), so this is
    exactly the encrypt routine -- one Cipher/decryptor call, no padding
    (CTR is a stream cipher). `key` must already be a real 16- or
    32-byte key (see _expand_channel_key) -- this does no further
    validation.
    """
    nonce = _build_nonce(packet_id, from_node)
    decryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _text_from_data_bytes(data_bytes: bytes) -> str | None:
    """A serialized Data submessage -> its text, if and only if it is a
    well-formed TEXT_MESSAGE_APP with valid UTF-8 payload bytes. None
    for anything else (wrong portnum, malformed, not UTF-8) -- shared by
    both branches of decode_encrypted_envelope below, since a live
    plaintext `decoded` field and a freshly AES-CTR-decrypted `encrypted`
    field are, at this point, indistinguishable serialized Data bytes.
    """
    try:
        data = _parse_data(data_bytes)
    except Exception:
        return None
    if data is None or data.get("portnum") != PORTNUM_TEXT_MESSAGE_APP:
        return None
    payload = data.get("payload")
    if not isinstance(payload, (bytes, bytearray)):
        return None
    try:
        return bytes(payload).decode("utf-8")
    except UnicodeDecodeError:
        return None


def decode_encrypted_envelope(raw: bytes, candidate_keys: list[bytes]) -> dict | None:
    """One MQTT payload from a `.../2/e/...` topic (a ServiceEnvelope
    protobuf) -> {"from": int, "id": int, "text": str}, or None.

    Despite the topic name, the MeshPacket inside is NOT always
    ciphertext: whether a gateway node publishes the real AES-CTR
    `encrypted` bytes or the plaintext `decoded` Data message on this
    exact same topic is that gateway's OWN, per-node
    `moduleConfig.mqtt.encryption_enabled` toggle, not a property of the
    channel -- verified directly against firmware's uplink path
    (MQTT::onSend, see this module's docstring for the source): when a
    gateway has that toggle off, `p = &mp_decoded` is what gets
    serialized onto this topic, plaintext, full stop. Live traffic
    against mqtt.meshtastic.org during development of this module showed
    the large majority of `/2/e/` packets on that broker are in fact
    already-plaintext `decoded`, not `encrypted` -- so the `decoded`
    case is handled directly here (no decryption needed, nothing to try
    a key against) rather than treated as out of scope.

    For a genuinely `encrypted` packet, tries every candidate key in
    turn (more than one net can share a broker with different
    channel_key values -- see app/checkin.py's module docstring on
    connector pooling, the same reasoning applied here) and returns the
    first one that decrypts to a well-formed TEXT_MESSAGE_APP Data
    message with valid UTF-8 text. Wrong-key decryption produces
    uniformly random-looking bytes, which fail the protobuf/UTF-8 checks
    with overwhelming probability -- this is not a cryptographic MAC
    check (channel-key AES-CTR has none), it is "did this happen to
    parse as the specific shape we expect," which is what makes trying
    multiple candidate keys safe: a wrong key is exponentially unlikely
    to produce something that both parses as a valid Data message AND
    decodes as UTF-8.

    Never raises and never logs above debug -- see this module's
    docstring: a broker carrying channels we hold no key for, or a
    packet type we don't decode (position, telemetry, ...), is the
    ordinary, expected case, not an error.
    """
    try:
        packet_bytes = _parse_service_envelope_packet(raw)
        if packet_bytes is None:
            return None
        pkt = _parse_mesh_packet(packet_bytes)
        if pkt is None:
            return None
        packet_id = pkt["id"]
        from_node = pkt["from"]
        decoded_bytes = pkt.get("decoded")
        ciphertext = pkt.get("encrypted")
    except Exception:
        log.debug("mqtt: malformed ServiceEnvelope/MeshPacket, skipping", exc_info=True)
        return None

    if isinstance(decoded_bytes, (bytes, bytearray)):
        text = _text_from_data_bytes(bytes(decoded_bytes))
        if text is not None:
            return {"from": from_node, "id": packet_id, "text": text}
        return None  # a real decoded Data message, just not text -- nothing to try a key against

    if not isinstance(ciphertext, (bytes, bytearray)) or not ciphertext:
        return None  # neither payload_variant present -- malformed

    for key in candidate_keys:
        try:
            plaintext = decrypt_payload(key, packet_id, from_node, bytes(ciphertext))
        except Exception:
            continue
        text = _text_from_data_bytes(plaintext)
        if text is not None:
            return {"from": from_node, "id": packet_id, "text": text}
    log.debug("mqtt: no candidate key decrypted packet id=%s from=%s", packet_id, from_node)
    return None


def decode_json_message(raw: bytes) -> dict | None:
    """One MQTT payload from a `.../2/json/...` topic ->
    {"from": int, "id": int, "text": str, "ts": int | None}, or None.

    Shape verified against firmware's own JSON serializer -- see this
    module's docstring -- not assumed: a text message is
    {"id":int,"timestamp":int,"from":int,"type":"text",
    "payload":{"text":str}, ...}. `payload` is occasionally the
    message's own parsed JSON value rather than the {"text": ...}
    wrapper (firmware tries JSON::Parse on the plaintext first and only
    falls back to wrapping it if that fails -- see
    MeshPacketSerializer::JsonSerialize) -- handled by accepting either
    a {"text": str} dict or, defensively, a bare string payload.
    """
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "text":
        return None
    from_node = obj.get("from")
    msg_id = obj.get("id")
    if not isinstance(from_node, int) or isinstance(from_node, bool):
        return None
    if not isinstance(msg_id, int) or isinstance(msg_id, bool):
        return None
    payload = obj.get("payload")
    text = None
    if isinstance(payload, dict):
        t = payload.get("text")
        if isinstance(t, str):
            text = t
    elif isinstance(payload, str):
        text = payload
    if text is None:
        return None
    ts = obj.get("timestamp")
    return {
        "from": from_node,
        "id": msg_id,
        "text": text,
        "ts": ts if isinstance(ts, int) and not isinstance(ts, bool) else None,
    }


def _channel_name_from_topic(topic: str, marker: str) -> str:
    """The channel-name path segment of a `.../2/<marker>/<channel>/<node>`
    topic -- purely informational (stored on mqtt_message_buffer for an
    operator to look at; matching against a net is by hashtag substring
    in the message text, same as meshview, never by this). '' if the
    topic doesn't have a segment after the marker.
    """
    parts = topic.split("/")
    try:
        idx = parts.index(marker)
    except ValueError:
        return ""
    if idx + 1 < len(parts):
        return parts[idx + 1]
    return ""


# ---- broker connection -----------------------------------------------------

_JSON_MARKER = "json"
_ENCRYPTED_MARKER = "e"


class _BrokerConnection:
    """One persistent paho-mqtt connection to one broker (one
    connector_url), shared by every enabled mqtt net configured against
    it -- the credentials/topic_root that shape the CONNECTION itself
    are taken from the first such net (`nets[0]`), the same "a
    connector_url is assumed to always mean one upstream configuration"
    rule app/checkin.py's _mc_client_for/kind_by_connector already
    apply to the other connector kinds. Per-net channel_key values are
    NOT part of that assumption -- see decode_encrypted_envelope, which
    tries every net's key as a candidate, so nets sharing a broker are
    free to use different channel keys.
    """

    def __init__(self, connector_url: str, nets: list[dict]) -> None:
        self.connector_url = connector_url
        self._nets = nets
        self._own_conn = None  # sqlite conn, opened lazily ON THE PAHO THREAD -- see module docstring
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

        primary = nets[0]
        username = primary["broker_username"]
        if username:
            self._client.username_pw_set(username, primary["broker_password"] or None)

        parsed = urlsplit(connector_url)
        self._host = parsed.hostname or ""
        is_tls = parsed.scheme == "mqtts"
        self._port = parsed.port or (8883 if is_tls else 1883)
        if is_tls:
            self._client.tls_set()  # system CA bundle, default verification

    @property
    def fingerprint(self) -> tuple:
        """Everything that shapes the CONNECTION itself (credentials,
        which broker, what we subscribe to) -- NOT channel_key, which
        only affects decryption of already-flowing messages and is
        re-read live from self._nets on every message (see
        _candidate_keys), so a channel_key-only edit never needs a
        reconnect. Compared by the reconciler (MqttSubscriber._reconcile_once)
        to decide whether an already-open connection can simply be
        handed its updated net list (update_nets) or must be torn down
        and rebuilt.
        """
        primary = self._nets[0]
        return (self.connector_url, primary["broker_username"],
                primary["broker_password"], primary["topic_root"])

    def connect(self) -> None:
        try:
            self._client.connect_async(self._host, self._port, keepalive=60)
        except Exception as e:
            log.warning("mqtt: connect_async failed for %s: %s", self.connector_url, e)
            self._record_status(False, str(e))
            return
        self._client.loop_start()

    def disconnect(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass
        if self._own_conn is not None:
            try:
                self._own_conn.close()
            except Exception:
                pass
            self._own_conn = None

    def update_nets(self, nets: list[dict]) -> None:
        """Refresh the net list a still-open connection serves --
        called every reconcile cycle even when nothing changed, so a
        channel_key edit (which does not change `fingerprint`, so never
        forces a reconnect) still takes effect on the very next message,
        not after some separate cache-refresh interval.
        """
        self._nets = nets

    def _candidate_keys(self) -> list[bytes]:
        keys: list[bytes] = []
        for n in self._nets:
            key = _expand_channel_key(n["channel_key"])
            if key is not None and key not in keys:
                keys.append(key)
        return keys

    def _record_status(self, ok: bool, error: str | None) -> None:
        """Write connection state onto every net sharing this broker --
        see module docstring on why this uses its own short-lived
        connection rather than self._own_conn (which is reserved for
        the buffer-write path and may not exist yet, e.g. before the
        first message ever arrives). Fires only on connect/disconnect
        transitions (from paho's own callbacks), not per message, so a
        fresh connection per call is not a hot path.
        """
        now = int(time.time())
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for n in self._nets:
                conn.execute(
                    "UPDATE checkin_net SET last_poll_at = ?, last_poll_error = ? WHERE id = ?",
                    (now, None if ok else (error or "")[:500], n["id"]),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            log.exception("mqtt: failed to record connection status for %s", self.connector_url)
        finally:
            conn.close()

    # ---- paho callbacks (run on paho's OWN background thread) ------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        failed = bool(getattr(reason_code, "is_failure", reason_code not in (0, None)))
        if failed:
            log.warning("mqtt: connect failed for %s: %s", self.connector_url, reason_code)
            self._record_status(False, "connect failed: %s" % reason_code)
            return
        topic_root = self._nets[0]["topic_root"] if self._nets else ""
        # Narrow subscription when topic_root is configured, rather than
        # '#' across the whole broker -- see checkin_net.topic_root's own
        # comment in app/db.py.
        sub_filter = (topic_root.rstrip("/") + "/#") if topic_root else "#"
        client.subscribe(sub_filter)
        log.info("mqtt: connected to %s, subscribed %s", self.connector_url, sub_filter)
        self._record_status(True, None)

    def _on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None) -> None:
        log.warning("mqtt: disconnected from %s (reason=%s)", self.connector_url, reason_code)
        self._record_status(False, "disconnected: %s" % reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            self._handle_message(msg.topic, msg.payload)
        except Exception:
            log.exception("mqtt: unhandled error processing message on %s", msg.topic)

    def _handle_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if _JSON_MARKER in parts:
            decoded = decode_json_message(payload)
            marker = _JSON_MARKER
        elif _ENCRYPTED_MARKER in parts:
            decoded = decode_encrypted_envelope(payload, self._candidate_keys())
            marker = _ENCRYPTED_MARKER
        else:
            return  # not a message topic we understand (e.g. firmware/stats topics) -- ignore

        if decoded is None:
            log.debug("mqtt: could not decode message on %s", topic)
            return

        channel_name = _channel_name_from_topic(topic, marker)
        self._insert_buffer_row(channel_name, decoded)

    def _insert_buffer_row(self, channel_name: str, decoded: dict) -> None:
        if self._own_conn is None:
            # Opened HERE, on the paho callback thread -- see module
            # docstring's threading section for why this must not be a
            # connection shared with (or created on) any other thread.
            self._own_conn = connect()
        now = int(time.time())
        ts = decoded.get("ts")
        packet_id = str(decoded["id"])
        try:
            self._own_conn.execute("BEGIN IMMEDIATE")
            self._own_conn.execute(
                "INSERT OR IGNORE INTO mqtt_message_buffer"
                "(connector, packet_id, from_node, channel_name, text, ts, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.connector_url, packet_id, decoded["from"], channel_name,
                 decoded["text"], ts if isinstance(ts, int) else now, now),
            )
            self._own_conn.execute("COMMIT")
        except Exception:
            self._own_conn.execute("ROLLBACK")
            log.exception("mqtt: failed to buffer message %s from %s", packet_id, self.connector_url)


# ---- subscriber (asyncio-side reconciler) ----------------------------------

_HOUSEKEEPING_INTERVAL_S = 3600.0


class MqttSubscriber:
    """Owns every _BrokerConnection and the asyncio task that keeps them
    matching checkin_net's current enabled 'mqtt' rows. Started/stopped
    from app/main.py's lifespan, alongside CheckinPoller -- see that
    module.
    """

    def __init__(self) -> None:
        self._brokers: dict[str, _BrokerConnection] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_housekeeping = 0.0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._reconcile_forever(), name="mqtt-subscriber")
        log.info("mqtt subscriber started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for bc in self._brokers.values():
            bc.disconnect()
        self._brokers.clear()
        log.info("mqtt subscriber stopped")

    async def _reconcile_forever(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("mqtt: reconcile cycle failed")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=max(settings.mqtt_reconcile_interval_seconds, 1)
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _reconcile_once(self) -> None:
        """Read the current set of enabled 'mqtt' nets, grouped by
        connector_url, and make self._brokers match it: connect a new
        broker, disconnect one that no longer has any enabled net, and
        for one that persists, either hand it the refreshed net list
        (credentials/topic_root unchanged -- see _BrokerConnection.
        fingerprint) or tear down and reconnect (credentials/topic_root
        changed -- those shape the connection itself, so nothing short
        of a reconnect can pick them up).
        """
        db_conn = connect()
        try:
            rows = [dict(r) for r in db_conn.execute(
                "SELECT * FROM checkin_net WHERE enabled = 1 AND kind = ?", (KIND_MQTT,)
            ).fetchall()]
        finally:
            db_conn.close()

        wanted: dict[str, list[dict]] = {}
        for n in rows:
            wanted.setdefault(n["connector_url"], []).append(n)

        for url in list(self._brokers):
            if url not in wanted:
                log.info("mqtt: disconnecting %s (no enabled net wants it anymore)", url)
                self._brokers.pop(url).disconnect()

        for url, group in wanted.items():
            existing = self._brokers.get(url)
            if existing is None:
                bc = _BrokerConnection(url, group)
                self._brokers[url] = bc
                bc.connect()
                continue
            # Compute the WOULD-BE fingerprint without constructing a
            # second live client (that would open a second paho
            # connection just to inspect a tuple) -- fingerprint only
            # reads plain dict fields, so this reuses the same logic
            # against the NEW group directly.
            primary = group[0]
            new_fp = (url, primary["broker_username"], primary["broker_password"], primary["topic_root"])
            if existing.fingerprint != new_fp:
                log.info("mqtt: reconnecting %s (credentials or topic_root changed)", url)
                existing.disconnect()
                bc = _BrokerConnection(url, group)
                self._brokers[url] = bc
                bc.connect()
            else:
                existing.update_nets(group)

        await self._maybe_housekeeping()

    async def _maybe_housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_housekeeping < _HOUSEKEEPING_INTERVAL_S:
            return
        self._last_housekeeping = now
        removed = await asyncio.to_thread(_prune_buffer)
        if removed:
            log.info("mqtt: housekeeping removed %d stale mqtt_message_buffer rows", removed)


def _prune_buffer() -> int:
    """Delete mqtt_message_buffer rows older than
    settings.mqtt_buffer_retention_hours -- same retention-housekeeping
    idiom app/mc_ingest.py's McIngestor._housekeeping_sync already uses
    for player_cell_ping, mirrored here rather than shared code since
    the two tables/cutoff columns are unrelated. Pruned on `received_at`
    (when THIS process buffered it), not `ts` (the message's own,
    upstream-supplied timestamp) -- received_at is trustworthy (this
    process's own clock) where a message's self-reported timestamp is
    not, and it is also the column CheckinPoller's read-first dedupe
    ultimately cares about staying bounded.
    """
    cutoff = int(time.time()) - settings.mqtt_buffer_retention_hours * 3600
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM mqtt_message_buffer WHERE received_at < ?", (cutoff,))
        removed = cur.rowcount
        conn.execute("COMMIT")
        return removed
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
