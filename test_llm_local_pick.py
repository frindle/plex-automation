"""Local layer model choice: per kind, the first listed (loaded) id approved for that kind; a pin must be listed."""
import llm_advisor as la

BASE = 'http://local.invalid'


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(self.status)

    def json(self):
        return self.body


class Http:
    def __init__(self, ids=None, entries=None, fail=False):
        self.entries = entries if entries is not None else [{'id': i, 'owned_by': 'this-mac'} for i in (ids or [])]
        self.fail, self.gets = fail, 0

    def get(self, url, headers=None, timeout=None):
        self.gets += 1
        if self.fail:
            raise ConnectionError('down')
        return Resp({'object': 'list', 'data': self.entries})


def env(**kw):
    return dict({'DARKBLOOM_BASE_URL': BASE, 'DARKBLOOM_API_KEY': 'k'}, **kw)


def layer(kind, e, http):
    la._MODELS_CACHE.clear()
    return la.build_layers(kind, e, http=http)['local']


def test_one_model_approved():
    lay = layer('import_identity', env(), Http(['qwen3.5-35b-a3b']))
    assert lay.gate() is None and lay.model() == 'qwen3.5-35b-a3b'


def test_one_model_not_approved_fails_closed():
    lay = layer('import_identity', env(), Http(['other-model']))
    assert lay.gate() == 'model-not-approved'


def test_second_listed_model_is_picked_when_only_it_is_approved():
    e = env(LLM_LOCAL_APPROVED_UPGRADE_GATE='model-b')
    lay = layer('upgrade_gate', e, Http(['model-a', 'model-b']))
    assert lay.gate() is None and lay.model() == 'model-b'


def test_first_approved_wins_when_several_are_approved():
    e = env(LLM_LOCAL_APPROVED_UPGRADE_GATE='model-b,model-a')
    lay = layer('upgrade_gate', e, Http(['model-a', 'model-b']))
    assert lay.model() == 'model-a'


def test_pin_loaded_wins_over_approval_order():
    e = env(LLM_LOCAL_MODEL='Org/Model-B', LLM_LOCAL_APPROVED_UPGRADE_GATE='model-a')
    lay = layer('upgrade_gate', e, Http(['model-a', 'model-b']))
    assert lay.gate() is None and lay.model() == 'model-b'


def test_pin_not_loaded_skips_and_never_names_it_to_darkbloom():
    e = env(LLM_LOCAL_MODEL='model-z')
    lay = layer('upgrade_gate', e, Http(['qwen3.5-35b-a3b']))
    assert lay.gate() == 'model-not-loaded'
    assert lay.model() == 'model-z'  # only reached if the gate were ignored; the chain never asks after a skip


def test_unreadable_or_empty_list_is_model_unknown():
    lay = layer('upgrade_gate', env(), Http(fail=True))
    try:
        lay.gate()
        raised = False
    except Exception:  # noqa: BLE001 - run_chain maps a raising gate to model-unknown
        raised = True
    assert raised
    assert layer('upgrade_gate', env(), Http([])).gate() == 'model-unknown'
    assert layer('upgrade_gate', env(LLM_LOCAL_MODEL='m'), Http([])).gate() == 'model-unknown'


def test_per_kind_difference_on_the_same_listing():
    http = Http(['model-a', 'model-b'])
    e = env(LLM_LOCAL_APPROVED_UPGRADE_GATE='model-a', LLM_LOCAL_APPROVED_TORRENT_ERROR='model-b')
    assert layer('upgrade_gate', e, http).model() == 'model-a'
    assert layer('torrent_error', e, http).model() == 'model-b'
    assert layer('dupe_confirm', e, http).gate() == 'model-not-approved'


def test_wildcard_takes_first_listed():
    e = env(LLM_LOCAL_APPROVED_DUPE_CONFIRM='*')
    lay = layer('dupe_confirm', e, Http(['model-a', 'model-b']))
    assert lay.gate() is None and lay.model() == 'model-a'


def test_entries_marked_not_loaded_are_ignored_if_such_a_field_appears():
    entries = [{'id': 'model-a', 'loaded': False}, {'id': 'model-c', 'status': 'unloaded'}, {'id': 'model-b'},
               {'nope': 1}, 'junk']
    e = env(LLM_LOCAL_APPROVED_UPGRADE_GATE='*')
    lay = layer('upgrade_gate', e, Http(entries=entries))
    assert lay.model() == 'model-b'
    assert layer('upgrade_gate', env(LLM_LOCAL_MODEL='model-a'), Http(entries=entries)).gate() == 'model-not-loaded'


def test_whole_list_cached_one_get():
    http = Http(['model-a', 'model-b'])
    la._MODELS_CACHE.clear()
    lays = [la.build_layers(k, env(), http=http)['local'] for k in ('import_identity', 'upgrade_gate')]
    for lay in lays:
        lay.gate()
        lay.model()
    assert http.gets == 1


def test_local_now_reports_model_or_skip_per_kind():
    la._MODELS_CACHE.clear()
    e = env(LLM_LOCAL_APPROVED_TORRENT_ERROR='model-b')
    out = la.local_now(e, Http(['qwen3.5-35b-a3b', 'model-b']))
    assert out['loaded'] == ['qwen3.5-35b-a3b', 'model-b']
    assert out['kinds']['import_identity'] == {'model': 'qwen3.5-35b-a3b', 'skip': None}
    assert out['kinds']['torrent_error'] == {'model': 'model-b', 'skip': None}
    assert out['kinds']['dupe_confirm'] == {'model': None, 'skip': 'model-not-approved'}


def test_local_now_pin_and_unreadable_and_unconfigured():
    la._MODELS_CACHE.clear()
    out = la.local_now(env(LLM_LOCAL_MODEL='model-z'), Http(['model-a']))
    assert out['pin'] == 'model-z' and out['kinds']['upgrade_gate']['skip'] == 'model-not-loaded'
    la._MODELS_CACHE.clear()
    out = la.local_now(env(), Http(fail=True))
    assert out['loaded'] is None and out['kinds']['upgrade_gate']['skip'] == 'model-unknown'
    assert la.local_now({}, Http()) is None
