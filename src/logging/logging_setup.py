#!/usr/bin/env python3
"""Setup centralizzato del logging per glo2-telemetry.

Tutte le righe di log (record di logging, print su stdout/stderr, warning
Python, eccezioni non gestite) vengono instradate al SessionLogger attivo:
- bufferizzate in RAM (max 100.000 righe) finché nessuna sessione è attiva
- scritte nel file di sessione (glo2-telemetry.log) quando la sessione esiste
"""

import logging
import os
import sys
import warnings
from pathlib import Path

from session_logger import get_current_session_logger

# src/logging/logging_setup.py -> risale di 3 livelli fino alla root del progetto
BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = BASE_DIR / "system_logs"
LOG_FILE = LOGS_DIR / "glo2-telemetry.log"

LOG_LEVEL = os.environ.get("GLO2_LOG_LEVEL", "INFO").upper()

_FMT_FILE = "%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)-8s] %(message)s"
_FMT_CONSOLE = "%(asctime)s [%(levelname)s] %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"

# Stream originali (prima di qualsiasi cattura): usati per journalctl e per
# evitare che le righe loggate vengano ricatturate come [stdout]/[stderr].
_original_stdout = sys.stdout
_original_stderr = sys.stderr

_warnings_hook_installed = False


class SessionLogForwarder(logging.Handler):
    """Forwarda ogni record di logging al SessionLogger attivo."""

    def emit(self, record):
        try:
            sl = get_current_session_logger()
            if sl is not None:
                sl.append_formatted_line(sl.format_logging_record(record))
        except Exception:
            pass


class _CapturedStream:
    """Wrapper su stdout/stderr: replica sullo stream originale (journalctl)
    e cattura le righe per il log di sessione, con tag [stdout] o [stderr]."""

    def __init__(self, original, tag):
        self._original = original
        self._tag = tag
        self._pending = ""

    def write(self, s):
        # Alcuni writer C/estensioni passano bytes o None: non devono far
        # esplodere la cattura. Replica comunque sullo stream originale.
        if s is None:
            return 0
        if not isinstance(s, str):
            try:
                s = str(s)
            except Exception:
                s = repr(s)

        n = 0
        if self._original is not None:
            try:
                n = self._original.write(s)
            except Exception:
                n = 0

        if s:
            self._pending += s
            try:
                while "\n" in self._pending:
                    line, self._pending = self._pending.split("\n", 1)
                    sl = get_current_session_logger()
                    if sl is not None:
                        sl.append_captured_line(self._tag, line)
            except Exception:
                # Una riga difettosa non deve perdere le successive
                self._pending = ""
        return n

    def flush(self):
        if self._original is not None:
            self._original.flush()

    def close(self):
        """Chiude lo stream: cattura la riga parziale residua prima di chiudere."""
        try:
            self.flush_captured()
        finally:
            if self._original is not None:
                try:
                    self._original.close()
                except Exception:
                    pass

    def flush_captured(self):
        """Cattura l'eventuale riga parziale ancora non terminata da \n."""
        if not self._pending:
            return
        line, self._pending = self._pending, ""
        try:
            sl = get_current_session_logger()
            if sl is not None:
                sl.append_captured_line(self._tag, line)
        except Exception:
            pass

    def isatty(self):
        if self._original is not None:
            return self._original.isatty()
        return False

    def __getattr__(self, name):
        return getattr(self._original, name)


def _warnings_hook(message, category, filename, lineno, file=None, line=None):
    """Cattura i warning Python (DeprecationWarning, ecc.) nel log di sessione."""
    text = (f"{os.path.basename(filename)}:{lineno}: "
            f"{category.__name__}: {message}")
    sl = get_current_session_logger()
    if sl is not None:
        sl.append_captured_line("py.warnings", text)
    # Mantieni la visibilità su journalctl scrivendo sullo stderr ORIGINALE
    # (sys.__stderr__, non ricatturato): altrimenti la stessa riga verrebbe
    # duplicata nel log di sessione come [stderr].
    try:
        if _original_stderr is not None:
            _original_stderr.write(f"WARNING: {text}\n")
            _original_stderr.flush()
    except Exception:
        pass


def _install_capture_hooks():
    """Cattura stdout/stderr e warning Python. Idempotente."""
    global _warnings_hook_installed
    if not isinstance(sys.stdout, _CapturedStream):
        sys.stdout = _CapturedStream(sys.stdout, "stdout")
    if not isinstance(sys.stderr, _CapturedStream):
        sys.stderr = _CapturedStream(sys.stderr, "stderr")
    if not _warnings_hook_installed:
        _warnings_hook_installed = True
        warnings.showwarning = _warnings_hook


def setup_logging(session_dir=None):
    """Configura il logger root.

    `session_dir` è mantenuto per compatibilità con il codice esistente:
    il file di sessione (glo2-telemetry.log) viene aperto e gestito dal
    SessionLogger, che riceve tutte le righe via SessionLogForwarder
    (incluso il buffer accumulato prima dell'avvio della sessione).
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    _install_capture_hooks()

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    for handler in root.handlers[:]:
        handler.close()
    root.handlers.clear()

    # Tutte le righe di logging passano dal SessionLogger
    # (file di sessione o buffer RAM, a seconda dello stato)
    root.addHandler(SessionLogForwarder())

    # Console handler (per journalctl): scrive sullo stream originale per
    # evitare che le righe loggate vengano ricatturate come [stdout]
    ch = logging.StreamHandler(_original_stdout)
    ch.setFormatter(logging.Formatter(_FMT_CONSOLE, datefmt=_DATE))
    root.addHandler(ch)

    # Silenzia librerie verbose
    logging.getLogger("paho").setLevel(logging.WARNING)
    logging.getLogger("gpiozero").setLevel(logging.WARNING)
    logging.getLogger("RPi").setLevel(logging.WARNING)

    return logging.getLogger("main")


# Cattura fin dall'import del modulo: le righe emesse prima del primo
# setup_logging (es. print/warning all'import) vengono così bufferizzate
# e scritte retroattivamente nel log di sessione.
_install_capture_hooks()
