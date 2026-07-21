#!/usr/bin/env python3
"""Unit tests for the BERT exporter's CSV following, rotation handling and
metric rendering. Stdlib unittest, no external deps.

    cd bert-server && python3 -m unittest discover -s tests -v
"""
import json
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exporter  # noqa: E402

HEADER = "timestamp,board,hybrid,line,testedBits,errorCount\n"


def write(path, text, mode="a"):
    with open(path, mode) as fh:
        fh.write(text)


class ParseTimestampTest(unittest.TestCase):
    def test_utc_epoch(self):
        # 2026-07-20T00:00:00Z
        self.assertEqual(exporter.parse_timestamp("2026-07-20T00:00:00Z"), 1784505600.0)

    def test_rejects_junk(self):
        with self.assertRaises(ValueError):
            exporter.parse_timestamp("not-a-time")


class CsvSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bertContinuous.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def source(self):
        return exporter.CsvSource(explicit_path=self.path)

    def test_reads_latest_per_link_and_computes_ber(self):
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,12\n")
        write(self.path, "2026-07-20T08:31:30Z,0,1,3,10000000000,0\n")
        src = self.source()
        src.poll()
        snap = src.get_snapshot()

        self.assertEqual(snap["up"], 1)
        self.assertEqual(snap["active_series"], 2)
        self.assertEqual(snap["rows"], 2)
        self.assertAlmostEqual(snap["series"][(0, 0, 3)]["ber"], 12 / 1e10)
        self.assertEqual(snap["series"][(0, 0, 3)]["error_count"], 12)
        self.assertEqual(snap["series"][(0, 1, 3)]["ber"], 0.0)

    def test_latest_row_wins_for_a_link(self):
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,12\n")
        src = self.source()
        src.poll()
        write(self.path, "2026-07-20T08:32:00Z,0,0,3,10000000000,99\n")
        src.poll()
        self.assertEqual(src.get_snapshot()["series"][(0, 0, 3)]["error_count"], 99)

    def test_incremental_read_only_consumes_new_bytes(self):
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,1\n")
        src = self.source()
        src.poll()
        first_offset = src.offset
        src.poll()  # nothing new
        self.assertEqual(src.offset, first_offset)
        self.assertEqual(src.get_snapshot()["rows"], 1)

    def test_partial_trailing_line_deferred(self):
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,1\n")
        write(self.path, "2026-07-20T08:31:30Z,0,0,4,10000000000,2")  # no newline yet
        src = self.source()
        src.poll()
        self.assertEqual(src.get_snapshot()["rows"], 1)   # partial line not counted
        write(self.path, "\n")                             # completed later
        src.poll()
        self.assertEqual(src.get_snapshot()["rows"], 2)

    def test_zero_tested_bits_is_nan_not_zero(self):
        # BER is UNDEFINED when nothing was tested; must not read as a perfect link.
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,0,0\n")
        src = self.source()
        src.poll()
        rec = src.get_snapshot()["series"][(0, 0, 3)]
        self.assertTrue(math.isnan(rec["ber"]))
        self.assertTrue(math.isnan(rec["tested_bits"]))
        self.assertFalse(rec["valid"])

    def test_error_count_sentinel_becomes_nan(self):
        # getBitErrorCounters() returns 0xFFFFFFFF on read failure - not 4.29e9 errors.
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,4294967295\n")
        src = self.source()
        src.poll()
        rec = src.get_snapshot()["series"][(0, 0, 3)]
        self.assertTrue(math.isnan(rec["ber"]))
        self.assertTrue(math.isnan(rec["error_count"]))
        self.assertFalse(rec["valid"])
        self.assertEqual(src.get_snapshot()["invalid_samples"], 1)

    def test_implausible_tested_bits_becomes_nan(self):
        # frame-counter read failure -> astronomically large testedBits.
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,18446744073709551615,5\n")
        src = self.source()
        src.poll()
        rec = src.get_snapshot()["series"][(0, 0, 3)]
        self.assertTrue(math.isnan(rec["ber"]))
        self.assertTrue(math.isnan(rec["tested_bits"]))
        self.assertFalse(rec["valid"])

    def test_sentinel_row_still_counts_for_liveness(self):
        # a sentinel row is still a WRITE by the DAQ: it must advance the timestamp.
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,4294967295\n")
        src = self.source()
        src.poll()
        snap = src.get_snapshot()
        self.assertGreater(snap["last_sample_timestamp"], 0)
        self.assertEqual(snap["rows"], 1)
        self.assertEqual(snap["valid_series"], 0)

    def test_disabling_sentinel_check_keeps_value(self):
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,4294967295\n")
        src = exporter.CsvSource(explicit_path=self.path, error_sentinel=-1)
        src.poll()
        rec = src.get_snapshot()["series"][(0, 0, 3)]
        self.assertEqual(rec["error_count"], 4294967295)
        self.assertTrue(rec["valid"])

    def test_bad_rows_skipped_and_counted(self):
        write(self.path, HEADER)
        write(self.path, "garbage,line,with,too,few\n")
        write(self.path, "2026-07-20T08:31:00Z,x,0,3,10000000000,1\n")  # non-int board
        write(self.path, "2026-07-20T08:31:30Z,0,0,3,10000000000,1\n")  # good
        src = self.source()
        src.poll()
        snap = src.get_snapshot()
        self.assertEqual(snap["active_series"], 1)
        self.assertGreaterEqual(snap["parse_errors"], 1)

    def test_rotation_resets_state(self):
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,50\n")
        src = self.source()
        src.poll()
        # New run truncates/replaces the file at the same path.
        write(self.path, HEADER, mode="w")
        write(self.path, "2026-07-20T09:00:00Z,0,0,0,10000000000,0\n")
        src.poll()
        snap = src.get_snapshot()
        self.assertEqual(snap["rows"], 1)                 # counted from the new file only
        self.assertNotIn((0, 0, 3), snap["series"])       # old link gone
        self.assertIn((0, 0, 0), snap["series"])

    def test_missing_file_marks_down(self):
        src = self.source()
        src.poll()
        snap = src.get_snapshot()
        self.assertEqual(snap["up"], 0)
        self.assertEqual(snap["file_present"], 0)


