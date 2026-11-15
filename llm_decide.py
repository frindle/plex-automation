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
    'dupe_confirm': ('same', 'different', 'unsure'),
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
        'Also reject an SDR candidate replacing an HDR file (a WEB DV file does not count as HDR here) unless '
        'the candidate has 5.1 or more audio channels, the existing file has fewer, and hdr_surround_available is '
        'false (no HDR release with 5.1 audio is available). '
        'Also reject a candidate that is not x265 (x264, AV1 or any other codec) replacing an x265 file, even at a '
        'higher resolution, a better source tier or with HDR: Penn always keeps HEVC over any other codec. '
        '2. Allow, even when the candidate is much bigger: a higher resolution; a better source tier (Bluray or '
        'Remux over a WEB-DL, WEBRip or HDTV file); HDR10 or disc Dolby Vision (Bluray/Remux) over SDR; lossless '
        'or Atmos audio over lossy; replacing an AV1 file (many Plex clients cannot play AV1 directly); a PROPER '
        'or REPACK (it fixes a broken release); a replacement of a poor release group. '
        '3. Allow: an x265 candidate replacing a file that is not x265, whatever its size, resolution or source '
        'tier: Penn always prefers HEVC. A re-encode of a WEB-DL counts. '
        '4. Allow at the same resolution and source tier: a candidate at least 1.4 times the existing size (a '
        'higher-bitrate encode is better quality); or a smaller candidate with '
        'the same codec (saves space). '
        '5. Throttle: same resolution and source tier from a different release group when no rule above '
        'applies. Sonarr and Radarr rank release groups by Penn\'s preferences, which these facts do not show. '
        '6. Anything else is a sidegrade (for example the same group at about the same size): reject it. '
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
        'series name (and season/episode if present); a missing year is not a mismatch. A later season (S02, S03...) '
        'can air in the first-air year or any year after it. '
        'When a Comparison line follows the Context, use it: it is computed from these facts; no Comparison line '
        'means nothing in it was certain either way, so judge the names yourself. A release that names '
        'exactly the matched title with a year one off the matched year is the same film or show (festival vs. wide '
        'release, a late-December premiere).'),
    'dupe_confirm': (
        'Plex lists two files under one library item. The duplicate cleanup keeps the keeper file and removes the '
        'other file, because Penn never keeps two copies of exactly the same media. Answer same only when both '
        'files hold the same film, or the same episode(s) of the same show, in the same cut: a different '
        'resolution, codec, size, release group or source is still the same media. Answer different when they '
        'are different films or episodes, a remake or another year, or a different cut or edition (Extended, '
        "Director's Cut, Theatrical, Unrated, IMAX and the like). A file holding more episodes than the other "
        '(S01E01E02 vs S01E01) is different. Use unsure whenever the names do not settle it; unsure keeps both '
        'files. When a Comparison line follows the Context, use it: it is computed from these facts.'),
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


_WEB_DV_ONLY = ('dv', 'dovi', 'dolby vision', 'dolbyvision')


def _num_or_none(v):
    try:
        n = _num(v)
    except (TypeError, ValueError):
        return None
    return n if n is not None and n > 0 else None


SURROUND_CHANNELS = 5.0
_SDR_TAG = re.compile(r'(?<![a-z0-9])sdr(?![a-z0-9])', re.I)


def _existing_is_hdr(facts):
    """True when the existing file is HDR that an SDR candidate would lose. A WEB-tier Dolby Vision file is not:
    replacing WEB DV (Profile 5) is fine."""
    old = facts.get('existing_hdr')
    if not old or str(old).strip().lower() in _FALSE_STRINGS:
        return False
    return not (_tier(facts.get('existing_quality')) == 1 and str(old).strip().lower() in _WEB_DV_ONLY)


def _candidate_is_sdr(facts):
    """True only when the candidate is surely SDR: no HDR/DV tag and either 1080p or lower, or an explicit SDR
    tag. An untagged 2160p release may still be HDR, so it is not called SDR."""
    if 'hdr' not in facts or _flag(facts.get('hdr')):
        return False
    title = facts.get('title')
    if isinstance(title, str) and _SDR_TAG.search(title):
        return True
    r = _res(facts.get('quality'))
    return r is not None and r <= 1080


