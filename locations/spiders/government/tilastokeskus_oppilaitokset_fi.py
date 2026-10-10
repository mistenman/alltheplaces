from typing import AsyncIterator, Iterable

from scrapy import Spider
from scrapy.http import JsonRequest, TextResponse

from locations.categories import Categories, apply_category
from locations.items import Feature
from locations.licenses import Licenses

# https://avoindata.suomi.fi/data/fi/dataset/oppilaitokset
# Only OLO=0 rows are active schools; other values are closed/merged/inactive units.


class TilastokeskusOppilaitoksetFISpider(Spider):
    name = "tilastokeskus_oppilaitokset_fi"
    allowed_domains = ["geo.stat.fi"]
    dataset_attributes = Licenses.CCBY4.value | {
        "attribution:name": "Contains data from the Oppilaitokset school register distributed by Statistics Finland (Tilastokeskus)",
        "attribution:website": "https://avoindata.suomi.fi/data/fi/dataset/oppilaitokset",
    }

    wfs_base_url = "https://geo.stat.fi/geoserver/oppilaitokset/wfs"
    page_size = 1000

    def wfs_url(self, start_index: int) -> str:
        return (
            f"{self.wfs_base_url}?service=WFS&version=2.0.0&request=GetFeature"
            f"&typeNames=oppilaitokset:oppilaitokset&srsName=EPSG:4326"
            f"&outputFormat=application/json&count={self.page_size}&startIndex={start_index}"
        )

    async def start(self) -> AsyncIterator[JsonRequest]:
        self._last_first_id = None
        yield JsonRequest(url=self.wfs_url(0), cb_kwargs={"start_index": 0})

    def parse(self, response: TextResponse, start_index: int) -> Iterable[Feature | JsonRequest]:
        payload = response.json()  # ty: ignore[unresolved-attribute]
        features = payload.get("features") or []

        if not features:
            self._count("empty_page")
            matched = payload.get("numberMatched")
            if isinstance(matched, int) and start_index < matched:
                self.logger.error("Empty WFS page at startIndex=%s of %s matched", start_index, matched)
            return

        # A server ignoring startIndex would loop forever; consecutive repeats stop it.
        first_id = features[0].get("id")
        if first_id is not None and first_id == self._last_first_id:
            self.logger.error("WFS page repeat at startIndex=%s, stopping", start_index)
            return
        self._last_first_id = first_id

        for feature in features:
            if item := self.parse_feature(feature):
                yield item

        # Step by received count so a server-side page cap cannot truncate the tail.
        next_index = start_index + len(features)
        yield JsonRequest(url=self.wfs_url(next_index), cb_kwargs={"start_index": next_index})

    def parse_feature(self, feature: dict) -> Feature | None:
        props = feature.get("properties") or {}

        if str(props.get("olo")).strip() != "0":
            self._count("skipped_status/" + str(props.get("olo")))
            return None

        item = Feature()

        tunn = props.get("tunn")
        if isinstance(tunn, str):
            tunn = tunn.strip()
        if not tunn:
            self._count("skipped_no_ref")
            return None
        item["ref"] = str(tunn)

        name = props.get("onimi")
        if not isinstance(name, str) or not name.strip():
            self._count("skipped_no_name")
            return None
        item["name"] = name.strip()

        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            self._count("skipped_bad_geometry")
            return None
        coords = geometry.get("coordinates") or []
        if len(coords) != 2:
            self._count("skipped_no_coords")
            return None
        try:
            item["lon"], item["lat"] = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            self._count("skipped_no_coords")
            return None

        self._count("oltyp/" + str(props.get("oltyp")).strip())
        if str(props.get("oltyp")).strip() == "12":  # Erityiskoulut are special-education schools.
            item["extras"]["school"] = "special_education_needs"

        apply_category(Categories.SCHOOL, item)

        return item

    def _count(self, key: str) -> None:
        crawler = getattr(self, "crawler", None)
        stats = crawler.stats if crawler else None
        if stats is None:
            return
        stats.inc_value(f"{self.name}/{key}")
