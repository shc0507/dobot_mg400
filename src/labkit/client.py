"""Standard-library contract client. No request is retried automatically."""

import http.client
import json
import math
import threading
import urllib.error
import urllib.parse
import urllib.request

from .auth import valid_token_for_bind
from .describe import CONTRACT_VERSION
from .errors import ServiceError
from .lease import validate_client


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class ServiceClient:
    def __init__(self, base_url, token, client_id, *, timeout=10):
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected an HTTP URL without credentials, query, or fragment.")
        if not isinstance(token, (str, type(None))) or not valid_token_for_bind(token, "localhost"):
            raise ValueError("A configured token needs at least 24 printable ASCII characters.")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Request timeout must be a finite positive number.")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.client_id = validate_client(client_id)
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())
        self._version_checked = False
        self._version_lock = threading.Lock()

    def _request(self, path, data=None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        encoded = None
        if data is not None:
            encoded = json.dumps(data, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["X-MG400-Client"] = self.client_id
        request = urllib.request.Request(self.base_url + path, data=encoded, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read()).get("error", error.reason)
            except (ValueError, AttributeError, OSError, http.client.HTTPException):
                detail = error.reason
            finally:
                error.close()
            raise ServiceError(error.code, detail) from error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise ServiceError(0, f"Service request failed: {error}") from error
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError) as error:
            raise ServiceError(502, "The service returned invalid JSON.") from error
        if not isinstance(value, dict):
            raise ServiceError(502, "The service response must be a JSON object.")
        return value

    def describe(self):
        result = self._request("api/describe")
        if type(result.get("contract")) is not int or result["contract"] != CONTRACT_VERSION:
            self._version_checked = False
            raise ServiceError(409, "Unsupported service contract version.")
        self._version_checked = True
        return result

    def _call(self, path, data=None):
        with self._version_lock:
            if not self._version_checked:
                self.describe()
        return self._request(path, data)

    def state(self):
        return self._call("api/state")

    def command(self, action, **args):
        return self._call("api/command", {"action": action, **args})

    def stop(self, wait_s=0):
        return self._call("api/stop", {"wait_s": wait_s})

    def heartbeat(self):
        return self._call("api/heartbeat", {})

    def renew(self):
        """Renew an existing lease atomically; never acquire or fall back."""
        return self._call("api/renew", {})

    def release(self):
        return self._call("api/release", {})

    def events(self, since=0):
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError("Event sequence must be a nonnegative integer.")
        return self._call("api/events?" + urllib.parse.urlencode({"since": since}))

    def heartbeat_thread(self, *, interval=1, on_error=None, renew_only=False):
        """Create a helper; renew_only requires an explicitly acquired lease."""
        return Heartbeat(self, interval=interval, on_error=on_error, renew_only=renew_only)


class Heartbeat:
    """Renew a lease until stopped or the first error. Never reacquire after an error."""

    def __init__(self, client, *, interval=1, on_error=None, renew_only=False):
        if type(renew_only) is not bool:
            raise ValueError("renew_only must be true or false.")
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("Heartbeat interval must be a finite positive number.")
        self.client = client
        self.interval = interval
        self.on_error = on_error
        self.renew_only = renew_only
        self.error = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("A heartbeat helper can start only once.")
            self._thread = threading.Thread(target=self._run, name="labkit-heartbeat", daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.renew_only:
                    self.client.renew()
                else:
                    self.client.heartbeat()
            except Exception as error:
                self.error = error
                self._stop.set()
                if self.on_error is not None:
                    self.on_error(error)
                return
            if self._stop.wait(self.interval):
                return

    def stop(self, timeout=None):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
