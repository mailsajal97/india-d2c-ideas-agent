from abc import ABC, abstractmethod
from utils.signal_schema import RawSignal
from utils.logger import get_logger

logger = get_logger()


class BaseCollector(ABC):
    source_name: str = "unknown"

    @abstractmethod
    def fetch(self) -> list[RawSignal]:
        pass

    def fetch_safe(self) -> list[RawSignal]:
        try:
            results = self.fetch()
            logger.info(f"[{self.source_name}] collected {len(results)} items")
            return results
        except Exception as e:
            logger.warning(f"[{self.source_name}] failed: {e}")
            return []
