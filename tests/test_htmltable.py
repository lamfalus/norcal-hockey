"""Tests for the tolerant table parser, focused on the malformed markup the
site actually serves."""

import unittest

from norcalstats.htmltable import all_tables, find_table, parse_tables, to_int


class TestMalformedMarkup(unittest.TestCase):
    def test_cell_opened_as_td_closed_as_th(self):
        # The scoresheet's period-score table does exactly this.
        html = "<table><tr><th>Visitor</th><td>San Jose Jr Sharks</th><td>4</td></tr></table>"
        table = parse_tables(html)[0]
        self.assertEqual(table[0].texts, ["Visitor", "San Jose Jr Sharks", "4"])

    def test_unclosed_cells(self):
        html = "<table><tr><td>a<td>b<td>c</tr></table>"
        self.assertEqual(parse_tables(html)[0][0].texts, ["a", "b", "c"])

    def test_uppercase_tags(self):
        html = "<TABLE><TR><TD align=center>7</TD><TD>Nine</TD></TR></TABLE>"
        self.assertEqual(parse_tables(html)[0][0].texts, ["7", "Nine"])

    def test_stray_close_tags_do_not_corrupt(self):
        html = "<table><tr><td>a</td></tr></tr></table></tr><table><tr><td>b</td></tr></table>"
        tables = all_tables(html)
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0][0].texts, ["a"])
        self.assertEqual(tables[1][0].texts, ["b"])

    def test_nbsp_is_whitespace(self):
        html = "<table><tr><td>&nbsp;Fri Aug 29&nbsp;</td></tr></table>"
        self.assertEqual(parse_tables(html)[0][0].text(0), "Fri Aug 29")

    def test_cells_outside_a_row(self):
        html = "<table><td>lonely</td></table>"
        self.assertEqual(parse_tables(html)[0][0].texts, ["lonely"])


class TestNesting(unittest.TestCase):
    def test_nested_table_does_not_leak_into_parent_cell(self):
        html = ("<table><tr><td>outer"
                "<table><tr><td>inner</td></tr></table>"
                "</td><td>after</td></tr></table>")
        top = parse_tables(html)
        self.assertEqual(len(top), 1, "nested table must not be top-level")
        row = top[0][0]
        self.assertEqual(row.text(0), "outer")
        self.assertEqual(row.text(1), "after")
        self.assertEqual(len(row[0].tables), 1)
        self.assertEqual(row[0].tables[0][0].texts, ["inner"])

    def test_document_order_matches_queryselectorall(self):
        # The original JS scrapers indexed document.querySelectorAll('table').
        html = ("<table><tr><td>0<table><tr><td>1</td></tr></table></td></tr></table>"
                "<table><tr><td>2</td></tr></table>")
        tables = all_tables(html)
        self.assertEqual(len(tables), 3)
        self.assertEqual(tables[1][0].text(0), "1")
        self.assertEqual(tables[2][0].text(0), "2")


class TestAttributes(unittest.TestCase):
    def test_row_and_cell_attributes_are_kept(self):
        html = ("<table><tr data-id='9' data-parent='1'>"
                "<td colspan=2 bgcolor='#BBBBBB'>x</td></tr></table>")
        row = parse_tables(html)[0][0]
        self.assertEqual(row.attrs["data-id"], "9")
        self.assertEqual(row.attrs["data-parent"], "1")
        self.assertEqual(row[0].colspan, 2)
        self.assertEqual(row[0].attrs["bgcolor"], "#BBBBBB")

    def test_links_collected_per_cell(self):
        html = ("<table><tr><td><a href='oss-scoresheet?game_id=5'>5</a></td>"
                "<td><a href='x'>x</a></td></tr></table>")
        row = parse_tables(html)[0][0]
        self.assertEqual(row[0].links, ["oss-scoresheet?game_id=5"])
        self.assertEqual(row.links, ["oss-scoresheet?game_id=5", "x"])

    def test_script_and_style_content_ignored(self):
        html = ("<table><tr><td>keep<script>var t='drop';</script>"
                "<style>td{color:red}</style></td></tr></table>")
        self.assertEqual(parse_tables(html)[0][0].text(0), "keep")


class TestHelpers(unittest.TestCase):
    def test_to_int_tolerates_noise(self):
        self.assertEqual(to_int("50647*"), 50647)
        self.assertEqual(to_int(""), 0)
        self.assertEqual(to_int("-59"), -59)
        self.assertEqual(to_int("n/a", -1), -1)

    def test_find_table_by_heading(self):
        html = ("<table><tr><th>Penalties</th></tr></table>"
                "<table><tr><th>Scoring</th></tr></table>")
        found = find_table(all_tables(html), "scoring")
        self.assertIsNotNone(found)
        self.assertEqual(found[0].text(0), "Scoring")

    def test_row_text_out_of_range_returns_default(self):
        row = parse_tables("<table><tr><td>a</td></tr></table>")[0][0]
        self.assertEqual(row.text(5), "")
        self.assertEqual(row.text(5, "?"), "?")


if __name__ == "__main__":
    unittest.main()
