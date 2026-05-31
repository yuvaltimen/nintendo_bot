"""Long-lived HTTP daemon that holds the BT pad and auto-reconnects on drop.

Runs on the Pi only (imports nxbt + fastapi). The Mac client talks to this over HTTP.

Lifecycle:
  startup → start bluetoothctl D-Bus agent → background pair attempt → serve HTTP
  watchdog → every 5s, no-op heartbeat macro; on failure, reconnect
  shutdown → tear down controller + agent
"""

import logging
import os
import subprocess
import threading
import time
from typing import Optional

import nxbt
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

log = logging.getLogger("switch_control.daemon")

# We poll BlueZ for link state instead of round-tripping a macro through nxbt.
# nxbt has its own internal save_connection recovery; we DO NOT want to race it
# (doing so saturates D-Bus and triggers state=crashed). We reconnect only when
# nxbt explicitly gives up (state=crashed) or when a client calls /reconnect.
HEARTBEAT_INTERVAL_S = 10.0
RECONNECT_BACKOFF_S = 3.0
BLUEZ_INFO_TIMEOUT_S = 2.0
# After a crash/reconnect, check more frequently for a few cycles so that
# an immediate re-crash is caught quickly rather than waiting 10s.
POST_RECONNECT_FAST_S = 3.0
POST_RECONNECT_FAST_CYCLES = 4
# Ignore a second /stop within this window — two rapid clear_macros() calls
# can corrupt the nxbt worker when the BT link is already under stress.
STOP_DEDUP_S = 0.5
# After a reconnect, hold off reporting "connected" for this many seconds.
# The BT link is often physically unstable immediately after nxbt reconnects
# (especially under Wi-Fi/BT antenna contention on Pi 4). Clients polling
# /status will see connected=False during the grace window and keep waiting,
# so their retry fires into a stable link rather than one about to crash again.
POST_CONNECT_GRACE_S = 2.0


class PressRequest(BaseModel):
    buttons: list[str]
    hold: float = 0.1


class TiltRequest(BaseModel):
    stick: str
    x: int = 0
    y: int = 0
    duration: float = 0.5


class MacroRequest(BaseModel):
    script: str
    block: bool = True
    timeout: float = 120.0


