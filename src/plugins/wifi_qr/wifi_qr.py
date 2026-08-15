"""
ZenBoard WiFi QR Plugin
Displays QR code on e-ink when Pi is in AP mode (no known WiFi found).
Layout: welcome banner top, big QR code centered in the middle, IP/instructions
below. Light mode only (fixed white background), full BWR palette for accents.
"""

import json
import logging

from PIL import Image, ImageDraw, ImageFont
from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

STATUS_FILE = "/tmp/zenboard_wifi_status.json"

FONT_OPTIONS = {
    "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "sans": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
}


class WifiQr(BasePlugin):

    def generate_image(self, settings, device_config):
        try:
            with open(STATUS_FILE) as f:
                status = json.load(f)
        except Exception:
            status = {}

        ap_ssid = status.get("ap_ssid") or "ZenBoard-Setup"
        ap_password = status.get("ap_password") or "changeme-zenboard"
        ap_ip = status.get("ap_ip") or "192.168.4.1"
        setup_url = f"http://{ap_ip}/setup"

        safe = self.get_safe_area(device_config)
        frame_w = safe["usable_width"]
        frame_h = safe["usable_height"]

        # Always light mode - fixed white canvas regardless of any app theme.
        img = Image.new("RGB", (frame_w, frame_h), (255, 255, 255))
        draw = ImageDraw.Draw(img)

        BLACK = (0, 0, 0)
        RED = (204, 0, 0)

        try:
            font_title = ImageFont.truetype(FONT_OPTIONS["bold"], 30)
            font_sub = ImageFont.truetype(FONT_OPTIONS["bold"], 16)
            font_mono = ImageFont.truetype(FONT_OPTIONS["bold"], 18)
            font_small = ImageFont.truetype(FONT_OPTIONS["sans"], 14)
        except Exception:
            font_title = font_sub = font_mono = font_small = ImageFont.load_default()

        def center_text(text, y, font, fill):
            bbox = draw.textbbox((0, 0), text, font=font)
            w = bbox[2] - bbox[0]
            draw.text(((frame_w - w) // 2, y), text, font=font, fill=fill)
            return bbox[3] - bbox[1]

        # ── TOP: welcome banner ──
        y = 10
        y += center_text("Welcome to ZenBoard", y, font_title, BLACK) + 6
        y += center_text(f"Connect to Wi-Fi “{ap_ssid}” to finish setup", y, font_sub, RED) + 4

        # thin red rule under banner
        draw.line([(frame_w * 0.18, y + 6), (frame_w * 0.82, y + 6)], fill=RED, width=2)
        y += 18

        # ── MIDDLE: big QR code, framed ──
        try:
            import qrcode
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=2,
            )
            qr.add_data(setup_url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

            # Reserve space for the bottom block (IP + steps) below the QR.
            bottom_block_h = 92
            available_h = frame_h - y - bottom_block_h - 14
            qr_size = min(available_h, frame_w - 60)
            qr_size = max(qr_size, 100)
            qr_img = qr_img.resize((qr_size, qr_size), Image.LANCZOS)

            qr_x = (frame_w - qr_size) // 2
            qr_y = y

            # red frame around the QR, white quiet-zone padding
            pad = 10
            draw.rectangle(
                [(qr_x - pad, qr_y - pad), (qr_x + qr_size + pad, qr_y + qr_size + pad)],
                outline=RED, width=3,
            )
            img.paste(qr_img, (qr_x, qr_y))

            y = qr_y + qr_size + pad + 14
        except ImportError:
            y += center_text("Install: pip install qrcode[pil]", y, font_small, RED) + 6
            qr_size = 0

        # ── BOTTOM: IP address + credentials ──
        y += center_text(f"Or connect manually — Wi-Fi: {ap_ssid}  ·  Pass: {ap_password}", y, font_small, BLACK) + 6
        y += center_text(setup_url, y, font_mono, RED) + 4
        center_text(f"IP: {ap_ip}", y, font_sub, BLACK)

        # ── apply calibrated safe area ──
        background = Image.new("RGB", device_config.get_resolution(), color="white")
        background.paste(img, (safe.get("start_x", 0), safe.get("start_y", 0)))
        return background
