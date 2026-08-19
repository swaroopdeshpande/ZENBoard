"""
System Health - ZENBoard
Shows the Pi's own vitals on the e-ink panel: CPU, memory, disk, temp,
uptime, load, network, throttle/undervoltage status, and today's e-ink
refresh count. Built specifically because this Pi has had real
crash/unresponsiveness incidents this session and there was no way to see
*why* after the fact - the throttled/undervoltage flag in particular
directly targets that.

No external API calls, no key needed - everything comes from psutil,
vcgencmd, and /proc. Dark-mode-off (light, BWR) to match the rest of the
dashboard's default look; red is reserved for values in warning territory.
"""

import logging
import subprocess
import time
from datetime import datetime, timedelta

import psutil

from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import stem_darken

logger = logging.getLogger(__name__)

# Warning thresholds - anything at/over these renders in red.
THRESH_CPU_PCT = 80
THRESH_TEMP_C = 70
THRESH_MEM_PCT = 85
THRESH_DISK_PCT = 90
THRESH_SWAP_PCT = 50


class SystemHealth(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== System Health: Starting ===")

        stats = self._collect_stats(device_config)
        dark = (settings or {}).get("displayMode", "light") == "dark"

        dimensions = device_config.get_resolution()
        # Portrait means a physically rotated frame, so the canvas rotates
        # with it. Without this the plugin lays out for 800x480 and is then
        # squeezed into a 480x800 panel.
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        safe = self.get_safe_area(device_config)
        template = "system_health_landscape.html" if is_landscape else "system_health_portrait.html"

        image = self.render_image(
            dimensions,
            template,
            "system_health.css",
            {
                **stats,
                "dark": dark,
                "frame_w": safe["usable_width"],
                "frame_h": safe["usable_height"],
                "plugin_settings": {
                    "topMargin": safe.get("top", 12),
                    "bottomMargin": safe.get("bottom", 0),
                    "leftMargin": safe.get("left", 8),
                    "rightMargin": safe.get("right", 11),
                    "backgroundOption": "color",
                    "backgroundColor": "#000000" if dark else "#ffffff",
                    "textColor": "#ffffff" if dark else "#000000",
                    "selectedFrame": "None",
                },
            },
        )

        if not image:
            raise RuntimeError("Failed to render System Health image")

        logger.info("=== System Health: Complete ===")
        image = stem_darken(image)
        return image

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def _collect_stats(self, device_config):
        now = datetime.now()

        per_core = psutil.cpu_percent(interval=0.6, percpu=True)
        cpu_pct = sum(per_core) / len(per_core)
        load1, load5, load15 = psutil.getloadavg()

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")

        uptime_seconds = time.time() - psutil.boot_time()
        uptime_str = self._fmt_duration(uptime_seconds)

        temp_c = self._cpu_temp()
        throttled = self._throttle_status()

        cpu_freq = psutil.cpu_freq()
        freq_mhz = round(cpu_freq.current) if cpu_freq else None

        proc_count = len(psutil.pids())

        wifi_ssid, wifi_signal = self._wifi_info()
        local_ip = self._local_ip()
        tailscale_ip = self._tailscale_ip()

        net = psutil.net_io_counters()
        net_sent_mb = round(net.bytes_sent / 1024 / 1024)
        net_recv_mb = round(net.bytes_recv / 1024 / 1024)

        inkypi_active = self._service_active("inkypi")
        tailscale_active = self._service_active("tailscaled")

        try:
            from utils.refresh_stats import get_daily_refresh_count
            refresh_data = get_daily_refresh_count()
            refresh_count = refresh_data.get("count", 0)
        except Exception as e:
            logger.debug(f"Refresh count unavailable: {e}")
            refresh_count = None

        return {
            "hostname": self._hostname(),
            "timestamp": now.strftime("%I:%M %p").lstrip("0") + " · " + now.strftime("%b %d"),

            "cpu_pct": round(cpu_pct),
            "cpu_warn": cpu_pct >= THRESH_CPU_PCT,
            "per_core": [round(c) for c in per_core],

            "temp_c": round(temp_c, 1) if temp_c is not None else None,
            "temp_warn": (temp_c or 0) >= THRESH_TEMP_C,

            "mem_pct": round(mem.percent),
            "mem_warn": mem.percent >= THRESH_MEM_PCT,
            "mem_used_mb": round(mem.used / 1024 / 1024),
            "mem_total_mb": round(mem.total / 1024 / 1024),

            "disk_pct": round(disk.percent),
            "disk_warn": disk.percent >= THRESH_DISK_PCT,
            "disk_used_gb": round(disk.used / 1024**3, 1),
            "disk_total_gb": round(disk.total / 1024**3, 1),

            "swap_pct": round(swap.percent),
            "swap_warn": swap.percent >= THRESH_SWAP_PCT,
            "swap_used_mb": round(swap.used / 1024 / 1024),

            "uptime_str": uptime_str,
            "load1": round(load1, 2),
            "load5": round(load5, 2),
            "load15": round(load15, 2),
            "load_warn": load1 >= psutil.cpu_count(),

            "freq_mhz": freq_mhz,
            "proc_count": proc_count,

            "wifi_ssid": wifi_ssid,
            "wifi_signal": wifi_signal,
            "local_ip": local_ip,
            "tailscale_ip": tailscale_ip,
            "net_sent_mb": net_sent_mb,
            "net_recv_mb": net_recv_mb,

            "throttled_raw": throttled["raw"],
            "throttled_now": throttled["now_flags"],
            "throttled_ever": throttled["ever_flags"],
            "throttle_warn": bool(throttled["now_flags"]) or bool(throttled["ever_flags"]),

            "inkypi_active": inkypi_active,
            "tailscale_active": tailscale_active,

            "refresh_count": refresh_count,
        }

    # ------------------------------------------------------------------
    # Individual metric helpers - each is defensive, never raises. A
    # missing metric shows as "--" in the template rather than breaking
    # the whole render.
    # ------------------------------------------------------------------

    @staticmethod
    def _cpu_temp():
        try:
            out = subprocess.run(
                ["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=3
            )
            # "temp=49.4'C"
            return float(out.stdout.strip().split("=")[1].split("'")[0])
        except Exception:
            pass
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            return None

    @staticmethod
    def _throttle_status():
        """Decode vcgencmd get_throttled bitmask. This is the single most
        useful metric for diagnosing the mystery crashes/hangs - bit 0
        means undervoltage is happening RIGHT NOW."""
        FLAGS = {
            0: "Undervoltage",
            1: "Freq capped",
            2: "Throttled",
            3: "Soft temp limit",
            16: "Undervoltage occurred",
            17: "Freq capped occurred",
            18: "Throttled occurred",
            19: "Soft temp limit occurred",
        }
        try:
            out = subprocess.run(
                ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3
            )
            raw = out.stdout.strip().split("=")[1]
            val = int(raw, 16)
            now_flags = [label for bit, label in FLAGS.items() if bit < 16 and val & (1 << bit)]
            ever_flags = [
                label for bit, label in FLAGS.items() if bit >= 16 and val & (1 << bit)
            ]
            return {"raw": raw, "now_flags": now_flags, "ever_flags": ever_flags}
        except Exception:
            return {"raw": None, "now_flags": [], "ever_flags": []}

    @staticmethod
    def _fmt_duration(seconds):
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @staticmethod
    def _hostname():
        try:
            with open("/proc/sys/kernel/hostname") as f:
                return f.read().strip()
        except Exception:
            return "zenboard"

    @staticmethod
    def _wifi_info():
        try:
            ssid = subprocess.run(
                ["iwgetid", "-r"], capture_output=True, text=True, timeout=3
            ).stdout.strip()
        except Exception:
            ssid = None
        signal = None
        try:
            out = subprocess.run(
                ["nmcli", "-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list"],
                capture_output=True, text=True, timeout=5
            ).stdout
            for line in out.splitlines():
                if line.startswith("*"):
                    signal = int(line.split(":")[1])
                    break
        except Exception:
            pass
        return (ssid or None), signal

    @staticmethod
    def _local_ip():
        try:
            for iface, addrs in psutil.net_if_addrs().items():
                if iface == "wlan0":
                    for a in addrs:
                        if a.family.name == "AF_INET":
                            return a.address
        except Exception:
            pass
        return None

    @staticmethod
    def _tailscale_ip():
        try:
            out = subprocess.run(
                ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
            )
            return out.stdout.strip() or None
        except Exception:
            return None

    @staticmethod
    def _service_active(name):
        try:
            out = subprocess.run(
                ["systemctl", "is-active", name], capture_output=True, text=True, timeout=3
            )
            return out.stdout.strip() == "active"
        except Exception:
            return None
