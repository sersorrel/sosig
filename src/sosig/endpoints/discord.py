from __future__ import annotations

import asyncio
from typing import Set

import discord

from sosig.endpoints.base import Endpoint, Message


class DiscordEndpoint(Endpoint):
    webhook_ids: Set[int]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.client = discord.Client()
        self.ready = asyncio.Event()

        @self.client.event
        async def on_ready() -> None:
            if not self.ready.is_set():
                self.logger.info("logged in as %s", self.client.user)
                self.webhook_ids = set()
                self.ready.set()

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            self.logger.debug("on_message: %s", message)
            if (
                message.author == self.client.user
                or message.author.id in self.webhook_ids
            ):
                # Ignore all messages we send. No exceptions.
                self.logger.debug(
                    "ignoring own message (%s == %s or %s in %s)",
                    message.author,
                    self.client.user,
                    message.author.id,
                    self.webhook_ids,
                )
                return
            self.logger.debug(
                "not ignoring message, author is %s (we are %s) and author id is %s (our webhooks are %s)",
                message.author,
                self.client.user,
                message.author.id,
                self.webhook_ids,
            )
            if message.channel.name not in self.received:
                # Ignore all messages not in channels we're bridging.
                # TODO: revisit this, we may want to allow commands in channels where we're only sending messages (not listening).
                self.logger.debug("ignoring message to %s", message.channel.name)
                return
            if message.clean_content.startswith("!bridge"):
                self.logger.debug("responding to bridge command %s", message)
                await message.channel.send("Bridge status: up!")
                return
            self.logger.debug(
                "received message: %s (content: %r)", message, message.clean_content
            )
            await self.received[message.channel.name].put(
                Message(
                    text=message.clean_content,
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
            text = message.text or "<empty message (image upload?)>"
            if message.thread_id is not None:
                text = "[thread] " + text
            self.logger.info("sending message: %s", message)
            try:
                await webhook.send(
                    text,
                    **{
                        x: y
                        for x, y in {
                            "username": message.username,
                            "avatar_url": message.avatar_url,
                        }.items()
                        if y is not None
                    },
                )
            except Exception:
                self.logger.exception("couldn't send message, ignoring")
            queue.task_done()

    async def run(self) -> None:
        self.logger.info("starting DiscordEndpoint")
        senders = []
        for c in self.sending:
            senders.append(asyncio.create_task(self.send_all(self.sending[c], c)))
        try:
            await self.client.start(self.config["token"])
        finally:
            self.logger.info("logging out...")
            await self.client.logout()
            self.logger.info("logged out.")
            await asyncio.gather(*senders)
            self.logger.info("bye!")
