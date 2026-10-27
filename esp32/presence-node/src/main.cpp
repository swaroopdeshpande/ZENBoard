#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <Adafruit_NeoPixel.h>
#include <WebServer.h>
#include <ElegantOTA.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include "secrets.h"

// =====================================================================
// ZenBoard presence node v2 — ESP32-C3 + HMMD mmWave over UART.
//
// Goals, in order: (1) assert presence FAST when someone walks in,
// (2) never assert on glitches / through-wall / out-of-range motion,
// (3) stay connected to MQTT without flapping, (4) OTA from here on.
//
// Detection strategy (all timings below):
//   * ENTRY needs a sustained in-gate ON bout (ENTRY_MS). A single
//     glitch line can never flip the state by itself.
//   * ON lines only count while the latest range is inside the gate
//     (MAX_RANGE_CM). Far/through-wall motion is treated as absent.
//     No recent range data -> fail OPEN (counts), so a sensor that only
//     sends ON/OFF still works.
//   * CLUTTER: a range parked within tolerance this long is furniture,
//     not a person (cupboard at a fixed 110cm!). Learned spots stop
//     feeding entry/hold until something moves off them. Anything that
//     moves always counts, and spots are forgotten after hours.
//   * RELEASE is hold expiry, full stop: no in-gate ON for HOLD_MS and
//     the state clears. Stray OFF lines never touch the hold timer, so
//     there is nothing extra to confirm - and any extra condition here
//     risks exactly the latch-up it pretends to prevent. (v2.4.0 tried a
//     "sustained OFF" check whose timer every OFF line refreshed; with a
//     chatty sensor it could never elapse, so presence latched forever.)
//     Total sensor silence also releases, via the same hold expiry.
//   * Range is median-of-3, so single spike readings can't gate wrong.
//
// Wiring: sensor TX->GPIO3, sensor RX->GPIO4, sensor 3.3V+GND,
//         WS2812B 5V+GND+DIN=GPIO0. LED look is HA-selectable per state
//         (two MQTT lights: present slot + absent slot, each with
//         color+brightness+on/off, persisted in NVS). Status overrides:
//         blinking red=comms down, blue=OTA running.
// =====================================================================

#define FW_VERSION "2.5.0"

static constexpr int8_t PIN_SENSOR_RX = 3;
static constexpr int8_t PIN_SENSOR_TX = 4;
static constexpr int8_t PIN_LED = 0;
static constexpr uint16_t NUM_LEDS = 1;
static constexpr uint32_t HMMD_BAUD = 115200;
static constexpr uint8_t LED_BRIGHTNESS = 51;   // 20% of 255

// ── detection tuning ──────────────────────────────────────────────────
static constexpr uint32_t ENTRY_MS = 1200;    // sustained ON before assert
static constexpr uint32_t HOLD_MS = 120000;   // 2 min: bridge stillness gaps,
// LED goes red 2 min after last real motion
static constexpr uint32_t SILENT_MS = 20000;  // no line at all -> stale flag
static constexpr uint32_t RANGE_FRESH_MS = 5000;  // older range doesn't gate
static constexpr uint16_t DEFAULT_MAX_RANGE_CM = 450;  // NVS-overridable
static constexpr uint16_t MAX_RANGE_MIN = 100;
static constexpr uint16_t MAX_RANGE_MAX = 1200;
// Minimum range: readings closer than this never count (sensor housing,
// plant leaves in the face, sub-blind-zone garbage). 0 = off.
static constexpr uint16_t DEFAULT_MIN_RANGE_CM = 30;   // NVS-overridable
static constexpr uint16_t MIN_RANGE_MAX = 500;

// ── mqtt ──────────────────────────────────────────────────────────────
// Broker lives on the ZenBoard. Was "192.168.0.9" (Sep 30 power cut), then
// "192.168.0.14" (Oct 23 reshuffle) - each time the DHCP lease moved the node
// got connection-refused and blinked red. Currently .15. Give the ZenBoard a
// DHCP reservation so this never recurs; until then this constant is the
// single thing to update when the address moves.
static const char *MQTT_HOST = "192.168.0.15";
static constexpr uint16_t MQTT_PORT = 1883;
static const char *TOPIC_BASE = "wled/presence-node/presence";
static const char *TOPIC_STATUS = "wled/presence-node/status";
static const char *TOPIC_CMD = "wled/presence-node/presence/cmd";
static const char *TOPIC_LED = "wled/presence-node/led";
static const char *DISC_LED_PRESENT_TOPIC =
    "homeassistant/light/a0f262f17548_led_present/config";
static const char *DISC_LED_ABSENT_TOPIC =
    "homeassistant/light/a0f262f17548_led_absent/config";

// Discovery kept byte-identical to the old node on purpose: HA keeps the
// same entities, no automation/dashboard changes needed.
static const char *DISC_BINARY_TOPIC =
    "homeassistant/binary_sensor/a0f262f17548_presence/config";
static const char *DISC_BINARY_PAYLOAD =
    "{\"name\":\"Presence\",\"uniq_id\":\"a0f262f17548_presence\","
    "\"stat_t\":\"wled/presence-node/presence/state\","
    "\"dev_cla\":\"occupancy\",\"pl_on\":\"ON\",\"pl_off\":\"OFF\","
    "\"dev\":{\"ids\":[\"a0f262f17548\"],\"name\":\"WLED\","
    "\"mf\":\"Waveshare\",\"mdl\":\"HMMD mmWave\"}}";
