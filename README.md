# Description:
A simple async IPC library made with discord bots in mind

# Installation:

```bash
$ git clone https://github.com/czubix/femipc.git
$ cd femipc
$ python3 -m pip install -U .
```

or

```bash
$ python3 -m pip install git+https://github.com/czubix/femipc.git
```

# Example:
```py
from femipc import Client

async def event_example() -> None:
    client = Client("/tmp/bot", ["/tmp/server"])

    @client.on("add_role")
    async def on_add_role(guild_id: int, user_id: int, role_id: int):
        ...

async def emit_example() -> None:
    client = Client("/tmp/server", ["/tmp/bot"])

    await client.emit("add_role", guild_id, user_id, role_id)
```
```py
from femipc import Client. listener

from typing import List

class IPC(Client):
    def __init__(self, path: str, peers: List[str]) -> None:
        super().__init__(path, peers)

    @listener("echo")
    async def on_test(self, *args):
        await self.emit("echo", *args)
```