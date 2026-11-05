"""Cuff pressure monitor - laptop alert service (EXSU 620 Project 02, Group 2)

Runs on the laptop next to the dashboard. Every second it reads the Pico (http://192.168.4.1),
stores readings, alarm events, acknowledgements, registered phones and every text message
in a SQLite database (cuff.db), and texts every new alarm to every registered phone.

Python 3.9 or newer, standard library only (no pip install needed).

  python3 cuff_service.py                 run the service (Ctrl+C to stop)
  python3 cuff_service.py report          shift report from the database (time in range, excursions, alarms, acks)
  python3 cuff_service.py report --csv    same, also saved as shift_report.csv

Text messages go through Twilio (free trial works with verified numbers). Set, before starting:
  export TWILIO_SID=ACxxxxxxxx  TWILIO_TOKEN=xxxxxxxx  TWILIO_FROM=+15145550000
Without these the service runs in DRY RUN: every message is written to the database and printed,
but not sent.

The laptop needs two connections: Wi-Fi on CuffMonitor (to reach the Pico) and internet for Twilio,
for example an iPhone on USB with Personal Hotspot, or Ethernet.
"""
import base64
import csv
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PICO = os.environ.get("PICO_URL", "http://192.168.4.1")
DB = os.environ.get("CUFF_DB", "cuff.db")
POLL_S = 1.0
CONTACTS_S = 10.0
OLD_EVENT_S = 60          # on start-up, events older than this are stored but not texted
RETRIES = 3
ALERT_PREFIXES = ("ADVISORY", "CRITICAL", "TECHNICAL", "PROBABLE")   # every alert, as asked
SID, TOKEN, FROM = (os.environ.get(k, "") for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"))
DRY_RUN = not (SID and TOKEN and FROM)

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings(ts_utc TEXT, pico_t REAL, cmh2o REAL, ok INTEGER, state TEXT, label TEXT);
CREATE TABLE IF NOT EXISTS alarm_events(event_id INTEGER PRIMARY KEY, ts_utc TEXT, pico_t REAL, kind TEXT,
    text TEXT, cmh2o REAL, UNIQUE(pico_t, text));
CREATE TABLE IF NOT EXISTS acks(event_id INTEGER, ts_utc TEXT, user_initials TEXT, method TEXT);
CREATE TABLE IF NOT EXISTS users(phone_e164 TEXT PRIMARY KEY, name TEXT, optin_ts_utc TEXT, active INTEGER);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, event_id INTEGER, phone_e164 TEXT, ts_utc TEXT,
    body TEXT, status TEXT, provider_msg_id TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts_utc);