static const char *DISC_DIST_TOPIC =
    "homeassistant/sensor/a0f262f17548_presence_distance/config";
static const char *DISC_DIST_PAYLOAD =
    "{\"name\":\"Presence distance\","
    "\"uniq_id\":\"a0f262f17548_presence_dist\","
    "\"stat_t\":\"wled/presence-node/presence/distance\","
    "\"unit_of_meas\":\"cm\",\"stat_cla\":\"measurement\","
    "\"dev\":{\"ids\":[\"a0f262f17548\"],\"name\":\"WLED\","
    "\"mf\":\"Waveshare\",\"mdl\":\"HMMD mmWave\"}}";

// Publish discipline: transitions go out IMMEDIATELY (lights must react),
// periodic stream at the configured rate (default 1 Hz, live-tunable for
// real-time watching), heartbeat keeps retained data fresh.
static constexpr uint32_t HEARTBEAT_MS = 60000;    // retained freshness
static constexpr uint8_t RATE_MIN_HZ = 1;
static constexpr uint8_t RATE_MAX_HZ = 20;
static constexpr uint8_t DEFAULT_RATE_HZ = 1;
static uint8_t rateHz = DEFAULT_RATE_HZ;

static uint32_t publishMinMs() { return 1000UL / rateHz; }

WiFiClient net;
PubSubClient mqtt(net);
Adafruit_NeoPixel led(NUM_LEDS, PIN_LED, NEO_GRB + NEO_KHZ800);
WebServer otaServer(80);
Preferences prefs;

static uint16_t maxRangeCm = DEFAULT_MAX_RANGE_CM;
static uint16_t minRangeCm = DEFAULT_MIN_RANGE_CM;

// ── detector state ────────────────────────────────────────────────────
static String line;
static bool present = false;
static bool seenAnyLine = false;
static uint32_t lastLineAt = 0;
static uint32_t lastOnAt = 0;       // last IN-GATE ON line
static uint32_t onBoutStart = 0;    // first ON of current bout, 0 = none
static uint32_t lastPublish = 0;
static uint32_t lastMqttOk = 0;
static bool dirty = false;          // something changed -> publish now
static bool transitionDirty = false;  // ON/OFF flip -> publish at once,
                                      // never throttled
static bool otaActive = false;
static uint32_t lastBlink = 0;
static bool blinkOn = false;

// range median-of-3
static uint16_t rbuf[3] = {0, 0, 0};
static uint8_t rbn = 0, rbi = 0;
static uint32_t lastRangeAt = 0;
static uint16_t rangeCm = 0;        // last published (median-or-latest)
static bool rangeReal = true;       // last clutter verdict (fail-open init)

// ── clutter rejection ───────────────────────────────────────────────
// A live target moves; furniture doesn't. Ranges are watched in a short
// window: spread >= MOTION_CM means something alive, and anything parked
// within CLUTTER_TOL_CM for CLUTTER_LEARN_MS gets learned as furniture.
// Learned spots stop feeding entry/hold until motion leaves them, and are
// forgotten after CLUTTER_FORGET_MS (rooms get rearranged). Worst case of
// a wrong learn is a delayed re-learn, never a permanently blind room.
static constexpr uint16_t CLUTTER_TOL_CM = 12;
static constexpr uint32_t DEFAULT_CLUTTER_LEARN_MS = 180000UL;  // 3 min parked
static constexpr uint32_t CLUTTER_FORGET_MS = 21600000UL;  // 6 h
static constexpr uint16_t MOTION_CM = 8;
static constexpr uint8_t MOTION_WIN = 20;
static uint32_t clutterLearnMs = DEFAULT_CLUTTER_LEARN_MS;  // NVS, live cmd

static uint16_t clutterVals[4] = {0, 0, 0, 0};
static uint32_t clutterAt[4] = {0, 0, 0, 0};
static uint16_t motionWin[MOTION_WIN] = {0};
static uint8_t motionN = 0;
static bool hasParked = false;
static uint16_t parkedAt = 0;
static uint32_t parkedSince = 0;

static bool clutterKnown(uint16_t d, uint32_t now) {
  for (uint8_t i = 0; i < 4; i++) {
    if (!clutterVals[i]) continue;
    if (now - clutterAt[i] > CLUTTER_FORGET_MS) { clutterVals[i] = 0; continue; }
    int dd = (int)d - (int)clutterVals[i];
    if (dd < 0) dd = -dd;
    if ((uint16_t)dd <= CLUTTER_TOL_CM) return true;
  }
  return false;
}

static void clutterLearn(uint16_t d, uint32_t now) {
  if (clutterKnown(d, now)) return;
  for (uint8_t i = 0; i < 4; i++) {
    if (!clutterVals[i]) {
      clutterVals[i] = d;
      clutterAt[i] = now;
      Serial.printf("[pn] clutter learned at %ucm\n", d);
      dirty = true;  // report the new spot in the next JSON
      return;
    }
  }
  uint8_t oi = 0;  // table full: replace oldest
  for (uint8_t i = 1; i < 4; i++)
    if (clutterAt[i] < clutterAt[oi]) oi = i;
  clutterVals[oi] = d;
  clutterAt[oi] = now;
  Serial.printf("[pn] clutter learned at %ucm (replaced oldest)\n", d);
  dirty = true;
}

