"""
Prajavani (ಪ್ರಜಾವಾಣಿ) e-paper source for the newspaper plugin.

The stock plugin pulls front pages from the Freedom Forum CDN, which only
carries US/international titles - no Indian-language papers. Prajavani
publishes its own e-paper through the Deccan Herald (TPML) API, so this
talks to that directly.

Endpoints (no auth needed for the free edition):
  /epaper/available-dates?month=&year=&publisher=PV&edition=
  /epaper/data?date=YYYYMMDD&edition=<edition_number>&publisher=PV

Two things that are easy to get wrong and cost a while to work out:
  - `date` must be YYYYMMDD. A dashed date returns 404 with a misleading
    "Object not found: s3://..." message rather than a validation error.
  - `edition` is the *edition_number* field, NOT the edition `id`. Passing
    the id silently 404s the same way.

Only the Bengaluru edition (edition_number 4) is readable without a
subscription - every other edition returns 403 "Guests and Freemium users
can only access Bengaluru edition".

The paywall shows up a second way, which is easy to miss: every page is
listed in the response, but the ones you are not entitled to have an empty
`imgFile` (only a useless 191x300 `imgThumbFile`). On a typical day that
leaves ~5 of ~36 pages actually offered, so the "random page" pool is a
handful rather than the whole paper. A non-empty `imgFile` is treated here
as *the* entitlement signal: pages without it are skipped and never
fetched by any route, rather than probed for another way in.

Rendering goes through the page PDF rather than the full-size image on
purpose. The images are ~4000x6300 webp, which decode to ~76MB of RGB and
reliably get this 512MB Pi OOM-killed (confirmed in dmesg). PyMuPDF
rasterises straight to the size we actually want, so peak RSS stays around
70MB and the download is ~400KB instead of ~1.8MB.
"""

import logging
import random
from datetime import datetime, timedelta

import fitz  # PyMuPDF - already a dependency of the ereader plugin
from PIL import Image

from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

API_BASE = "https://api-epaper-prod.deccanherald.com"
PUBLISHER = "PV"
FREE_EDITION_NUMBER = 4          # Bengaluru City - the only free one

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Origin": "https://epaper.prajavani.net",
    "Referer": "https://epaper.prajavani.net/",
}

# Pages the API labels as advertisements - a full-page ad is a poor thing to
# put on the frame, so they're skipped when picking a random page.
AD_SECTION_NAMES = {"ಜಾಹೀರಾತು", "ADVERTISEMENT", "ADVT"}

# Guard against a pathological page definition asking for a huge raster on a
# machine that cannot afford one.
MAX_RENDER_PIXELS = 4_000_000

# The page metadata cannot be trusted to identify advertisements: a full-page
# ad wrap is served under sectionName "PJ" / name "STATE" with a perfectly
# ordinary page number, indistinguishable from a news page until you look at
# the file itself. The PDF text layer separates them cleanly though - measured
# on a real edition:
#     page 2 (news)     17288 chars
#     page 3 (ad wrap)    476 chars
#     page 1 (ad front)   224 chars
# An ad is one big image with a few words of copy; a broadsheet page is
# thousands of characters. Anything below this is treated as an ad.
MIN_TEXT_CHARS = 2000

# Each rejected candidate costs another PDF download (~400KB), so cap the
# attempts rather than potentially walking the whole edition.
MAX_CANDIDATE_TRIES = 4


