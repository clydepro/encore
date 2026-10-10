"""Driving the appliance over ASGI, without a socket and without a browser.

`httpx.AsyncClient` on an `ASGITransport` rather than FastAPI's `TestClient`, for two reasons
that are both about this project rather than about style:

* `TestClient` is built on a synchronous shim that current Starlette deprecates on import,
  and the suite runs with `filterwarnings = error` (AEP 14: a warning in a test run is a
  finding). The async client is the supported path.
* SAPRS 9.10's subject is a response that stays open, and `ASGITransport` buffers a whole
  body before it returns one — `body_parts = []`, in its own source. A held-open stream never
  returns from it at all, so the streaming half of this file is `open_stream`: the same ASGI
  contract, driven by hand, with a disconnect the test decides when to send.

The lifespan is entered by hand, because `ASGITransport` does not run it. That is not a gap
to work around: it is the reason the composition root's startup and shutdown live in one
place (`encore/api/app.py`) instead of in a server hook, so the same code runs here as under
uvicorn (ADR-012). Pass `lifespan=False` for a second client against an app already started —
starting the same appliance twice would be a different program from the one that ships.
"""

from __future__ import annotations

import asyncio
import contextlib
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI


@asynccontextmanager
async def running(
    app: FastAPI, *, lifespan: bool = True, **client_kwargs: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """The app, started, behind one client. Exits through the same lifespan it entered."""

    # `raise_app_exceptions=False`, which is what `TestClient`'s
    # `raise_server_exceptions=False` means: Starlette's outermost middleware generates the
    # 500 response and then re-raises so the *server* can log it, and a transport that
    # re-raised here would hand the test a traceback instead of the answer it is checking.
    # The logging half is asserted separately, with `caplog`.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    if lifespan:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://encore.test", **client_kwargs
            ) as client,
        ):
            yield client
    else:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://encore.test", **client_kwargs
        ) as client:
            yield client


