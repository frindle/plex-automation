"""llm_decide.py -- advisory local-LLM release decisions with a deterministic fallback."""

import datetime
import json
import math
import os
import re
import unicodedata

KINDS = {
    'upgrade_gate': ('allow', 'throttle', 'reject'),
    'import_identity': ('match', 'mismatch', 'unsure'),
}
MIN_CONFIDENCE = 0.75
MIN_SEEDERS = 10

# Penn's standing preferences, shown to the model. Advisory only: apply_hard_rules() enforces the
# non-negotiable ones in code. Edit here, not in the prompts.
POLICY = {
    'upgrade_gate': (
        'Upgrades are throttled by design (30 KiB/s upgrade label); never ask for unlimited speed. '
        'Facts: quality, size_gb, codec and group describe the candidate (the new release); existing_quality, '
        'existing_size_gb, existing_codec and existing_group describe the file it would replace. '
        'x265 means x265, h265 or HEVC; x264 means x264, h264 or AVC. '
        'Source tiers: Remux and Bluray are the top tier. WEB-DL, WEBRip and HDTV are one tier: none of them is '
        'better or worse than another (Sonarr labels most x265 re-encodes HDTV). '
        'When a Comparison line follows the Context, use it: it is computed from these facts. '
        'Decide in this order and stop at the first rule that applies: '
        '1. Reject when the CANDIDATE has fewer than 10 seeders, is Dolby Vision Profile 5 (any WEB-DL DV except '
        'Hulu; HDR10 is preferred over WEB DV), or cannot play in Plex. Only the candidate counts here: replacing '
        'an existing WEB DV file is fine and is often the point of the upgrade. '
        '2. Allow, even when the candidate is much bigger: a higher resolution; a better source tier (Bluray or '
        'Remux over a WEB-DL, WEBRip or HDTV file); HDR10 or disc Dolby Vision (Bluray/Remux) over SDR; lossless '
        'or Atmos audio over lossy; replacing an AV1 file (many Plex clients cannot play AV1 directly); a PROPER '
        'or REPACK (it fixes a broken release); a replacement of a poor release group. '
        '3. Allow: an x265 candidate replacing an x264 file at the same resolution when it is smaller or about '
        'the same size. A re-encode of a WEB-DL counts: x265 saves space. '
        '4. Allow at the same resolution and source tier: a candidate at least 1.4 times the existing size (a '
        'higher-bitrate encode is better quality), unless it is x264 replacing x265; or a smaller candidate with '
        'the same codec (saves space). '
        '5. Reject: an x264 candidate that is bigger than the x265 file it replaces at the same resolution and '
        'source tier: it costs space for no visible gain. '
        '6. Throttle: same resolution and source tier from a different release group when no rule above '
        'applies. Sonarr and Radarr rank release groups by Penn\'s preferences, which these facts do not show. '
        '7. Anything else is a sidegrade (for example the same group at about the same size): reject it. '
        'Private-tracker ratio matters: prefer freeleech. freeleech is full, partial or no. '
        'For a season pack, episodes is how many episodes it holds, size_gb is the whole pack and '
        'existing_size_gb is the total of the existing files of those episodes (episodes_with_file of them have '
        'one). '
        'A null freeleech, seeders, size_gb or existing_size_gb value means unknown, not zero: never reject a '
        'release for an unknown size.'),
    'import_identity': (
        'Answer match only when title AND year both agree with the matched item. A different year, a remake, a '
        'reboot or a different series is a mismatch. Doubled year tokens (Title.2015.2015) are not a mismatch. '
        'Use unsure whenever the evidence is thin; unsure sends Penn a notification instead of importing. '
        "For Sonarr, queue titles usually carry no year and matched_year is the series' first-air year: judge the "
        'series name (and season/episode if present); a missing year is not a mismatch.'),
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


_RES = re.compile(r'(2160|1080|720|576|480)p', re.I)


def _res(quality):
    if not isinstance(quality, str):
        return None
    m = _RES.search(quality)
    if m:
        return int(m.group(1))
    return 480 if 'dvd' in quality.lower() else None


def _tier(quality):
    """2 = disc (Remux/Bluray), 1 = WEB-DL/WEBRip/HDTV, 0 = DVD/SD, None = unknown."""
    if not isinstance(quality, str):
        return None
    q = quality.lower()
    if 'remux' in q or 'bluray' in q or 'blu-ray' in q:
        return 2
    if 'web' in q or 'hdtv' in q:
        return 1
    if 'dvd' in q or 'sdtv' in q:
        return 0
    return None


_FIX_TAG = re.compile(r'(?<![a-z0-9])(proper|repack|rerip)(?![a-z0-9])', re.I)
_TIER_NAME = {2: 'disc (Bluray/Remux)', 1: 'WEB/HDTV', 0: 'DVD/SD'}


def _codec_family(codec):
    if not isinstance(codec, str):
        return None
    c = re.sub(r'[^a-z0-9]', '', codec.lower())
    if c in ('x265', 'h265', 'hevc'):
        return 'x265'
    if c in ('x264', 'h264', 'avc'):
        return 'x264'
    if c == 'av1':
        return 'AV1'
    return None


def _size_ratio(context):
    """Candidate size over existing size, per episode when only some episodes of a pack have a file."""
    try:
        new, old = _num(context.get('size_gb')), _num(context.get('existing_size_gb'))
    except (TypeError, ValueError):
        return None
    if new is None or old is None or new <= 0 or old <= 0:
        return None
    try:
        eps, have = _num(context.get('episodes')), _num(context.get('episodes_with_file'))
    except (TypeError, ValueError):
        eps = have = None
    if eps and have and 0 < have < eps:
        new, old = new / eps, old / have
    return new / old


def upgrade_comparison(context):
    """Plain-words comparison of the candidate against the file it replaces, computed in code so the model
    does not have to parse quality strings or divide sizes. Empty when nothing is known about the existing file."""
    if not isinstance(context, dict):
        return ''
    parts = []
    r_new, r_old = _res(context.get('quality')), _res(context.get('existing_quality'))
    if r_new and r_old:
        parts.append('resolution %s (%dp vs %dp)' % (
            'higher' if r_new > r_old else 'lower' if r_new < r_old else 'same', r_new, r_old))
    t_new, t_old = _tier(context.get('quality')), _tier(context.get('existing_quality'))
    if t_new is not None and t_old is not None:
        word = 'better' if t_new > t_old else 'worse' if t_new < t_old else 'same'
        parts.append('source tier %s (%s vs %s)' % (word, _TIER_NAME[t_new], _TIER_NAME[t_old]))
    c_new, c_old = _codec_family(context.get('codec')), _codec_family(context.get('existing_codec'))
    if c_new and c_old:
        parts.append('codec %s -> %s' % (c_old, c_new))
    ratio = _size_ratio(context)
    if ratio is not None:
        if ratio >= 1.4:
            word = 'much bigger: a higher-bitrate encode'
        elif ratio > 1.05:
            word = 'a little bigger'
        elif ratio >= 0.95:
            word = 'about the same size'
        else:
            word = 'smaller: saves space'
        parts.append('size %.2f times the existing file (%s)' % (ratio, word))
    g_new, g_old = context.get('group'), context.get('existing_group')
    if isinstance(g_new, str) and g_new.strip() and isinstance(g_old, str) and g_old.strip():
        parts.append('release group %s' % ('same' if g_new.strip().lower() == g_old.strip().lower() else 'different'))
    title = context.get('title')
    if isinstance(title, str) and _FIX_TAG.search(title):
        parts.append('the title marks a PROPER/REPACK (a fixed re-release)')
    hdr_old = context.get('existing_hdr')
    if 'hdr' in context and 'existing_hdr' in context:
        has_old = bool(hdr_old) and str(hdr_old).strip().lower() not in _FALSE_STRINGS
        if _flag(context.get('hdr')) and not has_old:
            parts.append('HDR gained (existing file is SDR)')
        elif has_old and not _flag(context.get('hdr')):
            parts.append('HDR lost (existing file has %s)' % hdr_old)
    if not parts:
        return ''
    return 'Comparison (computed from the facts, candidate vs existing file; trust it over your own reading): ' + \
        '; '.join(parts) + '.'


def build_prompt(kind, context):
    if kind is None or kind == '' or kind not in KINDS:
        raise ValueError('unknown kind: %r' % (kind,))
    actions = ', '.join(KINDS[kind])
    ctx_json = json.dumps(context, sort_keys=True, default=str)
    compare = upgrade_comparison(context) if kind == 'upgrade_gate' else ''
    return (
        'Decision kind: ' + str(kind) + '\n'
        'Allowed actions: ' + actions + '\n'
        'Policy: ' + POLICY[kind] + '\n'
        'Context: ' + ctx_json + '\n'
        + (compare + '\n' if compare else '') +
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
        if not math.isfinite(conf) or conf > 1:
            return rules_action
        # An unsure or mismatch identity answer never imports, however low its confidence: both are the safe side.
        if kind == 'import_identity' and action in ('unsure', 'mismatch'):
            return action
        if conf < MIN_CONFIDENCE:
            return rules_action
        return action
    except Exception:
        return rules_action


def log_decision(path, kind, context_summary, rules_action, decision, mode, extra=None):
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
        if isinstance(extra, dict):
            for k, v in extra.items():
                record.setdefault(k, v)
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


# Spin-off guard: a release named "NCIS Origins ..." matched to the series "NCIS" is a different show.
# The release's show name is the text before the first season/episode tag, year, resolution or
# anime episode marker. Extra words after the matched title make the match unsure, except
# country tags and filler words, which arr titles often leave out ("Shameless US" is "Shameless").
# Sonarr only: for movies year_matches already catches sequels, and releases add subtitles the
# Radarr title leaves out ("Mad Max 2 The Road Warrior" is "Mad Max 2").
_NAME_STOP = re.compile(
    r'(?<![a-z0-9])(?:s\d{1,2}(?:e\d{1,4})?|(?:season|series|part|vol|volume) ?\d+|\d+(?:st|nd|rd|th) season|'
    r'\d{1,2}x\d{2,3}|\d{3,4}p|\d{3}|4k|uhd|complete|ep? ?\d{1,4})(?![a-z0-9])|\s-\s*\d|[\(\[]')
_SPINOFF_IGNORE = frozenset(('us', 'uk', 'au', 'nz', 'ca', 'the', 'a', 'an', 'and', 'of'))
_YEAR_TOKEN = re.compile(r'(?:19|20)\d{2}')


def _title_tokens(s):
    s = ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))
    s = s.lower().replace('&', ' and ')
    s = re.sub(r"['\u2019]", '', s)
    return [t for t in re.sub(r'[^a-z0-9]+', ' ', s).split() if not _YEAR_TOKEN.fullmatch(t)]


