"""supersede_policy.py -- Penn's rule for torrent supersede decisions."""


def _check_upgrade_completes(new_torrent):
    """Return True if new_torrent has finished downloading (progress >= 100)."""
    progress = new_torrent.get('progress')
    if progress is None:
        return False
    return float(progress) >= 100


def should_supersede(event, old_torrent, new_torrent):
    """Return True only when an upgrade torrent has completed and been imported.

    Parameters
    ----------
    event : dict or None
        {'eventType': str, 'isUpgrade': bool, 'downloadId': str}
    old_torrent : dict or None
        {'hash': str, 'progress': float, 'label': str}
    new_torrent : dict or None
        {'hash': str, 'progress': float, 'label': str}

    Returns False for Grab, queue-add, progress 99.9, isUpgrade false/missing,
    new_torrent None/{} , same hash, {} inputs; never raises on None/empty/missing keys.
    """
    try:
        if not event or not isinstance(event, dict):
            return False
        if not old_torrent or not isinstance(old_torrent, dict):
            return False
        if not new_torrent or not isinstance(new_torrent, dict):
            return False

        event_type = event.get('eventType')
        if event_type not in ('Import', 'Download'):
            return False

        is_upgrade = event.get('isUpgrade')
        if not is_upgrade:
            return False

        if 'downloadId' not in event:
            return False

        old_hash = old_torrent.get('hash')
        if not old_hash:
            return False

        new_hash = new_torrent.get('hash')
        if not new_hash:
            return False

        if old_hash == new_hash:
            return False

        if not _check_upgrade_completes(new_torrent):
            return False

        return True
    except Exception:
        return False


def find_unimported_superseded(superseded, upgrade_history):
    """Return hashes of superseded torrents whose replacement was never imported.

    Parameters
    ----------
    superseded : list of dict
        Torrents currently labeled 'superseded'.
    upgrade_history : list of dict
        Radarr/Sonarr import-history dicts with replacedInfoHash.

    A superseded torrent is IMPORTED (not reported) iff some history entry has
    replacedInfoHash equal to its hash (case-insensitive); otherwise its hash is
    reported, preserving input order, no duplicates.
    """
    if not superseded:
        return []

    # Build set of replaced hashes (case-insensitive) from history
    replaced_hashes = set()
    if upgrade_history:
        for entry in upgrade_history:
            rh = entry.get('replacedInfoHash')
            if rh:
                replaced_hashes.add(str(rh).lower())

    result = []
    seen = set()
    for torrent in superseded:
        h = torrent.get('hash')
        if not h:
            continue
        h_lower = str(h).lower()
        if h_lower not in replaced_hashes and h_lower not in seen:
            result.append(h)
            seen.add(h_lower)

    return result
