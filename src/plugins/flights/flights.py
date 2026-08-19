"""
Flights overhead - aircraft currently passing over, as a print.

Visual idiom borrowed from FlightPortrait: a solid colour field, a small
letterspaced wordmark, aircraft profiles stacked with tiny technical captions
beneath. What is NOT borrowed is their artwork - those are hand-drawn airline
liveries, one per airline/type, and they are the actual product. They also
could not survive this panel: Singapore's navy-and-gold or Emirates'
red-white-green have nowhere to go in black/white/red.

So the aircraft are drawn here as silhouettes by family - narrowbody twin,
widebody twin, double-deck, turboprop, regional jet - which reads as a
blueprint rather than a poor imitation of a livery.

Data: adsb.lol, free and keyless. Aircraft are picked closest-first, since
"what is over my house" is the point.
"""

import base64
import logging
import os
import re
from datetime import datetime

import requests

from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import stem_darken

logger = logging.getLogger(__name__)

API = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"

# Cross-hatched photographs, built offline by tools/build_aircraft.py and
# committed with the plugin. Keyed AIRLINE_TYPE where a livery is worth
# recognising, falling back to TYPE, so an Air India Express 737 looks like
# one rather than like a generic jet.
ART_DIR = os.path.join(os.path.dirname(__file__), "render", "aircraft")

# How many aircraft fit is a trade against how large each one is drawn, so the
# row count is a user setting and each choice has its own pre-rendered art size.
# The art cannot be scaled at render time: resampling 1-bit line art fringes
# every stroke into grey, which this panel dithers into noise.
ROW_ART = {2: "large", 3: "small"}
DEFAULT_ROWS = 2

# A callsign is only worth printing when it reads as one - three letters for the
# operator then a flight number. Aircraft with no callsign fall back to their
# ICAO24 address, and printing a bare hex like "8016C7" as though it were an
# airline name just looks broken.
CALLSIGN_RE = re.compile(r"^[A-Z]{3}\d")

# Callsign prefix -> airline. ICAO three-letter prefixes, so IGO1476 is IndiGo.
AIRLINES = {
    "IGO": "IndiGo", "AIC": "Air India", "AXB": "Air India Express",
    "LLR": "Alliance Air", "AKJ": "Akasa Air", "SEJ": "SpiceJet",
    "VTI": "Vistara", "UAE": "Emirates", "QTR": "Qatar Airways",
    "SIA": "Singapore Airlines", "ETD": "Etihad", "THY": "Turkish",
    "BAW": "British Airways", "DLH": "Lufthansa", "AFR": "Air France",
    "KLM": "KLM", "SVA": "Saudia", "MSR": "EgyptAir", "ABY": "Air Arabia",
    "FDB": "flydubai", "GFA": "Gulf Air", "OMA": "Oman Air", "CPA": "Cathay",
    "UAL": "United", "AAL": "American", "DAL": "Delta", "ANA": "ANA",
    "JAL": "Japan Airlines", "CSN": "China Southern", "CES": "China Eastern",
    "DQA": "Maldivian", "IAD": "Air India Express",
}

# ICAO type -> (family, engine count). Family drives which silhouette is drawn.
TYPES = {
    "A380": ("double", 4), "B748": ("double", 4), "B744": ("double", 4),
    "B777": ("wide", 2), "B77W": ("wide", 2), "B772": ("wide", 2),
    "B787": ("wide", 2), "B788": ("wide", 2), "B789": ("wide", 2),
    "A330": ("wide", 2), "A332": ("wide", 2), "A333": ("wide", 2),
    "A339": ("wide", 2), "A350": ("wide", 2), "A359": ("wide", 2),
    "B767": ("wide", 2), "A340": ("wide", 4),
    "A320": ("narrow", 2), "A20N": ("narrow", 2), "A321": ("narrow", 2),
    "A21N": ("narrow", 2), "A319": ("narrow", 2), "A19N": ("narrow", 2),
    "B738": ("narrow", 2), "B38M": ("narrow", 2), "B737": ("narrow", 2),
    "B739": ("narrow", 2), "B39M": ("narrow", 2),
    "AT76": ("prop", 2), "AT75": ("prop", 2), "DH8D": ("prop", 2),
    "E190": ("regional", 2), "E195": ("regional", 2), "E75L": ("regional", 2),
    "CRJ9": ("regional", 2),
}


