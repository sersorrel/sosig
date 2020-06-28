from __future__ import annotations

import abc
import asyncio
import logging
from typing import Any, cast, Generic, Hashable, Mapping, NamedTuple, Optional, TypeVar


Location = TypeVar("Location", bound=Hashable)  # some unique id for a channel/server


class Message(NamedTuple):
    text: str
    username: Optional[str]
    avatar_url: Optional[str] = None
    thread_id: Optional[str] = None


class Endpoint(Generic[Location], metaclass=abc.ABCMeta):
    # messages received from the platform
    received: Mapping[Location, asyncio.Queue[Message]]
    # messages to send to the platform
    sending: Mapping[Location, asyncio.Queue[Message]]

    def __init__(
        self,
        sending: Mapping[Location, asyncio.Queue[Message]],
        received: Mapping[Location, asyncio.Queue[Message]],
        config: Mapping[str, str],
    ):
        self.logger = logging.getLogger(__name__ + "." + type(self).__name__)
        self.logger.debug(
            "initialising endpoint %s, sending: %s, received: %s",
            type(self).__name__,
            sending.keys(),
            received.keys(),
        )
        self.sending = sending
        self.received = received
        self.config = config

    @staticmethod
    def parse_location(l: Any) -> Location:
        return cast(Location, l)

    @abc.abstractmethod
    async def run(self) -> None:
        pass
