"""HTTP over a Unix domain socket, curl --unix-socket style.

The standard library has no Unix-socket transport for urllib, so this module
provides one: an ``http.client.HTTPConnection`` that dials ``AF_UNIX`` instead
of TCP, and a ``urllib.request`` handler that routes ``http://`` requests
through it. Only ``http://`` URLs are supported; TLS over a socket is
meaningless for a local llama-server and is refused with a clear error.
"""

from __future__ import annotations

import functools
import http.client
import socket
import urllib.request


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """An HTTPConnection that ignores host/port and dials a socket path."""

    def __init__(self, host: str, unix_socket: str,
                 timeout: float | None = None):
        # None means blocking, matching the global default; every caller in
        # this package passes an explicit timeout, so this only matters for
        # direct construction.
        super().__init__(host, timeout=timeout)
        self._socket_path = unix_socket

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self._socket_path)
        except Exception:
            sock.close()
            raise
        self.sock = sock


class UnixSocketHandler(urllib.request.HTTPHandler):
    """Route http:// requests through a Unix domain socket.

    Passing an instance of this handler to ``urllib.request.build_opener``
    suppresses the default TCP ``HTTPHandler``, so the socket is the only
    transport the resulting opener will use for ``http://`` URLs.
    """

    def __init__(self, unix_socket: str):
        super().__init__()
        self._unix_socket = unix_socket

    @property
    def unix_socket(self) -> str:
        return self._unix_socket

    def http_open(self, req):
        return self.do_open(
            functools.partial(_UnixSocketHTTPConnection,
                              unix_socket=self._unix_socket),
            req,
        )


def build_opener(unix_socket: str) -> urllib.request.OpenerDirector:
    """An opener whose http:// transport is the given Unix socket."""
    return urllib.request.build_opener(UnixSocketHandler(unix_socket))