// Returns true if dist belongs to a live target. Call per new median.
static bool clutterUpdate(uint16_t dist, uint32_t now) {
  if (motionN < MOTION_WIN) {
    motionWin[motionN++] = dist;
  } else {
    memmove(motionWin, motionWin + 1, (MOTION_WIN - 1) * sizeof(uint16_t));
    motionWin[MOTION_WIN - 1] = dist;
  }
  bool moving = false;
  if (motionN > 3) {
    uint16_t mn = motionWin[0], mx = motionWin[0];
    for (uint8_t i = 1; i < motionN; i++) {
      if (motionWin[i] < mn) mn = motionWin[i];
      if (motionWin[i] > mx) mx = motionWin[i];
    }
    moving = (mx - mn) >= MOTION_CM;
  }
  if (moving) {
    hasParked = false;  // alive: park tracking restarts, spot still counts
    return true;
  }
  int dp = (int)dist - (int)parkedAt;
  if (dp < 0) dp = -dp;
  if (!hasParked || (uint16_t)dp > CLUTTER_TOL_CM) {
    hasParked = true;
    parkedAt = dist;
    parkedSince = now;
  } else if (now - parkedSince > clutterLearnMs) {
    clutterLearn(dist, now);
  }
  return !clutterKnown(dist, now);
}

static uint16_t rangeMedian() {
  if (!rbn) return 0;
  uint16_t v[3] = {rbuf[0], rbuf[1], rbuf[2]};
  for (uint8_t i = 0; i < rbn; i++)
    for (uint8_t j = i + 1; j < rbn; j++)
      if (v[j] < v[i]) { uint16_t t = v[i]; v[i] = v[j]; v[j] = t; }
  return v[rbn / 2];
}

// ── calibration channel (kept for mqtt_cal.py) ────────────────────────
// cmd topic: "report" (16-gate energies), "normal" (ON/OFF lines),
// "hex:..." (raw config frame), "maxrange N" (persist gate 100..1200cm),
// "minrange N" (persist floor 0..500cm, 0=off), "rate N" (persist stream
// 1..20Hz, default 1), "clutterlearn N" (persist learn delay 0..3600s,
// default 180; 0 = learn on first parked reading - testing only),
// "clutter clear" (forget learned furniture), "status" (force publish),
// "reboot".
static bool reportMode = false;
static bool binEcho = false;
static uint32_t echoUntil = 0;
static uint8_t rfb[96];
static int rfn = 0;
static char replyHex[200];
static uint16_t replyLen = 0;

static void reply(const char *msg) {
  if (!mqtt.connected()) return;
  char t[96];
  snprintf(t, sizeof(t), "%s/reply", TOPIC_BASE);
  mqtt.publish(t, msg, false);
}

static void flushReply() {
  if (!replyLen) return;
  replyHex[replyLen] = 0;
  reply(replyHex);
  replyLen = 0;
}

// ── LED slots: HA-selectable color + brightness + on/off per state ────
// Two standard MQTT lights (discovery below): the "present" slot renders
// while someone is here, the "absent" slot while empty. Both carry full
// color + brightness + on/off, so HA shows native pickers and sliders.
// OFF on a slot means that state renders dark. Values persist in NVS.
static uint8_t ledPR = 0, ledPG = 255, ledPB = 0, ledPBri = LED_BRIGHTNESS;
static uint8_t ledAR = 255, ledAG = 0, ledAB = 0, ledABri = LED_BRIGHTNESS;
static bool ledPOn = true, ledAOn = true;

static void applyLed(uint8_t r, uint8_t g, uint8_t b, uint8_t bri, bool lit) {
  static uint8_t lr = 0, lg = 0, lb = 0, lbri = 0;
  static bool ll = false, init = false;
  if (init && lr == r && lg == g && lb == b && lbri == bri && ll == lit) return;
  init = true; lr = r; lg = g; lb = b; lbri = bri; ll = lit;
  if (!lit) {
    led.setPixelColor(0, 0, 0, 0);
  } else {
    led.setBrightness(bri);
    led.setPixelColor(0, r, g, b);
  }
  led.show();
}

static void ledTick(bool isPresent, bool commsOk, uint32_t now) {
  if (otaActive) {
    applyLed(0, 0, 255, 255, true);
    return;
  }
  if (!commsOk) {
    if (now - lastBlink >= 500) {
      lastBlink = now;
      blinkOn = !blinkOn;
    }
    applyLed(255, 0, 0, 255, blinkOn);
    return;
  }
  if (isPresent) applyLed(ledPR, ledPG, ledPB, ledPBri, ledPOn);
  else applyLed(ledAR, ledAG, ledAB, ledABri, ledAOn);
}

