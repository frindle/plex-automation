"""The sabnzbd.ini render step: unit conversion, connection limits, idempotence, mode 600, no secrets in output."""
import importlib.util
import os
import stat

import pytest

spec = importlib.util.spec_from_file_location('render_sab', os.path.join('usenet', 'render_sabnzbd_ini.py'))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

ENV = {'NNTP_PRIMARY_HOST': 'news.invalid', 'NNTP_PRIMARY_PORT': '563', 'NNTP_PRIMARY_USER': 'someuser',
       'NNTP_PRIMARY_PASS': 'p@ss word,"x"', 'SABNZBD_CONNECTIONS': '12', 'ALTMOUNT_CONNECTIONS': '8',
       'USENET_ACCOUNT_CONNECTIONS': '30', 'SABNZBD_API_KEY': 'k' * 32, 'SABNZBD_COMPLETE_DIR': '/data/Downloads/usenet'}


def test_bandwidth_conversion_shown():
    # megabit per second to bytes per second: 750 * 1,000,000 / 8
    assert r.bandwidth_max(750) == '93750000'
    assert r.bandwidth_max('1') == '125000'
    assert r.bandwidth_max('0') == '' and r.bandwidth_max('') == '' and r.bandwidth_max('junk') == ''
    assert r.bandwidth_max(0.5) == '62500'


def test_default_cap_is_750_mbit_and_zero_is_unlimited():
    out, _ = r.render('', {})
    assert 'bandwidth_max = 93750000' in out and 'bandwidth_perc = 100' in out
    out, _ = r.render('', {'SABNZBD_MAX_MBIT': '0'})
    assert 'bandwidth_max = ""' in out
    out, _ = r.render('', {'SABNZBD_MAX_MBIT': '100'})
    assert 'bandwidth_max = 12500000' in out


def test_connections_stay_under_the_account_limit():
    assert r.connections(12, 8, 30) == (12, '')
    n, note = r.connections(40, 8, 30)
    assert n == 22 and '40' in note and '22' in note
    assert r.connections(5, 30, 30)[0] is None                         # no room left
    assert r.connections(None, 8, 30)[0] is None and r.connections(5, 8, None)[0] is None
    assert r.connections(0, 8, 30)[0] is None


def test_server_entry_is_written_with_clamped_connections():
    out, rep = r.render('', {**ENV, 'SABNZBD_CONNECTIONS': '99'})
    assert rep['server'] and 'connections = 14' in out      # 30 - AltMount 8 - STAT pool 8 (default)
    assert 'host = news.invalid' in out and 'username = someuser' in out
    assert 'ssl = 1' in out and '[[usenet]]' in out and '[servers]' in out


def test_no_server_entry_without_the_limits_or_credentials():
    out, rep = r.render('', {**ENV, 'USENET_ACCOUNT_CONNECTIONS': ''})
    assert not rep['server'] and '[servers]' not in out and rep['notes']
    out, rep = r.render('', {k: v for k, v in ENV.items() if k != 'NNTP_PRIMARY_PASS'})
    assert not rep['server']


def test_plain_port_119_is_not_ssl():
    out, _ = r.render('', {**ENV, 'NNTP_PRIMARY_PORT': '119'})
    assert 'ssl = 0' in out


def test_special_characters_are_quoted():
    out, _ = r.render('', ENV)
    assert 'password = \'p@ss word,"x"\'' in out
    with pytest.raises(ValueError):
        r.render('', {**ENV, 'NNTP_PRIMARY_PASS': 'a"b\'c'})


def test_idempotent_and_keeps_other_lines():
    base = '__version__ = 19\n[misc]\nport = 8080\nbandwidth_max = 1\n[servers]\n[[other]]\nhost = keep.me\n'
    once, rep1 = r.render(base, ENV)
    twice, rep2 = r.render(once, ENV)
    assert once == twice and rep2['changed'] == 0 and rep1['changed'] > 0
    assert 'port = 8080' in once and 'host = keep.me' in once and 'bandwidth_max = 93750000' in once
    assert once.count('bandwidth_max') == 1 and once.count('[[usenet]]') == 1 and once.count('[servers]') == 1


