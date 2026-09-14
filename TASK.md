# Task: destructive maintenance endpoints must refuse to MUTATE on a GET (POST-only)

## The defect, CONFIRMED
Read directly in `arr-webhook.py`. These maintenance routes accept `methods=['POST','GET']`
(or `['GET','POST']`) and perform deletions/mutations on a GET:
- `/torrent-purge` (5694), `/purge-unstarted-superseded` (5745), `/complete-orphans` (5572),
  `/incomplete-orphans` (5491), `/run-stalled-seeds` (5474) — mutate when `?apply=1`.
- `/auto-rescue` (4159) and `/run-deluge-repair` (6270) — mutate BY DEFAULT on a bare GET
  (they take `?dry_run=1` to be safe, so a plain GET runs the real action).
A state-changing GET is triggerable by a browser prefetch, a crawler, a bookmark, or a CSRF
`<img src>`, so these must be POST-only for the mutating path while the dry-run report stays on GET.

## The property to satisfy
A request that would CHANGE STATE on one of those seven paths must return HTTP 405 unless
`request.method == 'POST'`. A non-mutating GET (dry-run report: no `?apply=1`; or `?dry_run=1`
for the two dry_run-style routes) must still be allowed through. POST must always be allowed
through the guard.

## Entry points (edit ONLY `arr-webhook.py`)
Only edit `arr-webhook.py`. Add ONE `@app.before_request` hook immediately AFTER the existing
`app.register_blueprint(share_bp)` line (around line 84). Do not change any route's `methods=`
list and do not edit any route body. Do not edit `verify.sh` or `fixture_getpost.py`.

## Exactly what to add (insert verbatim after `app.register_blueprint(share_bp)`)
    _DESTRUCTIVE_GET_GUARD_PATHS = {
        '/torrent-purge', '/purge-unstarted-superseded', '/complete-orphans',
        '/incomplete-orphans', '/run-stalled-seeds', '/auto-rescue', '/run-deluge-repair',
    }

    @app.before_request
    def _require_post_for_mutations():
        if request.method != 'GET':
            return
        if request.path not in _DESTRUCTIVE_GET_GUARD_PATHS:
            return
        apply = request.args.get('apply', '').lower() in ('1', 'true', 'yes')
        dry_run = request.args.get('dry_run', '').lower() in ('1', 'true', 'yes')
        mutating = apply or (request.path in ('/auto-rescue', '/run-deluge-repair') and not dry_run)
        if mutating:
            return jsonify({'ok': False, 'error': 'mutation requires POST'}), 405

## Must contain
- `@app.before_request`
- `_DESTRUCTIVE_GET_GUARD_PATHS`
- `mutation requires POST`

## Loop
Run `bash verify.sh` after every edit and fix the named FAILs until it prints `VERIFY_OK`.
Only edit `arr-webhook.py`; do not edit `verify.sh`.