static void publishLedDiscovery() {
  // Standard (non-JSON) mqtt light schema: HA renders native color
  // pickers + brightness sliders, no custom UI needed. Same device block
  // as the presence sensors so all three entities sit together.
  const char *dev = "\"device\":{\"ids\":[\"a0f262f17548\"],\"name\":\"WLED\","
                    "\"manufacturer\":\"Waveshare\",\"model\":\"HMMD mmWave\"}";
  char t[128], p[560];
  snprintf(t, sizeof(t), "%s", DISC_LED_PRESENT_TOPIC);
  snprintf(p, sizeof(p),
           "{\"name\":\"Presence LED present\","
           "\"unique_id\":\"a0f262f17548_led_present\","
           "\"command_topic\":\"%s/present/set\","
           "\"state_topic\":\"%s/present/state\","
           "\"brightness_command_topic\":\"%s/present/brightness/set\","
           "\"brightness_state_topic\":\"%s/present/brightness\","
           "\"rgb_command_topic\":\"%s/present/rgb/set\","
           "\"rgb_state_topic\":\"%s/present/rgb\",%s}",
           TOPIC_LED, TOPIC_LED, TOPIC_LED, TOPIC_LED,
           TOPIC_LED, TOPIC_LED, dev);
  mqtt.publish(t, p, true);
  snprintf(t, sizeof(t), "%s", DISC_LED_ABSENT_TOPIC);
  snprintf(p, sizeof(p),
           "{\"name\":\"Presence LED absent\","
           "\"unique_id\":\"a0f262f17548_led_absent\","
           "\"command_topic\":\"%s/absent/set\","
           "\"state_topic\":\"%s/absent/state\","
           "\"brightness_command_topic\":\"%s/absent/brightness/set\","
           "\"brightness_state_topic\":\"%s/absent/brightness\","
           "\"rgb_command_topic\":\"%s/absent/rgb/set\","
           "\"rgb_state_topic\":\"%s/absent/rgb\",%s}",
           TOPIC_LED, TOPIC_LED, TOPIC_LED, TOPIC_LED,
           TOPIC_LED, TOPIC_LED, dev);
  mqtt.publish(t, p, true);
}

static void publishLedStates() {
  if (!mqtt.connected()) return;
  char t[96], p[32];
  snprintf(t, sizeof(t), "%s/present/state", TOPIC_LED);
  mqtt.publish(t, ledPOn ? "ON" : "OFF", true);
  snprintf(t, sizeof(t), "%s/present/brightness", TOPIC_LED);
  snprintf(p, sizeof(p), "%u", ledPBri);
  mqtt.publish(t, p, true);
  snprintf(t, sizeof(t), "%s/present/rgb", TOPIC_LED);
  snprintf(p, sizeof(p), "%u,%u,%u", ledPR, ledPG, ledPB);
  mqtt.publish(t, p, true);
  snprintf(t, sizeof(t), "%s/absent/state", TOPIC_LED);
  mqtt.publish(t, ledAOn ? "ON" : "OFF", true);
  snprintf(t, sizeof(t), "%s/absent/brightness", TOPIC_LED);
  snprintf(p, sizeof(p), "%u", ledABri);
  mqtt.publish(t, p, true);
  snprintf(t, sizeof(t), "%s/absent/rgb", TOPIC_LED);
  snprintf(p, sizeof(p), "%u,%u,%u", ledAR, ledAG, ledAB);
  mqtt.publish(t, p, true);
}

static void onLedMessage(const char *topic, const String &msg) {
  // .../led/<present|absent>/<set|brightness/set|rgb/set>
  String t = topic;
  uint8_t *R, *G, *B, *Bri;
  bool *On;
  char pre;
  if (t.indexOf("/present/") >= 0) {
    R = &ledPR; G = &ledPG; B = &ledPB; Bri = &ledPBri; On = &ledPOn; pre = 'p';
  } else if (t.indexOf("/absent/") >= 0) {
    R = &ledAR; G = &ledAG; B = &ledAB; Bri = &ledABri; On = &ledAOn; pre = 'a';
  } else {
    return;
  }
  String m = msg;
  m.trim();
  bool changed = false;
  if (t.endsWith("/brightness/set")) {
    bool digits = m.length() > 0;
    for (size_t i = 0; i < m.length() && digits; i++)
      digits = isdigit((unsigned char)m[i]);
    if (digits) {
      long v = m.toInt();
      if (v >= 0 && v <= 255) { *Bri = (uint8_t)v; changed = true; }
    }
  } else if (t.endsWith("/rgb/set")) {
    int r, g, b;
    if (sscanf(m.c_str(), "%d,%d,%d", &r, &g, &b) == 3) {
      *R = (uint8_t)constrain(r, 0, 255);
      *G = (uint8_t)constrain(g, 0, 255);
      *B = (uint8_t)constrain(b, 0, 255);
      changed = true;
    }
  } else if (t.endsWith("/set")) {
    if (m.equalsIgnoreCase("ON")) { *On = true; changed = true; }
    else if (m.equalsIgnoreCase("OFF")) { *On = false; changed = true; }
  }
  if (!changed) return;
  char k[12];
  snprintf(k, sizeof(k), "led_%cr", pre); prefs.putUChar(k, *R);
  snprintf(k, sizeof(k), "led_%cg", pre); prefs.putUChar(k, *G);
  snprintf(k, sizeof(k), "led_%cb", pre); prefs.putUChar(k, *B);
  snprintf(k, sizeof(k), "led_%cbri", pre); prefs.putUChar(k, *Bri);
  snprintf(k, sizeof(k), "led_%con", pre); prefs.putUChar(k, *On ? 1 : 0);
  publishLedStates();  // echo back so HA shows what stuck
}

