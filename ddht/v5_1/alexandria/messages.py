import enum
from typing import Any, Dict, Generic, Tuple, Type, TypeVar

from eth_typing import Hash32
import ssz
from ssz import BaseSedes
from ssz.exceptions import DeserializationError

from ddht.constants import UINT8_TO_BYTES
from ddht.exceptions import DecodingError
from ddht.v5_1.alexandria.payloads import (
    AckPayload,
    Advertisement,
    AdvertisePayload,
    ContentPayload,
    FindNodesPayload,
    FoundNodesPayload,
    GetContentPayload,
    LocatePayload,
    LocationsPayload,
    PingPayload,
    PongPayload,
)
from ddht.v5_1.alexandria.sedes import (
    AckSedes,
    AdvertiseSedes,
    ContentSedes,
    FindNodesSedes,
    FoundNodesSedes,
    GetContentSedes,
    LocateSedes,
    LocationsSedes,
    PingSedes,
    PongSedes,
)

TPayload = TypeVar("TPayload")


TAlexandriaMessage = TypeVar("TAlexandriaMessage", bound="AlexandriaMessage[Any]")


class AlexandriaMessageType(enum.Enum):
    REQUEST = 1
    RESPONSE = 2


class AlexandriaMessage(Generic[TPayload]):
    message_id: int
    type: AlexandriaMessageType
    sedes: BaseSedes
    payload_type: Type[TPayload]

    payload: TPayload

    def __init__(self, payload: TPayload) -> None:
        self.payload = payload

    def __eq__(self, other: Any) -> bool:
        if type(self) is not type(other):
            return False
        return self.payload == other.payload  # type: ignore

    def to_wire_bytes(self) -> bytes:
        return b"".join(
            (
                UINT8_TO_BYTES[self.message_id],
                ssz.encode(self.get_payload_for_encoding(), sedes=self.sedes),
            )
        )

    def get_payload_for_encoding(self) -> Any:
        return self.payload

    @classmethod
    def from_payload_args(
        cls: Type[TAlexandriaMessage], payload_args: Any
    ) -> TAlexandriaMessage:
        payload = cls.payload_type(*payload_args)
        return cls(payload)


MESSAGE_REGISTRY: Dict[int, Type[AlexandriaMessage[Any]]] = {}


def register(message_class: Type[TAlexandriaMessage]) -> Type[TAlexandriaMessage]:
    message_id = message_class.message_id

    if message_id in MESSAGE_REGISTRY:
        raise ValueError(
            f"Message id already in registry: id={message_id} "
            f"class={MESSAGE_REGISTRY[message_id]}"
        )

    MESSAGE_REGISTRY[message_id] = message_class
    return message_class


@register
class PingMessage(AlexandriaMessage[PingPayload]):
    message_id = 1
    type = AlexandriaMessageType.REQUEST
    sedes = PingSedes
    payload_type = PingPayload

    payload: PingPayload


@register
class PongMessage(AlexandriaMessage[PongPayload]):
    message_id = 2
    type = AlexandriaMessageType.RESPONSE
    sedes = PongSedes
    payload_type = PongPayload

    payload: PongPayload


@register
class FindNodesMessage(AlexandriaMessage[FindNodesPayload]):
    message_id = 3
    type = AlexandriaMessageType.REQUEST
    sedes = FindNodesSedes
    payload_type = FindNodesPayload

    payload: FindNodesPayload

    @classmethod
    def from_payload_args(
        cls: Type[TAlexandriaMessage], payload_args: Any
    ) -> TAlexandriaMessage:
        # py-ssz uses an internal type for decoded `ssz.sedes.List` types that
        # we don't need or want so we force it to a normal tuple type here.
        distances = tuple(payload_args[0])
        payload = cls.payload_type(distances)
        return cls(payload)


