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
        self._orig_globals = {
            "SEEN_FILE": push_deals.SEEN_FILE,
            "QUOTA_FILE": push_deals.QUOTA_FILE,
            "QUEUE_FILE": push_deals.QUEUE_FILE,
            "TRACKER_FILE": push_deals.TRACKER_FILE,
            "OCR_CACHE_FILE": push_deals.OCR_CACHE_FILE,
            "PRICE_DB_FILE": push_deals.PRICE_DB_FILE,
            "LOCK_FILE_PATH": push_deals.LOCK_FILE_PATH,
            "BOT_TOKEN": push_deals.BOT_TOKEN,
        }
        push_deals.SEEN_FILE = os.path.join(tmp, "seen_deals.json")
        push_deals.QUOTA_FILE = os.path.join(tmp, "post_quota.json")
        push_deals.QUEUE_FILE = os.path.join(tmp, "flyer_queue.json")
        push_deals.TRACKER_FILE = os.path.join(tmp, "sales_tracker.json")
        push_deals.OCR_CACHE_FILE = os.path.join(tmp, "ocr_cache.json")
        push_deals.PRICE_DB_FILE = os.path.join(tmp, "price_history.db")
        push_deals.LOCK_FILE_PATH = os.path.join(tmp, "deal_pusher.lock")
        push_deals._PRICE_DB_CACHE.clear()
        push_deals.BOT_TOKEN = "test-token"
        self._net_patches = [
            patch.object(push_deals, "send_telegram_photo_bytes", return_value=True),
            patch.object(push_deals, "send_telegram", return_value=True),
            patch.object(push_deals.coupang_api, "search_products", return_value={"data": []}),
        ]
        for p in self._net_patches:
            p.start()
        self.db = push_deals.get_price_db()

    def tearDown(self) -> None:
        for p in reversed(self._net_patches):
            p.stop()
        push_deals._PRICE_DB_CACHE.clear()
        for k, v in self._orig_globals.items():
            setattr(push_deals, k, v)
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

    def test_p0_shipping_parens_plus_newline_and_extra_free(self):
        # (3,000원 배송비 별도) / 배송비 +3,000원 -> 판매가 17,900원 + 배송비 3,000원 보존
        for did, pid, title in (
            ("arca_ship_paren_byul", "40001", "생수 500ml 40병 17,900원 (3,000원 배송비 별도)"),
            ("arca_ship_plus", "40002", "생수 500ml 40병 17,900원 배송비 +3,000원"),
        ):
            with self.subTest(did=did):
                ok, _s, q = self._run_process_deal(
                    deal_id=did,
                    title=title,
                    body_text="40병 기준 17,900원",
                    product_id=pid,
                    extra_shipping="",
                )
                self.assertTrue(ok)
                self.assertEqual(q[0]["sale_price"], 17900)
                self.assertEqual(q[0]["shipping_fee"], 3000)
                self.assertEqual(q[0]["unit_info"], "1L당 1,045원 (배송포함)")
                obs = self._fetch_observations(pid)
                self.assertEqual(len(obs), 1)
                self.assertEqual(obs[0]["total_price"], 20900)

        p_byul = push_deals.parse_pricing_details("생수 500ml 40병 17,900원 배송비 별도", "")
        self.assertEqual(p_byul["sale_price"], 17900)
        self.assertEqual(p_byul["shipping_fee"], 0)
        self.assertEqual(p_byul["shipping_type"], "paid")

        p_nl = push_deals.parse_pricing_details("생수 500ml 40병 17,900원", "배송비\n3,000원")
        self.assertEqual(p_nl["sale_price"], 17900)
        self.assertEqual(p_nl["shipping_fee"], 3000)

        s_free = push_deals.parse_shipping_details("생수 500ml 40병 17,900원", "", extra_shipping="무료")
        self.assertEqual(s_free["shipping_type"], "free")

    def test_p0_limited_stock_and_1in_single_serving_preserved(self):
        ok1, _s1, q1 = self._run_process_deal(
            deal_id="arca_stock_limit",
            title="생수 500ml 20병 수량 한정 12,900원",
            body_text="무료배송",
            product_id="40003",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok1)
        self.assertEqual(q1[0]["option_key"], "pid:40003:cfg:vol:500ml:n20")
        self.assertEqual(q1[0]["unit_info"], "1L당 1,290원")

        ok2, _s2, q2 = self._run_process_deal(
            deal_id="arca_1in_serving",
            title="육개장 1인 6봉 12,900원",
            body_text="무료배송",
            product_id="40004",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok2)
        self.assertEqual(q2[0]["option_key"], "pid:40004:cfg:cnt:봉:n6")
        self.assertEqual(q2[0]["unit_info"], "1봉당 2,150원")

        for did, pid, title in (
            ("arca_lim_v1", "40005", "생수 500ml 20병 (1회 최대 2박스) 12,900원"),
            ("arca_lim_v2", "40006", "생수 500ml 20병 (1인: 2박스 제한) 12,900원"),
        ):
            with self.subTest(did=did):
                ok_v, _sv, q_v = self._run_process_deal(did, title, "무료배송", pid, "무료배송")
                self.assertTrue(ok_v)
                self.assertEqual(q_v[0]["option_key"], f"pid:{pid}:cfg:vol:500ml:n20")

    def test_p0_lookbehind_title_multipair_and_false_positive_guards(self):
        self.assertEqual(price_history.compute_effective_unit_metrics("사과 1kg,2kg 15,000원", 15000), {})
        ok_ms, _sms, q_ms = self._run_process_deal(
            deal_id="arca_multispec_nospace",
            title="사과 1kg,2kg 15,000원",
            body_text="무료배송",
            product_id="40007",
            extra_shipping="무료배송",
        )
        self.assertFalse(ok_ms)
        self.assertEqual(len(q_ms), 0)

        for did, pid, title, body in (
            ("arca_nospace_pairs", "40008", "생수 500ml 20병 9,900원", "20병 9,900원,40병 17,900원 무료배송"),
            ("arca_title_pairs", "40009", "생수 500ml 20병 9,900원, 40병 17,900원", "무료배송"),
        ):
            with self.subTest(did=did):
                ok_w, seen_w, q_w = self._run_process_deal(did, title, body, pid, "무료배송")
                self.assertFalse(ok_w)
                self.assertEqual(len(q_w), 0)
                self.assertNotIn(did, seen_w)
                self.assertEqual(len(self._fetch_observations(pid)), 0)
                self.assertIsNotNone(self.db.get_pending_wait(f"arca:{did}"))

        # 정상 딜 오탐 방지 ('1L에 1,045원', 할인쿠폰, Track A 할인 화살표)
        ok_e, _se, qe = self._run_process_deal(
            deal_id="arca_fp_per_e",
            title="생수 500ml 40병 17,900원",
            body_text="40병 17,900원, 1L에 1,045원 꼴 무료배송",
            product_id="40010",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok_e)
        self.assertEqual(qe[0]["option_key"], "pid:40010:cfg:vol:500ml:n40")

        ok_cp, _sc, qcp = self._run_process_deal(
            deal_id="arca_fp_coupon",
            title="생수 500ml 40병 17,900원",
            body_text="40병 2,000원 할인쿠폰 적용 무료배송",
            product_id="40011",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok_cp)
        self.assertEqual(qcp[0]["option_key"], "pid:40011:cfg:vol:500ml:n40")

        ok_ta, _sta, qta = self._run_process_deal(
            deal_id="arca_fp_track_a_arrow",
            title="생수 500ml 40병",
            body_text="40병 25,000원 -> 17,900원 무료배송 (28% 할인)",
            product_id="40012",
            extra_shipping="무료배송",
            votes=3,
            comments=3,
        )
        self.assertTrue(ok_ta)
        self.assertEqual(qta[0]["track"], "A")
        self.assertEqual(qta[0]["option_key"], "pid:40012:cfg:vol:500ml:n40")
        self.assertEqual(qta[0]["unit_info"], "1L당 895원")

    def test_p1_comma_counts_it_excl_wow_free_food_kw_and_dealbada_thumb(self):
        import flyer_renderer
        from unittest.mock import MagicMock

        ok_m, _sm, qm = self._run_process_deal(
            deal_id="arca_comma_1200",
            title="믹스커피 1,200포 12,000원",
            body_text="무료배송",
            product_id="40013",
            extra_shipping="무료배송",
        )
        self.assertTrue(ok_m)
        self.assertEqual(qm[0]["option_key"], "pid:40013:cfg:cnt:포:n1200")
        self.assertEqual(qm[0]["unit_info"], "1포당 10원")
        self.assertEqual(qm[0]["chip"], "1포당 10원 · 1,200포")
        self.assertEqual(flyer_renderer.extract_unit_part("물티슈 1,000매", "1매당 10원"), "1매당 10원 · 1,000매")

        self.assertEqual(
            price_history.compute_effective_unit_metrics("LG전자 2026 그램 AI 15 16G 1,515,700원", 1515700),
            {},
        )
        self.assertEqual(price_history.classify_deal_category("오리온 미쯔 황치즈 6p, 222g, 2개"), "food")

        # 호밍스 사골진곰탕 + 댓글 4개 무비추 -> Track B2 통과
        ok_h, _sh, qh = self._run_process_deal(
            deal_id="arca_homings_comments",
            title="호밍스 사골진곰탕, 500g, 5개 9,900원",
            body_text="와우 무료배송",
            product_id="40014",
            extra_shipping="와우 무료배송",
            votes=0,
            comments=4,
        )
        self.assertTrue(ok_h)
        self.assertEqual(qh[0]["option_key"], "pid:40014:cfg:wt:500g:n5")
        obs_h = self._fetch_observations("40014")
        self.assertEqual(len(obs_h), 1)
        self.assertEqual(obs_h[0]["shipping_type"], "wow_free")
        # free 기준과 wow_free 기준 격리 확인
        self.assertIsNone(self.db.get_observed_baseline("pid:40014:cfg:wt:500g:n5", shipping_type="free", min_days=1))

        # 딜바다 빈 썸네일 <a>가 앞서는 HTML에서도 정상 수집 확인
        html_thumb = """
        <html><body><table><tbody>
        <tr>
          <td class="td_thumb"><a href="//www.dealbada.com/bbs/board.php?bo_table=deal_domestic&wr_id=573001&page=1"></a></td>
          <td class="td_subject"><a href="//www.dealbada.com/bbs/board.php?bo_table=deal_domestic&wr_id=573001&page=1">[쿠팡] 케어웰 당뇨식 200ml 24개 (23,200원/무료)</a><span class="cnt_cmt">3</span></td>
          <td class="td_num">2</td>
        </tr>
        </tbody></table></body></html>
        """
        r_ok = MagicMock(status_code=200, text=html_thumb)
        r_empty = MagicMock(status_code=200, text="<html><body><table><tbody></tbody></table></body></html>")
        with patch.object(push_deals.requests, "get", side_effect=[r_ok, r_empty]):
            db_rows = push_deals.collect_dealbada_deals()
        self.assertEqual(len(db_rows), 1)
        self.assertEqual(db_rows[0]["id"], "dealbada_573001")


if __name__ == "__main__":
    unittest.main()
