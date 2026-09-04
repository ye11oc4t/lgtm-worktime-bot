import unittest
from datetime import date

from time_utils import format_duration, grass_level, parse_local_date, parse_month, render_grass


class TimeUtilsTest(unittest.TestCase):
    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(0), "0시간 0분 0초")
        self.assertEqual(format_duration(3661), "1시간 1분 1초")
        self.assertEqual(format_duration(-10), "0시간 0분 0초")

    def test_parse_local_date(self) -> None:
        default = date(2026, 9, 4)
        self.assertEqual(parse_local_date(None, default), default)
        self.assertEqual(parse_local_date("2026-09-03", default), date(2026, 9, 3))
        with self.assertRaises(ValueError):
            parse_local_date("09/03/2026", default)

    def test_parse_month(self) -> None:
        default = date(2026, 9, 4)
        self.assertEqual(parse_month(None, default), (2026, 9))
        self.assertEqual(parse_month("2026-08", default), (2026, 8))
        with self.assertRaises(ValueError):
            parse_month("2026/08", default)

    def test_grass_level_uses_fixed_24_hour_scale(self) -> None:
        self.assertEqual(grass_level(0), "⬛")
        self.assertEqual(grass_level(1), "🟦")
        self.assertEqual(grass_level(6 * 3600), "🟨")
        self.assertEqual(grass_level(12 * 3600), "🟧")
        self.assertEqual(grass_level(18 * 3600), "🟥")
        self.assertEqual(grass_level(30 * 3600), "🟥")

    def test_render_grass(self) -> None:
        rendered = render_grass(2026, 9, {date(2026, 9, 1): 8 * 3600})
        self.assertEqual(len(rendered.splitlines()), 7)
        self.assertIn("🟨", rendered)


if __name__ == "__main__":
    unittest.main()
