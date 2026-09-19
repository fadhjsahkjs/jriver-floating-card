import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from cloud_lyrics import CloudLyrics, assess


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cloud = CloudLyrics(Path(self.temp.name))
        self.track = {'Filename':'C:/music/a.flac','Name':'Song','Artist':'Artist','Album':'Album','Duration':200}
        self.candidate = {'source':'LRCLIB','id':'1','title':'Song','artists':['Artist'],'album':'Album','duration':200,'text':'[00:01]hello'}

    def test_version_artist_duration_guards(self):
        self.assertTrue(assess(self.track,dict(self.candidate))['safe'])
        for override in [{'title':'Song (Live)'},{'artists':['Cover Artist']},{'duration':203}]:
            self.assertFalse(assess(self.track,{**self.candidate,**override})['safe'])

    def test_uncertain_result_never_auto_adopted(self):
        c=assess(self.track,{**self.candidate,'duration':210})
        self.cloud.search=Mock(return_value={'candidates':[c]})
        self.cloud.fetch=Mock()
        self.assertNotIn('text',self.cloud.automatic(self.track))
        self.cloud.fetch.assert_not_called()

    def test_selection_cache_is_exact_recording_and_survives_restart(self):
        self.cloud.select(self.track,self.candidate,self.candidate['text'],manual=True)
        second=CloudLyrics(self.temp.name)
        self.assertTrue(second.cached(self.track)['selected']['manual'])
        self.assertIsNone(second.cached({**self.track,'Filename':'C:/music/live.flac'}))

    def test_one_source_outage_does_not_hide_other(self):
        self.cloud.request=Mock(side_effect=[OSError('offline'),{'code':200,'result':{'songs':[{'id':12,'name':'Song','artists':[{'name':'Artist'}],'album':{'name':'Album'},'duration':200000}]}}])
        result=self.cloud.search(self.track)
        self.assertEqual(result['candidates'][0]['source'],'网易云')
        self.assertTrue(result['candidates'][0]['safe'])
        self.assertEqual(len(result['errors']),1)


if __name__=='__main__':unittest.main()
