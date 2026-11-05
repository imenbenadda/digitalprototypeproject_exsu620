"""Desktop simulation of cuff_pressure.py (no Pico needed).

Usage:  python3 sim_cuff_pressure.py ../firmware/cuff_pressure.py          (logic tests)
        python3 sim_cuff_pressure.py ../firmware/cuff_pressure.py 8765     (also serves the dashboard;
        open http://127.0.0.1:8765 and switch scenario with /scen/normal, cough, suction or leak)

Runs the real Alarm + Transient classes: runs the real Alarm + Transient classes and PAGE
on scripted pressure traces, serves the dashboard, and prints a logic test."""
import sys, types, json, random, re, http.server, threading, time as _time

src = open(sys.argv[1]).read()
T = [1000.0]
fake_time = types.ModuleType("time")
fake_time.monotonic = lambda: T[0]
fake_time.sleep = lambda s: None
for name in ["board", "busio", "supervisor", "wifi", "socketpool", "adafruit_httpserver"]:
    m = types.ModuleType(name)
    for attr in ["Server", "Request", "Response", "JSONResponse", "GET", "POST"]:
        setattr(m, attr, None)
    sys.modules[name] = m
sys.modules["time"] = fake_time
head = src[: src.index("# ---------------- start up: Wi-Fi")]
g = {}
exec(compile(head, "fw", "exec"), g)
sys.modules["time"] = _time
Alarm, Transient, PAGE = g["Alarm"], g["Transient"], g["PAGE"]


def run(trace, dt=0.1, ack_at=None, by="NO"):
    """trace: function of elapsed s -> cmH2O. Returns (alarm, transient, graph, latest)."""
    T[0] = 1000.0
    a, tr = Alarm(), Transient()
    a.set("NORMAL", "zero set", T[0]); a.same_since = T[0]
    graph, last = [], None
    t0 = T[0]; n = int(trace.dur / dt)
    hi = None
    for i in range(n):
        T[0] = t0 + i * dt
        p = trace(i * dt) + random.gauss(0, 0.08)
        a.update(T[0], p, int(p * 1000), True, dt)
        tr.update(T[0], p, True, dt, a)
        hi = (hi or T[0]) if p > 30 else None
        if ack_at is not None and abs(i * dt - ack_at) < dt / 2:
            a.acknowledge(by)
        if i % 5 == 0:
            graph.append((round(T[0], 1), round(p, 2)))
        last = p
    return a, tr, graph[-360:], last, hi


def data(a, tr, p, hi):
    now = T[0]
    return {"t": round(now, 1), "p": round(p, 2), "ok": True, "state": a.state, "reason": a.reason,
            "since": round(a.since, 1), "acked": a.acked, "by": a.acked_by, "inside": round(a.time_inside),
            "counted": round(a.time_counted), "events": a.events[-20:], "version": "2026-11-05 v8 - not calibrated",
            "high_for": round(now - hi, 1) if hi else 0,
            "out_for": round(now - a.out_since, 1) if a.out_since is not None else 0,
            "event": tr.label(now), "event_t": round(tr.label_time(), 1),
            "leak": a.state == "CRITICAL" and "leak" in a.reason,
            "rate": round(a.leak_rate(now, g["LEAK_WINDOW"]), 2),
            "in_for": round(now - a.in_since, 1) if a.in_since is not None else 0, "in_dwell": 10,
            "low": 20, "high": 30, "crit_low": 15, "high_delay": 5}


def tr_(f, dur):
    f.dur = dur
    return f

base = lambda t: 25 + 0.4 * __import__("math").sin(t / 7)
SCEN = {
    "normal": tr_(lambda t: base(t), 400),
    "cough": tr_(lambda t: base(t) + (17 if 398.6 <= t < 400.2 else 0), 400.4),
    "suction": tr_(lambda t: base(t) - (13 if 394 <= t < 401 else 0), 399),
    "leak": tr_(lambda t: base(t) if t < 300 else 25 - (t - 300) * 4 / 60, 420),
}
random.seed(3)
STATE = {}
for k, f in SCEN.items():
    a, tr, gr, p, hi = run(f)
    STATE[k] = (data(a, tr, p, hi), gr)
    print(k, a.state, "|", a.reason, "| label:", tr.label(T[0]), "| events:", [e[1] for e in a.events])

# ---- logic tests ----
def check(name, f, expect_state, expect_label=None, **kw):
    a, tr, _, _, _ = run(f, **kw)
    ok = a.state == expect_state and (expect_label is None or tr.label(T[0]) == expect_label)
    print("TEST", "PASS" if ok else "FAIL", name, "->", a.state, tr.label(T[0]), [e[1] for e in a.events][-3:])
check("cough 2 s at 25 -> NORMAL, labelled", tr_(lambda t: 25 + (15 if 100 <= t < 102 else 0), 104), "NORMAL", "COUGH")
check("suction 8 s -> NORMAL, labelled", tr_(lambda t: 25 - (12 if 100 <= t < 108 else 0), 110), "NORMAL", "SUCTION")
check("rise to 40 and stay -> label dropped, CRITICAL high", tr_(lambda t: 25 + (15 if t >= 100 else 0), 140), "CRITICAL", "")
check("slow leak 4/min -> CRITICAL leak", tr_(lambda t: 25 if t < 60 else 25 - (t - 60) * 4 / 60, 200), "CRITICAL")
check("drop to 10 and stay -> CRITICAL low", tr_(lambda t: 25 if t < 60 else 10, 140), "CRITICAL")
check("critical acked, back in range -> NORMAL", tr_(lambda t: 25 if t < 60 or t > 130 else 10, 150), "NORMAL", ack_at=125)

if len(sys.argv) > 2:
    cur = ["normal"]
    contacts, svc = [], [None]
    import urllib.parse
    def live():
        d = dict(STATE[cur[0]][0])
        d["contacts"] = [[c["name"], "+******" + c["phone"][-4:], c["id"]] for c in contacts]
        d["svc_age"] = round(_time.time() - svc[0], 1) if svc[0] else -1
        return d
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def send(self, body, ct):
            b = body.encode(); self.send_response(200); self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path.startswith("/data"): self.send(json.dumps(live()), "application/json")
            elif self.path.startswith("/contacts"):
                svc[0] = _time.time(); self.send(json.dumps({"now": STATE[cur[0]][0]["t"], "contacts": contacts}), "application/json")
            elif self.path.startswith("/history"): self.send(json.dumps({"t": STATE[cur[0]][0]["t"], "pts": STATE[cur[0]][1]}), "application/json")
            elif self.path.startswith("/scen/"): cur[0] = self.path.split("/")[2]; self.send("ok", "text/plain")
            else: self.send(PAGE, "text/html")
        def do_POST(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if self.path.startswith("/register"):
                ph = "+1" + q.get("phone", "")[-10:]
                contacts.append({"id": len(contacts) + 1, "name": q.get("name") or "Nurse", "phone": ph, "optin_t": STATE[cur[0]][0]["t"]})
            elif self.path.startswith("/unregister"):
                contacts[:] = [c for c in contacts if str(c["id"]) != q.get("id")]
            self.send('{"ok": true}', "application/json")
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
