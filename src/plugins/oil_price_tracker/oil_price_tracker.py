"""Energy market board - Brent, WTI and natural gas with trend charts.

Rewritten from a three-number card layout to the boxed, charted idiom the
space_overview plugin uses: ruled panels, uppercase letterspaced labels, big
values with small units.

Data source changed as part of that. The old version called oilpriceapi.com,
which needs an API key and returns only the latest price - there is no history
in the response, so a chart was impossible. Yahoo Finance's public chart
endpoint is keyless, returns the whole series plus 52-week range metadata, and
is already the source stock_tracker uses here, so the two plugins now behave the
same way and share no new dependency.

The charts are hand-built inline SVG polylines - same approach as
stock_tracker. No plotting library and no image encode/decode step, which
matters on a 415 MB Pi where an extra raster buffer is enough to get the
process OOM-killed.

Colour is strictly black, white and #d40000. The panel is BWR and mid greys
dither into visible noise; anything below 200 is also snapped to black by
stem_darken() later in the pipeline, so a grey would not survive anyway.
"""

import logging
import time
from datetime import datetime, timezone

import requests

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HTTP_TIMEOUT = 15

# Yahoo futures tickers. Brent leads because it is the international benchmark
# and the one Indian fuel pricing tracks.
COMMODITIES = [
    {"ticker": "BZ=F", "label": "BRENT CRUDE", "unit": "USD / BBL", "dp": 2},
    {"ticker": "CL=F", "label": "WTI CRUDE", "unit": "USD / BBL", "dp": 2},
    {"ticker": "NG=F", "label": "NATURAL GAS", "unit": "USD / MMBTU", "dp": 3},
]

RANGE_INTERVAL = {
    "5d": "60m", "1mo": "1d", "3mo": "1d", "6mo": "1d", "1y": "1wk",
}
RANGE_LABEL = {
    "5d": "5 DAY", "1mo": "30 DAY", "3mo": "3 MONTH",
    "6mo": "6 MONTH", "1y": "1 YEAR",
}


class OilPriceTracker(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        rng = settings.get("range", "1mo")
        if rng not in RANGE_INTERVAL:
            rng = "1mo"
        theme = settings.get("theme", "dark")

        quotes = []
        errors = []
        for spec in COMMODITIES:
            try:
                quotes.append(self._fetch(spec, rng))
            except Exception as e:
                # One bad ticker must not blank the whole board - the other two
                # are still worth showing.
                logger.warning("oil: %s failed: %s", spec["ticker"], e)
                errors.append(f"{spec['label']}: {e}")

        if not quotes:
            raise RuntimeError("No energy quotes available. " + "; ".join(errors))

        dimensions = device_config.get_resolution()
        try:
            if device_config.get_config("orientation") == "vertical":
                dimensions = dimensions[::-1]
        except Exception:
            pass

        lead, rest = quotes[0], quotes[1:]
        lead["chart"] = self._sparkline(lead["closes"], 430, 150, lead["up"])
        for q in rest:
            q["chart"] = self._sparkline(q["closes"], 210, 52, q["up"])

        template_params = {
            "lead": lead,
            "rest": rest,
            "range_label": RANGE_LABEL[rng],
            "theme": theme,
            # The base template paints the body from an inline style, and the
            # body is 800x480 while the frame is inset to the calibrated 780x470.
            # Without this the 10px margin stayed black on the light theme and
            # printed as a black border round the page.
            # No body background: the frame paints the field, inside the
            # calibrated safe area. Setting it on the body pushed the colour
            # under the wooden mount.
            "plugin_settings": dict(settings),
            "stamp": datetime.now().strftime("%H:%M  %d %b %Y").upper(),
        }

        image = self.render_image(
            dimensions, "oil_price_tracker.html", "oil_price_tracker.css",
            template_params,
        )
        if not image:
            raise RuntimeError("Failed to render oil price image.")
        return image

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _fetch(self, spec, rng):
        resp = requests.get(
            CHART_URL.format(ticker=spec["ticker"]),
            params={"interval": RANGE_INTERVAL[rng], "range": rng},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()

        result = (payload.get("chart") or {}).get("result")
        if not result:
            raise RuntimeError(str((payload.get("chart") or {}).get("error")))

        r = result[0]
        meta = r["meta"]
        raw = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in raw if c is not None]
        if not closes:
            closes = [meta.get("regularMarketPrice", 0)]

        dp = spec["dp"]
        price = meta.get("regularMarketPrice", closes[-1])
        prev = (meta.get("chartPreviousClose") or meta.get("previousClose")
                or closes[0])
        change = price - prev
        pct = (change / prev * 100) if prev else 0

        hi52 = meta.get("fiftyTwoWeekHigh")
        lo52 = meta.get("fiftyTwoWeekLow")
        # Where the current price sits in the 52-week band, as a percentage.
        # Drawn as a marker on a rule, which says more at a glance than the two
        # bare numbers do.
        pos = None
        if hi52 and lo52 and hi52 > lo52:
            pos = max(0.0, min(100.0, (price - lo52) / (hi52 - lo52) * 100))

        return {
            "label": spec["label"],
            "unit": spec["unit"],
            "price": f"{price:,.{dp}f}",
            "up": change >= 0,
            "arrow": "▲" if change >= 0 else "▼",
            "change": f"{abs(change):,.{dp}f}",
            "pct": f"{abs(pct):.2f}%",
            "period_hi": f"{max(closes):,.{dp}f}",
            "period_lo": f"{min(closes):,.{dp}f}",
            "hi52": f"{hi52:,.{dp}f}" if hi52 else "--",
            "lo52": f"{lo52:,.{dp}f}" if lo52 else "--",
            "pos52": pos,
            "closes": closes[-120:],      # cap point count, keeps the SVG light
        }

    # ------------------------------------------------------------------
    # Chart
    # ------------------------------------------------------------------

    @staticmethod
    def _sparkline(closes, width, height, up):
        """Inline SVG trend line with a baseline and an end marker.

        Stroke widths are deliberately heavy. This is a 1-bit panel: a 1px line
        survives the render but the display's own quantisation breaks it into
        dashes, so nothing thinner than ~2px is reliable.

        The end marker is a rect, not a circle. The SVG is stretched to its
        container with preserveAspectRatio="none" so the line always spans the
        full width, and that same stretch turned a circle into a visible
        ellipse. A rect distorts too, but into a rectangle, which reads as
        intentional.
        """
        if not closes or len(closes) < 2:
            return ""

        lo, hi = min(closes), max(closes)
        span = (hi - lo) or 1
        pad = 6
        w, h = width, height
        step = (w - 2 * pad) / (len(closes) - 1)

        pts = []
        for i, c in enumerate(closes):
            x = pad + i * step
            y = pad + (h - 2 * pad) * (1 - (c - lo) / span)
            pts.append((x, y))

        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        lx, ly = pts[-1]

        # currentColor everywhere except the end dot, so one SVG works on both
        # the dark and light themes without being rebuilt.
        return (
            f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none">'
            f'<polyline points="{line}" fill="none" stroke="currentColor" '
            f'stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<rect x="{lx - 5:.1f}" y="{ly - 5:.1f}" width="10" height="10" '
            f'fill="#d40000"/>'
            f'</svg>'
        )
