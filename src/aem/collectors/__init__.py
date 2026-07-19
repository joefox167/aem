from .acl_live import AclLiveCollector
from .base import Collector
from .bullock_imax import BullockImaxCollector

ALL_COLLECTORS: dict[str, type[Collector]] = {
    AclLiveCollector.id: AclLiveCollector,
    BullockImaxCollector.id: BullockImaxCollector,
}