"""


def utc(epoch=None):
    return datetime.fromtimestamp(epoch if epoch is not None else time.time(), timezone.utc).isoformat(timespec="seconds")


def get(path):
    with urllib.request.urlopen(PICO + path, timeout=3) as r:
        return json.loads(r.read().decode())


def headline(text, p):
    """The same words the dashboard shows."""
    if text.startswith("CRITICAL") and "leak" in text:
        return "AIR LEAK DETECTED"
    if text.startswith("CRITICAL"):
        return "CRITICAL %s PRESSURE" % ("HIGH" if p > 30 else "LOW")
    if text.startswith("ADVISORY"):
        return "%s CUFF PRESSURE" % ("HIGH" if p > 30 else "LOW")
    if text.startswith("TECHNICAL"):
        return "CHECK SENSOR"
    return text.split(" - ")[0]           # PROBABLE COUGH / PROBABLE SUCTION


def send_sms(to, body):
    """Returns (status, provider id, error). Twilio REST API, no extra library."""
    if DRY_RUN:
        print("  DRY RUN text to %s: %s" % (to, body))
        return "dry-run", "", ""
    url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % SID
    data = urllib.parse.urlencode({"From": FROM, "To": to, "Body": body}).encode()
    req = urllib.request.Request(url, data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % (SID, TOKEN)).encode()).decode())
    err = ""
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.loads(r.read().decode())
                return j.get("status", "queued"), j.get("sid", ""), ""
        except Exception as e:                 # network down, wrong number, trial limits...
            err = str(e)
            time.sleep(2 ** attempt)
    return "failed", "", err


def sync_contacts(db):
    c = get("/contacts")
    offset = time.time() - c["now"]           # Pico clock -> wall clock
    seen = set()
    for u in c["contacts"]:
        seen.add(u["phone"])
        db.execute("INSERT INTO users(phone_e164,name,optin_ts_utc,active) VALUES(?,?,?,1) "
                   "ON CONFLICT(phone_e164) DO UPDATE SET name=excluded.name, active=1",
                   (u["phone"], u["name"], utc(u["optin_t"] + offset)))
    db.execute("UPDATE users SET active=0 WHERE phone_e164 NOT IN (%s)" % ",".join("?" * len(seen)) if seen
               else "UPDATE users SET active=0", tuple(seen))
    db.commit()


def handle_events(db, d, offset, first):
    for pico_t, text in d["events"]:
        kind = text.split(" ")[0]
        cur = db.execute("INSERT OR IGNORE INTO alarm_events(ts_utc,pico_t,kind,text,cmh2o) VALUES(?,?,?,?,?)",
                         (utc(pico_t + offset), pico_t, kind, text, d["p"]))
        if cur.rowcount == 0:
            continue                           # already stored
        eid = cur.lastrowid
        print("EVENT", utc(pico_t + offset), text)
        if kind == "ACKNOWLEDGED":
            who = text.split(" by ")[1].split(" - ")[0] if " by " in text else ""
            db.execute("INSERT INTO acks VALUES(?,?,?,?)", (eid, utc(pico_t + offset), who, "dashboard"))
        if not text.startswith(ALERT_PREFIXES) or (first and d["t"] - pico_t > OLD_EVENT_S):
            continue
        clock = datetime.fromtimestamp(pico_t + offset).strftime("%H:%M:%S")
        body = "CUFF MONITOR (bench prototype): %s at %s, cuff %.1f cmH2O. %s" % (
            headline(text, d["p"]), clock, d["p"], text.split(" - ", 1)[-1])
        for (phone,) in db.execute("SELECT phone_e164 FROM users WHERE active=1").fetchall():
            status, sid, err = send_sms(phone, body[:300])
            db.execute("INSERT INTO notifications(event_id,phone_e164,ts_utc,body,status,provider_msg_id,error) "
                       "VALUES(?,?,?,?,?,?,?)", (eid, phone, utc(), body, status, sid, err))
    db.commit()


def run():
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    print("Alert service. Pico:", PICO, " database:", DB, " texts:", "DRY RUN (no Twilio keys)" if DRY_RUN else "Twilio")
    first, last_contacts, down = True, 0, False
    while True:
        try:
            if time.time() - last_contacts >= CONTACTS_S:
                sync_contacts(db)
                last_contacts = time.time()
            d = get("/data")
            offset = time.time() - d["t"]
            db.execute("INSERT INTO readings VALUES(?,?,?,?,?,?)",
                       (utc(), d["t"], d["p"], int(d["ok"]), d["state"], d.get("event", "")))
            handle_events(db, d, offset, first)
            first = False
            if down:
                print("Pico reachable again")
                down = False
        except Exception as e:
            if not down:
                print("Cannot reach the Pico (%s). Is the laptop on CuffMonitor?" % e)
                down = True
        time.sleep(POLL_S)


def report(save_csv):
    """CR-4: time inside 20 to 30, every excursion with duration, alarms and who acknowledged."""
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT ts_utc, cmh2o, ok FROM readings ORDER BY ts_utc").fetchall()
    if not rows:
        print("No readings in", DB)
        return
    t = [datetime.fromisoformat(r[0]).timestamp() for r in rows]
    inside = sum(min(t[i + 1] - t[i], 5) for i in range(len(rows) - 1) if rows[i][2] and 20 <= rows[i][1] <= 30)
    total = sum(min(t[i + 1] - t[i], 5) for i in range(len(rows) - 1))
    exc, start = [], None
    for i, (ts, p, ok) in enumerate(rows):
        out = ok and not (20 <= p <= 30)
        if out and start is None:
            start, lo, hi = i, p, p
        if out:
            lo, hi = min(lo, p), max(hi, p)
        if start is not None and (not out or i == len(rows) - 1):
            exc.append((rows[start][0], ts, t[i] - t[start], lo, hi))
            start = None
    print("Shift report from %s to %s" % (rows[0][0], rows[-1][0]))
    print("Time inside 20-30 cmH2O: %.0f s of %.0f s (%.0f%%)" % (inside, total, 100 * inside / total if total else 0))
    print("\nExcursions (%d):" % len(exc))
    for e in exc:
        print("  %s to %s  %6.1f s  min %.1f  max %.1f cmH2O" % e)
    print("\nAlarms:")
    for ts, text in db.execute("SELECT ts_utc, text FROM alarm_events WHERE kind IN "
                               "('ADVISORY','CRITICAL','TECHNICAL','ACKNOWLEDGED','PROBABLE') ORDER BY ts_utc"):
        print("  %s  %s" % (ts, text))
    print("\nText messages: %d" % db.execute("SELECT count(*) FROM notifications").fetchone()[0])
    if save_csv:
        with open("shift_report.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["excursion start (UTC)", "end (UTC)", "duration (s)", "min cmH2O", "max cmH2O"])
            w.writerows([(a, b, round(c, 1), round(d, 2), round(e, 2)) for a, b, c, d, e in exc])
        print("Saved shift_report.csv")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report("--csv" in sys.argv)
    else:
        try:
            run()
        except KeyboardInterrupt:
            print("\nStopped. Data kept in", DB)
