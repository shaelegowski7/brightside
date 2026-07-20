"""Source adapter seam. Phase 1 implements HotUKDealsAdapter only. Phase 2
adds retailer clearance scrapers (Argos etc.) implementing the same
interface, so scheduler.py's poll loop doesn't need to change."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RawDeal:
    source: str
    retailer: str | None
    title: str
    url: str             # source's own dedupe key (e.g. HUKD thread URL)
    buy_price_pence: int
    image_url: str | None = None
    # Retailer scrapers (Phase 2) already have the final retailer URL + page
    # body from their own fetch, unlike HUKD deals which need resolver.py to
    # follow HUKD's redirect wrapper first. When set, pipeline.process_deal
    # treats `url` as already-resolved and skips the resolver call entirely.
    html: str | None = None


class SourceAdapter(ABC):
    @abstractmethod
    def poll(self) -> list[RawDeal]:
        ...
