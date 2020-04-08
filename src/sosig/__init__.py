from __future__ import annotations

import asyncio
import configparser
import importlib.metadata
import itertools
import logging
import sys
from collections import defaultdict
from typing import (
    Any,
    Dict,
    Generic,
    Mapping,
    MutableMapping,
    NamedTuple,
    Optional,
    Set,
    TypeVar,
)

import discord
import slack

__all__ = ["main"]


logging.basicConfig(
    format="%(name)20s [%(levelname)s] %(message)s", level=logging.WARNING
)
logging.getLogger(__name__).setLevel(logging.DEBUG)


class Message(NamedTuple):
    text: str
    username: Optional[str]
    avatar_url: Optional[str] = None
    thread_id: Optional[str] = None


Location = TypeVar("Location")  # some unique id for a channel/server/network/whatever


class Bridge(Generic[Location]):
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
            "initialising bridge %s, sending: %s, received: %s",
            type(self).__name__,
            sending.keys(),
            received.keys(),
        )
        self.sending = sending
        self.received = received
        self.config = config


class LoggerBridge(Bridge[Any]):
    async def send_all(self, queue: asyncio.Queue[Message]) -> None:
        while True:
            msg = await queue.get()
            self.logger.info("sending message: %s", msg)
            queue.task_done()

    async def run(self) -> None:
        self.logger.debug("starting LoggerBridge")
        senders = [
            asyncio.create_task(self.send_all(self.sending[c])) for c in self.sending
        ]
        sentinel = object()
        n = 0
        while True:
            await asyncio.sleep(10)
            for location, queue in self.received.items():
                self.logger.debug("received fake message %s from %s", n, location)
                await queue.put(
                    Message(
                        text=f"a fake message from {location} {n}", username="nobody",
                    )
                )
                n += 1


class DiscordBridge(Bridge):
    webhook_ids: Set[int]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.client = discord.Client()
        self.ready = asyncio.Event()

        @self.client.event
        async def on_ready() -> None:
            self.logger.info("logged in as %s", self.client.user)
            self.webhook_ids = set()
            self.ready.set()

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            if (
                message.author == self.client.user
                or message.author.id in self.webhook_ids
            ):
                # Ignore all messages we send. No exceptions.
                self.logger.debug("ignoring own message")
                return
            if message.channel.name not in self.received:
                # Ignore all messages not in channels we're bridging.
                # TODO: revisit this, we may want to allow commands in channels where we're only sending messages (not listening).
                self.logger.debug("ignoring message to %s", message.channel.name)
                return
            if message.content.startswith("!bridge"):
                self.logger.debug("responding to bridge command %s", message)
                await message.channel.send("Bridge status: up!")
                return
            self.logger.debug("bridging message: %s", message)
            await self.received[message.channel.name].put(
                Message(
                    text=message.content,
                    username=message.author.display_name,
                    avatar_url=str(message.author.avatar_url).replace(
                        ".webp", ".png"  # TODO: oh no this is awful
                    ),
                )
            )

    async def send_all(self, queue: asyncio.Queue[Message], channel) -> None:
        await self.ready.wait()
        self.logger.debug("starting sender for channel %s", channel)
        guild = self.client.guilds[0]  # TODO: guild selection
        self.logger.debug("got guild %s", guild)
        discord_channel = next(
            discord_channel
            for discord_channel in guild.channels
            if discord_channel.type is discord.ChannelType.text
            and discord_channel.name == channel
        )
        self.logger.debug("got channel %s", channel)
        webhook = next(
            (
                webhook
                for webhook in await discord_channel.webhooks()
                if webhook.type is discord.WebhookType.incoming
                and webhook.user == self.client.user
            ),
            None,
        )
        self.logger.debug("got webhook %s", webhook)
        if webhook is None:
            # set one up
            webhook = await discord_channel.create_webhook(name=f"sosig (#{channel})")
            self.logger.debug("created webhook %s", webhook)
        self.webhook_ids.add(webhook.id)
        while True:
            message = await queue.get()
            self.logger.info("sending message: %s", message)
            text = (
                message.text
                if message.thread_id is None
                else "[thread] " + message.text
            )
            await webhook.send(
                message.text,
                **{
                    x: y
                    for x, y in {
                        "username": message.username,
                        "avatar_url": message.avatar_url,
                    }.items()
                    if y is not None
                },
            )
            queue.task_done()

    async def run(self) -> None:
        self.logger.info("starting DiscordBridge")
        senders = []
        for c in self.sending:
            senders.append(asyncio.create_task(self.send_all(self.sending[c], c)))
        try:
            await self.client.start(self.config["token"])
        finally:
            self.logger.info("logging out...")
            await self.client.logout()
            self.logger.info("logged out.")


