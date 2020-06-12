from __future__ import annotations

import asyncio
from typing import Any

from sosig.endpoints.base import Endpoint, Message


class LoggerEndpoint(Endpoint[Any]):
    async def send_all(self, queue: asyncio.Queue[Message]) -> None:
        while True:
            msg = await queue.get()
            self.logger.info("sending message: %s", msg)
            queue.task_done()

    async def run(self) -> None:
        self.logger.debug("starting LoggerEndpoint")
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
