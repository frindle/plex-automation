"""llm_decide.py -- advisory local-LLM release decisions with a deterministic fallback."""

import datetime
import json
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
        'Private-tracker ratio matters: prefer freeleech and releases with 10+ seeders. Prefer throttle when unsure.'),
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
        text = re.sub(r'<think>.*?</think>', '', raw, flags=re.S)
        obj = None
        decoder = json.JSONDecoder()
        pos = 0
        while True:
            start = text.find('{', pos)
            if start < 0:
                break
            try:
                cand, end = decoder.raw_decode(text, start)
            except ValueError:
                pos = start + 1
                continue
            pos = end
            if isinstance(cand, dict) and 'action' in cand:
                obj = cand
        if obj is None:
            return None
        action = obj.get('action')
        if not isinstance(action, str) or action not in KINDS[kind]:
            return None
        conf = obj.get('confidence')
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            return None
        if conf < 0 or conf > 1:
            return None
        reason = obj.get('reason')
        if not isinstance(reason, str):
            return None
        reason = reason[:300]
        return {'action': action, 'confidence': float(conf), 'reason': reason}
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
    if mode != 'enforce':
        return rules_action
    if not isinstance(decision, dict):
        return rules_action
    k = kind if kind in KINDS else None
    if k is None:
        return rules_action
    action = decision.get('action')
    if action not in KINDS[k]:
        return rules_action
    conf = decision.get('confidence')
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        return rules_action
    if conf < MIN_CONFIDENCE:
        return rules_action
    return decision['action']


def log_decision(path, kind, context_summary, rules_action, decision, mode):
    try:
        if not path:
            return False
        if isinstance(context_summary, str):
            summary_str = context_summary
        else:
            summary_str = str(context_summary)
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
        line = json.dumps(record, default=str) + '\n'
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(line)
        return True
    except OSError:
        return False


def apply_hard_rules(kind, action, facts):
    """Deterministic rules the model can never override. Returns (action, rule_or_None).

    facts keys (all optional, missing => no veto from that rule): seed_days (required window, default 21),
    seeders, candidate_dv_web (bool, DV Profile 5 WEB release), year_matches (bool),
    first/second: {path, seeding (bool), seeded_days}.
    """
    try:
        facts = facts if isinstance(facts, dict) else {}
        if kind == 'upgrade_gate' and action == 'allow':
            if facts.get('candidate_dv_web') is True:
                return 'reject', 'dv_p5_web'
            seeders = facts.get('seeders')
            if isinstance(seeders, (int, float)) and not isinstance(seeders, bool) and seeders < MIN_SEEDERS:
                return 'reject', 'min_seeders'
        elif kind == 'import_identity' and action == 'match' and facts.get('year_matches') is False:
            return 'unsure', 'year_mismatch'
        elif kind == 'dupe_keeper' and action in ('keep_first', 'keep_second'):
            need = facts.get('seed_days', SEED_DAYS_DEFAULT)
            loser = facts.get('second' if action == 'keep_first' else 'first')
            loser = loser if isinstance(loser, dict) else {}
            if not str(loser.get('path', '')).startswith(LIBRARY_ROOTS):
                return 'keep_both', 'not_library_path'
            if loser.get('seeding') and (loser.get('seeded_days') is None or loser['seeded_days'] < need):
                return 'keep_both', 'seed_window'
    except Exception:
        pass
    return action, None
