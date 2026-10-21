import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dateutil import tz

from src.blocklist import event_blocked, load_blocklist
from src.child_filter import ChildDecision, _explicit_matches, _validate_decision, classify_event
from src.event_window import resolve_when
from src.exclusions import event_is_excluded
from src.family import FamilyPreferences, load_family_preferences
from src.main import _format_family_event, _maybe_send_upcoming_digest
from src.models import Event
from src.storage import SCHEMA, connect, get_child_classification, save_child_classification
from src.telegram import message_chunks

NOW = datetime(2026, 10, 21, 10, tzinfo=tz.gettz("Europe/Warsaw"))
PREFS = FamilyPreferences((18, 60), ("Wrocław", "Siechnice"))


def event(**kwargs):
    return replace(Event("test", "Warsztaty", NOW + timedelta(days=1), "Wrocław", "https://example.org/event"), **kwargs)


class FamilyTests(unittest.TestCase):
    def test_schedule_inclusive_boundaries(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_blocklist()
        for hour, minute, blocked in [(5, 59, True), (6, 0, False), (17, 30, False), (17, 31, True), (20, 0, True)]:
            with self.subTest(hour=hour, minute=minute):
                ev = event(start_at=NOW.replace(day=22, hour=hour, minute=minute))
                self.assertEqual(event_blocked(ev, NOW, cfg), blocked)

    def test_utc_start_uses_warsaw_time(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_blocklist()
        self.assertFalse(event_blocked(event(start_at=datetime.fromisoformat("2026-10-22T15:30:00+00:00")), NOW, cfg))
        self.assertTrue(event_blocked(event(start_at=datetime.fromisoformat("2026-10-22T15:31:00+00:00")), NOW, cfg))

    def test_month_and_year_age_ranges(self):
        for title, expected in [("6–24 miesięcy", (18,)), ("3-6 lat", (60,)),
                                ("0-6 lat", (18, 60)), ("od 6 lat", ()),
                                ("od 18 miesięcy", (18, 60)), ("wiek 6+", ()),
                                ("6-24 mies.", (18,))]:
            with self.subTest(title=title):
                self.assertEqual(_explicit_matches(title, PREFS), expected)

    def test_disjoint_ages_not_merged(self):
        self.assertEqual(_explicit_matches("0-2 lat oraz 7-10 lat", PREFS), (18,))

    def test_year_ranges_include_whole_age_year(self):
        self.assertEqual(_explicit_matches("3-5 lat", FamilyPreferences((65,), ("Wrocław",))), (65,))

    def test_mixed_units_not_reduced_to_minimum(self):
        self.assertIsNone(_explicit_matches("od 6 miesięcy do 3 lat", PREFS))

    def test_times_and_dates_not_ages(self):
        for title in ["Warsztaty 10-12", "Rodzinny piknik", "2026-10-21", "10:00–12:00"]:
            self.assertIsNone(_explicit_matches(title, PREFS))

    def test_preferences_version_and_localities(self):
        self.assertTrue(PREFS.allows_locality("Wroclaw"))
        self.assertFalse(PREFS.allows_locality("Warszawa"))
        self.assertFalse(PREFS.allows_locality(None))
        self.assertNotEqual(PREFS.policy_version, replace(PREFS, ages_months=(24, 60)).policy_version)
        self.assertEqual(load_family_preferences().ages_months, (18, 60))

    def decision(self, **updates):
        data = dict(suitable_ages_months=[60], locality="Wrocław", location_evidence="Wrocław",
                    confidence=0.9, reason="Workshop explicitly for preschool children")
        data.update(updates)
        return _validate_decision(data, event(), "", PREFS)

    def test_either_child_is_enough(self):
        for matches in [[18], [60], [18, 60]]:
            self.assertTrue(self.decision(suitable_ages_months=matches).relevant)
        self.assertFalse(self.decision(suitable_ages_months=[]).relevant)

    def test_nearby_and_distant_localities(self):
        data = dict(suitable_ages_months=[18], locality="Siechnice", location_evidence="Siechnice",
                    confidence=0.9, reason="Toddler activity")
        match = _validate_decision(data, event(venue="Siechnice"), "", PREFS)
        self.assertTrue(match.relevant)
        self.assertIn("drive time unverified", match.summary())
        data.update(locality="Warszawa", location_evidence="Warszawa")
        self.assertFalse(_validate_decision(data, event(venue="Warszawa"), "", PREFS).relevant)

    def test_default_event_city_does_not_prove_location(self):
        data = dict(suitable_ages_months=[60], locality="Wrocław", location_evidence="Wrocław",
                    confidence=0.9, reason="Workshop")
        self.assertFalse(_validate_decision(data, event(venue=None), "", PREFS).relevant)

    def test_invalid_classifier_types_rejected(self):
        for data in [dict(suitable_ages_months=[True]), dict(suitable_ages_months=[19]),
                     dict(confidence="high"), dict(confidence=float("nan")), dict(reason="")]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.decision(**data)

    def test_title_age_restriction_overrides_model_match(self):
        data = dict(suitable_ages_months=[18, 60], locality="Wrocław", location_evidence="Wrocław",
                    confidence=0.9, reason="Workshop")
        result = _validate_decision(data, event(title="Warsztaty 3-6 lat"), "", PREFS)
        self.assertEqual(result.suitable_ages_months, (60,))

    def test_museums_theatre_and_concerts_reach_classifier(self):
        for url in ["https://teatrlalek.wroclaw.pl/pl/wydarzenia/123",
                    "https://www.wroclaw.pl/go/wydarzenia/teatr/123-rodzinny",
                    "https://www.wroclaw.pl/go/wydarzenia/muzyka/123-koncert",
                    "https://mnwr.pl/warsztaty"]:
            self.assertFalse(event_is_excluded(event(url=url, venue="Muzeum Narodowe")))

    def test_non_event_and_online_exclusions_still_work(self):
        self.assertTrue(event_is_excluded(event(url="https://www.wroclaw.pl/go/wydarzenia/teatr")))
        self.assertTrue(event_is_excluded(event(title="Warsztaty online")))

    def test_meetup_pm_time_not_treated_as_undated(self):
        ev = event(title="Thu, Oct 22 · 6:00 PM CEST · Playgroup", start_at=None,
                   url="https://meetup.com/wroclaw/events/123")
        self.assertEqual(resolve_when(ev, NOW).tm.hour, 18)
        self.assertTrue(event_blocked(ev, NOW, load_blocklist()))

    def test_unknown_start_label(self):
        text = _format_family_event(event(start_at=None, raw_date_text="22.10.2026"), NOW,
                                  ChildDecision(True, None, None, 0.9, "Age range", (60,), "Wrocław"))
        self.assertIn("Start time unconfirmed", text)
        self.assertIn("5-year-old", text)

    def test_message_chunk_limits_and_event_boundaries(self):
        blocks = [str(i) + "x" * 600 for i in range(30)]
        text = "\n\n".join(blocks)
        chunks = message_chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))
        self.assertEqual("\n\n".join(chunks), text)
        self.assertEqual("".join(message_chunks("x" * 9000)), "x" * 9000)


class StorageAndDigestTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_legacy_schema_migration_idempotent(self):
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(child_classifications)")}
        self.assertTrue({"decision_json", "policy_version"} <= columns)
        save_child_classification(self.conn, "fp", relevant=True)
        self.assertIsNone(get_child_classification(self.conn, "fp", policy_version=PREFS.policy_version))

    def test_existing_database_migrates_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.db")
            old = sqlite3.connect(path)
            old.executescript(SCHEMA)
            old.execute("INSERT INTO child_classifications VALUES ('legacy',1,NULL,NULL,0.9,'old','2026-10-21')")
            old.commit()
            old.close()
            for _ in range(2):
                migrated = connect(path)
                self.assertIsNotNone(get_child_classification(migrated, "legacy"))
                self.assertIsNone(get_child_classification(migrated, "legacy", policy_version=PREFS.policy_version))
                migrated.close()

    def test_classification_cache_and_invalidation(self):
        decision = ChildDecision(True, None, None, 0.9, "Preschool activity", (60,), "Wrocław")
        with patch("src.child_filter.load_family_preferences", return_value=PREFS), \
             patch("src.child_filter._fetch_event_context", return_value="details") as fetch, \
             patch("src.child_filter._openai_classify", return_value=decision) as api:
            self.assertEqual(classify_event(self.conn, Mock(), event(), "fp"), decision)
            self.assertEqual(classify_event(self.conn, Mock(), event(), "fp"), decision)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(api.call_count, 1)
        self.assertIsNone(get_child_classification(self.conn, "fp", policy_version="changed-policy"))

    def test_transient_failure_not_cached(self):
        with patch("src.child_filter.load_family_preferences", return_value=PREFS), \
             patch("src.child_filter._fetch_event_context", return_value=""), \
             patch("src.child_filter._openai_classify", return_value=None):
            self.assertFalse(classify_event(self.conn, Mock(), event(), "fp").relevant)
        self.assertIsNone(get_child_classification(self.conn, "fp", policy_version=PREFS.policy_version))

    def test_family_keyword_alone_does_not_bypass_classifier(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("src.child_filter.load_family_preferences", return_value=PREFS), \
             patch("src.child_filter._fetch_event_context", return_value=""), \
             patch("src.child_filter._openai_classify", return_value=None):
            self.assertFalse(classify_event(self.conn, Mock(), event(title="Rodzinny piknik"), "fp").relevant)

    def test_digest_reapplies_schedule_and_reclassifies_legacy_results(self):
        early = event(start_at=NOW.replace(day=22, hour=10))
        late = event(start_at=NOW.replace(day=22, hour=18), url="https://example.org/late")
        telegram = Mock()
        decision = ChildDecision(True, None, None, 0.9, "Preschool activity", (60,), "Wrocław")
        with patch.dict(os.environ, {"UPCOMING_DIGEST_HOUR_LOCAL": "10"}, clear=True), \
             patch("src.main.list_events_for_upcoming_digest", return_value=[early, late]), \
             patch("src.main.classify_event", return_value=decision) as classifier:
            _maybe_send_upcoming_digest(conn=self.conn, now_local=NOW, window_days=3,
                settings=SimpleNamespace(dry_run=False, telegram_channel_id="test"), telegram=telegram, sources=[])
        self.assertEqual(classifier.call_count, 1)
        msg = telegram.send_message.call_args.args[1]
        self.assertIn("5-year-old", msg)
        self.assertNotIn("example.org/late", msg)

    def test_digest_withholds_event_when_current_classification_rejects_it(self):
        telegram = Mock()
        with patch.dict(os.environ, {"UPCOMING_DIGEST_HOUR_LOCAL": "10"}, clear=True), \
             patch("src.main.list_events_for_upcoming_digest", return_value=[event()]), \
             patch("src.main.classify_event", return_value=ChildDecision(False, None, None, 0.9, "Too old")):
            _maybe_send_upcoming_digest(conn=self.conn, now_local=NOW, window_days=3,
                settings=SimpleNamespace(dry_run=False, telegram_channel_id="test"), telegram=telegram, sources=[])
        telegram.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