// Publish everything; returns true only if ALL of it went out, so a
// failed publish keeps dirty=true and is retried instead of dropped.
static bool publishState(bool stale) {
  if (!mqtt.connected()) return false;
  char topic[96];

  snprintf(topic, sizeof(topic), "%s/state", TOPIC_BASE);
  if (!mqtt.publish(topic, present ? "ON" : "OFF", true)) return false;

  if (rangeCm > 0) {
    char buf[8];
    snprintf(buf, sizeof(buf), "%u", rangeCm);
    snprintf(topic, sizeof(topic), "%s/distance", TOPIC_BASE);
    if (!mqtt.publish(topic, buf, true)) return false;
  }

  bool rawActive = (millis() - lastOnAt < 3000);
  char cl[48];
  int co = snprintf(cl, sizeof(cl), "\"clutter\":[");
  bool first = true;
  for (uint8_t i = 0; i < 4; i++) {
    if (!clutterVals[i]) continue;
    if (millis() - clutterAt[i] > CLUTTER_FORGET_MS) continue;
    co += snprintf(cl + co, sizeof(cl) - co, "%s%u",
                   first ? "" : ",", clutterVals[i]);
    first = false;
  }
  snprintf(cl + co, sizeof(cl) - co, "]");
  char json[256];
  snprintf(json, sizeof(json),
           "{\"present\":%s,\"distance_cm\":%u,\"held\":%s,"
           "\"stale\":%s,\"rssi\":%d,\"fw\":\"%s\",%s}",
           present ? "true" : "false", rangeCm,
           (present && !rawActive) ? "true" : "false",
           stale ? "true" : "false", WiFi.RSSI(), FW_VERSION, cl);
  snprintf(topic, sizeof(topic), "%s/json", TOPIC_BASE);
  if (!mqtt.publish(topic, json, true)) return false;

  lastPublish = millis();
  dirty = false;
  return true;
}

static void onMqttConnect() {
  mqtt.publish(TOPIC_STATUS, "online", true);
  mqtt.publish(DISC_BINARY_TOPIC, DISC_BINARY_PAYLOAD, true);
  mqtt.publish(DISC_DIST_TOPIC, DISC_DIST_PAYLOAD, true);
  mqtt.subscribe(TOPIC_CMD);
  {
    char lt[96];
    snprintf(lt, sizeof(lt), "%s/#", TOPIC_LED);
    mqtt.subscribe(lt);
  }
  publishLedDiscovery();
  publishLedStates();
  dirty = true;  // publish current state immediately after (re)connect
  transitionDirty = true;
}

static String clientId() {
  char id[32];
  snprintf(id, sizeof(id), "zenboard-pn-%08x",
           (uint32_t)(ESP.getEfuseMac() >> 24));
  return String(id);
}

static void ensureMqtt() {
  // Exponential backoff: 3s, 6s, 12s ... capped at 60s. Never hammers.
  static uint32_t lastAttempt = 0;
  static uint8_t fails = 0;
  if (mqtt.connected()) { fails = 0; return; }
  uint32_t wait = 3000UL << fails;
  if (wait > 60000UL || wait < 3000UL) wait = 60000UL;  // shift-overflow guard
  if (millis() - lastAttempt < wait) return;
  lastAttempt = millis();
  bool ok;
  if (strlen(MQTT_USER) > 0) {
    ok = mqtt.connect(clientId().c_str(), MQTT_USER, MQTT_PASS,
                      TOPIC_STATUS, 1, true, "offline");
  } else {
    ok = mqtt.connect(clientId().c_str(), TOPIC_STATUS, 1, true, "offline");
  }
  if (ok) {
    onMqttConnect();
  } else {
    if (fails < 5) fails++;
  }
}

static void ensureWiFi() {
  static uint32_t lastAttempt = 0;
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastAttempt < 5000) return;
  lastAttempt = millis();
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("presence-node");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

// ── sensor config channel ─────────────────────────────────────────────
static void sensorRaw(const uint8_t *b, size_t n) {
  Serial1.write(b, n);
  echoUntil = millis() + 1500;
  binEcho = true;
}

// 0=debug 4=report 100=normal (wiki: cmd 0x0012, param 0x0000, u32 value)
static void setSensorMode(uint32_t v) {
  static const uint8_t ENTER[] = {0xFD, 0xFC, 0xFB, 0xFA, 0x04, 0x00,
                                  0xFF, 0x00, 0x02, 0x00, 0x04, 0x03, 0x02, 0x01};
  static const uint8_t EXIT[]  = {0xFD, 0xFC, 0xFB, 0xFA, 0x02, 0x00,
                                  0xFE, 0x00, 0x04, 0x03, 0x02, 0x01};
  uint8_t ctrl[18] = {0xFD, 0xFC, 0xFB, 0xFA, 0x08, 0x00, 0x12, 0x00,
                      0x00, 0x00,
                      (uint8_t)(v), (uint8_t)(v >> 8), (uint8_t)(v >> 16),
                      (uint8_t)(v >> 24),
                      0x04, 0x03, 0x02, 0x01};
  sensorRaw(ENTER, sizeof(ENTER));
  delay(60);
  sensorRaw(ctrl, sizeof(ctrl));
  delay(60);
  sensorRaw(EXIT, sizeof(EXIT));
  delay(60);
}