def sdr_over_hdr(facts):
    """An SDR candidate that would replace an HDR file. Needs both hdr facts; missing ones mean no."""
    if not isinstance(facts, dict) or 'hdr' not in facts or 'existing_hdr' not in facts:
        return False
    return _existing_is_hdr(facts) and _candidate_is_sdr(facts)


def non_hevc_over_hevc(facts):
    """A candidate of a known codec other than HEVC that would replace an HEVC file. Penn always keeps HEVC.
    An unknown codec on either side means no (the model decides)."""
    if not isinstance(facts, dict):
        return False
    old = _codec_family(facts.get('existing_codec'))
    new = _codec_family(facts.get('codec'))
    if new is None and isinstance(facts.get('codec'), str) and facts['codec'].strip():
        new = 'other'
    return old == 'x265' and new is not None and new != 'x265'


def sdr_surround_exception_possible(facts):
    """Penn's one exception to keeping HDR: the SDR candidate brings 5.1 audio the HDR file lacks. True when the
    channel facts allow it, so the caller should look for an HDR release with 5.1 audio (hdr_surround_available)."""
    if not sdr_over_hdr(facts):
        return False
    new = _num_or_none(facts.get('audio_channels'))
    old = _num_or_none(facts.get('existing_audio_channels'))
    return new is not None and new >= SURROUND_CHANNELS and (old is None or old < SURROUND_CHANNELS)


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
        note = ' (loses HEVC: Penn always keeps HEVC)' if c_old == 'x265' and c_new != 'x265' else \
            ' (gains HEVC: Penn always prefers HEVC)' if c_new == 'x265' and c_old != 'x265' else ''
        parts.append('codec %s -> %s%s' % (c_old, c_new, note))
    ratio = _size_ratio(context)
    if ratio is None and context.get('size_gb') is None and _num_or_none(context.get('existing_size_gb')):
        parts.append('candidate size unknown (never reject for an unknown size)')
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
            if t_old == 1 and str(hdr_old).strip().lower() in _WEB_DV_ONLY:
                parts.append('drops the existing WEB Dolby Vision file (replacing WEB DV is fine: not a loss)')
            else:
                parts.append('HDR lost (existing file has %s)' % hdr_old)
    a_new, a_old = _num_or_none(context.get('audio_channels')), _num_or_none(context.get('existing_audio_channels'))
    if a_new is not None and a_old is not None and (a_new >= SURROUND_CHANNELS) != (a_old >= SURROUND_CHANNELS):
        parts.append('audio %s (%g vs %g channels)' % (
            'gains surround' if a_new >= SURROUND_CHANNELS else 'loses surround', a_new, a_old))
    if context.get('hdr_surround_available') is True:
        parts.append('an HDR release with 5.1 audio is available')
    elif context.get('hdr_surround_available') is False:
        parts.append('no HDR release with 5.1 audio is available')
    if not parts:
        return ''
    return 'Comparison (computed from the facts, candidate vs existing file; trust it over your own reading): ' + \
        '; '.join(parts) + '.'


