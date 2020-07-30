import logging

from async_service import Service, run_trio_service
from eth.db.backends.level import LevelDB
from eth_keys import keys
from eth_utils import encode_hex
from eth_utils.toolz import merge
import trio

from ddht._utils import generate_node_key_file, read_node_key_file
from ddht.abc import NodeDBAPI
from ddht.boot_info import BootInfo
from ddht.constants import NUM_ROUTING_TABLE_BUCKETS
from ddht.enr import ENR, UnsignedENR
from ddht.exceptions import OldSequenceNumber
from ddht.identity_schemes import default_identity_scheme_registry
from ddht.kademlia import KademliaRoutingTable
from ddht.node_db import NodeDB
from ddht.v5.channel_services import (
    DatagramReceiver,
    DatagramSender,
    IncomingDatagram,
    IncomingMessage,
    IncomingPacket,
    OutgoingDatagram,
    OutgoingMessage,
    OutgoingPacket,
    PacketDecoder,
    PacketEncoder,
)
from ddht.v5.endpoint_tracker import EndpointTracker, EndpointVote
from ddht.v5.message_dispatcher import MessageDispatcher
from ddht.v5.messages import default_message_type_registry
from ddht.v5.packer import Packer
from ddht.v5.routing_table_manager import RoutingTableManager

logger = logging.getLogger("ddht.DDHT")


ENR_DATABASE_DIR_NAME = "enr-db"


def get_local_private_key(boot_info: BootInfo) -> keys.PrivateKey:
    if boot_info.private_key is None:
        # load from disk or generate
        node_key_file_path = boot_info.base_dir / "nodekey"
        if not node_key_file_path.exists():
            generate_node_key_file(node_key_file_path)
        return read_node_key_file(node_key_file_path)
    else:
        return boot_info.private_key


def get_local_enr(
    boot_info: BootInfo, node_db: NodeDBAPI, local_private_key: keys.PrivateKey
) -> ENR:
    minimal_enr = UnsignedENR(
        sequence_number=1,
        kv_pairs={
            b"id": b"v4",
            b"secp256k1": local_private_key.public_key.to_compressed_bytes(),
            b"udp": boot_info.port,
        },
        identity_scheme_registry=default_identity_scheme_registry,
    ).to_signed_enr(local_private_key.to_bytes())
    node_id = minimal_enr.node_id

    try:
        base_enr = node_db.get_enr(node_id)
    except KeyError:
        logger.info(f"No Node for {encode_hex(node_id)} found, creating new one")
        return minimal_enr
    else:
        if any(
            key not in base_enr or base_enr[key] != value
            for key, value in minimal_enr.items()
        ):
            logger.debug("Updating local ENR")
            return UnsignedENR(
                sequence_number=base_enr.sequence_number + 1,
                kv_pairs=merge(dict(base_enr), dict(minimal_enr)),
                identity_scheme_registry=default_identity_scheme_registry,
            ).to_signed_enr(local_private_key.to_bytes())
        else:
            return base_enr


class Application(Service):
    logger = logger
    _boot_info: BootInfo

    def __init__(self, boot_info: BootInfo) -> None:
        self._boot_info = boot_info

    async def run(self) -> None:
        identity_scheme_registry = default_identity_scheme_registry
        message_type_registry = default_message_type_registry

        enr_database_dir = self._boot_info.base_dir / ENR_DATABASE_DIR_NAME
        enr_database_dir.mkdir(exist_ok=True)
        node_db = NodeDB(default_identity_scheme_registry, LevelDB(enr_database_dir))

        local_private_key = get_local_private_key(self._boot_info)
        local_enr = get_local_enr(self._boot_info, node_db, local_private_key)
        local_node_id = local_enr.node_id

        routing_table = KademliaRoutingTable(local_node_id, NUM_ROUTING_TABLE_BUCKETS)

        node_db.set_enr(local_enr)
        for enr in self._boot_info.bootnodes:
            try:
                node_db.set_enr(enr)
            except OldSequenceNumber:
                pass
            routing_table.update(enr.node_id)

        port = self._boot_info.port
        listen_on = self._boot_info.listen_on

        socket = trio.socket.socket(
            family=trio.socket.AF_INET, type=trio.socket.SOCK_DGRAM
        )
        outgoing_datagram_channels = trio.open_memory_channel[OutgoingDatagram](0)
        incoming_datagram_channels = trio.open_memory_channel[IncomingDatagram](0)
        outgoing_packet_channels = trio.open_memory_channel[OutgoingPacket](0)
        incoming_packet_channels = trio.open_memory_channel[IncomingPacket](0)
        outgoing_message_channels = trio.open_memory_channel[OutgoingMessage](0)
        incoming_message_channels = trio.open_memory_channel[IncomingMessage](0)
        endpoint_vote_channels = trio.open_memory_channel[EndpointVote](0)

        # types ignored due to https://github.com/ethereum/async-service/issues/5
        datagram_sender = DatagramSender(  # type: ignore
            outgoing_datagram_channels[1], socket
        )
        datagram_receiver = DatagramReceiver(  # type: ignore
            socket, incoming_datagram_channels[0]
        )

        packet_encoder = PacketEncoder(  # type: ignore
            outgoing_packet_channels[1], outgoing_datagram_channels[0]
        )
        packet_decoder = PacketDecoder(  # type: ignore
            incoming_datagram_channels[1], incoming_packet_channels[0]
        )

        packer = Packer(
            local_private_key=local_private_key.to_bytes(),
            local_node_id=local_node_id,
            node_db=node_db,
            message_type_registry=message_type_registry,
            incoming_packet_receive_channel=incoming_packet_channels[1],
            incoming_message_send_channel=incoming_message_channels[0],
            outgoing_message_receive_channel=outgoing_message_channels[1],
            outgoing_packet_send_channel=outgoing_packet_channels[0],
        )

        message_dispatcher = MessageDispatcher(
            node_db=node_db,
            incoming_message_receive_channel=incoming_message_channels[1],
            outgoing_message_send_channel=outgoing_message_channels[0],
        )

        endpoint_tracker = EndpointTracker(
            local_private_key=local_private_key.to_bytes(),
            local_node_id=local_node_id,
            node_db=node_db,
            identity_scheme_registry=identity_scheme_registry,
            vote_receive_channel=endpoint_vote_channels[1],
        )

        routing_table_manager = RoutingTableManager(
            local_node_id=local_node_id,
            routing_table=routing_table,
            message_dispatcher=message_dispatcher,
            node_db=node_db,
            outgoing_message_send_channel=outgoing_message_channels[0],
            endpoint_vote_send_channel=endpoint_vote_channels[0],
        )

        logger.info(f"DDHT base dir: {self._boot_info.base_dir}")
        logger.info("Starting discovery service...")
        logger.info(f"Listening on {listen_on}:{port}")
        logger.info(f"Local Node ID: {encode_hex(local_enr.node_id)}")
        logger.info(f"Local ENR: {local_enr}")

        services = (
            datagram_sender,
            datagram_receiver,
            packet_encoder,
            packet_decoder,
            packer,
            message_dispatcher,
            endpoint_tracker,
            routing_table_manager,
        )
        await socket.bind((str(listen_on), port))
        with socket:
            async with trio.open_nursery() as nursery:
                for service in services:
                    nursery.start_soon(run_trio_service, service)