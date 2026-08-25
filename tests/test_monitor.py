import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import layoff_monitor as m


FIXTURES = Path(__file__).parent / "fixtures"


def article(title, source_url="https://www.statesman.com", guid="x", feed="local"):
    return {
        "title": title, "source": "Test Publisher", "source_url": source_url,
        "guid": guid, "link": "https://news.google.com/" + guid,
        "published": "2026-08-24T12:00:00Z", "feed": feed,
    }


def xlsx_fixture(rows):
    strings = []
    index = {}

    def shared(value):
        value = str(value)
        if value not in index:
            index[value] = len(strings)
            strings.append(value)
        return index[value]

    headers = ["NOTICE_DATE", "JOB_SITE_NAME", "COUNTY_NAME", "WDA_NAME",
               "TOTAL_LAYOFF_NUMBER", "LayOff_Date", "WFDD_RECEIVED_DATE", "CITY_NAME"]
    all_rows = [headers] + rows
    xml_rows = []
    for row_number, values in enumerate(all_rows, 1):
        cells = []
        for col, value in enumerate(values):
            ref = chr(65 + col) + str(row_number)
            if col in (0, 4, 5, 6) and row_number > 1:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{shared(value)}</v></c>')
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = f'<worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    shared_xml = '<sst xmlns="{}">{}</sst>'.format(
        ns, "".join("<si><t>{}</t></si>".format(x) for x in strings))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/sharedStrings.xml", shared_xml)
    return output.getvalue()


class ConfigMixin:
    @classmethod
    def setUpClass(cls):
        cls.cfg = m.load_config()


class NewsTests(ConfigMixin, unittest.TestCase):
    def test_builds_exactly_five_feeds(self):
        feeds = m.build_news_feeds(self.cfg)
        self.assertEqual(5, len(feeds))
        self.assertTrue(all("when%3A2d" in url for _, url in feeds))

    def test_parses_google_rss_fields_and_strips_source_suffix(self):
        items = m.parse_news_rss((FIXTURES / "news.xml").read_bytes(), "priority-1")
        self.assertEqual(3, len(items))
        self.assertEqual("Oracle cuts 1,000 jobs in restructuring", items[0]["title"])
        self.assertEqual("https://www.reuters.com", items[0]["source_url"])
        self.assertEqual("priority-1", items[0]["feed"])

    def test_oracle_companywide_signal(self):
        event, reason = m.classify_article(article(
            "Oracle cuts 1,000 jobs in restructuring", "https://www.reuters.com", feed="priority-1"), self.cfg)
        self.assertEqual("accepted", reason)
        self.assertEqual("Oracle", event["company"])
        self.assertEqual("companywide", event["scope"])
        self.assertEqual(1000, event["count"])

    def test_priority_local_qualifies_without_count(self):
        event, _ = m.classify_article(article("Tesla lays off workers at Austin Giga Texas facility"), self.cfg)
        self.assertEqual("local", event["scope"])

    def test_general_local_threshold(self):
        event, _ = m.classify_article(article("Acme lays off 50 workers in Round Rock"), self.cfg)
        self.assertIsNotNone(event)
        event, reason = m.classify_article(article("Acme lays off 49 workers in Round Rock"), self.cfg)
        self.assertIsNone(event)
        self.assertIn("below 50", reason)

    def test_local_facility_closure_without_count(self):
        event, _ = m.classify_article(article("Acme facility closing in Pflugerville amid layoffs"), self.cfg)
        self.assertTrue(event["closure"])

    def test_companywide_percent(self):
        event, _ = m.classify_article(article("Indeed layoffs cut 5% of workforce", feed="priority-3"), self.cfg)
        self.assertEqual("companywide", event["scope"])

    def test_word_numbers(self):
        self.assertEqual(1000, m.extract_numbers("Oracle cuts thousands of jobs")[0])
        self.assertEqual(200, m.extract_numbers("Dell cuts hundreds of jobs")[0])
        self.assertEqual(100, m.extract_numbers("Expedia plans 100 layoffs at North Austin office")[0])
        self.assertEqual(158, m.extract_numbers("Company laying off 158 people in Austin")[0])
        self.assertEqual(150, m.extract_numbers("Intel laying off another 150 people in Austin")[0])

    def test_false_positives_are_suppressed(self):
        titles = [
            "US unemployment claims fall with layoffs still sparse",
            "Austin workers rally after Microsoft layoffs",
            "Former employees launch startup after layoffs in Austin",
            "Oracle may face layoffs in Austin",
            "Five years after Austin layoffs, workers remember",
        ]
        for title in titles:
            event, _ = m.classify_article(article(title), self.cfg)
            self.assertIsNone(event, title)

    def test_austin_in_publisher_name_is_not_location_evidence(self):
        event, reason = m.classify_article(article(
            "Acme lays off 100 workers nationwide", source_url="https://www.kxan.com"), self.cfg)
        self.assertIsNone(event)
        self.assertIn("Austin area", reason)

    def test_public_exclusion_and_ut_exception(self):
        event, _ = m.classify_article(article("City of Austin lays off 100 workers"), self.cfg)
        self.assertIsNone(event)
        event, _ = m.classify_article(article("UT Austin lays off 20 workers in Austin"), self.cfg)
        self.assertIsNotNone(event)

    def test_untrusted_publisher(self):
        event, reason = m.classify_article(article(
            "Tesla lays off 1,000 Austin workers", "https://rumors.invalid"), self.cfg)
        self.assertIsNone(event)
        self.assertIn("allowlisted", reason)

    def test_cluster_and_material_local_upgrade(self):
        first, _ = m.classify_article(article(
            "Oracle cuts 1,000 jobs in restructuring", "https://www.reuters.com", "one", "priority-1"), self.cfg)
        second, _ = m.classify_article(article(
            "Oracle restructuring cuts 1,000 jobs", "https://www.statesman.com", "two", "priority-1"), self.cfg)
        key, old = m.find_cluster(second, {"event": first})
        self.assertEqual("event", key)
        self.assertEqual("companywide", old["scope"])


