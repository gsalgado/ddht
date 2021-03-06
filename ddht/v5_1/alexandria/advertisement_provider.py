import logging
from typing import Iterable, Sequence

from async_service import Service
from eth_utils.toolz import take
import trio

from ddht.base_message import InboundMessage
from ddht.v5_1.alexandria.abc import (
    AdvertisementDatabaseAPI,
    AdvertisementProviderAPI,
    AlexandriaClientAPI,
)
from ddht.v5_1.alexandria.advertisements import Advertisement
from ddht.v5_1.alexandria.messages import LocateMessage
from ddht.v5_1.alexandria.typing import ContentKey

MAX_RESPONSE_ADVERTISEMENTS = 32


class AdvertisementProvider(Service, AdvertisementProviderAPI):
    logger = logging.getLogger("ddht.AdvertisementProvider")

    def __init__(
        self,
        client: AlexandriaClientAPI,
        advertisement_dbs: Sequence[AdvertisementDatabaseAPI],
        concurrency: int = 3,
    ) -> None:
        self._client = client
        self._advertisement_dbs = tuple(advertisement_dbs)
        self._concurrency_lock = trio.Semaphore(concurrency)
        self._ready = trio.Event()

    async def ready(self) -> None:
        await self._ready.wait()

    async def run(self) -> None:
        async with trio.open_nursery() as nursery:
            async with self._client.subscribe(LocateMessage) as subscription:
                self._ready.set()
                async for request in subscription:
                    nursery.start_soon(self.serve_request, request)

    def _source_advertisements(
        self, content_key: ContentKey
    ) -> Iterable[Advertisement]:
        for advertisement_db in self._advertisement_dbs:
            yield from advertisement_db.query(content_key=content_key)

    async def serve_request(self, request: InboundMessage[LocateMessage]) -> None:
        with trio.move_on_after(10):
            async with self._concurrency_lock:
                content_key = request.message.payload.content_key

                advertisements = tuple(
                    take(
                        MAX_RESPONSE_ADVERTISEMENTS,
                        self._source_advertisements(content_key),
                    )
                )
                self.logger.debug(
                    "Serving request: id=%s  content_key=%s  num=%d",
                    request.request_id.hex(),
                    content_key.hex(),
                    len(advertisements),
                )
                await self._client.send_locations(
                    request.sender_node_id,
                    request.sender_endpoint,
                    advertisements=advertisements,
                    request_id=request.request_id,
                )
