import os
import time
from datetime import datetime

import requests

# Rolling cutoff: gap-fills for movies released more than N years ago
# ride the throttled -upgrade lane. Keeps aging automatic — no manual
# year bumps every January.
OLD_GAP_YEARS = int(os.environ.get('OLD_GAP_YEARS', '10'))

DELUGE_URL = os.environ.get('DELUGE_URL')
DELUGE_PASSWORD = os.environ.get('DELUGE_PASSWORD')
RADARR_URL = os.environ.get('RADARR_URL')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY')
RADARR_UPG_LABEL = os.environ.get('RADARR_UPGRADE_LABEL', 'radarr-upgrade')

s = requests.Session()

def deluge_login():
    s.post(f'{DELUGE_URL}/json', json={'method':'auth.login','params':[DELUGE_PASSWORD],'id':1})

def get_torrents():
    r = s.post(f'{DELUGE_URL}/json', json={'method':'core.get_torrents_status','params':[{},['name','label','total_done']],'id':2})
    return r.json().get('result', {})

# Step 1: Purge stalled upgrades
print('Step 1: Purging stalled radarr-upgrade torrents...')
deluge_login()
torrents = get_torrents()
to_remove = []
removed = 0
for h,i in torrents.items():
    if i.get('label') == RADARR_UPG_LABEL and i.get('total_done', 0) < 5*1024*1024:
        print(f'  Purging: {i["name"]} ({i["total_done"]/1024/1024:.1f}MB)')
        to_remove.append(h)
    elif i.get('label') == RADARR_UPG_LABEL:
        print(f'  Skipping in-progress: {i["name"]} ({i["total_done"]/1024/1024:.1f}MB)')
if to_remove:
    batch_size = 5
    for i in range(0, len(to_remove), batch_size):
        batch = to_remove[i:i+batch_size]
        try:
            s.post(f'{DELUGE_URL}/json', json={'method':'core.remove_torrents','params':[batch, False],'id':3}, timeout=15)
            removed += len(batch)
            print(f'  Removed batch {i//batch_size + 1} ({removed}/{len(to_remove)})')
        except Exception as e:
            print(f'  Batch failed: {e}, trying one by one...')
            for h in batch:
                try:
                    s.post(f'{DELUGE_URL}/json', json={'method':'core.remove_torrent','params':[h, False],'id':3}, timeout=10)
                    removed += 1
                except Exception as e2:
                    print(f'  Failed to remove {h}: {e2}')
print(f'Purged {removed} torrents')

# Step 2: Trigger bulk search
print('\nStep 2: Triggering Radarr bulk search...')
movies_r = requests.get(f'{RADARR_URL}/api/v3/movie', headers={'X-Api-Key': RADARR_API_KEY})
movie_ids = [m['id'] for m in movies_r.json() if m.get('monitored')]
if movie_ids:
    r = requests.post(f'{RADARR_URL}/api/v3/command', headers={'X-Api-Key': RADARR_API_KEY}, json={'name': 'MoviesSearch', 'movieIds': movie_ids})
    print(f'Bulk search triggered for {len(movie_ids)} movies (id: {r.json().get("id")})')
else:
    print('No monitored movies found, skipping bulk search')

# Step 3: Wait 90 minutes
print('\nStep 3: Waiting 90 minutes for grabs to complete...')
time.sleep(5400)

# Step 4: Relabel upgrades and queue to bottom
print('\nStep 4: Relabeling upgrades and queuing to bottom...')
deluge_login()
torrents = get_torrents()
radarr_torrents = {h: i for h, i in torrents.items() if i.get('label') == 'radarr'}

movies_r = requests.get(f'{RADARR_URL}/api/v3/movie', headers={'X-Api-Key': RADARR_API_KEY})
movies = {m['id']: m for m in movies_r.json()}

queue_r = requests.get(f'{RADARR_URL}/api/v3/queue', headers={'X-Api-Key': RADARR_API_KEY}, params={'pageSize': 500})
download_to_movie = {rec['downloadId'].lower(): rec.get('movieId') for rec in queue_r.json().get('records', []) if rec.get('downloadId')}

relabeled_hashes = []
for torrent_hash, info in radarr_torrents.items():
    movie_id = download_to_movie.get(torrent_hash.lower())
    if not movie_id:
        continue
    movie = movies.get(movie_id)
    # Two throttle cases:
    #   1. hasFile=True → real upgrade of an existing file
    #   2. hasFile=False + year older than the rolling cutoff → filling an
    #      old library gap; not urgent, don't let it hog bandwidth from
    #      active releases.
    year = movie.get('year') if movie else None
    old_cutoff = datetime.now().year - OLD_GAP_YEARS
    is_old_gap = movie and not movie.get('hasFile') and year and year < old_cutoff
    if movie and (movie.get('hasFile') or is_old_gap):
        reason = 'upgrade' if movie.get('hasFile') else f'old gap-fill (year={year}, cutoff={old_cutoff})'
        print(f'  Relabeling ({reason}): {info.get("name")}')
        # Ensure label exists
        labels_r = s.post(f'{DELUGE_URL}/json', json={'method':'label.get_labels','params':[],'id':4})
        if RADARR_UPG_LABEL not in labels_r.json().get('result', []):
            s.post(f'{DELUGE_URL}/json', json={'method':'label.add','params':[RADARR_UPG_LABEL],'id':5})
        s.post(f'{DELUGE_URL}/json', json={'method':'label.set_torrent','params':[torrent_hash, RADARR_UPG_LABEL],'id':6})
        relabeled_hashes.append(torrent_hash)

if relabeled_hashes:
    s.post(f'{DELUGE_URL}/json', json={'method':'core.queue_bottom','params':[relabeled_hashes],'id':7})
    print(f'Moved {len(relabeled_hashes)} torrents to bottom of queue')

print(f'\nDone: relabeled {len(relabeled_hashes)} upgrade torrents')
