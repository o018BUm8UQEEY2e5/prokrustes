"""Unit tests for Unix domain socket transport.

No server is needed except for one test that binds a real ``AF_UNIX`` socket
locally, which only proves our connection dials the socket path rather than
opening TCP. Everything else fakes the opener.
"""

from __future__ import annotations

import io
import json
import os
import socket as stdsocket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

from prokrustes import unixsocket


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, payloads, routes=None):
        self.calls = []
        self.gets = []
        self._payloads = list(payloads)
        self._routes = routes or {}

    def open(self, req, timeout=None):
        if req.data is None:
            # A GET, e.g. model auto-detection via /v1/models.
            self.gets.append(req.full_url)
            for suffix, payload in self._routes.items():
                if req.full_url.endswith(suffix):
                    return FakeResponse(payload)
            return FakeResponse({})
        self.calls.append(
            (req.full_url, json.loads(req.data.decode("utf-8")), timeout)
        )
        return FakeResponse(self._payloads.pop(0))


def patched_build_opener(opener, seen):
    def fake(handler):
        seen["handler"] = handler
        return opener

    return fake


class TestUnixSocketHandler(unittest.TestCase):
    def test_suppresses_default_tcp_handler(self):
        opener = unixsocket.build_opener("/tmp/prokrustes-test.sock")
        handlers = opener.handlers
        ours = [h for h in handlers
                if isinstance(h, unixsocket.UnixSocketHandler)]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0].unix_socket, "/tmp/prokrustes-test.sock")
        tcp = [h for h in handlers if type(h) is urllib.request.HTTPHandler]
        self.assertEqual(tcp, [])

    def test_connection_dials_unix_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.sock")
            server = stdsocket.socket(stdsocket.AF_UNIX, stdsocket.SOCK_STREAM)
            try:
                server.bind(path)
                server.listen(1)
                conn = unixsocket._UnixSocketHTTPConnection(
                    "example.com", unix_socket=path, timeout=5)
                try:
                    conn.connect()
                    self.assertEqual(conn.sock.family, stdsocket.AF_UNIX)
                finally:
                    conn.close()
            finally:
                server.close()

    def test_connection_failure_is_an_os_error(self):
        conn = unixsocket._UnixSocketHTTPConnection(
            "example.com",
            unix_socket="/nonexistent-dir-prokrustes/nope.sock",
            timeout=2,
        )
        with self.assertRaises(OSError):
            conn.connect()


class TestPostJsonOverSocket(unittest.TestCase):
    def _run(self, payloads, base="http://127.0.0.1:8080", path="/completion",
             url_payload=None, timeout=5.0, unix_socket="/tmp/llama.sock"):
        import prokrustes.cli as cli

        opener = FakeOpener(payloads)
        seen = {}
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, seen)
        try:
            result = cli._post_json(base, path, url_payload or {}, timeout,
                                    "missing-endpoint-hint", unix_socket)
        finally:
            urllib.request.build_opener = original
        return opener, seen, result

    def test_posts_through_socket_opener(self):
        opener, seen, result = self._run(
            [{"content": "x"}],
            url_payload={"prompt": "p", "grammar": "g"},
        )
        self.assertEqual(result, {"content": "x"})
        self.assertIsInstance(seen["handler"], unixsocket.UnixSocketHandler)
        self.assertEqual(seen["handler"].unix_socket, "/tmp/llama.sock")
        self.assertEqual(len(opener.calls), 1)
        url, payload, timeout = opener.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8080/completion")
        self.assertEqual(payload, {"prompt": "p", "grammar": "g"})
        self.assertEqual(timeout, 5.0)

    def test_socket_404_reports_endpoint(self):
        import prokrustes.cli as cli

        def raising_open(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 404, "not found", {}, io.BytesIO(b"nope"))

        opener = FakeOpener([])
        opener.open = raising_open
        seen = {}
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, seen)
        try:
            with self.assertRaises(SystemExit) as ctx:
                cli._post_json("http://127.0.0.1:8080", "/completion", {},
                               5.0, "missing-endpoint-hint", "/tmp/llama.sock")
        finally:
            urllib.request.build_opener = original
        self.assertIn("404", str(ctx.exception))
        self.assertIn("missing-endpoint-hint", str(ctx.exception))

    def test_socket_error_names_socket_not_tcp_url(self):
        import prokrustes.cli as cli

        def raising_open(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 500, "boom", {}, io.BytesIO(b'{"error": "x"}'))

        opener = FakeOpener([])
        opener.open = raising_open
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, {})
        try:
            with self.assertRaises(SystemExit) as ctx:
                cli._post_json("http://127.0.0.1:8080", "/apply-template", {},
                               5.0, "hint", "/tmp/llama.sock")
        finally:
            urllib.request.build_opener = original
        message = str(ctx.exception)
        self.assertIn("POST /apply-template via /tmp/llama.sock", message)
        self.assertIn("500", message)
        self.assertNotIn("127.0.0.1:8080", message)

    def test_socket_unreachable_names_socket(self):
        import prokrustes.cli as cli

        def raising_open(req, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        opener = FakeOpener([])
        opener.open = raising_open
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, {})
        try:
            with self.assertRaises(SystemExit) as ctx:
                cli._post_json("http://127.0.0.1:8080", "/completion", {},
                               5.0, "hint", "/tmp/llama.sock")
        finally:
            urllib.request.build_opener = original
        self.assertIn("/tmp/llama.sock", str(ctx.exception))

    def test_socket_rejects_non_http_url(self):
        import prokrustes.cli as cli

        with self.assertRaises(SystemExit) as ctx:
            cli.post_completion("https://example.com/x", {"prompt": "p"},
                                5.0, "/tmp/llama.sock")
        self.assertIn("http://", str(ctx.exception))


