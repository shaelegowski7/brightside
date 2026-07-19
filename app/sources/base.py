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


class SourceAdapter(ABC):
    @abstractmethod
    def poll(self) -> list[RawDeal]:
        ...
