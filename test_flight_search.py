import json
import sqlite3
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import flight_search as fs


def config():
    return {
        "search": {
            "origins": ["BUD", "VIE"],
            "destinations": ["TYO", "OSA"],
            "date_start": "2027-03-22",
            "date_end": "2027-04-03",
            "trip_min_days": 12,
            "trip_max_days": 12,
            "max_stops": 2,
            "checked_bags": 1,
            "adults": 2,
            "step_days": 1,
        },
        "costs": {
            "shinkansen_eur": 90.0,
            "domestic_flight_eur": 65.0,
            "flixbus_eur": 15.0,
        },
        "currency": {"huf_per_eur": 400.0, "eur_per_gbp": 1.2},
        "cache": {"ttl_hours": 4, "max_rank_age_hours": 12},
        "scan": {
            "workers": 1,
            "requests_per_second": 1000,
            "top_n": 40,
            "history_top_n": 6,
        },
        "ranking": {"max_leg_hours": 30},
        "skyscanner": {
            "enabled": False,
            "max_age_hours": 36,
            "currency": "HUF",
        },
        "history": {"retention_days": 180},
    }


def detail(route, hours=18):
    start, end = route.split(" -> ")[0], route.split(" -> ")[-1]
    return {
        "price": 500.0,
        "route": route,
        "stops": route.count(" -> ") - 1,
        "dur_h": hours,
        "airlines": ["Test Air"],
        "legs": [{"from": start, "to": end, "date": "2027-03-22"}],
    }


class RankingTests(unittest.TestCase):
    def test_round_trip_endpoint_cost_and_open_jaw_uniqueness(self):
        cfg = config()
        rows = {
            ("RT", "VIE", "TYO", "2027-03-22", "2027-04-03"): detail("VIE -> HND"),
            ("OW", "VIE", "OSA", "2027-03-22", ""): detail("VIE -> KIX"),
            ("OW", "TYO", "VIE", "2027-04-03", ""): detail("HND -> VIE"),
        }

        itins = fs.build_itineraries(cfg, rows)
        rt = next(it for it in itins if it["kind"] == "RT")
        oj = [it for it in itins if it["kind"] == "OJ"]

        self.assertEqual(rt["ret_dest"], "VIE")
        self.assertEqual(rt["transfers"], 185.0)
        self.assertEqual(len(oj), 1)
        self.assertEqual(len({it["key"] for it in itins}), len(itins))

    def test_cache_key_contains_passenger_and_query_semantics(self):
        cfg = config()
        key = fs.key_of("RT", "BUD", "TYO", "2027-03-22", "2027-04-03", cfg)
        self.assertIn("|economy|EUR|en|2|", key)
        cfg["search"]["adults"] = 3
        self.assertNotEqual(
            key,
            fs.key_of("RT", "BUD", "TYO", "2027-03-22", "2027-04-03", cfg),
        )

    def test_normalizes_party_total_and_filters_long_options(self):
        cfg = config()
        itins = [detail("BUD -> HND", 35), detail("BUD -> HND", 20)]
        itins[0]["price"] = 800
        itins[1]["price"] = 1000
        fs.normalize_per_person(itins, 2)
        price, selected = fs.summarize(itins, cfg)
        self.assertEqual(price, 500)
        self.assertEqual(selected["party_price"], 1000)

    def test_all_options_over_max_leg_hours_raise_too_long(self):
        cfg = config()
        itins = [detail("BUD -> HND", 35)]
        with self.assertRaises(fs.TooLongError):
            fs.summarize(itins, cfg)