class TestFillOverSocket(unittest.TestCase):
    def _fill(self, source, payloads, chat, unix_socket="/tmp/llama.sock",
              model="test-model", routes=None):
        import prokrustes.cli as cli
        from argparse import Namespace

        compiled = cli.compile_template(source, use_prefill=False)
        opener = FakeOpener(payloads, routes)
        seen = {}
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, seen)
        try:
            args = Namespace(chat=chat, template_in_prompt=True,
                             no_prompt=False, unix_socket=unix_socket,
                             model=model,
                             n_predict=16, temperature=0.0, top_k=1,
                             top_p=1.0, seed=7, dynatemp=0.0, stop=[],
                             timeout=5.0)
            content = cli.fill("http://127.0.0.1:8080/", compiled, args)
        finally:
            urllib.request.build_opener = original
        return compiled, opener, seen, content

    def test_raw_fill_threads_unix_socket(self):
        import prokrustes.cli as cli

        compiled, opener, seen, content = self._fill(
            "a={v:/x/}", [{"content": "a=x", "stop_type": "eos"}], False)
        self.assertEqual(content, "a=x")
        self.assertIsInstance(seen["handler"], unixsocket.UnixSocketHandler)
        self.assertEqual(len(opener.calls), 1)
        url, payload, _ = opener.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8080/completion")
        self.assertEqual(payload["prompt"], cli.build_prompt(compiled))
        self.assertEqual(payload["grammar"], compiled.grammar)

    def test_chat_fill_threads_unix_socket_for_both_calls(self):
        import prokrustes.cli as cli

        rendered = "<CHAT>...<|im_start|>assistant\n"
        compiled, opener, seen, content = self._fill(
            "a={v:/x/}",
            [{"prompt": rendered}, {"content": "a=x", "stop_type": "eos"}],
            True,
        )
        self.assertEqual(content, "a=x")
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(opener.calls[0][0],
                         "http://127.0.0.1:8080/apply-template")
        self.assertEqual(opener.calls[0][1],
                         {"messages": cli.build_chat_messages(compiled),
                          "model": "test-model"})
        self.assertEqual(opener.calls[1][0],
                         "http://127.0.0.1:8080/completion")
        self.assertEqual(opener.calls[1][1]["prompt"],
                         rendered + compiled.prefill)


class TestServerModel(unittest.TestCase):
    def _detect(self, routes, base="http://127.0.0.1:8080",
                unix_socket="/tmp/llama.sock"):
        import prokrustes.cli as cli

        opener = FakeOpener([], routes)
        seen = {}
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, seen)
        try:
            model = cli.server_model(base, 5.0, unix_socket)
        finally:
            urllib.request.build_opener = original
        return opener, seen, model

    def test_returns_first_model_id(self):
        opener, seen, model = self._detect(
            {"/v1/models": {"data": [{"id": "qwen3"}, {"id": "other"}]}})
        self.assertEqual(model, "qwen3")
        self.assertEqual(opener.gets, ["http://127.0.0.1:8080/v1/models"])
        self.assertIsInstance(seen["handler"], unixsocket.UnixSocketHandler)

    def test_none_when_endpoint_missing(self):
        # Old servers have no /v1/models; fall back to sending no model.
        _, _, model = self._detect({})
        self.assertIsNone(model)

    def test_none_on_empty_or_malformed_list(self):
        for body in ({"data": []}, {"data": [{"id": 42}]}, {"other": 1}):
            _, _, model = self._detect({"/v1/models": body})
            self.assertIsNone(model)


