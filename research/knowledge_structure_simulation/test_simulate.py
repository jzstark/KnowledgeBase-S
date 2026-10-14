"""Check the simulation against analytic cases, not desired improvement."""
import unittest

import numpy as np

from simulate import observations, policy_orders, recall_curve, relevance
from summarize_density import paired_bootstrap, target_costs


class SimulationChecks(unittest.TestCase):
    def test_bootstrap_preserves_paired_equal_curves(self):
        curves = np.array([[0, 0.5, 1], [0, 1, 1]], dtype=float)
        recalls = np.repeat(curves[:, None, :], 3, axis=1)
        flat, hierarchy, reductions = paired_bootstrap(recalls, np.array([0, 1, 2]), 0.9, repeats=50)
        np.testing.assert_array_equal(flat, hierarchy)
        np.testing.assert_array_equal(reductions, np.zeros(50))

    def test_target_cost_marks_unreached_instead_of_free(self):
        costs = target_costs(np.array([[0, 0.5, 1], [0, 0.5, 0.8]]), np.array([0, 1, 2]), 0.9)
        self.assertEqual(costs[0], 2)
        self.assertTrue(np.isnan(costs[1]))

    def test_zero_depth_is_flat(self):
        scores = np.array([0.1, 2, -1, 0.3])
        orders, visits = policy_orders(scores, [], 2, 0.1, 0.9, 0.1, 0)
        for order in orders:
            np.testing.assert_array_equal(order, [1, 3, 0, 2])
        self.assertEqual(visits, [[], [], []])

    def test_summary_cost_is_charged_and_empty_is_not_full_recall(self):
        truth = np.array([True, False, True])
        curve, counts = recall_curve(truth, np.array([0, 1, 2]), 2.5,
                                     np.array([0, 2.5, 3.4, 3.5, 5.5]))
        np.testing.assert_array_equal(curve, [0, 0, 0, 0.5, 1])
        self.assertEqual(int(counts[-1]), 3)
        curve, _ = recall_curve(np.zeros(3, dtype=bool), np.arange(3), 0, np.array([3]))
        self.assertTrue(np.isnan(curve[0]))

    def test_pruned_paths_are_not_inspected_and_fallback_covers_every_leaf(self):
        signals = [np.array([True, False, True, True]), np.array([False, True])]
        orders, visits = policy_orders(np.arange(7), signals, 2, 0.1, 0.95, 0.05, 0)
        self.assertEqual(visits[1], [4, 2])
        self.assertEqual(visits[2], [2, 2])
        np.testing.assert_array_equal(orders[2][:3], [6, 5, 4])
        for order in orders:
            np.testing.assert_array_equal(np.sort(order), np.arange(7))

    def test_uninformative_summaries_cannot_improve_flat_ranking(self):
        scores = np.array([0.3, -0.2, 1, 0.4])
        orders, _ = policy_orders(scores, [np.array([True, False])], 2, 0.1, 0.5, 0.5, 0)
        np.testing.assert_array_equal(orders[0], orders[1])

    def test_two_stage_cost_matches_analytic_expectation(self):
        rng = np.random.default_rng(725)
        n, g, p, s, f, c = 70, 7, 0.03, 0.85, 0.1, 1.53
        truth = rng.random((20000, n // g, g)) < p
        positive = rng.random((20000, n // g)) < np.where(truth.any(axis=2), s, f)
        measured = n / g * c + g * positive.sum(axis=1)
        q = 1 - (1 - p) ** g
        expected = n / g * c + n * (q * s + (1 - q) * f)
        se = measured.std(ddof=1) / np.sqrt(len(measured))
        self.assertLess(abs(measured.mean() - expected), 5 * se)
        # Verify the production observation generator's non-oracle noise too.
        samples = []
        for row in truth[:1000]:
            signal = observations(rng, row.ravel(), g, 1, s, f, 0)[0]
            samples.append(n / g * c + g * signal.sum())
        se = np.std(samples, ddof=1) / np.sqrt(len(samples))
        self.assertLess(abs(np.mean(samples) - expected), 5 * se)

    def test_cluster_model_has_expected_marginal_and_correlation(self):
        rng = np.random.default_rng(326)
        p, rho = 0.2, 0.15
        samples = np.array([relevance(rng, 200, p, rho, 2) for _ in range(2000)])
        self.assertAlmostEqual(float(samples.mean()), p, delta=0.004)
        correlation = np.corrcoef(samples[:, ::2].ravel(), samples[:, 1::2].ravel())[0, 1]
        self.assertAlmostEqual(float(correlation), rho, delta=0.012)


if __name__ == "__main__":
    unittest.main()