def test_categories_exist():
    out, _ = r.render('', ENV)
    for c in ('radarr', 'sonarr'):
        assert f'[[{c}]]' in out
    out, _ = r.render('', {**ENV, 'SABNZBD_CATEGORY_RADARR': 'movies-usenet'})
    assert '[[movies-usenet]]' in out


def test_hold_category_has_its_own_folder_no_script_and_default_priority():
    out, _ = r.render('', ENV)
    body = out.split('[[watch-hold]]')[1].split('[')[0]
    assert 'dir = watch-hold' in body and 'script = None' in body and 'priority = -100' in body
    again, rep = r.render(out, ENV)
    assert again == out and rep['changed'] == 0 and out.count('[[watch-hold]]') == 1
    kept, _ = r.render('[categories]\n[[watch-hold]]\nname = watch-hold\ndir = elsewhere\n', ENV)
    assert 'dir = watch-hold' in kept and 'dir = elsewhere' not in kept


def test_file_is_mode_600_and_main_prints_no_secret(tmp_path, capsys):
    p = tmp_path / 'sabnzbd.ini'
    p.write_text('[misc]\nport = 8080\n')
    os.chmod(p, 0o644)
    assert r.main([str(p)], ENV) == 0
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    printed = capsys.readouterr().out
    for secret in (ENV['NNTP_PRIMARY_PASS'], ENV['SABNZBD_API_KEY'], ENV['NNTP_PRIMARY_USER'], ENV['NNTP_PRIMARY_HOST']):
        assert secret not in printed
    assert 'updated' in printed
    before = p.read_text()
    assert r.main([str(p)], ENV) == 0 and 'unchanged' in capsys.readouterr().out
    assert p.read_text() == before and not [f for f in os.listdir(tmp_path) if f.endswith('.tmp')]


def test_missing_file_is_created_private(tmp_path):
    p = tmp_path / 'sabnzbd.ini'
    assert r.main([str(p)], ENV) == 0 and stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_new_child_section_lands_under_its_own_parent():
    base = '[servers]\n[[other]]\nhost = keep.me\n[categories]\n[[*]]\nname = *\n'
    out, _ = r.render(base, ENV)
    lines = out.splitlines()
    assert r._find(lines, ('servers', 'usenet')) is not None and r._find(lines, ('categories', 'radarr')) is not None
    assert lines.index('[[usenet]]') < lines.index('[categories]')


def test_stat_pool_is_subtracted_and_no_room_switches_the_old_entry_off():
    assert r.connections(12, 8, 30, 8) == (12, '') and r.connections(20, 8, 30, 8)[0] == 14
    assert r.connections(5, 8, 30, 22)[0] is None
    once, _ = r.render('', ENV)
    assert 'enable = 1' in once
    out, rep = r.render(once, {**ENV, 'NNTP_PRIMARY_CONNS': '22'})
    assert not rep['server'] and 'enable = 0' in out and 'enable = 1' not in out and rep['notes']
    again, rep2 = r.render(out, {**ENV, 'NNTP_PRIMARY_CONNS': '22'})
    assert again == out and rep2['changed'] == 0
    # credentials removed: same switch-off
    out2, _ = r.render(once, {k: v for k, v in ENV.items() if k != 'NNTP_PRIMARY_PASS'})
    assert 'enable = 0' in out2


def test_file_is_chowned_to_puid_pgid_before_the_replace(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, 'fchown', lambda fd, u, g: calls.append((u, g)))
    p = tmp_path / 'sabnzbd.ini'
    assert r.write_private(str(p), 'x\n', 99, 100) and calls == [(99, 100)]
    calls.clear()
    p2, p3 = tmp_path / 'b.ini', tmp_path / 'c.ini'
    assert r.main([str(p2)], {**ENV, 'PUID': '1000', 'PGID': '1001'}) == 0 and calls == [(1000, 1001)]
    calls.clear()
    assert r.main([str(p3)], ENV) == 0 and calls == [(99, 100)]      # default 99/100
    assert stat.S_IMODE(os.stat(p3).st_mode) == 0o600