class Flights(BasePlugin):

    def generate_settings_template(self):
        t = super().generate_settings_template()
        t["style_settings"] = False
        return t

    def generate_image(self, settings, device_config):
        lat = float(settings.get("latitude") or 13.93)
        lon = float(settings.get("longitude") or 75.57)
        radius = int(settings.get("radius") or 150)
        field = settings.get("field", "red")

        aircraft = self._fetch(lat, lon, radius)

        safe = self.get_safe_area(device_config)
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        # Take the closest three *that have artwork*. Slicing first and
        # filtering afterwards silently left blank rows when a type was
        # missing from the library.
        try:
            max_rows = int(settings.get("rows", DEFAULT_ROWS))
        except (TypeError, ValueError):
            max_rows = DEFAULT_ROWS
        size = ROW_ART.get(max_rows, ROW_ART[DEFAULT_ROWS])

        # Portrait always uses the 396px art: the 600px library is wider than
        # the 456px portrait frame, and scaling 1-bit line art fringes every
        # stroke. The taller frame fits five aircraft instead of two.
        is_landscape = dimensions[0] >= dimensions[1]
        if not is_landscape:
            size, max_rows = "small", 4

        rows = []
        for a in aircraft:
            row = self._row(a, lat, lon, size)
            if row:
                rows.append(row)
            if len(rows) == max_rows:
                break

        image = self.render_image(
            dimensions,
            "flights.html" if is_landscape else "flights_portrait.html",
            "flights.css",
            {
                "rows": rows,
                "row_size": size,
                "field": field,
                "count": len(aircraft),
                "radius": radius,
                "stamp": datetime.now().strftime("%H:%M · %d %b %Y").upper(),
                "usable_width": safe["usable_width"],
                "usable_height": safe["usable_height"],
                "plugin_settings": settings,
            })
        if not image:
            raise RuntimeError("Failed to render flights image")
        return stem_darken(image)

    # ------------------------------------------------------------------

    def _fetch(self, lat, lon, dist):
        url = API.format(lat=lat, lon=lon, dist=dist)
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "ZenBoard/1.0 (e-ink frame)"})
        r.raise_for_status()
        ac = r.json().get("ac") or []

        out = []
        for a in ac:
            # Airborne only, and it must have a position to be worth showing.
            alt = a.get("alt_baro")
            if not isinstance(alt, (int, float)):
                continue
            if a.get("lat") is None:
                continue
            out.append(a)

        # closest first - "what is over my house" is the whole point
        out.sort(key=lambda a: self._km(lat, lon, a["lat"], a["lon"]))
        return out

    @staticmethod
    def _km(la1, lo1, la2, lo2):
        from math import radians, sin, cos, asin, sqrt
        la1, lo1, la2, lo2 = map(radians, (la1, lo1, la2, lo2))
        h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
        return 6371 * 2 * asin(sqrt(h))

    @staticmethod
    def _art(call, icao_type, size):
        """Best available image: airline+type, then type, then nothing."""
        for key in ("%s_%s" % (call[:3], icao_type), icao_type):
            path = os.path.join(ART_DIR, size, key + ".png")
            if key and os.path.exists(path):
                with open(path, "rb") as f:
                    return "data:image/png;base64," + base64.b64encode(f.read()).decode()
        return None

    def _row(self, a, lat, lon, size):
        call = (a.get("flight") or "").strip().upper()
        icao_type = (a.get("t") or "").strip().upper()
        family, engines = TYPES.get(icao_type, ("narrow", 2))
        airline = AIRLINES.get(call[:3], "")
        alt_ft = a.get("alt_baro") or 0
        art = self._art(call, icao_type, size)
        if not art:
            # Nothing to show for this type; better to drop the row than to
            # print a caption with an empty space where the aircraft goes.
            return None

        return {
            "art": art,
            # Falls back through callsign, then registration, before giving up.
            # Many aircraft transmit no callsign at all, and their tail number is
            # far more use to a reader than a bare ICAO24 hex would be.
            "callsign": (call if CALLSIGN_RE.match(call)
                         else (a.get("r") or "").strip().upper() or "UNIDENTIFIED"),
            "airline": airline,
            "type": icao_type or "—",
            "reg": (a.get("r") or "").strip(),
            "family": family,
            "engines": engines,
            "alt_ft": f"{int(alt_ft):,}",
            "alt_m": f"{int(alt_ft * 0.3048):,}",
            "speed": int(a.get("gs") or 0),
            "km": round(self._km(lat, lon, a["lat"], a["lon"])),
        }
