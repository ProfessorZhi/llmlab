"""Real-loopback integration tests for the local admin preview entry.

Everything in this package drives the **real** preview WSGI app over a real TCP
socket on ``127.0.0.1`` (see ``dev/preview_auth.py``), so the pieces that unit
tests cannot reach are covered: the WSGI container, the cookie actually coming
back from the client, the origin/CSRF guards as a browser triggers them, and the
absence of a hang on a bodyless ``GET``.

Nothing here reaches the network: no Feishu is configured by default, and the
optional mock provider talks only to this same loopback server.
"""
