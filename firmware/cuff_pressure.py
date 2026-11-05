# Cuff Pressure Monitor - live graph in Thonny + Wi-Fi dashboard with alarms
# EXSU 620 Digital Prototyping, Project 02 - runs on the Pico 2 W (CircuitPython 10) from Thonny
#
# Wiring
#   BMP280 sensor:  VIN-3V3 OUT   GND-GND   SCK-GP5   SDI-GP4
#
# Needs CIRCUITPY/lib/adafruit_httpserver (the web server library)
#
# How to run
#   1. Press the green Run button in Thonny
#   2. Keep the syringes off for the first 2 seconds: that sets zero (room air)
#   3. View > Plotter shows the live graph, with lines at 20 and 30 cmH2O
#   4. On the computer, join the Wi-Fi network CuffMonitor (password below)
#   5. Open a browser at  http://192.168.4.1  (the Shell prints the exact address)
#
# Alarm states (project card Figure 3)
#   NORMAL     green  - pressure inside 20 to 30 cmH2O
#   ADVISORY   amber, 3 pulses - outside 20 to 30 for 30 s. Back to NORMAL after 10 s inside.
#   CRITICAL   red, 10 pulses, latched - from ADVISORY: below 15 or above 35 for 30 s,
#              or falling faster than 2 cmH2O per minute.
#              Back to NORMAL only when acknowledged AND back inside 20 to 30.
#   TECHNICAL  cyan, chirp - sensor not answering, signal flat, line open to room air,
#              or zero (baseline) not set yet. Can happen from any state.
# Acknowledge: the button on the dashboard, or type a then Enter in the Thonny Shell.
#
# Dashboard (revision B, 2026-11-05): black medical-monitor screen that fits one view.
#   One large message at the top:
#     NORMAL (green, steady)
#     PROBABLE COUGH / PROBABLE SUCTION (orange, slow blink, one short low tone, no alarm state)
#     LOW / HIGH CUFF PRESSURE (orange, slow blink, medium alarm) = ADVISORY
#     AIR LEAK DETECTED / CRITICAL LOW / CRITICAL HIGH (red, fast blink, high alarm) = CRITICAL
#     CHECK SENSOR (cyan, steady, low tone) = TECHNICAL
#   Big cuff pressure number (green 20 to 30, yellow outside, red below 15 or above 30 for 5 s),
#   3 min trend, time in range, fall rate, events, Acknowledge, sound and Export buttons.
# Sound (IEC 60601-1-8 style): click once anywhere on the dashboard to turn it on.
#   Critical = high priority (10 beeps, every 5 s until acknowledged)
#   Advisory or above 30 for 5 s = medium priority (3 beeps, every 7 s); Technical = low priority (2 beeps)
#   Probable cough or suction = one low priority tone, no alarm (project card: under 5 s must not alarm).
# Text message alerts (v8): SMS ALERTS button on the dashboard stores name + phone + consent time
#   on the Pico. The laptop alert service (service/cuff_service.py) reads them from /contacts,
#   stores everything in SQLite and texts every alarm. The Pico itself never needs internet.

import time
import array
import struct
import sys
import board
import busio
import supervisor
import wifi
import socketpool
from adafruit_httpserver import Server, Request, Response, JSONResponse, GET, POST

# ---------------- settings you can change ----------------
WIFI_NAME = "CuffMonitor"        # the network the Pico creates
WIFI_PASSWORD = "cuffpressure"   # at least 8 characters
PORT = 80

LOW, HIGH = 20, 30               # cmH2O, safe window
CRIT_LOW, CRIT_HIGH = 15, 35     # cmH2O, critical limits
OUT_DWELL = 30                   # s outside the window before ADVISORY
IN_DWELL = 10                    # s back inside before ADVISORY clears
CRIT_DWELL = 30                  # s beyond critical limits before CRITICAL
LEAK_RATE = 2.0                  # cmH2O per minute falling = leak -> CRITICAL
LEAK_WINDOW = 60                 # s of data used to measure the falling rate
FLAT_TIME = 10                   # s of identical readings = sensor frozen
OPEN_AIR = 1.0                   # cmH2O; below this for OPEN_AIR_TIME = line open to air
OPEN_AIR_TIME = 5                # s
HIGH_ALERT_DELAY = 5             # s above 30 before the circle turns red and the dashboard beeps
SAMPLES_PER_SECOND = 10
GRAPH_SECONDS = 180              # how much history a freshly opened dashboard gets
LOG_SECONDS = 2 * 3600           # log kept for Export (one row per second, oldest dropped after 2 h)
VERSION = "2026-11-05 v8"
MAX_CONTACTS = 5                 # phones that get text message alerts (through the laptop alert service)

# Cough and suction labels (shown on the dashboard, never raise an alarm state)
COUGH_RISE = 5.0                 # cmH2O above the recent baseline = probable cough
SUCTION_DROP = 5.0               # cmH2O below the recent baseline = probable suction
TRANSIENT_BACK = 2.5             # back within this of the baseline = the event is over
COUGH_MAX = 5                    # s; a rise lasting longer is not a cough (alarm rules take over)
SUCTION_MAX = 10                 # s; a dip lasting longer is not a suction pass
TRANSIENT_SHOW = 8               # s the label stays on screen after the event
BASELINE_MIN = 15                # cmH2O; only label events once the cuff is inflated
BASELINE_TAU = 10                # s; how slowly the baseline follows the pressure

# Calibration against the cuff manometer (see README "Calibrate"). Start with 1.0 and 0.0.
#   shown pressure = CAL_GAIN x sensor pressure + CAL_OFFSET
#   In Excel: CAL_GAIN = SLOPE(manometer column, dashboard column)
#             CAL_OFFSET = INTERCEPT(manometer column, dashboard column)
CAL_GAIN = 1.0
CAL_OFFSET = 0.0
# ----------------------------------------------------------

PA_TO_CMH2O = 0.0101972  # 1 Pa = 0.0101972 cmH2O