class Daemon:
    def __init__(self):
        self.nx = nxbt.Nxbt()
        self.idx: Optional[int] = None
        self.lock = threading.Lock()
        self.agent_proc: Optional[subprocess.Popen] = None
        self.reconnect_count = 0
        self.last_command_at: float = 0.0
        self.last_error: Optional[str] = None
        self.last_heartbeat_ok: bool = False
        self._last_stop_t: float = 0.0
        self._last_connect_at: float = 0.0
        self.started_at: float = time.time()
        self._stop = threading.Event()
        self.app = FastAPI(title="Switch Control Daemon")
        self._register_routes()

    # ----- BT agent -----

    def start_bt_agent(self):
        """Spawn bluetoothctl with the NoInputNoOutput pairing agent.

        Required workaround for newer BlueZ vs nxbt - see PI_CONTROLLER.md §5.7d.
        """
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=0,
        )
        for cmd in ("agent NoInputNoOutput\n", "default-agent\n", "pairable on\n"):
            proc.stdin.write(cmd)
        proc.stdin.flush()
        # Give BlueZ a beat to register the agent on the D-Bus path.
        time.sleep(0.5)
        self.agent_proc = proc
        log.info("bluetoothctl D-Bus agent started (pid=%s)", proc.pid)

    def stop_bt_agent(self):
        if self.agent_proc is not None:
            try:
                self.agent_proc.stdin.write("quit\n")
                self.agent_proc.stdin.flush()
                self.agent_proc.wait(timeout=2)
            except Exception:
                self.agent_proc.kill()
            self.agent_proc = None

    # ----- pad lifecycle -----

    def _state_str(self) -> str:
        if self.idx is None:
            return "unpaired"
        try:
            return self.nx.state[self.idx].get("state", "unknown")
        except Exception:
            return "unknown"

    def _is_connected(self) -> bool:
        if self._state_str() != "connected":
            return False
        # Enforce the post-reconnect grace period. nxbt reports "connected" the
        # instant the L2CAP sockets come up, but the link is often not stable
        # for another second or two — especially on Pi 4 with shared Wi-Fi/BT
        # antenna. Reporting False here causes wait_connected() on the Mac to
        # keep polling until the grace window passes.
        return (time.time() - self._last_connect_at) >= POST_CONNECT_GRACE_S

    def _wait_for_state(self, target: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self._state_str()
            if s == target:
                return True
            if s == "crashed":
                return False
            time.sleep(0.1)
        return False

    def _pair(self, fresh: bool = False) -> bool:
        """Create the virtual controller. fresh=True requires Change Grip/Order open."""
        with self.lock:
            if self.idx is not None:
                try:
                    self.nx.remove_controller(self.idx)
                except Exception:
                    pass
                self.idx = None
                time.sleep(0.3)

            kwargs = {}
            if not fresh:
                addrs = self.nx.get_switch_addresses()
                if addrs:
                    kwargs["reconnect_address"] = addrs
            try:
                self.idx = self.nx.create_controller(nxbt.PRO_CONTROLLER, **kwargs)
            except Exception as e:
                self.last_error = f"create_controller: {e}"
                log.error(self.last_error)
                return False

        # Block outside the lock so watchdog/status can still observe state.
        ok = self._wait_for_state("connected", timeout=60.0)
        if ok:
            self.reconnect_count += 1
            self._last_connect_at = time.time()
            self.last_error = None
            log.info("connected (reconnect_count=%d) — grace period %ss",
                     self.reconnect_count, POST_CONNECT_GRACE_S)
        else:
            self.last_error = f"pair: ended in state={self._state_str()}"
            log.warning(self.last_error)
        return ok

    # ----- watchdog -----

    def _bluez_link_up(self) -> bool:
        """Check BlueZ for an active link to a known Switch. No nxbt round-trip."""
        addrs = self.nx.get_switch_addresses() or []
        if not addrs:
            return False
        for addr in addrs:
            try:
                result = subprocess.run(
                    ["bluetoothctl", "info", addr],
                    capture_output=True,
                    text=True,
                    timeout=BLUEZ_INFO_TIMEOUT_S,
                )
                if "Connected: yes" in result.stdout:
                    return True
            except Exception:
                continue
        return False

    def _watchdog(self):
        """Passive: reports state, only auto-reconnects when nxbt gave up.

        nxbt's mainloop has its own save_connection() that recovers from BT drops
        by re-establishing the L2CAP sockets. Racing it caused the dbus NoReply
        crashes we saw - so we let it do its thing and only step in if state ends
        up at 'crashed' (the unambiguous nxbt-gave-up signal).

        After a reconnect we use shorter check intervals for POST_RECONNECT_FAST_CYCLES
        cycles so that an immediate re-crash (e.g. due to Wi-Fi/BT antenna contention)
        is caught in ~3 s rather than waiting the full 10 s heartbeat.
        """
        fast_cycles_remaining = 0
        while not self._stop.is_set():
            interval = POST_RECONNECT_FAST_S if fast_cycles_remaining > 0 else HEARTBEAT_INTERVAL_S
            time.sleep(interval)
            if fast_cycles_remaining > 0:
                fast_cycles_remaining -= 1

            self.last_heartbeat_ok = self._bluez_link_up()
            state = self._state_str()
            if state == "crashed":
                log.warning(
                    "nxbt state=crashed (bluez_link_up=%s) — triggering recovery reconnect",
                    self.last_heartbeat_ok,
                )
                self._pair(fresh=False)
                time.sleep(RECONNECT_BACKOFF_S)
                fast_cycles_remaining = POST_RECONNECT_FAST_CYCLES

    # ----- HTTP -----

    def _require_connected(self):
        if not self._is_connected():
            raise HTTPException(
                status_code=503,
                detail=f"pad not connected (state={self._state_str()})",
            )

    def _wait_macro(self, macro_id: str, idx_at_submit: int, timeout: float) -> bool:
        """Wait for macro_id to land in finished_macros on the same controller index.

        If self.idx changes (or becomes None) we know a reconnect swapped the
        controller out from under us; the macro is lost and we return False.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            current_idx = self.idx
            if current_idx is None or current_idx != idx_at_submit:
                return False
            try:
                finished = self.nx.state[current_idx].get("finished_macros", [])
            except Exception:
                return False
            if macro_id in finished:
                return True
            time.sleep(0.02)
        return False

    def _register_routes(self):
        app = self.app

        @app.get("/status")
        def status():
            raw_state = self._state_str()
            grace_remaining = max(0.0, POST_CONNECT_GRACE_S - (time.time() - self._last_connect_at))
            stabilizing = raw_state == "connected" and grace_remaining > 0
            return {
                "state": raw_state,
                "connected": self._is_connected(),   # False during grace period
                "stabilizing": stabilizing,           # True = connected but in grace window
                "grace_remaining_s": round(grace_remaining, 2) if stabilizing else 0,
                "switch_addresses": list(self.nx.get_switch_addresses() or []),
                "reconnect_count": self.reconnect_count,
                "uptime_s": time.time() - self.started_at,
                "last_command_at": self.last_command_at,
                "last_heartbeat_ok": self.last_heartbeat_ok,
                "last_error": self.last_error,
            }

        @app.post("/press")
        def press(req: PressRequest):
            self._require_connected()
            with self.lock:
                idx_at_submit = self.idx
                if idx_at_submit is None:
                    raise HTTPException(503, "pad not connected")
                try:
                    macro_id = self.nx.press_buttons(
                        idx_at_submit, req.buttons, down=req.hold, block=False
                    )
                    self.last_command_at = time.time()
                except Exception as e:
                    self.last_error = str(e)
                    raise HTTPException(500, str(e))
            ok = self._wait_macro(macro_id, idx_at_submit, timeout=req.hold + 2.0)
            if not ok:
                raise HTTPException(503, "press did not complete (likely reconnect)")
            return {"ok": True, "macro_id": macro_id}

        @app.post("/tilt")
        def tilt(req: TiltRequest):
            self._require_connected()
            with self.lock:
                idx_at_submit = self.idx
                if idx_at_submit is None:
                    raise HTTPException(503, "pad not connected")
                try:
                    macro_id = self.nx.tilt_stick(
                        idx_at_submit,
                        req.stick,
                        x=req.x,
                        y=req.y,
                        tilted=req.duration,
                        block=False,
                    )
                    self.last_command_at = time.time()
                except Exception as e:
                    self.last_error = str(e)
                    raise HTTPException(500, str(e))
            ok = self._wait_macro(macro_id, idx_at_submit, timeout=req.duration + 2.0)
            if not ok:
                raise HTTPException(503, "tilt did not complete (likely reconnect)")
            return {"ok": True, "macro_id": macro_id}

        @app.post("/macro")
        def macro(req: MacroRequest):
            self._require_connected()
            with self.lock:
                idx_at_submit = self.idx
                if idx_at_submit is None:
                    raise HTTPException(503, "pad not connected")
                try:
                    macro_id = self.nx.macro(idx_at_submit, req.script, block=False)
                    self.last_command_at = time.time()
                except Exception as e:
                    self.last_error = str(e)
                    raise HTTPException(500, str(e))
            if not req.block:
                return {"ok": True, "macro_id": macro_id, "blocked": False}
            ok = self._wait_macro(macro_id, idx_at_submit, timeout=req.timeout)
            if not ok:
                raise HTTPException(503, "macro did not complete (likely reconnect)")
            return {"ok": True, "macro_id": macro_id, "blocked": True}

        @app.post("/stop")
        def stop():
            with self.lock:
                now = time.time()
                if now - self._last_stop_t < STOP_DEDUP_S:
                    # Second /stop within the dedup window — skip the clear_macros
                    # call. Two rapid clear_macros() can corrupt the nxbt worker
                    # when the BT link is already under stress (e.g. rapid Ctrl-C).
                    return {"ok": True, "deduped": True}
                self._last_stop_t = now
                try:
                    if self.idx is not None:
                        self.nx.clear_macros(self.idx)
                except Exception as e:
                    raise HTTPException(500, str(e))
            return {"ok": True}

        @app.post("/pair")
        def pair_fresh():
            """Initiate a fresh pair. Switch MUST be on Change Grip/Order."""
            threading.Thread(target=self._pair, kwargs={"fresh": True}, daemon=True).start()
            return {"ok": True, "note": "pairing started; Switch must be on Change Grip/Order"}

        @app.post("/reconnect")
        def reconnect():
            """Force re-pair to known Switch (no UI needed on the Switch side)."""
            threading.Thread(target=self._pair, kwargs={"fresh": False}, daemon=True).start()
            return {"ok": True, "note": "reconnect started"}

    # ----- run -----

    def serve(self, host: str = "0.0.0.0", port: int = 8765):
        log.info("starting daemon on %s:%d", host, port)
        self.start_bt_agent()
        # Background initial pair so HTTP comes up immediately.
        threading.Thread(target=self._pair, kwargs={"fresh": False}, daemon=True).start()
        # Watchdog.
        threading.Thread(target=self._watchdog, daemon=True).start()
        try:
            uvicorn.run(self.app, host=host, port=port, log_level="info")
        finally:
            self._stop.set()
            self.stop_bt_agent()
            if self.idx is not None:
                try:
                    self.nx.remove_controller(self.idx)
                except Exception:
                    pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    port = int(os.environ.get("SWITCH_CONTROL_PORT", "8765"))
    Daemon().serve(port=port)


if __name__ == "__main__":
    main()
