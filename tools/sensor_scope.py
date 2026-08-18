#!/usr/bin/env python3
"""ZenBoard sensor scope - live raw view of the HMMD mmWave sensor in a browser.

A standalone diagnostic. It is not part of InkyPi and shares nothing with it;
run it on its own to see exactly what the sensor is saying, then stop it.

  sudo systemctl stop zenboard_sensor
  sudo python3 sensor_scope.py
  # open http://zenboard.local:8085/  (or the Tailscale address)
  sudo systemctl start zenboard_sensor

The sensor service must be stopped first because /dev/serial0 allows a single
reader; two readers each get a random half of the bytes and both see garbage.
The scope refuses to start while that service is running unless --force.

Everything is deliberately shown at once, including the things that turned out
to matter and are invisible in a plain value readout:

  report rate vs change rate
      The module reports at ~9.5 Hz but its internal estimate only updates
      every 1-3 s. Watching the value alone hides that completely, and it is
      the single most important property of this sensor - it is why tap and
      gesture detection are impossible here. Both rates are shown side by side.

  hold plateaus
      How long the current value has been repeating, plus a history. This is
      the same fact from the other direction, and makes the internal smoothing
      directly visible.

  unparsed lines
      Anything that is not "ON"/"OFF"/"Range N" is counted and shown verbatim.
      An undocumented close-range or energy message would appear here, and the
      module ignores every documented command, so this is the only way to find
      out if it has anything else to say.

Standard library plus pyserial only - nothing to install on a 415 MB Pi.
"""

import argparse
import collections
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial
except ImportError:
    sys.exit("pyserial missing: sudo apt install python3-serial")

PORT_DEFAULT = "/dev/serial0"
BAUD_DEFAULT = 115200

RANGE_RE = re.compile(rb"Range\s+(-?\d+)", re.I)
ON_RE = re.compile(rb"\bON\b", re.I)
OFF_RE = re.compile(rb"\bOFF\b", re.I)
REPORT_HEAD, REPORT_TAIL = b"\xf4\xf3\xf2\xf1", b"\xf8\xf7\xf6\xf5"

SERIES_MAX = 900          # ~95 s of chart history at 9.5 Hz
RAW_MAX = 60
HOLD_MAX = 40
WINDOW_S = 30.0           # stats window


