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


async def handle_tcp(reader, writer):
    peer = writer.get_extra_info('peername')
    ip = peer[0] if peer else 'unknown'
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            lines = data.splitlines() or [data]
            for line in lines:
                if line:
                    message = line.decode('utf-8', errors='replace')
                    await asyncio.to_thread(record_log, ip, 'TCP', message)
    finally:
        writer.close()
        await writer.wait_closed()


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
    tcp_server = await asyncio.start_server(handle_tcp, HOST, PORT)
    asyncio.create_task(prune_loop())
    print(f'Tessera syslog collector listening on UDP/TCP {PORT}', flush=True)
    try:
        async with tcp_server:
            await tcp_server.serve_forever()
    finally:
        udp_transport.close()


if __name__ == '__main__':
    asyncio.run(main())
