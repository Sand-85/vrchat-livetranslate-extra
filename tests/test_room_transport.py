"""Real local WebSocket authentication and redirect boundaries; no external service."""
import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import websockets
from vlt.room.client import RoomClient
from vlt.room.model import RoomConfig
from vlt.room.protocol import ProtocolError


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_ascii_and_unicode_authentication_reaches_real_server(self):
        received = []

        async def handler(ws):
            request = getattr(ws, 'request', None)
            headers = request.headers if request else ws.request_headers
            received.append(headers.get('Authorization'))
            await ws.wait_closed()

        async with websockets.serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            for token in ('synthetic-token', '合成令牌 テスト'):
                client = RoomClient(RoomConfig(server_url=f'ws://127.0.0.1:{port}/ws',
                                               token=token), lambda _: None)
                ws = await asyncio.wait_for(client._open_ws(client._connect_url()), 3)
                await ws.close()
                if token.isascii():
                    self.assertEqual(received[-1], 'Bearer ' + token)
                else:
                    self.assertEqual(base64.urlsafe_b64decode(received[-1].split()[1]).decode(), token)

    async def test_real_redirect_never_forwards_authorization_to_another_server(self):
        reached = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                reached.append(self.headers.get('Authorization'))
                self.send_response(403)
                self.end_headers()

            def log_message(self, *args):
                pass

        target = ThreadingHTTPServer(('127.0.0.1', 0), Target)

        class Redirect(Target):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                self.send_response(307)
                self.send_header('Location', f'ws://127.0.0.1:{target.server_port}/ws')
                self.send_header('Content-Length', '0')
                self.end_headers()

        redirect = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (target, redirect)]
        for thread in threads:
            thread.start()
        try:
            client = RoomClient(RoomConfig(server_url=f'ws://127.0.0.1:{redirect.server_port}/ws',
                                           token='synthetic-token'), lambda _: None)
            with self.assertRaisesRegex(ProtocolError, 'HTTP 307'):
                await asyncio.wait_for(client._open_ws(client._connect_url()), 3)
            self.assertEqual(reached, [])
        finally:
            for server in (target, redirect):
                await asyncio.to_thread(server.shutdown)
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
