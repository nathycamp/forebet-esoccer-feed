import copy
import gzip
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.stamp = self.now.isoformat()
        def row(home, away, status, minute):
            return {'home_tag': home, 'away_tag': away, 'status': status,
                    'kickoff_utc': (self.now+timedelta(minutes=minute)).isoformat(),
                    'live_score': '0-0' if status == 'live' else None,
                    'actual_score': '2-1' if status == 'played' else None,
                    'home_club': 'Arsenal', 'away_club': 'PSG', 'match_clock': '2' if status == 'live' else None}
        self.rows = [row('Later', 'Away', 'upcoming', 30),
                     row('Old', 'Away', 'played', -60),
                     row('Next', 'Away', 'upcoming', 10),
                     row('Scorpion', 'LiveAway', 'live', -3),
                     row('Home', 'SCORPION', 'played', -20),
                     row('Unresolved', 'Away', 'upcoming', -60)]
        with app.cache_lock:
            app.cache.update({'fetched_at': self.stamp, 'stale': False, 'leagues': {
                key: {'fixtures': copy.deepcopy(self.rows), 'fetched_at': self.stamp,
                      'changed_at': self.stamp, 'status': 'ok'} for key in app.LEAGUES}})
        self.client = app.app.test_client()

    def test_all_leagues_and_multi_league_selection(self):
        self.assertEqual(set(self.client.get('/predictions?status=live').get_json()['leagues']), set(app.LEAGUES))
        result = self.client.get('/predictions?league=esb8,volta6').get_json()
        self.assertEqual(set(result['leagues']), {'esb8', 'volta6'})
        self.assertEqual(set(result['league_fetched_at']), {'esb8', 'volta6'})
        self.assertEqual(set(result['league_clock_checks']), {'esb8', 'volta6'})

    def test_live_filter_across_every_league(self):
        with patch.object(app, 'run_reader', side_effect=AssertionError('request triggered a scrape')):
            result = self.client.get('/predictions?status=live').get_json()
        for rows in result['leagues'].values():
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['home_tag'], 'Scorpion')
            self.assertEqual(rows[0]['live_score'], '0-0')

    def test_next_and_latest_limits_apply_per_league(self):
        result = self.client.get('/predictions?status=upcoming&limit=1').get_json()
        for rows in result['leagues'].values():
            self.assertEqual([r['home_tag'] for r in rows], ['Next'])
        result = self.client.get('/predictions?status=played&limit=1').get_json()
        for rows in result['leagues'].values():
            self.assertEqual([r['home_tag'] for r in rows], ['Home'])

    def test_player_filter_matches_either_side_case_insensitively(self):
        result = self.client.get('/predictions?player=%20scorpion%20').get_json()
        self.assertTrue(all(len(rows) == 2 for rows in result['leagues'].values()))
        result = self.client.get('/predictions?player=Scorp').get_json()
        self.assertTrue(all(rows == [] for rows in result['leagues'].values()))

    def test_projection_combines_with_filters_and_never_changes_cache(self):
        original = copy.deepcopy(app.cache)
        result = self.client.get('/predictions?league=h2hgg,gt12&status=live&fields=home_tag,live_score&limit=1').get_json()
        self.assertEqual(result['leagues']['h2hgg'], [{'home_tag': 'Scorpion', 'live_score': '0-0'}])
        self.assertEqual(app.cache, original)
        self.assertEqual(self.client.get('/predictions').get_json()['leagues']['gt12'], self.rows)

    def test_invalid_inputs_are_400(self):
        for query in ('league=unknown', 'league=', 'status=finished', 'status=', 'fields=secret',
                      'fields=', 'limit=0', 'limit=-1', 'limit=1001', 'limit=1.5', 'limit=',
                      'player=', 'limit=1&limit=2', 'player=A&player=B', 'typo=gt12'):
            with self.subTest(query=query):
                response = self.client.get('/predictions?'+query)
                self.assertEqual(response.status_code, 400)
                self.assertIn('error', response.get_json())

    def test_unavailable_league_is_preserved_even_with_fields(self):
        del app.cache['leagues']['esb8']
        result = self.client.get('/predictions?league=esb8&fields=home_tag').get_json()
        self.assertEqual(result['leagues']['esb8'], {'status': 'error'})
        self.assertTrue(result['stale'])

    def test_gzip_negotiation_and_conditional_poll(self):
        plain = self.client.get('/predictions')
        compressed = self.client.get('/predictions', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(compressed.headers['Content-Encoding'], 'gzip')
        self.assertEqual(gzip.decompress(compressed.data), plain.data)
        self.assertLess(len(compressed.data), len(plain.data))
        self.assertIn('Accept-Encoding', compressed.headers['Vary'])
        unchanged = self.client.get('/predictions', headers={
            'Accept-Encoding': 'gzip', 'If-None-Match': compressed.headers['ETag']})
        self.assertEqual(unchanged.status_code, 304)
        self.assertEqual(unchanged.data, b'')
        self.assertEqual(unchanged.headers['ETag'], compressed.headers['ETag'])
        disabled = self.client.get('/predictions', headers={'Accept-Encoding': 'gzip;q=0'})
        self.assertNotIn('Content-Encoding', disabled.headers)

    def test_etag_changes_for_scores_filters_and_stale_state(self):
        first = self.client.get('/predictions?status=live')
        tag = first.headers['ETag']
        other = self.client.get('/predictions?status=played', headers={'If-None-Match': tag})
        self.assertEqual(other.status_code, 200)
        app.cache['leagues']['gt12']['fixtures'][3]['live_score'] = '1-0'
        changed = self.client.get('/predictions?status=live', headers={'If-None-Match': tag})
        self.assertEqual(changed.status_code, 200)
        tag = changed.headers['ETag']
        with patch.object(app, 'datetime', wraps=datetime) as dt:
            dt.now.return_value = self.now+timedelta(minutes=7)
            stale = self.client.get('/predictions?status=live', headers={'If-None-Match': tag})
        self.assertEqual(stale.status_code, 200)
        self.assertTrue(stale.get_json()['stale'])

    def test_head_and_empty_matches(self):
        self.assertEqual(self.client.head('/predictions?status=live').status_code, 200)
        self.assertEqual(self.client.head('/predictions?status=live').data, b'')
        result = self.client.get('/predictions?player=missing').get_json()
        self.assertTrue(all(rows == [] for rows in result['leagues'].values()))


if __name__ == '__main__':
    unittest.main()