def identity_comparison(facts):
    """Plain-words name and year check for import_identity, computed in code. It states agreement only when the
    whole name agrees and the year does not conflict, and it states a year conflict whenever plex's year check
    would fail. A partial agreement (same year, different name) says nothing: the model judges it alone, and
    apply_hard_rules() still checks every "match". Empty when there is nothing certain to say."""
    if not isinstance(facts, dict):
        return ''
    qt, mt = facts.get('queue_title'), facts.get('matched_title')
    if not (isinstance(qt, str) and isinstance(mt, str)):
        return ''
    year = facts.get('matched_year')
    try:
        year = None if isinstance(year, bool) else int(year)
    except (TypeError, ValueError):
        year = None
    years = sorted({int(y) for y in _RELEASE_YEAR.findall(_DAILY_DATE.sub(' ', qt))})
    shown = '/'.join(map(str, years))
    ym = facts.get('year_matches')
    off_by_one = _same_name_year_off_by_one(facts)
    if ym is not None and not _flag(ym) and not off_by_one:
        part = ('release year %s is not the matched year %s' % (shown, year)) if years and year else \
            'the release year does not match the matched year'
        return ('Comparison (computed from the facts; trust it over your own reading): ' + part +
                '; a title from another year is usually a remake, reboot, sequel or a different show or film.')
    name = release_show_name(qt)
    mt_name = ' '.join(_title_tokens(mt))
    if name and name != mt_name and _core(name.split()) == _core(mt_name.split()):
        tags_r = sorted(_COUNTRIES.intersection(name.split()))
        tags_m = sorted(_COUNTRIES.intersection(mt_name.split()))
        if tags_r and tags_m and tags_r != tags_m:
            return ('Comparison (computed from the facts; trust it over your own reading): country tags differ '
                    '(release %s, matched title %s): a different version of the show.' % (
                        '/'.join(tags_r).upper(), '/'.join(tags_m).upper()))
        if tags_r != tags_m:
            return ('Comparison (computed from the facts; trust it over your own reading): the names agree apart '
                    'from the country tag %s, which only the %s carries.' % (
                        '/'.join(tags_r or tags_m).upper(), 'release' if tags_r else 'matched title'))
        return ''
    if not name or name != mt_name:
        return ''
    parts = ['the release names exactly the matched title']
    if off_by_one:
        parts.append('release year %s is one off the matched year %d: not a year mismatch' % (shown, year))
    elif year and years and year in years:
        parts.append('year %d agrees' % year)
    elif year and not years and facts.get('service') == 'Sonarr':
        parts.append('it carries no year (normal for Sonarr; %d is the first-air year)' % year)
    elif years:
        return ''
    return 'Comparison (computed from the facts; trust it over your own reading): ' + '; '.join(parts) + '.'


def build_prompt(kind, context):
    if kind is None or kind == '' or kind not in KINDS:
        raise ValueError('unknown kind: %r' % (kind,))
    actions = ', '.join(KINDS[kind])
    ctx_json = json.dumps(context, sort_keys=True, default=str)
    compare = upgrade_comparison(context) if kind == 'upgrade_gate' else \
        identity_comparison(context) if kind == 'import_identity' else \
        dupe_comparison(context) if kind == 'dupe_confirm' else ''
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
        # Likewise a dupe answer other than same keeps both files.
        if kind == 'dupe_confirm' and action in ('unsure', 'different'):
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


def _core(words):
    """Words with country tags and filler words dropped ('the traitors us' -> ['traitors'])."""
    return [w for w in words if w not in _SPINOFF_IGNORE]


def spinoff_words(queue_title, matched_title):
    """Words the release's name adds after the matched title ([] when it doesn't extend it).
    Compared without country tags and filler words, so 'The Traitors New Blood' extends 'The Traitors (US)'."""
    rel = _core(release_show_name(queue_title).split())
    mt = _core(_title_tokens(str(matched_title or '')))
    if not rel or not mt or rel[:len(mt)] != mt:
        return []
    return rel[len(mt):]


def reverse_spinoff_words(queue_title, matched_title):
    """Words the matched title adds after the release's name ('NCIS' release matched to 'NCIS: Origins',
    'The Traitors US' to 'The Traitors: New Blood')."""
    rel = _core(release_show_name(queue_title).split())
    mt = _core(_title_tokens(str(matched_title or '')))
    # A name with no real word ('The.100' parses to 'the') says nothing about which show it is.
    if not rel or not mt or mt[:len(rel)] != rel:
        return []
    return mt[len(rel):]


_COUNTRIES = frozenset(('us', 'uk', 'au', 'nz', 'ca'))