class FailureTests(unittest.TestCase):
    @patch("flight_search.time.sleep")
    @patch("flight_search.fetch_html", side_effect=OSError("network down"))
    def test_transport_errors_are_retried_and_returned(self, fetch, _sleep):
        itins, error, suggestions, client, page = fs.fetch_with_retry(None)
        self.assertIsNone(itins)
        self.assertIn("network down", error)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(suggestions, [])
        self.assertIsNone(client)
        self.assertIsNone(page)

    @patch(
        "flight_search.fetch_with_retry",
        return_value=(None, "OSError: temporary", [], None, None),
    )
    def test_failed_refresh_retains_last_good_price(self, _fetch):
        cfg = config()
        cfg["search"]["origins"] = ["BUD"]
        cfg["search"]["destinations"] = ["TYO"]
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        key = fs.key_of("RT", "BUD", "TYO", "2027-03-22", "2027-04-03", cfg)
        conn.execute(
            """INSERT INTO price_cache
               (key, kind, origin, dest, d1, d2, price, n_results, detail, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                "RT",
                "BUD",
                "TYO",
                "2027-03-22",
                "2027-04-03",
                700.0,
                1,
                json.dumps(detail("BUD -> HND")),
                "2026-01-01T00:00:00Z",
            ),
        )
        args = SimpleNamespace(workers=1, step=0, rank_only=False, force=True, limit=1)

        progress = fs.run_scan(cfg, conn, args, "2026-09-13T00:00:00Z")
        row = conn.execute(
            "SELECT price, fetched_at, last_attempt_at, last_error "
            "FROM price_cache WHERE key=?",
            (key,),
        ).fetchone()

        self.assertEqual(progress["fail"], 1)
        self.assertEqual(row[0], 700.0)
        self.assertEqual(row[1], "2026-01-01T00:00:00Z")
        self.assertIsNotNone(row[2])
        self.assertIn("temporary", row[3])

    @patch(
        "flight_search.fetch_with_retry",
        return_value=(None, "OSError: persistent", [], None, None),
    )
    def test_failed_query_does_not_starve_unseen_queries(self, _fetch):
        cfg = config()
        cfg["search"]["origins"] = ["BUD"]
        cfg["search"]["destinations"] = ["TYO"]
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        args = SimpleNamespace(workers=1, step=0, rank_only=False, force=False, limit=1)

        fs.run_scan(cfg, conn, args, "2026-09-13T00:00:00Z")
        fs.run_scan(cfg, conn, args, "2026-09-13T01:00:00Z")

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM price_cache").fetchone()[0], 2
        )

    @patch("flight_search.fetch_return_legs")
    @patch("flight_search.fetch_with_retry")
    def test_invalid_return_is_not_recorded_as_success(self, fetch, returns):
        cfg = config()
        cfg["search"]["origins"] = ["BUD"]
        cfg["search"]["destinations"] = ["TYO"]
        outbound = detail("BUD -> HND", 20)
        outbound["price"] = 2000
        outbound["legs"] = [{"from": "BUD", "to": "HND", "date": "2027-03-22"}]
        returned = detail("HND -> BUD", 40)
        returned["price"] = 2000
        returned["legs"] = [{"from": "HND", "to": "BUD", "date": "2027-04-03"}]
        fetch.return_value = ([outbound], None, [], object(), "page")
        returns.return_value = [returned]
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        args = SimpleNamespace(workers=1, step=0, rank_only=False, force=False, limit=1)

        progress = fs.run_scan(cfg, conn, args, "2026-09-13T00:00:00Z")

        self.assertEqual(progress["fail"], 1)
        self.assertIsNone(conn.execute("SELECT price FROM price_cache").fetchone()[0])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 0
        )

    @patch("flight_search.fetch_return_legs")
    @patch("flight_search.fetch_with_retry")
    def test_next_outbound_is_used_when_cheapest_return_is_invalid(
        self, fetch, returns
    ):
        cfg = config()
        cfg["search"]["origins"] = ["BUD"]
        cfg["search"]["destinations"] = ["TYO"]
        first = detail("BUD -> HND", 20)
        first["price"] = 2000
        first["legs"] = [{"from": "BUD", "to": "HND", "date": "2027-03-22"}]
        second = deepcopy(first)
        second["price"] = 2200
        invalid_return = detail("HND -> BUD", 40)
        invalid_return["price"] = 2000
        invalid_return["legs"] = [{"from": "HND", "to": "BUD", "date": "2027-04-03"}]
        valid_return = deepcopy(invalid_return)
        valid_return["price"] = 2100
        valid_return["dur_h"] = 20
        fetch.return_value = ([first, second], None, [], object(), "page")
        returns.side_effect = [[invalid_return], [valid_return]]
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        args = SimpleNamespace(workers=1, step=0, rank_only=False, force=False, limit=1)

        progress = fs.run_scan(cfg, conn, args, "2026-09-13T00:00:00Z")

        self.assertEqual(progress["fail"], 0)
        self.assertEqual(
            conn.execute("SELECT price FROM price_cache").fetchone()[0], 1050
        )
        self.assertEqual(returns.call_count, 2)


class SkyscannerTests(unittest.TestCase):
    def test_currency_conversion_is_explicit(self):
        self.assertEqual(fs._ss_eur(40000, "HUF", 400, 1.2), 100)
        self.assertEqual(fs._ss_eur(100, "GBP", 400, 1.2), 120)
        self.assertEqual(fs._ss_eur(100, "EUR", 400, 1.2), 100)
        with self.assertRaises(ValueError):
            fs._ss_eur(100, "USD", 400, 1.2)

    def test_fast_payload_uses_configured_party_size(self):
        payload = fs._ss_fast_payload(
            "BUD", "TYO", "2027-03-22", "2027-04-03", adults=2
        )
        self.assertEqual(payload["adults"], 2)

    def test_identity_is_stable_across_price_changes(self):
        cfg = config()
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deal = {
            "price_raw": 300000,
            "price_fmt": "300 000 Ft",
            "currency": "HUF",
            "eur": 375.0,
            "agents": ["Agent"],
            "self_transfer": True,
            "protected": False,
            "legs": [
                {
                    "from": "BUD",
                    "to": "HND",
                    "stops": 1,
                    "dep": "2027-03-22T10:00",
                    "arr": "2027-03-23T10:00",
                    "dur_min": 1200,
                    "carriers": ["Airline"],
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                    "arr": "2027-04-03T20:00",
                    "dur_min": 1200,
                    "carriers": ["Airline"],
                },
            ],
        }
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            100,
            json.dumps([deal]),
            fetched,
            2,
            "HUF",
        )
        first = fs._ss_itineraries(cfg, [row])
        changed = deepcopy(deal)
        changed["eur"] = 350.0
        changed["price_fmt"] = "280 000 Ft"
        second_row = (*row[:5], json.dumps([changed]), *row[6:])
        second = fs._ss_itineraries(cfg, [second_row])

        self.assertEqual(first[0]["key"], second[0]["key"])
        self.assertFalse(first[0]["bag_included"])
        self.assertTrue(first[0]["self_transfer"])

    def test_over_stop_limit_is_excluded(self):
        cfg = config()
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deal = {
            "eur": 300,
            "agents": ["Agent"],
            "legs": [
                {"from": "BUD", "to": "HND", "stops": 3},
                {"from": "HND", "to": "BUD", "stops": 1},
            ],
        }
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            1,
            json.dumps([deal]),
            fetched,
            2,
            "HUF",
        )
        self.assertEqual(fs._ss_itineraries(cfg, [row]), [])

    def test_invalid_deals_are_skipped_before_accepting_eligible_ones(self):
        cfg = config()
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        invalid = {
            "eur": 200,
            "agents": ["Invalid"],
            "legs": [
                {
                    "from": "BUD",
                    "to": "HND",
                    "stops": 3,
                    "dep": "2027-03-22T10:00",
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                },
            ],
        }
        valid = deepcopy(invalid)
        valid["eur"] = 300
        valid["agents"] = ["Valid"]
        valid["legs"][0]["stops"] = 1
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            3,
            json.dumps([invalid, invalid, valid]),
            fetched,
            2,
            "HUF",
        )

        result = fs._ss_itineraries(cfg, [row])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["agent"], "Valid")

    def test_deal_without_google_match_is_still_ranked(self):
        cfg = config()
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deal = {
            "eur": 300,
            "agents": ["Agent"],
            "legs": [
                {
                    "from": "BUD",
                    "to": "HND",
                    "stops": 1,
                    "dep": "2027-03-22T10:00",
                    "arr": "2027-03-23T10:00",
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                    "arr": "2027-04-03T20:00",
                },
            ],
        }
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            1,
            json.dumps([deal]),
            fetched,
            2,
            "HUF",
        )

        result = fs._ss_itineraries(cfg, [row])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ota_base_fare"], 300)
        self.assertEqual(result[0]["bag_estimate"], 0)

    def test_up_to_five_unique_deals_per_pair_are_accepted(self):
        cfg = config()
        cfg["skyscanner"]["checked_bag_estimate_eur"] = 0
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deals = []
        for i in range(6):
            deals.append(
                {
                    "eur": 300 + i,
                    "agents": [f"Agent{i}"],
                    "legs": [
                        {
                            "from": "BUD",
                            "to": "HND",
                            "stops": 1,
                            "dep": "2027-03-22T10:00",
                            "arr": "2027-03-23T10:00",
                        },
                        {
                            "from": "HND",
                            "to": "BUD",
                            "stops": 1,
                            "dep": "2027-04-03T10:00",
                            "arr": "2027-04-03T20:00",
                        },
                    ],
                }
            )
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            6,
            json.dumps(deals),
            fetched,
            2,
            "HUF",
        )

        result = fs._ss_itineraries(cfg, [row])

        self.assertEqual(len(result), 5)

    def test_stale_rows_become_indicative_then_expire(self):
        cfg = config()
        cfg["skyscanner"]["max_age_hours"] = 168
        cfg["skyscanner"]["indicative_after_hours"] = 24
        deal = {
            "eur": 300,
            "agents": ["Agent"],
            "legs": [
                {
                    "from": "BUD",
                    "to": "HND",
                    "stops": 1,
                    "dep": "2027-03-22T10:00",
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                },
            ],
        }
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            1,
            json.dumps([deal]),
            "2026-09-11T00:00:00Z",  # ~60h old at test run time
            2,
            "HUF",
        )
        result = fs._ss_itineraries(cfg, [row])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["indicative"])

        expired = (*row[:6], "2026-05-01T00:00:00Z", *row[7:])
        self.assertEqual(fs._ss_itineraries(cfg, [expired]), [])

    def test_deal_cheaper_than_google_is_not_required_anymore(self):
        cfg = config()
        cfg["skyscanner"]["checked_bag_estimate_eur"] = 0
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deal = {
            "eur": 600,
            "agents": ["Agent"],
            "legs": [
                {
                    "from": "BUD",
                    "to": "HND",
                    "stops": 1,
                    "dep": "2027-03-22T10:00",
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                },
            ],
        }
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            1,
            json.dumps([deal]),
            fetched,
            2,
            "HUF",
        )

        # Google RT at 500 — the deal is more expensive but still ranked
        result = fs._ss_itineraries(cfg, [row])

        self.assertEqual(len(result), 1)

    def test_universe_matches_rt_plan(self):
        cfg = config()
        universe = set(fs._ss_universe(cfg))
        rt_plan = {spec[1:] for spec in fs.plan_queries(cfg) if spec[0] == "RT"}
        self.assertEqual(universe, rt_plan)

    def test_exploration_is_oldest_first_and_balanced(self):
        due = [
            ((0, "", c), c)
            for c in [
                ("BUD", "TYO", "2027-03-22", "2027-04-03"),
                ("BUD", "OSA", "2027-03-22", "2027-04-03"),
                ("VIE", "TYO", "2027-03-22", "2027-04-03"),
                ("VIE", "OSA", "2027-03-22", "2027-04-05"),
                ("VIE", "OSA", "2027-03-23", "2027-04-06"),
            ]
        ]
        picked = fs._select_exploration(due, 3)
        self.assertEqual(len(picked), 3)
        # round-robin across (route, duration) cells, oldest within each
        routes = {(c[0], c[1]) for c in picked}
        self.assertLessEqual(len(routes), 3)

    def test_wrong_route_or_date_is_excluded(self):
        cfg = config()
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deal = {
            "eur": 300,
            "agents": ["Agent"],
            "legs": [
                {
                    "from": "VIE",
                    "to": "HND",
                    "stops": 1,
                    "dep": "2027-03-23T10:00",
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                },
            ],
        }
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            1,
            json.dumps([deal]),
            fetched,
            2,
            "HUF",
        )

        self.assertEqual(fs._ss_itineraries(cfg, [row]), [])

    def test_duplicate_deals_do_not_hide_next_unique_deal(self):
        cfg = config()
        fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        first = {
            "eur": 300,
            "agents": ["First"],
            "legs": [
                {
                    "from": "BUD",
                    "to": "HND",
                    "stops": 1,
                    "dep": "2027-03-22T10:00",
                    "arr": "2027-03-23T10:00",
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                    "arr": "2027-04-03T20:00",
                },
            ],
        }
        second = deepcopy(first)
        second["agents"] = ["Second"]
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            3,
            json.dumps([first, first, second]),
            fetched,
            2,
            "HUF",
        )

        result = fs._ss_itineraries(cfg, [row])

        self.assertEqual(len(result), 2)


class SchedulerTests(unittest.TestCase):
    def _cfg(self):
        cfg = config()
        cfg["skyscanner"] = {
            "enabled": True,
            "min_age_hours": 3,
            "hot_combos": 1,
            "neighbour_combos": 0,
            "explore_combos": 3,
            "hot_refresh_hours": 18,
            "explore_refresh_hours": 120,
            "top_deals": 10,
            "eligible_deals": 5,
            "max_age_hours": 168,
            "indicative_after_hours": 24,
            "currency": "HUF",
            "checked_bag_estimate_eur": 120,
            "domain": "skyscanner.hu",
        }
        return cfg

    @staticmethod
    def _raw_deal(price_raw=60000):
        return {
            "price_raw": price_raw,
            "price_fmt": f"{price_raw} Ft",
            "currency": "HUF",
            "agents": ["Agent"],
            "legs": [
                {
                    "from": "BUD",
                    "to": "HND",
                    "stops": 1,
                    "dep": "2027-03-22T10:00",
                    "arr": "2027-03-23T10:00",
                    "dur_min": 1200,
                    "carriers": ["Airline"],
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                    "arr": "2027-04-03T20:00",
                    "dur_min": 1200,
                    "carriers": ["Airline"],
                },
            ],
        }

    @patch("flight_search._travelpayouts_cheap_pairs", return_value={})
    @patch("flight_search.skyscanner_spotcheck")
    def test_tiered_selection_and_persistence(self, spotcheck, _tp):
        cfg = self._cfg()
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        winner = ("BUD", "TYO", "2027-03-22", "2027-04-03")
        conn.execute(
            """INSERT INTO skyscanner_prices
               (key, origin, dest, d1, d2, total_results, deals_json,
                fetched_at, adults, currency)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "v2_2|" + "|".join(winner),
                *winner,
                1,
                json.dumps([{"eur": 300.0, "agents": ["Agent"]}]),
                "2026-09-11T00:00:00Z",  # older than hot_refresh_hours
                2,
                "HUF",
            ),
        )
        spotcheck.return_value = [
            (winner[0], winner[1], winner[2], winner[3], 5, [self._raw_deal()])
        ]
        args = SimpleNamespace(rank_only=False, no_skyscanner=False)

        fs.run_skyscanner_if_due(cfg, conn, args, [])

        combos = {
            tuple(r[0].split("|")[1:])
            for r in conn.execute("SELECT key FROM skyscanner_attempts")
        }
        # hot tier refreshed the stored winner; exploration filled the rest
        self.assertIn(winner, combos)
        self.assertEqual(len(combos), 4)
        self.assertEqual(spotcheck.call_count, 1)
        selected = spotcheck.call_args[0][2]
        self.assertIn(winner, selected)
        self.assertEqual(len(selected), 4)
        # results persisted
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM skyscanner_prices WHERE key LIKE 'v2_2|%'"
            ).fetchone()[0],
            1,
        )
        self.assertIsNotNone(
            conn.execute(
                "SELECT value FROM state WHERE key='skyscanner_last_run_v2_2'"
            ).fetchone()
        )

    @patch("flight_search._travelpayouts_cheap_pairs", return_value={})
    @patch("flight_search.skyscanner_spotcheck")
    def test_recent_run_blocks_rescan(self, spotcheck, _tp):
        cfg = self._cfg()
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO state (key, value) VALUES ('skyscanner_last_run_v2_2', ?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        args = SimpleNamespace(rank_only=False, no_skyscanner=False)

        fs.run_skyscanner_if_due(cfg, conn, args, [])

        spotcheck.assert_not_called()


class TooLongTests(unittest.TestCase):
    def _long_itin(self):
        it = detail("BUD -> HND", 35)
        it["price"] = 1400
        it["legs"] = [{"from": "BUD", "to": "HND", "date": "2027-03-22"}]
        return it

    @patch("flight_search.fetch_with_retry")
    def test_too_long_result_is_recorded_as_confirmed(self, fetch):
        cfg = config()
        cfg["search"]["origins"] = ["BUD"]
        cfg["search"]["destinations"] = ["TYO"]
        fetch.return_value = ([self._long_itin()], None, [], object(), "page")
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        args = SimpleNamespace(workers=1, step=0, rank_only=False, force=False, limit=1)

        progress = fs.run_scan(cfg, conn, args, "2026-09-13T00:00:00Z")

        row = conn.execute(
            "SELECT price, fetched_at, detail FROM price_cache"
        ).fetchone()
        self.assertEqual(progress["fail"], 0)
        self.assertEqual(progress["too_long"], 1)
        self.assertIsNone(row[0])
        self.assertIsNotNone(row[1])
        self.assertIn("too_long", row[2])

        # confirmed rows are only TTL-refreshed, not retried every run:
        # the second run only fetches never-seen OW queries; the RT row stays
        fetch.reset_mock()
        fs.run_scan(cfg, conn, args, "2026-09-13T01:00:00Z")
        rt_fetched, rt_detail = conn.execute(
            "SELECT fetched_at, detail FROM price_cache WHERE key LIKE '%|RT|%'"
        ).fetchone()
        self.assertEqual(rt_fetched, row[1])
        self.assertIn("too_long", rt_detail)


class ParserTests(unittest.TestCase):
    def test_unrecognized_rpc_json_is_not_confirmed_empty(self):
        body = '[["wrb.fr","rpc", "{\\"error\\":true}"]]'
        itins, recognized = fs._parse_rpc_itins(body, with_envelope=True)
        self.assertEqual(itins, [])
        self.assertFalse(recognized)


class RenderTests(unittest.TestCase):
    def test_table_filters_and_per_bucket_candidates_are_rendered(self):
        cfg = config()
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        rows = {
            ("RT", "BUD", "TYO", "2027-03-22", "2027-04-03"): detail("BUD -> HND"),
            ("RT", "VIE", "TYO", "2027-03-22", "2027-04-03"): detail("VIE -> HND"),
        }
        itins = fs.build_itineraries(cfg, rows)
        fs.label_itins(itins)
        args = SimpleNamespace(top=1)
        progress = {"n": 0, "fail": 0, "empty": 0, "deferred": 0}

        rendered = fs.render_html(
            cfg,
            itins,
            {},
            None,
            "2026-09-13T00:00:00Z",
            conn,
            args,
            progress,
        )

        self.assertIn('id="filter-origin"', rendered)
        self.assertIn('value="BUD">Budapest (BUD)', rendered)
        self.assertIn('value="VIE">Vienna (VIE)', rendered)
        self.assertIn('id="filter-days-min"', rendered)
        self.assertIn('data-origin="BUD" data-days="12"', rendered)
        self.assertIn('data-origin="VIE" data-days="12"', rendered)
        self.assertIn("const TABLE_LIMIT = 1;", rendered)

    def test_ss_rows_render_source_kind_indicative_and_fare_breakdown(self):
        cfg = config()
        cfg["skyscanner"] = {
            "enabled": True,
            "max_age_hours": 168,
            "indicative_after_hours": 24,
            "eligible_deals": 5,
            "checked_bag_estimate_eur": 30,
            "currency": "HUF",
        }
        fetched = "2026-09-11T00:00:00Z"  # ~60h old -> indicative
        deal = {
            "eur": 300,
            "agents": ["Agent"],
            "self_transfer": True,
            "legs": [
                {
                    "from": "BUD",
                    "to": "HND",
                    "stops": 1,
                    "dep": "2027-03-22T10:00",
                    "arr": "2027-03-23T10:00",
                    "dur_min": 1200,
                },
                {
                    "from": "HND",
                    "to": "BUD",
                    "stops": 1,
                    "dep": "2027-04-03T10:00",
                    "arr": "2027-04-03T20:00",
                    "dur_min": 1200,
                },
            ],
        }
        row = (
            "BUD",
            "TYO",
            "2027-03-22",
            "2027-04-03",
            1,
            json.dumps([deal]),
            fetched,
            2,
            "HUF",
        )
        itins = fs.build_itineraries(cfg, {}, ss_rows=[row])
        fs.label_itins(itins)
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        args = SimpleNamespace(top=40)
        progress = {"n": 0, "fail": 0, "empty": 0, "deferred": 0}

        rendered = fs.render_html(
            cfg, itins, {}, None, "2026-09-13T00:00:00Z", conn, args, progress
        )

        self.assertIn('data-kind="SS"', rendered)
        self.assertRegex(rendered, r"indicative</b>, checked \d+h ago")
        self.assertIn('value="ota">OTA (Skyscanner)', rendered)
        self.assertIn("Cheapest OTA (Skyscanner)", rendered)
        # airfare cell keeps the bag-normalized total plus the raw breakdown
        self.assertIn(
            '<span data-eur="300">300</span> fare + <span data-eur="30">30</span> bags',
            rendered,
        )
        self.assertEqual(
            [it["kind"] for it in itins],
            ["SS"],
        )


class DatabaseTests(unittest.TestCase):
    def test_history_key_is_unique_per_run(self):
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        row = ("run", "key", "RT", "label", 1, 2, 3)
        conn.execute("INSERT INTO itinerary_history VALUES (?,?,?,?,?,?,?)", row)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO itinerary_history VALUES (?,?,?,?,?,?,?)", row)

    def test_malformed_stored_skyscanner_data_falls_back_to_google(self):
        cfg = config()
        conn = fs.init_db(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """INSERT INTO skyscanner_prices
               (key, origin, dest, d1, d2, total_results, deals_json,
                fetched_at, adults, currency)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "v2_2|BUD|TYO|2027-03-22|2027-04-03",
                "BUD",
                "TYO",
                "2027-03-22",
                "2027-04-03",
                1,
                "not-json",
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                2,
                "HUF",
            ),
        )
        rows = {("RT", "BUD", "TYO", "2027-03-22", "2027-04-03"): detail("BUD -> HND")}

        with self.assertLogs("flight_search", level="ERROR"):
            result = fs.build_with_optional_skyscanner(cfg, conn, rows)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "RT")


if __name__ == "__main__":
    unittest.main()
