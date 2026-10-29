"""llm_decide.py -- advisory local-LLM release decisions with a deterministic fallback."""

import datetime
import json
import math
import os
import posixpath
import re

KINDS = {
    'upgrade_gate': ('allow', 'throttle', 'reject'),
    'import_identity': ('match', 'mismatch', 'unsure'),
    'dupe_keeper': ('keep_first', 'keep_second', 'keep_both'),
}
MIN_CONFIDENCE = 0.75
SEED_DAYS_DEFAULT = 21
MIN_SEEDERS = 10
LIBRARY_ROOTS = ('/media/movies', '/media/tv')

# Penn's standing preferences, shown to the model. Advisory only: apply_hard_rules() enforces the
# non-negotiable ones in code. Edit here, not in the prompts.
POLICY = {
    'upgrade_gate': (
        'Upgrades are throttled by design (30 KiB/s upgrade label); never ask for unlimited speed. '
        'Only allow an upgrade that is a clear, visible improvement: higher resolution, HDR10 or disc Dolby Vision '
        '(Bluray/Remux) over SDR, lossless or Atmos audio over lossy, or a replacement of a poor release group. '
        'Reject sidegrades, smaller re-encodes of a better file, and anything Plex cannot play: Dolby Vision Profile 5 '
        '(any WEB-DL DV except Hulu) is never wanted, HDR10 is preferred over WEB DV. '
        'Private-tracker ratio matters: prefer freeleech; releases with fewer than 10 seeders are always rejected. '
        'Prefer throttle when unsure.'),
    'import_identity': (
        'Answer match only when title AND year both agree with the matched item. A different year, a remake, a '
        'reboot or a different series is a mismatch. Doubled year tokens (Title.2015.2015) are not a mismatch. '
        'Use unsure whenever the evidence is thin; unsure sends Penn a notification instead of importing.'),
    'dupe_keeper': (
        'Keep the best version: highest resolution, then HDR10 or disc DV over SDR or WEB DV P5, then lossless audio, '
        'then the better release group. Never choose a keeper that would delete a file still seeding on the tracker '
        'inside the minimum seed window, and only library files under /media/movies or /media/tv may be removed. '
        'When versions are close or the evidence is unclear choose keep_both.'),
}




def _valid_decision(kind, obj):
    if not isinstance(obj, dict):
        return None
    action = obj.get('action')
    if not isinstance(action, str) or action not in KINDS[kind]:
        return None
    conf = obj.get('confidence')
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return None
    try:
        conf = float(conf)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(conf) or not 0.0 <= conf <= 1.0:
        return None
    reason = obj.get('reason')
    if not isinstance(reason, str):
        return None
    return {'action': action, 'confidence': conf, 'reason': reason[:300]}


def _last_valid(kind, text):
    """Last JSON object in text that is a fully valid decision (thinking/prose objects are skipped)."""
    found = None
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        start = text.find('{', pos)
        if start < 0:
            return found
        try:
            cand, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            pos = start + 1
            continue
        pos = end
        dec = _valid_decision(kind, cand)
        if dec is not None:
            found = dec


def parse_decision(kind, raw):
    try:
        if kind is None or kind not in KINDS:
            return None
        if not isinstance(raw, (str, bytes)):
            return None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode('utf-8')
            except Exception:
                return None
        text = re.sub(r'<think>.*?</think>', '', raw, flags=re.S | re.I)
        # An opener with no closer means the reply was cut off mid-thought: nothing after it is an answer.
        cut = text.lower().find('<think>')
        if cut >= 0:
            text = text[:cut]
        # Qwen chat templates often emit only the closing tag (the opener lives in the prompt).
        idx = text.lower().rfind('</think>')
        if idx >= 0:
            tail = _last_valid(kind, text[idx + len('</think>'):])
            if tail is not None:
                return tail
        return _last_valid(kind, text)
    except Exception:
        return None


def build_prompt(kind, context):
    if kind is None or kind == '' or kind not in KINDS:
        raise ValueError('unknown kind: %r' % (kind,))
    actions = ', '.join(KINDS[kind])
    ctx_json = json.dumps(context, sort_keys=True, default=str)
    return (
        'Decision kind: ' + str(kind) + '\n'
        'Allowed actions: ' + actions + '\n'
        'Policy: ' + POLICY[kind] + '\n'
        'Context: ' + ctx_json + '\n'
        'Respond ONLY with valid JSON: {"action": <one of ' + actions + '>, "confidence": 0-1, "reason": "..."}'
    )


def decide(kind, context, transport, mode='shadow'):
    if mode == 'off' or mode not in ('off', 'shadow', 'enforce') or transport is None:
        return None
    try:
        return parse_decision(kind, transport(build_prompt(kind, context)))
    except Exception:
        return None


