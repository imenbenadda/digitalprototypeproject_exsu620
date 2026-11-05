# Continuous endotracheal cuff pressure monitor

EXSU 620 Surgical Innovation, Fall 2026, Project 02, Group 2
Imen Benadda, Nathalie Obi, Kayla Arab, Karman Kang, Lucy Han

A bench prototype that measures cuff pressure continuously in cmH2O, tells a cough or a suction pass from a leak, and alarms on a black, monitor style Wi-Fi dashboard when the cuff stays outside 20 to 30 cmH2O for too long. **Bench only. Not a medical device.**

**Status (5 Nov 2026, firmware `2026-11-05 v8`, alert service v1):** live measurement, alarm rules, Wi-Fi dashboard and CSV export ran on the board in two bench runs (3 Nov 2026, `data/`). Dashboard revision B, the cough and suction labels, the SQLite database and the text message alerts were tested in the desktop simulation (`tools/`) only; texts ran in dry run. Full results and limitations are in `docs/EXSU620_Group2_Design_Dossier.pdf`.

---

## 1. Repository contents

| Path | What it is |
|---|---|
| `firmware/cuff_pressure.py` | The only file that runs on the Pico: sensor, zero, alarm rules, cough/suction labels, Wi-Fi network, dashboard page, 2 h log |
| `service/cuff_service.py` | Laptop alert service: SQLite database (`cuff.db`), a text message for every alert, shift report |
| `data/cuff_log_2026-11-03_1701.csv` | Bench run 1, 3 Nov 2026, 68 rows (room air baseline, then the tubing leak) |
| `data/cuff_log_2026-11-03_1703.csv` | Bench run 2, 3 Nov 2026, 91 rows (technical, advisory, critical, acknowledgement) |
| `tools/sim_cuff_pressure.py` | Runs the firmware's alarm logic on a computer with scripted traces, and serves the dashboard for testing |
| `docs/EXSU620_Group2_Design_Dossier.pdf` | Design dossier D1 to D9 |
| `docs/dashboard_sms_signup.png` | Dashboard SMS ALERTS sign-up (name, phone, consent) |
| `docs/dashboard_states.png` | Dashboard revision B: normal, probable cough, probable suction, air leak detected |
| `CHANGELOG.md` | Every change, by date |

## 2. Hardware

- Raspberry Pi Pico 2 W, CircuitPython 10.3.1
- Adafruit BMP280 pressure sensor, inside a sealed air chamber
- Breadboard, jumper wires, micro USB **data** cable
- Two three way stopcocks, 200 cc syringe, tubing
- PVC trachea phantom with a balloon as the cuff
- Dial manometer from a manual arm blood pressure cuff set (reference, reads mmHg; 1 mmHg = 1.36 cmH2O)

### Wiring (I2C, by pin name)

| BMP280 | Pico 2 W |
|---|---|
| VIN | 3V3 OUT (through the breadboard + rail) |
| GND | GND (through the breadboard − rail) |
| SCK | GP5 (SCL) |
| SDI | GP4 (SDA) |

Leave 3Vo, SDO and CS unconnected.

### Air circuit (final design)

- **Stopcock A:** sealed air chamber with the BMP280 · 200 cc syringe (air in and out, balloon inflation) · stopcock B
- **Stopcock B:** stopcock A · tubing to the balloon in the PVC trachea · tubing to the dial manometer

Why: the first single stopcock build leaked in the tubing and the leak could not be sealed. With two stopcocks and a 200 cc syringe, air can be drawn back in and the balloon re-inflated during a run, and the second stopcock gives a port for the manometer.

**Project rule:** the manometer is for calibration and validation only. Turn stopcock B away from the manometer during a logging run.

## 3. Software set-up

1. Install **Thonny** and select the Pico (CircuitPython) in the bottom right corner.
2. Copy `adafruit_httpserver` (CircuitPython 10.x bundle, 4.8.2) into `CIRCUITPY/lib`. The BMP280 driver is built into the script.
3. Open `firmware/cuff_pressure.py` in Thonny.

Settings at the top of the script: Wi-Fi name and password, alarm limits and dwell times, cough/suction thresholds, and `CAL_GAIN` / `CAL_OFFSET`.

## 4. Run it

1. Open stopcock A to room air, syringe off. Press **Stop**, then **Run** in Thonny. The first 2 s set zero.
2. Wait about 10 s for the Shell to print `Wi-Fi network: CuffMonitor` and the dashboard address.
3. On the laptop join Wi-Fi **CuffMonitor** (password `cuffpressure`) and open **http://192.168.4.1**. The laptop has no internet while joined.
4. Click the orange **CLICK TO ENABLE SOUND** button once (browsers block sound until a click). **TEST SOUND** plays the critical alarm.
5. Inflate the balloon with the 200 cc syringe through stopcock A, then close the syringe port.
6. At the end press **EXPORT CSV** before stopping the script. The log is in the Pico's memory and resets when the script restarts.

**View → Plotter** in Thonny shows the live graph with lines at 20 and 30.

## 5. What the dashboard shows (revision B)

One screen, no scrolling, black background:

| Message | Colour | Blink | Sound | When |
|---|---|---|---|---|
| NORMAL | Green text | No | None | No alarm |
| PROBABLE COUGH | Orange panel | Slow | One low tone | Rise of more than 5 cmH2O that ends within 5 s. Never an alarm |
| PROBABLE SUCTION | Orange panel | Slow | One low tone | Drop of more than 5 cmH2O that ends within 10 s. Never an alarm |
| LOW / HIGH CUFF PRESSURE | Orange panel | Slow (0.6 Hz) | Medium, 3 beeps every 7 s | Advisory: outside 20 to 30 for 30 s |
| AIR LEAK DETECTED | Red panel | Fast (2 Hz) | High, 10 beeps every 5 s | Critical: falling over 2 cmH2O/min over 60 s and 20 s |
| CRITICAL LOW / HIGH PRESSURE | Red panel | Fast (2 Hz) | High, 10 beeps every 5 s | Critical: below 15 or above 35 for a further 30 s |
| CHECK SENSOR | Cyan text | No | Low, 2 beeps | Technical: sensor silent, flat signal, room air for 5 s, or no zero |
| NO SIGNAL FROM MONITOR | Cyan text | No | Low, every 10 s | The browser lost the Pico |

- **Acknowledge** (with initials) works only during a red alarm. It silences the sound; the panel stays red, without blinking, until pressure is back inside 20 to 30.
- Below the message: big pressure number (green 20 to 30, yellow outside, red below 15 or above 30 for 5 s) with HI/LO limits, 3 min trend, time in range, fall rate, last 8 events.
- Typing `a` then Enter in the Thonny Shell also acknowledges.

## 6. Database and text message alerts

1. On the dashboard press **SMS ALERTS**, enter a name and mobile number, tick consent, **ADD PHONE** (up to 5 phones). Numbers stay on the Pico until it restarts.
2. Give the laptop internet while it stays on CuffMonitor, e.g. iPhone on USB with Personal Hotspot, or Ethernet.
3. For real texts, make a free Twilio trial account, verify the phones, then in Terminal:
   ```
   export TWILIO_SID=ACxxxxxxxx TWILIO_TOKEN=xxxxxxxx TWILIO_FROM=+15145550000
   ```
   Without these the service runs in **dry run** (messages stored and printed, not sent).
4. Start the service: `python3 service/cuff_service.py`. The dashboard top bar then shows `service on`.
5. Every advisory, critical, air leak, technical, probable cough and probable suction is texted to every phone, e.g. `CUFF MONITOR (bench prototype): AIR LEAK DETECTED at 21:14:05, cuff 16.9 cmH2O. pressure falling fast - possible leak`.
6. Shift report (CR-4): `python3 service/cuff_service.py report` (add `--csv` for `shift_report.csv`): time in range, every excursion with duration, alarms, acknowledgements, messages.

Tables in `cuff.db`: `readings`, `alarm_events`, `acks`, `users` (phone, name, consent time), `notifications` (each text with status and error). Open it with DB Browser for SQLite. No patient information is stored or sent.

## 7. Calibrate against the manometer

1. Set `CAL_GAIN = 1.0`, `CAL_OFFSET = 0.0`, run with the line open to room air for the zero.
2. Turn stopcock B so the manometer, sensor chamber and balloon share air.
3. With the syringe set the manometer to at least 5 levels (the dial starts at 20 mmHg), wait 10 s at each, write down manometer (converted to cmH2O) and dashboard values. Repeat up and down, 3 times.
4. In Excel: `=SLOPE(manometer, dashboard)` = `CAL_GAIN`, `=INTERCEPT(manometer, dashboard)` = `CAL_OFFSET`. Residual = manometer − (gain × dashboard + offset).
5. Put the values in the script, Stop, Run. The dashboard footer shows "calibrated".

## 8. Test without the Pico

```
cd tools
python3 sim_cuff_pressure.py ../firmware/cuff_pressure.py          # 6 logic tests
python3 sim_cuff_pressure.py ../firmware/cuff_pressure.py 8765     # plus dashboard at http://127.0.0.1:8765
```

Run the alert service against the simulator: `PICO_URL=http://127.0.0.1:8765 python3 service/cuff_service.py`.
Switch the scenario with `http://127.0.0.1:8765/scen/normal` (or `cough`, `suction`, `leak`) and reload the page.

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `pressure sensor not found` | Recheck VIN, GND, SCK to GP5, SDI to GP4 and the breadboard rails |
| Can't see CuffMonitor Wi-Fi | The script must be running. Wait 10 s and reopen the Wi-Fi menu |
| NO SIGNAL FROM MONITOR | The laptop fell back to another Wi-Fi, or the script stopped. Rejoin CuffMonitor |
| No sound | Click the orange sound button; turn the laptop volume up |
| Dashboard shows an old version | Thonny ran an old copy. Close the tab, reopen the file, Stop, Run |
| Pico not in Thonny | Use a data USB cable; reselect the interpreter |
| Board shorted while measuring | The multimeter probe was in the 10A socket. Use V/Ω |
| `service OFF` on the dashboard | Start `cuff_service.py` on the laptop; check it prints no "Cannot reach the Pico" |
| Texts not arriving | Laptop has no internet, Twilio keys not set (dry run), or the phone is not verified on the Twilio trial. See the `notifications` table `status` and `error` |
| Pressure keeps falling | Tubing leak: split the circuit with the stopcocks to find the side, soapy water on joints, twist Luer joints firmly. Slow drift that flattens is latex creep |

## 10. Limitations

Two short bench runs only; manometer comparison not recorded; leak label and dashboard revision B tested in simulation only; zero taken once at start-up (room pressure drift); SQLite and texts tested in simulation only (dry run); no bedside alarm independent of the laptop. See dossier sections 9 to 11.
