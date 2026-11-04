"""Fixed reads and a proposal-only orchestrator connection. No device commands."""

import json
import re
from urllib.parse import urlsplit

import anyio
import httpx

from labkit.auth import loopback_name, valid_token
from labkit.lease import validate_client
from orchestrator.app import Catalog

SERVICES = ("robot", "stage", "thermal", "vision", "furnace", "microscope")
MAX_RESPONSE = 1048576
READ_TIMEOUT = 3


class Backend:
    def __init__(
        self, store, recipes, *, endpoints=None, proposal=None, demo=False, transport=None
    ):
        self.store, self.catalog, self.demo = store, Catalog(recipes), demo
        self.endpoints = dict(endpoints or {})
        self.proposal = proposal
        if proposal:
            url, token, caller = proposal
            parts = urlsplit(url)
            if (
                parts.scheme != "http"
                or not loopback_name(parts.hostname or "")
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or "\\" in url
                or any(ord(char) < 33 or ord(char) == 127 for char in url)
                or any(part in {".", ".."} for part in parts.path.split("/"))
                or "%" in parts.path
                or "//" in parts.path
                or not isinstance(token, str)
                or len(token) < 24
                or not valid_token(token)
            ):
                raise ValueError(
                    "Proposal URL requires direct HTTP loopback and a gateway credential."
                )
            _ = parts.port
            validate_client(caller)
        if set(self.endpoints) - set(SERVICES):
            raise ValueError("Unknown configured service.")
        for url, token in self.endpoints.values():
            parts = urlsplit(url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or not isinstance(token, str)
                or not valid_token(token)
            ):
                raise ValueError("Service URLs must be HTTP(S) base paths without credentials.")
            _ = parts.port
        if demo and (self.endpoints or proposal):
            raise ValueError("Demo mode cannot configure service endpoints.")
        self.http = httpx.AsyncClient(
            timeout=2, follow_redirects=False, trust_env=False, transport=transport
        )

    async def close(self):
        # Close HTTP resources only. Never call a device stop/release method.
        await self.http.aclose()

    def _redact(self, value):
        if isinstance(value, dict):
            return {
                key: "[redacted]"
                if any(
                    word in key.lower() for word in ("token", "password", "authorization", "secret")
                )
                else self._redact(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self._redact(child) for child in value]
        if isinstance(value, str):
            for _, token in self.endpoints.values():
                if token:
                    value = value.replace(token, "[redacted]")
            if self.proposal:
                value = value.replace(self.proposal[1], "[redacted]")
        return value

    async def _read(self, service, path):
        if service not in self.endpoints:
            raise ValueError("Select a configured service from list_services.")
        url, token = self.endpoints[service]
        headers = {"Accept": "application/json", "X-MG400-Panel": "1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with anyio.fail_after(READ_TIMEOUT):
                async with self.http.stream(
                    "GET", url.rstrip("/") + "/api/" + path, headers=headers
                ) as response:
                    if response.status_code != 200:
                        raise ValueError("The service read failed.")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE:
                            raise ValueError("The service response exceeds 1 MiB.")
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise ValueError("The service returned an invalid object.")
                    return result
        except (TimeoutError, httpx.HTTPError, UnicodeError, json.JSONDecodeError):
            raise ValueError("The service read failed.") from None

    async def _describe(self, service):
        result = await self._read(service, "describe")
        if type(result.get("contract")) is not int or result["contract"] != 1:
            raise ValueError("The service does not support contract version 1.")
        return result

    async def _approval(self, name, args):
        if self.proposal is None:
            raise ValueError("Proposal access is not configured.")
        url, token, caller = self.proposal
        path, method, body = "/api/proposals", "POST", args
        if name == "get_approval":
            if not isinstance(args["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", args["id"]):
                raise ValueError("Invalid proposal ID.")
            path += "/" + args["id"]
            method, body = "GET", None
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "X-MG400-Client": caller,
        }
        try:
            with anyio.fail_after(READ_TIMEOUT):
                async with self.http.stream(
                    method, url.rstrip("/") + path, headers=headers, json=body
                ) as response:
                    if response.status_code not in {200, 201}:
                        raise ValueError("Proposal access failed.")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE:
                            raise ValueError("The proposal response exceeds 1 MiB.")
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise ValueError("The proposal response is invalid.")
                    return result
        except (TimeoutError, httpx.HTTPError, UnicodeError, json.JSONDecodeError):
            raise ValueError("Proposal access failed.") from None

    async def call(self, name, args):
        return self._redact(await self._call(name, args))

    async def _call(self, name, args):
        if name in {"propose_run", "get_approval"}:
            return await self._approval(name, args)
        if name == "list_services":
            if self.demo:
                return {"services": [{"id": "demo", "demo": True, "read_only": True}]}
            result = []
            for service in self.endpoints:
                try:
                    description = await self._describe(service)
                    result.append({"id": service, "available": True, "description": description})
                except ValueError:
                    result.append({"id": service, "available": False})
            return {"services": result}
        if name == "get_state":
            if self.demo:
                if args["service"] != "demo":
                    raise ValueError("Select demo from list_services.")
                return {"service": "demo", "demo": True, "connected": False, "read_only": True}
            await self._describe(args["service"])
            return await self._read(args["service"], "state")
        if name == "list_recipes":
            return {"recipes": await anyio.to_thread.run_sync(self.catalog.list)}
        if name == "describe_recipe":
            return await anyio.to_thread.run_sync(self.catalog.read, args["id"])
        if name == "list_runs":
            return {"runs": await anyio.to_thread.run_sync(lambda: self.store.runs(**args))}
        if name == "get_run":
            return await anyio.to_thread.run_sync(
                lambda: self.store.get(args["id"], item_limit=args.get("item_limit", 100))
            )
        if name == "get_chip":

            def chip():
                rows = self.store.runs(
                    chip_id=args["id"], limit=args.get("limit", 100), offset=args.get("offset", 0)
                )
                records = []
                for row in rows:
                    run = self.store.get(row["id"], item_limit=100, chip_id=args["id"])
                    records.append(
                        {
                            "run": row,
                            **{
                                table: [
                                    item for item in run[table] if item["chip_id"] == args["id"]
                                ]
                                for table in ("steps", "samples", "artifacts", "events")
                            },
                            "truncated": run["truncated"],
                        }
                    )
                return {"chip_id": args["id"], "records": records}

            return await anyio.to_thread.run_sync(chip)
        raise ValueError("Unknown gateway tool.")


def configured_endpoints(settings):
    """Reuse the explicit orchestrator service URLs. The services need no token."""
    return {
        name: (
            settings[f"MG400_ORCH_{name.upper()}_URL"],
            "",
        )
        for name in SERVICES
        if settings.get(f"MG400_ORCH_{name.upper()}_URL")
    }
