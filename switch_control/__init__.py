"""Switch Control: Mac C&C → Pi BT daemon → Switch.

The package is import-safe on both Mac and Pi. Submodules have platform constraints:
  - switch_control.client    - Mac and Pi (stdlib only)
  - switch_control.pad       - Pi only (imports nxbt)
  - switch_control.daemon    - Pi only (imports nxbt + fastapi)
"""

from .client import RemotePad, Buttons, Sticks

__all__ = ["RemotePad", "Buttons", "Sticks"]
