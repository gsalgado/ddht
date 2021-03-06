import itertools
from typing import (
    AsyncContextManager,
    AsyncIterator,
    Collection,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from async_generator import asynccontextmanager
from async_service import Service, background_trio_service
from eth_enr import ENRAPI, ENRManagerAPI, QueryableENRDatabaseAPI
from eth_enr.exceptions import OldSequenceNumber
from eth_typing import NodeID
from eth_utils import ValidationError, get_extended_debug_logger
from eth_utils.toolz import cons, first, take
from lru import LRU
import trio

from ddht._utils import adaptive_timeout, every, reduce_enrs, weighted_choice
from ddht.base_message import InboundMessage
from ddht.constants import ROUTING_TABLE_BUCKET_SIZE
from ddht.endpoint import Endpoint
from ddht.exceptions import (
    DuplicateProtocol,
    EmptyFindNodesResponse,
    MissingEndpointFields,
)
from ddht.kademlia import (
    KademliaRoutingTable,
    at_log_distance,
    compute_log_distance,
    iter_closest_nodes,
)
from ddht.token_bucket import TokenBucket
from ddht.v5_1.abc import (
    ClientAPI,
    DispatcherAPI,
    EventsAPI,
    NetworkAPI,
    NetworkProtocol,
    PoolAPI,
    TalkProtocolAPI,
)
from ddht.v5_1.constants import ROUTING_TABLE_KEEP_ALIVE
from ddht.v5_1.exceptions import ProtocolNotSupported
from ddht.v5_1.explorer import Explorer
from ddht.v5_1.messages import (
    FindNodeMessage,
    PingMessage,
    PongMessage,
    TalkRequestMessage,
)
from ddht.validation import validate_found_nodes_distances

UNRESPONSIVE_CACHE = LRU(2048)


@asynccontextmanager
async def common_recursive_find_nodes(
    network: NetworkProtocol,
    target: NodeID,
    *,
    concurrency: int = 3,
    unresponsive_cache: Dict[NodeID, float] = UNRESPONSIVE_CACHE,
) -> AsyncIterator[trio.abc.ReceiveChannel[ENRAPI]]:
    """
    An optimized version of the recursive lookup algorithm for a kademlia
    network.

    Continually lookup nodes in the target part of the network, keeping track
    of all of the nodes we have seen.

    Exit once we have queried all of the `k` closest nodes to the target.

    The concurrency structure here is optimized to minimize the effect of
    unresponsive nodes on the total time it takes to perform the recursive
    lookup.  Some requests will hang for up to 10 seconds.  The
    `adaptive_timeout` combined with the multiple concurrent workers helps
    mitigate the overall slowdown caused by a few unresponsive nodes since the
    other queries can be issues concurrently.
    """
    network.logger.debug2("Recursive find nodes: %s", target.hex())
    start_at = trio.current_time()

    # The set of NodeID values we have already queried.
    queried_node_ids: Set[NodeID] = set()

    # The set of NodeID that timed out
    #
    # The `local_node_id` is
    # included in this as a convenience mechanism so that we don't have to
    # continually fiter it out of the various filters
    unresponsive_node_ids: Set[NodeID] = {network.local_node_id}

    # We maintain a cache of nodes that were recently deemed unresponsive
    # within the last 10 minutes.
    unresponsive_node_ids.update(
        node_id
        for node_id, last_unresponsive_at in unresponsive_cache.items()
        if trio.current_time() - last_unresponsive_at < 300
    )

    # Accumulator of the node_ids we have seen
    received_node_ids: Set[NodeID] = set()

    # Tracker for node_ids that are actively being requested.
    in_flight: Set[NodeID] = set()

    condition = trio.Condition()

    def get_unqueried_node_ids() -> Tuple[NodeID, ...]:
        """
        Get the three nodes that are closest to the target such that the node
        is in the closest `k` nodes which haven't been deemed unresponsive.
        """
        # Construct an iterable of *all* the nodes we know about ordered by
        # closeness to the target.
        candidates = iter_closest_nodes(
            target, network.routing_table, received_node_ids
        )
        # Remove any unresponsive nodes from that iterable
        responsive_candidates = itertools.filterfalse(
            lambda node_id: node_id in unresponsive_node_ids, candidates
        )
        # Grab the closest K
        closest_k_candidates = take(
            network.routing_table.bucket_size, responsive_candidates,
        )
        # Filter out any from the closest K that we've already queried or that are in-flight
        closest_k_unqueried = itertools.filterfalse(
            lambda node_id: node_id in queried_node_ids or node_id in in_flight,
            closest_k_candidates,
        )

        return tuple(take(3, closest_k_unqueried))

    async def do_lookup(
        node_id: NodeID, send_channel: trio.abc.SendChannel[ENRAPI]
    ) -> None:
        """
        Perform an individual lookup on the target part of the network from the
        given `node_id`
        """
        if node_id == target:
            distance = 0
        else:
            distance = compute_log_distance(node_id, target)

        try:
            found_enrs = await network.find_nodes(node_id, distance)
        except (trio.TooSlowError, MissingEndpointFields, ValidationError):
            unresponsive_node_ids.add(node_id)
            unresponsive_cache[node_id] = trio.current_time()
            return
        except trio.Cancelled:
            # We don't add these to the unresponsive cache since they didn't
            # necessarily exceed the fulle 10s request/response timeout.
            unresponsive_node_ids.add(node_id)
            raise

        for enr in found_enrs:
            try:
                network.enr_db.set_enr(enr)
            except OldSequenceNumber:
                pass

        async with condition:
            new_enrs = tuple(
                enr for enr in found_enrs if enr.node_id not in received_node_ids
            )
            received_node_ids.update(enr.node_id for enr in new_enrs)

        for enr in new_enrs:
            try:
                await send_channel.send(enr)
            except (trio.BrokenResourceError, trio.ClosedResourceError):
                # In the event that the consumer of `recursive_find_nodes`
                # exits early before the lookup has completed we can end up
                # operating on a closed channel.
                return

    async def worker(
        worker_id: NodeID, send_channel: trio.abc.SendChannel[ENRAPI]
    ) -> None:
        """
        Pulls unqueried nodes from the closest k nodes and performs a
        concurrent lookup on them.
        """
        for round in itertools.count():
            async with condition:
                node_ids = get_unqueried_node_ids()

                if not node_ids:
                    await condition.wait()
                    continue

                # Mark the node_ids as having been queried.
                queried_node_ids.update(node_ids)
                # Mark the node_ids as being in-flight.
                in_flight.update(node_ids)

                # Some of the node ids may have come from our routing table.
                # These won't be present in the `received_node_ids` so we
                # detect this here and send them over the channel.
                try:
                    for node_id in node_ids:
                        if node_id not in received_node_ids:
                            enr = network.enr_db.get_enr(node_id)
                            received_node_ids.add(node_id)
                            await send_channel.send(enr)
                except (trio.BrokenResourceError, trio.ClosedResourceError):
                    # In the event that the consumer of `recursive_find_nodes`
                    # exits early before the lookup has completed we can end up
                    # operating on a closed channel.
                    return

            if len(node_ids) == 1:
                await do_lookup(node_ids[0], send_channel)
            else:
                tasks = tuple(
                    (do_lookup, (node_id, send_channel)) for node_id in node_ids
                )
                try:
                    await adaptive_timeout(*tasks, threshold=1, variance=2.0)
                except trio.TooSlowError:
                    pass

            async with condition:
                # Remove the `node_ids` from the in_flight set.
                in_flight.difference_update(node_ids)

                condition.notify_all()

    async def _monitor_done(send_channel: trio.abc.SendChannel[ENRAPI]) -> None:
        async with send_channel:
            async with condition:
                while True:
                    # this `fail_after` is a failsafe to prevent deadlock situations
                    # which are possible with `Condition` objects.
                    with trio.move_on_after(60) as scope:
                        node_ids = get_unqueried_node_ids()

                        if not node_ids and not in_flight:
                            break
                        else:
                            await condition.wait()

                    if scope.cancelled_caught:
                        network.logger.error("Deadlock")

    send_channel, receive_channel = trio.open_memory_channel[ENRAPI](256)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(_monitor_done, send_channel)

        for worker_id in range(concurrency):
            nursery.start_soon(worker, worker_id, send_channel)

        async with receive_channel:
            yield receive_channel

        nursery.cancel_scope.cancel()

    elapsed = trio.current_time() - start_at

    network.logger.debug(
        "Lookup for %s finished in %f seconds: seen=%d  queried=%d  unresponsive=%d",
        target.hex(),
        elapsed,
        len(received_node_ids),
        len(queried_node_ids),
        len(unresponsive_node_ids),
    )


@asynccontextmanager
async def common_network_stream_find_nodes(
    network: NetworkAPI,
    node_id: NodeID,
    endpoint: Endpoint,
    distances: Collection[int],
    *,
    request_id: Optional[bytes] = None,
) -> AsyncIterator[trio.abc.ReceiveChannel[ENRAPI]]:
    if not distances:
        raise TypeError("Must provide at least one distance")

    if endpoint is None:
        endpoint = await network.endpoint_for_node_id(node_id)

    async def _stream_find_nodes_response(
        send_channel: trio.abc.SendChannel[ENRAPI],
    ) -> None:
        async with network.client.stream_find_nodes(
            node_id, endpoint, distances=distances, request_id=request_id
        ) as resp_aiter:
            async with send_channel:
                async for response in resp_aiter:
                    enrs = response.message.enrs
                    for enr in enrs:
                        try:
                            await send_channel.send(enr)
                        except (trio.BrokenResourceError, trio.ClosedResourceError):
                            break

    send_channel, receive_channel = trio.open_memory_channel[ENRAPI](256)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(
            _stream_find_nodes_response, send_channel,
        )

        try:
            async with receive_channel:
                try:
                    yield receive_channel
                except trio.EndOfChannel as err:
                    raise trio.TooSlowError from err
        finally:
            nursery.cancel_scope.cancel()


class Network(Service, NetworkAPI):
    _bootnodes: Tuple[ENRAPI, ...]
    _talk_protocols: Dict[bytes, TalkProtocolAPI]

    def __init__(self, client: ClientAPI, bootnodes: Collection[ENRAPI],) -> None:
        self.logger = get_extended_debug_logger("ddht.Network")

        self.client = client

        self._bootnodes = tuple(bootnodes)
        self.routing_table = KademliaRoutingTable(
            self.client.enr_manager.enr.node_id, ROUTING_TABLE_BUCKET_SIZE,
        )
        self._routing_table_ready = trio.Event()
        self._last_pong_at = LRU(2048)

        self._talk_protocols = {}

        self._ping_handler_ready = trio.Event()
        self._find_nodes_handler_ready = trio.Event()

    async def ready(self) -> None:
        await self._ping_handler_ready.wait()
        await self._find_nodes_handler_ready.wait()

    #
    # Proxied ClientAPI properties
    #
    @property
    def local_node_id(self) -> NodeID:
        return self.client.local_node_id

    @property
    def events(self) -> EventsAPI:
        return self.client.events

    @property
    def dispatcher(self) -> DispatcherAPI:
        return self.client.dispatcher

    @property
    def enr_manager(self) -> ENRManagerAPI:
        return self.client.enr_manager

    @property
    def pool(self) -> PoolAPI:
        return self.client.pool

    @property
    def enr_db(self) -> QueryableENRDatabaseAPI:
        return self.client.enr_db

    #
    # TALK API
    #
    def add_talk_protocol(self, protocol: TalkProtocolAPI) -> None:
        if protocol.protocol_id in self._talk_protocols:
            raise DuplicateProtocol(
                f"A protocol is already registered for '{protocol.protocol_id!r}'"
            )
        self._talk_protocols[protocol.protocol_id] = protocol

    #
    # High Level API
    #
    async def bond(
        self, node_id: NodeID, *, endpoint: Optional[Endpoint] = None
    ) -> bool:
        self.logger.debug2(
            "Bonding with %s", node_id.hex(),
        )

        try:
            pong = await self.ping(node_id, endpoint=endpoint)
        except trio.TooSlowError:
            self.logger.debug("Bonding with %s timed out during ping", node_id.hex())
            return False
        except MissingEndpointFields:
            self.logger.debug(
                "Bonding with %s failed due to missing endpoint information",
                node_id.hex(),
            )
            return False

        try:
            enr = await self.lookup_enr(
                node_id, enr_seq=pong.enr_seq, endpoint=endpoint
            )
        except trio.TooSlowError:
            self.logger.debug(
                "Bonding with %s timed out during ENR retrieval", node_id.hex(),
            )
            return False
        except EmptyFindNodesResponse:
            self.logger.debug(
                "Bonding with %s failed due to them not returing their ENR record",
                node_id.hex(),
            )
            return False

        self.routing_table.update(enr.node_id)

        self.logger.debug(
            "Bonded with %s successfully", node_id.hex(),
        )

        self._routing_table_ready.set()
        return True

    async def _bond(self, node_id: NodeID, endpoint: Optional[Endpoint] = None) -> None:
        await self.bond(node_id, endpoint=endpoint)

    async def ping(
        self,
        node_id: NodeID,
        *,
        endpoint: Optional[Endpoint] = None,
        request_id: Optional[bytes] = None,
    ) -> PongMessage:
        if endpoint is None:
            endpoint = await self.endpoint_for_node_id(node_id)
        response = await self.client.ping(node_id, endpoint, request_id=request_id)
        return response.message

    async def find_nodes(
        self,
        node_id: NodeID,
        *distances: int,
        endpoint: Optional[Endpoint] = None,
        request_id: Optional[bytes] = None,
    ) -> Tuple[ENRAPI, ...]:
        if not distances:
            raise TypeError("Must provide at least one distance")

        if endpoint is None:
            endpoint = await self.endpoint_for_node_id(node_id)
        responses = await self.client.find_nodes(
            node_id, endpoint, distances=distances, request_id=request_id
        )

        # Validate that all responses are indeed at one of the
        # specified distances.
        for response in responses:
            validate_found_nodes_distances(response.message.enrs, node_id, distances)

        return tuple(enr for response in responses for enr in response.message.enrs)

    def stream_find_nodes(
        self,
        node_id: NodeID,
        endpoint: Endpoint,
        distances: Collection[int],
        *,
        request_id: Optional[bytes] = None,
    ) -> AsyncContextManager[trio.abc.ReceiveChannel[ENRAPI]]:
        return common_network_stream_find_nodes(
            self, node_id, endpoint, distances, request_id=request_id
        )

    async def talk(
        self,
        node_id: NodeID,
        *,
        protocol: bytes,
        payload: bytes,
        endpoint: Optional[Endpoint] = None,
        request_id: Optional[bytes] = None,
    ) -> bytes:
        if endpoint is None:
            endpoint = await self.endpoint_for_node_id(node_id)
        response = await self.client.talk(
            node_id, endpoint, protocol, payload, request_id=request_id
        )
        payload = response.message.payload
        if not payload:
            raise ProtocolNotSupported(protocol)
        return response.message.payload

    async def lookup_enr(
        self, node_id: NodeID, *, enr_seq: int = 0, endpoint: Optional[Endpoint] = None
    ) -> ENRAPI:
        if node_id == self.local_node_id:
            raise Exception(f"Cannot lookup local ENR: node_id={node_id.hex()}")

        try:
            enr = self.enr_db.get_enr(node_id)
        except KeyError:
            if endpoint is None:
                # Try to use a recursive network lookup to find the desired
                # node.
                async with self.recursive_find_nodes(node_id) as enr_aiter:
                    async for found_enr in enr_aiter:
                        if found_enr.node_id == node_id:
                            endpoint = Endpoint.from_enr(found_enr)
                            break
                    else:
                        # we weren't given an endpoint and we don't have an enr which would give
                        # us an endpoint, there's no way to reach this node.
                        raise KeyError(f"Could not find ENR: node_id={node_id.hex()}")
        else:
            if enr.sequence_number >= enr_seq:
                return enr

        enr = await self._fetch_enr(node_id, endpoint=endpoint)
        try:
            self.enr_db.set_enr(enr)
        except OldSequenceNumber:
            pass

        return enr

    async def _fetch_enr(
        self, node_id: NodeID, *, endpoint: Optional[Endpoint]
    ) -> ENRAPI:
        enrs = await self.find_nodes(node_id, 0, endpoint=endpoint)
        if not enrs:
            raise EmptyFindNodesResponse(f"{node_id.hex()} did not return its ENR")

        # Assuming we're given enrs for a single node, this reduce returns the enr for
        # that node with the highest sequence number
        return reduce_enrs(enrs)[0]

    def recursive_find_nodes(
        self, target: NodeID
    ) -> AsyncContextManager[trio.abc.ReceiveChannel[ENRAPI]]:
        return common_recursive_find_nodes(self, target)

    @asynccontextmanager
    async def explore(
        self, target: NodeID, concurrency: int = 3,
    ) -> AsyncIterator[trio.abc.ReceiveChannel[ENRAPI]]:
        explorer = Explorer(self, target, concurrency)
        with trio.move_on_after(300) as scope:
            async with background_trio_service(explorer):
                await explorer.ready()

                async with explorer.stream() as receive_channel:
                    yield receive_channel

        if scope.cancelled_caught:
            self.logger.error("Timeout from `stream_locate`")

    #
    # Long Running Processes
    #
    async def run(self) -> None:
        self.manager.run_daemon_child_service(self.client)
        await self.client.wait_listening()

        self.manager.run_daemon_task(self._periodically_report_routing_table)
        self.manager.run_daemon_task(self._ping_oldest_routing_table_entry)
        self.manager.run_daemon_task(self._track_last_pong)
        self.manager.run_daemon_task(self._manage_routing_table)
        self.manager.run_daemon_task(self._pong_when_pinged)
        self.manager.run_daemon_task(self._serve_find_nodes)
        self.manager.run_daemon_task(self._handle_unhandled_talk_requests)

        await self.manager.wait_finished()

    async def _periodically_report_routing_table(self) -> None:
        async for _ in every(30, initial_delay=10):
            non_empty_buckets = tuple(
                reversed(
                    tuple(
                        (idx, bucket)
                        for idx, bucket in enumerate(self.routing_table.buckets, 1)
                        if bucket
                    )
                )
            )
            total_size = sum(len(bucket) for idx, bucket in non_empty_buckets)
            bucket_info = "|".join(
                tuple(
                    f"{idx}:{'F' if len(bucket) == self.routing_table.bucket_size else len(bucket)}"
                    for idx, bucket in non_empty_buckets
                )
            )
            self.logger.debug(
                "routing-table-info: size=%d  buckets=%s", total_size, bucket_info,
            )

    async def _ping_oldest_routing_table_entry(self) -> None:
        await self._routing_table_ready.wait()

        while self.manager.is_running:
            # Here we preserve the lazy iteration while still checking that the
            # iterable is not empty before passing it into `min` below which
            # throws an ambiguous `ValueError` otherwise if the iterable is
            # empty.
            nodes_iter = self.routing_table.iter_all_random()
            try:
                first_node_id = first(nodes_iter)
            except StopIteration:
                await trio.sleep(ROUTING_TABLE_KEEP_ALIVE)
                continue
            else:
                least_recently_ponged_node_id = min(
                    cons(first_node_id, nodes_iter),
                    key=lambda node_id: self._last_pong_at.get(node_id, 0),
                )

            too_old_at = trio.current_time() - ROUTING_TABLE_KEEP_ALIVE
            try:
                last_pong_at = self._last_pong_at[least_recently_ponged_node_id]
            except KeyError:
                pass
            else:
                if last_pong_at > too_old_at:
                    await trio.sleep(last_pong_at - too_old_at)
                    continue

            did_bond = await self.bond(least_recently_ponged_node_id)
            if not did_bond:
                self.routing_table.remove(least_recently_ponged_node_id)

    async def _track_last_pong(self) -> None:
        async with self.dispatcher.subscribe(PongMessage) as subscription:
            async for message in subscription:
                self._last_pong_at[message.sender_node_id] = trio.current_time()

    async def _manage_routing_table(self) -> None:
        # First load all the bootnode ENRs into our database
        for enr in self._bootnodes:
            try:
                self.enr_db.set_enr(enr)
            except OldSequenceNumber:
                pass

        # Now repeatedly try to bond with each bootnode until one succeeds.
        while self.manager.is_running:
            with trio.move_on_after(20):
                async with trio.open_nursery() as nursery:
                    for enr in self._bootnodes:
                        if enr.node_id == self.local_node_id:
                            continue
                        endpoint = Endpoint.from_enr(enr)
                        nursery.start_soon(self._bond, enr.node_id, endpoint)

                    await self._routing_table_ready.wait()
                    break

        # Now we enter into an infinite loop that continually probes the
        # network to beep the routing table fresh.  We both perform completely
        # random lookups, as well as targeted lookups on the outermost routing
        # table buckets which are not full.
        #
        # The `TokenBucket` allows us to burst at the beginning, making quick
        # successive probes, then slowing down once the
        #
        # TokenBucket starts with 10 tokens, refilling at 1 token every 30
        # seconds.
        token_bucket = TokenBucket(1 / 30, 10)

        async with trio.open_nursery() as nursery:
            while self.manager.is_running:
                await token_bucket.take()

                # Get the logarithmic distance to the "largest" buckets
                # that are not full.
                non_full_bucket_distances = tuple(
                    idx + 1
                    for idx, bucket in enumerate(self.routing_table.buckets)
                    if len(bucket) < self.routing_table.bucket_size  # noqa: E501
                )[-16:]

                # Probe one of the not-full-buckets with a weighted preference
                # towards the largest buckets.
                distance_to_probe = weighted_choice(non_full_bucket_distances)
                target_node_id = at_log_distance(self.local_node_id, distance_to_probe)

                async with self.recursive_find_nodes(target_node_id) as enr_aiter:
                    async for enr in enr_aiter:
                        if enr.node_id == self.local_node_id:
                            continue

                        try:
                            self.enr_db.set_enr(enr)
                        except OldSequenceNumber:
                            pass

                        nursery.start_soon(self._bond, enr.node_id)

    async def _pong_when_pinged(self) -> None:
        async def _maybe_add_to_routing_table(
            request: InboundMessage[PingMessage],
        ) -> None:
            try:
                enr = await self.lookup_enr(
                    request.sender_node_id,
                    enr_seq=request.message.enr_seq,
                    endpoint=request.sender_endpoint,
                )
            except (trio.TooSlowError, EmptyFindNodesResponse):
                return

            self.routing_table.update(enr.node_id)
            self._routing_table_ready.set()

        async with trio.open_nursery() as nursery:
            async with self.dispatcher.subscribe(PingMessage) as subscription:
                self._ping_handler_ready.set()

                async for request in subscription:
                    await self.dispatcher.send_message(
                        request.to_response(
                            PongMessage(
                                request.request_id,
                                self.enr_manager.enr.sequence_number,
                                request.sender_endpoint.ip_address,
                                request.sender_endpoint.port,
                            )
                        )
                    )
                    nursery.start_soon(_maybe_add_to_routing_table, request)

    async def _serve_find_nodes(self) -> None:
        async with self.dispatcher.subscribe(FindNodeMessage) as subscription:
            self._find_nodes_handler_ready.set()

            async for request in subscription:
                response_enrs: List[ENRAPI] = []
                distances = set(request.message.distances)
                if len(distances) != len(request.message.distances):
                    self.logger.debug(
                        "Ignoring invalid FindNodeMessage from %s@%s: duplicate distances",
                        request.sender_node_id.hex(),
                        request.sender_endpoint,
                    )
                    continue
                elif not distances:
                    self.logger.debug(
                        "Ignoring invalid FindNodeMessage from %s@%s: empty distances",
                        request.sender_node_id.hex(),
                        request.sender_endpoint,
                    )
                    continue
                elif any(
                    distance > self.routing_table.num_buckets for distance in distances
                ):
                    self.logger.debug(
                        "Ignoring invalid FindNodeMessage from %s@%s: distances: %s",
                        request.sender_node_id.hex(),
                        request.sender_endpoint,
                        distances,
                    )
                    continue

                for distance in distances:
                    if distance == 0:
                        response_enrs.append(self.enr_manager.enr)
                    elif distance <= self.routing_table.num_buckets:
                        node_ids_at_distance = self.routing_table.get_nodes_at_log_distance(
                            distance,
                        )
                        for node_id in node_ids_at_distance:
                            response_enrs.append(self.enr_db.get_enr(node_id))
                    else:
                        raise Exception("Should be unreachable")

                await self.client.send_found_nodes(
                    request.sender_node_id,
                    request.sender_endpoint,
                    enrs=response_enrs,
                    request_id=request.request_id,
                )

    async def _handle_unhandled_talk_requests(self) -> None:
        async with self.dispatcher.subscribe(TalkRequestMessage) as subscription:
            async for request in subscription:
                if request.message.protocol not in self._talk_protocols:
                    await self.client.send_talk_response(
                        request.sender_node_id,
                        request.sender_endpoint,
                        payload=b"",
                        request_id=request.message.request_id,
                    )

    #
    # Utility
    #
    async def endpoint_for_node_id(self, node_id: NodeID) -> Endpoint:
        try:
            enr = self.enr_db.get_enr(node_id)
        except KeyError:
            enr = await self.lookup_enr(node_id)

        return Endpoint.from_enr(enr)
