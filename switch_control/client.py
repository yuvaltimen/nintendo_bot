"""HTTP client for the Pi-side daemon. Stdlib only — runs on Mac or Pi."""

import json
import time
import urllib.error
import urllib.request


class Buttons:
    Y = "Y"
    X = "X"
    B = "B"
    A = "A"
    JCL_SR = "JCL_SR"
    JCL_SL = "JCL_SL"
    R = "R"
    ZR = "ZR"
    MINUS = "MINUS"
    PLUS = "PLUS"
    R_STICK_PRESS = "R_STICK_PRESS"
    L_STICK_PRESS = "L_STICK_PRESS"
    HOME = "HOME"
    CAPTURE = "CAPTURE"
    DPAD_DOWN = "DPAD_DOWN"
    DPAD_UP = "DPAD_UP"
    DPAD_RIGHT = "DPAD_RIGHT"
    DPAD_LEFT = "DPAD_LEFT"
    JCR_SR = "JCR_SR"
    JCR_SL = "JCR_SL"
    L = "L"
    ZL = "ZL"


class Sticks:
    LEFT_STICK = "L_STICK"
    RIGHT_STICK = "R_STICK"


class DaemonError(RuntimeError):
    """The daemon returned a non-2xx response."""


class NotConnected(DaemonError):
    """Daemon reports the Switch pad is not connected."""


class RemotePad:
    """Mirrors the local SwitchPad API but goes over HTTP to the Pi daemon.

    The daemon holds the BT connection across many script runs, so iteration
    on the Mac becomes: edit a script, run it locally, no SSH / rsync needed.
    """

    def __init__(self, host: str, port: int = 8765, timeout: float = 180.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    # ----- low-level HTTP -----

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 503:
                raise NotConnected(detail) from None
            raise DaemonError(f"HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise DaemonError(f"could not reach daemon at {self.base}: {e}") from None

    # ----- state -----

    def status(self) -> dict:
        return self._request("GET", "/status")

    def is_connected(self) -> bool:
        try:
            return bool(self.status().get("connected"))
        except DaemonError:
            return False

    def wait_connected(self, timeout: float = 30.0, poll: float = 0.5):
        deadline = time.time() + timeout
        last_state = "unreachable"
        while time.time() < deadline:
            try:
                s = self.status()
                last_state = s.get("state", "unknown")
                if s.get("connected"):
                    return
            except DaemonError:
                pass
            time.sleep(poll)
        hint = {
            "connecting": " — Switch is likely on Change Grip/Order; press A on a paired joycon to dismiss it",
            "reconnecting": " — nxbt is reconnecting; usually resolves in ~10s",
            "crashed": " — try pad.reconnect()",
            "unpaired": " — no Switch ever paired; put Switch on Change Grip/Order and call pad.pair_fresh()",
            "unreachable": " — could not reach the daemon at all; is it running?",
        }.get(last_state, "")
        raise TimeoutError(
            f"daemon did not become connected within {timeout}s (state={last_state}){hint}"
        )

    def pair_fresh(self):
        """Trigger first-time pair. Switch must be on Change Grip/Order."""
        return self._request("POST", "/pair")

    def reconnect(self):
        """Trigger re-pair to known Switch."""
        return self._request("POST", "/reconnect")

    # ----- inputs -----

    def _send(self, path: str, body: dict, retries: int, recover_timeout: float):
        for attempt in range(retries + 1):
            try:
                return self._request("POST", path, body)
            except NotConnected:
                if attempt >= retries:
                    raise
                self.wait_connected(timeout=recover_timeout)

    def press(self, *buttons, hold: float = 0.1, retries: int = 0, recover_timeout: float = 20.0):
        return self._send(
            "/press", {"buttons": list(buttons), "hold": hold}, retries, recover_timeout
        )

    def tilt(self, stick: str, x: int = 0, y: int = 0, duration: float = 0.5,
             retries: int = 0, recover_timeout: float = 20.0):
        return self._send(
            "/tilt", {"stick": stick, "x": x, "y": y, "duration": duration},
            retries, recover_timeout,
        )

    def macro(self, script: str, block: bool = True, timeout: float = 120.0,
              retries: int = 0, recover_timeout: float = 20.0):
        return self._send(
            "/macro", {"script": script, "block": block, "timeout": timeout},
            retries, recover_timeout,
        )

    def stop(self):
        return self._request("POST", "/stop")

    # ----- ergonomics -----

    def wait_for_ready(self, prompt: str = "Press Enter when the script should take over... "):
        try:
            input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()

    def sleep(self, seconds: float):
        time.sleep(seconds)

    # ----- resilient run -----

    def run_resilient(self, fn, *, retries: int = 1, recover_timeout: float = 20.0):
        """Run fn(self), and on NotConnected wait for auto-reconnect and retry."""
        last_err = None
        for attempt in range(retries + 1):
            try:
                return fn(self)
            except NotConnected as e:
                last_err = e
                if attempt < retries:
                    self.wait_connected(timeout=recover_timeout)
                    continue
                raise
        raise DaemonError(str(last_err))
