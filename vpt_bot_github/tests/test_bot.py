import sys
import unittest
from unittest.mock import patch
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402


class ScheduleTests(unittest.TestCase):
    def test_parse_date_from_filename(self):
        self.assertEqual(
            bot.parse_date_from_name("17.11.2026 вторник 2 НЕДЕЛЯ.xlsx"),
            date(2026, 11, 17),
        )

    def test_parse_two_digit_year(self):
        self.assertEqual(bot.parse_date_from_name("01.09.26.xlsx"), date(2026, 9, 1))

    def test_invalid_date(self):
        self.assertIsNone(bot.parse_date_from_name("31.02.2026.xlsx"))

    def test_extract_group_pairs(self):
        rows = [
            ["1-26 РКЛ-3", "", "", ""],
            ["1", "Математика", "Иванов И.И.", "203"],
            ["2", "Русский язык", "Петрова А.А.", "105"],
            ["2-26 ДРУГАЯ", "", "", ""],
            ["1", "Не должна попасть", "", ""],
        ]
        self.assertEqual(
            bot.extract_group_pairs(rows, "РКЛ-3"),
            [(1, "Математика", "Иванов И.И.", "203"),
             (2, "Русский язык", "Петрова А.А.", "105")],
        )

    def test_bell_schedule_weekday_and_saturday(self):
        self.assertEqual(bot.bell(1, date(2026, 11, 16)), ("08:30", "10:00"))
        self.assertEqual(bot.bell(3, date(2026, 11, 21)), ("11:50", "13:20"))


class BotLogicTests(unittest.TestCase):
    def test_parse_commands(self):
        self.assertEqual(bot.parse_command("/today"), "/today")
        self.assertEqual(bot.parse_command("Новости"), "/7")
        self.assertEqual(bot.parse_command("/новости"), "/7")
        self.assertEqual(bot.parse_command("/новости@my_bot"), "/7")

    def test_music_removed_from_main_menu(self):
        labels = [button["text"] for row in bot.KB_MAIN["keyboard"] for button in row]
        self.assertFalse(any("Музыка" in label for label in labels))
        self.assertTrue(any("Новости" in label for label in labels))

    def test_news_digest_has_general_topics_without_music_cache(self):
        fake_items = [{"title": "Тестовая новость", "link": "https://example.com/news", "date": ""}]
        with patch.object(bot, "_rss_items", return_value=fake_items):
            digest = bot._collect_news_digest()
        self.assertIn("НОВОСТИ МИРА", digest)
        self.assertIn("НОВОСТИ РОССИИ", digest)
        self.assertIn("ТЕХНОЛОГИИ И ИНТЕРНЕТ", digest)
        self.assertNotIn("music_cache.json", digest)
        self.assertEqual(bot.parse_command("📦 Посылки"), "/3")

    def test_quiet_hours_regular_period(self):
        cfg = {"quiet_start": "03:00", "quiet_end": "08:00"}
        self.assertTrue(bot.in_quiet(bot.dt.datetime(2026, 11, 18, 4, 0, tzinfo=bot.MSK), cfg))
        self.assertFalse(bot.in_quiet(bot.dt.datetime(2026, 11, 18, 9, 0, tzinfo=bot.MSK), cfg))

    def test_quiet_hours_cross_midnight(self):
        cfg = {"quiet_start": "22:00", "quiet_end": "07:00"}
        self.assertTrue(bot.in_quiet(bot.dt.datetime(2026, 11, 18, 23, 0, tzinfo=bot.MSK), cfg))
        self.assertTrue(bot.in_quiet(bot.dt.datetime(2026, 11, 18, 6, 0, tzinfo=bot.MSK), cfg))
        self.assertFalse(bot.in_quiet(bot.dt.datetime(2026, 11, 18, 12, 0, tzinfo=bot.MSK), cfg))


if __name__ == "__main__":
    unittest.main()
