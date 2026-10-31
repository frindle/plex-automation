"""Frozen tests: classify() must ignore Sample/ video files."""
import os
import sys
import importlib.util

spec = importlib.util.spec_from_file_location(
    "target", os.path.join(os.path.dirname(os.path.abspath(__file__)), 'episode_retention.py'))
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)

N = 'Married.At.First.Sight.AU.S13E37.1080p.WEB'


def T(files, name=N, label='sonarr'):
    return {'name': name, 'label': label, 'files': [{'path': p} for p in files]}


def test_sample_dir_ignored():
    t = T([N + '/Sample/sample-abc.mkv', N + '/abc.mkv'])
    assert target.classify(t) == 'episode'


def test_sample_dir_case_insensitive():
    t = T([N + '/SAMPLE/abc-sample.mkv', N + '/abc.mkv'])
    assert target.classify(t) == 'episode'


def test_sample_basename_ignored():
    t = T([N + '/sample-abc.mkv', N + '/abc.mkv'])
    assert target.classify(t) == 'episode'


def test_real_two_videos_still_pack():
    t = T([N + '/a.mkv', N + '/b.mkv'])
    assert target.classify(t) == 'season-pack'


def test_only_sample_video_is_pack_and_library_seed_works():
    assert target.classify(T([N + '/Sample/sample-x.mkv'])) == 'season-pack'
    assert target.classify(T([N + '/Sample/sample-x.mkv', N + '/x.mkv'], label='library-seed')) == 'episode'
