import re
from typing import AsyncIterator, Iterable

from scrapy import Spider
from scrapy.exceptions import CloseSpider
from scrapy.http import JsonRequest, Response

from locations.categories import Categories, Extras, apply_category, apply_yes_no
from locations.hours import DAYS_FI, OpeningHours
from locations.items import Feature, SocialMedia, set_social_media

GROUP_SHOP = "63c5520f34d341001995f3ca"
GROUP_REPAIR = "63c5522b34d341001995f3cb"
FIELD_HOURS = "63c55e80a7ee610012eb0dcf"
FIELD_PHONE_CONT = "63c6512134d341001995f408"
FIELD_WEBSITE = "63c55e3f2a67ee00333a9cdd"
FIELD_FACEBOOK = "63c55607a7ee610012eb0dc7"
FIELD_INSTAGRAM = "63c555862466640020289c98"
FIELD_WHATSAPP = "63c555e82a67ee00333a9cd4"
FIELD_ACTIVE = "6537747eb3b2be0011d192df"
SERVICE_EXTRAS = {
    "63c64e182466640020289cb2": Extras.VEHICLE_INSPECTION_SERVICES,  # Katsastuspalvelu.
    "63c64d142466640020289cb0": Extras.VEHICLE_AIR_CONDITIONING_SERVICES,  # Ilmastointi.
    "63c650962466640020289cb8": Extras.VEHICLE_TYRE_SERVICES,  # Vaihto/Tasapainotus.
    "63c64f5b34d341001995f3fe": Extras.VEHICLE_EXHAUST_SERVICES,  # Pakokaasutesti.
    "63c64f3fa7ee610012eb0def": Extras.VEHICLE_EXHAUST_SERVICES,  # Pakokaasutesti diesel.
    "63c64f1c2a67ee00333a9d00": Extras.VEHICLE_WHEEL_ALIGNMENT_SERVICES,  # Ohjauskulmat.
    "63c64e802466640020289cb4": Extras.VEHICLE_WINDSCREEN_REPLACEMENT_SERVICES,  # Lasityot.
    "63c64ea134d341001995f3fc": Extras.VEHICLE_PAINTING_SERVICES,  # Maalaamo.
    "63c650b12466640020289cba": Extras.VEHICLE_BODY_REPAIR_SERVICES,  # Vauriokorjaamo.
    "63c64a8ba7ee610012eb0de5": Extras.VEHICLE_TYRE_SERVICES,  # Myymala renkaat.
    "63c64d59a7ee610012eb0ded": Extras.VEHICLE_BRAKE_SERVICES,  # Jarrutesti.
    "63c64d38a7ee610012eb0deb": Extras.VEHICLE_SUSPENSION_SERVICES,  # Iskaritestaus.
    "63c64df62a67ee00333a9cf6": Extras.VEHICLE_CLUTCH_SERVICES,  # Kaksoiskytkin.
    "63c64fba2a67ee00333a9d02": Extras.VEHICLE_PLANNED_MAINTENANCE_SERVICES,  # Pikahuolto.
}
HOURS_CLOSED = ["suljettu", "closed", "off"]
DROP_SEGMENT = re.compile(r"\baukioloajan\b|\bpuh\.", re.IGNORECASE)
DEPT_CUT = re.compile(r"\b(tukkumyynti|kahvio)\b\s*:.*", re.IGNORECASE)
OCLOCK = re.compile(r"(?i)\bklo\b")
DOT_TYPO = re.compile(r"(\d{1,2}[.:]\d{2})\.(\d{1,2}[.:]\d{2})")  # Upstream joins times with a dot (08.00.17.00).
DASH_MINUTES = re.compile(r"(\d{1,2}[.:]\d{2}\s*-\s*\d{1,2})-(\d{2})(?!\d)")  # Upstream dashes minutes (08.00-16-30).
BY_AGREEMENT = re.compile(r"(?i)sopimuksen mukaan")
SHOP_LABEL = re.compile(r"(?i)^myymälä:\s*")
LUNCH_BREAK = re.compile(r"\((\d{1,2}[.:]\d{2})\s*-\s*(\d{1,2}[.:]\d{2})[^)]*?\bsuljettu\b[^)]*\)", re.IGNORECASE)
HEAD_RANGE = re.compile(r"^\s*([A-Za-z]{2}\s*-\s*[A-Za-z]{2})\s+(\d{1,2}[.:]\d{2})\s*-\s*(\d{1,2}[.:]\d{2})")
DAY_TOKEN = re.compile(r"(?i)(?<!\w)(ma|ti|ke|to|pe|la|su)(?!\w)")


