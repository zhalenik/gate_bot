"""The offline guard must allow OS-local asyncio pipes and reject external I/O."""
import asyncio
import socket

import pytest


def test_default_asyncio_loop_works_without_external_network():
    async def run():
        await asyncio.sleep(0)
        return 'local-loop-ready'

    assert asyncio.run(run()) == 'local-loop-ready'


def test_loopback_pipe_can_exchange_bytes():
    with socket.socket() as listener, socket.socket() as client:
        listener.settimeout(2)
        client.settimeout(2)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        server, _ = listener.accept()
        with server:
            server.settimeout(2)
            client.sendall(b'local')
            assert server.recv(5) == b'local'


@pytest.mark.parametrize('method', ['connect', 'connect_ex'])
def test_external_numeric_address_is_blocked_before_connect(method):
    with socket.socket() as client:
        with pytest.raises(AssertionError, match='forbidden'):
            getattr(client, method)(('192.0.2.1', 443))


def test_external_dns_lookup_is_blocked():
    with pytest.raises(AssertionError, match='forbidden'):
        socket.getaddrinfo('example.invalid', 443)


def test_async_external_numeric_address_is_blocked():
    async def run():
        with socket.socket() as client:
            client.setblocking(False)
            await asyncio.get_running_loop().sock_connect(client, ('192.0.2.1', 443))

    with pytest.raises(AssertionError, match='forbidden'):
        asyncio.run(run())