static void onMqttMessage(char *topic, byte *payload, unsigned int length) {
  if (strncmp(topic, TOPIC_LED, strlen(TOPIC_LED)) == 0) {
    String msg;
    msg.reserve(length);
    for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
    onLedMessage(topic, msg);
    return;
  }
  if (strcmp(topic, TOPIC_CMD) != 0) return;
  String cmd;
  cmd.reserve(length);
  for (unsigned int i = 0; i < length; i++) cmd += (char)payload[i];
  cmd.trim();

  if (cmd.equalsIgnoreCase("report")) {
    setSensorMode(4);
    reportMode = true;
  } else if (cmd.equalsIgnoreCase("normal")) {
    setSensorMode(100);
    reportMode = false;
  } else if (cmd.startsWith("hex:") || cmd.startsWith("HEX:")) {
    String h = cmd.substring(4);
    h.replace(" ", "");
    uint8_t buf[96];
    size_t n = 0;
    for (size_t i = 0; i + 1 < h.length() && n < sizeof(buf); i += 2) {
      buf[n++] = (uint8_t)strtoul(h.substring(i, i + 2).c_str(), nullptr, 16);
    }
    if (n) sensorRaw(buf, n);
  } else if (cmd.startsWith("maxrange ") || cmd.startsWith("MAXRANGE ")) {
    long v = cmd.substring(cmd.indexOf(' ') + 1).toInt();
    if (v >= MAX_RANGE_MIN && v <= MAX_RANGE_MAX) {
      maxRangeCm = (uint16_t)v;
      prefs.putUShort("maxrange", maxRangeCm);
      char ack[48];
      snprintf(ack, sizeof(ack), "maxrange=%u", maxRangeCm);
      reply(ack);
    } else {
      reply("maxrange: need 100..1200");
    }
  } else if (cmd.startsWith("minrange ") || cmd.startsWith("MINRANGE ")) {
    long v = cmd.substring(cmd.indexOf(' ') + 1).toInt();
    if (v >= 0 && v <= MIN_RANGE_MAX) {
      minRangeCm = (uint16_t)v;
      prefs.putUShort("minrange", minRangeCm);
      char ack[48];
      snprintf(ack, sizeof(ack), "minrange=%u", minRangeCm);
      reply(ack);
    } else {
      reply("minrange: need 0..500");
    }
  } else if (cmd.startsWith("rate ") || cmd.startsWith("RATE ")) {
    long v = cmd.substring(cmd.indexOf(' ') + 1).toInt();
    if (v >= RATE_MIN_HZ && v <= RATE_MAX_HZ) {
      rateHz = (uint8_t)v;
      prefs.putUChar("ratehz", rateHz);
      char ack[48];
      snprintf(ack, sizeof(ack), "rate=%uHz", rateHz);
      reply(ack);
    } else {
      reply("rate: need 1..20");
    }
  } else if (cmd.equalsIgnoreCase("status")) {
    dirty = true;
  } else if (cmd.startsWith("clutterlearn ") || cmd.startsWith("CLUTTERLEARN ")) {
    long v = cmd.substring(cmd.indexOf(' ') + 1).toInt();
    if (v >= 0 && v <= 3600) {
      clutterLearnMs = (uint32_t)v * 1000UL;
      prefs.putULong("clutterlearn", clutterLearnMs);
      char ack[48];
      snprintf(ack, sizeof(ack), "clutterlearn=%lus", (unsigned long)v);
      reply(ack);
    } else {
      reply("clutterlearn: need 0..3600");
    }
  } else if (cmd.equalsIgnoreCase("clutter clear")) {
    for (uint8_t i = 0; i < 4; i++) { clutterVals[i] = 0; clutterAt[i] = 0; }
    hasParked = false;
    motionN = 0;
    reply("clutter cleared");
  } else if (cmd.equalsIgnoreCase("reboot")) {
    reply("rebooting");
    delay(200);
    ESP.restart();
  }
}

// ── detector input: ON/OFF lines feed the bout integrators ────────────
// Out-of-gate ON lines are demoted to OFF input: far/through-wall motion
// must never assert presence, nor refresh the hold timer.
static void onSensorOn(uint32_t now, bool gateOk) {
  lastLineAt = now;
  seenAnyLine = true;
  if (!gateOk) {
    // Out-of-gate motion (far, too close, or learned clutter): breaks any
    // entry bout, and never feeds the hold timer. Deliberately does NOT
    // touch lastOnAt - only real in-gate motion sustains presence.
    onBoutStart = 0;
    return;
  }
  if (!onBoutStart) onBoutStart = now;
  lastOnAt = now;
}

static void onSensorOff(uint32_t now) {
  lastLineAt = now;
  seenAnyLine = true;
  // Breaks an entry bout, nothing more. OFF lines never clear an asserted
  // presence directly - only hold expiry does - so stray OFFs are harmless.
  onBoutStart = 0;
}