class TestApplyTemplateModel(unittest.TestCase):
    def _apply(self, model):
        import prokrustes.cli as cli

        opener = FakeOpener([{"prompt": "<rendered>"}])
        seen = {}
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, seen)
        try:
            prompt = cli.apply_chat_template(
                "http://127.0.0.1:8080", [{"role": "user", "content": "x"}],
                5.0, "/tmp/llama.sock", model)
        finally:
            urllib.request.build_opener = original
        return opener, prompt

    def test_model_included_when_known(self):
        opener, prompt = self._apply("qwen3")
        self.assertEqual(prompt, "<rendered>")
        self.assertEqual(opener.calls[0][1],
                         {"messages": [{"role": "user", "content": "x"}],
                          "model": "qwen3"})

    def test_model_omitted_when_unknown(self):
        opener, _ = self._apply(None)
        self.assertEqual(opener.calls[0][1],
                         {"messages": [{"role": "user", "content": "x"}]})


class TestFillModelDiscovery(unittest.TestCase):
    def test_explicit_model_skips_detection(self):
        import prokrustes.cli as cli

        rendered = "<CHAT>assistant\n"
        compiled = cli.compile_template("a={v:/x/}", use_prefill=False)
        opener = FakeOpener(
            [{"prompt": rendered}, {"content": "a=x", "stop_type": "eos"}])
        seen = {}
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, seen)
        try:
            from argparse import Namespace
            args = Namespace(chat=True, template_in_prompt=True,
                             no_prompt=False, unix_socket="/tmp/llama.sock",
                             model="explicit",
                             n_predict=16, temperature=0.0, top_k=1,
                             top_p=1.0, seed=7, dynatemp=0.0, stop=[],
                             timeout=5.0)
            content = cli.fill("http://127.0.0.1:8080/", compiled, args)
        finally:
            urllib.request.build_opener = original
        self.assertEqual(content, "a=x")
        self.assertEqual(opener.gets, [])
        self.assertEqual(opener.calls[0][1]["model"], "explicit")
        self.assertEqual(opener.calls[1][1]["model"], "explicit")

    def test_auto_detected_model_used_everywhere(self):
        import prokrustes.cli as cli

        rendered = "<CHAT>assistant\n"
        compiled = cli.compile_template("a={v:/x/}", use_prefill=False)
        opener = FakeOpener(
            [{"prompt": rendered}, {"content": "a=x", "stop_type": "eos"}],
            {"/v1/models": {"data": [{"id": "auto"}]}})
        seen = {}
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, seen)
        try:
            from argparse import Namespace
            args = Namespace(chat=True, template_in_prompt=True,
                             no_prompt=False, unix_socket="/tmp/llama.sock",
                             model=None,
                             n_predict=16, temperature=0.0, top_k=1,
                             top_p=1.0, seed=7, dynatemp=0.0, stop=[],
                             timeout=5.0)
            content = cli.fill("http://127.0.0.1:8080/", compiled, args)
        finally:
            urllib.request.build_opener = original
        self.assertEqual(content, "a=x")
        self.assertEqual(opener.gets, ["http://127.0.0.1:8080/v1/models"])
        self.assertEqual(opener.calls[0][1]["model"], "auto")
        self.assertEqual(opener.calls[1][1]["model"], "auto")

    def test_old_server_without_models_endpoint(self):
        import prokrustes.cli as cli

        rendered = "<CHAT>assistant\n"
        compiled = cli.compile_template("a={v:/x/}", use_prefill=False)
        # No /v1/models route: the GET falls back to {}, i.e. an old server.
        opener = FakeOpener(
            [{"prompt": rendered}, {"content": "a=x", "stop_type": "eos"}])
        original = urllib.request.build_opener
        urllib.request.build_opener = patched_build_opener(opener, {})
        try:
            from argparse import Namespace
            args = Namespace(chat=True, template_in_prompt=True,
                             no_prompt=False, unix_socket="/tmp/llama.sock",
                             model=None,
                             n_predict=16, temperature=0.0, top_k=1,
                             top_p=1.0, seed=7, dynatemp=0.0, stop=[],
                             timeout=5.0)
            content = cli.fill("http://127.0.0.1:8080/", compiled, args)
        finally:
            urllib.request.build_opener = original
        self.assertEqual(content, "a=x")
        self.assertEqual(opener.gets, ["http://127.0.0.1:8080/v1/models"])
        self.assertNotIn("model", opener.calls[0][1])
        self.assertNotIn("model", opener.calls[1][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