def resolve_action(kind, rules_action, decision, mode='shadow'):
    """Advisory only: callers must still pass the result through apply_hard_rules()."""
    try:
        if mode != 'enforce' or not isinstance(decision, dict) or not isinstance(kind, str) or kind not in KINDS:
            return rules_action
        action = decision.get('action')
        if not isinstance(action, str) or action not in KINDS[kind]:
            return rules_action
        conf = decision.get('confidence')
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            return rules_action
        conf = float(conf)
        if not math.isfinite(conf) or conf > 1 or conf < MIN_CONFIDENCE:
            return rules_action
        return action
    except Exception:
        return rules_action


def log_decision(path, kind, context_summary, rules_action, decision, mode):
    try:
        # An int/bool path would be treated as a file descriptor (True -> stdout) and closed afterwards.
        if isinstance(path, bool) or not isinstance(path, (str, os.PathLike)) or not os.fspath(path):
            return False
        if not isinstance(decision, dict):
            decision = None
        llm_action = decision.get('action') if decision else None
        confidence = decision.get('confidence') if decision else None
        reason = decision.get('reason') if decision else None
        agree = (llm_action == rules_action) if decision is not None else False
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record = {
            'ts': ts,
            'kind': kind,
            'context_summary': context_summary,
            'rules_action': rules_action,
            'llm_action': llm_action,
            'confidence': confidence,
            'reason': reason,
            'mode': mode,
            'agree': agree,
        }
        try:
            line = json.dumps(record, default=str) + '\n'
        except (TypeError, ValueError, RecursionError):
            record['context_summary'] = repr(context_summary)[:2000]
            line = json.dumps(record, default=str) + '\n'
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(line)
        return True
    except Exception:
        return False


_SAFE_ACTION = {'upgrade_gate': 'reject', 'import_identity': 'unsure', 'dupe_keeper': 'keep_both'}
_FALSE_STRINGS = ('', '0', 'false', 'no', 'n', 'off', 'none', 'null')


def _flag(v):
    """Lenient truthiness for facts: 'false'/'0'/'no' strings are False, anything else truthy is True."""
    if isinstance(v, str):
        return v.strip().lower() not in _FALSE_STRINGS
    return bool(v)


def _num(v):
    """Finite float, None when missing; raises ValueError on present-but-unusable values (fail closed)."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError('bool is not a number')
    f = float(v)
    if not math.isfinite(f):
        raise ValueError('non-finite')
    return f


def _library_path(p):
    if not isinstance(p, str) or not p.startswith('/') or '\x00' in p:
        return None
    norm = posixpath.normpath(p)
    if any(norm.startswith(root + '/') for root in LIBRARY_ROOTS):
        return norm
    return None


def apply_hard_rules(kind, action, facts):
    """Deterministic rules the model can never override. Returns (action, rule_or_None).

    facts keys (all optional, missing => no veto from that rule): seed_days (required window, default 21),
    seeders, candidate_dv_web (bool, DV Profile 5 WEB release), year_matches (bool),
    first/second: {path, seeding (bool), seeded_days}. Present-but-malformed facts fail closed.
    """
    try:
        facts = facts if isinstance(facts, dict) else {}
        if kind == 'upgrade_gate' and action in ('allow', 'throttle'):
            # throttle still downloads, so it is vetoed exactly like allow.
            if _flag(facts.get('candidate_dv_web')):
                return 'reject', 'dv_p5_web'
            seeders = _num(facts.get('seeders'))
            if seeders is not None and seeders < MIN_SEEDERS:
                return 'reject', 'min_seeders'
        elif kind == 'import_identity' and action == 'match':
            ym = facts.get('year_matches')
            if ym is not None and not _flag(ym):
                return 'unsure', 'year_mismatch'
        elif kind == 'dupe_keeper' and action in ('keep_first', 'keep_second'):
            try:
                need = _num(facts.get('seed_days'))
            except (TypeError, ValueError):
                need = None
            if need is None or need < 0:
                need = SEED_DAYS_DEFAULT
            keeper = facts.get('first' if action == 'keep_first' else 'second')
            loser = facts.get('second' if action == 'keep_first' else 'first')
            keeper = keeper if isinstance(keeper, dict) else {}
            loser = loser if isinstance(loser, dict) else {}
            loser_path = _library_path(loser.get('path'))
            if loser_path is None:
                return 'keep_both', 'not_library_path'
            if _flag(loser.get('seeding')):
                days = _num(loser.get('seeded_days'))
                if days is None or days < need:
                    return 'keep_both', 'seed_window'
            keeper_path = keeper.get('path')
            if isinstance(keeper_path, str) and keeper_path.startswith('/') and \
                    posixpath.normpath(keeper_path) == loser_path:
                return 'keep_both', 'same_file'
        return action, None
    except Exception:
        if isinstance(kind, str) and kind in _SAFE_ACTION and action != _SAFE_ACTION[kind]:
            return _SAFE_ACTION[kind], 'malformed_facts'
        return action, None
