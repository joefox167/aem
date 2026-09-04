from .acl_live import AclLiveCollector
from .base import Collector
from .bullock_imax import BullockImaxCollector
from .paramount import ParamountCollector
from .ticketmaster import TicketmasterCollector

ALL_COLLECTORS: dict[str, type[Collector]] = {
    AclLiveCollector.id: AclLiveCollector,
    BullockImaxCollector.id: BullockImaxCollector,
    ParamountCollector.id: ParamountCollector,
    TicketmasterCollector.id: TicketmasterCollector,
}