def country_differs(queue_title, matched_title):
    """True when both names carry a country tag and the tags differ: 'The Traitors UK' is not 'The Traitors (US)'."""
    rel = _COUNTRIES.intersection(release_show_name(queue_title).split())
    mt = _COUNTRIES.intersection(_title_tokens(str(matched_title or '')))
    return bool(rel and mt and rel != mt)


_ARTICLES = frozenset(('the', 'a', 'an'))
# Digits releases use in place of letters: 'PLUR1BUS', 'S3VERANCE'.
_LOOKALIKE = str.maketrans('013457', 'oieast')


# Release names Penn confirmed are the matched title, for pairs no general rule can safely accept:
# (release show name, matched title), both as _title_tokens joined by spaces.
_CONFIRMED_ALIASES = frozenset((
    ('apollo has fallen', 'has fallen'),  # Penn 2026-11-14: Sonarr's 'Has Fallen' is released as 'Apollo Has Fallen'
))


def confirmed_alias(queue_title, matched_title):
    """True when Penn has confirmed this release name is the matched title."""
    return (release_show_name(queue_title), ' '.join(_title_tokens(str(matched_title or '')))) in _CONFIRMED_ALIASES


def _is_tail(short, longer):
    """`short` is the last words of `longer` and has 2+ real words: 'has fallen' / 'apollo has fallen'."""
    return len(short) < len(longer) and len(_core(short)) >= 2 and longer[-len(short):] == short


# A possessive brand in front of the real title: "Tom Clancy's ", "Marvel's ", "Dr. Seuss' ".
_POSSESSIVE_LEAD = re.compile(r"^[^:]{0,40}?\w['\u2019]s?\s+")


def _one_letter_dropped(short, full):
    """short is full with exactly one letter left out ('ioness' / 'lioness'); full needs 5+ letters."""
    return len(full) >= 5 and len(short) == len(full) - 1 and \
        any(full[:i] + full[i + 1:] == short for i in range(len(full)))


def first_word_differs(queue_title, matched_title, allow_tail=False):
    """True when the release name and the matched title start with different words (articles skipped).
    Back-test: 0 of 150 real matches differ here, so a 'match' across different first words is never trusted.
    Exceptions: digits standing in for letters ('PLUR1BUS' is 'Pluribus'); with allow_tail (the caller has a
    matching year), one name is the other's last 2+ real words ('Mandalorian and Grogu' 2026 is 'Star Wars:
    The Mandalorian and Grogu' 2026; without a year it is not trusted, since a franchise name in front of an
    older show's name is a spin-off: 'Fear the Walking Dead', 'Dexter New Blood', '9-1-1 Lone Star'); also with
    allow_tail, a release that leaves one letter out of the first word and is otherwise the same name
    ('ioness 2023' is 'Lioness' 2023);
    the release drops a possessive brand and the rest is the same title of 2+ real words ('Without Remorse'
    is "Tom Clancy's Without Remorse"; a lone 'Anatomy' is not "Grey's Anatomy", a lone 'Daredevil' is not
    "Marvel's Daredevil")."""
    rel = [w for w in release_show_name(queue_title).split() if w not in _ARTICLES]
    mt = [w for w in _title_tokens(str(matched_title or '')) if w not in _ARTICLES]
    if not (rel and mt and rel[0] != mt[0]):
        return False
    if rel[0].translate(_LOOKALIKE) == mt[0].translate(_LOOKALIKE):
        return False
    if ''.join(rel) == ''.join(mt):  # the whole name, spaced differently: 'SpiderMan' is 'Spider-Man'
        return False
    if allow_tail and (_is_tail(mt, rel) or _is_tail(rel, mt)):
        return False
    if allow_tail and rel[1:] == mt[1:] and _one_letter_dropped(rel[0], mt[0]):
        return False  # a typo in the release name with a matching year: 'ioness 2023 S03' is 'Lioness' (2023)
    lead = _POSSESSIVE_LEAD.match(str(matched_title))
    if lead:
        rest = [w for w in _title_tokens(str(matched_title)[lead.end():]) if w not in _ARTICLES]
        if rest == rel and len(_core(rest)) >= 2:
            return False
    return True