def release_show_name(queue_title):
    """Show/movie name at the front of a release title: 'NCIS.Origins.S03E05.1080p' -> 'ncis origins'."""
    t = str(queue_title or '')
    t = re.sub(r'^\s*(?:\[[^\]]*\]\s*)+', '', t)          # leading [Group] tags
    t = re.sub(r'\.(?:mkv|mp4|avi|m4v|ts)$', '', t, flags=re.I)
    t = re.sub(r'[._]+', ' ', t).lower().strip()
    t = ''.join(c for c in unicodedata.normalize('NFKD', t) if not unicodedata.combining(c))
    t = re.sub(r'(?<![a-z0-9])(?:19|20)\d{2}(?![a-z0-9])', '(', t, count=1)  # a year ends the name too
    m = _NAME_STOP.search(t)
    return ' '.join(_title_tokens(t[:m.start()] if m else t))


def spinoff_words(queue_title, matched_title):
    """Words the release's name adds after the matched title ([] when it doesn't extend it)."""
    rel = release_show_name(queue_title).split()
    mt = _title_tokens(str(matched_title or ''))
    if not rel or not mt or rel[:len(mt)] != mt:
        return []
    return [w for w in rel[len(mt):] if w not in _SPINOFF_IGNORE]


_SAFE_ACTION = {'upgrade_gate': 'reject', 'import_identity': 'unsure'}
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


def apply_hard_rules(kind, action, facts):
    """Deterministic rules the model can never override. Returns (action, rule_or_None).

    facts keys (all optional, missing => no veto from that rule): seeders, candidate_dv_web (bool, DV Profile 5
    WEB release), year_matches (bool), queue_title + matched_title (spin-off guard). Present-but-malformed facts
    fail closed.
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
            qt, mt = facts.get('queue_title'), facts.get('matched_title')
            if facts.get('service') != 'Radarr' and isinstance(qt, str) and isinstance(mt, str) and spinoff_words(qt, mt):
                return 'unsure', 'spinoff_title'
        return action, None
    except Exception:
        if isinstance(kind, str) and kind in _SAFE_ACTION and action != _SAFE_ACTION[kind]:
            return _SAFE_ACTION[kind], 'malformed_facts'
        return action, None
