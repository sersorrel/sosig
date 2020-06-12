from __future__ import annotations

import asyncio
import configparser
import importlib.metadata
import itertools
import logging
import sys
from collections import defaultdict

from sosig.endpoints import DiscordEndpoint, SlackEndpoint

__all__ = ["main"]


logging.basicConfig(
    format="%(name)20s [%(levelname)s] %(message)s", level=logging.WARNING
)
logging.getLogger(__name__).setLevel(logging.DEBUG)


class DevNullQueue(asyncio.Queue):
    """A subclass of Queue that is always empty.

    Attempting to put things into the queue will immediately succeed;
    coroutines that get an item from the queue will block forever.
    """

    def _init(self, maxsize):
        self._queue = []  # necessary, internals look at len(self._queue)

    def _get(self):
        pass

    def _put(self, item):
        pass


async def message_pusher(source, dest):
    logging.getLogger(__name__ + ".message_pusher").debug(
        "starting message pusher from %r to %r", source, dest
    )
    while True:
        await dest.put(await source.get())
        source.task_done()


async def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} CONFIG_FILE")
        sys.exit(1)

    logger = logging.getLogger(__name__)
    logger.info("starting %s %s", __name__, importlib.metadata.version(__name__))

    config_file = sys.argv[1]
    config_parser = configparser.ConfigParser(strict=True, interpolation=None)
    config_parser.read(config_file)

    # Set up the endpoints.
    CONFIG = {
        DiscordEndpoint: ["general"],
        SlackEndpoint: ["general"],
    }
    endpoints = []
    for endpoint, cfg in CONFIG.items():
        endpoints.append(
            endpoint(
                sending={chan: asyncio.Queue() for chan in cfg},
                received=defaultdict(
                    DevNullQueue, {chan: asyncio.Queue() for chan in cfg}
                ),
                config=config_parser[endpoint.__name__],
            )
        )
    # Join the relevant endpoints together.
    LINKS = [
        ((DiscordEndpoint, "general"), (SlackEndpoint, "general")),
        ((SlackEndpoint, "general"), (DiscordEndpoint, "general")),
    ]
    broadcasters = []  # TODO bad name
    for source, dest in LINKS:
        source_endpoint = next(endpoint for endpoint in endpoints if type(endpoint) == source[0])
        dest_endpoint = next(endpoint for endpoint in endpoints if type(endpoint) == dest[0])
        broadcasters.append(
            message_pusher(
                source_endpoint.received[source[1]], dest_endpoint.sending[dest[1]]
            )
        )

    tasks = itertools.chain(
        (asyncio.create_task(endpoint.run()) for endpoint in endpoints),
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
