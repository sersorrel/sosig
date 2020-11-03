from __future__ import annotations

import asyncio
import importlib.metadata
import itertools
import logging
import sys
from collections import defaultdict
from typing import Any, Dict, List, NoReturn, Set, Tuple, Type, TypeVar

import tomlkit

import sosig.endpoints
from sosig.endpoints.base import Endpoint, Message

__all__ = ["main"]


logging.basicConfig(format="%(name)40s [%(levelname)s] %(message)s", level=logging.INFO)
logging.getLogger(__name__).setLevel(logging.DEBUG)
logging.getLogger("aiorun").setLevel(logging.DEBUG)


T = TypeVar("T")
# Really Endpoint[L] should be Endpoint.L, but Python doesn't have
# support for associated types like that.
L = Any


class DevNullQueue(asyncio.Queue):
    """A subclass of Queue that is always empty.

    Attempting to put things into the queue will immediately succeed;
    coroutines that get an item from the queue will block forever.
    """

    def _init(self, maxsize: int) -> None:
        self._queue: List[None] = []  # necessary, internals look at len(self._queue)

    def _get(self) -> T:
        pass

    def _put(self, item: T) -> None:
        pass


async def broadcaster(
    source: asyncio.Queue[Message], *dests: asyncio.Queue[Message],
) -> NoReturn:
    logging.getLogger(__name__ + ".broadcaster").debug(
        "starting broadcaster from %r to %r", source, dests
    )
    while True:
        msg = await source.get()
        # TODO: remove this processing step (it should probably be the
        # responsibility of endpoints to not inadvertently ping people,
        # plus this will potentially subtly break codeblocks).
        text = msg.text
        text = text.replace("@everyone", "@\u200ceveryone")
        text = text.replace("@channel", "@\u200cchannel")
        text = text.replace("@here", "@\u200chere")
        msg = msg._replace(text=text)
        for dest in dests:
            await dest.put(msg)
        source.task_done()


async def main() -> None:
    assert sys.version_info >= (3, 8)

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} CONFIG_FILE")
        sys.exit(1)

    logger = logging.getLogger(__name__)
    logger.info("starting %s %s", __name__, importlib.metadata.version(__name__))

    config_file = sys.argv[1]
    with open(config_file) as f:
        config = tomlkit.parse(f.read())

    # A general note on design here: originally, I wanted all channels
    # (where a "channel" is an (endpoint, location) pair) that ended up
    # in the same room to receive into the same queue, which would mean
    # we could have a single broadcaster per room which would read from
    # that queue and broadcast into the send queues of all channels in
    # that room. However, I couldn't see a good way to avoid
    # rebroadcasting a message into the send queue of the channel it
    # arrived from if I did that (and I didn't want to make that the
    # responsibility of the endpoints, because that shouldn't be their
    # problem), so instead we have a single broadcaster per channel,
    # which broadcasts into every other channel in the same room.

    # Work out what endpoints we need to set up, and how they need to be linked.
    required_endpoints: Dict[Type[Endpoint[L]], Set[L]] = defaultdict(set)
    rooms: List[Dict[Type[Endpoint[L]], Set[L]]] = []
    for room in config.get("room", []):
        this_room = defaultdict(set)
        for endpoint_config in room["endpoints"]:
            endpoint_type: Type[Endpoint[L]] = getattr(
                sosig.endpoints, endpoint_config["name"], None
            )
            assert endpoint_type is not None, "custom endpoints not supported (yet)"
            location = endpoint_type.parse_location(endpoint_config["location"])
            required_endpoints[endpoint_type].add(location)
            this_room[endpoint_type].add(location)
        rooms.append(this_room)

    # Set up those endpoints.
    endpoints: Dict[Type[Endpoint[L]], Endpoint[L]] = {}
    for endpoint_type, locations in required_endpoints.items():
        endpoints[endpoint_type] = endpoint_type(
            sending={loc: asyncio.Queue() for loc in locations},
            received=defaultdict(
                DevNullQueue, {loc: asyncio.Queue() for loc in locations}
            ),
            config=config[endpoint_type.__name__],
        )

    # Join the relevant endpoints together.
    broadcasters = []
    for room in rooms:
        for endpoint_type, endpoint_locations in room.items():
            endpoint = endpoints[endpoint_type]
            for loc in endpoint_locations:
                broadcasters.append(
                    broadcaster(
                        endpoint.received[loc],
                        *[
                            endpoints[_endpoint_type].sending[_loc]
                            for _endpoint_type, _endpoint_locations in room.items()
                            for _loc in _endpoint_locations
                            if (_endpoint_type, _loc) != (endpoint_type, loc)
                        ],
                    )
                )

    # Wrap the endpoints and the broadcasters in an asyncio.Task.
    tasks = itertools.chain(
        (asyncio.create_task(endpoint.run()) for endpoint in endpoints.values()),
        map(asyncio.create_task, broadcasters),
    )

    try:
        logger.info("starting tasks...")
        await asyncio.gather(*tasks)
    finally:
        logger.info("exited -- see ya!")
        # We keep hitting some weird case where the app hangs for 5-10 seconds here -- logs suggest that it's a TCP problem (are we making Discord unhappy by logging in and out so much?)
        #
        #                   sosig [INFO] exited -- see ya!
        #     [delay]
        #     websockets.protocol [DEBUG] client ! timed out waiting for TCP close
        #     websockets.protocol [DEBUG] client x closing TCP connection
        #     websockets.protocol [DEBUG] client - event = eof_received()
        #     websockets.protocol [DEBUG] client - event = connection_lost(None)
        #     websockets.protocol [DEBUG] client - state = CLOSED
        #     websockets.protocol [DEBUG] client x code = 1006, reason = [no reason]
