from __future__ import annotations

import asyncio
import re
import textwrap
from typing import Set

import pydle

from sosig.endpoints.base import Endpoint, Message


class IRCClient(pydle.Client):
    # Really try to reconnect.
    RECONNECT_MAX_ATTEMPTS = None

    def __init__(self, *args, endpoint, **kwargs) -> None:
        self.endpoint = endpoint
        self.logger = self.endpoint.logger
        self.done = asyncio.Event()

        super().__init__(*args, **kwargs)

    async def on_connect(self) -> None:
        self.logger.info("logged in as %s", self.nickname)
        self.endpoint.ready.set()

        for channel in self.endpoint.received:
            await self.join(channel)

    # TODO: IRC has formatting! we currently don't deal with it.
    async def on_channel_message(self, target, by, message) -> None:
        self.logger.debug(
            'on_message: target=%s by=%s message="%s"', target, by, message
        )

        # Ignore all messages we send.
        if by == self.nickname:
            self.logger.debug("ignoring own message (%s == %s)", by, self.nickname)
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

        # Strip IRC formatting - see https://modern.ircdocs.horse/formatting.html for an overview of the formatting.
        # also see https://stackoverflow.com/questions/10567701/regex-replace-of-mirc-colour-codes
        message_clean = re.sub(r"[\x02\x0f\x11\x16\x1d\x1e\x1f]", "", message)
        message_clean = re.sub(r"\x03(\d{1,2}(,\d{1,2})?)?", "", message_clean)

        await self.endpoint.received[target].put(
            Message(
                text=message_clean,
                username=by,
                avatar_url=self.endpoint.avatar_url.replace("$username", by),
            )
        )

    async def on_channel_notice(self, target, by, contents) -> None:
        await self.on_channel_message(target, by, "*" + contents + "*")

    async def on_ctcp_action(self, by, target, contents) -> None:
        await self.on_channel_message(target, by, "_" + contents + "_")

    async def on_disconnect(self, expected) -> None:
        await super().on_disconnect(expected)
        if expected or not self.connected:  # ok, shut down then
            self.done.set()


def mask_ping(username):
    return username[0] + "\u200C" + username[1:]


# This is the same thing slack-irc does.
def colorize_username(username, key=None):
    if not key:
        key = username

    colour_table = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]

    v = 5381
    for c in key:
        v = v * 33 + ord(c)
    return "\x03{}{}\x0F".format(colour_table[v % len(colour_table)], username)


class IRCEndpoint(Endpoint):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ready = asyncio.Event()
        self.client = IRCClient(
            self.config["nickname"],
            realname=self.config["realname"],
            endpoint=self,
            sasl_username=self.config.get("sasl_username", None),
            sasl_password=self.config.get("sasl_password", None),
            sasl_identity=self.config.get("sasl_identity", ""),
        )

        default_url = "http://api.adorable.io/avatars/48/$username.png"
        self.avatar_url = self.config.get("avatar_url", default_url)

        self.mask_usernames = self.config.get("mask_usernames", True)
        self.colorize_username = self.config.get("colorize_usernames", True)

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
                if message.text.startswith("!"):
                    self.logger.debug("Passing through command %s", message)
                    await self.client.message(
                        channel, "Command sent from remote by %s" % message.username
                    )
                    await self.client.message(channel, message.text)
                    queue.task_done()
                    continue

                # This is currently completely arbitrary. If it becomes an issue, maybe revisit it.
                # We can't do what pydle does because it uses an internal method to calculate the length.
                max_message_len = 300

                if self.mask_usernames and message.username.lower() in (
                    user.lower() for user in self.client.channels[channel]["users"]
                ):
                    username = mask_ping(message.username)
                else:
                    username = message.username

                if self.colorize_username:
                    prefix = "<%s> " % colorize_username(username, key=message.username)
                else:
                    prefix = "<%s> " % username

                for line in message.text.splitlines():
                    if len(line) + len(prefix) > max_message_len:
                        wrapped_lines = textwrap.wrap(
                            line,
                            width=max_message_len,
                            initial_indent=prefix,
                            subsequent_indent=prefix,
                        )
                        for wrapped_line in wrapped_lines:
                            await self.client.message(channel, wrapped_line)
                    else:
                        await self.client.message(channel, prefix + line)
            except asyncio.CancelledError:
                self.logger.debug("sender for channel %s cancelled, exiting", channel)
                raise
            except Exception:
                self.logger.exception("couldn't send message, ignoring")
            queue.task_done()

    async def run(self) -> None:
        self.logger.info("starting IRCEndpoint")
        senders = []
        for c in self.sending:
            senders.append(asyncio.create_task(self.send_all(self.sending[c], c)))

        try:
            is_ssl = self.config.get("ssl", True)
            hostname = str(self.config["server"])
            port = int(self.config.get("port", 6697 if is_ssl else 6667))

            await self.client.connect(hostname=hostname, port=port, tls=is_ssl)
            await self.client.done.wait()
        finally:
            self.logger.info("begin shutdown")
            if self.client.connected:
                self.logger.info("sending quit...")
                await self.client.quit("fug")
            self.logger.info("waiting for senders...")
            await asyncio.gather(*senders, return_exceptions=True)
            self.logger.info("done. bye!")
