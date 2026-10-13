import unittest

from voice_qa.config import is_e164, normalize_phone


class NormalizePhoneTests(unittest.TestCase):
    def test_normalizes_us_number(self):
        self.assertEqual(normalize_phone("(415) 555-0123"), "+14155550123")

    def test_preserves_international_country_code(self):
        self.assertEqual(normalize_phone("+44 20 7946 0958"), "+442079460958")

    def test_empty_value(self):
        self.assertEqual(normalize_phone("  "), "")

    def test_nondigits_are_empty(self):
        self.assertEqual(normalize_phone("not-a-number"), "")

    def test_e164_validation(self):
        self.assertTrue(is_e164("+14155550123"))
        self.assertFalse(is_e164("+123"))


if __name__ == "__main__":
    unittest.main()