@asynccontextmanager
async def started(app: FastAPI, **client_kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
    """The app, running, with a client for the requests that are not the stream.

    A held-open response and an ordinary request cannot share a transport here — one is
    driven by hand, the other by `httpx` — so they share a lifespan instead. Entering it twice
    would start the appliance twice, which is a different program from the one that ships.
    """

    async with (
        app.router.lifespan_context(app),
        running(app, lifespan=False, **client_kwargs) as client,
    ):
        yield client


class Stream:
    """A held-open ASGI response, read a frame at a time, then closed on purpose.

    The client `ASGITransport` cannot be: this scope is run by hand, one `send` at a time, so
    a response that never finishes is a response a test can still read. The other half is the
    disconnect — a real browser's socket dies at a moment the test chooses, and `close()` lets
    a test choose it, rather than inheriting whatever a transport's buffering decides about
    when the app may notice.
    """

    def __init__(self, app: Any, path: str, *, headers: dict[str, str] | None = None) -> None:
        self._sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._receive_done = False
        self._buffer = ""
        self._event = ""
        self._payload: list[str] = []
        # Set while the test is reading, clear while it is not. This is the socket buffer:
        # nothing in `Stream` invents backpressure, it just lets a test decline to drain.
        self._drain = asyncio.Event()
        self._drain.set()
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path.split("?", maxsplit=1)[0],
            "raw_path": path.split("?", maxsplit=1)[0].encode(),
            "query_string": path.partition("?")[2].encode(),
            "root_path": "",
            "client": ("127.0.0.1", 54321),
            "server": ("encore.test", 80),
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in (headers or {"accept": "text/event-stream"}).items()
            ],
        }
        self._task = asyncio.create_task(self._run(app, scope))

    async def _run(self, app: Any, scope: dict[str, Any]) -> None:
        try:
            await app(scope, self._receive, self._send)
        except asyncio.CancelledError:  # pragma: no cover - the server hung up on us
            raise
        except Exception:
            traceback.print_exc()
        finally:
            self._sent.put_nowait({"type": "sentinel"})

    async def _receive(self) -> dict[str, Any]:
        if not self._receive_done:
            self._receive_done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    def pause(self) -> None:
        """Stop taking what the app sends, the way a sleeping radio stops a TCP window.

        The generator blocks in its `send`, which is the only way a *client that has not
        disconnected* can fall behind — and falling behind is what the publisher's bound
        exists for (SAPRS 9.10).
        """

        self._drain.clear()

    async def resume(self) -> None:
        self._drain.set()

    async def _send(self, message: dict[str, Any]) -> None:
        await asyncio.wait_for(self._drain.wait(), timeout=10.0)
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {
                key.decode().lower(): value.decode() for key, value in message["headers"]
            }
        self._sent.put_nowait(message)

    async def next(self, count: int = 1, *, within: float = 3.0) -> list[tuple[str, str]]:
        """The next `count` frames, in the order they were sent.

        `within` rather than a timeout on the caller's side, because a frame that never arrives
        is the failure every one of these tests is about, and a test that hangs instead of
        failing is a worse report of it.
        """

        frames: list[tuple[str, str]] = []
        while len(frames) < count:
            if "\n\n" in self._buffer:  # a frame ends at a blank line, on the wire
                block, self._buffer = _split(self._buffer)
                frames.append(_frame(block))
                continue
            try:
                message = await self._read(within)
            except AssertionError:
                # A quiet stream is an answer, once something arrived: the frames collected
                # are what there was, and raising here would turn "the appliance sent two
                # frames and then nothing" into a timeout nobody can read.
                if frames:
                    return frames
                raise
            if message["type"] == "sentinel":
                return frames
            self._buffer += message.get("body", b"").replace(b"\r\n", b"\n").decode()
        return frames

    async def _read(self, within: float) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._sent.get(), within)
        except TimeoutError as exc:  # pragma: no cover - the message that never came
            raise AssertionError("no SSE frame within the timeout") from exc

    async def until(self, name: str, *, limit: int = 40) -> tuple[str, str]:
        """The next frame called `name`, skipping the progress ticks that land first.

        A tick arrives once a second whether or not anything interesting happened, and a test
        that asserted on positions would be flaky by construction.
        """

        seen: list[str] = []
        while len(seen) < limit:
            event, data = await self._one()
            if event == name:
                return event, data
            if event != "progress":
                seen.append(event)
        raise AssertionError(f"never saw {name!r}; got {seen}")

    async def names(self, name: str, *, limit: int = 40) -> tuple[list[str], str]:
        """Every frame name up to and including `name`, and that frame's data.

        `until` answers "did it happen"; this answers "in what order", which is the question a
        journey asks — a `SongStarted` that arrived before its `QueueAdvanced` is a room that
        showed the wrong track for a moment, and only the sequence can tell. Progress ticks are
        excluded for the same reason `until` excludes them: they say nothing about the fact
        under test and arrive on a timer.
        """

        seen: list[str] = []
        while len(seen) < limit:
            event, data = await self._one()
            if event == "progress":
                continue
            seen.append(event)
            if event == name:
                return seen, data
        raise AssertionError(f"never saw {name!r}; got {seen}")

    async def _one(self) -> tuple[str, str]:
        got = await self.next(1)
        if not got:
            raise AssertionError("the response ended before the frame arrived")
        return got[0]

    async def close(self) -> None:
        """Hang up, and wait for the appliance to notice.

        The waiting is the assertion's premise: a generator that never sees the disconnect
        leaves its `Stream` registered, and a test that returned first would pass on whatever
        cleanup happened to have run.
        """

        self._drain.set()
        self._disconnect.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._task, 3.0)
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


def _split(buffer: str) -> tuple[str, str]:
    index = buffer.find("\n\n")
    return buffer[:index], buffer[index + 2 :]


def _frame(block: str) -> tuple[str, str]:
    """One wire frame, as (event, data), with multi-line data rejoined.

    Every HTML fragment arrives as several `data:` lines; a browser puts the newlines back, and
    an assertion about markup should see what a browser sees.
    """

    event, payload = "message", []
    for line in block.split("\n"):
        if line.startswith(":"):
            continue  # a comment, including `sse-starlette`'s keep-alive pings
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            payload.append(line[len("data:") :].lstrip())
    return event, "\n".join(payload)


@asynccontextmanager
async def held_open(app: FastAPI, path: str = "/events", **kwargs: Any) -> AsyncIterator[Stream]:
    """`Stream`, with the lifespan around it and the hang-up after it."""

    async with app.router.lifespan_context(app):
        made = Stream(app, path, **kwargs)
        try:
            yield made
        finally:
            await made.close()


async def frames(stream: Stream, count: int = 1) -> list[tuple[str, str]]:
    """The next `count` frames of a held-open response, as (event, data) pairs."""

    return await stream.next(count)