def _same_words(a, b):
    """Two word runs name the same thing: equal once spaces go ('pd' / 'p d', 'shield' / 's h i e l d')
    or one is the initials of the other ('svu' / 'special victims unit', 'la' / 'los angeles')."""
    ja, jb = ''.join(a), ''.join(b)
    return ja == jb or (len(a) == 1 and len(b) > 1 and ja == ''.join(w[0] for w in b)) or \
        (len(b) == 1 and len(a) > 1 and jb == ''.join(w[0] for w in a))


def title_diverges(queue_title, matched_title):
    """True when the release name and the matched title start the same, then go different ways:
    'Chicago PD' matched to 'Chicago Fire', 'Star Trek Discovery' to 'Star Trek: Strange New Worlds'.
    Country tags and filler words are skipped; spelling ('P.D.') and initialisms ('SVU') still match.
    A name that only extends the other is the spin-off guards' job, a different first word is first_word_differs'."""
    rel = [w for w in release_show_name(queue_title).split() if w not in _SPINOFF_IGNORE]
    mt = [w for w in _title_tokens(str(matched_title or '')) if w not in _SPINOFF_IGNORE]
    if not rel or not mt or rel[0] != mt[0]:
        return False
    k = 0
    while k < min(len(rel), len(mt)) and rel[k] == mt[k]:
        k += 1
    ra, mb = rel[k:], mt[k:]
    return bool(ra and mb) and not _same_words(ra, mb)


# Release years, with a daily show's air date (Show.2026.10.30) left out, as arr-webhook reads them.
_DAILY_DATE = re.compile(r'(?<!\d)(19|20)\d{2}[.\- ]\d{2}[.\- ]\d{2}(?!\d)')
_RELEASE_YEAR = re.compile(r'(?<![a-z0-9])((?:19|20)\d{2})(?![a-z0-9])', re.I)


def _same_name_year_off_by_one(facts):
    """True when the release names exactly the matched title and its year is one off the matched year.
    The release and the database often disagree by a year (festival vs. wide release, a late-December
    premiere); with the whole name the same that is still the same show or film. Any other year gap,
    or any difference in the name, still fails the year check."""
    qt, mt, year = facts.get('queue_title'), facts.get('matched_title'), facts.get('matched_year')
    if not (isinstance(qt, str) and isinstance(mt, str)) or isinstance(year, bool):
        return False
    try:
        year = int(year)
    except (TypeError, ValueError):
        return False
    name = release_show_name(qt)
    if not name or name != ' '.join(_title_tokens(mt)):
        return False
    years = {int(y) for y in _RELEASE_YEAR.findall(_DAILY_DATE.sub(' ', qt))}
    return year not in years and any(abs(y - year) == 1 for y in years)


# dupe_confirm: two files Plex lists under one item, the keeper and the copy the cleanup would remove.
_EPISODE = re.compile(r'(?<![a-z0-9])s(\d{1,3})((?:[ ._-]?e\d{1,4})+)(?![0-9])', re.I)
_EP_NUM = re.compile(r'e(\d{1,4})', re.I)
_EP_RANGE = re.compile(r'(?<![a-z0-9])s(\d{1,3})[ ._]?e(\d{1,4})[ ._]?-[ ._]?e?(\d{1,4})(?![0-9])', re.I)
_PLEX_EDITION = re.compile(r'\{edition-([^}]+)\}', re.I)
_EDITIONS = (
    ('extended', re.compile(r'(?<![a-z0-9])extended(?![a-z0-9])', re.I)),
    ('directors cut', re.compile(r"(?<![a-z0-9])director'?s?[ ._-]?cut(?![a-z0-9])", re.I)),
    ('theatrical', re.compile(r'(?<![a-z0-9])theatrical(?![a-z0-9])', re.I)),
    ('unrated', re.compile(r'(?<![a-z0-9])(unrated|uncut|uncensored)(?![a-z0-9])', re.I)),
    ('imax', re.compile(r'(?<![a-z0-9])imax(?![a-z0-9])', re.I)),
    ('final cut', re.compile(r'(?<![a-z0-9])final[ ._-]?cut(?![a-z0-9])', re.I)),
    ('ultimate', re.compile(r'(?<![a-z0-9])ultimate[ ._-]?(cut|edition)(?![a-z0-9])', re.I)),
    ('special edition', re.compile(r'(?<![a-z0-9])special[ ._-]?edition(?![a-z0-9])', re.I)),
    ('redux', re.compile(r'(?<![a-z0-9])redux(?![a-z0-9])', re.I)),
    ('alternate', re.compile(r'(?<![a-z0-9])alternate[ ._-]?(cut|ending|version)(?![a-z0-9])', re.I)),
    ('rogue cut', re.compile(r'(?<![a-z0-9])rogue[ ._-]?cut(?![a-z0-9])', re.I)),
)
# Durations further apart than this (minutes, and a share of the longer one) are not one cut: a PAL speed-up
# is about 4%, a re-encode a minute or two.
DUPE_DURATION_MIN = 5.0
DUPE_DURATION_SHARE = 0.08


