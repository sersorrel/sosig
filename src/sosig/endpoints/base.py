from __future__ import annotations

import asyncio
import logging
from typing import Generic, Mapping, NamedTuple, Optional, TypeVar


Location = TypeVar("Location")  # some unique id for a channel/server/network/whatever


class Message(NamedTuple):
    text: str
    username: Optional[str]
    avatar_url: Optional[str] = None
    thread_id: Optional[str] = None


class Endpoint(Generic[Location]):
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
