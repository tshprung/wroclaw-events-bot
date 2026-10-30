"""Regressions from real Telegram duplicate and unwanted-series examples."""
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dateutil import tz

from src.blocklist import event_blocked, load_blocklist
from src.child_filter import ChildDecision, _explicit_matches, _validate_decision, classify_event
from src.dedupe import collapse_event_occurrences, collapse_wroclaw_go_twin_listings, fingerprint, should_skip_cross_source_duplicate
from src.event_identity import normalize_event
from src.family import load_family_preferences
from src.main import _format_family_event, _maybe_send_upcoming_digest
from src.models import Event, Source
from src.sources.parsers import parse_krajownik_wroclaw_wydarzenia
from src.storage import connect, insert_event_if_new, list_events_for_upcoming_digest, save_child_classification
from src.telegram import TelegramClient

WARSAW = tz.gettz('Europe/Warsaw')
NOW = datetime(2026, 10, 30, 9, tzinfo=WARSAW)
START = datetime(2026, 10, 31, 17, tzinfo=WARSAW)
PARADE_TITLE = 'Hyakki-yakō, czyli nocna parada stu japońskich demonów'
VENUE = 'Muzeum Teatru im. Henryka Tomaszewskiego'
PARADE = Event('wroclaw_go_all', PARADE_TITLE, START, VENUE,
    'https://www.wroclaw.pl/go/wydarzenia/rozrywka/1402364-hyakki-yak-czyli-nocna-parada-stu-japonskich-demonow')
CARD = Event('krajownik_wroclaw_wydarzenia', 'Wydarzenie (Krajownik)', None, None,
    'https://krajownik.pl/wroclaw/wydarzenia/hyakki-yako-czyli-nocna-parada-stu-japonskich-demonow-405180',
    raw_date_text=f'{PARADE_TITLE} 31 Października 2026 , od 17:00 Wrocław {VENUE}')
BAACH = Event('wroclaw_go_all', 'Baach!', START, 'Wrocławski Teatr Lalek - Scena na Piętrze',
    'https://www.wroclaw.pl/go/wydarzenia/teatr/1406261-baach')
BAAACH = replace(BAACH, title='BAAACH! | 3+', venue='Wrocławski Teatr Lalek',
    url='https://www.wroclaw.pl/go/wydarzenia/teatr/1417499-baaach-3-plus')
DESCRIPTION = 'A costume parade with mask and lantern decorating workshops, followed by a walk through the city.'


class IdentityTests(unittest.TestCase):
    def test_krajownik_placeholder_recovers_title_time_and_venue(self):
        recovered = normalize_event(CARD)
        self.assertEqual(recovered.title, PARADE_TITLE)
        self.assertEqual(recovered.start_at, START)
        self.assertEqual(recovered.venue, VENUE)

    def test_krajownik_card_parser_preserves_real_identity(self):
        source = Source('krajownik', 'Krajownik', 'https://krajownik.pl/wydarzenia', 'krajownik_wroclaw_wydarzenia')
        rows = parse_krajownik_wroclaw_wydarzenia(source, f'<a href="{CARD.url}">{CARD.raw_date_text}</a>')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, PARADE_TITLE)
        self.assertEqual(rows[0].start_at, START)

    def test_parade_duplicates_in_either_source_order(self):
        self.assertTrue(should_skip_cross_source_duplicate(CARD, [PARADE], WARSAW))
        self.assertTrue(should_skip_cross_source_duplicate(PARADE, [CARD], WARSAW))
        self.assertEqual(len(collapse_event_occurrences([CARD, PARADE], WARSAW)), 1)

    def test_baach_variants_keep_age_restriction(self):
        for rows in [[BAACH, BAAACH], [BAAACH, BAACH]]:
            kept = collapse_wroclaw_go_twin_listings(rows, WARSAW)
            self.assertEqual(kept, [BAAACH])

    def test_different_showtimes_remain_separate(self):
        other = replace(BAAACH, start_at=START.replace(hour=15))
        self.assertEqual(len(collapse_event_occurrences([BAACH, other], WARSAW)), 2)

    def test_different_days_remain_separate(self):
        self.assertFalse(should_skip_cross_source_duplicate(replace(PARADE, start_at=START + timedelta(days=7)), [CARD], WARSAW))

    def test_similar_short_titles_at_different_venues_not_merged(self):
        self.assertFalse(should_skip_cross_source_duplicate(replace(BAAACH, venue='Odra Centrum'), [BAACH], WARSAW))

    def test_first_slug_word_does_not_define_event(self):
        a = replace(PARADE, title='Warsztaty ceramiczne', url='https://www.wroclaw.pl/go/wydarzenia/edukacja/1-warsztaty-ceramiczne')
        b = replace(a, title='Warsztaty taneczne', url='https://www.wroclaw.pl/go/wydarzenia/edukacja/2-warsztaty-taneczne')
        self.assertEqual(len(collapse_wroclaw_go_twin_listings([a, b], WARSAW)), 2)

    def test_unknown_time_does_not_absorb_known_performances(self):
        unknown = replace(BAACH, start_at=None, raw_date_text='31.10.2026')
        self.assertFalse(should_skip_cross_source_duplicate(unknown, [BAAACH], WARSAW))

    def test_numbered_parts_not_merged(self):
        a = replace(PARADE, title='Warsztaty odkrywców część 1')
        b = replace(a, title='Warsztaty odkrywców część 2')
        self.assertFalse(should_skip_cross_source_duplicate(a, [b], WARSAW))

    def test_old_stored_krajownik_row_prevents_next_run_duplicate(self):
        conn = connect(':memory:')
        self.addCleanup(conn.close)
        insert_event_if_new(conn, fingerprint(CARD), CARD)
        save_child_classification(conn, fingerprint(CARD), relevant=True)
        seen = list_events_for_upcoming_digest(conn, now_utc=NOW.astimezone(timezone.utc),
            end_utc=(NOW + timedelta(days=3)).astimezone(timezone.utc),
            seen_since_utc=NOW - timedelta(days=14), limit_scheduled=100, limit_recent_unknown=100)
        self.assertTrue(should_skip_cross_source_duplicate(PARADE, seen, WARSAW))