class GlobResolutionTest(unittest.TestCase):
    def test_follows_newest_match(self):
        with tempfile.TemporaryDirectory() as root:
            old = os.path.join(root, "Results", "run1", "bertContinuous.csv")
            new = os.path.join(root, "Results", "run2", "bertContinuous.csv")
            for p in (old, new):
                os.makedirs(os.path.dirname(p))
                write(p, HEADER, mode="w")
            write(old, "2026-07-20T08:00:00Z,0,0,0,10000000000,1\n")
            write(new, "2026-07-20T09:00:00Z,0,0,9,10000000000,2\n")
            os.utime(old, (1000, 1000))       # make run1 clearly older
            os.utime(new, (2000, 2000))
            src = exporter.CsvSource(results_root=root, glob_pattern="**/bertContinuous.csv")
            src.poll()
            snap = src.get_snapshot()
            self.assertTrue(snap["path"].endswith("run2/bertContinuous.csv"))
            self.assertIn((0, 0, 9), snap["series"])


class StatusJsonTest(unittest.TestCase):
    def test_status_is_json_serialisable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bertContinuous.csv")
            write(path, HEADER, mode="w")
            write(path, "2026-07-20T08:31:00Z,0,1,3,10000000000,12\n")
            src = exporter.CsvSource(explicit_path=path)
            src.poll()
            payload = exporter.status_json(src.get_snapshot())
            text = json.dumps(payload)          # must not raise (tuple keys flattened)
        self.assertEqual(payload["active_series"], 1)
        self.assertEqual(payload["series"][0]["board"], 0)
        self.assertEqual(payload["series"][0]["error_count"], 12)
        self.assertIn("ber", payload["series"][0])
        self.assertIn('"line": 3', text)

    def test_nan_becomes_null_and_stays_strict_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bertContinuous.csv")
            write(path, HEADER, mode="w")
            write(path, "2026-07-20T08:31:00Z,0,0,3,0,0\n")   # undefined -> NaN
            src = exporter.CsvSource(explicit_path=path)
            src.poll()
            payload = exporter.status_json(src.get_snapshot())
            text = json.dumps(payload, allow_nan=False)       # would raise if NaN leaked
        self.assertIsNone(payload["series"][0]["ber"])
        self.assertNotIn("NaN", text)


