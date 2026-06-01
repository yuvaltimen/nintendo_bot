"""HTTP client for the Pi-side daemon. Stdlib only - runs on Mac or Pi."""

import json
import re
import time
import urllib.error
import urllib.request


def scrub_macro(script: str) -> str:
    """Strip inline # comments, comment-only lines, and Unicode dash variants.

    Two classes of problem are fixed:

    1. Inline # comments — nxbt's tokenizer has no comment syntax.  "Y 0.1s  #
       attack" produces unknown tokens "#" and "attack", crashing the nxbt worker.

    2. Unicode dashes — LLMs sometimes generate en-dashes (–, U+2013), em-dashes
       (—, U+2014), or minus signs (−, U+2212) inside stick coordinates instead of
       ASCII hyphens.  "L_STICK@+000–100" is an invalid token; normalising to
       "L_STICK@+000-100" fixes it silently.

    Applied automatically inside RemotePad.macro() so all callers are protected.
    """
    # Unicode dash → ASCII hyphen (must run before comment stripping)
    script = (
        script
        .replace('–', '-')   # en-dash
        .replace('—', '-')   # em-dash
        .replace('−', '-')   # minus sign
    )
    cleaned_lines = []
    for line in script.splitlines():
        clean = re.sub(r'\s*#.*$', '', line)
        if clean.strip():
            cleaned_lines.append(clean)
    return '\n'.join(cleaned_lines)


# Buttons that indicate timing-sensitive or combat/interaction actions.
# Macros containing any of these should NOT be auto-extended.
_COMBAT_TOKENS = re.compile(
    r'\b(Y|X|A|ZL|ZR|PLUS|MINUS|DPAD_UP|DPAD_DOWN|DPAD_LEFT|DPAD_RIGHT'
    r'|L_STICK_PRESS|R_STICK_PRESS)\b'
)
_DURATION_RE = re.compile(r'(\d+(?:\.\d+)?)s')
_BARE_DURATION_LINE = re.compile(r'^\s*\d+(?:\.\d+)?s\s*$')


def extend_macro_to_interval(macro: str, interval: float, buffer: float = 0.3) -> str:
    """Extend a pure-movement macro to fill the agent's decision interval.

    Between LLM calls the controller goes silent, leaving Link standing still.
    This function appends one extra line that repeats the last movement inputs
    for however long is needed to bring the total macro duration close to
    `interval - buffer` seconds.

    Macros that contain combat or interaction buttons (Y, A, X, ZL, ZR, DPAD,
    etc.) are returned unchanged — those sequences have frame-timing constraints
    that must not be padded.

    Args:
        macro:    Macro string, already scrubbed.
        interval: Agent decision interval in seconds.
        buffer:   Headroom left before the next LLM call (default 0.3 s).

    Example:
        extend_macro_to_interval("L_STICK@+000+100 B 1.5s", 5.0)
        # → "L_STICK@+000+100 B 1.5s\\nL_STICK@+000+100 B 3.2s"
    """
    if not macro or _COMBAT_TOKENS.search(macro):
        return macro

    target = interval - buffer
    current = sum(float(d) for d in _DURATION_RE.findall(macro))
    remaining = round(target - current, 2)

    if remaining < 0.2:
        return macro

    # Find the last line that has real input tokens (not a bare pause duration).
    last_input_line = None
    for line in reversed(macro.splitlines()):
        if line.strip() and not _BARE_DURATION_LINE.match(line):
            last_input_line = line
            break

    if last_input_line is None:
        return macro

    # Strip the trailing duration from that line to get the bare input tokens.
    base = re.sub(r'\s+\d+(?:\.\d+)?s\s*$', '', last_input_line).strip()
    if not base:
        return macro

    return macro.rstrip() + f'\n{base} {remaining}s'


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
            "connecting": " - Switch is likely on Change Grip/Order; press A on a paired joycon to dismiss it",
            "reconnecting": " - nxbt is reconnecting; usually resolves in ~10s",
            "crashed": " - try pad.reconnect()",
            "unpaired": " - no Switch ever paired; put Switch on Change Grip/Order and call pad.pair_fresh()",
            "unreachable": " - could not reach the daemon at all; is it running?",
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
        script = scrub_macro(script)
        if not script:
            # Nothing left after stripping comments — skip the HTTP call entirely.
            return {"ok": True, "skipped": "empty after comment scrub"}
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
