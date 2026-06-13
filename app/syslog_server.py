#!/usr/bin/env python3
import asyncio
import os

from log_store import init_log_db, prune_old_logs, record_log


HOST = '0.0.0.0'
PORT = int(os.environ.get('TESSERA_SYSLOG_PORT', '514'))


class SyslogUDPProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        message = data.decode('utf-8', errors='replace')
        asyncio.create_task(asyncio.to_thread(record_log, addr[0], 'UDP', message))


async def prune_loop():
    while True:
        await asyncio.to_thread(prune_old_logs)
        await asyncio.sleep(3600)


async def main():
    init_log_db()
    loop = asyncio.get_running_loop()
    udp_transport, _ = await loop.create_datagram_endpoint(
        SyslogUDPProtocol,
        local_addr=(HOST, PORT),
    )
    asyncio.create_task(prune_loop())
    print(f'Tessera syslog collector listening on UDP {PORT}', flush=True)
    try:
        await asyncio.Future()
    finally:
        udp_transport.close()


if __name__ == '__main__':
    asyncio.run(main())
