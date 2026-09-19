"""JRiver-only transport and exact-recording lyric lookup for the desktop companion."""
from __future__ import annotations

import bisect
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from track_reviews import review_for_track
from settings import SETTINGS


def setting(name, default):
    if os.environ.get(name):
        return os.environ[name]
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            pass
    return default


MCWS = os.environ.get('JRIVER_MCWS_URL', SETTINGS.get('mcws_url', 'http://127.0.0.1:52199/MCWS/v1')).rstrip('/')
DATA = Path(os.environ.get('JRIVER_CARD_DATA', str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'JRiverFloatingCard')))


class Bridge:
    def __init__(self, base=MCWS):
        self.base = base
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('Invalid MCWS URL')
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None,base,SETTINGS.get('mcws_username',''),SETTINGS.get('mcws_password',''))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                      urllib.request.HTTPBasicAuthHandler(manager),urllib.request.HTTPDigestAuthHandler(manager))

    def get(self, endpoint, **params):
        url = self.base + '/' + endpoint + '?' + urllib.parse.urlencode(params)
        with self.opener.open(url, timeout=4) as response:
            return response.read(8 * 1024 * 1024)

    def items(self, endpoint, **params):
        root = ET.fromstring(self.get(endpoint, **params))
        if root.get('Status') != 'OK':
            raise RuntimeError(root.get('Message') or 'JRiver 拒绝了操作')
        return {x.get('Name'): x.text or '' for x in root.findall('Item')}

    def status(self):
        return self.items('Playback/Info', Zone='-1')

    def details(self, key):
        rows = json.loads(self.get('File/GetInfo', File=key, Action='JSON',
                                   Fields='Filename,Name,Artist,Album,Duration,Rating,Lyrics'))
        if not rows:
            raise RuntimeError('当前曲目信息不可用')
        track = rows[0]
        track['review'] = review_for_track(track)
        track['lyrics'] = lyrics_for_track(track)
        return track

    def cover(self, key):
        return self.get('File/GetImage', File=key, Width=240, Height=240, Square=1)

    def command(self, action, *, key=None, value=None, zone='-1'):
        # No arbitrary endpoint or field is accepted by the UI.
        if action in ('Previous', 'Next', 'Play'):
            return self.items('Playback/' + action, Zone=zone)
        if action == 'Pause':
            return self.items('Playback/Pause', State=-1, Zone=zone)
        if action == 'seek':
            current = self.status()
            if str(current.get('FileKey')) != str(key) or str(current.get('ZoneID')) != str(zone):
                raise RuntimeError('曲目或播放区域已切换，请重试')
            position = max(0, min(int(value), int(current.get('DurationMS', 0))))
            return self.items('Playback/Position', Position=position, Mode='ms', Zone=zone)
        if action == 'rating' and key is not None and int(value) in range(6):
            return self.set_rating(key, value)
        raise ValueError('不支持的操作')

    def set_rating(self, key, value, expected=None):
        if os.name != 'nt' or urllib.parse.urlsplit(self.base).hostname not in ('localhost','127.0.0.1','::1'):
            raise RuntimeError('星级写入需要本机 Windows JRiver；远程服务器仅支持播放控制')
        if not str(key).isdigit() or int(value) not in range(6):
            raise ValueError('无效的星级或曲目')
        rows = json.loads(self.get('File/GetInfo', File=key, Action='JSON', Fields='Filename,Name,Rating'))
        if not rows or str(rows[0].get('Key')) != str(key):
            raise RuntimeError('JRiver 曲目已不可用')
        track = rows[0]
        before = int(track.get('Rating') or 0)
        if expected is not None and before != int(expected):
            raise RuntimeError('星级已在其他地方修改，未覆盖该修改')
        # The HTTP session is read-only. Use JRiver's existing local COM automation,
        # with exact library/file identity and a compare-before-write guard.
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        args = [str(powershell), '-NoProfile', '-NonInteractive', '-File',
                str(Path(__file__).with_name('Set-JRiver-FileRating.ps1')),
                '-FileKey', str(key), '-Rating', str(int(value)),
                '-ExpectedFilename', track['Filename'], '-ExpectedRating', str(before)]
        try:
            done = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                  timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('评分写入结果未确认，请先检查 JRiver 中的星级') from exc
        try:
            result = json.loads(done.stdout.lstrip('\ufeff'))
        except ValueError as exc:
            raise RuntimeError('本机 JRiver 评分接口未返回有效结果') from exc
        if done.returncode or result.get('error'):
            raise RuntimeError('评分未保存：' + result.get('error', 'JRiver COM 操作失败'))
        # Read back from MCWS as well, so the displayed library and written library agree.
        for _ in range(5):
            check = json.loads(self.get('File/GetInfo', File=key, Action='JSON', Fields='Rating'))
            if check and int(check[0].get('Rating') or 0) == int(value):
                result['name'] = track.get('Name', '')
                return result
            time.sleep(.1)
        raise RuntimeError('JRiver 写入已返回，但重新读取星级未一致，请检查播放器')


def parse_lrc(text):
    """Keep bilingual same-time lines together and honor standard millisecond offset."""
    offset_match = re.search(r'\[offset:([+-]?\d+)\]', text, re.I)
    offset = int(offset_match[1]) if offset_match else 0
    grouped = {}
    for line in text.splitlines():
        stamps = re.findall(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]', line)
        if not stamps:
            continue
        words = re.sub(r'\[[^\]]*\]|<\d+:\d+(?:\.\d+)?>', '', line).strip()
        for minute, second, fraction in stamps:
            ms = max(0, int(minute) * 60000 + int(second) * 1000 + int((fraction or '').ljust(3, '0')) + offset)
            grouped.setdefault(ms, [])
            if words and words not in grouped[ms]:
                grouped[ms].append(words)
    return [(ms, '\n'.join(lines)) for ms, lines in sorted(grouped.items())]


def lyric_index(lines, position):
    return bisect.bisect_right([x[0] for x in lines], position) - 1


def read_lyrics(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('歌词文件过大')
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'utf-16', 'gb18030', 'cp932'):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            pass
    return raw.decode('utf-8', 'replace')


def lyric_id(filename):
    return hashlib.sha256(str(filename).replace('\\', '/').casefold().encode()).hexdigest()


def lyrics_for_track(track):
    filename = track.get('Filename', '')
    candidates = [(DATA / 'lyrics' / (lyric_id(filename) + '.lrc'), '手动关联 LRC')]
    if filename:
        candidates.append((Path(filename).with_suffix('.lrc'), '同名 LRC'))
    for path, source in candidates:
        try:
            text = read_lyrics(path)
        except (OSError, ValueError):
            continue
        if lines := parse_lrc(text):
            return {'lines': lines, 'source': source, 'plain': ''}
    embedded = track.get('Lyrics', '') or ''
    if lines := parse_lrc(embedded):
        return {'lines': lines, 'source': '曲库内嵌 LRC', 'plain': ''}
    return {'lines': [], 'source': '无时间轴' if embedded else '暂无同步歌词', 'plain': embedded}


def import_lyrics(filename, source):
    text = read_lyrics(source)
    if not parse_lrc(text):
        raise ValueError('文件中没有有效的 LRC 时间轴')
    dest = DATA / 'lyrics' / (lyric_id(filename) + '.lrc')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix('.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(dest)