class BMP280:
    """Reads pressure from the BMP280 over I2C (math from the Bosch datasheet)."""

    def __init__(self, i2c, addr=0x77):
        self.i2c = i2c
        self.addr = addr
        if self.read_reg(0xD0, 1)[0] != 0x58:
            raise OSError("pressure sensor not found - check the sensor wires")
        self.write_reg(0xE0, 0xB6)  # reset
        time.sleep(0.01)
        (self.T1, self.T2, self.T3,
         self.P1, self.P2, self.P3, self.P4, self.P5,
         self.P6, self.P7, self.P8, self.P9) = struct.unpack("<HhhHhhhhhhhh", bytes(self.read_reg(0x88, 24)))
        self.write_reg(0xF5, 2 << 2)                    # light smoothing filter
        self.write_reg(0xF4, (2 << 5) | (5 << 2) | 3)   # high resolution, continuous
        time.sleep(0.1)

    def read_reg(self, reg, n):
        buf = bytearray(n)
        self.i2c.writeto_then_readfrom(self.addr, bytes([reg]), buf)
        return buf

    def write_reg(self, reg, val):
        self.i2c.writeto(self.addr, bytes([reg, val]))

    def pressure_pa(self):
        d = self.read_reg(0xF7, 6)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)

        var1 = (((adc_t >> 3) - (self.T1 << 1)) * self.T2) >> 11
        var2 = (((((adc_t >> 4) - self.T1) * ((adc_t >> 4) - self.T1)) >> 12) * self.T3) >> 14
        t_fine = var1 + var2

        var1 = t_fine - 128000
        var2 = var1 * var1 * self.P6
        var2 = var2 + ((var1 * self.P5) << 17)
        var2 = var2 + (self.P4 << 35)
        var1 = ((var1 * var1 * self.P3) >> 8) + ((var1 * self.P2) << 12)
        var1 = (((1 << 47) + var1) * self.P1) >> 33
        if var1 == 0:
            return 0
        p = 1048576 - adc_p
        p = (((p << 31) - var2) * 3125) // var1
        var1 = (self.P9 * (p >> 13) * (p >> 13)) >> 25
        var2 = (self.P8 * p) >> 19
        p = ((p + var1 + var2) >> 8) + (self.P7 << 4)
        return p / 256


class Alarm:
    """The Figure 3 state machine. Call update() once per sample."""

    def __init__(self):
        self.state = "TECHNICAL"      # baseline not set yet
        self.before_technical = "NORMAL"
        self.reason = "zero not set yet"
        self.since = time.monotonic()  # when the current state started
        self.acked = False
        self.acked_by = ""
        self.out_since = None         # when pressure left 20 to 30
        self.in_since = None          # when pressure came back inside 20 to 30
        self.crit_since = None        # when pressure went below 15 or above 35
        self.history = []             # (time, cmH2O) once per second, for the leak rate
        self.last_hist = -1
        self.open_since = None
        self.same_since = None
        self.last_raw = None
        self.events = []              # recent state changes and acknowledgements, for the dashboard
        self.time_inside = 0.0        # s spent inside 20 to 30 (CR-4)
        self.time_counted = 0.0       # s counted since zero was set

    def log(self, now, text):
        self.events.append((round(now, 1), text))
        self.events = self.events[-300:]
        print("EVENT:", text)

    def set(self, state, reason, now=None):
        if now is None:
            now = time.monotonic()
        if state != self.state:
            self.state = state
            self.reason = reason
            self.since = now
            if state == "CRITICAL":
                self.acked = False
                self.acked_by = ""
            self.log(now, "%s - %s" % (state, reason))

    def acknowledge(self, by=""):
        if self.state == "CRITICAL" and not self.acked:
            self.acked = True
            self.acked_by = by
            self.log(time.monotonic(), "ACKNOWLEDGED%s - stays red until pressure is back in the safe window"
                     % ((" by " + by) if by else ""))
            return True
        return False

    def leak_rate(self, now, window):
        """Falling rate in cmH2O per minute over the last `window` s (positive = falling)."""
        pts = [(t, p) for t, p in self.history if now - t <= window]
        if len(pts) < window * 0.9:
            return 0
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mp = sum(p for _, p in pts) / n
        num = sum((t - mt) * (p - mp) for t, p in pts)
        den = sum((t - mt) ** 2 for t, _ in pts)
        return -num / den * 60 if den else 0

    def update(self, now, cmh2o, raw_pa, sensor_ok, dt):
        # ---- TECHNICAL checks (any state) ----
        problem = None
        if not sensor_ok:
            problem = "pressure sensor not answering"
        else:
            if raw_pa != self.last_raw:
                self.same_since = now
                self.last_raw = raw_pa
            if now - self.same_since >= FLAT_TIME:
                problem = "signal flat - sensor frozen"
            if abs(cmh2o) < OPEN_AIR:
                if self.open_since is None:
                    self.open_since = now
                if now - self.open_since >= OPEN_AIR_TIME:
                    problem = "reading room air - sensing line open or cuff empty"
            else:
                self.open_since = None

        if problem:
            if self.state != "TECHNICAL":
                self.before_technical = self.state
            self.set("TECHNICAL", problem, now)
            self.reason = problem
            self.out_since = self.in_since = self.crit_since = None
            return
        if self.state == "TECHNICAL":
            self.set(self.before_technical, "sensing OK again", now)

        # ---- timers ----
        inside = LOW <= cmh2o <= HIGH
        self.time_counted += dt
        if inside:
            self.time_inside += dt
            self.out_since = None
            if self.in_since is None:
                self.in_since = now
        else:
            self.in_since = None
            if self.out_since is None:
                self.out_since = now

        if cmh2o < CRIT_LOW or cmh2o > CRIT_HIGH:
            if self.crit_since is None:
                self.crit_since = now
        else:
            self.crit_since = None

        if now - self.last_hist >= 1:
            self.history.append((now, cmh2o))
            self.history = self.history[-LEAK_WINDOW - 5:]
            self.last_hist = now

        # ---- transitions ----
        if self.state == "NORMAL":
            if self.out_since is not None and now - self.out_since >= OUT_DWELL:
                self.set("ADVISORY", "outside the safe window too long", now)
        elif self.state == "ADVISORY":
            if self.in_since is not None and now - self.in_since >= IN_DWELL:
                self.set("NORMAL", "back inside the safe window", now)
            elif self.crit_since is not None and now - self.crit_since >= CRIT_DWELL:
                self.set("CRITICAL", "beyond the critical limits too long", now)
            elif (self.leak_rate(now, LEAK_WINDOW) > LEAK_RATE
                  and self.leak_rate(now, LEAK_WINDOW / 3) > LEAK_RATE):
                # still falling over the whole minute AND right now = a leak,
                # not a one-off drop that has already settled
                self.set("CRITICAL", "pressure falling fast - possible leak", now)
        elif self.state == "CRITICAL":
            if self.acked and inside:
                self.set("NORMAL", "acknowledged and back in range", now)


