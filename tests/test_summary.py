"""Daily summary: what it picks, the email it builds, and when it moves on."""

import smtplib

import pytest
from fastapi.testclient import TestClient
from sleeper import config, summary, sweep
from sleeper.db import DB
from sleeper.web import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


class FakeSMTP:
    sent: list = []
    fail = False
    refused: dict = {}

    def __init__(self, host, *_args, **_kw):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ehlo(self):
        return None

    def has_extn(self, _name):
        return False

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise smtplib.SMTPRecipientsRefused({})
        FakeSMTP.sent.append(msg)
        return FakeSMTP.refused


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    monkeypatch.setenv("SLEEPER_PUBLIC_URL", "https://sleeper.example")
    monkeypatch.setenv("SLEEPER_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("SLEEPER_MAIL_FROM", "noreply@example.com")
    monkeypatch.setenv("SLEEPER_SUMMARY_TO", "luke@example.com")
    monkeypatch.setattr(summary.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(summary, "_thumb", lambda url: JPEG)
    monkeypatch.setattr(sweep.notify, "send", lambda *a, **k: "sent")
    FakeSMTP.sent, FakeSMTP.fail, FakeSMTP.refused = [], False, {}
    s = config.load()
    db = DB(s.db_path)
    client = TestClient(create_app(s, db))
    client.post(
        "/api/import",
        json={
            "wants": [{"set": "DBS", "who": "Vegito", "q": "vegito psa 10", "max": {"any": 1150}}]
        },
    )
    return s, db, client


def row(item_id, price, text_extra=""):
    return {
        "id": item_id,
        "title": "PSA 10 Vegito BT7-118 SPR Dragon Ball Super",
        "price": f"${price}.00",
        "text": f"PSA 10 Vegito BT7-118 SPR ${price}.00 +$5.64 delivery {text_extra}",
        "img": f"https://i.ebayimg.com/images/g/{item_id}/s-l140.jpg",
    }


class Browser:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def search(self, url):
        return [] if "LH_Sold" in url else self.rows

    def item_photos(self, _item_id):
        return []


def sweep_with(db, s, rows):
    return sweep.run(db, s, browser=lambda: Browser(rows))


def baseline(db, s, rows):
    """The first sweep, then a summary start point after it (as in a long-running install)."""
    run = sweep_with(db, s, rows)
    db.put(summary.LAST_RUN, run)


def test_summary_emails_new_deals_with_photos_and_moves_on(env):
    s, db, client = env
    baseline(db, s, [row("1", 700)])
    sweep_with(db, s, [row("1", 700), row("2", 650, "3 bids 45m left")])
    page = client.get("/summary")
    assert page.status_code == 200 and "1 new deal" in page.text
    assert "Auctions ending in the next 24 hours" in page.text

    assert client.post("/summary/send").status_code == 200  # follows the redirect
    msg = FakeSMTP.sent[-1]
    assert msg["Subject"] == "Sleeper: 1 new deal (1 graded)"
    assert msg["To"] == "luke@example.com" and "noreply@example.com" in msg["From"]
    assert msg["Message-ID"].endswith("@example.com>") and msg["Date"]
    html = msg.get_body(("html",)).get_content()
    assert "cid:" in html and "https://sleeper.example/deal/2/psa" in html
    assert "max bid $1,144.36" in html
    images = [p for p in msg.walk() if p.get_content_type() == "image/jpeg"]
    assert images and all(p["Content-ID"] for p in images)
    assert "https://sleeper.example/deal/2/psa" in msg.get_body(("plain",)).get_content()

    # nothing new since: the next summary says so
    client.post("/summary/send")
    assert FakeSMTP.sent[-1]["Subject"].startswith("Sleeper: no new deals")


def test_failed_send_keeps_the_deals_for_next_time(env):
    s, db, client = env
    baseline(db, s, [row("1", 700)])
    sweep_with(db, s, [row("1", 700), row("2", 650)])
    FakeSMTP.fail = True
    assert summary.run(db, s, lambda g, img: ("<p>x</p>", "x")).startswith("failed")
    assert db.get(summary.LAST_RUN, None) == 1
    FakeSMTP.fail = False
    client.post("/summary/send")
    assert FakeSMTP.sent[-1]["Subject"] == "Sleeper: 1 new deal (1 graded)"


def test_summary_off_without_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPER_DATA", str(tmp_path))
    for k in ("SLEEPER_SMTP_HOST", "SLEEPER_MAIL_FROM", "SLEEPER_SUMMARY_TO"):
        monkeypatch.delenv(k, raising=False)
    s = config.load()
    client = TestClient(create_app(s, DB(s.db_path)))
    page = client.get("/summary")
    assert page.status_code == 200 and "The daily email is off" in page.text
    assert client.post("/summary/send").status_code == 409


def test_running_sweeps_and_photo_mismatches_are_left_out(env):
    s, db, _ = env
    baseline(db, s, [row("1", 700)])
    sweep_with(db, s, [row("1", 700), row("2", 650), row("3", 640)])
    db.run("UPDATE listings SET art = ? WHERE item_id = '3'", '{"verdict": "differs"}')
    running = db.run("INSERT INTO runs (status) VALUES ('running')")
    g = summary.gather(db, s)
    assert [r["item_id"] for r in g["new_graded"]] == ["2"]
    assert g["last_run"] < running


def test_a_failed_baseline_alert_counts_as_a_problem(env):
    s, db, _ = env
    baseline(db, s, [row("1", 700)])
    db.run("INSERT INTO runs (status, alerts) VALUES ('success', 'baseline failed: TimeoutError')")
    assert summary.gather(db, s)["bad_runs"] == 1


def test_first_window_survives_a_failed_send(env):
    s, db, client = env
    sweep_with(db, s, [row("1", 700)])
    sweep_with(db, s, [row("1", 700), row("2", 650)])
    FakeSMTP.fail = True
    client.post("/summary/send")
    first = db.get(summary.FIRST_FROM, None)
    assert first is not None and db.get(summary.LAST_RUN, None) is None
    FakeSMTP.fail = False
    client.post("/summary/send")
    assert db.get(summary.FIRST_FROM, None) == first  # unchanged by the retry
    assert FakeSMTP.sent[-1]["Subject"].startswith("Sleeper: 2 new deals")


def test_photo_downloads_stop_when_the_cdn_is_down(env, monkeypatch):
    s, db, _ = env
    baseline(db, s, [row("1", 700)])
    sweep_with(db, s, [row(str(i), 600 + i) for i in range(2, 12)])
    calls = []
    monkeypatch.setattr(summary, "_thumb", lambda url: calls.append(url) or None)
    assert summary.run(db, s, lambda g, img: ("<p>x</p>", "x")) == "sent"
    assert len(calls) == 3


@pytest.mark.usefixtures("env")
def test_summary_needs_an_absolute_public_url(monkeypatch):
    assert config.load().summary_on
    monkeypatch.setenv("SLEEPER_PUBLIC_URL", "")
    assert not config.load().summary_on


def test_plain_text_carries_sweep_health(env):
    s, db, client = env
    baseline(db, s, [row("1", 700)])
    db.run("INSERT INTO runs (status, alerts) VALUES ('success', 'failed: TimeoutError')")
    client.post("/summary/send")
    text = FakeSMTP.sent[-1].get_body(("plain",)).get_content()
    assert "1 of 1 sweeps had a problem" in text


def test_sent_email_has_a_web_copy_and_ended_auctions_drop_out(env):
    s, db, client = env
    assert client.get("/summary/last").status_code == 404
    baseline(db, s, [row("1", 700)])
    sweep_with(db, s, [row("1", 700), row("2", 650, "3 bids 45m left"), row("3", 640)])
    db.run("UPDATE listings SET ends = '2000-01-01T00:00' WHERE item_id = '2'")
    client.post("/summary/send")
    assert FakeSMTP.sent[-1]["Subject"] == "Sleeper: 1 new deal (1 graded)"
    web = client.get("/summary/last")
    assert web.status_code == 200 and "i.ebayimg.com" in web.text and "cid:" not in web.text


def test_a_sweep_that_never_ends_fails_the_summary_loudly(env, monkeypatch):
    s, db, _ = env
    monkeypatch.setattr(summary, "_settled", lambda *_a, **_k: None)
    assert summary.run(db, s, lambda g, img: ("x", "x")).startswith("failed")
    assert "failed" in db.get("summary_last_result", "")


def test_a_partly_refused_send_is_not_a_success(env):
    s, db, _ = env
    baseline(db, s, [row("1", 700)])
    sweep_with(db, s, [row("1", 700), row("2", 650)])
    FakeSMTP.refused = {"b@example.com": (550, b"no such user")}
    assert summary.run(db, s, lambda g, img: ("x", "x")).startswith("failed")
    assert db.get(summary.LAST_RUN, None) == 1
