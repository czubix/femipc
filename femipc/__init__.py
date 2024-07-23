"""
Copyright 2023-2024 czubix

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

import asyncio, socket, select, threading, pickle, struct, uuid, os, logging

from enum import Enum

from typing import List, Dict, Tuple, Optional, Callable, Awaitable, Any

class Opcodes(Enum):
    EMIT = 0
    RESPONSE = 1

class SocketReader(threading.Thread):
    def __init__(self, socket: socket.socket, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        super().__init__(daemon=True, name=f"SocketReader:{id(self):#x}")

        self.loop = loop or asyncio.get_event_loop()

        self.socket = socket

        self._end = threading.Event()

        self.queue = asyncio.Queue()

    def stop(self) -> None:
        self._end.set()

    def get_packet(self, opcode: Opcodes, nonce: bytes, event: str, data: bytes) -> bytes:
        return struct.pack("H16sHI", opcode.value, nonce, len(event), len(data))

    def run(self) -> None:
        while not self._end.is_set():
            try:
                readable, _, _ = select.select([self.socket], [], [], 30)
            except (ValueError, TypeError, OSError):
                continue

            if not readable:
                continue

            try:
                data = self.socket.recv(24)
                opcode, nonce, event_length, data_length = struct.unpack("H16sHI", data)
                opcode = Opcodes(opcode)

                data = self.socket.recv(event_length + data_length)
                event = data[:event_length].decode()
                data = data[event_length:]
            except OSError:
                continue
            else:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, (opcode, nonce, event, data))

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
    def __init__(self, path: str, peers: Optional[List[str]] = None, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.loop = loop or asyncio.get_event_loop()

        if os.path.exists(path):
            os.remove(path)

        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.socket.bind(path)
        
        self.path = path
        self.peers: List[str] = peers or []

        self.reader = SocketReader(self.socket)
        self.reader.start()

        self.receiver = self.loop.create_task(self._receiver())

        self.events: Dict[str, List[CallbackType]] = {}
        self.futures: Dict[bytes, asyncio.Future] = {}

        for attr in self.__dir__():
            attr = getattr(self, attr)
            if isinstance(attr, Listener):
                attr: Listener
                attr.client = self
                if not attr.event in self.events:
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

    async def send_packet(self, opcode: Opcodes, nonce: bytes, event: str, args: Tuple[Any]) -> None:
        data = pickle.dumps(args)

        for peer in self.peers:
            try:
                await self.loop.sock_sendto(self.socket, self.reader.get_packet(opcode, nonce, event, data), peer)
                await self.loop.sock_sendto(self.socket, event.encode() + data, peer)
            except ConnectionRefusedError:
                logging.debug("peer %s is unavailable" % peer)

    async def emit(self, event: str, *args, nowait: bool = False) -> Any:
        nonce = uuid.uuid4().bytes
        if not nowait:
            future = self.loop.create_future()
            self.futures[nonce] = future

        await self.send_packet(Opcodes.EMIT, nonce, event, args)

        if not nowait:
            return await asyncio.wait_for(future, 10)

    async def _receiver(self) -> None:
        while True:
            opcode, nonce, event, data = await self.reader.queue.get()

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
                    await self.send_packet(Opcodes.RESPONSE, nonce, event, result)
                except Exception as error:
                    logging.error(error)

    def close(self) -> None:
        self.receiver.cancel()
        self.reader.stop()
        self.socket.close()
        os.remove(self.path)