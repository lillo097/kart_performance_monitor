#!/usr/bin/env python3
"""Setup centralizzato del logging per glo2-telemetry."""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

# src/logging/logging_setup.py -> risale di 3 livelli fino alla root del progetto
BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "glo2-telemetry.log"

LOG_LEVEL = os.environ.get("GLO2_LOG_LEVEL", "INFO").upper()

_FMT_FILE = "%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)-8s] %(message)s"
_FMT_CONSOLE = "%(asctime)s [%(levelname)s] %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"


def setup_logging(session_dir=None):
    """Configura il logger root, scrivendo nel log della sessione se fornita."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    for handler in root.handlers[:]:
        handler.close()
    root.handlers.clear()

    if session_dir is not None:
        session_dir = Path(session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)
        session_log_file = session_dir / LOG_FILE.name

        fh = logging.handlers.RotatingFileHandler(
            str(session_log_file),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(_FMT_FILE, datefmt=_DATE))
        root.addHandler(fh)

    # --- Console handler (per journalctl) ---
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(_FMT_CONSOLE, datefmt=_DATE))
    root.addHandler(ch)

    # Silenzia librerie verbose
    logging.getLogger("paho").setLevel(logging.WARNING)
    logging.getLogger("gpiozero").setLevel(logging.WARNING)
    logging.getLogger("RPi").setLevel(logging.WARNING)

    return logging.getLogger("main")