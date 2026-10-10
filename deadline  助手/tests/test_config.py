"""No user accounts or private campus content in these fixtures."""

import unittest

from manaba_deadline.config import ConfigError, DEFAULT_URL, ScraperConfig, normalize_home_url


class ConfigTests(unittest.TestCase):
    def test_defaults_and_safe_serialization(self):
        config = ScraperConfig()
        self.assertEqual(config.interval_seconds, 600)
        self.assertEqual(config.timezone, "Asia/Tokyo")
        self.assertFalse(config.headless)
        self.assertEqual(config.origin, "https://manaba.ryukoku.ac.jp")
        self.assertEqual(set(config.as_dict()), {"url", "interval_seconds", "timeout_seconds", "throttle_seconds", "max_pages", "max_courses", "headless"})

    def test_supported_pasted_urls_canonicalize(self):
        for suffix in ("", "/", "/ct", "/ct/", "/ct/home", "/ct/home_course", "/ct/home_library_query", "/ct/home_library_query___"):
            with self.subTest(suffix=suffix):
                self.assertEqual(normalize_home_url(f" https://manaba.ryukoku.ac.jp{suffix} "), DEFAULT_URL)

    def test_other_sites_and_private_url_components_rejected(self):
        for url in (
            "https://example.org/ct/home", "http://manaba.ryukoku.ac.jp/ct/home",
            "https://manaba.ryukoku.ac.jp.evil.example/ct/home",
            "https://user:secret@manaba.ryukoku.ac.jp/ct/home",
            "https://manaba.ryukoku.ac.jp:443/ct/home", "https://manaba.ryukoku.ac.jp/ct/home?account=secret",
            "https://manaba.ryukoku.ac.jp/ct/home#secret", "https://manaba.ryukoku.ac.jp/ct/course_123",
            "https://manaba.ryukoku.ac.jp/ct/home_library_query___unsafe", "https://[bad", "", None,
        ):
            with self.subTest(url=url):
                with self.assertRaises(ConfigError) as error:
                    normalize_home_url(url)
                self.assertNotIn("secret", str(error.exception))

    def test_invalid_numbers_rejected(self):
        for values in (
            {"interval_seconds": 59}, {"interval_seconds": True}, {"interval_seconds": float("inf")},
            {"interval_seconds": float("nan")}, {"timeout_seconds": 0}, {"timeout_seconds": 301},
            {"throttle_seconds": 0.1}, {"max_pages": 0}, {"max_pages": 3.5}, {"max_courses": False},
            {"headless": "false"}, {"timeout_seconds": "30"},
        ):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                ScraperConfig(**values)


if __name__ == "__main__":
    unittest.main()