class RenderTest(unittest.TestCase):
    def test_render_contains_expected_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bertContinuous.csv")
            write(path, HEADER, mode="w")
            write(path, "2026-07-20T08:31:00Z,0,1,3,10000000000,12\n")
            src = exporter.CsvSource(explicit_path=path)
            src.poll()
            text = exporter.render_metrics(src)
        self.assertIn("bert_up 1", text)
        self.assertIn('bert_bit_error_rate{board="0",hybrid="1",line="3"}', text)
        self.assertIn('bert_error_count{board="0",hybrid="1",line="3"} 12', text)
        self.assertIn("# TYPE bert_bit_error_rate gauge", text)
        self.assertIn("bert_last_sample_timestamp_seconds", text)
        # BER value rendered as a real float, not integer-truncated to 0
        self.assertIn("1.2e-09", text)

    def test_invalid_sample_renders_as_NaN(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bertContinuous.csv")
            write(path, HEADER, mode="w")
            write(path, "2026-07-20T08:31:00Z,0,0,3,10000000000,4294967295\n")  # sentinel
            src = exporter.CsvSource(explicit_path=path)
            src.poll()
            text = exporter.render_metrics(src)
        self.assertIn('bert_bit_error_rate{board="0",hybrid="0",line="3"} NaN', text)
        self.assertIn('bert_sample_valid{board="0",hybrid="0",line="3"} 0', text)
        self.assertIn("bert_invalid_samples_total 1", text)

    def test_render_never_raises_on_weird_state(self):
        # Even if the series holds unexpected types, render must produce text, not crash.
        src = exporter.CsvSource(explicit_path="/nonexistent")
        src.poll()                                   # up=0, empty series
        text = exporter.render_metrics(src)
        self.assertIn("bert_up 0", text)
        self.assertIn("bert_render_errors_total", text)


if __name__ == "__main__":
    unittest.main()


class RunTotalsTest(unittest.TestCase):
    """Each CSV row is one ~1 s window and testedBits does NOT accumulate in the
    file, so the run exposure only exists if the exporter sums it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bertContinuous.csv")
        write(self.path, HEADER)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self):
        return exporter.CsvSource(explicit_path=self.path)

    def test_totals_sum_windows_per_link(self):
        write(self.path, "2026-07-20T08:31:00Z,0,22,0,300000000,0\n")
        write(self.path, "2026-07-20T08:31:01Z,0,22,0,320000000,2\n")
        write(self.path, "2026-07-20T08:31:02Z,0,23,0,100000000,0\n")
        src = self.source()
        src.poll()
        snap = src.get_snapshot()

        t = snap["totals"][(0, 22, 0)]
        self.assertEqual(t["tested"], 620000000)
        self.assertEqual(t["errors"], 2)
        self.assertEqual(t["samples"], 2)
        # the latest sample still reports only its own window
        self.assertEqual(snap["series"][(0, 22, 0)]["tested_bits"], 320000000)
        self.assertEqual(snap["run_tested_bits"], 720000000)
        self.assertEqual(snap["run_errors"], 2)

    def test_totals_accumulate_across_polls(self):
        write(self.path, "2026-07-20T08:31:00Z,0,22,0,300000000,0\n")
        src = self.source()
        src.poll()
        write(self.path, "2026-07-20T08:31:01Z,0,22,0,300000000,0\n")
        src.poll()
        self.assertEqual(src.get_snapshot()["run_tested_bits"], 600000000)

    def test_invalid_rows_excluded_from_totals(self):
        # a sentinel errorCount must not add 4 billion errors to the run
        write(self.path, "2026-07-20T08:31:00Z,0,22,0,300000000,0\n")
        write(self.path, "2026-07-20T08:31:01Z,0,22,0,300000000,4294967295\n")
        src = self.source()
        src.poll()
        snap = src.get_snapshot()
        self.assertEqual(snap["run_errors"], 0)
        self.assertEqual(snap["run_tested_bits"], 300000000)
        self.assertEqual(snap["totals"][(0, 22, 0)]["samples"], 1)

    def test_new_run_dir_resets_totals(self):
        # the real scenario: the DAQ starts Run_43 in a new directory and the
        # glob follows it. The limit must restart from that run's exposure only.
        root = self.tmp.name
        r42 = os.path.join(root, "Run_42", "bertContinuous.csv")
        r43 = os.path.join(root, "Run_43", "bertContinuous.csv")
        os.makedirs(os.path.dirname(r42))
        os.makedirs(os.path.dirname(r43))
        write(r42, HEADER)
        write(r42, "2026-07-20T08:31:00Z,0,22,0,300000000,0\n")
        src = exporter.CsvSource(results_root=root, glob_pattern="**/bertContinuous.csv")
        src.poll()
        self.assertEqual(src.get_snapshot()["run_tested_bits"], 300000000)

        write(r43, HEADER)
        write(r43, "2026-07-20T09:00:00Z,0,22,0,100000000,0\n")
        os.utime(r43, (time.time() + 10, time.time() + 10))   # newest by mtime
        src.poll()
        snap = src.get_snapshot()
        self.assertTrue(snap["path"].endswith("Run_43/bertContinuous.csv"))
        self.assertEqual(snap["run_tested_bits"], 100000000,
                         "a new run must not inherit the previous run's exposure")

    def test_limit_falls_as_exposure_grows(self):
        write(self.path, "2026-07-20T08:31:00Z,0,22,0,1000000000,0\n")
        src = self.source()
        src.poll()
        first = src.get_snapshot()["run_ber_limit_95"]
        write(self.path, "2026-07-20T08:31:01Z,0,22,0,1000000000,0\n")
        src.poll()
        second = src.get_snapshot()["run_ber_limit_95"]
        self.assertAlmostEqual(first, 2.995732273553991 / 1e9)
        self.assertLess(second, first)
        self.assertAlmostEqual(second, 2.995732273553991 / 2e9)

    def test_limit_is_nan_before_any_exposure(self):
        src = self.source()
        src.poll()
        self.assertTrue(math.isnan(src.get_snapshot()["run_ber_limit_95"]))

    def test_totals_render_and_status_json(self):
        write(self.path, "2026-07-20T08:31:00Z,0,22,0,300000000,0\n")
        src = self.source()
        src.poll()
        text = exporter.render_metrics(src)
        self.assertIn('bert_tested_bits_total{board="0",hybrid="22",line="0"} 300000000', text)
        self.assertIn("bert_run_tested_bits_total 300000000", text)
        self.assertIn("bert_run_ber_upper_limit_95", text)
        # /status must stay strict JSON with the totals flattened in
        doc = json.loads(json.dumps(exporter.status_json(src.get_snapshot())))
        rec = doc["series"][0]
        self.assertEqual(rec["total_tested"], 300000000)
        self.assertEqual(rec["total_errors"], 0)


class FecColumnTest(unittest.TestCase):
    """fecUplink/fecDownlink, added to the CSV at Run_43. Six-column rows from
    earlier runs must keep working, and "column absent" must not read as zero."""

    FEC_HEADER = ("timestamp,board,hybrid,line,testedBits,errorCount,"
                  "fecUplink,fecDownlink\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bertContinuous.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def source(self):
        return exporter.CsvSource(explicit_path=self.path)

    def test_reads_both_fec_columns(self):
        write(self.path, self.FEC_HEADER)
        write(self.path, "2026-07-21T12:49:19Z,0,22,0,325996128,0,3,7\n")
        src = self.source()
        src.poll()
        rec = src.get_snapshot()["series"][(0, 22, 0)]
        self.assertEqual(rec["fec_uplink"], 3)
        self.assertEqual(rec["fec_downlink"], 7)

    def test_zero_fec_is_a_value_not_a_gap(self):
        # the whole point of the columns: 0 is the good result and must render.
        write(self.path, "2026-07-21T12:49:19Z,0,22,0,325996128,0,0,0\n")
        src = self.source()
        src.poll()
        text = exporter.render_metrics(src)
        self.assertIn('bert_fec_uplink{board="0",hybrid="22",line="0"} 0', text)
        self.assertIn('bert_fec_downlink{board="0",hybrid="22",line="0"} 0', text)
        self.assertIn("bert_run_fec_uplink_max 0", text)

    def test_six_column_rows_still_parse(self):
        write(self.path, HEADER)
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,12\n")
        src = self.source()
        src.poll()
        snap = src.get_snapshot()
        self.assertEqual(snap["rows"], 1)
        self.assertEqual(snap["parse_errors"], 0)
        self.assertAlmostEqual(snap["series"][(0, 0, 3)]["ber"], 12 / 1e10)
        self.assertIsNone(snap["series"][(0, 0, 3)]["fec_uplink"])

    def test_absent_column_emits_no_series_and_nan_run_max(self):
        # a pre-Run_43 file must not claim "0 FEC errors" - it has no idea.
        write(self.path, "2026-07-20T08:31:00Z,0,0,3,10000000000,0\n")
        src = self.source()
        src.poll()
        snap = src.get_snapshot()
        self.assertTrue(math.isnan(snap["run_fec_uplink_max"]))
        text = exporter.render_metrics(src)
        self.assertIn("bert_run_fec_uplink_max NaN", text)
        self.assertNotIn('bert_fec_uplink{', text)

    def test_run_max_is_worst_link_not_a_sum(self):
        # the counter is per optical group, so the DAQ repeats it on every hybrid
        # row sharing that group; summing would multiply-count it.
        write(self.path, self.FEC_HEADER)
        write(self.path, "2026-07-21T12:49:19Z,0,22,0,325996128,0,4,0\n")
        write(self.path, "2026-07-21T12:49:19Z,0,23,0,325996128,0,4,0\n")
        src = self.source()
        src.poll()
        self.assertEqual(src.get_snapshot()["run_fec_uplink_max"], 4)

    def test_run_max_keeps_the_peak_after_it_drops(self):
        write(self.path, self.FEC_HEADER)
        write(self.path, "2026-07-21T12:49:19Z,0,22,0,325996128,0,9,0\n")
        src = self.source()
        src.poll()
        write(self.path, "2026-07-21T12:49:20Z,0,22,0,325996128,0,1,0\n")
        src.poll()
        snap = src.get_snapshot()
        self.assertEqual(snap["series"][(0, 22, 0)]["fec_uplink"], 1)   # latest
        self.assertEqual(snap["fec"][(0, 22, 0)]["uplink_max"], 9)      # run peak

    def test_fec_sentinel_becomes_nan_without_voiding_the_ber(self):
        write(self.path, self.FEC_HEADER)
        write(self.path, "2026-07-21T12:49:19Z,0,22,0,325996128,0,4294967295,0\n")
        src = self.source()
        src.poll()
        rec = src.get_snapshot()["series"][(0, 22, 0)]
        self.assertTrue(math.isnan(rec["fec_uplink"]))
        self.assertTrue(rec["valid"])          # the BER sample is untouched
        self.assertEqual(rec["ber"], 0.0)

    def test_malformed_fec_does_not_drop_the_ber_row(self):
        write(self.path, self.FEC_HEADER)
        write(self.path, "2026-07-21T12:49:19Z,0,22,0,325996128,0,oops,-1\n")
        src = self.source()
        src.poll()
        snap = src.get_snapshot()
        self.assertEqual(snap["parse_errors"], 0)
        rec = snap["series"][(0, 22, 0)]
        self.assertEqual(rec["ber"], 0.0)
        self.assertIsNone(rec["fec_uplink"])
        self.assertIsNone(rec["fec_downlink"])

    def test_new_run_resets_fec_peak(self):
        root = self.tmp.name
        r42 = os.path.join(root, "Run_42", "bertContinuous.csv")
        r43 = os.path.join(root, "Run_43", "bertContinuous.csv")
        for path in (r42, r43):
            os.makedirs(os.path.dirname(path))
        write(r42, self.FEC_HEADER + "2026-07-21T12:49:19Z,0,22,0,325996128,0,9,0\n")
        src = exporter.CsvSource(results_root=root)
        src.poll()
        self.assertEqual(src.get_snapshot()["run_fec_uplink_max"], 9)

        write(r43, self.FEC_HEADER + "2026-07-21T13:00:00Z,0,22,0,325996128,0,1,0\n")
        os.utime(r43, (time.time() + 10, time.time() + 10))   # newest by mtime
        src.poll()
        self.assertEqual(src.get_snapshot()["run_fec_uplink_max"], 1)

    def test_fec_in_status_json(self):
        write(self.path, self.FEC_HEADER)
        write(self.path, "2026-07-21T12:49:19Z,0,22,0,325996128,0,2,5\n")
        src = self.source()
        src.poll()
        doc = json.loads(json.dumps(exporter.status_json(src.get_snapshot())))
        rec = doc["series"][0]
        self.assertEqual(rec["fec_uplink"], 2)
        self.assertEqual(rec["fec_downlink"], 5)
        self.assertEqual(rec["fec_uplink_max"], 2)
        self.assertEqual(doc["run_fec_downlink_max"], 5)


class PoissonLimitTest(unittest.TestCase):
    def test_zero_errors_is_exact(self):
        self.assertAlmostEqual(exporter.poisson_upper_limit_95(0), 2.995732273553991)

    def test_approximation_tracks_exact_for_small_k(self):
        # exact 0.5*chi2_0.95(2k+2) for k = 1..5
        exact = {1: 4.7439, 2: 6.2958, 3: 7.7537, 4: 9.1535, 5: 10.5130}
        for k, want in exact.items():
            got = exporter.poisson_upper_limit_95(k)
            self.assertLess(abs(got - want) / want, 0.01, f"k={k}: {got} vs {want}")

    def test_limit_undefined_without_exposure(self):
        self.assertTrue(math.isnan(exporter.ber_limit(0, 0)))
