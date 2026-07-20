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
