import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import llm_advisor as la


def _serve(status, body, seen):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass
    srv = HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, 'http://127.0.0.1:%d/v1/chat/completions' % srv.server_port


def test_returns_message_content_and_sends_prompt_model_nothink():
    seen = []
    srv, url = _serve(200, {'choices': [{'message': {'content': 'HELLO'}}]}, seen)
    try:
        assert la.http_transport(url, 'm1', 5)('the prompt') == 'HELLO'
    finally:
        srv.shutdown()
    body = seen[0]
    assert body['model'] == 'm1'
    assert body['messages'][-1] == {'role': 'user', 'content': 'the prompt'}
    assert body['chat_template_kwargs'] == {'enable_thinking': False}


def test_http_error_raises():
    srv, url = _serve(500, {'error': 'x'}, [])
    try:
        with pytest.raises(Exception):
            la.http_transport(url, 'm', 5)('p')
    finally:
        srv.shutdown()


def test_malformed_body_raises():
    srv, url = _serve(200, {'nope': 1}, [])
    try:
        with pytest.raises(Exception):
            la.http_transport(url, 'm', 5)('p')
    finally:
        srv.shutdown()
