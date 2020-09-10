from __future__ import annotations

import asyncio
import textwrap
from typing import Set

import pydle

from sosig.endpoints.base import Endpoint, Message


class IRCClient(pydle.Client):
    def __init__(self, *args, endpoint, **kwargs) -> None:
        self.endpoint = endpoint
        self.logger = self.endpoint.logger

        super().__init__(*args, **kwargs)

    async def on_connect(self) -> None:
        self.logger.info("logged in as %s", self.nickname)
        self.endpoint.ready.set()

        for channel in self.endpoint.received:
            await self.join(channel)

    async def on_message(self, target, source, message) -> None:
        self.logger.debug(
            'on_message: target=%s source=%s message="%s"', target, source, message
        )

        # Ignore all messages we send.
        if source == self.nickname:
            self.logger.debug("ignoring own message (%s == %s)", source, self.nickname)
            return

        if target not in self.endpoint.received:
            # Ignore all messages not in channels we're bridging.
            # TODO: revisit this, we may want to allow commands in channels where we're only sending messages (not listening).
            self.logger.debug("ignoring message to %s", target)
            return

        # Handle bridge commands first.
        if message.startswith("!bridge"):
            self.logger.debug("responding to bridge command %s", message)
            await self.message(target, "Bridge status: up!")
            return

        self.logger.debug(
            "received message: %s", message,
        )

        await self.endpoint.received[target].put(
            Message(
                text=message,
                username=source,
                avatar_url="https://9net.org/~stary/sosig-dev.png",
            )
        )


class IRCEndpoint(Endpoint):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ready = asyncio.Event()
        self.client = IRCClient(
            self.config["nickname"], realname=self.config["realname"], endpoint=self
        )

    async def send_all(self, queue: asyncio.Queue[Message], channel) -> None:
        await self.ready.wait()
        self.logger.debug("starting sender for channel %s", channel)

        while True:
            message = await queue.get()
            text = message.text or "<empty message (image upload?)>"
            if message.thread_id is not None:
                text = "[thread] " + text
            self.logger.info("sending message: %s", message)
            try:
                # XXX Do this better? ie prefix list
                # This will break if it's >512 or whatever but who cares, the bot will also break
                if message.text.startswith("!"):
                    self.logger.debug("Passing through command %s", message)
                    await self.client.message(
                        channel, "Command sent from remote by %s" % message.username
                    )
                    await self.client.message(channel, message.text)
                    return

                max_message_len = 300  # XXX don't @ me
                prefix = "<%s> " % message.username

                if len(message.text) > max_message_len:
                    lines = textwrap.wrap(
                        message.text,
                        width=max_message_len,
                        initial_indent=prefix,
                        subsequent_indent=prefix,
                    )
                    for line in lines:
                        await self.client.message(channel, line)
                else:
                    await self.client.message(channel, prefix + message.text)
            except Exception:
                self.logger.exception("couldn't send message, ignoring")
            queue.task_done()

    async def run(self) -> None:
        self.logger.info("starting IRCEndpoint")
        senders = []
        for c in self.sending:
            senders.append(asyncio.create_task(self.send_all(self.sending[c], c)))
        try:
            # XXX: bring this up with pydle maintainers lmao
            # connect does a create_task so it's all fucked
            is_ssl = self.config["ssl"]
            hostname = str(self.config["server"])
            port = int(self.config.get("port", 6697 if is_ssl else 6667))

            await self.client.connect(hostname=hostname, port=port, tls=is_ssl)
        finally:
            pass
            """
            self.logger.info("logging out...")
            if self.client.connected:
                await self.client.quit("fug")
            self.logger.info("logged out.")
            await asyncio.gather(*senders)
            self.logger.info("bye!")"""
