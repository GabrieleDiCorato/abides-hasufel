"""Pluggable log writer Protocol used by ``Kernel``.

The kernel itself does not pickle dataframes any more; it delegates to
an injected :class:`LogWriter`. Two concrete implementations ship in
this module:

* :class:`NullLogWriter` — writes nothing. Used when ``skip_log=True``
  or in unit tests.
* :class:`BZ2PickleLogWriter` — the legacy on-disk format
  (``<root>/<run_id>/<name>.bz2`` with bzip2-compressed
  :func:`pandas.DataFrame.to_pickle` payloads). The run directory is
  materialised lazily on the first write so dry-run configs do not
  litter empty directories.
"""

from __future__ import annotations

import contextlib
import os
from typing import Protocol

import pandas as pd


class LogWriter(Protocol):
    """Minimal contract the kernel needs from any log sink."""

    def write_agent_log(
        self, agent_name: str, df_log: pd.DataFrame, filename: str | None = None
    ) -> None:
        """Persist a single agent's event log.

        ``filename`` is supplied verbatim by the caller when the agent
        wants a non-default file name; implementations should honour it
        without altering the extension.
        """

    def write_summary_log(self, df_log: pd.DataFrame) -> None:
        """Persist the kernel-level summary log."""


class NullLogWriter:
    """No-op writer. Touches no disk."""

    def write_agent_log(
        self, agent_name: str, df_log: pd.DataFrame, filename: str | None = None
    ) -> None:
        return

    def write_summary_log(self, df_log: pd.DataFrame) -> None:
        return


class BZ2PickleLogWriter:
    """Legacy on-disk format: ``<root>/<run_id>/<name>.bz2``.

    The output directory is created on the first successful write so
    callers that never log anything do not leave behind empty dirs.
    """

    def __init__(self, root: str | os.PathLike, run_id: str) -> None:
        self._root: str = os.path.abspath(os.fspath(root))
        self._run_id: str = run_id
        self._dir_ready: bool = False

    @property
    def output_dir(self) -> str:
        return os.path.join(self._root, self._run_id)

    def _ensure_dir(self) -> None:
        if not self._dir_ready:
            os.makedirs(self.output_dir, exist_ok=True)
            self._dir_ready = True

    def write_agent_log(
        self, agent_name: str, df_log: pd.DataFrame, filename: str | None = None
    ) -> None:
        self._ensure_dir()
        file = f"{filename}.bz2" if filename else f"{agent_name.replace(' ', '')}.bz2"
        self._atomic_to_pickle(df_log, os.path.join(self.output_dir, file))

    def write_summary_log(self, df_log: pd.DataFrame) -> None:
        self._ensure_dir()
        self._atomic_to_pickle(df_log, os.path.join(self.output_dir, "summary_log.bz2"))

    @staticmethod
    def _atomic_to_pickle(df_log: pd.DataFrame, final_path: str) -> None:
        """Pickle ``df_log`` to ``final_path`` atomically.

        Writes to a sibling ``<final_path>.tmp`` file first, then
        :func:`os.replace` swaps it into place. If serialisation fails
        midway the temp file is removed and ``final_path`` is left
        untouched, so partially-written ``.bz2`` files never appear on
        disk to confuse downstream readers.
        """
        tmp_path = f"{final_path}.tmp"
        try:
            df_log.to_pickle(tmp_path, compression="bz2")
            os.replace(tmp_path, final_path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)
            raise
