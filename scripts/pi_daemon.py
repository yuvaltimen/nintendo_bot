"""Entry point: start the Switch Control daemon on the Pi.

Run on the Raspberry Pi with:
    sudo /home/yuvaltimen/nxbt/.venv/bin/python scripts/pi_daemon.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_control.daemon import main

if __name__ == "__main__":
    main()
