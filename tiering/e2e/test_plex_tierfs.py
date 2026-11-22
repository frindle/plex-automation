"""Plex reading offloaded files through the real tierfs mount.

Plex reads a file for its own work (scans, video preview thumbnails, intro and
credits markers, loudness) as well as for people. Only a person's read may bring
an offloaded file back; everything else must read through the remote and leave
the file offloaded.

- test_plex_transcoder_jobs: the command lines of Plex's analysis jobs, as
  captured from Plex 1.43, run by a binary named "Plex Transcoder" (ffmpeg
  under that name: tierfs goes by the process name and its command line).
  Needs root, FUSE, ffmpeg and E2E_TIERFS_BIN.
- test_real_plex: a real Plex Media Server (docker, unclaimed, no account)
  scans the library, the runner offloads it, Plex scans again and makes preview
  thumbnails, then a download through Plex brings one file back. Also needs
  docker and E2E_PLEX=1.

Run from the repo root, as root:
  E2E_TIERFS_BIN=... E2E_PLEX=1 python -m pytest --confcutdir=tiering/e2e \
      tiering/e2e/test_plex_tierfs.py
"""
import glob
import os
import shutil
import subprocess
import tempfile

import pytest

import plexctl
import stack as S

REAL = bool(S.TIERFS_BIN) and S.ROOT and shutil.which("ffmpeg") is not None
needs_real = pytest.mark.skipif(not REAL, reason="needs root, ffmpeg and E2E_TIERFS_BIN")
needs_plex = pytest.mark.skipif(
    not (REAL and os.environ.get("E2E_PLEX") == "1" and shutil.which("docker")),
    reason="needs docker and E2E_PLEX=1 as well")

ONE = "Movies/Test Movie One (2001)/Test Movie One (2001).mkv"  # from make_media.sh
TWO = "Movies/Test Movie Two (2002)/Test Movie Two (2002).mkv"
# In the library they keep their release names, as a usenet grab does.
LIB = {ONE: "Movies/Test Movie One (2001)/Test.Movie.One.2001.1080p.WEB-GRP.mkv",
       TWO: "Movies/Test Movie Two (2002)/Test.Movie.Two.2002.1080p.WEB-GRP.mkv"}
PLEX_UP = os.path.join(S.HERE, "plex_up.sh")


@pytest.fixture(scope="module")
def media():
    """Two real movies (h264/aac, about 70 MB each) from make_media.sh."""
    if not REAL:
        pytest.skip("needs root, ffmpeg and E2E_TIERFS_BIN")
    d = tempfile.mkdtemp(prefix="tier-e2e-media-", dir=os.environ.get("E2E_WORK") or None)
    subprocess.run([os.path.join(S.HERE, "make_media.sh"), d, "--movies-only"],
                   check=True, capture_output=True, timeout=600)
    yield {ONE: os.path.join(d, ONE), TWO: os.path.join(d, TWO)}
    shutil.rmtree(d, ignore_errors=True)


def offloaded(stack, *fs):
    stack.start()
    stack.wait_state(list(fs), "offloaded", timeout=180)
    for f in fs:
        assert os.path.islink(f["disk"])
        assert os.readlink(f["disk"]).startswith(stack.mount + "/")


def events(stack, f, ev):
    id = stack.row(f)["id"]
    return [e for e in stack.tierfs_events(ev) if e["id"] == id]


def no_promote(stack, f, what):
    assert events(stack, f, "want") == [], f"{what} asked for a promote"
    assert stack.state_of(f) == "offloaded", \
        f"{what}: {[e['kind'] for e in stack.db_events(f)]}"


# ------------------------------------------------------------------ analysis jobs

def fake_transcoder(stack):
    p = os.path.join(stack._dir("bin"), "Plex Transcoder")
    shutil.copy(shutil.which("ffmpeg"), p)
    os.chmod(p, 0o755)
    return p


