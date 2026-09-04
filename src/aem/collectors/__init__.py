from .acl_live import AclLiveCollector
from .base import Collector
from .bullock_imax import BullockImaxCollector
from .ticketmaster import TicketmasterCollector

ALL_COLLECTORS: dict[str, type[Collector]] = {
    AclLiveCollector.id: AclLiveCollector,
    BullockImaxCollector.id: BullockImaxCollector,
    TicketmasterCollector.id: TicketmasterCollector,
}
