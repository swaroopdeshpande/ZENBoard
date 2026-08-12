import io
import json
import logging
import math
import time
import requests
import ephem
import base64
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from PIL import Image

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

CACHE_FILE = "/tmp/space_overview_cache.json"
ISS_ID = 25544

# Bengaluru defaults
DEFAULT_LAT = 12.9716
DEFAULT_LNG = 77.5946
DEFAULT_ALT = 920


class SpaceOverview(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        template_params["plugin_settings"] = {}
        template_params["plugin_instance"] = ""
        template_params["refresh_settings"] = {}
        return template_params

    def generate_image(self, settings, device_config):
        n2yo_key = settings.get("n2yoKey", "").strip()
        lat = float(settings.get("lat", DEFAULT_LAT))
        lng = float(settings.get("lng", DEFAULT_LNG))
        alt = float(settings.get("alt", DEFAULT_ALT))
        cache_ttl = int(settings.get("cacheTtl", "300"))

        data = self._get_space_data(n2yo_key, lat, lng, alt, cache_ttl)

        dimensions = device_config.get_resolution()
        is_landscape = dimensions[0] > dimensions[1]

        map_b64 = self._render_map(data, dimensions, is_landscape)

        template = "space_landscape.html" if is_landscape else "space_portrait.html"
        return self.render_image(
            dimensions,
            template,
            "space.css",
            {
                "iss": data["iss"],
                "next_pass": data["next_pass"],
                "moon": data["moon"],
                "launches": data["launches"],
                "map_b64": map_b64,
                "timestamp": data["timestamp"],
                "plugin_settings": settings,
                "logo_position": settings.get("logoPosition", "bottom-right"),
                "theme": settings.get("theme", "dark"),
            },
        )

    def _get_space_data(self, n2yo_key, lat, lng, alt, cache_ttl):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
            if time.time() - cache.get("timestamp", 0) < cache_ttl:
                logger.info("Using cached space data")
                return cache["data"]
        except Exception:
            pass

        data = {
            "iss": self._get_iss_position(),
            "next_pass": self._get_next_pass(n2yo_key, lat, lng, alt),
            "moon": self._get_moon_phase(),
            "launches": self._get_upcoming_launches(),
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        }

        try:
            with open(CACHE_FILE, "w") as f:
                json.dump({"timestamp": time.time(), "data": data}, f)
        except Exception:
            pass

        return data

    def _get_iss_position(self):
        try:
            r = requests.get("https://api.wheretheiss.at/v1/satellites/25544", timeout=10)
            d = r.json()
            return {
                "lat": round(d["latitude"], 2),
                "lng": round(d["longitude"], 2),
                "altitude": round(d["altitude"]),
                "speed": round(d["velocity"]),
                "visibility": d.get("visibility", "unknown"),
            }
        except Exception as e:
            logger.error(f"ISS position error: {e}")
            return {"lat": 0, "lng": 0, "altitude": 408, "speed": 27600, "visibility": "unknown"}

    def _get_next_pass(self, n2yo_key, lat, lng, alt):
        if not n2yo_key:
            return {"countdown": "N2YO key needed", "az": "N/A", "duration": "N/A", "visibility": "N/A"}
        try:
            url = f"https://api.n2yo.com/rest/v1/satellite/visualpasses/{ISS_ID}/{lat}/{lng}/{alt}/5/300/&apiKey={n2yo_key}"
            r = requests.get(url, timeout=10)
            d = r.json()
            passes = d.get("passes", [])
            if not passes:
                return {"countdown": "No pass soon", "az": "N/A", "duration": "N/A", "visibility": "N/A"}

            next_pass = passes[0]
            start_utc = next_pass["startUTC"]
            now = time.time()
            diff = start_utc - now

            if diff < 0:
                countdown = "VISIBLE NOW"
            else:
                days = int(diff // 86400)
                hours = int((diff % 86400) // 3600)
                mins = int((diff % 3600) // 60)
                if days > 0:
                    countdown = f"T-{days}D {hours}H"
                elif hours > 0:
                    countdown = f"T-{hours}H {mins}M"
                else:
                    countdown = f"T-{mins}M"

            mag = next_pass.get("maxEl", 0)
            visibility = "HIGH VISIBILITY" if mag > 40 else "LOW VISIBILITY"

            return {
                "countdown": countdown,
                "duration": f"{next_pass.get('duration', 0)}s",
                "max_elevation": f"{mag}°",
                "visibility": visibility,
                "start_time": datetime.utcfromtimestamp(start_utc).strftime("%b %d %H:%M UTC"),
            }
        except Exception as e:
            logger.error(f"Next pass error: {e}")
            return {"countdown": "Error", "az": "N/A", "duration": "N/A", "visibility": "N/A"}

    def _get_moon_phase(self):
        try:
            now = datetime.utcnow()
            moon = ephem.Moon(now)
            phase_pct = round(moon.phase * 100, 1)
            
            # Calculate age from new moon
            new_moon = ephem.previous_new_moon(now)
            age = (ephem.Date(now) - ephem.Date(new_moon)) * 29.53

            # Determine phase name
            if age < 1.85:
                phase_name = "NEW MOON"
            elif age < 7.38:
                phase_name = "WAXING CRESCENT"
            elif age < 9.22:
                phase_name = "FIRST QUARTER"
            elif age < 14.77:
                phase_name = "WAXING GIBBOUS"
            elif age < 16.61:
                phase_name = "FULL MOON"
            elif age < 22.15:
                phase_name = "WANING GIBBOUS"
            elif age < 23.99:
                phase_name = "LAST QUARTER"
            else:
                phase_name = "WANING CRESCENT"

            # Next full moon
            next_full = ephem.next_full_moon(datetime.utcnow())
            next_full_dt = ephem.Date(next_full).datetime()
            next_full_str = next_full_dt.strftime("%b %d").upper()

            return {
                "phase_name": phase_name,
                "illumination": phase_pct,
                "next_full": next_full_str,
                "age": round(age, 1),
            }
        except Exception as e:
            logger.error(f"Moon phase error: {e}")
            return {"phase_name": "UNKNOWN", "illumination": 0, "next_full": "N/A", "age": 0}

    def _get_upcoming_launches(self):
        try:
            url = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=3&format=json"
            r = requests.get(url, timeout=10)
            d = r.json()
            launches = []
            for launch in d.get("results", [])[:3]:
                net = launch.get("net", "")
                try:
                    net_dt = datetime.fromisoformat(net.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    diff = (net_dt - now).total_seconds()
                    days = int(diff // 86400)
                    hours = int((diff % 86400) // 3600)
                    if days > 0:
                        countdown = f"T-{days}D {hours}H"
                    else:
                        countdown = f"T-{hours}H"
                except Exception:
                    countdown = "TBD"

                launches.append({
                    "name": launch.get("name", "Unknown")[:30],
                    "countdown": countdown,
                    "provider": launch.get("launch_service_provider", {}).get("abbrev", "")[:10],
                })
            return launches
        except Exception as e:
            logger.error(f"Launches error: {e}")
            return [{"name": "Data unavailable", "countdown": "N/A", "provider": ""}]

    def _render_map(self, data, dimensions, is_landscape):
        """Render world map with ISS position and orbit path."""
        try:
            w, h = dimensions
            fig_w = (w * 0.58) / 96 if is_landscape else (w * 0.95) / 96
            fig_h = (h * 0.52) / 96 if is_landscape else (h * 0.38) / 96

            fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=96)
            fig.patch.set_facecolor("#0a0a0a")
            ax.set_facecolor("#0a0a0a")

            # Draw dotted world map using country boundaries
            try:
                import cartopy.crs as ccrs
                import cartopy.feature as cfeature
                ax.remove()
                ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
                ax.set_facecolor("#0a0a0a")
                ax.set_global()

                # Add land as dotted pattern
                ax.add_feature(cfeature.LAND, facecolor="#2a2a2a", edgecolor="#444444", linewidth=0.3)
                ax.add_feature(cfeature.OCEAN, facecolor="#0a0a0a")
                ax.add_feature(cfeature.COASTLINE, edgecolor="#555555", linewidth=0.4)
                ax.gridlines(color="#1a1a1a", linewidth=0.3, linestyle="--")

                iss_lat = data["iss"]["lat"]
                iss_lng = data["iss"]["lng"]

                # Simulate orbit path (ISS orbital period ~92 min, inclination ~51.6°)
                orbit_lngs = np.linspace(iss_lng - 180, iss_lng + 180, 200)
                orbit_lats = 51.6 * np.sin(np.radians(orbit_lngs - iss_lng + 90))

                # Past path (dashed)
                ax.plot(orbit_lngs[:100], orbit_lats[:100],
                       color="#888888", linewidth=0.8, linestyle="--",
                       transform=ccrs.PlateCarree(), alpha=0.6)

                # Future path (solid)
                ax.plot(orbit_lngs[100:], orbit_lats[100:],
                       color="#ffffff", linewidth=1.0,
                       transform=ccrs.PlateCarree(), alpha=0.8)

                # ISS dot
                ax.scatter([iss_lng], [iss_lat], color="white", s=60, zorder=5,
                          transform=ccrs.PlateCarree(), linewidths=1.5, edgecolors="#aaaaaa")

                ax.set_extent([-180, 180, -90, 90])

            except Exception as e:
                logger.warning(f"Cartopy failed: {e}, using gridmap")
                # Fallback without cartopy — simple grid
                ax.set_xlim(-180, 180)
                ax.set_ylim(-90, 90)
                ax.set_facecolor("#0a0a0a")
                ax.grid(color="#333333", linewidth=0.5, alpha=0.8)

                iss_lat = data["iss"]["lat"]
                iss_lng = data["iss"]["lng"]

                orbit_lngs = np.linspace(-180, 180, 200)
                orbit_lats = 51.6 * np.sin(np.radians(orbit_lngs - iss_lng + 90))
                ax.plot(orbit_lngs, orbit_lats, color="#ffffff", linewidth=1.0, alpha=0.7)
                ax.scatter([iss_lng], [iss_lat], color="white", s=60, zorder=5)

            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_visible(False)

            plt.tight_layout(pad=0)

            buf = io.BytesIO()
            plt.savefig(buf, format="png", bbox_inches="tight",
                       facecolor="#0a0a0a", dpi=96, pad_inches=0)
            plt.close(fig)
            buf.seek(0)
            return base64.b64encode(buf.read()).decode("utf-8")

        except Exception as e:
            logger.error(f"Map render error: {e}")
            return ""
