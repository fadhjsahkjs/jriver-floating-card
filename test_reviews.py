import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import track_reviews as reviews


class ReviewTests(unittest.TestCase):
    def test_optional_provider_does_not_require_a_private_database(self):
        with patch.dict(reviews.SETTINGS, {}, clear=True):
            self.assertEqual(reviews.review_for_track({}), {'status':'missing'})

    def test_exact_recording_refresh_and_invalid_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'reviews.json'
            track=dict(Filename='C:/Music/track.flac',Name='Song',Artist='Artist',Album='Album')
            record=dict(filename=track['Filename'],title='Song',artist='Artist',album='Album',
                        comment='First review',final_score=88.5,final_star=5)
            with patch.dict(reviews.SETTINGS, {'reviews_file':str(path)}, clear=True):
                def write():path.write_text(json.dumps({'schema':1,'reviews':[record]}),'utf-8')
                write();self.assertEqual(reviews.review_for_track(track)['final_score'],88.5)
                self.assertEqual(reviews.review_for_track(dict(track,Album='Live Album'))['status'],'missing')
                self.assertEqual(reviews.review_for_track(dict(track,Filename='C:/Music/live.flac'))['status'],'missing')
                record.update(comment='Revised longer review',final_score=76.34,final_star=3);write()
                result=reviews.review_for_track(track)
                self.assertEqual(result['comment'],'Revised longer review');self.assertEqual(result['final_star'],3)
                record.update(final_score=True);write()
                self.assertNotIn('final_score',reviews.review_for_track(track))

    def test_duplicate_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'reviews.json'
            path.write_text(json.dumps({'schema':1,'reviews':[{'filename':'C:/A.flac'},{'filename':'c:\\a.flac'}]}),'utf-8')
            with patch.dict(reviews.SETTINGS, {'reviews_file':str(path)}, clear=True):
                self.assertEqual(reviews.review_for_track({'Filename':'C:/A.flac'})['status'],'unavailable')

    def test_malformed_optional_content_does_not_break_playback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'reviews.json'
            with patch.dict(reviews.SETTINGS, {'reviews_file':str(path)}, clear=True):
                for payload in ([],{'schema':1,'reviews':['bad']},{'schema':1,'reviews':[{'filename':'C:/A.flac','comment':[]}]}):
                    path.write_text(json.dumps(payload),'utf-8')
                    self.assertEqual(reviews.review_for_track({'Filename':'C:/A.flac'})['status'],'unavailable')


if __name__=='__main__':unittest.main()