class Scope:
    """Owns the serial port and all derived state. One reader thread."""

    def __init__(self, port, baud, ema_alpha, log_path):
        self.port, self.baud, self.ema_alpha = port, baud, ema_alpha
        self.log_path = log_path
        self.log = open(log_path, "w") if log_path else None

        self.lock = threading.Lock()
        self.started = time.time()
        self.stop = False
        self.err = None

        self.bytes_total = 0
        self.lines_total = 0
        self.range_total = 0
        self.presence_total = 0
        self.unparsed_total = 0
        self.frames_total = 0          # binary report frames, if it ever sends any
        self.last_hex = ""

        self.cm = None
        self.ema = None
        self.presence = None
        self.last_change_t = None
        self.hold_start = None

        self.series = collections.deque(maxlen=SERIES_MAX)      # (t, cm)
        self.pres_series = collections.deque(maxlen=SERIES_MAX)  # (t, 0/1)
        self.raw = collections.deque(maxlen=RAW_MAX)             # (t, text, kind)
        self.holds = collections.deque(maxlen=HOLD_MAX)          # (cm, seconds)
        self.unparsed = collections.deque(maxlen=RAW_MAX)
        self.report_stamps = collections.deque(maxlen=600)
        self.change_stamps = collections.deque(maxlen=600)
        self.byte_stamps = collections.deque(maxlen=600)         # (t, nbytes)

    # ---------------- reader ----------------

    def run(self):
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.05)
        except Exception as e:
            with self.lock:
                self.err = f"cannot open {self.port}: {e}"
            return

        buf = bytearray()
        while not self.stop:
            try:
                chunk = ser.read(256)
                now = time.time()
                if chunk:
                    with self.lock:
                        self.bytes_total += len(chunk)
                        self.byte_stamps.append((now, len(chunk)))
                        self.last_hex = chunk[-32:].hex(" ")
                    buf.extend(chunk)
                    self._drain_frames(buf, now)
                    self._drain_lines(buf, now)
                    if len(buf) > 8192:
                        del buf[:-1024]
                else:
                    # keep the hold timer honest even when nothing arrives
                    with self.lock:
                        pass
            except Exception as e:
                with self.lock:
                    self.err = f"read error: {e}"
                time.sleep(0.5)
        try:
            ser.close()
        except Exception:
            pass
        if self.log:
            self.log.close()

    def _drain_frames(self, buf, now):
        """Binary report frames. The module has never sent one - it ignores the
        command that would enable them - but if firmware ever differs, the
        per-gate energies would be the most useful data available, so look."""
        while True:
            i = buf.find(REPORT_HEAD)
            if i < 0:
                return
            j = buf.find(REPORT_TAIL, i)
            if j < 0:
                return
            p = bytes(buf[i + 4:j])
            del buf[:j + 4]
            if len(p) >= 5:
                gates = [int.from_bytes(p[5 + k * 2:7 + k * 2], "little")
                         for k in range(min(16, max(0, (len(p) - 5) // 2)))]
                with self.lock:
                    self.frames_total += 1
                    self.raw.append((now, f"REPORT present={bool(p[2])} "
                                          f"dist={p[3] | (p[4] << 8)} gates={gates}",
                                     "frame"))
                self._record(p[3] | (p[4] << 8), bool(p[2]), now)

    def _drain_lines(self, buf, now):
        while b"\n" in buf:
            line, _, _rest = bytes(buf).partition(b"\n")
            del buf[:len(line) + 1]
            line = line.strip()
            if not line:
                continue
            text = line.decode("ascii", "replace")
            with self.lock:
                self.lines_total += 1

            cm = pres = None
            m = RANGE_RE.search(line)
            if m:
                cm = int(m.group(1))
            if ON_RE.search(line):
                pres = True
            elif OFF_RE.search(line):
                pres = False

            kind = "range" if m else ("presence" if pres is not None else "unparsed")
            with self.lock:
                self.raw.append((now, text, kind))
                if kind == "unparsed":
                    self.unparsed_total += 1
                    self.unparsed.append((now, text))
            self._record(cm, pres, now)

    def _record(self, cm, pres, now):
        with self.lock:
            if pres is not None:
                self.presence_total += 1
                if self.presence != pres:
                    self.presence = pres
                    self.pres_series.append((now, 1 if pres else 0))

            if cm is None or not (0 < cm < 10000):
                if self.log:
                    self.log.flush()
                return

            self.range_total += 1
            self.report_stamps.append(now)

            if cm != self.cm:
                # A value change is a different event from a report, and the gap
                # between the two rates is the whole story with this sensor.
                if self.cm is not None and self.hold_start is not None:
                    self.holds.append((self.cm, now - self.hold_start))
                self.change_stamps.append(now)
                self.hold_start = now
                self.last_change_t = now
            elif self.hold_start is None:
                self.hold_start = now

            self.cm = cm
            self.ema = (cm if self.ema is None
                        else self.ema_alpha * cm + (1 - self.ema_alpha) * self.ema)
            self.series.append((now, cm))

            if self.log:
                self.log.write(json.dumps({"t": round(now, 4), "cm": cm,
                                           "presence": self.presence}) + "\n")

    # ---------------- snapshot ----------------

    @staticmethod
    def _hz(stamps, now, window=5.0):
        n = sum(1 for t in stamps if now - t <= window)
        return n / window

    def snapshot(self):
        now = time.time()
        with self.lock:
            recent = [cm for t, cm in self.series if now - t <= WINDOW_S]
            stats = {}
            if len(recent) >= 2:
                d = [abs(recent[i + 1] - recent[i]) for i in range(len(recent) - 1)]
                stats = {
                    "n": len(recent),
                    "min": min(recent), "max": max(recent),
                    "median": round(statistics.median(recent), 1),
                    "mean": round(statistics.mean(recent), 1),
                    "sd": round(statistics.pstdev(recent), 2),
                    "step_mean": round(statistics.mean(d), 2) if d else 0,
                    "step_max": max(d) if d else 0,
                }

            # 20 cm bins to 500, then an overflow bin
            hist = [0] * 26
            for _t, cm in self.series:
                hist[min(25, cm // 20)] += 1

            holds = [round(s, 2) for _cm, s in self.holds]
            bps = sum(n for t, n in self.byte_stamps if now - t <= 5.0) / 5.0

            return {
                "now": now,
                "uptime": round(now - self.started, 1),
                "err": self.err,
                "port": self.port, "baud": self.baud,
                "cm": self.cm,
                "ema": round(self.ema, 1) if self.ema is not None else None,
                "presence": self.presence,
                "hold_s": round(now - self.hold_start, 2) if self.hold_start else None,
                "since_change": (round(now - self.last_change_t, 2)
                                 if self.last_change_t else None),
                "report_hz": round(self._hz(self.report_stamps, now), 2),
                "change_hz": round(self._hz(self.change_stamps, now), 2),
                "bytes_per_s": round(bps, 1),
                "totals": {
                    "bytes": self.bytes_total, "lines": self.lines_total,
                    "range": self.range_total, "presence": self.presence_total,
                    "unparsed": self.unparsed_total, "frames": self.frames_total,
                },
                "stats": stats,
                "hist": hist,
                "holds": holds,
                "hold_mean": round(statistics.mean(holds), 2) if holds else None,
                "series": [[round(t - now, 2), cm] for t, cm in self.series],
                "pres_series": [[round(t - now, 2), v] for t, v in self.pres_series],
                "raw": [[round(t - now, 2), txt, kind]
                        for t, txt, kind in list(self.raw)[::-1]],
                "unparsed_lines": [[round(t - now, 2), txt]
                                   for t, txt in list(self.unparsed)[::-1]],
                "last_hex": self.last_hex,
                "log": self.log_path,
            }


PAGE = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZenBoard sensor scope</title>
<style>
:root{--bg:#0e0f12;--fg:#e8e8ea;--dim:#8b8f98;--line:#23252b;--accent:#d40000;
      --ok:#39d353;--warn:#e3b341;--card:#15171c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
header{display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;
       padding:12px 16px;border-bottom:1px solid var(--line)}
h1{font-size:14px;margin:0;letter-spacing:2px;font-weight:600}
.badge{font-size:11px;color:var(--dim)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.live{background:var(--ok)}.dot.err{background:var(--accent)}
main{padding:14px 16px;display:grid;gap:12px;
     grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:11px 13px}
.card.wide{grid-column:1/-1}
h2{font-size:10px;letter-spacing:1.8px;color:var(--dim);margin:0 0 9px;font-weight:600;
   text-transform:uppercase}
.big{font-size:40px;font-weight:600;line-height:1}
.unit{font-size:14px;color:var(--dim);padding-left:4px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px}
.kv dt{color:var(--dim)}.kv dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
canvas{width:100%;display:block;image-rendering:auto}
pre{margin:0;max-height:230px;overflow:auto;font-size:11px;white-space:pre-wrap;
    word-break:break-all}
.r-range{color:var(--fg)}.r-presence{color:var(--warn)}
.r-unparsed{color:var(--accent);font-weight:600}.r-frame{color:var(--ok)}
.t{color:var(--dim)}
.note{color:var(--dim);font-size:11px;margin-top:8px;line-height:1.5}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;
      border:1px solid var(--line)}
.pill.on{background:#123d1c;border-color:#1d6b2e;color:var(--ok)}
.pill.off{background:#2a1214;border-color:#5c1f22;color:#ff8a8a}
</style>
<header>
  <h1>SENSOR SCOPE</h1>
  <span class="badge"><span class="dot" id="dot"></span> <span id="conn">connecting</span></span>
  <span class="badge" id="portinfo"></span>
  <span class="badge" id="uptime"></span>
  <span class="badge" id="logpath"></span>
</header>
<main>
  <div class="card">
    <h2>Distance</h2>
    <div><span class="big" id="cm">--</span><span class="unit">cm</span></div>
    <dl class="kv" style="margin-top:10px">
      <dt>smoothed (EMA)</dt><dd id="ema">--</dd>
      <dt>presence</dt><dd id="pres">--</dd>
      <dt>held for</dt><dd id="hold">--</dd>
      <dt>since last change</dt><dd id="since">--</dd>
    </dl>
  </div>

  <div class="card">
    <h2>Rates</h2>
    <dl class="kv">
      <dt>report rate</dt><dd id="rhz">--</dd>
      <dt><b>value change rate</b></dt><dd id="chz">--</dd>
      <dt>bytes/s</dt><dd id="bps">--</dd>
      <dt>mean hold</dt><dd id="holdmean">--</dd>
    </dl>
    <div class="note">The module reports far faster than its estimate actually
      moves. The gap between these two numbers is the internal smoothing, and it
      is why sub-second events such as taps cannot be recovered.</div>
  </div>

  <div class="card">
    <h2>Window stats (30 s)</h2>
    <dl class="kv">
      <dt>samples</dt><dd id="s_n">--</dd>
      <dt>min / max</dt><dd id="s_mm">--</dd>
      <dt>median / mean</dt><dd id="s_med">--</dd>
      <dt>std dev</dt><dd id="s_sd">--</dd>
      <dt>mean |step|</dt><dd id="s_step">--</dd>
      <dt>max |step|</dt><dd id="s_stepmax">--</dd>
    </dl>
  </div>

  <div class="card">
    <h2>Counters</h2>
    <dl class="kv">
      <dt>bytes</dt><dd id="t_bytes">--</dd>
      <dt>lines</dt><dd id="t_lines">--</dd>
      <dt>range readings</dt><dd id="t_range">--</dd>
      <dt>presence lines</dt><dd id="t_pres">--</dd>
      <dt>binary frames</dt><dd id="t_frames">--</dd>
      <dt>unparsed</dt><dd id="t_unparsed">--</dd>
    </dl>
  </div>

  <div class="card wide">
    <h2>Distance timeline &mdash; last 95 s</h2>
    <canvas id="chart" height="230"></canvas>
  </div>

  <div class="card wide">
    <h2>Presence timeline</h2>
    <canvas id="pchart" height="58"></canvas>
  </div>

  <div class="card">
    <h2>Distance histogram (20 cm bins)</h2>
    <canvas id="hist" height="170"></canvas>
  </div>

  <div class="card">
    <h2>Hold durations (most recent last)</h2>
    <canvas id="holds" height="170"></canvas>
    <div class="note">Each bar is how long one value persisted before the module
      changed it.</div>
  </div>

  <div class="card wide">
    <h2>Raw stream (newest first)</h2>
    <pre id="raw"></pre>
    <div class="note">last bytes (hex): <span id="hex" class="t"></span></div>
  </div>

  <div class="card wide">
    <h2>Unrecognised lines</h2>
    <pre id="unp">none yet &mdash; anything here is undocumented output worth knowing about</pre>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

function prep(c){
  const r = window.devicePixelRatio || 1, w = c.clientWidth;
  c.width = w * r; c.height = c.height ? c.height : 200;
  if (c.dataset.h) c.height = c.dataset.h * r; else { c.dataset.h = c.height; c.height = c.height * r; }
  const x = c.getContext('2d'); x.setTransform(r,0,0,r,0,0);
  return [x, w, c.dataset.h * 1];
}

function line(c, series, lo, hi){
  const [x,w,h] = prep(c);
  x.clearRect(0,0,w,h);
  if (!series.length) return;
  const pad = 26, span = 95;
  // grid + axis labels
  x.strokeStyle = css('--line'); x.fillStyle = css('--dim');
  x.font = '10px ui-monospace'; x.lineWidth = 1;
  for (let i=0;i<=4;i++){
    const y = pad + (h-pad-14) * i/4, v = Math.round(hi - (hi-lo)*i/4);
    x.beginPath(); x.moveTo(34,y); x.lineTo(w,y); x.stroke();
    x.fillText(String(v).padStart(4), 2, y+3);
  }
  for (let s=0;s>=-90;s-=15){
    const px = 34 + (w-34) * (1 + s/span);
    x.fillText(s+'s', px-8, h-2);
  }
  x.strokeStyle = css('--accent'); x.lineWidth = 1.6; x.beginPath();
  let started = false;
  for (const [t,v] of series){
    if (t < -span) continue;
    const px = 34 + (w-34) * (1 + t/span);
    const py = pad + (h-pad-14) * (1 - (v-lo)/(hi-lo));
    started ? x.lineTo(px,py) : (x.moveTo(px,py), started=true);
  }
  x.stroke();
}

function steps(c, series){
  const [x,w,h] = prep(c);
  x.clearRect(0,0,w,h);
  const span = 95;
  x.fillStyle = css('--line'); x.fillRect(34,h/2-8,w-34,16);
  if (!series.length) return;
  // fill each ON interval as a block
  for (let i=0;i<series.length;i++){
    const [t,v] = series[i];
    if (!v) continue;
    const t2 = i+1 < series.length ? series[i+1][0] : 0;
    const a = 34 + (w-34)*(1+Math.max(t,-span)/span);
    const b = 34 + (w-34)*(1+Math.max(t2,-span)/span);
    x.fillStyle = css('--ok'); x.fillRect(a, h/2-8, Math.max(1,b-a), 16);
  }
  x.fillStyle = css('--dim'); x.font='10px ui-monospace';
  x.fillText('ON', 2, h/2+4);
}

function bars(c, vals, labels){
  const [x,w,h] = prep(c);
  x.clearRect(0,0,w,h);
  if (!vals.length) return;
  const max = Math.max(...vals) || 1, bw = (w-30)/vals.length;
  x.font='9px ui-monospace';
  vals.forEach((v,i)=>{
    const bh = (h-20) * v/max;
    x.fillStyle = css('--accent');
    x.fillRect(30+i*bw+1, h-16-bh, Math.max(1,bw-2), bh);
    if (labels && i%3===0){ x.fillStyle=css('--dim'); x.fillText(labels[i], 30+i*bw, h-4); }
  });
  x.fillStyle=css('--dim'); x.fillText(String(max), 2, 10);
}

const histLabels = Array.from({length:26},(_,i)=> i===25 ? '500+' : String(i*20));

function render(d){
  $('dot').className = 'dot ' + (d.err ? 'err' : 'live');
  $('conn').textContent = d.err ? d.err : 'live';
  $('portinfo').textContent = d.port + ' @ ' + d.baud + ' 8N1';
  $('uptime').textContent = 'up ' + d.uptime + 's';
  $('logpath').textContent = d.log ? ('logging -> ' + d.log) : '';

  $('cm').textContent = d.cm===null ? '--' : d.cm;
  $('ema').textContent = d.ema===null ? '--' : d.ema + ' cm';
  $('pres').innerHTML = d.presence===null ? '--'
      : '<span class="pill '+(d.presence?'on':'off')+'">'+(d.presence?'ON':'OFF')+'</span>';
  $('hold').textContent = d.hold_s===null ? '--' : d.hold_s + ' s';
  $('since').textContent = d.since_change===null ? '--' : d.since_change + ' s';

  $('rhz').textContent = d.report_hz + ' Hz';
  $('chz').textContent = d.change_hz + ' Hz';
  $('bps').textContent = d.bytes_per_s;
  $('holdmean').textContent = d.hold_mean===null ? '--' : d.hold_mean + ' s';

  const s = d.stats||{};
  $('s_n').textContent = s.n ?? '--';
  $('s_mm').textContent = s.n ? s.min+' / '+s.max+' cm' : '--';
  $('s_med').textContent = s.n ? s.median+' / '+s.mean : '--';
  $('s_sd').textContent = s.n ? s.sd : '--';
  $('s_step').textContent = s.n ? s.step_mean+' cm' : '--';
  $('s_stepmax').textContent = s.n ? s.step_max+' cm' : '--';

  const t = d.totals;
  $('t_bytes').textContent = t.bytes; $('t_lines').textContent = t.lines;
  $('t_range').textContent = t.range; $('t_pres').textContent = t.presence;
  $('t_frames').textContent = t.frames; $('t_unparsed').textContent = t.unparsed;

  const vals = d.series.map(p=>p[1]);
  const lo = 0, hi = Math.max(50, Math.ceil((Math.max(...vals,50)+10)/50)*50);
  line($('chart'), d.series, lo, hi);
  steps($('pchart'), d.pres_series);
  bars($('hist'), d.hist, histLabels);
  bars($('holds'), d.holds, d.holds.map(v=>v.toFixed(1)));

  $('raw').innerHTML = d.raw.map(([t,txt,kind]) =>
      '<span class="t">'+t.toFixed(2)+'s</span> <span class="r-'+kind+'">'
      + txt.replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) + '</span>'
    ).join('\n');
  $('hex').textContent = d.last_hex || '(none)';
  if (d.unparsed_lines.length)
    $('unp').textContent = d.unparsed_lines
      .map(([t,txt])=> t.toFixed(2)+'s  '+txt).join('\n');
}

const es = new EventSource('/stream');
es.onmessage = e => { try { render(JSON.parse(e.data)); } catch(_){} };
es.onerror = () => { $('dot').className='dot err'; $('conn').textContent='disconnected'; };
</script>
"""


class Handler(BaseHTTPRequestHandler):
    scope = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                      # the page polls constantly; do not spam stdout

    def _head(self, ctype, body=None, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if body is not None:
            self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = PAGE.encode()
            self._head("text/html; charset=utf-8", body)
            self.wfile.write(body)

        elif path == "/snapshot":
            body = json.dumps(self.scope.snapshot()).encode()
            self._head("application/json", body)
            self.wfile.write(body)

        elif path == "/log":
            p = self.scope.log_path
            if not p or not os.path.exists(p):
                self.send_error(404, "no log (start with --log PATH)")
                return
            if self.scope.log:
                self.scope.log.flush()
            with open(p, "rb") as f:
                body = f.read()
            self._head("application/x-ndjson", body,
                       {"Content-Disposition": 'attachment; filename="sensor.jsonl"'})
            self.wfile.write(body)

        elif path == "/stream":
            self._head("text/event-stream",
                       extra={"Cache-Control": "no-cache", "Connection": "keep-alive"})
            try:
                while True:
                    payload = json.dumps(self.scope.snapshot())
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.2)          # 5 Hz is plenty for a 9.5 Hz source
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)


def sensor_service_running():
    try:
        out = subprocess.run(["systemctl", "is-active", "zenboard_sensor"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() == "active"
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=PORT_DEFAULT)
    ap.add_argument("--baud", type=int, default=BAUD_DEFAULT)
    ap.add_argument("--http-port", type=int, default=8085)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--ema", type=float, default=0.30,
                    help="EMA alpha for the smoothed reading (higher = snappier)")
    ap.add_argument("--log", help="also append every reading to this JSONL file, "
                                  "downloadable from /log")
    ap.add_argument("--force", action="store_true",
                    help="start even if zenboard_sensor holds the port")
    args = ap.parse_args()

    if sensor_service_running() and not args.force:
        sys.exit("zenboard_sensor is running and owns %s - two readers each get "
                 "half the bytes.\n  sudo systemctl stop zenboard_sensor\n"
                 "Then re-run. Use --force to override." % args.port)

    scope = Scope(args.port, args.baud, args.ema, args.log)
    threading.Thread(target=scope.run, daemon=True).start()

    Handler.scope = scope
    srv = ThreadingHTTPServer((args.bind, args.http_port), Handler)
    srv.daemon_threads = True

    host = os.uname().nodename
    print(f"  sensor scope on http://{host}.local:{args.http_port}/")
    print(f"  reading {args.port} @ {args.baud}")
    if args.log:
        print(f"  logging to {args.log}  (download at /log)")
    print("  Ctrl-C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping")
    finally:
        scope.stop = True
        srv.shutdown()


if __name__ == "__main__":
    main()
