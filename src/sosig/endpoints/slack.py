from __future__ import annotations

import asyncio
from typing import Dict, Mapping, MutableMapping

import slack

from sosig.endpoints.base import Endpoint, Message


class SlackEndpoint(Endpoint):
    user_cache: MutableMapping[str, Mapping]  # key is string id

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ready = asyncio.Event()
        self.rtm_client = slack.RTMClient(token=self.config["token"], run_async=True)
        self.user_cache = {}

        async def on_hello(rtm_client, web_client, data) -> None:
            self.logger.debug("HELLO with %s, %s, %s", rtm_client, web_client, data)
            self.web_client = web_client
            self.channel_name_to_id: Dict[str, str] = {}
            self.channel_id_to_name: Dict[str, str] = {}
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
            if user_id:
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
                username = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or "slack!" + user_data["id"]
                )
                avatar_url = profile.get("image_original")
            else:
                if not (data.get("username") and data.get("icons")):
                    response = await self.web_client.bots_info(bot=bot_id)
                    assert response["ok"]
                    bot_data = response["bot"]
                    assert bot_data["id"] == bot_id
                username = data.get("username") or bot_data["name"]
                avatar_url = (
                    data.get("icons", {}).get("image_original")
                    or data.get("icons", {}).get("image_72")
                    or bot_data["icons"].get("image_original")
                    or bot_data["icons"].get("image_72")
                )

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
            self.logger.info("logging out...")
            self.rtm_client.stop()  # apparently this isn't a coroutine??
            self.logger.info("logged out.")
