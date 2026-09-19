"""Two bounded lyric providers; exact recording validation precedes automatic adoption."""
from __future__ import annotations
import hashlib
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def normal(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(text)).casefold() if c.isalnum())


def assess(track, candidate):
    title = normal(track.get('Name', '')) == normal(candidate['title'])
    artist = normal(track.get('Artist', '')) in [normal(a) for a in candidate['artists']]
    duration = float(track.get('Duration') or 0)
    delta = abs(duration - candidate['duration']) if duration and candidate['duration'] else 9999
    album = normal(track.get('Album', '')) == normal(candidate['album'])
    # Do not strip live/remix/TV-size qualifiers. Similar titles are candidates only.
    candidate['safe'] = bool(title and artist and delta <= 2)
    candidate['delta'] = round(delta, 2)
    candidate['rank'] = 50 * title + 30 * artist + 10 * album + max(0, 10 - delta)
    return candidate


class CloudLyrics:
    def __init__(self, data):
        self.root = Path(data) / 'cloud-lyrics'
        self.opener = urllib.request.build_opener()
        self.lock = threading.Lock()
        self.next_request = 0

    def request(self, url, data=None, headers=None):
        # All providers use one sequential, paced client, including manual previews.
        with self.lock:
            if self.next_request - time.monotonic() > 1:
                raise RuntimeError('歌词服务限流，稍后再试')
            time.sleep(max(0, self.next_request - time.monotonic()))
            req = urllib.request.Request(url, data=data, headers=headers or {})
            try:
                with self.opener.open(req, timeout=7) as response:
                    return json.loads(response.read(2 * 1024 * 1024))
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry = exc.headers.get('Retry-After', '60')
                    try:
                        self.next_request = time.monotonic() + max(1, int(retry))
                    except ValueError:
                        self.next_request = time.monotonic() + 60
                raise
            finally:
                self.next_request = max(self.next_request, time.monotonic() + .35)

    def path(self, track):
        identity = [track.get(x) for x in ('Filename', 'Name', 'Artist', 'Album', 'Duration')]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        return self.root / (key + '.json')

    def cached(self, track):
        try:
            saved = json.loads(self.path(track).read_text(encoding='utf-8'))
            if saved.get('selected') or time.time() - saved['time'] < 86400:
                return saved
        except (OSError, ValueError, KeyError):
            pass
        return None

    def store(self, track, result):
        self.root.mkdir(parents=True, exist_ok=True)
        dest = self.path(track)
        temp = dest.with_suffix('.tmp')
        temp.write_text(json.dumps({'time': time.time(), **result}, ensure_ascii=False), encoding='utf-8')
        temp.replace(dest)

    def search(self, track, force=False):
        cached = self.cached(track)
        if cached and not force:
            return cached
        candidates, errors = [], []
        try:
            query = urllib.parse.urlencode({'track_name': track['Name'], 'artist_name': track['Artist']})
            rows = self.request('https://lrclib.net/api/search?' + query, headers={
                'User-Agent': 'JRiverFloating/2.0 (local JRiver companion; https://www.jriver.com/)'})
            for row in rows[:20]:
                if row.get('syncedLyrics'):
                    candidates.append(assess(track, {'source': 'LRCLIB', 'id': str(row['id']),
                        'title': row['trackName'], 'artists': [row['artistName']], 'album': row.get('albumName', ''),
                        'duration': float(row.get('duration') or 0), 'text': row['syncedLyrics']}))
        except Exception as exc:
            errors.append('LRCLIB：' + type(exc).__name__)
        try:
            data = urllib.parse.urlencode({'s': track['Name'] + ' ' + track['Artist'], 'type': 1, 'offset': 0, 'limit': 8}).encode()
            result = self.request('https://music.163.com/api/search/get/web', data, {
                'Referer': 'https://music.163.com/', 'User-Agent': 'Mozilla/5.0'})
            if result.get('code') != 200:
                raise RuntimeError('服务返回错误')
            for row in (result.get('result') or {}).get('songs', [])[:8]:
                candidates.append(assess(track, {'source': '网易云', 'id': str(row['id']), 'title': row['name'],
                    'artists': [a['name'] for a in row.get('artists', [])],
                    'album': row.get('album', {}).get('name', ''), 'duration': float(row.get('duration') or 0) / 1000}))
        except Exception as exc:
            errors.append('网易云：' + type(exc).__name__)
        result = {'candidates': sorted(candidates, key=lambda c: c['rank'], reverse=True), 'errors': errors}
        # A transient total outage should be retried next time, not cached for a day.
        if len(errors) < 2:
            self.store(track, result)
        return result

    def fetch(self, candidate):
        if candidate['source'] == 'LRCLIB':
            return candidate['text']
        if candidate['source'] != '网易云' or not str(candidate['id']).isdigit():
            raise ValueError('未知歌词来源')
        query = urllib.parse.urlencode({'id': candidate['id'], 'lv': -1, 'tv': -1, 'kv': -1})
        result = self.request('https://music.163.com/api/song/lyric?' + query, headers={
            'Referer': 'https://music.163.com/', 'User-Agent': 'Mozilla/5.0'})
        if result.get('code') != 200:
            raise ValueError('歌词获取失败')
        return (result.get('lrc', {}).get('lyric') or '') + '\n' + (result.get('tlyric', {}).get('lyric') or '')

    def select(self, track, candidate, text, manual=False):
        result = self.cached(track) or {}
        if not manual and result.get('selected', {}).get('manual'):
            return result['selected']
        result['selected'] = {'source': candidate['source'], 'id': candidate['id'], 'text': text, 'manual': manual,
                              'title': candidate['title'], 'artists': candidate['artists']}
        self.store(track, result)
        return result['selected']

    def automatic(self, track):
        from floating_bridge import parse_lrc
        result = self.search(track)
        if result.get('selected'):
            return result['selected']
        for candidate in [x for x in result.get('candidates', []) if x['safe']][:2]:
            try:
                text = self.fetch(candidate)
                if parse_lrc(text):
                    return self.select(track, candidate, text)
            except Exception:
                continue
        return {'candidates': len(result.get('candidates', [])), 'errors': result.get('errors', [])}
