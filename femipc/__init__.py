"""
Copyright 2023-2025 czubix

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import asyncio
import threading
import pickle
import struct
import uuid
import os
import logging

from enum import Enum

from typing import Optional, Callable, Awaitable, Any, NoReturn

if os.name == "nt":
    import win32pipe # noqa: F401
    import win32file # noqa: F401
    import pywintypes # noqa: F401
elif os.name == "posix":
    import socket
    import select

class Opcodes(Enum):
    EMIT = 0
    RESPONSE = 1

class CommunicatorReadError(Exception):
    pass

class CommunicatorWriteError(Exception):
    pass

class Communicator:
    def __init__(self, path: str, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> NoReturn:
        raise NotImplementedError

    async def send_to(self, data: bytes, path: str) -> NoReturn:
        raise NotImplementedError

    def recv(self, size: int) -> NoReturn:
        raise NotImplementedError

    def can_read(self) -> NoReturn:
        raise NotImplementedError

    def close(self) -> NoReturn:
        raise NotImplementedError

class WindowsCommunicator(Communicator):
    def __init__(self, path: str, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.loop = loop or asyncio.get_event_loop()

    async def sendto(self, data: bytes, path: str) -> None:
        pass

    def recv(self, size: int) -> bytes:
        pass

    def can_read(self) -> bool:
        return True

    def close(self) -> None:
        pass

class PosixCommunicator(Communicator):
    def __init__(self, path: str, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if os.path.exists(path):
            os.remove(path)

        self.loop = loop or asyncio.get_event_loop()

        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.setblocking(True)
        self.socket.bind(path)

    async def sendto(self, data: bytes, path: str) -> None:
        try:
            for chunk in [data[i:i+65507] for i in range(0, len(data), 65507)]:
                await self.loop.sock_sendto(self.socket, chunk, path)
        except ConnectionRefusedError as exc:
            raise CommunicatorWriteError from exc

    def recv(self, size: int) -> bytes:
        try:
            return self.socket.recv(size)
        except (socket.error, OSError) as exc:
            raise CommunicatorReadError from exc

    def can_read(self) -> bool:
        try:
            readable, _, _ = select.select([self.socket], [], [])
        except (ValueError, TypeError, OSError):
            return False

        if not readable:
            return False

        return True

    def close(self) -> None:
        self.socket.close()

class Reader(threading.Thread):
    def __init__(self, communicator: WindowsCommunicator | PosixCommunicator, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        super().__init__(daemon=True, name=f"Reader:{id(self):#x}")

        self.loop = loop or asyncio.get_event_loop()

        self.communicator = communicator

        self._end = threading.Event()

        self.queue = asyncio.Queue()

    def stop(self) -> None:
        self._end.set()

    def run(self) -> None:
        while not self._end.is_set():
            if not self.communicator.can_read():
                continue

            try:
                data = self.communicator.recv(8)
                event_length, data_length = struct.unpack("<II", data)

                length = 1 + 1 + 16 + event_length + data_length

                chunks, remainder = divmod(length, 65507)
                chunks = [65507] * chunks + ([remainder] if remainder > 0 else [])

                data = self.communicator.recv(chunks[0])
                opcode = Opcodes(data[0])
                nowait = bool(data[1])
                nonce = data[2:18]
                event = data[18:18+event_length].decode()
                data = data[18+event_length:]

                for chunk in chunks[1:]:
                    data += self.communicator.recv(chunk)
            except CommunicatorReadError:
                continue
            else:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, (opcode, nowait, nonce, event, data))

CallbackType = Callable[..., Awaitable[Any]]

MISSING = Any

class Listener:
    def __init__(self, event: str, callback: CallbackType) -> None:
        self.__name__ = event
        self.event = event
        self.callback = callback
        self.client: Client = MISSING

    def __str__(self) -> str:
        return f"{self.callback!r}"

    def __repr__(self) -> str:
        return f"{self.callback!r}"

    async def __call__(self, *args) -> Any:
        return await self.callback(self.client, *args)

def listener(event: str) -> Callable[[CallbackType], Listener]:
    def wrapper(func: CallbackType) -> Listener:
        return Listener(event, func)
    return wrapper

class Client:
    def __init__(self, path: str, peers: Optional[list[str]] = None, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.loop = loop or asyncio.get_event_loop()

        self.path = path
        self.peers: list[str] = peers or []

        if os.name == "nt":
            self.peers.append(self.path)
            self.communicator = WindowsCommunicator(path)
        elif os.name == "posix":
            self.communicator = PosixCommunicator(path)

        self.reader = Reader(self.communicator)
        self.reader.start()

        self.receiver = self.loop.create_task(self._receiver())

        self.events: dict[str, list[CallbackType]] = {}
        self.futures: dict[bytes, asyncio.Future] = {}

        for attr in self.__dir__():
            attr = getattr(self, attr)
            if isinstance(attr, Listener):
                attr: Listener
                attr.client = self
                if attr.event not in self.events:
                    self.events[attr.event] = []
                self.events[attr.event].append(attr)

    def on(self, event: str, *, func: Optional[CallbackType] = None) -> Callable[[CallbackType], CallbackType]:
        def wrapper(func: CallbackType) -> CallbackType:
            if event not in self.events:
                self.events[event] = []
            self.events[event].append(func)
            return func
        if func is not None:
            return wrapper(func)
        return wrapper

    def add_peer(self, peer: str) -> None:
        if peer in self.peers:
            raise Exception("This peer already exists")
        self.peers.append(peer)

    def remove_peer(self, peer: str) -> None:
        if peer not in self.peers:
            raise Exception("Peer not found")
        self.peers.remove(peer)

    async def send_packet(self, opcode: Opcodes, nowait: bool, nonce: bytes, event: str, args: tuple[Any]) -> None:
        data = pickle.dumps(args)

        for peer in self.peers:
            try:
                await self.communicator.sendto(struct.pack("<II", len(event), len(data)), peer)
                await self.communicator.sendto(opcode.value.to_bytes() + nowait.to_bytes() + nonce + event.encode() + data, peer)
            except CommunicatorWriteError:
                logging.debug("peer %s is unavailable" % peer)

        logging.debug("sent %s %s%s" % (event, "nowait " if nowait and opcode is Opcodes.EMIT else "", "event" if opcode is Opcodes.EMIT else "response"))

    async def emit(self, event: str, *args, nowait: bool = False) -> Any:
        nonce = uuid.uuid4().bytes
        if not nowait:
            future = self.loop.create_future()
            self.futures[nonce] = future

        await self.send_packet(Opcodes.EMIT, nowait, nonce, event, args)

        if not nowait:
            return await asyncio.wait_for(future, 10)

    async def _receiver(self) -> None:
        while True:
            opcode, nowait, nonce, event, data = await self.reader.queue.get()

            if opcode is Opcodes.RESPONSE:
                future = self.futures.get(nonce)
                if future:
                    logging.debug("received %s response" % event)
                    future.set_result(pickle.loads(data))
                continue

            if event not in self.events:
                logging.debug("event %s not found" % event)
                continue

            logging.debug("received %s event" % event)

            for func in self.events[event]:
                try:
                    result = await func(*(pickle.loads(data) if data else ()))
                    if not nowait:
                        await self.send_packet(Opcodes.RESPONSE, True, nonce, event, result)
                except Exception as exc:
                    logging.error(exc)

    def close(self) -> None:
        self.receiver.cancel()
        self.reader.stop()
        self.communicator.close()