class Transient:
    """Labels short spikes (probable cough) and short dips (probable suction).
    Only a label for the dashboard and the log: the Figure 3 alarm states are not changed."""

    def __init__(self):
        self.base = None       # slow moving baseline, cmH2O
        self.kind = ""         # "COUGH" or "SUCTION" while the event is going on
        self.start = 0
        self.peak = 0.0
        self.shown = ""        # label still on screen after the event ended
        self.shown_until = 0

    def update(self, now, p, ok, dt, alarm):
        if not ok:
            self.kind = ""
            return
        if self.base is None:
            self.base = p
            return
        d = p - self.base
        if not self.kind:
            if self.base >= BASELINE_MIN and d > COUGH_RISE:
                self.kind, self.start, self.peak = "COUGH", now, d
                self.shown, self.shown_until = "COUGH", now + COUGH_MAX + TRANSIENT_SHOW
            elif self.base >= BASELINE_MIN and d < -SUCTION_DROP:
                self.kind, self.start, self.peak = "SUCTION", now, d
                self.shown, self.shown_until = "SUCTION", now + SUCTION_MAX + TRANSIENT_SHOW
            else:
                self.base += (p - self.base) * min(1.0, dt / BASELINE_TAU)
            return
        if abs(d) > abs(self.peak):
            self.peak = d
        limit = COUGH_MAX if self.kind == "COUGH" else SUCTION_MAX
        if abs(d) <= TRANSIENT_BACK:
            alarm.log(now, "PROBABLE %s - %.1f s, peak %+.1f cmH2O, no alarm" % (self.kind, now - self.start, self.peak))
            self.shown_until = now + TRANSIENT_SHOW
            self.kind = ""
        elif now - self.start > limit:
            alarm.log(now, "not a %s - lasted over %d s, alarm rules apply" % (self.kind.lower(), limit))
            self.kind, self.shown, self.base = "", "", p

    def label(self, now):
        if self.kind:
            return self.kind
        return self.shown if now < self.shown_until else ""

    def label_time(self):
        return self.start


def shell_ack_requested():
    """True when 'a' was typed in the Thonny Shell."""
    typed = False
    while supervisor.runtime.serial_bytes_available:
        if sys.stdin.read(1).lower() == "a":
            typed = True
    return typed


