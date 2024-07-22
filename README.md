# Description:
A simple async IPC library that is perfect for discord bots and more

# Installation:

```bash
$ git clone https://github.com/czubix/femipc.git
$ cd scheduler
$ python3 -m pip install -U .
```

or

```bash
$ python3 -m pip install git+https://github.com/czubix/femipc.git
```

# Example:
```py
from femipc import Client

bot = ...

async def event_example() -> None:
    client = Client("/tmp/bot", ["/tmp/server"])

    @client.on("add_role")
    async def on_add_role(guild_id: int, user_id: int, role_id: int):
        guild = bot.get_guild(guild_id)

        if not guild:
            return

        member = guild.get_member(user_id)

        if not member:
            return

        role = guild.get_role(role_id)

        if not role:
            return

        await member.add_roles(role_id)

async def emit_example() -> None:
    client = Client("/tmp/server", ["/tmp/bot"])

    await client.emit("add_role", guild_id, user_id, role_id)
```