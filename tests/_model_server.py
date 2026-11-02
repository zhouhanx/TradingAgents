"""A scripted local OpenAI-compatible server for timeout and retry tests (no network).

Each request takes the next step from the script; the last step repeats. A step:

* ``delay``: seconds before the status line (headers) is sent;
* ``status`` / ``headers`` / ``body``: a plain JSON answer;
* ``chunks``: a streamed answer, a list of ``(gap_seconds, delta_dict)``;
* ``stall``: after the chunks, keep the connection open this long without data;
* ``keepalive``: ``(interval, seconds)`` of SSE ``: keep-alive`` comments before
  the chunks (a reasoning model that sends nothing but keep-alives);
* ``unfinished``: end the stream cleanly after the chunks, without a
  ``finish_reason`` or ``[DONE]``;
* ``trickle``: seconds between single bytes of a plain JSON body.
* ``finish``: the closing chunk's finish_reason (default ``"stop"``); ``None``
  sends no closing chunk; ``done``: ``False`` leaves out ``data: [DONE]``;
* ``after``: extra chunk payloads sent after the closing chunk;
* ``events``: a raw SSE stream of ``(event, data)`` pairs (Responses API), a
  ``"[DONE]"`` item sends the marker.

``hits`` also records each request's client port (``peer``), so a test can see
two requests share one keep-alive connection.

``disconnects`` records, per request index, when the server saw the client go
away (a write failed), so a test can check that a cut-off closed the socket.
"""

from __future__ import annotations

import http.server
import json
import socket as _socket
import threading
import time


def completion(content="ok", model="grok-4.7"):
    return {"id": "c1", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def chunk(delta, finish=None, model="grok-4.7"):
    return {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def text_chunks(parts, gap=0.0):
    steps = [(gap, {"role": "assistant", "content": ""})]
    steps += [(gap, {"content": part}) for part in parts]
    return steps


class ModelServer:
    def __init__(self, script):
        self.script = list(script)
        self.hits: list[dict] = []
        self.disconnects: dict[int, float] = {}
        self._t0 = time.monotonic()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                index = len(server.hits)
                server.hits.append({"t": round(time.monotonic() - server._t0, 3),
                                    "stream": bool(body.get("stream")), "body": body,
                                    "peer": self.client_address[1], "path": self.path})
                step = server.script[min(index, len(server.script) - 1)]
                try:
                    server._answer(self, step)
                except (BrokenPipeError, ConnectionResetError):
                    server.disconnects[index] = round(time.monotonic() - server._t0, 3)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _answer(handler, step):
        time.sleep(step.get("delay", 0))
        if "events" in step:
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.send_header("transfer-encoding", "chunked")
            # Every streamed answer closes its connection, so say so: otherwise the
            # client may reuse it for the retry and race the close (flaky "Server
            # disconnected without sending a response").
            handler.send_header("connection", "close")
            handler.end_headers()
            for item in step["events"]:
                if item == "[DONE]":
                    data = b"data: [DONE]\n\n"
                else:
                    event, payload = item
                    data = f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
            handler.close_connection = True
            return
        if "chunks" in step:
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.send_header("transfer-encoding", "chunked")
            # Every streamed answer closes its connection, so say so: otherwise the
            # client may reuse it for the retry and race the close (flaky "Server
            # disconnected without sending a response").
            handler.send_header("connection", "close")
            handler.end_headers()

            def send(data: bytes):
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                handler.wfile.flush()

            if step.get("keepalive"):
                interval, seconds = step["keepalive"]
                until = time.monotonic() + seconds
                while time.monotonic() < until:
                    send(b": keep-alive\n\n")
                    time.sleep(interval)
            for gap, delta in step["chunks"]:
                time.sleep(gap)
                send(f"data: {json.dumps(chunk(delta))}\n\n".encode())
            if step.get("stall"):
                time.sleep(step["stall"])
                return
            if step.get("unfinished"):    # a clean end, but the answer never finished
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
                handler.close_connection = True
                return
            if step.get("drop"):          # the connection breaks mid-answer
                handler.close_connection = True
                handler.connection.shutdown(_socket.SHUT_RDWR)
                return
            if step.get("finish", "stop") is not None:
                send(f"data: {json.dumps(chunk({}, finish=step.get('finish', 'stop')))}\n\n".encode())
            for extra in step.get("after", ()):   # e.g. a usage-only chunk
                send(f"data: {json.dumps(extra)}\n\n".encode())
            if step.get("done", True):
                send(b"data: [DONE]\n\n")
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
            handler.close_connection = True
            return
        payload = json.dumps(step.get("body", completion())).encode()
        handler.send_response(step.get("status", 200))
        for key, value in (step.get("headers") or {}).items():
            handler.send_header(key, value)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(payload)))
        handler.end_headers()
        if step.get("trickle"):
            for index in range(len(payload)):
                handler.wfile.write(payload[index:index + 1])
                handler.wfile.flush()
                time.sleep(step["trickle"])
            return
        handler.wfile.write(payload)


# The suite refuses every connection (tests/conftest.py). Saved at import, before
# that guard is installed, so a test can let 127.0.0.1 through and nothing else.
_CONNECT, _GETADDRINFO = _socket.socket.connect, _socket.getaddrinfo


def allow_loopback_only(monkeypatch):
    refused_connect, refused_lookup = _socket.socket.connect, _socket.getaddrinfo

    def connect(sock, address):
        return (_CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        return (_GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs)

    monkeypatch.setattr(_socket.socket, "connect", connect)
    monkeypatch.setattr(_socket, "getaddrinfo", getaddrinfo)
