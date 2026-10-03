from hub.sessions import (
    FULL_IDLE,
    FULL_MAX,
    IP_MAX_FAILURES,
    PREAUTH_MAX_FAILURES,
    PREAUTH_TTL,
    USERNAME_DELAY_CAP,
    Sessions,
    Throttle,
)


def test_sessions_are_stored_hashed(store):
    raw, _ = Sessions(store).create("full", "1.1.1.1", "ua", now=1000)
    with store.connect() as conn:
        ids = [r["id_hash"] for r in conn.execute("SELECT id_hash FROM sessions")]
    assert raw not in ids and len(ids) == 1


def test_kinds_do_not_mix(store):
    sessions = Sessions(store)
    raw, _ = sessions.create("preauth", "1.1.1.1", "ua", now=1000)
    assert sessions.get(raw, "full", now=1001) is None
    assert sessions.get(raw, "preauth", now=1001) is not None


def test_preauth_expires_after_five_minutes(store):
    sessions = Sessions(store)
    raw, _ = sessions.create("preauth", "1.1.1.1", "ua", now=1000)
    assert sessions.get(raw, "preauth", now=1000 + PREAUTH_TTL - 1)
    assert sessions.get(raw, "preauth", now=1000 + PREAUTH_TTL) is None


def test_full_session_idle_and_max_age(store):
    sessions = Sessions(store)
    raw, _ = sessions.create("full", "1.1.1.1", "ua", now=0)
    t = 0
    while t + FULL_IDLE - 10 < FULL_MAX:  # keep it busy: idle never hits
        t += FULL_IDLE - 10
        assert sessions.get(raw, "full", now=t), t
    assert sessions.get(raw, "full", now=FULL_MAX) is None

    raw, _ = sessions.create("full", "1.1.1.1", "ua", now=0)
    assert sessions.get(raw, "full", now=FULL_IDLE) is None


def test_preauth_dies_after_wrong_codes(store):
    sessions = Sessions(store)
    raw, session = sessions.create("preauth", "1.1.1.1", "ua")
    for _ in range(PREAUTH_MAX_FAILURES - 1):
        sessions.add_failure(session)
    assert sessions.get(raw, "preauth") is not None
    sessions.add_failure(session)
    assert sessions.get(raw, "preauth") is None


def test_delay_grows_and_is_capped_but_resets_on_success(store):
    throttle = Throttle(store)
    delays = []
    for _ in range(8):
        delays.append(throttle.delay_seconds())
        throttle.record("9.9.9.9", "owner", "password", ok=False)
    assert delays == [0, 1, 2, 4, 8, 16, USERNAME_DELAY_CAP, USERNAME_DELAY_CAP]
    throttle.record("1.1.1.1", "owner", "password", ok=True)
    assert throttle.delay_seconds() == 0


def test_ip_block_is_per_address_and_expires(store):
    throttle = Throttle(store)
    for _ in range(IP_MAX_FAILURES):
        throttle.record("9.9.9.9", "owner", "password", ok=False, now=1000)
    assert throttle.ip_blocked("9.9.9.9", now=1001)
    assert not throttle.ip_blocked("1.1.1.1", now=1001)  # the owner, elsewhere
    assert not throttle.ip_blocked("9.9.9.9", now=1000 + 15 * 60 + 1)