def file_episodes(name):
    """Episodes a file name holds, as (season, episode) pairs: S01E01, S01E01E02, S01E01-E03. Empty when the
    name carries no SxxEyy."""
    if not isinstance(name, str):
        return frozenset()
    out = set()
    for m in _EP_RANGE.finditer(name):
        a, b = int(m.group(2)), int(m.group(3))
        if a <= b <= a + 50:
            out.update((int(m.group(1)), e) for e in range(a, b + 1))
    for m in _EPISODE.finditer(name):
        out.update((int(m.group(1)), int(e)) for e in _EP_NUM.findall(m.group(2)))
    return frozenset(out)


def file_years(name):
    """Release years in a file name (a daily show's air date left out)."""
    if not isinstance(name, str):
        return frozenset()
    return frozenset(int(y) for y in _RELEASE_YEAR.findall(_DAILY_DATE.sub(' ', name)))


def file_editions(name):
    """Edition tags in a file name (Plex {edition-...} tags too), normalised; empty for a plain release."""
    if not isinstance(name, str):
        return frozenset()
    out = set()
    for m in _PLEX_EDITION.finditer(name):
        tag = ' '.join(_title_tokens(m.group(1)))
        hit = next((k for k, rx in _EDITIONS if rx.search(m.group(1))), None)
        out.add(hit or tag)
    rest = _PLEX_EDITION.sub(' ', name)
    out.update(k for k, rx in _EDITIONS if rx.search(rest))
    out.discard('')
    return frozenset(out)


def _ep_label(eps):
    return ', '.join('S%02dE%02d' % e for e in sorted(eps))


def dupe_conflict(facts):
    """(rule, words) when the two files are surely not the same media, from their names and durations; None
    otherwise. Checks: episodes, years more than one apart, edition tags, durations."""
    if not isinstance(facts, dict):
        return None
    kf, lf = facts.get('keeper_file'), facts.get('loser_file')
    ke, le = file_episodes(kf), file_episodes(lf)
    if ke and le and ke != le:
        return 'episode_differs', 'episodes differ (keeper %s, other %s)' % (_ep_label(ke), _ep_label(le))
    ky, ly = file_years(kf), file_years(lf)
    if ky and ly and not any(abs(a - b) <= 1 for a in ky for b in ly):
        return 'year_differs', 'years differ (keeper %s, other %s)' % (
            '/'.join(map(str, sorted(ky))), '/'.join(map(str, sorted(ly))))
    ked, led = file_editions(kf), file_editions(lf)
    if ked != led:
        return 'edition_differs', 'edition tags differ (keeper %s, other %s)' % (
            ', '.join(sorted(ked)) or 'none', ', '.join(sorted(led)) or 'none')
    kd, ld = _num_or_none(facts.get('keeper_duration_min')), _num_or_none(facts.get('loser_duration_min'))
    if kd and ld and abs(kd - ld) > max(DUPE_DURATION_MIN, DUPE_DURATION_SHARE * max(kd, ld)):
        return 'duration_differs', 'running times differ (keeper %g min, other %g min)' % (kd, ld)
    return None


