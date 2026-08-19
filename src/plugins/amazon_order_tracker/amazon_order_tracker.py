"""
Amazon Order Tracker - Gmail API Edition v3
Robust subject-pattern parsing, dedup, correct status priority sort.
"""

import email
import imaplib
import logging
import json
import re
import time
import base64
import io
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from PIL import Image

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

CACHE_DIR = Path("/home/zenith/InkyPi/src/plugins/amazon_order_tracker/.cache")
CACHE_DIR.mkdir(exist_ok=True)

CACHE_FILE = CACHE_DIR / "amazon_orders_cache.json"

# Subject prefix -> status. Checked in order (most specific first).
# Real-world subjects mined from actual Amazon.in account (see conversation):
#   "Item cancelled successfully: ..." / "Item cancelled: ..."
#   "Your Amazon order XXX has been cancelled"
#   "Your refund for ..." / "Your return of ..."
#   "Problem during shipping: ..."
#   "Payment declined: ..."
#   "Delivered: ..." / "Delivered in 3 hrs 13 mins"
#   "Delivery attempted: ..."
#   "Out for delivery: ..."
#   "Shipped: ..."
#   "Ordered: ..."
SUBJECT_STATUS_PATTERNS = [
    (r"^Item cancelled", "Cancelled"),
    (r"has been cancelled", "Cancelled"),
    (r"^Your refund for", "Refunded"),
    (r"^Your return of", "Returned"),
    (r"^Problem during shipping", "Problem"),
    (r"^Payment declined", "Payment Issue"),
    (r"^Delivered", "Delivered"),
    (r"^Delivery attempted", "Delivery Attempted"),
    (r"^Out for delivery", "Out for Delivery"),
    (r"^Shipped", "Shipped"),
    (r"^Ordered", "Ordered"),
]

# Subjects that are not order-status emails at all (surveys, feedback asks).
EXCLUDE_SUBJECT_PATTERNS = [
    r"^Rate your",
    r"feedback",
    r"^How was",
    r"review your",
    r"^Your .* review",
    r"^Sign-in",
]

# Bidi isolate marks Amazon wraps quantity numbers in: "Ordered: ⁦2⁩ \"X\""
BIDI_MARKS_RE = re.compile(r"[⁦⁧⁨⁩]")

ORDER_NUM_RE = re.compile(r"(\d{3}-\d{7}-\d{7})")
# Any of the quote styles Amazon uses: " " " ' ' '
QUOTE_CHARS = "\"“”'‘’"
QUOTED_NAME_RE = re.compile(
    r"[:]\s*\d*\s*[" + QUOTE_CHARS + r"]([^" + QUOTE_CHARS + r"]{3,90})[" + QUOTE_CHARS + r"]"
)
MORE_ITEMS_RE = re.compile(r"and\s+(\d+)\s+(?:more|other)\s+items?", re.IGNORECASE)

# Real product photo URLs Amazon embeds in Ordered/Shipped emails (not in
# Out-for-delivery/Delivered templates - those only have logos/icons).
IMAGE_URL_RE = re.compile(r"https://m\.media-amazon\.com/images/I/[A-Za-z0-9+_./-]+\.jpg")

# "Arriving Wed, Aug 13" / "Arriving today" / "Guaranteed delivery by Wed, August 13"
ARRIVING_RE = re.compile(
    r"(?:Arriving|Guaranteed delivery by)\s*[:\-]?\s*"
    r"(today|tomorrow|[A-Za-z]{3,9},?\s+[A-Za-z]{3,9}\s+\d{1,2})",
    re.IGNORECASE,
)

STATUS_ICON = {
    "Ordered": "ORD",
    "Shipped": "SHP",
    "Out for Delivery": "OUT",
    "Delivery Attempted": "!",
    "Delivered": "OK",
    "Problem": "!!!",
    "Payment Issue": "$!",
    "Refunded": "RFD",
    "Returned": "RET",
    "Cancelled": "X",
}

# Statuses that need the red channel (urgent / needs-attention on BWR display)
URGENT_STATUSES = {"Problem", "Payment Issue", "Cancelled", "Delivery Attempted", "Out for Delivery"}

STATUS_DELIVERY_LABEL = {
    "Ordered": "STATUS",
    "Shipped": "STATUS",
    "Out for Delivery": "ARRIVING",
    "Delivery Attempted": "ATTEMPTED",
    "Delivered": "DELIVERED",
    "Problem": "ISSUE",
    "Payment Issue": "ISSUE",
    "Refunded": "REFUNDED",
    "Returned": "RETURNED",
    "Cancelled": "STATUS",
}