class SlackBridge(Bridge):
    user_cache: MutableMapping[str, Mapping]  # key is string id

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready = asyncio.Event()
        self.rtm_client = slack.RTMClient(token=self.config["token"], run_async=True)
        self.user_cache = {}

        async def on_hello(rtm_client, web_client, data):
            self.logger.debug("HELLO with %s, %s, %s", rtm_client, web_client, data)
            self.web_client = web_client
            self.channel_name_to_id = {}
            self.channel_id_to_name = {}
            DONE = object()
            cursor = None
            while cursor is not DONE:
                r = await self.web_client.conversations_list(
                    **({"cursor": cursor} if cursor else {})
                )
                self.logger.debug("got conversations list: %s", r)
                assert r["ok"]
                self.channel_name_to_id.update(
                    {c["name"]: c["id"] for c in r["channels"]}
                )
                self.channel_id_to_name.update(
                    {c["id"]: c["name"] for c in r["channels"]}
                )
                cursor = r["response_metadata"]["next_cursor"] or DONE
            self.logger.debug("cached channel names/ids")
            auth_data = await self.web_client.auth_test()
            assert auth_data["ok"]
            self.logger.debug("got own user information %s", auth_data)
            self.user_id = auth_data["user_id"]
            self.bot_id = auth_data["bot_id"]
            self.ready.set()

        async def on_message(rtm_client, web_client, data):
            subtype = data.get("subtype")
            text = data.get("text")
            hidden = data.get("hidden")
            user_id = data.get("user")
            bot_id = data.get("bot_id")
            channel_id = data.get("channel")

            if subtype == "bot_message" and bot_id == self.bot_id:
                # Ignore all messages we send. No exceptions.
                self.logger.debug("ignoring own message %s", data)
                return
            if text and text.startswith("!bridge"):
                self.logger.debug("responding to bridge command %s", text)
                await self.web_client.chat_postMessage(
                    channel=channel_id, text="Bridge status: up!"
                )
                return
            if hidden:
                self.logger.debug("ignoring hidden message %s", data)
                return
            if "thread_ts" in data and subtype != "thread_broadcast":
                self.logger.debug("ignoring threaded message %s", data)
                return
            self.logger.debug("received message: %s", data)

            if user_id in self.user_cache:
                user_data = self.user_cache[user_id]
                profile = user_data["profile"]
                self.logger.debug(
                    "got cached user entry with %s, %s",
                    profile.get("display_name"),
                    profile.get("real_name"),
                )
            else:
                response = await self.web_client.users_info(user=user_id)
                assert response["ok"]
                user_data = response["user"]
                assert user_data["id"] == user_id
                self.user_cache[user_id] = user_data
                self.logger.debug("got user info %s", data)
                profile = user_data["profile"]
            username = (
                profile.get("display_name")
                or profile.get("real_name")
                or "slack!" + user["id"]
            )
            avatar_url = profile.get("image_original")
            # TODO: handle channels not in the channel cache
            await self.received[self.channel_id_to_name.get(channel_id)].put(
                Message(
                    text=text,
                    **{
                        k: v
                        for k, v in {
                            "username": username,
                            "avatar_url": avatar_url,
                        }.items()
                        if v
                    },
                )
            )

        # TODO: raise an issue on the slackclient repo mentioning that this sucks and the official way (run_on) sucks more
        self.rtm_client._callbacks["hello"] = [on_hello]
        self.rtm_client._callbacks["message"] = [on_message]

    async def send_all(self, queue: asyncio.Queue[Message], channel) -> None:
        await self.ready.wait()
        channel_id = self.channel_name_to_id[channel]
        self.logger.debug("starting sender for channel %s (%s)", channel, channel_id)
        while True:
            message = await queue.get()
            self.logger.info("sending message: %s", message)
            await self.web_client.chat_postMessage(
                channel=channel_id,
                as_user=False,
                text=message.text,
                **{
                    x: y
                    for x, y in {
                        "username": message.username,
                        "icon_url": message.avatar_url,
                    }.items()
                    if y is not None
                },
            )
            queue.task_done()

    async def run(self) -> None:
        self.logger.info("starting SlackBridge")
        senders = []
        for c in self.sending:
            senders.append(asyncio.create_task(self.send_all(self.sending[c], c)))
        try:
            # We would like to be able to do this here:
            #
            #     await self.rtm_client.start()
            #
            # but we can't, because the RTMClient *sucks utter balls*.
            # Specifically, it fucks with the signal handler that aiorun
            # has already installed, and means that ctrl-c doesn't work
            # any more.
            #
            # Instead, we have to do it by hand:
            await asyncio.ensure_future(self.rtm_client._connect_and_read())
        finally:
            self.logger.info("logging out...")
            self.rtm_client.stop()  # apparently this isn't a coroutine??
            self.logger.info("logged out.")


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

    # Set up the bridges.
    CONFIG = {
        DiscordBridge: ["general"],
        SlackBridge: ["general"],
    }
    bridges = []
    for bridge, cfg in CONFIG.items():
        bridges.append(
            bridge(
                sending={chan: asyncio.Queue() for chan in cfg},
                received=defaultdict(
                    DevNullQueue, {chan: asyncio.Queue() for chan in cfg}
                ),
                config=config_parser[bridge.__name__],
            )
        )
    # Join the relevant bridges together.
    LINKS = [
        ((DiscordBridge, "general"), (SlackBridge, "general")),
        ((SlackBridge, "general"), (DiscordBridge, "general")),
    ]
    broadcasters = []  # TODO bad name
    for source, dest in LINKS:
        source_bridge = next(bridge for bridge in bridges if type(bridge) == source[0])
        dest_bridge = next(bridge for bridge in bridges if type(bridge) == dest[0])
        broadcasters.append(
            message_pusher(
                source_bridge.received[source[1]], dest_bridge.sending[dest[1]]
            )
        )

    tasks = itertools.chain(
        (asyncio.create_task(bridge.run()) for bridge in bridges),
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