def _get_json(session, url):
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _text_chars(pdf_bytes):
    """Length of page 1's text layer - the ad/news discriminator."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            return len(doc[0].get_text())
        finally:
            doc.close()
    except Exception as e:
        logger.warning(f"Prajavani: could not read PDF text layer ({e})")
        return 0


def _render_pdf(pdf_bytes, target_h, top_fraction=None):
    """Rasterise page 1 of the PDF to roughly target_h pixels tall.

    top_fraction crops in PDF space *before* rendering, so the cropped area
    fills the frame at full detail instead of being scaled down and then
    thrown away.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[0]
        rect = page.rect
        clip = None
        source_h = rect.height

        if top_fraction:
            source_h = rect.height * top_fraction
            clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + source_h)

        zoom = target_h / source_h if source_h else 1.0
        if (rect.width * zoom) * (source_h * zoom) > MAX_RENDER_PIXELS:
            zoom = (MAX_RENDER_PIXELS / (rect.width * source_h)) ** 0.5
            logger.warning("Prajavani: clamped render zoom to stay within memory budget")

        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def fetch_page_image(target_height=470, top_fraction=None,
                     edition_number=FREE_EDITION_NUMBER, skip_first_page=True,
                     skip_ads=True, rng=None):
    """Return (PIL.Image, meta dict) for a random page of the current edition.

    Falls back through the previous few days - the edition is published
    overnight, so an early-morning refresh can legitimately find nothing for
    'today' yet.
    """
    rng = rng or random
    session = get_http_session()

    last_error = None
    for day_offset in range(0, 4):
        date = datetime.today() - timedelta(days=day_offset)
        date_str = date.strftime("%Y%m%d")
        url = f"{API_BASE}/epaper/data?date={date_str}&edition={edition_number}&publisher={PUBLISHER}"

        try:
            payload = _get_json(session, url)
        except Exception as e:
            last_error = e
            logger.info(f"Prajavani: no edition for {date_str} ({e})")
            continue

        base = payload.get("data_url_suffix")
        sections = (payload.get("data") or {}).get("sections") or []
        pages = [p for s in sections for p in (s.get("pages") or [])]
        if not base or not pages:
            last_error = RuntimeError("edition payload had no pages")
            continue

        front_page = pages[0]
        # Non-empty imgFile == this page is offered to us; also needs a pdfFile
        # since that is what we actually rasterise.
        candidates = [p for p in pages if p.get("imgFile") and p.get("pdfFile")]
        if not candidates:
            last_error = RuntimeError("every page for this date is behind the paywall")
            logger.info(f"Prajavani: {date_str} has no freely available pages")
            continue

        if skip_first_page:
            # Drops only pages[0], which is the advertising wrap sold over the
            # cover - NOT the paper's real front page. Those are different
            # objects here: the wrap is listed first (STATE, ~224 chars of
            # text) while the actual front page turns up later in the list
            # under a supplement section name. The real front page is left in,
            # because the free tier yields so few readable pages that
            # excluding it would leave exactly one and the frame would show
            # the same image until the next day's edition.
            candidates = [p for p in candidates if p is not front_page] or candidates
        if skip_ads:
            # Cheap metadata pre-filter. It catches the pages honest enough to
            # call themselves ads, but not the ones mislabelled as news - the
            # text-layer check below is what actually does the work.
            non_ads = [p for p in candidates
                       if (p.get("sectionName") or "").strip().upper() not in AD_SECTION_NAMES
                       and (p.get("name") or "").strip().upper() not in AD_SECTION_NAMES]
            if non_ads:
                candidates = non_ads

        # Try candidates in random order, keeping the first that actually looks
        # like a news page. Falls back to whichever had the most text, so a
        # thin-but-real page still beats failing outright.
        order = list(candidates)
        rng.shuffle(order)
        best = None
        for page in order[:MAX_CANDIDATE_TRIES]:
            resp = session.get(base + page["pdfFile"], headers=HEADERS, timeout=45)
            resp.raise_for_status()
            chars = _text_chars(resp.content) if skip_ads else MIN_TEXT_CHARS

            if best is None or chars > best[0]:
                best = (chars, page, resp.content)
            if chars >= MIN_TEXT_CHARS:
                break
            logger.info(f"Prajavani: page {page.get('displayName')} looks like an "
                        f"advertisement ({chars} chars of text), trying another")

        if best is None:
            last_error = RuntimeError("no candidate page could be downloaded")
            continue

        chars, page, pdf_bytes = best
        image = _render_pdf(pdf_bytes, target_height, top_fraction)

        meta = {
            "date": date.strftime("%d %b %Y"),
            "page_no": page.get("displayName") or page.get("absPageNo"),
            "section": page.get("sectionName") or page.get("name"),
            "total_pages": len(pages),
            "available_pages": len(candidates),
            "text_chars": chars,
            "looks_like_ad": chars < MIN_TEXT_CHARS,
        }
        logger.info(f"Prajavani: {meta['date']} page {meta['page_no']} "
                    f"({meta['section']}, {chars} chars) - chosen from "
                    f"{meta['available_pages']} free pages of {meta['total_pages']}")
        return image, meta

    raise RuntimeError(f"Prajavani e-paper unavailable for the last 4 days: {last_error}")
