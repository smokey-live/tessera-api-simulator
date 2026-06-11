#!/usr/bin/env python3
import asyncio, os
from tessera_sim import handle_tcp, init_state
async def main():
    init_state()
    server=await asyncio.start_server(handle_tcp,'0.0.0.0', int(os.environ.get('TESSERA_TCP_PORT','3000')))
    async with server: await server.serve_forever()
asyncio.run(main())
