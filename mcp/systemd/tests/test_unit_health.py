"""Unit tests for the failed-unit sweep — no network, no systemd.

unit_health.py is a standalone script in the parent dir; load it by path.
"""
import importlib.util
import pathlib

_p = pathlib.Path(__file__).resolve().parents[1] / "unit_health.py"
_spec = importlib.util.spec_from_file_location("unit_health", _p)
uh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uh)

# Verbatim `systemctl --failed --no-legend --plain --no-pager` from a real host,
# where postfix had been failed for an unknown length of time unnoticed.
LISTING = (
    "postfix@-.service loaded failed failed Postfix Mail Transport Agent (instance -)\n"
    "landible-book-events.service loaded failed failed Audiobook push events\n"
)


class _Poster:
    def __init__(self, fail_after=None):
        self.sent, self.fail_after = [], fail_after

    def __call__(self, event):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise uh.NotDelivered("relayed to 0 targets")
        self.sent.append(event)


def test_unit_names_come_off_the_real_listing():
    assert uh.failed_units(LISTING) == ["postfix@-.service", "landible-book-events.service"]


def test_noise_and_emptiness_are_not_units():
    assert uh.failed_units("") == []
    assert uh.failed_units("0 loaded units listed.") == []
    assert uh.failed_units("\u25cf broken.service loaded failed failed Thing") == ["broken.service"]


def test_a_failed_unit_alerts_once_not_every_run():
    post, state = _Poster(), {}
    assert uh.run(uh.failed_units(LISTING), state, post, detail=lambda u: "boom")
    assert [e["title"] for e in post.sent] == ["landible-book-events.service", "postfix@-.service"]
    assert post.sent[0]["event"] == "unit_failed" and post.sent[0]["source"] == "systemd"
    assert post.sent[0]["message"] == "boom"

    uh.run(uh.failed_units(LISTING), state, post, detail=lambda u: "boom")
    assert len(post.sent) == 2          # still failed, already said so


def test_a_unit_that_recovers_can_alert_again():
    post, state = _Poster(), {}
    uh.run(["a.service"], state, post, detail=lambda u: "x")
    assert state["failed_seen"] == ["a.service"]

    uh.run([], state, post, detail=lambda u: "x")        # recovered
    assert state["failed_seen"] == []

    uh.run(["a.service"], state, post, detail=lambda u: "x")   # fails again
    assert len(post.sent) == 2


def test_a_push_that_did_not_land_is_retried_not_forgotten():
    post, state = _Poster(fail_after=0), {}
    assert not uh.run(["a.service"], state, post, detail=lambda u: "x")
    assert state["failed_seen"] == []                   # nothing marked

    post.fail_after = None
    assert uh.run(["a.service"], state, post, detail=lambda u: "x")
    assert len(post.sent) == 1 and state["failed_seen"] == ["a.service"]


def test_not_delivered_rides_the_existing_retry_path():
    assert issubclass(uh.NotDelivered, OSError)


def test_the_message_is_short_enough_for_a_notification():
    ev = uh.failure_event("x.service", "y" * 5000)
    assert len(ev["message"]) == uh.MESSAGE_CHARS
    assert uh.failure_event("x.service", "")["message"] == "no detail"