class WarnTests(ConfigMixin, unittest.TestCase):
    def test_parse_warn_workbook_and_excel_dates(self):
        data = xlsx_fixture([[46200, "Tesla (Austin)", "Travis", "Capital Area WDA", 100,
                              46260, 46201, "Austin"]])
        rows = m.parse_warn_xlsx(data)
        self.assertEqual(1, len(rows))
        self.assertEqual("Tesla (Austin)", rows[0]["job_site_name"])
        self.assertEqual(100, rows[0]["total_layoff_number"])
        self.assertRegex(rows[0]["notice_date"], r"2026-\d\d-\d\d")

    def test_warn_correction_changes_version_not_base(self):
        row = {"job_site_name": "Tesla", "city_name": "Austin", "notice_date": "2026-01-01",
               "total_layoff_number": 100, "layoff_date": "2026-03-01"}
        base1, version1 = m.warn_keys(row)
        row["total_layoff_number"] = 120
        base2, version2 = m.warn_keys(row)
        self.assertEqual(base1, base2)
        self.assertNotEqual(version1, version2)

    def test_process_warn_filters_counties_and_alerts_correction(self):
        current = datetime(2026, 8, 24, tzinfo=timezone.utc)
        first = xlsx_fixture([
            [46200, "Tesla (Austin)", "Travis", "Capital Area WDA", 100, 46260, 46201, "Austin"],
            [46200, "Dallas Co", "Dallas", "Dallas WDA", 900, 46260, 46201, "Dallas"],
        ])
        state = {"warn_initialized": True, "warn_events": {}, "warn_files": {}}
        with mock.patch.object(m, "request_bytes", return_value=(200, first, {})):
            alerts, _ = m.process_warn(state, self.cfg, current)
        self.assertEqual(1, len(alerts))
        self.assertIn("100 jobs", alerts[0])
        corrected = xlsx_fixture([
            [46200, "Tesla (Austin)", "Travis", "Capital Area WDA", 120, 46260, 46201, "Austin"],
        ])
        with mock.patch.object(m, "request_bytes", return_value=(200, corrected, {})):
            alerts, _ = m.process_warn(state, self.cfg, current)
        self.assertEqual(1, len(alerts))
        self.assertIn("UPDATE", alerts[0])
        self.assertIn("120 jobs", alerts[0])


