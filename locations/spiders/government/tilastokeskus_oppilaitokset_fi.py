from typing import AsyncIterator, Iterable

from scrapy import Spider
from scrapy.http import JsonRequest, TextResponse

from locations.categories import Categories, apply_category
from locations.items import Feature
from locations.licenses import Licenses

# https://avoindata.suomi.fi/data/fi/dataset/oppilaitokset
# Tilastokeskus school register extract: peruskoulut, lukiot and
# yhtenäiskoulut with name and point geometry. OLO=0 rows are active;
# other values are closed, merged or inactive units, not locations.


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
        yield JsonRequest(url=self.wfs_url(0), cb_kwargs={"start_index": 0})

    def parse(self, response: TextResponse, start_index: int) -> Iterable[Feature | JsonRequest]:
        payload = response.json()  # ty: ignore[unresolved-attribute]
        features = payload.get("features") or []

        for feature in features:
            if item := self.parse_feature(feature):
                yield item

        # Step by received count and stop on an empty page, so a
        # server-side page cap can never silently truncate the tail.
        if features:
            next_index = start_index + len(features)
            yield JsonRequest(url=self.wfs_url(next_index), cb_kwargs={"start_index": next_index})

    def parse_feature(self, feature: dict) -> Feature | None:
        props = feature.get("properties") or {}

        if str(props.get("olo")) != "0":
            self._count("skipped_status/" + str(props.get("olo")))
            return None

        item = Feature()

        if not (tunn := props.get("tunn")):
            self._count("skipped_no_ref")
            return None
        item["ref"] = str(tunn)

        if name := (props.get("onimi") or "").strip():
            item["name"] = name
        else:
            self._count("skipped_no_name")
            return None

        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if geometry.get("type") != "Point" or len(coords) != 2:
            self._count("skipped_no_coords")
            return None
        item["lon"], item["lat"] = coords[0], coords[1]

        if str(props.get("oltyp")) == "12":
            item["extras"]["school"] = "special_education_needs"

        apply_category(Categories.SCHOOL, item)

        return item

    def _count(self, key: str) -> None:
        crawler = getattr(self, "crawler", None)
        stats = crawler.stats if crawler else None
        if stats is None:
            return
        stats.inc_value(f"{self.name}/{key}")