class AmazonOrderTracker(BasePlugin):
    """Amazon order tracker using Gmail API."""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        template_params["display_modes"] = [
            {"value": "todays_deliveries", "label": "Today's Deliveries"},
            {"value": "in_transit", "label": "In Transit"},
            {"value": "latest_orders", "label": "Latest Orders"},
            {"value": "all_active", "label": "All Active Orders"},
            {"value": "this_week", "label": "Arriving This Week"},
            {"value": "detailed", "label": "Detailed View"},
        ]
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== Amazon Order Tracker (Gmail v3): Starting ===")

        display_mode = settings.get("displayMode", "todays_deliveries")
        cache_ttl = int(settings.get("cacheTtl", "900"))

        orders, auth_error = self._get_orders(cache_ttl, settings)
        if not orders:
            raise RuntimeError("No Amazon orders found in Gmail. Check credentials.")

        # Amazon doesn't always send a "Delivered" email (payment issues,
        # missed notifications, etc). Without that signal a Shipped/Out for
        # Delivery order would sit in "in transit" forever. Recomputed live
        # every render since staleness is time-relative - never baked into
        # the cache.
        orders = self._apply_staleness(orders)

        logger.info(f"Total unique orders: {len(orders)}")

        filtered_orders = self._filter_orders_by_mode(orders, display_mode)

        if not filtered_orders:
            filtered_orders = sorted(
                orders, key=lambda x: x.get("email_date_obj", ""), reverse=True
            )[:1]

        logger.info(f"Filtered to {len(filtered_orders)} for mode '{display_mode}'")

        # Rows are compact (~90px) now, so landscape/portrait fit more than 3.
        if display_mode == "detailed":
            display_orders = filtered_orders[:1]
        elif display_mode == "todays_deliveries":
            display_orders = filtered_orders[:3]
        else:
            display_orders = filtered_orders[:5]

        # Only fetch photos for the handful actually shown - not all 39
        # orders. Keeps this light enough for a Pi Zero 2W.
        for order in display_orders:
            if order.get("image_url"):
                order["image_b64"] = self._fetch_product_image_b64(order["image_url"])

        dimensions = device_config.get_resolution()
        # Portrait means a physically rotated frame, so the canvas rotates
        # with it. Without this the plugin lays out for 800x480 and is then
        # squeezed into a 480x800 panel.
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        template = "amazon_v2_detailed.html" if display_mode == "detailed" else \
                   "amazon_v2_landscape.html" if is_landscape else "amazon_v2_portrait.html"

        image = self.render_image(
            dimensions,
            template,
            "amazon_v2.css",
            {
                "orders": display_orders,
                "order": display_orders[0] if display_orders else None,
                "display_mode": display_mode,
                "multi": len(display_orders) > 1,
                "auth_error": auth_error,
                "plugin_settings": settings,
            }
        )

        if not image:
            raise RuntimeError("Failed to render image")

        logger.info("=== Amazon Order Tracker (Gmail v3): Complete ===")
        return image

    # ------------------------------------------------------------------
    # Staleness (no Delivered email ever arrived for an old Shipped order)
    # ------------------------------------------------------------------

    STALE_CUTOFF_DAYS = 12

    def _apply_staleness(self, orders):
        now = datetime.now()
        for o in orders:
            if o.get("status") not in ("Shipped", "Out for Delivery", "Delivery Attempted"):
                continue
            try:
                email_dt = datetime.fromisoformat(o["email_date_obj"])
            except Exception:
                continue
            if (now - email_dt).days > self.STALE_CUTOFF_DAYS:
                o["status"] = "Presumed Delivered"
                o["status_icon"] = "OK?"
                o["delivery_label"] = "LAST UPDATE"
                o["delivery_date"] = email_dt.strftime("%d %b")
        return orders

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _filter_orders_by_mode(self, orders, mode):
        today = datetime.now().date()
        week_end = today + timedelta(days=7)

        # Always sort newest-first by email date as base ordering
        orders = sorted(orders, key=lambda x: x.get("email_date_obj", ""), reverse=True)

        if mode == "todays_deliveries":
            return [
                o for o in orders
                if o.get("status") in ("Out for Delivery", "Delivery Attempted")
                or (o.get("status") == "Delivered" and o.get("is_today"))
                # Shipped order carrying an "Arriving today" promise from the
                # email body - don'''t wait on Amazon'''s Out-for-delivery email
                # to actually send before showing it as due today.
                or (o.get("status") == "Shipped" and o.get("arriving_today"))
            ]

        elif mode == "in_transit":
            return [
                o for o in orders
                if o.get("status") in ("Shipped", "Out for Delivery", "Delivery Attempted")
            ]

        elif mode == "latest_orders":
            return orders[:10]

        elif mode == "all_active":
            return [
                o for o in orders
                if o.get("status") not in ("Delivered", "Presumed Delivered", "Cancelled")
            ]

        elif mode == "this_week":
            return [
                o for o in orders
                if o.get("status") not in ("Delivered", "Presumed Delivered", "Cancelled")
            ][:10]

        elif mode == "detailed":
            active = [o for o in orders if o.get("status") not in ("Delivered", "Cancelled")]
            return active if active else orders[:1]

        return orders

    # ------------------------------------------------------------------
    # Fetch / cache
    # ------------------------------------------------------------------

    def _get_orders(self, cache_ttl, settings):
        """Returns (orders, auth_error). auth_error is set whenever we had to
        fall back to a stale cache because the live Gmail fetch failed - the
        caller surfaces this as a visible banner instead of silently serving
        old data with no indication anything's wrong (that silent fallback
        is exactly what let a week-old "Shipped" status sit unnoticed)."""
        if CACHE_FILE.exists():
            try:
                with open(CACHE_FILE, "r") as f:
                    cached = json.load(f)
                if time.time() - cached.get("timestamp", 0) < cache_ttl:
                    logger.info("Using cached orders")
                    return cached.get("orders", []), None
            except Exception as e:
                logger.warning(f"Cache read failed: {e}")

        try:
            orders = self._fetch_via_imap(settings)
            self._save_cache(orders)
            return orders, None
        except Exception as e:
            logger.error(f"Gmail fetch failed: {e}")
            if CACHE_FILE.exists():
                try:
                    with open(CACHE_FILE, "r") as f:
                        cached = json.load(f)
                    cache_age_hours = round((time.time() - cached.get("timestamp", 0)) / 3600, 1)
                    return cached.get("orders", []), {
                        "message": str(e),
                        "cache_age_hours": cache_age_hours,
                    }
                except Exception:
                    pass
            raise RuntimeError(f"Failed to fetch orders: {e}")

    def _fetch_via_imap(self, settings):
        """Gmail App Password + IMAP - no OAuth, no Google Cloud project, no
        7-day refresh-token expiry. Uses Gmail's X-GM-RAW IMAP extension,
        which accepts the exact same search syntax as the old Gmail API
        query did, so the filtering logic didn't need to change."""
        gmail_email = (settings.get("gmailEmail") or "").strip()
        app_password = (settings.get("gmailAppPassword") or "").strip().replace(" ", "")

        if not gmail_email or not app_password:
            raise RuntimeError("Gmail address and app password required. Set them in plugin settings.")

        logger.info("Fetching Amazon orders via IMAP...")

        query = (
            'from:(amazon.in) '
            'subject:(Ordered OR Shipped OR "Out for delivery" OR Delivered OR '
            '"Delivery attempted" OR "Problem during shipping" OR "Payment declined" OR '
            'cancelled OR canceled OR refund OR return)'
        )

        # 15s socket timeout - imaplib has NO timeout by default, so a
        # single stalled read can hang the whole render forever with zero
        # error logged (this hung for 10+ minutes before this fix landed).
        imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        try:
            imap.login(gmail_email, app_password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"IMAP login failed - check email/app password: {e}")

        try:
            imap.select("INBOX", readonly=True)
            escaped_query = query.replace(chr(92), chr(92)+chr(92)).replace(chr(34), chr(92)+chr(34))
            typ, data = imap.uid("search", None, "X-GM-RAW", f'"{escaped_query}"')
            if typ != "OK":
                raise RuntimeError(f"IMAP search failed: {typ}")

            uids = data[0].split()
            # newest first, capped the same way the old maxResults=150 was
            uids = uids[-150:]
            logger.info(f"IMAP search returned {len(uids)} messages")

            by_order = {}
            skipped = 0

            # One fetch per message - batching multiple full RFC822 bodies
            # into a single FETCH command turned out to stall badly on this
            # Pi (likely buffering a multi-MB single response with images
            # inline is too much at once); per-message fetches make steady,
            # visible progress even though each is its own round-trip.
            # timeout=15 on the connection means a single stuck fetch can
            # only cost 15s, not hang forever like before this fix.
            uid_list = list(reversed(uids))
            for idx, uid in enumerate(uid_list):
                try:
                    typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        skipped += 1
                        continue
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)
                    order = self._parse_amazon_email(msg)
                    if not order:
                        skipped += 1
                        continue

                    key = order["order_number"] or f"unknown-{uid}"
                    existing = by_order.get(key)
                    if not existing:
                        by_order[key] = order
                    else:
                        self._merge_order(existing, order)
                except Exception as e:
                    logger.debug(f"Parse error on uid {uid}: {e}")
                    skipped += 1

                if (idx + 1) % 20 == 0:
                    logger.info(f"Fetched {idx + 1}/{len(uid_list)}")

            logger.info(f"Parsed {len(by_order)} unique orders, skipped {skipped} emails")
            return list(by_order.values())
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    @staticmethod
    def _merge_order(existing, new):
        """Merge duplicate order emails into single order record in-place on `existing`.

        Amazon always sends status emails in chronological order (Ordered ->
        Shipped -> Out for delivery -> Delivered, or an exception like
        Problem/Cancelled/Refunded at any point). IMAP search result order
        is NOT guaranteed chronological, so purely compare timestamps -
        whichever email is newer wins the status, full stop. This avoids a
        stale "Shipped" surviving over a later "Problem during shipping" or
        "Cancelled" email.
        """
        if new["email_date_obj"] >= existing["email_date_obj"]:
            existing["status"] = new["status"]
            existing["status_icon"] = new["status_icon"]
            existing["delivery_label"] = new["delivery_label"]
            existing["delivery_date"] = new["delivery_date"]
            existing["email_date_obj"] = new["email_date_obj"]
            existing["is_today"] = new["is_today"]

        # Prefer a real (non-generic) product name whenever we find one
        if new["product_name"] and not new["product_name"].startswith("Order #"):
            if not existing["product_name"] or existing["product_name"].startswith("Order #"):
                existing["product_name"] = new["product_name"]

        # Sticky image: Out-for-delivery/Delivered emails carry no photo,
        # so once we've found one (from an Ordered/Shipped email) never
        # let a later image-less email erase it.
        if new.get("image_url") and not existing.get("image_url"):
            existing["image_url"] = new["image_url"]

        # Sticky "arriving today" signal - an Out-for-delivery email doesn't
        # repeat the "Arriving <date>" promise text, so don't let a later
        # merge erase an earlier-detected one.
        if new.get("arriving_today") and not existing.get("arriving_today"):
            existing["arriving_today"] = True

    # ------------------------------------------------------------------
    # Email parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_subject(raw_subject):
        parts = decode_header(raw_subject)
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                out.append(text.decode(enc or "utf-8", "ignore"))
            else:
                out.append(text)
        return "".join(out)

    def _parse_amazon_email(self, msg):
        try:
            raw_subject = msg.get("Subject", "") or ""
            raw_subject = self._decode_subject(raw_subject).strip()

            if not raw_subject:
                return None

            for pattern in EXCLUDE_SUBJECT_PATTERNS:
                if re.search(pattern, raw_subject, re.IGNORECASE):
                    return None

            # Strip bidi-isolate marks Amazon wraps around quantity digits,
            # e.g. 'Ordered: ⁦ 2 ⁩ "X..."' -> 'Ordered: 2 "X..."'
            subject = BIDI_MARKS_RE.sub("", raw_subject)

            status = self._detect_status_from_subject(subject)
            if not status:
                return None

            product_name = self._extract_product_name(subject)

            body = self._get_email_body(msg)
            order_num = ORDER_NUM_RE.search(subject)
            order_num = order_num.group(1) if order_num else None
            if not order_num and body:
                match = ORDER_NUM_RE.search(body)
                order_num = match.group(1) if match else None

            if not order_num:
                return None

            # Product photo: only present in Ordered/Shipped templates.
            html_body = self._get_email_html(msg)
            img_match = IMAGE_URL_RE.search(html_body) if html_body else None
            image_url = img_match.group(0) if img_match else None

            # "Arriving <date>" promise text - lets a Shipped order that's
            # actually due today show as due today instead of generic "In
            # Transit", without waiting on Amazon's Out-for-delivery email.
            arrive_source = html_body or body or ""
            arrive_match = ARRIVING_RE.search(arrive_source)
            arriving_today = bool(arrive_match) and arrive_match.group(1).lower() == "today"

            if not product_name:
                product_name = f"Order #{order_num}"
            else:
                more = MORE_ITEMS_RE.search(subject)
                if more:
                    product_name = f"{product_name} +{more.group(1)} more"

            date_hdr = msg.get("Date")
            try:
                email_dt = parsedate_to_datetime(date_hdr) if date_hdr else datetime.now()
                if email_dt.tzinfo:
                    email_dt = email_dt.astimezone().replace(tzinfo=None)
            except Exception:
                email_dt = datetime.now()
            is_today = email_dt.date() == datetime.now().date()

            delivery_label = self._delivery_label(status, email_dt, is_today)
            if status == "Shipped" and arriving_today:
                delivery_label = "Today"

            return {
                "order_number": order_num,
                "product_name": product_name[:80],
                "status": status,
                "status_icon": STATUS_ICON.get(status, "?"),
                "delivery_label": STATUS_DELIVERY_LABEL.get(status, "STATUS"),
                "delivery_date": delivery_label,
                "email_date_obj": email_dt.isoformat(),
                "is_today": is_today,
                "arriving_today": arriving_today,
                "email_subject": subject,
                "image_url": image_url,
            }

        except Exception as e:
            logger.debug(f"Email parse error: {e}")
            return None

    @staticmethod
    def _detect_status_from_subject(subject):
        for pattern, status in SUBJECT_STATUS_PATTERNS:
            if re.search(pattern, subject):
                return status
        return None

    @staticmethod
    def _extract_product_name(subject):
        """Pull quoted product name out of subject, clean ellipsis/quotes."""
        match = QUOTED_NAME_RE.search(subject)
        if not match:
            return None
        name = match.group(1).strip()
        name = name.rstrip(".…").strip()
        if not name or "http" in name.lower():
            return None
        return name

    @staticmethod
    def _delivery_label(status, email_dt, is_today):
        if status == "Delivered":
            return "Today" if is_today else email_dt.strftime("%d %b")
        if status in ("Out for Delivery", "Delivery Attempted"):
            return "Today" if is_today else email_dt.strftime("%d %b")
        if status == "Shipped":
            return "In Transit"
        if status == "Ordered":
            return "Processing"
        if status == "Delayed":
            return "Delayed"
        if status == "Cancelled":
            return "Cancelled"
        return ""

    @staticmethod
    def _get_email_body(msg):
        try:
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and not part.get_filename():
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            return payload.decode(charset, "ignore")
            else:
                if msg.get_content_type() == "text/plain":
                    payload = msg.get_payload(decode=True)
                    if payload:
                        charset = msg.get_content_charset() or "utf-8"
                        return payload.decode(charset, "ignore")
        except Exception as e:
            logger.debug(f"Body extraction error: {e}")
        return ""

    @staticmethod
    def _get_email_html(msg):
        try:
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html" and not part.get_filename():
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            return payload.decode(charset, "ignore")
            else:
                if msg.get_content_type() == "text/html":
                    payload = msg.get_payload(decode=True)
                    if payload:
                        charset = msg.get_content_charset() or "utf-8"
                        return payload.decode(charset, "ignore")
        except Exception as e:
            logger.debug(f"HTML extraction error: {e}")
        return ""


    # ------------------------------------------------------------------
    # Product image fetch + BWR conversion (for the small handful of
    # orders actually being displayed - not all 39, keeps this light
    # enough for a Pi Zero 2W).
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_product_image_b64(image_url):
        try:
            resp = requests.get(
                image_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if resp.status_code != 200:
                return None

            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            img.thumbnail((200, 200), Image.LANCZOS)

            out = io.BytesIO()
            img.save(out, format="PNG")
            return base64.b64encode(out.getvalue()).decode("ascii")
        except Exception as e:
            logger.debug(f"Image fetch failed for {image_url}: {e}")
            return None

    # ------------------------------------------------------------------
    # Cache serialization (plain dicts now, no date objects to convert)
    # ------------------------------------------------------------------

    def _save_cache(self, orders):
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump({"timestamp": time.time(), "orders": orders}, f)
            logger.info(f"Cached {len(orders)} orders")
        except Exception as e:
            logger.warning(f"Cache save error: {e}")