@register
class FoundNodesMessage(AlexandriaMessage[FoundNodesPayload]):
    message_id = 4
    type = AlexandriaMessageType.RESPONSE
    sedes = FoundNodesSedes
    payload_type = FoundNodesPayload

    payload: FoundNodesPayload

    @classmethod
    def from_payload_args(
        cls: Type[TAlexandriaMessage], payload_args: Any
    ) -> TAlexandriaMessage:
        # py-ssz uses an internal type for decoded `ssz.sedes.List` types that
        # we don't need or want so we force it to a normal tuple type here.
        total, ssz_wrapped_enrs = payload_args
        enrs = tuple(ssz_wrapped_enrs)
        payload = cls.payload_type(total, enrs)
        return cls(payload)


@register
class GetContentMessage(AlexandriaMessage[GetContentPayload]):
    message_id = 5
    type = AlexandriaMessageType.REQUEST
    sedes = GetContentSedes
    payload_type = GetContentPayload

    payload: GetContentPayload


@register
class ContentMessage(AlexandriaMessage[ContentPayload]):
    message_id = 6
    type = AlexandriaMessageType.RESPONSE
    sedes = ContentSedes
    payload_type = ContentPayload

    payload: ContentPayload


@register
class AdvertiseMessage(AlexandriaMessage[AdvertisePayload]):
    message_id = 7
    type = AlexandriaMessageType.REQUEST
    sedes = AdvertiseSedes
    payload_type = tuple

    payload: AdvertisePayload

    def get_payload_for_encoding(
        self,
    ) -> Tuple[Tuple[bytes, Hash32, int, int, int, int], ...]:
        return tuple(advertisement.to_sedes_payload() for advertisement in self.payload)

    @classmethod
    def from_payload_args(
        cls: Type[TAlexandriaMessage], payload_args: Any
    ) -> TAlexandriaMessage:
        payload = tuple(
            Advertisement.from_sedes_payload(sedes_payload)
            for sedes_payload in payload_args
        )
        return cls(payload)


@register
class AckMessage(AlexandriaMessage[AckPayload]):
    message_id = 8
    type = AlexandriaMessageType.RESPONSE
    sedes = AckSedes
    payload_type = AckPayload

    payload: AckPayload

    @classmethod
    def from_payload_args(
        cls: Type[TAlexandriaMessage], payload_args: Any
    ) -> TAlexandriaMessage:
        # py-ssz uses an internal type for decoded `ssz.sedes.List` types that
        # we don't need or want so we force it to a normal tuple type here.
        advertisement_radius, raw_acked = payload_args
        payload = cls.payload_type(advertisement_radius, tuple(raw_acked))
        return cls(payload)


@register
class LocateMessage(AlexandriaMessage[LocatePayload]):
    message_id = 9
    type = AlexandriaMessageType.REQUEST
    sedes = LocateSedes
    payload_type = LocatePayload

    payload: LocatePayload


@register
class LocationsMessage(AlexandriaMessage[LocationsPayload]):
    message_id = 10
    type = AlexandriaMessageType.RESPONSE
    sedes = LocationsSedes
    payload_type = LocationsPayload

    payload: LocationsPayload

    def get_payload_for_encoding(
        self,
    ) -> Tuple[int, Tuple[Tuple[bytes, Hash32, int, int, int, int], ...]]:
        return (
            self.payload.total,
            tuple(
                advertisement.to_sedes_payload()
                for advertisement in self.payload.locations
            ),
        )

    @classmethod
    def from_payload_args(
        cls: Type[TAlexandriaMessage], payload_args: Any
    ) -> TAlexandriaMessage:
        total, ssz_advertisement_list = payload_args
        advertisements = tuple(
            Advertisement.from_sedes_payload(sedes_payload)
            for sedes_payload in ssz_advertisement_list
        )
        payload = cls.payload_type(total, advertisements)
        return cls(payload)


def decode_message(data: bytes) -> AlexandriaMessage[Any]:
    message_id = data[0]
    try:
        message_class = MESSAGE_REGISTRY[message_id]
    except KeyError:
        raise DecodingError(f"Unknown message type: id={message_id}")

    try:
        payload_args = ssz.decode(data[1:], sedes=message_class.sedes)
    except DeserializationError as err:
        raise DecodingError(str(err)) from err

    return message_class.from_payload_args(payload_args)
