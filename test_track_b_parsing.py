"""실제 push_deals.process_deal을 거치는 Track B 파싱·구성키·대기열·관측 DB 회귀 테스트."""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["DEAL_PUSHER_LOG_FILE"] = ""

import price_history
import push_deals


class TrackBParsingRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        push_deals.SEEN_FILE = os.path.join(tmp, "seen_deals.json")
        push_deals.QUOTA_FILE = os.path.join(tmp, "post_quota.json")
        push_deals.QUEUE_FILE = os.path.join(tmp, "flyer_queue.json")
        push_deals.TRACKER_FILE = os.path.join(tmp, "sales_tracker.json")
        push_deals.OCR_CACHE_FILE = os.path.join(tmp, "ocr_cache.json")
        push_deals.PRICE_DB_FILE = os.path.join(tmp, "price_history.db")
        push_deals.LOCK_FILE_PATH = os.path.join(tmp, "deal_pusher.lock")
        push_deals._PRICE_DB_CACHE.clear()
        push_deals.BOT_TOKEN = "test-token"
        self.db = push_deals.get_price_db()

    def tearDown(self) -> None:
        push_deals._PRICE_DB_CACHE.clear()
        self._tmp.cleanup()

    def _run_process_deal(
        self,
        deal_id: str,
        title: str,
        body_text: str,
        product_id: str,
        extra_shipping: str = "무료배송",
        votes: int = 5,
        comments: int = 2,
    ):
        seen = set()
        flyer_queue = []
        ocr_cache = {}

        def fake_detail(_url: str):
            return {
                "coupang_url": f"https://www.coupang.com/vp/products/{product_id}",
                "body_text": body_text,
                "img_url": None,
                "ocr_img_urls": [],
                "extracted_title": title,
                "extracted_shipping": extra_shipping,
            }

        deal = {
            "id": deal_id,
            "title": title,
            "link": f"https://arca.live/b/hotdeal/{product_id}",
            "votes": votes,
            "downvotes": 0,
            "comments": comments,
            "hits": 0,
        }
        with patch.object(
            push_deals.coupang_api,
            "create_deeplink",
            return_value={"data": [{"shortenUrl": f"https://link.coupang.com/a/{product_id}"}]},
        ):
            ok = push_deals.process_deal(
                deal,
                fake_detail,
                "arca",
                seen,
                flyer_queue=flyer_queue,
                ocr_cache=ocr_cache,
            )
        return ok, seen, flyer_queue

    def _fetch_observations(self, product_id: str):
        with self.db._connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM price_observations WHERE product_id = ?",
                    (product_id,),
                ).fetchall()
            ]

    def test_case1_body_option_price_pairs_conflict_holds_wait(self):
        ok, seen, queue = self._run_process_deal(
            deal_id="arca_case1",
            title="생수 500ml 40병 9,900원",
            body_text="20병 9,900원 / 40병 17,900원\n무료배송",
            product_id="30001",
            extra_shipping="무료배송",
        )
        self.assertFalse(ok)
        self.assertEqual(len(queue), 0)
        self.assertNotIn("arca_case1", seen)
        self.assertEqual(len(self._fetch_observations("30001")), 0)

        wait_row = self.db.get_pending_wait("arca:arca_case1")
        self.assertIsNotNone(wait_row)
        self.assertEqual(wait_row["reason"], "wait")

    def test_case2_purchase_limit_excluded_while_real_bundle_preserved(self):
        ok, seen, queue = self._run_process_deal(
            deal_id="arca_case2_limit",
            title="생수 500ml 20병 (1인 2박스 구매 제한) 12,900원",
            body_text="무료배송",
            product_id="30002",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok)
        self.assertIn("arca_case2_limit", seen)
        self.assertEqual(len(queue), 1)
        item = queue[0]
        self.assertEqual(item["option_key"], "pid:30002:cfg:vol:500ml:n20")
        self.assertEqual(item["unit_info"], "1L당 1,290원")
        self.assertIn("1L당 1,290원", item["evidence_note"])
        self.assertIn("20병", item["chip"])
        self.assertNotIn("2박스", item["chip"])

        obs = self._fetch_observations("30002")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["option_key"], "pid:30002:cfg:vol:500ml:n20")
        self.assertEqual(obs[0]["shipping_type"], "free")
        self.assertEqual(obs[0]["total_price"], 12900)
        self.assertEqual(obs[0]["metric_type"], "per_100ml")
        self.assertAlmostEqual(obs[0]["metric_value"], 129.0, places=2)

        # 실제 묶음 구성 '500ml 20병 2박스' -> 40병(1L당 645원) 유지 검증
        ok_b, _seen_b, queue_b = self._run_process_deal(
            deal_id="arca_case2_bundle",
            title="생수 500ml 20병 2박스 12,900원",
            body_text="무료배송",
            product_id="30020",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok_b)
        self.assertEqual(len(queue_b), 1)
        self.assertEqual(queue_b[0]["option_key"], "pid:30020:cfg:vol:500ml:n40")
        self.assertEqual(queue_b[0]["unit_info"], "1L당 645원")
        obs_b = self._fetch_observations("30020")
        self.assertEqual(len(obs_b), 1)
        self.assertEqual(obs_b[0]["option_key"], "pid:30020:cfg:vol:500ml:n40")
        self.assertAlmostEqual(obs_b[0]["metric_value"], 64.5, places=2)

    def test_case3_thousands_comma_volume_parsed_fully(self):
        ok, _seen, queue = self._run_process_deal(
            deal_id="arca_case3_comma",
            title="생수 1,500ml 12병 18,000원",
            body_text="무료배송",
            product_id="30003",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok)
        self.assertEqual(len(queue), 1)
        item = queue[0]
        self.assertEqual(item["option_key"], "pid:30003:cfg:vol:1500ml:n12")
        self.assertEqual(item["unit_info"], "1L당 1,000원")
        self.assertIn("1L당 1,000원", item["evidence_note"])

        obs = self._fetch_observations("30003")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["option_key"], "pid:30003:cfg:vol:1500ml:n12")
        self.assertEqual(obs[0]["total_price"], 18000)
        self.assertEqual(obs[0]["metric_type"], "per_100ml")
        self.assertAlmostEqual(obs[0]["metric_value"], 100.0, places=2)

    def test_existing_duplicate_parenthetical_40_case_preserved(self):
        ok, _seen, queue = self._run_process_deal(
            deal_id="arca_case4_dup",
            title="생수 500ml 40개 (40병) 9,900원",
            body_text="배송비 3,000원",
            product_id="30004",
            extra_shipping="배송비 3,000원",
        )
        self.assertTrue(ok)
        self.assertEqual(len(queue), 1)
        item = queue[0]
        self.assertEqual(item["option_key"], "pid:30004:cfg:vol:500ml:n40")
        self.assertEqual(item["shipping_fee"], 3000)
        self.assertEqual(item["unit_info"], "1L당 645원 (배송포함)")

        obs = self._fetch_observations("30004")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["option_key"], "pid:30004:cfg:vol:500ml:n40")
        self.assertEqual(obs[0]["shipping_type"], "paid")
        self.assertEqual(obs[0]["total_price"], 12900)
        self.assertAlmostEqual(obs[0]["metric_value"], 64.5, places=2)

    def test_single_body_restatement_passes_and_single_conflict_waits(self):
        ok_match, _s1, q_match = self._run_process_deal(
            deal_id="arca_match",
            title="생수 500ml 40병 17,900원",
            body_text="40병 17,900원 무료배송 (1병당 448원)",
            product_id="30005",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok_match)
        self.assertEqual(len(q_match), 1)
        self.assertEqual(q_match[0]["option_key"], "pid:30005:cfg:vol:500ml:n40")

        ok_conf, _s2, q_conf = self._run_process_deal(
            deal_id="arca_single_conflict",
            title="생수 500ml 40병 9,900원",
            body_text="40병 17,900원 무료배송",
            product_id="30006",
            extra_shipping="무료배송",
        )
        self.assertFalse(ok_conf)
        self.assertEqual(len(q_conf), 0)
        self.assertEqual(len(self._fetch_observations("30006")), 0)
        self.assertIsNotNone(self.db.get_pending_wait("arca:arca_single_conflict"))

    def test_case_body_option_price_with_particles_and_gijun_holds_wait(self):
        for deal_id, pid, body_text in (
            (
                "arca_particle_pairs",
                "30007",
                "20병은 9,900원, 40병은 17,900원입니다.\n무료배송",
            ),
            (
                "arca_gijun_pairs",
                "30008",
                "20병 기준 9,900원 / 40병 기준 17,900원\n무료배송",
            ),
        ):
            with self.subTest(deal_id=deal_id):
                ok, seen, queue = self._run_process_deal(
                    deal_id=deal_id,
                    title="생수 500ml 40병 9,900원",
                    body_text=body_text,
                    product_id=pid,
                    extra_shipping="무료배송",
                )
                self.assertFalse(ok)
                self.assertEqual(len(queue), 0)
                self.assertNotIn(deal_id, seen)
                self.assertEqual(len(self._fetch_observations(pid)), 0)
                wait_row = self.db.get_pending_wait(f"arca:{deal_id}")
                self.assertIsNotNone(wait_row)
                self.assertEqual(wait_row["reason"], "wait")

        # 단일 구성 + 단가·배송비 병기 정상 문구 오탐 없음 확인
        ok_clean, _s, q_clean = self._run_process_deal(
            deal_id="arca_gijun_single_ok",
            title="생수 500ml 40병 17,900원",
            body_text="40병 기준 17,900원 (1L당 1,045원, 배송비 3,000원)",
            product_id="30009",
            extra_shipping="배송비 3,000원",
        )
        self.assertTrue(ok_clean)
        self.assertEqual(len(q_clean), 1)
        self.assertEqual(q_clean[0]["option_key"], "pid:30009:cfg:vol:500ml:n40")
        self.assertEqual(q_clean[0]["unit_info"], "1L당 1,045원 (배송포함)")

    def test_case_paren_real_qty_preserved_alongside_purchase_limit(self):
        ok, seen, queue = self._run_process_deal(
            deal_id="arca_paren_qty_limit",
            title="생수 500ml (20병, 1인 2박스 구매 제한) 12,900원",
            body_text="무료배송",
            product_id="30010",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok)
        self.assertIn("arca_paren_qty_limit", seen)
        self.assertEqual(len(queue), 1)
        item = queue[0]
        self.assertEqual(item["option_key"], "pid:30010:cfg:vol:500ml:n20")
        self.assertEqual(item["unit_info"], "1L당 1,290원")
        self.assertIn("1L당 1,290원", item["evidence_note"])
        self.assertIn("20병", item["chip"])
        self.assertNotIn("2박스", item["chip"])

        obs = self._fetch_observations("30010")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["option_key"], "pid:30010:cfg:vol:500ml:n20")
        self.assertEqual(obs[0]["shipping_type"], "free")
        self.assertEqual(obs[0]["total_price"], 12900)
        self.assertEqual(obs[0]["metric_type"], "per_100ml")
        self.assertAlmostEqual(obs[0]["metric_value"], 129.0, places=2)

    def test_adjacent_shipping_preserves_option_price_and_conflicts_wait(self):
        # 쉼표 유무 및 정순/역순 배송비 표기에 무관하게 옵션 가격 17,900원 보존 확인
        for snippet in (
            "40병 기준 17,900원 배송비 3,000원",
            "40병 기준 17,900원, 배송비 3,000원",
            "40병 기준 17,900원 (3,000원 배송비)",
            "40병 기준 17,900원, (3,000원 배송비)",
        ):
            with self.subTest(snippet=snippet):
                pairs = price_history._extract_option_price_pairs(snippet)
                self.assertEqual(pairs, [("cnt:병:40", "cnt_t1", 40, 17900)])

        for deal_id, pid, title, body_text in (
            (
                "arca_adj_conflict_1",
                "30011",
                "생수 500ml 40병 9,900원",
                "40병 기준 17,900원 배송비 3,000원",
            ),
            (
                "arca_adj_conflict_2",
                "30012",
                "생수 500ml 40병 3,000원",
                "40병 기준 17,900원 배송비 3,000원",
            ),
            (
                "arca_adj_conflict_3",
                "30013",
                "생수 500ml 40병 9,900원",
                "20병 기준 9,900원 / 40병 기준 17,900원 (3,000원 배송비)",
            ),
        ):
            with self.subTest(deal_id=deal_id):
                ok, seen, queue = self._run_process_deal(
                    deal_id=deal_id,
                    title=title,
                    body_text=body_text,
                    product_id=pid,
                    extra_shipping="",
                )
                self.assertFalse(ok)
                self.assertEqual(len(queue), 0)
                self.assertNotIn(deal_id, seen)
                self.assertEqual(len(self._fetch_observations(pid)), 0)
                wait_row = self.db.get_pending_wait(f"arca:{deal_id}")
                self.assertIsNotNone(wait_row)
                self.assertEqual(wait_row["reason"], "wait")

    def test_adjacent_shipping_matching_single_option_passes_comma_independent(self):
        variants = (
            ("arca_adj_ok_1", "30014", "생수 500ml 40병 17,900원", "40병 기준 17,900원 배송비 3,000원"),
            ("arca_adj_ok_2", "30015", "생수 500ml 40병 17,900원", "40병 기준 17,900원, 배송비 3,000원"),
            ("arca_adj_ok_3", "30016", "생수 500ml 40병 17,900원", "40병 기준 17,900원 (3,000원 배송비)"),
            ("arca_adj_ok_4", "30017", "생수 500ml 40병 17,900원", "40병 기준 17,900원, (3,000원 배송비)"),
            ("arca_adj_ok_5", "30018", "생수 500ml 40병 17,900원 배송비 3,000원", "40병 기준 17,900원 배송비 3,000원"),
        )
        for deal_id, pid, title, body_text in variants:
            with self.subTest(deal_id=deal_id):
                ok, seen, queue = self._run_process_deal(
                    deal_id=deal_id,
                    title=title,
                    body_text=body_text,
                    product_id=pid,
                    extra_shipping="",
                )
                self.assertTrue(ok)
                self.assertIn(deal_id, seen)
                self.assertEqual(len(queue), 1)
                item = queue[0]
                self.assertEqual(item["sale_price"], 17900)
                self.assertEqual(item["shipping_fee"], 3000)
                self.assertEqual(item["unit_info"], "1L당 1,045원 (배송포함)")
                self.assertIn("1L당 1,045원", item["evidence_note"])
                self.assertEqual(item["option_key"], f"pid:{pid}:cfg:vol:500ml:n40")
                self.assertEqual(item["clean_title"], "생수 500ml 40병")

                obs = self._fetch_observations(pid)
                self.assertEqual(len(obs), 1)
                self.assertEqual(obs[0]["option_key"], f"pid:{pid}:cfg:vol:500ml:n40")
                self.assertEqual(obs[0]["total_price"], 20900)
                self.assertEqual(obs[0]["sale_price"], 17900)
                self.assertEqual(obs[0]["shipping_fee"], 3000)
                self.assertEqual(obs[0]["shipping_type"], "paid")
                self.assertAlmostEqual(obs[0]["metric_value"], 104.5, places=2)

    def test_self_check_compact_multiplier_and_1in1hoe_limit(self):
        ok1, _s1, q1 = self._run_process_deal(
            deal_id="arca_sc_compact_x",
            title="생수 500ml 20병x2 12,900원",
            body_text="무료배송",
            product_id="30021",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok1)
        self.assertEqual(q1[0]["option_key"], "pid:30021:cfg:vol:500ml:n40")
        self.assertEqual(q1[0]["unit_info"], "1L당 645원")

        ok2, _s2, q2 = self._run_process_deal(
            deal_id="arca_sc_1in1hoe",
            title="생수 500ml 20병 (1인 1회 2박스 제한) 12,900원",
            body_text="무료배송",
            product_id="30022",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok2)
        self.assertEqual(q2[0]["option_key"], "pid:30022:cfg:vol:500ml:n20")
        self.assertEqual(q2[0]["unit_info"], "1L당 1,290원")
        self.assertIn("20병", q2[0]["chip"])
        self.assertNotIn("2박스", q2[0]["chip"])

    def test_self_check_arrow_equals_weight_and_bundle_conflicts(self):
        for deal_id, pid, title, body_text in (
            ("arca_sc_arrow", "30023", "생수 500ml 40병 9,900원", "20병 -> 9,900원 / 40병 -> 17,900원"),
            ("arca_sc_eq", "30024", "생수 500ml 40병 9,900원", "20병 = 9,900원 / 40병 = 17,900원"),
            ("arca_sc_wt_only", "30025", "삼겹살 1kg", "500g 8,900원 무료배송"),
            ("arca_sc_bundle_conf", "30026", "생수 500ml 20병 2박스 12,900원", "40병 기준 17,900원 무료배송"),
        ):
            with self.subTest(deal_id=deal_id):
                ok, seen, queue = self._run_process_deal(
                    deal_id=deal_id,
                    title=title,
                    body_text=body_text,
                    product_id=pid,
                    extra_shipping="무료배송",
                )
                self.assertFalse(ok)
                self.assertEqual(len(queue), 0)
                self.assertNotIn(deal_id, seen)
                self.assertEqual(len(self._fetch_observations(pid)), 0)
                self.assertIsNotNone(self.db.get_pending_wait(f"arca:{deal_id}"))

        ok_match, _sm, qm = self._run_process_deal(
            deal_id="arca_sc_bundle_match",
            title="생수 500ml 20병 2박스 12,900원",
            body_text="40병 기준 12,900원 무료배송",
            product_id="30027",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok_match)
        self.assertEqual(qm[0]["option_key"], "pid:30027:cfg:vol:500ml:n40")
        self.assertEqual(qm[0]["unit_info"], "1L당 645원")


if __name__ == "__main__":
    unittest.main()
