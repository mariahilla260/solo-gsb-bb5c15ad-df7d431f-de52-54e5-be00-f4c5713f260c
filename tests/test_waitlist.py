"""End-to-end tests for the waitlist (queueing for booked-out windows)."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import psycopg

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_point(client, tz="UTC"):
    r = client.post("/points", json={"name": "候补测试点", "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def publish(client, pid, rules, expected=0):
    r = client.post(
        f"/points/{pid}/rules",
        json={"expected_version": expected, "rules": rules},
    )
    assert r.status_code == 201, r.text
    return r.json()["version"]


def tomorrow_at(hour, minute=0):
    """Tomorrow (UTC) at a fixed wall-clock hour; always safely in the future."""
    day = (datetime.now(UTC) + timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def reserve(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/reservations",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def occupy(client, pid, key, start, end):
    """Book a window successfully; return the reservation payload."""
    r = reserve(client, pid, key, start, end)
    assert r.status_code == 201, r.text
    return r.json()


def apply_waitlist(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/waitlist",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def queued_entry(client, pid, key, start, end):
    """Apply for a booked-out window successfully; return the entry payload."""
    r = apply_waitlist(client, pid, key, start, end)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "queued"
    return body


def query_entry(client, pid, entry_id, token):
    return client.get(f"/points/{pid}/waitlist/{entry_id}?token={token}")


def open_point(client, tz="UTC"):
    """A point open tomorrow 08:00-12:00 (local); returns pid."""
    pid = create_point(client, tz=tz)
    publish(
        client,
        pid,
        {
            "weekly": [
                {
                    "weekday": tomorrow_at(0).isoweekday(),
                    "start_time": "08:00",
                    "end_time": "12:00",
                }
            ],
            "closed_dates": [],
            "makeup": [],
        },
    )
    return pid


def db_execute(sql, params=()):
    """Direct database access for state a public API cannot reach (e.g.
    moving a waitlist start into the past to exercise the expiry path)."""
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(sql, params)


# ---------------------------------------------------------------------------
# Application validation
# ---------------------------------------------------------------------------

def test_apply_on_occupied_window_returns_number_token_and_status(client):
    pid = open_point(client)
    occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))

    r = apply_waitlist(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["point_id"] == pid
    assert body["status"] == "queued"
    assert body["start"] == iso(tomorrow_at(9))
    assert body["end"] == iso(tomorrow_at(10))
    assert body["rules_version"] == 1
    assert body["query_token"]
    assert body["promoted_reservation_id"] is None
    assert body["decided_at"] is None


def test_apply_on_free_window_is_rejected(client):
    pid = open_point(client)
    # Nobody booked anything: the window is free and must be reserved directly.
    r = apply_waitlist(client, pid, "wait-free", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "window_available"


def test_apply_rejected_windows(client):
    pid = open_point(client)
    occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))

    # Not covered by the published calendar (open 08:00-12:00 tomorrow).
    r = apply_waitlist(client, pid, "w-out", tomorrow_at(13), tomorrow_at(14))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "outside_open_hours"

    # End not after start / overlong duration / start in the past.
    r = apply_waitlist(client, pid, "w-bad", tomorrow_at(10), tomorrow_at(9))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_period"

    r = apply_waitlist(
        client, pid, "w-long", tomorrow_at(8), tomorrow_at(12, 30)
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_period"

    past = datetime.now(UTC) - timedelta(hours=1)
    r = apply_waitlist(client, pid, "w-past", past, past + timedelta(hours=1))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_period"


def test_apply_without_published_rules_is_409(client):
    pid = create_point(client)
    r = apply_waitlist(client, pid, "w-norules", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "no_published_version"


def test_apply_unknown_point_is_404(client):
    r = apply_waitlist(client, 999999, "w-ghost", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "point_not_found"


def test_apply_idempotency_replay_and_conflict(client):
    pid = open_point(client)
    occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    first = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    # Same key + same content replays the original application (200).
    r = apply_waitlist(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 200, r.text
    replay = r.json()
    assert replay["id"] == first["id"]
    assert replay["query_token"] == first["query_token"]
    assert replay["status"] == "queued"

    # Same key + different content conflicts.
    r = apply_waitlist(client, pid, "wait-1", tomorrow_at(9, 30), tomorrow_at(10))
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"]["code"] == "idempotency_conflict"
    assert body["error"]["details"]["entry_id"] == first["id"]


# ---------------------------------------------------------------------------
# Lookup + withdraw
# ---------------------------------------------------------------------------

def test_query_requires_a_valid_token(client):
    pid = open_point(client)
    occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    r = client.get(f"/points/{pid}/waitlist/{entry['id']}")
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "invalid_query_token"

    r = query_entry(client, pid, entry["id"], "wrong")
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "invalid_query_token"

    r = query_entry(client, pid, 999999, entry["query_token"])
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "waitlist_entry_not_found"

    r = query_entry(client, pid, entry["id"], entry["query_token"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["reservation"] is None
    assert "query_token" not in body


def test_withdraw_while_queued_and_repeat_withdraw(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    r = client.post(
        f"/points/{pid}/waitlist/{entry['id']}/withdraw",
        json={"query_token": entry["query_token"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "withdrawn"
    assert r.json()["decided_at"] is not None

    # Repeat withdraw is idempotent.
    r = client.post(
        f"/points/{pid}/waitlist/{entry['id']}/withdraw",
        json={"query_token": entry["query_token"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "withdrawn"

    # The withdrawn entry no longer participates in promotions: releasing the
    # booked window leaves it withdrawn, and the window becomes bookable.
    r = client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    assert r.status_code == 200, r.text
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert state["status"] == "withdrawn"
    assert state["reservation"] is None
    r = reserve(client, pid, "after-withdraw", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 201, r.text


def test_withdraw_requires_a_valid_token(client):
    pid = open_point(client)
    occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    r = client.post(f"/points/{pid}/waitlist/{entry['id']}/withdraw", json={})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "invalid_query_token"

    r = client.post(
        f"/points/{pid}/waitlist/{entry['id']}/withdraw",
        json={"query_token": "wrong"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "invalid_query_token"

    r = client.post(
        f"/points/{pid}/waitlist/999999/withdraw",
        json={"query_token": entry["query_token"]},
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "waitlist_entry_not_found"


# ---------------------------------------------------------------------------
# Promotion on release
# ---------------------------------------------------------------------------

def test_cancel_promotes_the_earliest_eligible_entry(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    r = client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    assert r.status_code == 200, r.text

    r = query_entry(client, pid, entry["id"], entry["query_token"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "promoted"
    assert body["decided_at"] is not None
    reservation = body["reservation"]
    assert reservation is not None
    assert reservation["id"] != booked["id"]
    assert reservation["start"] == iso(tomorrow_at(9))
    assert reservation["end"] == iso(tomorrow_at(10))
    assert reservation["status"] == "active"
    assert reservation["cancel_token"]

    # The promoted reservation is a real one: its token cancels it.
    r = client.post(
        f"/points/{pid}/reservations/{reservation['id']}/cancel",
        json={"cancel_token": reservation["cancel_token"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"


def test_reschedule_release_promotes_waitlist_entry(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    r = client.post(
        f"/points/{pid}/reservations/{booked['id']}/reschedule",
        json={
            "cancel_token": booked["cancel_token"],
            "idempotency_key": "move-1",
            "expected_revision": 1,
            "start": iso(tomorrow_at(10)),
            "end": iso(tomorrow_at(11)),
        },
    )
    assert r.status_code == 200, r.text

    body = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert body["status"] == "promoted"
    assert body["reservation"]["start"] == iso(tomorrow_at(9))


def test_promotion_fifo_order_and_still_blocked_stays_queued(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    first = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    second = queued_entry(client, pid, "wait-2", tomorrow_at(9), tomorrow_at(10))

    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    first_state = query_entry(client, pid, first["id"], first["query_token"]).json()
    second_state = query_entry(
        client, pid, second["id"], second["query_token"]
    ).json()
    assert first_state["status"] == "promoted"
    # The second entry overlaps the freshly promoted reservation: it stays.
    assert second_state["status"] == "queued"
    assert second_state["reservation"] is None

    # Releasing the promoted reservation promotes the next one in line.
    promoted = first_state["reservation"]
    client.post(
        f"/points/{pid}/reservations/{promoted['id']}/cancel",
        json={"cancel_token": promoted["cancel_token"]},
    )
    second_state = query_entry(
        client, pid, second["id"], second["query_token"]
    ).json()
    assert second_state["status"] == "promoted"
    assert second_state["reservation"]["id"] != promoted["id"]


def test_promotion_happens_at_most_once(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert state["status"] == "promoted"
    promoted = state["reservation"]

    # Cancelling the promoted reservation frees the window again, but the
    # entry is terminal and must not be promoted a second time.
    client.post(
        f"/points/{pid}/reservations/{promoted['id']}/cancel",
        json={"cancel_token": promoted["cancel_token"]},
    )
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert state["status"] == "promoted"
    assert state["reservation"]["id"] == promoted["id"]
    assert state["reservation"]["status"] == "cancelled"

    # The freed window is bookable by a fresh reservation.
    r = reserve(client, pid, "after-all", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 201, r.text


def test_promotion_uses_the_rules_version_current_at_promotion(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    assert entry["rules_version"] == 1

    # Republish an equivalent calendar (the active reservation stays covered).
    publish(
        client,
        pid,
        {
            "weekly": [
                {
                    "weekday": tomorrow_at(0).isoweekday(),
                    "start_time": "08:00",
                    "end_time": "12:00",
                },
                {"weekday": 7, "start_time": "18:00", "end_time": "20:00"},
            ],
            "closed_dates": [],
            "makeup": [],
        },
        expected=1,
    )

    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert state["status"] == "promoted"
    # The entry keeps the version it applied under; the reservation is issued
    # under the version current at promotion time.
    assert state["rules_version"] == 1
    assert state["reservation"]["rules_version"] == 2


def test_entry_whose_start_passed_is_expired_on_release(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    # Move the queued window into the past (a state no API client can reach
    # directly) to exercise the release-scan expiry path.
    past = datetime.now(UTC) - timedelta(hours=2)
    db_execute(
        "UPDATE waitlist_entries SET start_utc = %s, end_utc = %s WHERE id = %s",
        (past, past + timedelta(hours=1), entry["id"]),
    )

    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert state["status"] == "expired"
    assert state["reservation"] is None
    assert state["decided_at"] is not None


def test_entry_not_open_under_current_rules_stays_queued(client):
    pid = open_point(client)
    # 09:30-10:30 booked; the waitlist window 10:00-11:00 overlaps it.
    booked = occupy(client, pid, "booked", tomorrow_at(9, 30), tomorrow_at(10, 30))
    entry = queued_entry(
        client, pid, "wait-1", tomorrow_at(10), tomorrow_at(11)
    )

    # Republish so the reservation stays covered (08:00-10:30) but the
    # waitlist window no longer is.
    publish(
        client,
        pid,
        {
            "weekly": [
                {
                    "weekday": tomorrow_at(0).isoweekday(),
                    "start_time": "08:00",
                    "end_time": "10:30",
                }
            ],
            "closed_dates": [],
            "makeup": [],
        },
        expected=1,
    )
    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert state["status"] == "queued"
    assert state["reservation"] is None

    # Re-open the window and release another booking: the queued entry is
    # promoted by that later release.
    publish(
        client,
        pid,
        {
            "weekly": [
                {
                    "weekday": tomorrow_at(0).isoweekday(),
                    "start_time": "08:00",
                    "end_time": "12:00",
                }
            ],
            "closed_dates": [],
            "makeup": [],
        },
        expected=2,
    )
    blocker = occupy(client, pid, "blocker", tomorrow_at(10), tomorrow_at(11))
    client.post(
        f"/points/{pid}/reservations/{blocker['id']}/cancel",
        json={"cancel_token": blocker["cancel_token"]},
    )
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert state["status"] == "promoted"
    assert state["reservation"]["rules_version"] == 3


def test_promoted_entry_cannot_be_withdrawn(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    r = client.post(
        f"/points/{pid}/waitlist/{entry['id']}/withdraw",
        json={"query_token": entry["query_token"]},
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"]["code"] == "waitlist_not_withdrawable"
    assert body["error"]["details"]["reason"] == "already_promoted"


def test_expired_entry_cannot_be_withdrawn(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    past = datetime.now(UTC) - timedelta(hours=2)
    db_execute(
        "UPDATE waitlist_entries SET start_utc = %s, end_utc = %s WHERE id = %s",
        (past, past + timedelta(hours=1), entry["id"]),
    )
    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    r = client.post(
        f"/points/{pid}/waitlist/{entry['id']}/withdraw",
        json={"query_token": entry["query_token"]},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["details"]["reason"] == "already_expired"


def test_replay_after_promotion_reports_current_status(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    r = apply_waitlist(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == entry["id"]
    assert body["status"] == "promoted"
    assert body["promoted_reservation_id"] is not None


# ---------------------------------------------------------------------------
# Concurrency: releases, bookings and promotions stay consistent
# ---------------------------------------------------------------------------

def test_concurrent_cancels_promote_exactly_once(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    first = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    second = queued_entry(client, pid, "wait-2", tomorrow_at(9), tomorrow_at(10))

    results = []

    def cancel():
        r = client.post(
            f"/points/{pid}/reservations/{booked['id']}/cancel",
            json={"cancel_token": booked["cancel_token"]},
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=cancel) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [200] * 6
    first_state = query_entry(client, pid, first["id"], first["query_token"]).json()
    second_state = query_entry(
        client, pid, second["id"], second["query_token"]
    ).json()
    # Exactly one promotion happened (FIFO); the loser stays queued.
    assert first_state["status"] == "promoted"
    assert second_state["status"] == "queued"


def test_concurrent_cancel_and_booking_never_overlap(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))

    outcomes = {}

    def cancel():
        r = client.post(
            f"/points/{pid}/reservations/{booked['id']}/cancel",
            json={"cancel_token": booked["cancel_token"]},
        )
        outcomes["cancel"] = r.status_code

    def book():
        r = reserve(client, pid, "racer", tomorrow_at(9), tomorrow_at(10))
        outcomes["book"] = r.status_code

    threads = [threading.Thread(target=cancel), threading.Thread(target=book)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes["cancel"] == 200
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    if outcomes["book"] == 201:
        # The booking won the lock first: the entry is still blocked by it.
        assert state["status"] == "queued"
    else:
        # The release won: the entry was promoted and the booking conflicts.
        assert outcomes["book"] == 409
        assert state["status"] == "promoted"


def test_promoted_reservation_obeys_overlap_for_new_bookings(client):
    pid = open_point(client)
    booked = occupy(client, pid, "booked", tomorrow_at(9), tomorrow_at(10))
    entry = queued_entry(client, pid, "wait-1", tomorrow_at(9), tomorrow_at(10))
    client.post(
        f"/points/{pid}/reservations/{booked['id']}/cancel",
        json={"cancel_token": booked["cancel_token"]},
    )
    state = query_entry(client, pid, entry["id"], entry["query_token"]).json()
    assert state["status"] == "promoted"

    # The promoted reservation occupies the window like any other booking.
    r = reserve(client, pid, "overlapper", tomorrow_at(9, 30), tomorrow_at(10, 30))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "reservation_overlap"
    assert r.json()["error"]["details"]["conflicting_reservation_ids"] == [
        state["reservation"]["id"]
    ]

    # And it is protected by the change guard on rule publication.
    r = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {"weekly": [], "closed_dates": [], "makeup": []},
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "reservation_change_conflict"
