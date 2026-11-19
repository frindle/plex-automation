"""backtest_local scoring and refusals, with synthetic facts and a faked layer (no network)."""
import json

import backtest_local as bl

UP = {'seeders': 50, 'quality': 'WEB-DL'}
DV = {'seeders': 50, 'candidate_dv_web': True}
LOW = {'seeders': 2}


def reply(action, conf=0.9):
    return json.dumps({'action': action, 'confidence': conf, 'reason': 'x'})


def fake(actions):
    it = iter(actions)

    def transport(prompt):
        a = next(it)
        if isinstance(a, Exception):
            raise a
        return a
    return transport


def rows(n, facts=UP, rules='allow', **extra):
    return [dict({'facts': dict(facts), 'rules_action': rules, 'id': 'r%d' % i}, **extra) for i in range(n)]


def test_scores_answered_rules_and_human():
    data = rows(10, human='allow')
    data[0]['human'] = 'reject'
    res = bl.score('upgrade_gate', data, fake([reply('allow')] * 9 + [reply('reject')]))
    assert res['total'] == 10 and res['answered'] == 10
    assert res['rules_n'] == 10 and res['rules_agree'] == 9 and res['disagree_ids'] == ['#10/r9']
    assert res['human_n'] == 10 and res['human_agree'] == 8
    assert bl.verdict('upgrade_gate', res)[0] == 'FAIL'


def test_unanswered_garbage_and_errors_count_against_answered():
    res = bl.score('upgrade_gate', rows(10), fake([reply('allow')] * 8 + ['not json', TimeoutError('t')]))
    assert res['answered'] == 8
    v, why = bl.verdict('upgrade_gate', res)
    assert v == 'FAIL' and 'answered 80.0%' in why[0]


def test_hard_rule_block_is_listed_with_ids_only():
    data = rows(10) + [{'facts': DV, 'rules_action': 'reject', 'id': 'dv1', 'title': 'Secret Title'}]
    res = bl.score('upgrade_gate', data, fake([reply('allow')] * 11))
    assert res['blocks'] == ['#11/dv1:allow->reject:dv_p5_web']
    text = '\n'.join(bl.report('upgrade_gate', 'm1', res))
    assert 'Secret Title' not in text and 'dv1' in text and 'read every block' in text


def test_low_seeders_block_even_when_rules_agree():
    res = bl.score('upgrade_gate', rows(1, LOW, rules='reject'), fake([reply('throttle')]))
    assert res['blocks'][0].endswith('min_seeders') and res['rules_agree'] == 1


def test_pass_needs_ten_rows_else_inconclusive():
    res = bl.score('upgrade_gate', rows(9), fake([reply('allow')] * 9))
    assert bl.verdict('upgrade_gate', res)[0] == 'INCONCLUSIVE'
    res = bl.score('upgrade_gate', rows(10), fake([reply('allow')] * 10))
    assert bl.verdict('upgrade_gate', res) == ('PASS', [])


def test_agreement_not_scored_for_ask_only_kinds():
    data = rows(10, {'x': 1}, rules='ask')
    res = bl.score('torrent_error', data, fake([reply('recheck')] * 10))
    assert res['rules_agree'] == 0 and res['pairs'] == {'ask->recheck': 10}
    assert bl.verdict('torrent_error', res)[0] == 'PASS'
    assert 'not scored' in '\n'.join(bl.report('torrent_error', 'm', res))


def test_unregistered_remove_variants_agree_and_facts_dict_form():
    data = [dict({'rules_action': 'remove', 'seeded_days': 3}) for _ in range(10)]
    res = bl.score('unregistered_triage', data, fake([reply('remove_and_research')] * 10))
    assert res['rules_agree'] == 10 and bl.verdict('unregistered_triage', res)[0] == 'PASS'


def test_latency_percentiles_use_clock():
    ticks = iter(i * 0.1 for i in range(100))
    res = bl.score('upgrade_gate', rows(10), fake([reply('allow')] * 10), clock=lambda: next(ticks))
    assert bl.percentile(res['lat_ms'], 50) == bl.percentile(res['lat_ms'], 95) == 100
    assert bl.percentile([], 50) is None and bl.percentile([1, 2, 3, 4], 95) == 4


class R:
    def __init__(self, body):
        self.body, self.status_code = body, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class Http:
    def __init__(self, ids=(), hold=(), fail=False):
        self.ids, self.hold, self.fail, self.gets, self.posts = list(ids), list(hold), fail, [], 0

    def get(self, url, **kw):
        self.gets.append(url)
        if url.endswith('/v1/models'):
            if self.fail:
                raise ConnectionError('down')
            return R({'data': [{'id': i} for i in self.ids]})
        return R({'hold': self.hold.pop(0) if self.hold else False, 'cause': 'plex'})

    def post(self, *a, **k):
        self.posts += 1
        raise AssertionError('no chat expected')


ENV = {'DARKBLOOM_BASE_URL': 'http://x.invalid', 'DARKBLOOM_API_KEY': 'k'}


def facts_file(tmp_path, n=10):
    p = tmp_path / 'f.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in rows(n)) + '\n')
    return str(p)


def test_pick_model_first_listed_and_refusals():
    assert bl.pick_model('http://x', 'k', Http(['a', 'b'])) == ('a', None)
    assert bl.pick_model('http://x', 'k', Http(['a', 'b']), want='b') == ('b', None)
    assert bl.pick_model('http://x', 'k', Http(['a']), want='b') == (None, 'model-not-loaded')
    assert bl.pick_model('http://x', 'k', Http(['a']), pin='b') == (None, 'pin-not-loaded')
    assert bl.pick_model('http://x', 'k', Http([])) == (None, 'no-model-loaded')
    assert bl.pick_model('http://x', 'k', Http(fail=True)) == (None, 'models-unreadable')


def test_main_refuses_without_sending_a_chat(tmp_path, capsys):
    http = Http(['a'])
    env = dict(ENV, LLM_LOCAL_MODEL='z')
    assert bl.main(['--kind', 'upgrade_gate', '--facts', facts_file(tmp_path)], env=env, http=http) == 2
    assert 'pin-not-loaded' in capsys.readouterr().out and http.posts == 0
    assert bl.main(['--kind', 'upgrade_gate'], env={}, http=http) == 2


def test_main_runs_prints_model_and_waits_on_hold(tmp_path, capsys):
    http = Http(['m1'], hold=[True, True, False])
    naps = []
    code = bl.main(['--kind', 'upgrade_gate', '--facts', facts_file(tmp_path), '--limit', '10'],
                   env=dict(ENV, LLM_LOCAL_HOLD_URL='http://hold.invalid/h'), http=http,
                   transport=lambda p: reply('allow'), sleep=naps.append)
    out = capsys.readouterr().out
    assert code == 0 and out.startswith('model=m1') and 'verdict=PASS' in out and len(naps) == 2


def test_main_gives_up_when_gpu_stays_held(tmp_path, capsys):
    http = Http(['m1'], hold=[True] * 50)
    code = bl.main(['--kind', 'upgrade_gate', '--facts', facts_file(tmp_path), '--wait-min', '0'],
                   env=dict(ENV, LLM_LOCAL_HOLD_URL='http://hold.invalid/h'), http=http,
                   transport=lambda p: reply('allow'), sleep=lambda s: None)
    assert code == 3 and 'GPU still held' in capsys.readouterr().out
