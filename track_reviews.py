"""Optional review JSON adapter. No private database or scoring model is required."""
import json
import math
import threading
from settings import SETTINGS, local_path

_lock = threading.Lock()
_stamp = None
_records = {}


def normalized(path):
    return str(path or '').replace('\\', '/').casefold()


def review_for_track(track):
    global _stamp, _records
    configured = SETTINGS.get('reviews_file')
    if not configured:
        return {'status': 'missing'}
    try:
        path = local_path(configured)
        with _lock:
            stat = path.stat()
            stamp = (str(path), stat.st_mtime_ns, stat.st_size)
            if stamp != _stamp:
                if stat.st_size > 32 * 1024 * 1024:
                    raise ValueError('Review file exceeds 32 MiB')
                payload = json.loads(path.read_text('utf-8'))
                if not isinstance(payload, dict) or payload.get('schema') != 1 or not isinstance(payload.get('reviews'), list):
                    raise ValueError('Invalid review schema')
                records = {}
                for item in payload['reviews']:
                    if not isinstance(item, dict) or not all(isinstance(item.get(k,''),str) for k in
                            ('filename','title','artist','album','description','comment','model','final_score_version','final_score_source')):
                        raise ValueError('Invalid review fields')
                    key = normalized(item['filename'])
                    if not key or key in records:
                        raise ValueError('Missing or duplicate recording path')
                    records[key] = item
                _records, _stamp = records, stamp
            r = _records.get(normalized(track.get('Filename')))
            if not r or any(str(r.get(k, '')) != str(track.get(field, '')) for k, field in
                            (('title','Name'), ('artist','Artist'), ('album','Album'))):
                return {'status': 'missing'}
            result = {k: r[k] for k in ('description','comment','model','listening_focus',
                                       'final_score_version','final_score_source') if k in r}
            result.update(status='available', source_kind='imported_review')
            score, star = r.get('final_score'), r.get('final_star')
            if (type(score) in (int,float) and math.isfinite(score) and 0 <= score <= 100
                    and type(star) is int and 1 <= star <= 5 and r.get('final_score_scale',100) == 100):
                result.update(final_score=score, final_star=star, final_score_scale=100)
            return result
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'unavailable'}