static void handleLine(const String &raw) {
  String s = raw;
  s.trim();
  if (s.length() == 0) return;
  uint32_t now = millis();

  if (s.equalsIgnoreCase("ON")) {
    // In-gate AND live: fresh range must also pass the clutter verdict.
    // Stale/missing range fails open (ON/OFF-only sensors keep working).
    bool recent = (now - lastRangeAt <= RANGE_FRESH_MS);
    uint16_t med = rangeMedian();
    bool inRange = !recent ||
                   ((minRangeCm == 0 || med >= minRangeCm) &&
                    med <= maxRangeCm && rangeReal);
    onSensorOn(now, inRange);
    dirty = true;
  } else if (s.equalsIgnoreCase("OFF")) {
    onSensorOff(now);
    // OFF alone never flips state here; the release integrator in loop()
    // decides, so one stray OFF can't drop occupancy mid-hold.
    dirty = true;
  } else if (s.indexOf("Range") >= 0 || s.indexOf("range") >= 0) {
    int idx = s.indexOf("ange") + 4;
    long v = s.substring(idx).toInt();
    if (v > 0 && v < 10000) {
      rbuf[rbi] = (uint16_t)v;
      rbi = (rbi + 1) % 3;
      if (rbn < 3) rbn++;
      lastRangeAt = now;
      lastLineAt = now;
      seenAnyLine = true;
      uint16_t m = rangeMedian();
      rangeReal = clutterUpdate(m, now);
      if (m != rangeCm) { rangeCm = m; dirty = true; }
      else dirty = true;  // still counts as sensor-alive traffic
    }
  }
}

static void readSensor() {
  while (Serial1.available()) {
    int v = Serial1.read();

    // config-reply window: forward bytes as hex on <base>/reply
    if (binEcho) {
      if (replyLen < sizeof(replyHex) - 3)
        replyLen += snprintf(replyHex + replyLen, sizeof(replyHex) - replyLen,
                             "%02X", v);
      if (millis() > echoUntil || replyLen > sizeof(replyHex) - 4) {
        flushReply();
        binEcho = millis() <= echoUntil;
      }
      continue;
    }

    // report mode: assemble F4 F3 F2 F1 frames, publish energies
    if (reportMode) {
      if (rfn < (int)sizeof(rfb)) rfb[rfn++] = (uint8_t)v;
      if (rfn >= 6 && !(rfb[0] == 0xF4 && rfb[1] == 0xF3 &&
                        rfb[2] == 0xF2 && rfb[3] == 0xF1)) {
        memmove(rfb, rfb + 1, --rfn);
      } else if (rfn >= 6) {
        int plen = rfb[4] | (rfb[5] << 8);
        int total = 6 + plen + 4;
        if (plen < 35 || total > (int)sizeof(rfb)) { rfn = 0; }
        else if (rfn >= total) {
          if (rfb[total - 4] == 0xF8 && rfb[total - 3] == 0xF7 &&
              rfb[total - 2] == 0xF6 && rfb[total - 1] == 0xF5) {
            uint8_t pres = rfb[6];
            uint16_t dist = rfb[7] | (rfb[8] << 8);
            uint16_t e[16];
            memcpy(e, rfb + 9, 32);
            if (mqtt.connected()) {
              char t[96], j[320];
              snprintf(t, sizeof(t), "%s/cal", TOPIC_BASE);
              int o = snprintf(j, sizeof(j),
                               "{\"p\":%u,\"d\":%u,\"e\":[", pres, dist);
              for (int i = 0; i < 16 && o < (int)sizeof(j) - 8; i++)
                o += snprintf(j + o, sizeof(j) - o, "%s%u", i ? "," : "", e[i]);
              snprintf(j + o, sizeof(j) - o, "]}");
              mqtt.publish(t, j, false);
            }
            lastLineAt = millis();   // sensor is alive while calibrating too
          }
          rfn = 0;
        }
      }
      continue;
    }

    // normal mode: ASCII ON / OFF / Range N
    char c = (char)v;
    if (c == '\n' || c == '\r') {
      if (line.length()) { handleLine(line); line = ""; }
    } else if (line.length() < 31) {
      line += c;
    } else {
      line = "";
    }
  }
}

