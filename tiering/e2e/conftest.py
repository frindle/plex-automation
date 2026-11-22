"""Fixtures for the offload tier e2e tests. Run from the repo root:

  python -m pytest --confcutdir=tiering/e2e tiering/e2e

Set E2E_TIERFS_BIN to a built tierfs binary (and run as root) to mount the real
filesystem; without it an emulator stands in for the mount.
"""
import os
import shutil
import tempfile

import pytest

import stack as stack_mod


@pytest.fixture
def make_stack():
    made = []

    def make(disks=1):
        base = os.environ.get("E2E_WORK") or None
        root = tempfile.mkdtemp(prefix="tier-e2e-", dir=base)
        s = stack_mod.Stack(root, disks=disks)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()
        shutil.rmtree(s.root, ignore_errors=True)


@pytest.fixture
def stack(make_stack):
    return make_stack()
