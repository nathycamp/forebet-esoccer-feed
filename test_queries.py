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



class PicksTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.stamp = self.now.isoformat()
        def row(minute, status='played', home='Alice', away='Bob', score='4-1'):
            return {'home_tag': home, 'away_tag': away, 'home_club': 'Arsenal', 'away_club': 'Chelsea',
                    'kickoff_utc': (self.now+timedelta(minutes=minute)).isoformat(), 'status': status,
                    'actual_score': score if status == 'played' else None, 'p1': 45, 'px': 25, 'p2': 30}
        self.rows = [row(-20*(index+1)) for index in range(40)]
        self.rows += [row(10, 'upcoming'), row(15, 'upcoming', 'Carol', 'Dave'), row(-60, 'upcoming', 'Late', 'Row'),
                      row(45, 'upcoming', 'Far', 'Ahead')]
        with app.cache_lock:
            app.cache.update({'fetched_at': self.stamp, 'stale': False, 'leagues': {
                key: {'fixtures': copy.deepcopy(self.rows), 'fetched_at': self.stamp,
                      'changed_at': self.stamp, 'status': 'ok'} for key in app.LEAGUES}})
        with app.picks_lock:
            app.picks_cache.update({'key': None, 'result': None})
        self.client = app.app.test_client()

    def test_goal_calls_use_league_line_and_label_confidence(self):
        with patch.object(app, 'run_reader', side_effect=AssertionError('request triggered a scrape')):
            result = self.client.get('/picks').get_json()
        self.assertFalse(result['stale'])
        esb8, gt12 = result['leagues']['esb8'], result['leagues']['gt12']
        self.assertEqual([pick['home_tag'] for pick in esb8], ['Alice', 'Carol'])
        alice = esb8[0]
        self.assertEqual(alice['goals_line'], 5.5)
        self.assertIn(alice['goals_call'], {'over', 'under'})
        self.assertGreaterEqual(alice['goals_probability'], .5)
        self.assertEqual(alice['goals_confidence'], 'trusted')
        self.assertEqual(gt12[0]['goals_confidence'], 'low')
        self.assertEqual(alice['favourite'], 'Alice')
        self.assertEqual(alice['favourite_strength'], 'weak')
        self.assertEqual(alice['call_status'], 'ok')
        carol = esb8[1]
        self.assertIsNone(carol['goals_call'])
        self.assertEqual(carol['call_status'], 'no call: new players')

    def test_only_next_kickoff_or_twenty_minute_window(self):
        result = self.client.get('/picks?league=esb8').get_json()
        self.assertEqual([pick['home_tag'] for pick in result['leagues']['esb8']], ['Alice', 'Carol'])
        with patch.object(app, 'datetime', wraps=datetime) as dt:
            dt.now.return_value = self.now+timedelta(minutes=16)
            with app.cache_lock:
                app.cache['fetched_at'] = (self.now+timedelta(minutes=16)).isoformat()
                for saved in app.cache['leagues'].values():
                    saved['fetched_at'] = app.cache['fetched_at']
            result = self.client.get('/picks?league=esb8').get_json()
        # Nothing else within 20 minutes, so the next kickoff is still shown.
        self.assertEqual([pick['home_tag'] for pick in result['leagues']['esb8']], ['Far'])

    def test_live_game_score_updates_the_next_pick(self):
        quiet = self.client.get('/picks?league=esb8').get_json()['leagues']['esb8'][0]
        live = {'home_tag': 'Alice', 'away_tag': 'Bob', 'home_club': 'Arsenal', 'away_club': 'Chelsea',
                'kickoff_utc': (self.now-timedelta(minutes=4)).isoformat(), 'status': 'live',
                'live_score': '6-5', 'match_clock': '4', 'actual_score': None, 'p1': 45, 'px': 25, 'p2': 30}
        with app.cache_lock:
            for saved in app.cache['leagues'].values():
                saved['fixtures'].append(dict(live))
                saved['fetched_at'] = app.cache['fetched_at'] = (self.now+timedelta(seconds=1)).isoformat()
        busy = self.client.get('/picks?league=esb8').get_json()['leagues']['esb8'][0]
        self.assertGreater(busy['expected_goals'], quiet['expected_goals'])
        self.assertIn('live_score_evidence_used', busy['warnings'])
        self.assertNotIn('live_score_evidence_used', quiet['warnings'])

    def test_top_picks_take_the_likeliest_option_in_any_market(self):
        result = self.client.get('/picks').get_json()
        top = result['top_picks']
        self.assertEqual(result['top_picks_status'], 'ok')
        self.assertLessEqual(len(top), app.TOP_PICKS)
        for pick in top:
            self.assertIn(pick['market'], {'1x2', 'double_chance', 'over_under'})
            self.assertGreaterEqual(pick['probability'], app.TOP_PICK_MIN_PROBABILITY)
            self.assertIn('league_rate', pick)
            self.assertEqual(pick['proven_edge'], pick['market'] == 'over_under'
                             and pick['league'] in app.TRUSTED_GOAL_LEAGUES)
        self.assertEqual([p['probability'] for p in top], sorted((p['probability'] for p in top), reverse=True))
        with patch.object(app, 'TOP_PICK_MIN_PROBABILITY', 1.):
            result = self.client.get('/picks').get_json()
        self.assertEqual(result['top_picks'], [])
        self.assertEqual(result['top_picks_status'], 'no top pick right now')

    def test_goal_lines_give_every_line_with_fair_odds(self):
        pick = self.client.get('/picks?league=esb8').get_json()['leagues']['esb8'][0]
        lines = pick['goal_lines']
        self.assertIn(str(pick['goals_line']), lines)
        overs = [lines[line]['over'] for line in sorted(lines, key=float)]
        self.assertEqual(overs, sorted(overs, reverse=True))
        for value in lines.values():
            self.assertAlmostEqual(value['over'] + value['under'], 1, places=3)
            if value['fair_over_odds']:
                self.assertAlmostEqual(value['fair_over_odds'], 1 / value['over'], delta=.01)

    def test_safest_option_prefers_the_proven_goals_call(self):
        pick = self.client.get('/picks?league=esb8').get_json()['leagues']['esb8'][0]
        option = pick['safest_option']
        if pick['goals_call'] is not None:
            self.assertTrue(option['proven_edge'])
            self.assertEqual(option['market'], 'over_under')
            self.assertAlmostEqual(option['probability'], pick['goals_probability'], places=3)
        else:
            best = max(list(pick['one_x_two'].values()) + list(pick['double_chance'].values()) + [pick['goals_probability']])
            self.assertAlmostEqual(option['probability'], best, places=3)
        carol = self.client.get('/picks?league=esb8').get_json()['leagues']['esb8'][1]
        self.assertIsNone(carol['safest_option'])

    def test_top_query_returns_only_the_safest_options(self):
        with patch.object(app, 'TOP_PICK_MIN_PROBABILITY', 1.):
            result = self.client.get('/picks?top=2').get_json()
        self.assertNotIn('leagues', result)
        top = result['top_picks']
        self.assertEqual(len(top), 2)
        self.assertGreaterEqual((top[0]['proven_edge'], top[0]['probability']), (top[1]['proven_edge'], top[1]['probability']))
        result = self.client.get('/picks?top=1&league=volta6&fields=home_tag,kickoff_utc').get_json()
        self.assertEqual(set(result['top_picks'][0]), {'home_tag', 'kickoff_utc', 'league', 'market', 'selection',
                                                       'probability', 'league_rate', 'proven_edge'})
        self.assertEqual(result['top_picks'][0]['league'], 'volta6')
        for query in ('top=0', 'top=11', 'top=x', 'top=1&top=2'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/picks?'+query).status_code, 400)

    def test_winner_markets_are_information_only(self):
        pick = self.client.get('/picks?league=esb8').get_json()['leagues']['esb8'][0]
        self.assertAlmostEqual(sum(pick['one_x_two'].values()), 1, places=3)
        self.assertAlmostEqual(pick['double_chance']['1X'], pick['one_x_two']['1'] + pick['one_x_two']['X'], places=3)
        self.assertTrue(pick['winner_markets_trust'].startswith('not trusted'))

    def test_near_even_calls_are_not_made(self):
        with patch.object(app, 'MIN_CALL_PROBABILITY', 1.):
            pick = self.client.get('/picks?league=esb8').get_json()['leagues']['esb8'][0]
        self.assertIsNone(pick['goals_call'])
        self.assertEqual(pick['call_status'], 'no call: too close')
        self.assertGreaterEqual(pick['goals_probability'], .5)

    def test_picks_accept_the_same_filters(self):
        result = self.client.get('/picks?league=volta6,esb8&player=carol&fields=home_tag,goals_call').get_json()
        self.assertEqual(set(result['leagues']), {'esb8', 'volta6'})
        self.assertEqual(set(result['league_fetched_at']), {'esb8', 'volta6'})
        self.assertEqual(set(result['leagues']['esb8'][0]), {'home_tag', 'goals_call'})
        self.assertEqual(result['leagues']['esb8'][0]['home_tag'], 'Carol')
        result = self.client.get('/picks?limit=1').get_json()
        self.assertTrue(all([pick['home_tag'] for pick in rows] == ['Alice'] for rows in result['leagues'].values()))
        for query in ('status=upcoming', 'fields=p1', 'league=unknown', 'limit=0', 'typo=1'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/picks?'+query).status_code, 400)

    def test_stale_source_withholds_calls(self):
        with patch.object(app, 'datetime', wraps=datetime) as dt:
            dt.now.return_value = self.now+timedelta(minutes=7)
            result = self.client.get('/picks?league=esb8').get_json()
        self.assertTrue(result['stale'])
        pick = result['leagues']['esb8'][0]
        self.assertIsNone(pick['goals_call'])
        self.assertIsNone(pick['favourite'])
        self.assertEqual(pick['call_status'], 'withheld: stale source')

    def test_picks_are_computed_once_per_refresh(self):
        with patch.object(app, 'predict_feed', wraps=app.predict_feed) as engine:
            self.client.get('/picks')
            self.client.get('/picks?league=esb8')
            self.assertEqual(engine.call_count, 1)
            with app.cache_lock:
                app.cache['fetched_at'] = app.cache['leagues']['esb8']['fetched_at'] = (self.now+timedelta(seconds=1)).isoformat()
            self.client.get('/picks')
            self.assertEqual(engine.call_count, 2)

    def test_unavailable_league_is_preserved(self):
        del app.cache['leagues']['esb8']
        result = self.client.get('/picks?league=esb8,gt12').get_json()
        self.assertEqual(result['leagues']['esb8'], {'status': 'error'})
        self.assertTrue(result['stale'])
        self.assertEqual(len(result['leagues']['gt12']), 2)

if __name__ == '__main__':
    unittest.main()