void setup() {
  Serial.begin(115200);

  prefs.begin("presence", false);
  maxRangeCm = prefs.getUShort("maxrange", DEFAULT_MAX_RANGE_CM);
  if (maxRangeCm < MAX_RANGE_MIN || maxRangeCm > MAX_RANGE_MAX)
    maxRangeCm = DEFAULT_MAX_RANGE_CM;
  minRangeCm = prefs.getUShort("minrange", DEFAULT_MIN_RANGE_CM);
  if (minRangeCm > MIN_RANGE_MAX) minRangeCm = DEFAULT_MIN_RANGE_CM;
  rateHz = prefs.getUChar("ratehz", DEFAULT_RATE_HZ);
  if (rateHz < RATE_MIN_HZ || rateHz > RATE_MAX_HZ) rateHz = DEFAULT_RATE_HZ;
  clutterLearnMs = prefs.getULong("clutterlearn", DEFAULT_CLUTTER_LEARN_MS);
  if (clutterLearnMs > 3600000UL) clutterLearnMs = DEFAULT_CLUTTER_LEARN_MS;

  Serial1.setRxBufferSize(2048);   // never drop bytes between loop passes
  Serial1.begin(HMMD_BAUD, SERIAL_8N1, PIN_SENSOR_RX, PIN_SENSOR_TX);
  line.reserve(64);

  led.begin();
  // Slot looks come from NVS (HA-selectable); defaults preserve the
  // original green-when-present / red-when-empty behavior.
  ledPR = prefs.getUChar("led_pr", 0);
  ledPG = prefs.getUChar("led_pg", 255);
  ledPB = prefs.getUChar("led_pb", 0);
  ledPBri = prefs.getUChar("led_pbri", LED_BRIGHTNESS);
  ledPOn = prefs.getUChar("led_pon", 1);
  ledAR = prefs.getUChar("led_ar", 255);
  ledAG = prefs.getUChar("led_ag", 0);
  ledAB = prefs.getUChar("led_ab", 0);
  ledABri = prefs.getUChar("led_abri", LED_BRIGHTNESS);
  ledAOn = prefs.getUChar("led_aon", 1);

  Serial.printf("\n[pn] presence-node v%s gate %u..%ucm entry=%ums hold=%lus rate=%uHz learn=%lus\n",
                FW_VERSION, minRangeCm, maxRangeCm, ENTRY_MS, HOLD_MS / 1000UL, rateHz,
                clutterLearnMs / 1000UL);

  // Force ASCII mode in case the sensor was left in report mode by a
  // calibration session: otherwise boot parses binary as garbage.
  setSensorMode(100);
  reportMode = false;
  Serial.println("[pn] sensor forced to normal mode");

  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);  // USB-powered: no modem sleep, steadier MQTT/OTA
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("presence-node");
  WiFi.begin(WIFI_SSID, WIFI_PASS);   // no blocking wait: sensor runs first

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(1024);  // discovery payloads (~450B) must fit whole
  mqtt.setKeepAlive(45);
  mqtt.setSocketTimeout(5);
  mqtt.setCallback(onMqttMessage);

  // ── OTA: browser portal at http://presence-node.local/update (ElegantOTA)
  // and PlatformIO/ArduinoOTA on port 3232 (`pio run -t upload` next time).
  otaServer.on("/", []() {
    char s[160];
    snprintf(s, sizeof(s),
             "presence-node v%s uptime=%lus present=%d range=%ucm rssi=%d rate=%uHz",
             FW_VERSION, millis() / 1000UL, present ? 1 : 0,
             rangeCm, WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0, rateHz);
    otaServer.send(200, "text/plain", s);
  });
  ElegantOTA.begin(&otaServer);
  otaServer.begin();

  ArduinoOTA.setHostname("presence-node");
  ArduinoOTA.onStart([]() {
    otaActive = true;  // ledTick paints blue from here
    Serial.println("[pn] OTA start");
  });
  ArduinoOTA.onEnd([]() {
    Serial.println("[pn] OTA end, rebooting");
  });
  ArduinoOTA.onError([](ota_error_t e) {
    otaActive = false;
    Serial.printf("[pn] OTA error %d\n", (int)e);
  });
  ArduinoOTA.begin();
  ElegantOTA.onStart([]() {
    otaActive = true;  // ledTick paints blue from here
    Serial.println("[pn] ElegantOTA start");
  });
  ElegantOTA.onEnd([](bool success) {
    Serial.printf("[pn] ElegantOTA end (ok=%d), rebooting\n", success ? 1 : 0);
  });
  Serial.println("[pn] OTA ready: http://presence-node.local/update + :3232");
}

void loop() {
  readSensor();                       // UART drained before anything else

  // echo window expired with no trailing byte -> flush what we hold
  if (binEcho && millis() > echoUntil) {
    flushReply();
    binEcho = false;
  }

  uint32_t now = millis();
  bool stale = seenAnyLine && (now - lastLineAt > SILENT_MS);

  // ── detector ──────────────────────────────────────────────────────
  if (seenAnyLine && !reportMode) {
    if (!present) {
      // ENTRY: ON bout sustained past ENTRY_MS, in gate. A lone glitch
      // line starts a bout but can never complete it alone.
      if (onBoutStart && (now - onBoutStart >= ENTRY_MS)) {
        present = true;
        dirty = true;
        transitionDirty = true;
      }
    } else {
      // RELEASE: no in-gate ON for a full hold window. That is the whole
      // rule - OFF lines, out-of-gate motion and silence all converge
      // here, because none of them refresh lastOnAt.
      if (now - lastOnAt >= HOLD_MS) {
        present = false;
        rangeCm = 0;
        rbn = 0;
        onBoutStart = 0;
        dirty = true;
        transitionDirty = true;
      }
    }
  }

  bool commsOk = (WiFi.status() == WL_CONNECTED) && mqtt.connected();
  ledTick(present, commsOk, now);
  if (commsOk) lastMqttOk = now;

  if (WiFi.status() == WL_CONNECTED) {
    ensureMqtt();
    if (mqtt.connected()) {
      mqtt.loop();
      if (transitionDirty) {
        // ON/OFF flips go out immediately, and retry every pass until
        // they succeed - a transition is never throttled, never dropped.
        if (publishState(stale)) transitionDirty = false;
      } else if (dirty && now - lastPublish >= publishMinMs()) {
        // Range churn and everything else: at most the configured rate.
        publishState(stale);  // clears dirty only on full success
      } else if (!dirty && HEARTBEAT_MS && now - lastPublish > HEARTBEAT_MS) {
        publishState(stale);  // retained freshness
      }
    }
  } else {
    ensureWiFi();
  }

  otaServer.handleClient();
  ElegantOTA.loop();
  ArduinoOTA.handle();

  delay(5);  // yield to WiFi stack; detection latency impact: nil
}
