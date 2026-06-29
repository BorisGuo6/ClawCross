#!/usr/bin/env python3
"""Forward a Tailscale-facing local port to the loopback-only mihomo port."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import socket
import subprocess
import sys


def log(message: str) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    print(f"{now} {message}", flush=True)


def get_tailscale_ipv4(tailscale_cli: str) -> str | None:
    try:
        proc = subprocess.run(
            [tailscale_cli, "ip", "-4"],
            capture_output=True,
            check=False,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        log(f"tailscale ip failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"tailscale ip failed rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()}")
        return None
    for line in proc.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        await close_writer(writer)


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    peer = client_writer.get_extra_info("peername")
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(target_host, target_port)
    except Exception as exc:
        log(f"upstream connect failed peer={peer}: {exc}")
        await close_writer(client_writer)
        return

    await asyncio.gather(
        pipe(client_reader, upstream_writer),
        pipe(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def serve(args: argparse.Namespace) -> None:
    while True:
        bind_host = args.bind_host or get_tailscale_ipv4(args.tailscale_cli)
        if not bind_host:
            log("no Tailscale IPv4 available; retrying")
            await asyncio.sleep(args.retry_seconds)
            continue

        try:
            server = await asyncio.start_server(
                lambda r, w: handle_client(r, w, args.target_host, args.target_port),
                host=bind_host,
                port=args.listen_port,
                family=socket.AF_INET,
                reuse_address=True,
            )
        except OSError as exc:
            log(f"listen failed {bind_host}:{args.listen_port}: {exc}; retrying")
            await asyncio.sleep(args.retry_seconds)
            continue

        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        log(f"listening {sockets} -> {args.target_host}:{args.target_port}")
        async with server:
            try:
                await server.serve_forever()
            except asyncio.CancelledError:
                raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind-host", default="")
    parser.add_argument("--listen-port", type=int, default=7993)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=7993)
    parser.add_argument("--tailscale-cli", default="/usr/local/bin/tailscale")
    parser.add_argument("--retry-seconds", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    try:
        asyncio.run(serve(parse_args()))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