class SeriesAndDescriptionTests(unittest.TestCase):
    def test_blocks_both_series_across_sources_and_future_weeks(self):
        cfg = load_blocklist()
        titles = ['Bezpłatne zajęcia ogólnorozwojowe z elementami koszykówki dla dzieci od 2 roku życia',
                  'Warsztaty dla dzieci. Mały geolog: na tropie dinozaurów i minerałów.']
        for title in titles:
            for days in [0, 7, 21]:
                for ev in [replace(PARADE, title=title, start_at=START + timedelta(days=days), url='https://example.org/new-id'),
                           replace(CARD, raw_date_text=title + ' 31 Października 2026 , od 10:00 Wrocław Odra Centrum', url='https://example.org/other-source')]:
                    with self.subTest(title=title, days=days, source=ev.url):
                        self.assertTrue(event_blocked(ev, NOW, cfg))

    def test_block_series_by_url_even_with_missing_title(self):
        for slug in ['maly-geolog-123', 'bezplatne-zajecia-ogolnorozwojowe-z-elementami-koszykowki-999']:
            self.assertTrue(event_blocked(replace(PARADE, title='Wydarzenie', url='https://example.org/' + slug), NOW))

    def test_other_dinosaur_or_sports_activities_remain_eligible(self):
        for title in ['Rodzinny piknik sportowy', 'Dinozaury w muzeum', 'Rodzinny mecz koszykówki']:
            self.assertFalse(event_blocked(replace(PARADE, title=title), NOW))

    def test_description_replaces_suitability_reasoning(self):
        decision = ChildDecision(True, None, None, 0.95, 'INTERNAL: suits the five-year-old', (60,), 'Wrocław', DESCRIPTION)
        msg = _format_family_event(CARD, NOW, decision)
        self.assertIn(DESCRIPTION, msg)
        self.assertIn(PARADE_TITLE, msg)
        self.assertIn('31.10.2026 17:00', msg)
        for unwanted in ['Suitable for:', 'Why:', 'INTERNAL:', 'Wydarzenie (Krajownik)']:
            self.assertNotIn(unwanted, msg)

    def test_standalone_age_badge_excludes_toddler(self):
        prefs = load_family_preferences()
        self.assertEqual(_explicit_matches(BAAACH.title, prefs), (60,))
        data = dict(suitable_ages_months=[18, 60], locality='Wrocław', location_evidence='Wrocław',
                    confidence=0.9, reason='Model thinks both children fit', event_description='A theatre performance for ages 3+.')
        self.assertEqual(_validate_decision(data, BAAACH, 'Wrocław', prefs).suitable_ages_months, (60,))

    def test_description_required_for_positive_classifier_result(self):
        data = dict(suitable_ages_months=[60], locality='Wrocław', location_evidence='Wrocław', confidence=0.9, reason='Fits')
        with self.assertRaises(ValueError):
            _validate_decision(data, PARADE, 'Wrocław', load_family_preferences())

    def test_description_survives_cached_classification(self):
        conn = connect(':memory:')
        self.addCleanup(conn.close)
        decision = ChildDecision(True, None, None, 0.95, 'Internal reason', (60,), 'Wrocław', DESCRIPTION)
        with patch('src.child_filter._fetch_event_context', return_value='Wrocław'), \
             patch('src.child_filter._openai_classify', return_value=decision) as api:
            first = classify_event(conn, Mock(), PARADE, fingerprint(PARADE))
            second = classify_event(conn, Mock(), PARADE, fingerprint(PARADE))
        self.assertEqual(first, second)
        self.assertEqual(second.event_description, DESCRIPTION)
        self.assertEqual(api.call_count, 1)

    def test_digest_contains_only_one_parade_description(self):
        conn = connect(':memory:')
        self.addCleanup(conn.close)
        decision = ChildDecision(True, None, None, 0.95, 'Internal reason', (60,), 'Wrocław', DESCRIPTION)
        telegram = Mock()
        with patch.dict(os.environ, {'UPCOMING_DIGEST_HOUR_LOCAL': str(NOW.hour)}, clear=True), \
             patch('src.main.list_events_for_upcoming_digest', return_value=[CARD, PARADE]), \
             patch('src.main.classify_event', return_value=decision) as api:
            _maybe_send_upcoming_digest(conn=conn, now_local=NOW, window_days=3,
                settings=SimpleNamespace(dry_run=False, telegram_channel_id='test'), telegram=telegram, sources=[])
        self.assertEqual(api.call_count, 1)
        self.assertEqual(telegram.send_message.call_args.args[1].count(DESCRIPTION), 1)

    def test_long_photo_caption_keeps_full_description_and_link(self):
        text = 'Description ' * 110 + PARADE.url
        with patch.object(TelegramClient, 'send_message') as send, patch('src.telegram.requests.post') as post:
            TelegramClient('test-token').send_photo('test', 'https://example.org/photo.jpg', caption=text)
        send.assert_called_once_with('test', text)
        post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
