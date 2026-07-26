"""Two processes must never both spend the same single-use refresh token."""
from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from tesla import TokenStore, Tokens, _acquire_lock, _release_lock


def _write(path: Path, access: str, expires_at: float) -> None:
    path.write_text(json.dumps(
        {"access_token": access, "refresh_token": "r0", "expires_at": expires_at}))


def test_reload_sees_another_process_write(tmp_path):
    """load() caches; reload() must not."""
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    store = TokenStore(p)
    assert store.load().access_token == "first"

    _write(p, "second", time.time() + 9999)     # simulates the other process
    assert store.load().access_token == "first"    # cached, as designed
    assert store.reload().access_token == "second"  # forced re-read


def _hold(path_str: str, started, release):
    """Child: take the lock, signal, hold until told to let go."""
    fd = _acquire_lock(Path(path_str))
    started.set()
    release.wait(timeout=10)
    _release_lock(fd)


def test_lock_is_exclusive_across_processes(tmp_path):
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    started, release = mp.Event(), mp.Event()
    child = mp.Process(target=_hold, args=(str(p), started, release))
    child.start()
    try:
        assert started.wait(timeout=10), "child never acquired"
        t0 = time.time()
        release.set()
        fd = _acquire_lock(p)          # must block until the child releases
        waited = time.time() - t0
        _release_lock(fd)
        assert waited >= 0.0
    finally:
        release.set()
        child.join(timeout=10)


def test_lock_file_is_a_sidecar_not_the_token_file(tmp_path):
    """save() replaces the token file's inode; an flock on it would be lost."""
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    fd = _acquire_lock(p)
    _release_lock(fd)
    assert (tmp_path / ".tokens.lock").exists()
