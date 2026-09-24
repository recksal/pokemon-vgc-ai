"""Small HTTP boundary for the Champions post-game analyzer.

The service deliberately accepts saved player-view decision bundles only. It does not
log bundle contents, handle Showdown credentials, or capture live ladder games.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from vgc.game_analyzer import analyze_decision_bundle

MAX_BODY_BYTES = 8 * 1024 * 1024
Analyzer = Callable[..., Awaitable[dict[str, object]]]


def normalize_request(payload: object) -> tuple[dict[str, object], int]:
    """Accept either a raw replay bundle or {"bundle": ..., "top_k": ...}."""

    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    raw_bundle = payload.get("bundle", payload)
    if not isinstance(raw_bundle, dict):
        raise ValueError("bundle must be a JSON object")
    top_k_value = payload.get("top_k", 3)
    if isinstance(top_k_value, bool) or not isinstance(top_k_value, int):
        raise ValueError("top_k must be an integer")
    if not 1 <= top_k_value <= 10:
        raise ValueError("top_k must be between 1 and 10")
    if raw_bundle.get("schema") != "vgc-decision-replay-v1":
        raise ValueError("expected a vgc-decision-replay-v1 bundle")
    return raw_bundle, top_k_value


async def analyze_payload(
    payload: object,
    *,
    analyzer: Analyzer = analyze_decision_bundle,
) -> dict[str, object]:
    bundle, top_k = normalize_request(payload)
    return await analyzer(bundle, top_k=top_k)


def authorized(header: str | None, expected_token: str | None) -> bool:
    if not expected_token:
        return True
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:], expected_token)


class AnalyzerHandler(BaseHTTPRequestHandler):
    server_version = "ChampionsAnalyzer/1"

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._json(
            HTTPStatus.OK,
            {
                "status": "ok",
                "service": "pokemon-vgc-ai-game-analyzer",
                "input_schema": "vgc-decision-replay-v1",
                "output_schema": "vgc-game-analysis-v1",
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/analyze":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not authorized(
            self.headers.get("authorization"),
            os.environ.get("ANALYZER_API_TOKEN"),
        ):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            content_length = int(self.headers.get("content-length", "0"))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content-length"})
            return
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": f"body must be between 1 and {MAX_BODY_BYTES} bytes"},
            )
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
            report = asyncio.run(analyze_payload(payload))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except Exception:
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "analysis failed; inspect service logs for the request error"},
            )
            return
        self._json(HTTPStatus.OK, {"analysis": report})

    def log_message(self, format_string: str, *args: Any) -> None:
        # Preserve normal request metadata without ever logging replay payloads.
        super().log_message(format_string, *args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the Champions game analyzer over HTTP.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AnalyzerHandler)
    print(f"Champions analyzer listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
