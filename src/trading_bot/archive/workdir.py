"""Bounded archive working-directory contract (least privilege).

Production failure cause (2026-08-10): host ``archive-work`` mounted at ``/work``
was owned by the deploy user while the container ran as UID/GID ``10001``, so
``Path.mkdir`` raised ``PermissionError``. Resume temporarily used ``chmod 777``
and ``-u 0:0`` — that must not be the lasting contract.

Approved contract:
* host directory owned by the archive runtime UID/GID (``10001:10001``);
* mode ``0700`` (owner-only; never world-writable);
* container mount at an explicit path (commonly ``/work``);
* fail closed before export if the path is not writable by the current process.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class ArchiveWorkdirError(PermissionError):
    """Raised when the archive workdir contract is violated."""


def _process_ids() -> tuple[int | None, int | None]:
    geteuid = getattr(os, "geteuid", None)
    getegid = getattr(os, "getegid", None)
    uid = int(geteuid()) if callable(geteuid) else None
    gid = int(getegid()) if callable(getegid) else None
    return uid, gid


def ensure_archive_workdir(path: Path, *, mode: int = 0o700) -> Path:
    """Create/validate a private writable archive work directory.

    Does not chmod directories owned by another user (would fail or escalate).
    Refuses world-writable paths so operators do not silently keep ``777``.
    """

    uid, gid = _process_ids()
    target = path.expanduser().resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveWorkdirError(
            f"archive workdir {target} is not creatable by uid={uid}: {exc}"
        ) from exc

    st = target.stat()
    if not stat.S_ISDIR(st.st_mode):
        raise ArchiveWorkdirError(f"archive workdir {target} is not a directory")
    # POSIX world-writable check; Windows permission bits are not equivalent.
    if os.name != "nt" and st.st_mode & stat.S_IWOTH:
        raise ArchiveWorkdirError(
            f"archive workdir {target} is world-writable; "
            "refuse least-privilege violation (expected mode 0700, owner-only)"
        )

    # Prefer tightening mode when we own the directory (POSIX).
    if uid is not None and st.st_uid == uid:
        try:
            target.chmod(mode)
        except OSError as exc:
            raise ArchiveWorkdirError(
                f"cannot set mode {oct(mode)} on archive workdir {target}: {exc}"
            ) from exc

    probe = target / ".archive_workdir_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ArchiveWorkdirError(
            f"archive workdir {target} is not writable by uid={uid} "
            f"(gid={gid}): {exc}. Host path must be owned by the "
            "container runtime UID/GID (10001:10001) with mode 0700."
        ) from exc
    return target