class StateTests(ConfigMixin, unittest.TestCase):
    def test_first_news_run_seeds_without_alerting(self):
        rss = (FIXTURES / "news.xml").read_bytes()
        with mock.patch.object(m, "request_bytes", return_value=(200, rss, {})):
            state = {}
            alerts, changed = m.process_news(state, self.cfg)
        self.assertTrue(changed)
        self.assertEqual([], alerts)
        self.assertTrue(state["news_initialized"])
        self.assertGreater(len(state["news_seen"]), 0)

    def test_initialized_news_run_alerts_current_qualifying_items_once(self):
        rss = (FIXTURES / "news.xml").read_bytes()
        state = {"news_initialized": True, "news_seen": {}, "news_clusters": {}}
        current = datetime(2026, 8, 24, 18, tzinfo=timezone.utc)
        with mock.patch.object(m, "request_bytes", return_value=(200, rss, {})):
            alerts, changed = m.process_news(state, self.cfg, now=current)
        self.assertTrue(changed)
        self.assertEqual(2, len(alerts))
        self.assertTrue(any("Oracle" in alert for alert in alerts))
        self.assertTrue(any("Tesla" in alert for alert in alerts))
        with mock.patch.object(m, "request_bytes", return_value=(200, rss, {})):
            repeated, _ = m.process_news(state, self.cfg, now=current)
        self.assertEqual([], repeated)

    def test_news_discards_stale_google_results(self):
        rss = (FIXTURES / "news.xml").read_bytes()
        state = {"news_initialized": True, "news_seen": {}, "news_clusters": {}}
        current = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with mock.patch.object(m, "request_bytes", return_value=(200, rss, {})):
            alerts, _ = m.process_news(state, self.cfg, now=current)
        self.assertEqual([], alerts)
        self.assertEqual({}, state["news_seen"])

    def test_slack_failure_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text("{}\n")
            with mock.patch.object(m, "STATE_FILE", state_path), \
                 mock.patch.object(m, "read_secrets", return_value=("xoxb-test", ["U1"])), \
                 mock.patch.object(m, "process_warn", side_effect=lambda s, c: (["alert"], True)), \
                 mock.patch.object(m, "process_news", return_value=([], False)), \
                 mock.patch.object(m, "post_slack", side_effect=OSError("down")):
                result = m.main([])
            self.assertEqual(1, result)
            self.assertEqual({}, json.loads(state_path.read_text()))

    def test_prunes_old_news_state(self):
        state = {
            "news_seen": {"old": "2026-01-01T00:00:00Z", "new": "2026-08-23T00:00:00Z"},
            "news_clusters": {
                "old": {"published": "2026-01-01T00:00:00Z"},
                "new": {"published": "2026-08-23T00:00:00Z"},
            },
        }
        m.prune_state(state, self.cfg, datetime(2026, 8, 24, tzinfo=timezone.utc))
        self.assertEqual({"new"}, set(state["news_seen"]))
        self.assertEqual({"new"}, set(state["news_clusters"]))


class OperationsTests(unittest.TestCase):
    def test_loop_and_workflow_have_expected_cadence(self):
        root = Path(__file__).parents[1]
        loop = (root / "loop.sh").read_text()
        workflow = (root / ".github/workflows/monitor.yml").read_text()
        self.assertIn("POLL_INTERVAL=${POLL_INTERVAL:-300}", loop)
        self.assertIn("MAX_RUNTIME=${MAX_RUNTIME:-20400}", loop)
        self.assertIn("COMMIT_EVERY=${COMMIT_EVERY:-1800}", loop)
        self.assertIn("cron: '0 * * * *'", workflow)
        self.assertIn("timeout-minutes: 355", workflow)


if __name__ == "__main__":
    unittest.main()
