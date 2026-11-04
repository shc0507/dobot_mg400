"""The orchestrator waitress options with a real loopback server."""

import http.client
import threading

from waitress import create_server

from orchestrator.__main__ import SERVER_OPTIONS


def test_server_blocks_slow_viewers_early_and_still_sends_large_responses():
    assert SERVER_OPTIONS["outbuf_high_watermark"] == 256 * 1024
    body = bytes(range(256)) * (16 * 1024)  # 4 MiB, 16 times the watermark

    def app(environ, start_response):
        start_response("200 OK", [("Content-Length", str(len(body)))])
        return [body]

    server = create_server(app, host="127.0.0.1", port=0, **SERVER_OPTIONS)
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.effective_port, timeout=10)
        connection.request("GET", "/")
        response = connection.getresponse()
        received = bytearray()
        while chunk := response.read(64 * 1024):
            received += chunk
        connection.close()
        assert response.status == 200 and bytes(received) == body
    finally:
        server.close()
        worker.join(5)