def dupe_comparison(facts):
    """Plain-words comparison of the two files for dupe_confirm, computed in code. Empty when nothing is known."""
    if not isinstance(facts, dict):
        return ''
    conflict = dupe_conflict(facts)
    if conflict:
        return ('Comparison (computed from the facts; trust it over your own reading): %s: not the same media.'
                % conflict[1])
    parts = []
    kf, lf = facts.get('keeper_file'), facts.get('loser_file')
    ke, le = file_episodes(kf), file_episodes(lf)
    if ke and ke == le:
        parts.append('same episodes (%s)' % _ep_label(ke))
    ky, ly = file_years(kf), file_years(lf)
    if ky and ly:
        parts.append('years agree (%s)' % '/'.join(map(str, sorted(ky | ly))))
    if not file_editions(kf) and not file_editions(lf):
        parts.append('neither name carries an edition tag')
    else:
        parts.append('same edition tags (%s)' % ', '.join(sorted(file_editions(kf))))
    kd, ld = _num_or_none(facts.get('keeper_duration_min')), _num_or_none(facts.get('loser_duration_min'))
    if kd and ld:
        parts.append('running times %g and %g min (close)' % (kd, ld))
    return ('Comparison (computed from the facts; trust it over your own reading): ' + '; '.join(parts) +
            '. Quality differences (resolution, codec, size, group) do not make the media different.')


_SAFE_ACTION = {'upgrade_gate': 'reject', 'import_identity': 'unsure', 'dupe_confirm': 'unsure'}
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
    WEB release), codec + existing_codec (non-HEVC over HEVC), hdr + existing_hdr (+ audio_channels, existing_audio_channels,
    hdr_surround_available: SDR over HDR), year_matches (bool), queue_title + matched_title (spin-off, reverse spin-off, country,
    first-word and diverging-title guards), keeper_file + loser_file (+ keeper_duration_min, loser_duration_min:
    dupe_confirm episode, year, edition and running-time guards). Present-but-malformed facts
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
            if non_hevc_over_hevc(facts):
                return 'reject', 'non_hevc_over_hevc'
            # Keep HDR: SDR replaces it only for 5.1 audio the HDR file lacks, and only when no HDR release has
            # 5.1 (hdr_surround_available is False; unknown keeps the HDR file).
            if sdr_over_hdr(facts) and not (sdr_surround_exception_possible(facts)
                                            and facts.get('hdr_surround_available') is False):
                return 'reject', 'sdr_over_hdr'
        elif kind == 'import_identity' and action == 'match':
            ym = facts.get('year_matches')
            if ym is not None and not _flag(ym) and not _same_name_year_off_by_one(facts):
                return 'unsure', 'year_mismatch'
            qt, mt = facts.get('queue_title'), facts.get('matched_title')
            if isinstance(qt, str) and isinstance(mt, str) and not confirmed_alias(qt, mt):
                if facts.get('service') != 'Radarr' and spinoff_words(qt, mt):
                    return 'unsure', 'spinoff_title'
                if facts.get('service') != 'Radarr' and reverse_spinoff_words(qt, mt):
                    return 'unsure', 'spinoff_title_reverse'
                if facts.get('service') != 'Radarr' and country_differs(qt, mt):
                    return 'unsure', 'title_country'
                if first_word_differs(qt, mt, allow_tail=ym is not None and _flag(ym)):
                    return 'unsure', 'title_first_word'
                if facts.get('service') != 'Radarr' and title_diverges(qt, mt):
                    return 'unsure', 'title_diverges'
        elif kind == 'dupe_confirm' and action == 'same':
            conflict = dupe_conflict(facts)
            if conflict:
                return 'different', conflict[0]
        return action, None
    except Exception:
        if isinstance(kind, str) and kind in _SAFE_ACTION and action != _SAFE_ACTION[kind]:
            return _SAFE_ACTION[kind], 'malformed_facts'
        return action, None