# ---------------- dashboard page (served by the Pico, no internet needed) ----------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cuff Pressure Monitor</title>
<style>
:root{--bg:#000;--panel:#050a17;--edge:#1b2a4d;--head:#0b1730;--lab:#8fb0e8;--mute:#6f7f9c;--ink:#e8eefc;
--G:#2ee66b;--Y:#ffd21a;--O:#ff9500;--R:#ff2d2d;--C:#22d3ee}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--ink);overflow:hidden;
font-family:"Helvetica Neue",Helvetica,Arial,sans-serif}
#app{height:100vh;display:grid;grid-template-rows:44px 25vh 1fr 18vh 58px;gap:6px;padding:6px}
.top{display:flex;align-items:center;gap:16px;padding:0 12px;border:1px solid var(--edge);background:var(--head);border-radius:4px}
.top b{letter-spacing:.12em;font-size:15px;color:#cfe0ff}
.top .lab{font-size:12px;color:var(--mute)}
.top .sp{flex:1}
#clock{font-size:20px;font-variant-numeric:tabular-nums;color:#cfe0ff}
#link{font-size:12px;color:var(--G)}
.panel{border:1px solid var(--edge);background:var(--panel);border-radius:4px;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.ph{height:24px;flex:none;display:flex;align-items:center;padding:0 10px;font-size:12px;font-weight:700;letter-spacing:.1em;color:var(--lab);
background:var(--head) repeating-linear-gradient(120deg,transparent 0 6px,rgba(143,176,232,.10) 6px 9px) no-repeat right/90px 100%;border-bottom:1px solid var(--edge)}
.ph span{margin-left:auto;font-weight:400;color:var(--mute);letter-spacing:.04em}
#msgbox{border:3px solid var(--G);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;border-radius:4px;padding:4px 12px;background:#000}
#msg{font-size:min(10.5vh,7.2vw);font-weight:800;letter-spacing:.04em;line-height:1;color:var(--G)}
#sub{font-size:min(2.8vh,1.9vw);margin-top:1.2vh;color:#cfd8ea}
#since{font-size:min(2.2vh,1.5vw);margin-top:.6vh;color:var(--mute)}
.blinkR{animation:bR .5s steps(1,end) infinite}
.blinkO{animation:bO 1.6s steps(1,end) infinite}
@keyframes bR{50%{filter:brightness(.35)}}
@keyframes bO{50%{filter:brightness(.45)}}
.mid{display:grid;grid-template-columns:2.1fr 1fr;gap:6px;min-height:0}
canvas{flex:1;width:100%;min-height:0;display:block}
.num{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;position:relative}
#pnum{font-size:min(19vh,11vw);font-weight:300;line-height:1;font-variant-numeric:tabular-nums;color:var(--G)}
#punit{font-size:min(2.6vh,1.8vw);color:var(--mute);margin-top:.6vh}
#pzone{font-size:min(2.4vh,1.7vw);margin-top:.8vh;font-weight:700;letter-spacing:.08em}
.lim{position:absolute;right:12px;top:8px;text-align:right;font-size:13px;color:var(--mute);line-height:1.35}
.lim i{font-style:normal;color:#cfe0ff}
.low3{display:grid;grid-template-columns:1fr 1fr 1.6fr;gap:6px;min-height:0}
.big{flex:1;display:flex;align-items:center;justify-content:center;gap:10px}
.big .v{font-size:min(7.5vh,5vw);font-weight:300;font-variant-numeric:tabular-nums}
.big .u{font-size:min(2.2vh,1.5vw);color:var(--mute)}
.note{font-size:12px;color:var(--mute);text-align:center;padding:0 8px 6px}
#ev{list-style:none;margin:0;padding:6px 10px;font-size:min(1.9vh,1.3vw);overflow:hidden;flex:1}
#ev li{padding:2px 0;border-bottom:1px solid #0f1a33;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#ev li b{color:var(--lab);font-weight:400;font-variant-numeric:tabular-nums;margin-right:8px}
.bar{display:grid;grid-template-columns:2.2fr 1fr 1fr 1fr;gap:6px}
.bar button,.bar .ackw{border:1px solid var(--edge);background:var(--head);color:#cfe0ff;border-radius:4px;font-size:15px;font-weight:700;letter-spacing:.08em;cursor:pointer}
.bar button:disabled{opacity:.4;cursor:default}
.ackw{display:flex;align-items:center;gap:8px;padding:0 8px}
.ackw input{width:110px;height:38px;font-size:16px;background:#000;color:#fff;border:1px solid var(--edge);border-radius:4px;padding:0 8px}
#ack{flex:1;height:44px;border:2px solid var(--edge)}
#ack.live{background:var(--R);border-color:#fff;color:#fff}
#snd.off{background:var(--O);color:#000;border-color:var(--O)}
#smsbtn{border:1px solid var(--edge);background:#000;color:#cfe0ff;border-radius:4px;font-size:12px;font-weight:700;letter-spacing:.08em;padding:6px 10px;cursor:pointer}
#smsbtn i{font-style:normal;font-weight:400;color:var(--mute)}
#sms{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;z-index:9}
#sms .box{width:min(520px,92vw);background:var(--panel);border:1px solid var(--edge);border-radius:4px}
#sms .in{padding:14px;display:grid;gap:10px;font-size:14px}
#sms input[type=text],#sms input[type=tel]{height:38px;font-size:16px;background:#000;color:#fff;border:1px solid var(--edge);border-radius:4px;padding:0 8px;width:100%}
#sms button{height:38px;border:1px solid var(--edge);background:var(--head);color:#cfe0ff;border-radius:4px;font-weight:700;letter-spacing:.06em;cursor:pointer}
#sms ul{list-style:none;margin:0;padding:0}#sms li{display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #0f1a33}
#smsmsg{color:var(--mute);font-size:12px}
#expmsg{position:fixed;bottom:70px;right:12px;font-size:12px;color:var(--mute)}
</style></head><body><div id="app">
<div class="top"><b>CUFF PRESSURE MONITOR</b><span class="lab">Endotracheal cuff &middot; bench prototype, not for clinical use</span>
 <span class="sp"></span><button id="smsbtn">SMS ALERTS <i id="smsst">0 phones</i></button><span id="link">&#9679; CONNECTING</span><span id="clock">--:--:--</span></div>
<div id="msgbox"><div id="msg">CONNECTING</div><div id="sub">Waiting for the Pico</div><div id="since"></div></div>
<div class="mid">
 <div class="panel"><div class="ph">CUFF PRESSURE TREND<span>last 3 min &middot; green band 20&ndash;30 &middot; red dashed 15 / 35 cmH2O</span></div><canvas id="c"></canvas></div>
 <div class="panel"><div class="ph">CUFF P<span>cmH2O</span></div>
  <div class="num"><div class="lim">HI <i>30</i><br>LO <i>20</i></div><div id="pnum">--.-</div><div id="punit">cmH2O</div><div id="pzone"></div></div></div>
</div>
<div class="low3">
 <div class="panel"><div class="ph">TIME IN RANGE<span>20&ndash;30</span></div><div class="big"><span class="v" id="tir">--</span><span class="u">%</span></div><div class="note" id="tirn"></div></div>
 <div class="panel"><div class="ph">FALL RATE<span>last 60 s</span></div><div class="big"><span class="v" id="rate">--</span><span class="u">cmH2O/min</span></div><div class="note">leak alarm above 2</div></div>
 <div class="panel"><div class="ph">EVENTS<span id="ver"></span></div><ul id="ev"><li>none yet</li></ul></div>
</div>
<div class="bar">
 <div class="ackw"><input id="by" maxlength="12" placeholder="Initials"><button id="ack" disabled>ACKNOWLEDGE</button></div>
 <button id="snd" class="off">CLICK TO ENABLE SOUND</button>
 <button id="test">TEST SOUND</button>
 <button id="exp">EXPORT CSV</button>
</div>
<div id="expmsg"></div>
<div id="sms"><div class="box"><div class="ph">TEXT MESSAGE ALERTS<span>course participants only, no patient data</span></div><div class="in">
 <div>Each alarm (advisory, critical, leak, technical, probable cough or suction) is texted to every phone below by the laptop alert service. <span id="svcst"></span></div>
 <input type="text" id="sname" maxlength="20" placeholder="Name (e.g. Nurse Kayla)">
 <input type="tel" id="sphone" maxlength="16" placeholder="Mobile number, e.g. 514 555 0123">
 <label><input type="checkbox" id="sok"> I agree to receive alarm text messages from this bench prototype (consent time is stored)</label>
 <button id="sadd">ADD PHONE</button><span id="smsmsg"></span>
 <ul id="slist"></ul>
 <button id="sclose">CLOSE</button></div></div></div>
</div><script>
var COL={G:'#2ee66b',Y:'#ffd21a',O:'#ff9500',R:'#ff2d2d',C:'#22d3ee',W:'#e8eefc'};
var pts=[],offset=0,fails=0,lastD=null,lastLabel='';
function $(i){return document.getElementById(i)}
function clock(t){return new Date(Date.now()-(offset-t)*1000).toLocaleTimeString()}
function dur(s){s=Math.max(0,Math.round(s));var m=Math.floor(s/60);return m?m+' min '+(s%60)+' s':s+' s'}
/* ---- sound: IEC 60601-1-8 style patterns ---- */
var A={ctx:null,wave:null,on:false,muted:false,busy:0},nextAt=0,lastKind=null;
function soundUI(){var b=$('snd');if(!A.on){b.className='off';b.textContent='CLICK TO ENABLE SOUND'}
 else{b.className=A.muted?'off':'';b.textContent=A.muted?'SOUND MUTED':'MUTE SOUND'}}
function unlock(){try{if(!A.ctx){var AC=window.AudioContext||window.webkitAudioContext;A.ctx=new AC();
 var re=new Float32Array([0,1,.6,.45,.3,.2]),im=new Float32Array(re.length);A.wave=A.ctx.createPeriodicWave(re,im)}
 A.ctx.resume();if(!A.on){A.on=true;tone(0,.12,660,.5)}}catch(e){}soundUI()}
function tone(at,len,f,vol){var c=A.ctx,o=c.createOscillator(),g=c.createGain(),t=c.currentTime+at+.02;o.setPeriodicWave(A.wave);o.frequency.value=f;
 g.gain.setValueAtTime(0,t);g.gain.linearRampToValueAtTime(vol,t+.015);g.gain.setValueAtTime(vol,t+len-.025);g.gain.linearRampToValueAtTime(0,t+len);
 o.connect(g);g.connect(c.destination);o.start(t);o.stop(t+len+.05)}
var PAT={high:{on:[0,.25,.5,1,1.25,2.25,2.5,2.75,3.25,3.5],f:880,len:.15,vol:.6},med:{on:[0,.4,.8],f:660,len:.2,vol:.5},low:{on:[0,.4],f:520,len:.2,vol:.4}};
function play(k){if(!A.on||A.muted||!A.ctx)return false;var now=A.ctx.currentTime;if(now<A.busy)return false;var P=PAT[k];
 if(A.ctx.state!=='running')A.ctx.resume();P.on.forEach(function(s){tone(s,P.len,P.f,P.vol)});A.busy=now+P.on[P.on.length-1]+P.len+.1;return true}
function sounds(kind,every){var now=Date.now()/1000;if(kind!==lastKind){nextAt=0;lastKind=kind}
 if(kind&&now>=nextAt&&play(kind))nextAt=every?now+every:1e12}
['click','keydown','touchstart'].forEach(function(e){document.addEventListener(e,function(ev){var was=A.on;unlock();
 if(ev.type==='click'&&ev.target.id==='snd'&&was){A.muted=!A.muted;soundUI()}},true)});
$('test').onclick=function(){unlock();A.busy=0;play('high')};
$('ack').onclick=function(){var b=encodeURIComponent($('by').value.replace(/[^A-Za-z ]/g,''));fetch('/ack?by='+b,{method:'POST'}).then(poll)};
/* ---- what the big message says ---- */
function judge(d){var inside=d.ok&&d.p>=d.low&&d.p<=d.high,r;
 if(d.state==='TECHNICAL')r={t:'CHECK SENSOR',c:'C',b:'',s:d.reason,k:null};
 else if(d.state==='CRITICAL'&&d.leak)r={t:'AIR LEAK DETECTED',c:'R',b:'R',s:'Pressure falling '+d.rate.toFixed(1)+' cmH2O/min. Check pilot line, valve and cuff.',k:'high'};
 else if(d.state==='CRITICAL')r={t:d.p>d.high?'CRITICAL HIGH PRESSURE':'CRITICAL LOW PRESSURE',c:'R',b:'R',s:d.reason,k:'high'};
 else if(d.state==='ADVISORY')r={t:(d.p>d.high?'HIGH':'LOW')+' CUFF PRESSURE',c:'O',b:'O',s:inside?'Back in range, clears in '+Math.max(0,Math.ceil(d.in_dwell-d.in_for))+' s':'Outside 20\u201330 cmH2O for over 30 s. Adjust cuff volume.',k:inside?null:'med'};
 else if(d.event==='COUGH')r={t:'PROBABLE COUGH',c:'O',b:'O',s:'Short pressure spike. No action needed unless it lasts over 5 s.',k:'once'};
 else if(d.event==='SUCTION')r={t:'PROBABLE SUCTION',c:'O',b:'O',s:'Short pressure dip. No action needed unless it lasts over 10 s.',k:'once'};
 else r={t:'NORMAL',c:'G',b:'',s:inside?'Cuff pressure inside 20\u201330 cmH2O':'Outside 20\u201330 for '+dur(d.out_for)+' \u00b7 alarm at 30 s',k:null};
 if(d.state==='CRITICAL'&&d.acked){r.b='';r.k=null;r.s='ACKNOWLEDGED'+(d.by?' by '+d.by:'')+' \u00b7 stays red until back in 20\u201330'}
 return r}
function zone(d){if(!d.ok)return['W','NO READING'];var p=d.p;if(p<d.crit_low)return['R','VERY LOW'];if(p<d.low)return['Y','LOW'];
 if(p<=d.high)return['G','IN RANGE'];if(d.high_for>=d.high_delay)return['R','HIGH '+Math.round(d.high_for)+' s'];return['Y','HIGH']}
function paint(r){var box=$('msgbox'),m=$('msg'),col=COL[r.c];
 box.style.borderColor=col;
 if(r.c==='R'||r.c==='O'){box.style.background=col;m.style.color='#000';$('sub').style.color='#000';$('since').style.color='#111'}
 else{box.style.background='#000';m.style.color=col;$('sub').style.color='#cfd8ea';$('since').style.color='#6f7f9c'}
 box.className=r.b==='R'?'blinkR':r.b==='O'?'blinkO':'';m.textContent=r.t;$('sub').textContent=r.s}
function show(d){offset=d.t;lastD=d;phones(d);var r=judge(d);paint(r);
 $('since').textContent=d.state==='NORMAL'&&!d.event?'':'since '+clock(d.event?d.event_t:d.since);
 var z=zone(d);$('pnum').textContent=d.ok?d.p.toFixed(1):'--.-';$('pnum').style.color=COL[z[0]];$('pzone').textContent=z[1];$('pzone').style.color=COL[z[0]];
 $('tir').textContent=d.counted>0?Math.round(100*d.inside/d.counted):'--';$('tirn').textContent=d.counted>0?dur(d.inside)+' of '+dur(d.counted):'';
 $('rate').textContent=d.ok?Math.max(0,d.rate).toFixed(1):'--';$('rate').style.color=d.rate>2?COL.R:'#e8eefc';
 $('ack').disabled=!(d.state==='CRITICAL'&&!d.acked);$('ack').className=$('ack').disabled?'':'live';
 $('ver').textContent='v '+d.version;
 var ev=$('ev');ev.innerHTML='';d.events.slice(-8).reverse().forEach(function(e){var li=document.createElement('li');li.innerHTML='<b></b>';li.firstChild.textContent=clock(e[0]);li.appendChild(document.createTextNode(e[1]));ev.appendChild(li)});
 if(!d.events.length)ev.innerHTML='<li>none yet</li>';
 if(r.k==='once'){if(d.event!==lastLabel)play('low')}else if(r.k==='high')sounds('high',5);else if(r.k==='med')sounds('med',7);else sounds(null,0);
 if(d.state==='TECHNICAL'&&lastState!=='TECHNICAL')play('low');lastState=d.state;lastLabel=d.event;
 if(d.ok)pts.push([d.t,d.p]);while(pts.length&&d.t-pts[0][0]>200)pts.shift();draw(COL[r.c])}
var lastState=null;
function draw(col){var cv=$('c'),k=window.devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;cv.width=W*k;cv.height=H*k;
 var g=cv.getContext('2d');g.scale(k,k);g.clearRect(0,0,W,H);var L=38,R=10,T=8,B=20,w=W-L-R,h=H-T-B,ymax=45,span=180,tn=offset;
 function Y(v){return T+h*(1-Math.max(0,Math.min(ymax,v))/ymax)}function X(t){return L+w*(1-(tn-t)/span)}
 g.fillStyle='rgba(46,230,107,.10)';g.fillRect(L,Y(30),w,Y(20)-Y(30));
 g.strokeStyle='#13213f';g.lineWidth=1;g.fillStyle='#6f7f9c';g.font='11px Helvetica,Arial,sans-serif';g.textAlign='right';
 for(var v=0;v<=ymax;v+=5){g.beginPath();g.moveTo(L,Y(v));g.lineTo(L+w,Y(v));g.stroke();if(v%10==0||v==15||v==25||v==35)g.fillText(v,L-6,Y(v)+4)}
 g.textAlign='center';for(var s=0;s<=span;s+=30){g.beginPath();g.moveTo(X(tn-s),T);g.lineTo(X(tn-s),T+h);g.stroke();g.fillText(s?'-'+s+' s':'now',X(tn-s),H-5)}
 g.strokeStyle='#ff2d2d';g.setLineDash([6,4]);[15,35].forEach(function(v){g.beginPath();g.moveTo(L,Y(v));g.lineTo(L+w,Y(v));g.stroke()});g.setLineDash([]);
 g.strokeStyle='#2ee66b';[20,30].forEach(function(v){g.beginPath();g.moveTo(L,Y(v));g.lineTo(L+w,Y(v));g.stroke()});
 g.strokeStyle=col==COL.R||col==COL.O?col:'#ffd21a';g.lineWidth=2.2;g.beginPath();var f=true;
 pts.forEach(function(p){if(tn-p[0]>span)return;var x=X(p[0]),y=Y(p[1]);if(f){g.moveTo(x,y);f=false}else g.lineTo(x,y)});g.stroke()}
function poll(){fetch('/data',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){fails=0;$('link').textContent='\u25cf LINKED';$('link').style.color=COL.G;show(d)})
 .catch(function(){fails++;if(fails>2){$('link').textContent='\u25cf NO LINK';$('link').style.color=COL.R;
  paint({t:'NO SIGNAL FROM MONITOR',c:'C',b:'',s:'Is this computer still on the CuffMonitor Wi-Fi, and is the Pico running?'});$('since').textContent='';sounds('low',10)}})}
fetch('/history',{cache:'no-store'}).then(function(r){return r.json()}).then(function(h){pts=h.pts.concat(pts);offset=h.t;draw('#ffd21a')}).catch(function(){});
setInterval(function(){$('clock').textContent=new Date().toLocaleTimeString()},1000);
/* ---- export to Excel (.csv) ---- */
function two(n){return(n<10?'0':'')+n}
function wall(t,now){var d=new Date(Date.now()-(now-t)*1000);return[d.getFullYear()+'-'+two(d.getMonth()+1)+'-'+two(d.getDate()),two(d.getHours())+':'+two(d.getMinutes())+':'+two(d.getSeconds())]}
function q(x){x=String(x);return /[",\n]/.test(x)?'"'+x.replace(/"/g,'""')+'"':x}
$('exp').onclick=function(){var btn=this,rows=[],evs=[],now=0,from=0;btn.disabled=true;$('expmsg').textContent='Collecting data...';
 function part(){fetch('/log?from='+from,{cache:'no-store'}).then(function(r){return r.json()}).then(function(L){now=L.now;if(from===0)evs=L.events;
  for(var i=0;i<L.t.length;i++)rows.push([L.t[i],L.p[i],L.s[i]]);from+=L.t.length;$('expmsg').textContent='Collecting data... '+rows.length+' rows';
  if(L.t.length&&from<L.total)part();else save()}).catch(function(){$('expmsg').textContent='Export failed. Is the computer still on CuffMonitor?';btn.disabled=false})}
 function save(){var N=['NORMAL','ADVISORY','CRITICAL','TECHNICAL'],out=['Date,Time,Seconds since start,Pressure (cmH2O),Alarm state,In range 20-30,Event'],ei=0;
  evs.sort(function(a,b){return a[0]-b[0]});
  rows.forEach(function(r,k){var w=wall(r[0],now),ev=[],lim=k+1<rows.length?rows[k+1][0]:1e12;while(ei<evs.length&&evs[ei][0]<lim){ev.push(evs[ei][1]);ei++}
   var ok=r[1]!==null;out.push([w[0],w[1],r[0].toFixed(1),ok?r[1].toFixed(2):'',N[r[2]]||'',ok?(r[1]>=20&&r[1]<=30?'yes':'no'):'',q(ev.join(' | '))].join(','))});
  var n=new Date(),name='cuff_log_'+n.getFullYear()+'-'+two(n.getMonth()+1)+'-'+two(n.getDate())+'_'+two(n.getHours())+two(n.getMinutes())+'.csv';
  var blob=new Blob(['\ufeff'+out.join('\r\n')],{type:'text/csv'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;
  document.body.appendChild(a);a.click();setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},2000);
  $('expmsg').textContent='Saved '+name+' ('+rows.length+' rows) to Downloads.';btn.disabled=false}
 part()};
function phones(d){var n=(d.contacts||[]).length;$('smsst').textContent=n+' phone'+(n==1?'':'s')+(n?(d.svc_age>=0&&d.svc_age<15?' \u00b7 service on':' \u00b7 SERVICE OFF'):'');
 $('smsst').style.color=n&&!(d.svc_age>=0&&d.svc_age<15)?COL.O:'';$('svcst').textContent=d.svc_age>=0&&d.svc_age<15?'Alert service: connected.':'Alert service: NOT connected (start cuff_service.py on the laptop).';
 var ul=$('slist');ul.innerHTML='';(d.contacts||[]).forEach(function(c){var li=document.createElement('li');li.textContent=c[0]+'  '+c[1];var b=document.createElement('button');b.textContent='REMOVE';
  b.onclick=function(){fetch('/unregister?id='+c[2],{method:'POST'}).then(poll)};li.appendChild(b);ul.appendChild(li)})}
$('smsbtn').onclick=function(){$('sms').style.display='flex'};$('sclose').onclick=function(){$('sms').style.display='none'};
$('sadd').onclick=function(){var ph=$('sphone').value.replace(/[^0-9]/g,''),nm=$('sname').value.replace(/[^A-Za-z ]/g,'').trim();
 if(!$('sok').checked){$('smsmsg').textContent='Tick the consent box first.';return}if(ph.length<10){$('smsmsg').textContent='Enter a 10 digit number.';return}
 fetch('/register?phone='+ph+'&name='+encodeURIComponent(nm),{method:'POST'}).then(function(r){return r.json()}).then(function(j){$('smsmsg').textContent=j.ok?'Added.':'Not added: '+j.error;
  if(j.ok){$('sphone').value='';$('sok').checked=false}poll()})};
soundUI();setInterval(poll,500);poll();window.onresize=function(){if(lastD)draw(COL[judge(lastD).c])};
</script></body></html>

"""


# ---------------- start up: Wi-Fi network + web server ----------------
try:
    wifi.radio.stop_ap()      # in case it is still on from the last run
except Exception:
    pass
wifi.radio.start_ap(ssid=WIFI_NAME, password=WIFI_PASSWORD)
address = str(wifi.radio.ipv4_address_ap)
pool = socketpool.SocketPool(wifi.radio)
server = Server(pool, debug=False)

alarm = Alarm()
transient = Transient()
contacts = []       # [id, name, phone E.164, consent time (monotonic)] for text message alerts
next_contact = 1
svc_seen = None     # last time the laptop alert service checked in
latest = {"p": 0.0, "ok": False}
high_since = None   # when pressure went above HIGH (for the circle and the high beep)
graph = []          # (time, cmH2O) twice per second for the dashboard history
STATE_CODES = {"NORMAL": 0, "ADVISORY": 1, "CRITICAL": 2, "TECHNICAL": 3}
log_t = array.array("f", [0.0] * LOG_SECONDS)   # ring buffer, one row per second, for Export
log_p = array.array("f", [0.0] * LOG_SECONDS)
log_s = bytearray(LOG_SECONDS)
log_head = 0        # where the next row goes
log_count = 0
last_log = -1


def log_row(now, cmh2o, ok, state):
    global log_head, log_count
    log_t[log_head] = now
    log_p[log_head] = cmh2o if ok else -999.0
    log_s[log_head] = STATE_CODES.get(state, 3)
    log_head = (log_head + 1) % LOG_SECONDS
    log_count = min(log_count + 1, LOG_SECONDS)
last_graph = -1


@server.route("/", GET)
def home(request: Request):
    return Response(request, PAGE, content_type="text/html")


@server.route("/data", GET)
def data(request: Request):
    return JSONResponse(request, {
        "t": round(time.monotonic(), 1),
        "p": round(latest["p"], 2),
        "ok": latest["ok"],
        "state": alarm.state,
        "reason": alarm.reason,
        "since": round(alarm.since, 1),
        "acked": alarm.acked,
        "by": alarm.acked_by,
        "inside": round(alarm.time_inside),
        "counted": round(alarm.time_counted),
        "events": alarm.events[-20:],
        "version": VERSION + (" - calibrated: gain %.3f, offset %.2f" % (CAL_GAIN, CAL_OFFSET)
                              if (CAL_GAIN != 1.0 or CAL_OFFSET != 0.0) else " - not calibrated"),
        "high_for": round(time.monotonic() - high_since, 1) if high_since is not None else 0,
        "out_for": round(time.monotonic() - alarm.out_since, 1) if alarm.out_since is not None else 0,
        "event": transient.label(time.monotonic()),
        "event_t": round(transient.label_time(), 1),
        "leak": alarm.state == "CRITICAL" and "leak" in alarm.reason,
        "rate": round(alarm.leak_rate(time.monotonic(), LEAK_WINDOW), 2),
        "in_for": round(time.monotonic() - alarm.in_since, 1) if alarm.in_since is not None else 0,
        "in_dwell": IN_DWELL,
        "contacts": [[c[1], "+" + "*" * (len(c[2]) - 5) + c[2][-4:], c[0]] for c in contacts],
        "svc_age": round(time.monotonic() - svc_seen, 1) if svc_seen is not None else -1,
        "low": LOW, "high": HIGH, "crit_low": CRIT_LOW, "high_delay": HIGH_ALERT_DELAY,
    })


@server.route("/history", GET)
def history(request: Request):
    return JSONResponse(request, {"t": round(time.monotonic(), 1), "pts": graph})


@server.route("/log", GET)
def log_part(request: Request):
    """Rows for Export, 400 at a time: /log?from=0, /log?from=400, ..."""
    try:
        start = int(request.query_params.get("from") or 0)
    except ValueError:
        start = 0
    first = (log_head - log_count) % LOG_SECONDS
    ts, ps, ss = [], [], []
    for k in range(start, min(start + 400, log_count)):
        i = (first + k) % LOG_SECONDS
        ts.append(round(log_t[i], 1))
        ps.append(None if log_p[i] < -900 else round(log_p[i], 2))
        ss.append(log_s[i])
    return JSONResponse(request, {"now": round(time.monotonic(), 1), "total": log_count,
                                  "t": ts, "p": ps, "s": ss,
                                  "events": alarm.events if start == 0 else []})


@server.route("/ack", POST)
def ack(request: Request):
    by = request.query_params.get("by") or ""
    by = "".join(ch for ch in by.replace("+", " ").replace("%20", " ") if ch.isalpha() or ch == " ")[:12].strip()
    done = alarm.acknowledge(by)
    return JSONResponse(request, {"acknowledged": done})


def clean_name(text):
    text = (text or "").replace("%20", " ").replace("+", " ")
    return "".join(ch for ch in text if ch.isalpha() or ch == " ")[:20].strip()


@server.route("/register", POST)
def register(request: Request):
    """Dashboard SMS ALERTS form: name, 10 or 11 digit phone. Consent is the tick box (time stored)."""
    global next_contact
    digits = "".join(ch for ch in (request.query_params.get("phone") or "") if ch.isdigit())
    name = clean_name(request.query_params.get("name")) or "Nurse"
    if len(digits) == 10:
        digits = "1" + digits        # North American number
    if len(digits) != 11:
        return JSONResponse(request, {"ok": False, "error": "use a 10 digit number"})
    if any(c[2] == "+" + digits for c in contacts):
        return JSONResponse(request, {"ok": False, "error": "already registered"})
    if len(contacts) >= MAX_CONTACTS:
        return JSONResponse(request, {"ok": False, "error": "list full (%d)" % MAX_CONTACTS})
    now = time.monotonic()
    contacts.append([next_contact, name, "+" + digits, round(now, 1)])
    next_contact += 1
    alarm.log(now, "SMS alerts on for %s (consent given)" % name)
    return JSONResponse(request, {"ok": True})


@server.route("/unregister", POST)
def unregister(request: Request):
    try:
        cid = int(request.query_params.get("id") or 0)
    except ValueError:
        cid = 0
    for c in contacts:
        if c[0] == cid:
            contacts.remove(c)
            alarm.log(time.monotonic(), "SMS alerts off for %s" % c[1])
            return JSONResponse(request, {"ok": True})
    return JSONResponse(request, {"ok": False})


@server.route("/contacts", GET)
def contact_list(request: Request):
    """Full numbers for the laptop alert service only. Each call also counts as its heartbeat."""
    global svc_seen
    svc_seen = time.monotonic()
    return JSONResponse(request, {"now": round(svc_seen, 1),
                                  "contacts": [{"id": c[0], "name": c[1], "phone": c[2], "optin_t": c[3]} for c in contacts]})


server.start(address, PORT)
print("Cuff monitor version", VERSION, " gain", CAL_GAIN, " offset", CAL_OFFSET)
print("Wi-Fi network:", WIFI_NAME, "  password:", WIFI_PASSWORD)
print("Dashboard: http://%s" % address + ("" if PORT == 80 else ":%d" % PORT))


def serve_for(seconds):
    """Answer the dashboard while waiting."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            server.poll()
        except OSError:
            pass
        time.sleep(0.01)


# ---------------- start up: sensor + zero ----------------
i2c = busio.I2C(board.GP5, board.GP4)   # SCL = GP5, SDA = GP4
while not i2c.try_lock():
    pass
sensor = None
while sensor is None:
    try:
        sensor = BMP280(i2c)
    except Exception:
        alarm.reason = "pressure sensor not found - check VIN, GND, SCK to GP5, SDI to GP4"
        print("Pressure sensor not found - check the sensor wires. Trying again...")
        serve_for(2)
print("Pressure sensor found")

print("Setting zero... keep the syringes off")
alarm.reason = "setting zero - keep the syringes off"
total = 0
for _ in range(20):
    total += sensor.pressure_pa()
    serve_for(0.1)
zero_pa = total / 20
alarm.set("NORMAL", "zero set")
alarm.same_since = time.monotonic()
print("Zero set")

# ---------------- live loop ----------------
period = 1 / SAMPLES_PER_SECOND
next_sample = time.monotonic()
last_sample = next_sample
while True:
    now = time.monotonic()
    if now >= next_sample:
        next_sample += period
        if next_sample < now:          # fell behind (e.g. a slow page load): don't try to catch up
            next_sample = now + period
        try:
            raw = sensor.pressure_pa()
            ok = True
        except Exception:
            raw, ok = zero_pa, False
        cmh2o = (raw - zero_pa) * PA_TO_CMH2O * CAL_GAIN + CAL_OFFSET
        latest["p"], latest["ok"] = cmh2o, ok
        if ok and cmh2o > HIGH:
            if high_since is None:
                high_since = now
        else:
            high_since = None
        alarm.update(now, cmh2o, raw, ok, now - last_sample)
        transient.update(now, cmh2o, ok, now - last_sample, alarm)
        last_sample = now
        if shell_ack_requested():
            alarm.acknowledge("Shell")
        if now - last_log >= 1:
            log_row(now, cmh2o, ok, alarm.state)
            last_log = now
        if ok and now - last_graph >= 0.5:
            graph.append((round(now, 1), round(cmh2o, 2)))
            if len(graph) > GRAPH_SECONDS * 2:
                graph.pop(0)
            last_graph = now
        # Thonny Plotter draws each "name: number" on this line
        print("cuff_cmH2O: %.2f  low: %d  high: %d" % (cmh2o, LOW, HIGH))
    try:
        server.poll()                  # answer the dashboard
    except OSError:
        pass
    time.sleep(0.005)