class FixusFISpider(Spider):
    name = "fixus_fi"
    # Q127515000 verified (Fixus, P856 fixus.fi); no NSI entry yet.
    item_attributes = {"brand": "Fixus", "brand_wikidata": "Q127515000"}
    allowed_domains = ["locator.maplet.com"]

    async def start(self) -> AsyncIterator[JsonRequest]:
        yield JsonRequest(
            "https://locator.maplet.com/api/public/v3/fixusmap/places?type=place&layer=default",
            dont_filter=True,
        )

    def parse(self, response: Response) -> Iterable[Feature]:
        places = response.json()  # ty: ignore[unresolved-attribute]
        if not places:
            self._stat("empty/feed")
            raise CloseSpider("no records")
        for place in places:
            if item := self.parse_place(place):
                yield item

    def parse_place(self, place: dict) -> Feature | None:
        ref = place.get("_id")
        if not ref:
            self.logger.warning("Skipping record without ref")
            self._stat("empty/ref")
            return None
        group = (place.get("group") or {}).get("$id")
        # Shop+repair pairs share an address under distinct refs; both are emitted.
        if group == GROUP_SHOP:
            category = Categories.SHOP_CAR_PARTS
        elif group == GROUP_REPAIR:
            category = Categories.SHOP_CAR_REPAIR
        else:
            self.logger.warning("Skipping %s: unmapped group %s", ref, group)
            self._stat(f"unmapped/group/{group}")
            return None
        name = (place.get("name") or {}).get("fi", "").strip()
        street = (place.get("address") or {}).get("fi", "").strip()
        city = (place.get("city") or {}).get("fi", "").strip()
        if not name or not street or not city:
            self.logger.warning("Skipping %s: missing name/address/city", ref)
            self._stat("empty/core")
            return None
        coords = (place.get("location") or {}).get("coordinates") or []
        if len(coords) != 2:
            self.logger.warning("Skipping %s: missing coords", ref)
            self._stat("empty/coords")
            return None
        if (place.get("data") or {}).get(FIELD_ACTIVE) is False:
            self.logger.warning("Skipping %s: inactive", ref)
            self._stat("empty/inactive")
            return None
        item = Feature()
        item["ref"] = ref
        item["branch"] = name
        item["street_address"] = street
        item["city"] = city
        if postcode := (place.get("postcode") or "").strip():
            item["postcode"] = postcode
        item["lon"], item["lat"] = coords[0], coords[1]  # GeoJSON order; 2 SE branches keep their own coords.
        if phone := self.parse_phone(place):
            item["phone"] = phone
        else:
            self._stat("empty/phone")
        if email := (place.get("email") or "").strip():
            item["email"] = email
        else:
            self._stat("empty/email")
        if website := self.parse_website(place):
            item["website"] = website
        else:
            self._stat("empty/website")
        if hours := self.parse_hours((place.get("data") or {}).get(FIELD_HOURS)):
            item["opening_hours"] = hours
        self.apply_socials(item, place.get("data") or {})
        self.apply_services(item, place.get("data") or {})
        apply_category(category, item)
        return item

    def parse_phone(self, place: dict) -> str | None:
        phone = (place.get("phone") or "").strip()
        cont = ((place.get("data") or {}).get(FIELD_PHONE_CONT) or "").strip().lstrip(", ")
        if cont:
            self._stat("phone/continued")
            phone = f"{phone}, {cont}" if phone else cont
        return phone or None

    def parse_website(self, place: dict) -> str | None:
        if site := (place.get("site") or "").strip():
            return site
        koti = (place.get("data") or {}).get(FIELD_WEBSITE) or ""
        koti = koti.strip() if isinstance(koti, str) else ""
        if koti.startswith(("http://", "https://")):  # Scheme-less upstream values are dropped.
            self._stat("website/fallback")
            return koti
        return None

    def apply_socials(self, item: Feature, data: dict) -> None:
        if facebook := self.parse_link(data.get(FIELD_FACEBOOK)):
            item["facebook"] = facebook
        if instagram := self.parse_link(data.get(FIELD_INSTAGRAM)):
            set_social_media(item, SocialMedia.INSTAGRAM, instagram)
        if whatsapp := self.parse_whatsapp(data.get(FIELD_WHATSAPP)):
            set_social_media(item, SocialMedia.WHATSAPP, whatsapp)

    def parse_link(self, value: object) -> str | None:
        url = value.strip() if isinstance(value, str) else ""
        return url if url.startswith(("http://", "https://")) else None

    def parse_whatsapp(self, value: object) -> str | None:
        text = value.strip() if isinstance(value, str) else ""
        if text.startswith("https://wa.me/"):
            digits = text.removeprefix("https://wa.me/")
            if digits.startswith("0"):  # Domestic trunk prefix is dropped after +358.
                return "+358" + digits.lstrip("0")
            return "+" + digits
        return text if text.startswith("+") else None

    def apply_services(self, item: Feature, data: dict) -> None:
        for field_id, extra in SERVICE_EXTRAS.items():
            if data.get(field_id) is True:
                apply_yes_no(extra, item, True)
                self._stat("service/applied")

    def parse_hours(self, raw: str | None) -> OpeningHours | None:
        text = (raw or "").strip()
        if not text:
            self._stat("hours/empty")
            return None
        if lunch := LUNCH_BREAK.search(text):
            if head := HEAD_RANGE.match(text):
                oh = OpeningHours()
                try:
                    oh.add_ranges_from_string(f"{head[1]} {head[2]}-{lunch[1]}", days=DAYS_FI)
                    oh.add_ranges_from_string(f"{head[1]} {lunch[2]}-{head[3]}", days=DAYS_FI)
                except ValueError:
                    self._stat("hours/bad_range")
                    return None
                self._stat("hours/lunch_split")
                return oh
            self._stat("hours/lunch_unparsed")
        text = OCLOCK.sub("", text)
        text = DOT_TYPO.sub(r"\1-\2", text)
        text = text.replace("-.", "-")  # Upstream range typo (08.00-.17.00).
        text = DASH_MINUTES.sub(r"\1:\2", text)
        if BY_AGREEMENT.search(text):
            text = BY_AGREEMENT.split(text)[0]
            self._stat("hours/dropped_segment")
        cleaned = DEPT_CUT.sub("", text)
        if cleaned != text:
            self._stat("hours/dropped_segment")
        segments = [SHOP_LABEL.sub("", s).strip() for s in cleaned.split(",")]
        kept = [s for s in segments if s and not DROP_SEGMENT.search(s)]
        if len(kept) != len([s for s in segments if s]):
            self._stat("hours/dropped_segment")
        if not kept or not DAY_TOKEN.search(" ".join(kept)):
            self._stat("hours/unparsed")
            return None
        oh = OpeningHours()
        try:
            oh.add_ranges_from_string(", ".join(kept), days=DAYS_FI, closed=HOURS_CLOSED)
        except ValueError:
            self._stat("hours/bad_range")
            return None
        return oh

    def _stat(self, key: str) -> None:
        stats = getattr(getattr(self, "crawler", None), "stats", None)
        if stats is not None:
            stats.inc_value(f"atp/fixus_fi/{key}")