@needs_real
def test_plex_transcoder_jobs(stack, media):
    """Plex's preview, marker and loudness jobs read through the remote and never
    promote; the same binary writing a stream (any other muxer) is a player."""
    f = stack.add("m", LIB[ONE], src=media[ONE])
    offloaded(stack, f)
    lib = f["disk"]
    tx = fake_transcoder(stack)
    out = stack._dir("transcode")
    idx = stack._dir("pms/Media/localhost/b/0f3a9c.bundle/Contents/Indexes/tmp")
    jobs = {
        # Plex 1.43.5/1.43.6 preview thumbnails: keyframes, no -f at all.
        "preview thumbnails": [
            "-codec:v", "h264", "-skip_frame:v", "nokey", "-i", lib, "-threads", "0",
            "-nostats", "-q", "3", "-loglevel", "error", "-filter_complex",
            "[0:V:0] fps=fps=0.500000:round=up,scale=w=320:h=240:"
            "force_original_aspect_ratio=decrease [out]",
            "-map", "[out]", os.path.join(idx, "img-%06d.jpg")],
        # 1.43.6 intro and credits markers: mono 8 kHz flac to a file.
        "markers": [
            "-nostdin", "-loglevel", "error", "-i", lib, "-vn", "-ac", "1", "-ar", "8000",
            "-f", "flac", os.path.join(out, "plex-marker-0f3a9c")],
        "loudness": [
            "-nostdin", "-loglevel", "error", "-i", lib, "-vn",
            "-af", "loudnorm=print_format=summary", "-f", "null", "-"],
    }
    for what, args in jobs.items():
        seen = len(events(stack, f, "remote_read"))
        r = subprocess.run([tx] + args, capture_output=True, timeout=300)
        assert r.returncode == 0, (what, r.stderr.decode(errors="replace")[-600:])
        assert len(events(stack, f, "remote_read")) > seen, f"{what} never read the remote"
        no_promote(stack, f, what)
    assert glob.glob(os.path.join(idx, "img-*.jpg")), "no preview images written"
    assert os.path.getsize(os.path.join(out, "plex-marker-0f3a9c")) > 0

    # Plex Transcoder streaming to a client: waits for the promote, then succeeds.
    r = subprocess.run([tx, "-nostdin", "-loglevel", "error", "-i", lib, "-map", "0",
                        "-c", "copy", "-f", "matroska", os.path.join(out, "stream.mkv")],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-600:]
    assert events(stack, f, "want"), "a client stream did not ask for a promote"
    stack.wait_state(f, "whole", timeout=120)
    assert not os.path.islink(lib)
    assert S.sha256_file(lib) == f["sha256"]


# ------------------------------------------------------------------ a real Plex

@pytest.fixture
def real_plex(stack):
    """An unclaimed Plex in docker that sees the disk and the tierfs mount at the
    same paths as the host. The mount already exists, so a plain bind is enough."""
    work = stack._dir("plex")
    lib = os.path.join(stack.disks[0], "Media")
    for d in ("Movies", "TV"):
        os.makedirs(os.path.join(lib, d), exist_ok=True)
    r = subprocess.run([PLEX_UP, "up", work, lib, stack.mount], capture_output=True,
                       text=True, timeout=400, env=dict(os.environ, PLEX_MNT_OPT="rw"))
    if r.returncode != 0:
        logs = subprocess.run(["docker", "logs", "--tail", "40", "tiering-e2e-plex"],
                              capture_output=True, text=True).stdout
        subprocess.run([PLEX_UP, "down"], capture_output=True)
        pytest.fail(f"plex did not start: {r.stdout}{r.stderr}\n{logs}")
    px = plexctl.Plex(timeout=180)
    px.work, px.lib = work, lib
    yield px
    subprocess.run([PLEX_UP, "down"], capture_output=True)


def bifs(px):
    return glob.glob(os.path.join(px.work, "plexconfig", "**", "index-sd.bif"), recursive=True)


def by_file(px):
    return {p["file"]: p for p in px.parts()}


@needs_plex
def test_real_plex(stack, media, real_plex):
    px = real_plex
    fs = [stack.add("one", LIB[ONE], src=media[ONE]),
          stack.add("two", LIB[TWO], src=media[TWO])]

    # 1. Plex knows the files while they are whole.
    px.create_standard(os.path.join(px.lib, "Movies"), os.path.join(px.lib, "TV"))
    movies = [s["key"] for s in px.sections() if s["type"] == "movie"][0]
    px.scan(movies)
    px.wait_scan()
    S.wait_until(lambda: len(px.parts()) == 2, 120, poll=2, what="Plex to list both files")
    before = by_file(px)
    assert set(before) == {f["disk"] for f in fs}

    # 2. Offload both. L becomes a symlink into the mount.
    offloaded(stack, *fs)

    # 3. A forced rescan reads through the mount, keeps the parts, promotes nothing.
    px.scan(movies, force=True)
    px.wait_scan()
    after = by_file(px)
    for f in fs:
        for k in ("partId", "mediaId", "ratingKey", "size"):
            assert after[f["disk"]][k] == before[f["disk"]][k], (f["key"], k)
        no_promote(stack, f, "a forced scan")

    # 4. Video preview thumbnails, the way Plex makes them when a file is added.
    px.request("/:/prefs", method="PUT", params={"GenerateBIFBehavior": "asap"})
    for f in fs:
        px.request(f"/library/metadata/{after[f['disk']]['ratingKey']}/analyze", method="PUT")
    S.wait_until(lambda: len(bifs(px)) == 2, 240, poll=1, what="two preview (BIF) files")
    px.wait_scan()
    for f in fs:
        assert events(stack, f, "remote_read"), f"{f['key']}: previews never read the remote"
        no_promote(stack, f, "preview thumbnails")

    # 5. A download through Plex is a person: it waits for the promote and gets
    # every byte. The other file stays offloaded, and Plex keeps the same part.
    one, two = fs
    got = px.fetch_sha256(after[one["disk"]]["key"])
    assert got["sha256"] == one["sha256"]
    assert events(stack, one, "want")
    stack.wait_state(one, "whole", timeout=120)
    assert S.sha256_file(one["disk"]) == one["sha256"]
    no_promote(stack, two, "a download of another file")
    px.scan(movies)
    px.wait_scan()
    assert by_file(px)[one["disk"]]["partId"] == before[one["disk"]]["partId"]
