from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from collections.abc import MutableMapping
from typing import Dict, Generic, Iterator, Mapping, MutableMapping, Tuple, TypeVar

import slack_sdk.rtm

from sosig.endpoints.base import Endpoint, Message

K = TypeVar("K")
V = TypeVar("V")


class ExpiredKeyError(KeyError):
    """Mapping key expired."""


class DecayingDict(MutableMapping[K, V]):
    """A dictionary that forgets what's in it, after a while.

    The first argument specifies the time each item should live for, in
    seconds (defaulting to 300 seconds, or 5 minutes).

    Expired items won't be discarded until they're explicitly removed
    (with `del`) or `DecayingDict.compact()` is called.
    """

    def __init__(self, timeout: float = 300, /, *args, **kwargs) -> None:
        if not isinstance(timeout, (int, float)):
            raise TypeError("first argument to DecayingDict is the timeout")
        self._timeout = timeout
        self._data: Dict[K, Tuple[V, float]] = {}
        tmp: Dict[K, V] = dict(*args, **kwargs)
        for key, value in tmp.items():
            self.__setitem__(key, value)

    def __getitem__(self, key: K) -> V:
        value, expiry = self._data[key]
        if time.monotonic() > expiry:
            raise ExpiredKeyError(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        self._data[key] = (value, time.monotonic() + self._timeout)

    def __delitem__(self, key: K) -> None:
        del self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._timeout}, {repr({key: value for key, (value, _) in self._data.items()})})"

    def compact(self) -> None:
        t = time.monotonic()
        to_delete = []
        for key, (value, expiry) in self._data.items():
            if t > expiry:
                to_delete.append(key)
        for key in to_delete:
            del self._data[key]


class SlackEndpoint(Endpoint):
    user_cache: DecayingDict[str, Mapping]  # key is string id

    async def _get_user_profile(self, user_id: str) -> Dict:
        assert user_id.startswith("U")
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
            self.logger.debug("got user info %s", user_data)
            profile = user_data["profile"]
        return profile

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ready = asyncio.Event()
        self.rtm_client = slack_sdk.rtm.RTMClient(token=self.config["token"], run_async=True)
        self.user_cache = DecayingDict()

        async def on_hello(rtm_client, web_client, data) -> None:
            self.logger.debug("HELLO with %s, %s, %s", rtm_client, web_client, data)
            self.web_client = web_client
            self.channel_name_to_id: Dict[str, str] = {}
            self.channel_id_to_name: Dict[str, str] = {}
            DONE = object()
            cursor = None
            while cursor is not DONE:
                r = await self.web_client.conversations_list(
                    types="public_channel,private_channel",
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

        async def on_message(rtm_client, web_client, data) -> None:
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

            # TODO: replace this mess with something that branches on the subtype
            # TODO: work out a better way to get the highest-resolution avatar
            if user_id:
                profile = await self._get_user_profile(user_id)
                username = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or "slack!" + user_id
                )
                avatar_url = profile.get("image_original")
            else:
                icons = {}
                if not (data.get("username") and data.get("icons")):
                    response = await self.web_client.bots_info(bot=bot_id)
                    assert response["ok"]
                    bot_data = response["bot"]
                    assert bot_data["id"] == bot_id
                    icons.update(bot_data["icons"])
                icons.update(data.get("icons", {}))
                username = data.get("username") or bot_data["name"]
                avatar_url = icons.get("image_original") or icons.get("image_72")

            profiles: Dict[str, Dict] = {}
            for match in re.finditer(r"<@([UW][^>]+)>", text):
                profiles[match.group(1)] = await self._get_user_profile(match.group(1))
            for pattern, replacement in [
                (r"<#(C[^|]+)\|([^>]+)>", lambda m: f"#{m.group(2)}"),
                (
                    r"<@([UW][^>]+)>",
                    lambda m: f"@{(profile := profiles[m.group(1)]).get('display_name') or profile.get('real_name') or 'slack!' + m.group(1)}",
                ),
                # TODO: look up group IDs
                (r"<!subteam^([^>]+)>", lambda m: f"@<group with id {m.group(1)}>"),
                (
                    r"<!date^(?P<timestamp>[^^]+)^(?P<format>[^^]+)(?:^(P<link>[^|]+))?\|(?P<text>[^>]+)>",
                    lambda m: m.group("text"),
                ),
                (r"<!here(|here)?>", "@here"),
                (r"<!channel>", "@channel"),
                (r"<!everyone>", "@everyone"),
                (r"&lt;", "<"),
                (r"&gt;", ">"),
                (r"&amp;", "&"),
            ]:
                text = re.sub(pattern, replacement, text)

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

        async def on_error(rtm_client, web_client, data):
            self.logger.error("slackclient error: %s", data)

        # TODO: raise an issue on the slackclient repo mentioning that this sucks and the official way (run_on) sucks more
        self.rtm_client._callbacks["hello"] = [on_hello]
        self.rtm_client._callbacks["message"] = [on_message]
        self.rtm_client._callbacks["error"] = [on_error]

    async def send_all(self, queue: asyncio.Queue[Message], channel) -> None:
        await self.ready.wait()
        channel_id = self.channel_name_to_id[channel]
        self.logger.debug("starting sender for channel %s (%s)", channel, channel_id)
        while True:
            message = await queue.get()
            text = message.text
            for pattern, replacement in [
                (r"&", "&amp;"),
                (r"<", "&lt;"),
                (r">", "&gt;"),
            ]:
                text = re.sub(pattern, replacement, text)
            message = message._replace(text=text)
            self.logger.info("sending message: %s", message)
            try:
                await self.web_client.chat_postMessage(
                    channel=channel_id,
                    as_user=False,
                    text=message.text or "<empty message (image upload?)>",
                    link_names=False,
                    **{
                        x: y
                        for x, y in {
                            "username": message.username,
                            "icon_url": message.avatar_url,
                        }.items()
                        if y is not None
                    },
                )
            except asyncio.CancelledError:
                self.logger.debug(
                    "sender for channel %s (%s) cancelled, exiting", channel, channel_id
                )
                raise
            except Exception:
                self.logger.exception("couldn't send message, ignoring")
            queue.task_done()

    async def run(self) -> None:
        self.logger.info("starting SlackEndpoint")
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
            self.logger.info("begin shutdown")
            self.logger.info("stopping client...")
            await self.rtm_client.async_stop()
            self.logger.info("waiting for senders...")
            await asyncio.gather(*senders, return_exceptions=True)
            self.logger.info("done. bye!")
