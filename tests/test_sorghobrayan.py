import math
import unittest
from jinx_profit.donor_sorghobrayan import (
    BASE_NOTCH, decode_notch, equal_capital_allocations,
    donor_absolute_allocations, predict_notches,
)

class SorghobrayanTests(unittest.TestCase):
    def test_original_inverse_log_notch(self):
        self.assertAlmostEqual(decode_notch(math.log1p(40)), 40)
        self.assertEqual(decode_notch(math.log1p(2)), 24)
        self.assertEqual(decode_notch(math.log1p(200)), 120)

    def test_equal_capital_redistribution(self):
        sizes = equal_capital_allocations([30, 60, 90], 100)
        self.assertEqual([round(x, 5) for x in sizes], [50,100,150])
        self.assertAlmostEqual(sum(sizes), 300)

    def test_usd_conversion_of_donor_notches(self):
        amounts = donor_absolute_allocations([40], 150)
        self.assertAlmostEqual(amounts[0], 40 * BASE_NOTCH * 150)

    def test_model_is_required(self):
        with self.assertRaises(ValueError):
            predict_notches(None, [{"compute_units": 2}], ["compute_units"])

    def test_bad_allocations_fail(self):
        with self.assertRaises(ValueError):
            equal_capital_allocations([0], 100)
        with self.assertRaises(ValueError):
            decode_notch(float("nan"))

if __name__ == "__main__":
    unittest.main()
