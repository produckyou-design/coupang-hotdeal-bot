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
            patch.object(push_deals, "register_tracked_link", side_effect=lambda url, **_kw: url),
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
        enable_track_b2: bool = True,
        source_name: str = "arca",
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
        with (
            patch.object(push_deals, "ENABLE_TRACK_B2_NO_ORIG_FOOD", enable_track_b2),
            patch.object(
                push_deals.coupang_api,
                "create_deeplink",
                return_value={"data": [{"shortenUrl": f"https://link.coupang.com/a/{product_id}"}]},
            ),
        ):
            ok = push_deals.process_deal(
                deal,
                fake_detail,
                source_name,
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

        # 호밍스 사골진곰탕: 댓글 4개만(votes=0)으로는 강화된 Track B2 보류 확인
        ok_h_wait, seen_hw, q_hw = self._run_process_deal(
            deal_id="arca_homings_comments_wait",
            title="호밍스 사골진곰탕, 500g, 5개 9,900원",
            body_text="와우 무료배송",
            product_id="40014",
            extra_shipping="와우 무료배송",
            votes=0,
            comments=4,
        )
        self.assertFalse(ok_h_wait)
        self.assertEqual(len(q_hw), 0)
        self.assertNotIn("arca_homings_comments_wait", seen_hw)
        self.assertIsNotNone(self.db.get_pending_wait("arca:arca_homings_comments_wait"))

        # 호밍스 사골진곰탕 + 추천 5개 · 댓글 4개 -> Track B2 통과
        ok_h, _sh, qh = self._run_process_deal(
            deal_id="arca_homings_comments",
            title="호밍스 사골진곰탕, 500g, 5개 9,900원",
            body_text="와우 무료배송",
            product_id="40014",
            extra_shipping="와우 무료배송",
            votes=5,
            comments=4,
        )
        self.assertTrue(ok_h)
        self.assertEqual(qh[0]["option_key"], "pid:40014:cfg:wt:500g:n5")
        self.assertEqual(qh[0]["votes"], 5)
        self.assertEqual(qh[0]["comments"], 4)
        self.assertEqual(qh[0]["evidence_badge"], "아카라이브 인기")
        self.assertIn("아카라이브 추천 5 · 댓글 4", qh[0]["evidence_note"])
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

    def test_track_b2_strict_gate_source_and_caption_reaction(self):
        import flyer_renderer
        from PIL import Image
        from unittest.mock import MagicMock

        # 1) Track B2 인기 게이트 경계값 검증
        for src, v, c, dw in (
            ("arca", 2, 8, 0),      # 쫀떡궁합급 차단
            ("arca", 2, 3, 0),      # 리터스포트급 차단
            ("arca", 0, 4, 0),
            ("arca", 4, 9, 0),      # 추천 4 + 댓글 9 차단
            ("arca", 5, 2, 2),      # 비추 2 차단 (추천 8 미만)
            ("dealbada", 3, 3, 0),  # 딜바다 추천 3 차단
        ):
            with self.subTest(reject=(src, v, c, dw)):
                self.assertFalse(
                    push_deals._has_track_b_food_reaction(
                        {"votes": v, "comments": c, "downvotes": dw},
                        src,
                        "초콜릿 100g 2개",
                        "무료배송",
                    )
                )

        for src, v, c, dw in (
            ("arca", 5, 0, 0),
            ("arca", 3, 10, 0),
            ("arca", 8, 5, 2),
            ("dealbada", 4, 3, 0),
        ):
            with self.subTest(allow=(src, v, c, dw)):
                self.assertTrue(
                    push_deals._has_track_b_food_reaction(
                        {"votes": v, "comments": c, "downvotes": dw},
                        src,
                        "초콜릿 100g 2개",
                        "무료배송",
                    )
                )

        # 2) 한줄 반응 추출 및 비속어·부정·안내문구 필터 검증
        quote = push_deals.extract_best_reaction_quote(
            [
                "등록된 댓글이 없습니다.",
                "ㅁㅊ 싸긴하네 36개에 만원이면",
                "아 로켓아니네ㅜㅜ",
                "비싸네 거름",
                "이거 달달고소해서 맛있더라 가격 괜찮네",
            ],
            fallback_reaction="",
        )
        self.assertEqual(quote, "이거 달달고소해서 맛있더라 가격 괜찮네")

        fb_quote = push_deals.extract_best_reaction_quote(
            ["맛있어요", "링크 확인"],
            fallback_reaction="역대 최저가 단가 괜찮네요",
        )
        self.assertEqual(fb_quote, "역대 최저가 단가 괜찮네요")

        # 3) 상세 파서 comment_texts 추출 확인 (모킹 HTML, 네트워크 없음)
        arca_html = """
        <html><body>
          <table class="article-info">
            <tr><th>링크</th><td><a href="https://www.coupang.com/vp/products/50001">쿠팡</a></td></tr>
            <tr><th>상품명</th><td>배스킨라빈스 쫀떡궁합 파이 264g 3개</td></tr>
            <tr><th>가격</th><td>9,520원</td></tr>
          </table>
          <div class="article-body">총 36개 무료배송</div>
          <div class="comment-item"><div class="message"><div class="text">ㅁㅊ 싸긴하네 펼쳐보기▼</div></div></div>
          <div class="comment-item"><div class="message"><div class="text">이거 달달고소해서 맛있더라 가격 괜찮네 펼쳐보기▼</div></div></div>
        </body></html>
        """
        ruli_html = """
        <html><body>
          <div class="source_url"><a href="https://www.coupang.com/vp/products/50002">쿠팡</a></div>
          <div class="view_content">무료배송</div>
          <div class="comment_view">
            <div class="text_wrapper"><div class="text">이 글을 보지 않았다면 돈을 아꼈을 것인데!</div></div>
            <div class="text_wrapper"><div class="text">2개 사니까 단가 저렴하고 괜찮네요</div></div>
          </div>
        </body></html>
        """
        dealbada_html = """
        <html><body>
          <div id="bo_v_title">케어웰 당뇨식 200ml 24개</div>
          <div id="bo_v_link"><a href="https://www.coupang.com/vp/products/50003">쿠팡</a></div>
          <div id="bo_v_con">무료배송</div>
          <div id="bo_vc"><article><p>이 가격이면 쟁여둘만 하네요</p></article></div>
        </body></html>
        """
        with patch.object(push_deals.requests, "get", return_value=MagicMock(status_code=200, text=arca_html)):
            d_arca = push_deals.parse_arcalive_detail("https://arca.live/b/hotdeal/50001")
        self.assertEqual(d_arca["comment_texts"], ["ㅁㅊ 싸긴하네", "이거 달달고소해서 맛있더라 가격 괜찮네"])

        with patch.object(push_deals.requests, "get", return_value=MagicMock(status_code=200, text=ruli_html)):
            d_ruli = push_deals.parse_ruliweb_detail("https://bbs.ruliweb.com/market/board/1020/read/50002")
        self.assertEqual(d_ruli["comment_texts"], ["이 글을 보지 않았다면 돈을 아꼈을 것인데!", "2개 사니까 단가 저렴하고 괜찮네요"])

        with patch.object(push_deals.requests, "get", return_value=MagicMock(status_code=200, text=dealbada_html)):
            d_db = push_deals.parse_dealbada_detail("https://www.dealbada.com/bbs/board.php?bo_table=deal_domestic&wr_id=50003")
        self.assertEqual(d_db["comment_texts"], ["이 가격이면 쟁여둘만 하네요"])

        # 4) process_deal이 comment_texts -> reaction_text, votes, comments, 출처 배지를 적재하는지 확인
        seen = set()
        queue = []
        ocr_cache = {}
        detail_stub = lambda _u: {
            "coupang_url": "https://www.coupang.com/vp/products/50004",
            "body_text": "무료배송",
            "img_url": None,
            "ocr_img_urls": [],
            "extracted_title": "베스킨라빈스 쫀떡궁합 파이, 264g, 3개",
            "extracted_price": 9520,
            "extracted_shipping": "무료배송",
            "comment_texts": d_arca["comment_texts"],
        }
        deal_obj = {
            "id": "arca_50004",
            "title": "베스킨라빈스 쫀떡궁합 파이, 264g, 3개 9,520원",
            "link": "https://arca.live/b/hotdeal/50004",
            "votes": 6,
            "downvotes": 0,
            "comments": 12,
            "hits": 0,
        }
        with (
            patch.object(push_deals, "ENABLE_TRACK_B2_NO_ORIG_FOOD", True),
            patch.object(
                push_deals.coupang_api,
                "create_deeplink",
                return_value={"data": [{"shortenUrl": "https://link.coupang.com/a/50004"}]},
            ),
        ):
            ok_p = push_deals.process_deal(deal_obj, detail_stub, "arca", seen, flyer_queue=queue, ocr_cache=ocr_cache)
        self.assertTrue(ok_p)
        item = queue[0]
        self.assertEqual(item["votes"], 6)
        self.assertEqual(item["comments"], 12)
        self.assertEqual(item["evidence_badge"], "아카라이브 인기")
        self.assertIn("100g당 1,202원", item["evidence_note"])
        self.assertIn("아카라이브 추천 6 · 댓글 12", item["evidence_note"])
        self.assertEqual(item["reaction_text"], "이거 달달고소해서 맛있더라 가격 괜찮네")

        # 5) 캡션 출처·추천·댓글·반응 표기 + 0건 출처 표기 + 할인율 구간별 문구 + 4건 길이 제한 + 전단지 렌더링 검증
        cap_b2, _ = flyer_renderer.format_flyer_caption_and_buttons([item])
        self.assertIn("[아카라이브 인기]", cap_b2)
        self.assertIn('💬 커뮤니티 반응 (아카라이브 · 추천 6 · 댓글 12): "이거 달달고소해서 맛있더라 가격 괜찮네"', cap_b2)

        track_a_zero = {
            "clean_title": "QCY 무선 오픈형 이어폰 블루투스",
            "short_title": "QCY 오픈형",
            "source": "arca",
            "orig_price": 41400,
            "sale_price": 25900,
            "discount_rate": 37,
            "unit_info": "",
            "shipping_label": "로켓 무료배송",
            "deeplink": "https://link.coupang.com/a/50005",
            "votes": 0,
            "comments": 0,
            "reaction_text": "",
        }
        cap_a0, _ = flyer_renderer.format_flyer_caption_and_buttons([track_a_zero])
        self.assertIn("📌 출처: 아카라이브", cap_a0)
        self.assertNotIn("추천 0", cap_a0)
        self.assertNotIn("댓글 0", cap_a0)

        # 78%는 반값 특가가 아니라 대폭 할인으로 표기 확인
        mega_78 = {**track_a_zero, "orig_price": 25000, "sale_price": 5400, "discount_rate": 78}
        cap_78, _ = flyer_renderer.format_flyer_caption_and_buttons([mega_78])
        self.assertIn("78%↓ 대폭 할인", cap_78)
        self.assertIn("🚨<b>[대폭 할인]</b>", cap_78)
        self.assertNotIn("반값", cap_78)

        mega_65 = {**track_a_zero, "discount_rate": 65}
        cap_65, _ = flyer_renderer.format_flyer_caption_and_buttons([mega_65])
        self.assertIn("65%↓ 반값 이상 할인", cap_65)

        mega_55 = {**track_a_zero, "discount_rate": 55}
        cap_55, _ = flyer_renderer.format_flyer_caption_and_buttons([mega_55])
        self.assertIn("55%↓ 반값 할인", cap_55)
        self.assertNotIn("특가", cap_55)

        verbose_batch = [
            {
                **item,
                "clean_title": f"프리미엄 유기농 생수 대용량 특가 상품 패키지 테스트 {idx}번 500ml 40개입",
                "deeplink": f"https://link.coupang.com/a/very-long-slug-item-{idx}-abcdefghij",
                "reaction_text": "역대 최저가 수준이라 바로 여러 세트 쟁여뒀습니다 정말 가성비 좋네요",
            }
            for idx in range(1, 5)
        ]
        cap_4, btns_4 = flyer_renderer.format_flyer_caption_and_buttons(verbose_batch)
        self.assertLessEqual(len(cap_4), 1020)
        self.assertEqual(len(btns_4), 4)
        self.assertEqual(cap_4.count("<b>"), cap_4.count("</b>"))
        self.assertEqual(cap_4.count("<a "), cap_4.count("</a>"))

        white_img = Image.new("RGB", (200, 200), (255, 255, 255))
        with patch.object(flyer_renderer, "resolve_clean_product_image", return_value=white_img):
            png_1 = flyer_renderer.render_hotdeal_flyer([item])
            png_2 = flyer_renderer.render_hotdeal_flyer([item, track_a_zero])
            png_78 = flyer_renderer.render_hotdeal_flyer([mega_78])
        self.assertTrue(png_1.startswith(b"\x89PNG"))
        self.assertTrue(png_2.startswith(b"\x89PNG"))
        self.assertTrue(png_78.startswith(b"\x89PNG"))

    def test_default_min_20_percent_and_track_b2_disabled(self):
        from datetime import datetime, timedelta

        self.assertTrue(push_deals.ENABLE_GOLDBOX)
        self.assertEqual(push_deals.GOLDBOX_PER_SLOT, 4)
        self.assertEqual(push_deals.LOW_PRICE_BAIT_MAX_PRICE, 15_000)
        self.assertEqual(push_deals.LOW_PRICE_BAIT_MIN_RATE, 15)
        self.assertFalse(push_deals.ENABLE_TRACK_B2_NO_ORIG_FOOD)
        self.assertTrue(push_deals.ENABLE_COMMUNITY_HOT_NO_ORIG)
        self.assertEqual(
            push_deals.CATEGORY_MIN_DISCOUNT_MAP,
            {"food": 20, "living": 20, "it": 20, "general": 20},
        )
        self.assertEqual(push_deals.HIGH_TICKET_MIN_ORIG_PRICE, 300_000)

        # 1) 비활성 모드 회귀: 정가 없는 식품 딜은 추천이 많아도 송출 제외, 단 관측 DB에는 적재
        with patch.object(push_deals, "ENABLE_COMMUNITY_HOT_NO_ORIG", False):
            ok_b2, seen_b2, q_b2 = self._run_process_deal(
                deal_id="arca_b2_disabled_lemon",
                title="솔리몬 스퀴즈드 레몬즙, 1L, 1개 5,400원",
                body_text="무료배송",
                product_id="60001",
                extra_shipping="무료배송",
                votes=10,
                comments=20,
                enable_track_b2=False,
            )
        self.assertFalse(ok_b2)
        self.assertEqual(len(q_b2), 0)
        self.assertIn("arca_b2_disabled_lemon", seen_b2)
        self.assertEqual(len(self._fetch_observations("60001")), 1)

        # 2) 저가 미끼 조건 미달 및 20% 미만 할인 차단 검증
        for did, pid, title, body, ship in (
            ("arca_low_food_3", "60002", "솔리몬 스퀴즈드 레몬즙, 1L, 1개", "정가 5,880원 -> 특가 5,680원 무료배송", "무료배송"),
            ("arca_low_food_14", "60022", "국내산 생수 500ml 20병", "정가 10,000원 -> 특가 8,600원 무료배송", "무료배송"),
            ("arca_low_food_paid_18", "60023", "국내산 생수 500ml 20병", "정가 10,000원 -> 특가 8,200원 배송비 3,000원", "배송비 3,000원"),
            ("arca_low_food_over15k", "60003", "국내산 생수 500ml 40병", "정가 20,000원 -> 특가 16,400원 무료배송", "무료배송"),
            ("arca_low_living_19", "60004", "고밀도 순면 세면타월 10매", "정가 20,000원 -> 특가 16,200원 무료배송", "무료배송"),
            ("arca_low_general_18", "60024", "스마트폰 거치대 접이식", "정가 10,000원 -> 특가 8,200원 무료배송", "무료배송"),
            ("arca_low_it_under30", "60005", "무선 기계식 키보드 텐키리스", "정가 250,000원 -> 특가 205,000원 무료배송", "무료배송"),
        ):
            with self.subTest(reject_low=did):
                ok_l, _sl, q_l = self._run_process_deal(
                    deal_id=did,
                    title=title,
                    body_text=body,
                    product_id=pid,
                    extra_shipping=ship,
                    votes=5,
                    comments=5,
                    enable_track_b2=False,
                )
                self.assertFalse(ok_l)
                self.assertEqual(len(q_l), 0)

        # 2-b) 15,000원 이하 식품/생필품 + 무료·와우배송 + 15%+ 저가 미끼 통과 검증
        for did, pid, title, body, ship, exp_rate in (
            ("arca_bait_food_18", "60025", "국내산 생수 500ml 20병", "정가 10,000원 -> 특가 8,200원 무료배송", "무료배송", 18),
            ("arca_bait_living_15", "60026", "고밀도 순면 세면타월 5매", "정가 15,000원 -> 특가 12,700원 와우 무료배송", "와우 무료배송", 15),
        ):
            with self.subTest(bait_allow=did):
                ok_bt, _sbt, q_bt = self._run_process_deal(
                    deal_id=did,
                    title=title,
                    body_text=body,
                    product_id=pid,
                    extra_shipping=ship,
                    votes=5,
                    comments=2,
                    enable_track_b2=False,
                )
                self.assertTrue(ok_bt)
                self.assertEqual(q_bt[0]["track"], "A")
                self.assertEqual(q_bt[0]["discount_rate"], exp_rate)

        # 3) 20% 할인(식품 포함)은 정상 통과
        ok_f20, _sf20, q_f20 = self._run_process_deal(
            deal_id="arca_food_20_ok",
            title="국내산 생수 500ml 20병",
            body_text="정가 10,000원 -> 특가 8,000원 무료배송",
            product_id="60006",
            extra_shipping="무료배송",
            votes=2,
            comments=0,
            enable_track_b2=False,
        )
        self.assertTrue(ok_f20)
        self.assertEqual(q_f20[0]["track"], "A")
        self.assertEqual(q_f20[0]["discount_rate"], 20)

        # 4) 30만 원 이상 고가 상품 예외: 12%+ 및 5만원+ 절약 시 통과, 미달 시 차단
        for did, pid, title, body in (
            ("arca_ht_350k_ok", "60010", "프리미엄 로봇청소기 스테이션", "정가 350,000원 -> 특가 295,000원 무료배송"),
            ("arca_ht_1200k_ok", "60011", "삼성 갤럭시북 프로 16인치 노트북", "정가 1,200,000원 -> 특가 1,000,000원 무료배송"),
        ):
            with self.subTest(high_ticket_allow=did):
                ok_ht, _sht, q_ht = self._run_process_deal(
                    deal_id=did,
                    title=title,
                    body_text=body,
                    product_id=pid,
                    extra_shipping="무료배송",
                    votes=0,
                    comments=0,
                    enable_track_b2=False,
                )
                self.assertTrue(ok_ht)
                self.assertEqual(q_ht[0]["track"], "A")

        for did, pid, body in (
            ("arca_ht_11pct_reject", "60012", "정가 350,000원 -> 특가 310,000원 무료배송"),
            ("arca_ht_45k_reject", "60013", "정가 350,000원 -> 특가 305,000원 무료배송"),
        ):
            with self.subTest(high_ticket_reject=did):
                ok_hr, _shr, q_hr = self._run_process_deal(
                    deal_id=did,
                    title="프리미엄 무선청소기 거치대 세트",
                    body_text=body,
                    product_id=pid,
                    extra_shipping="무료배송",
                    votes=5,
                    comments=5,
                    enable_track_b2=False,
                )
                self.assertFalse(ok_hr)
                self.assertEqual(len(q_hr), 0)

        # 5) Track B1 (30일 관측 DB 중앙값 비교) 역시 20% 이상 하락만 통과
        now_kst = datetime.now(push_deals.KST)
        for d_ago in range(2, 7):
            self.db.record_observation(
                option_key="pid:60020:cfg:vol:500ml:n40",
                product_id="60020",
                total_price=20000,
                sale_price=20000,
                shipping_type="free",
                category="food",
                source="arca",
                obs_date=(now_kst - timedelta(days=d_ago)).strftime("%Y-%m-%d"),
            )

        ok_b1_18, _sb18, q_b18 = self._run_process_deal(
            deal_id="arca_b1_18_reject",
            title="국내산 생수 500ml 40병 16,400원",
            body_text="무료배송",
            product_id="60020",
            extra_shipping="무료배송",
            votes=0,
            comments=0,
            enable_track_b2=False,
        )
        self.assertFalse(ok_b1_18)
        self.assertEqual(len(q_b18), 0)

        ok_b1_21, _sb21, q_b21 = self._run_process_deal(
            deal_id="arca_b1_21_ok",
            title="국내산 생수 500ml 40병 15,800원",
            body_text="무료배송",
            product_id="60020",
            extra_shipping="무료배송",
            votes=0,
            comments=0,
            enable_track_b2=False,
        )
        self.assertTrue(ok_b1_21)
        self.assertEqual(q_b21[0]["track"], "B")
        self.assertEqual(q_b21[0]["evidence_badge"], "30일 관측가 21%↓")

    def test_goldbox_bait_selection_and_flyer_rendering(self):
        from datetime import datetime
        import flyer_renderer
        from PIL import Image

        raw_items = [
            {"productId": 90001, "productName": "호텔 숙박권", "productPrice": 29000, "isRocket": False, "isFreeShipping": False, "categoryName": "국내투어", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90001", "rank": 1},
            {"productId": 90002, "productName": "3인용 가죽 소파", "productPrice": 319000, "isRocket": True, "isFreeShipping": True, "categoryName": "가구/홈인테리어", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90002", "rank": 2},
            {"productId": 90003, "productName": "여성 긴소매 티셔츠", "productPrice": 35100, "isRocket": True, "isFreeShipping": False, "categoryName": "패션의류", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90003", "rank": 3},
            {"productId": 90004, "productName": "코카콜라 제로 350ml 24개", "productPrice": 18700, "isRocket": True, "isFreeShipping": True, "categoryName": "식품", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90004", "rank": 4},
            {"productId": 90005, "productName": "포켓형 핫팩 100g 30개", "productPrice": 11130, "isRocket": True, "isFreeShipping": False, "categoryName": "생활용품", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90005", "rank": 5},
            {"productId": 90006, "productName": "하림 닭가슴살 블랙페퍼 100g 10개", "productPrice": 16510, "isRocket": True, "isFreeShipping": False, "categoryName": "로켓프레시", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90006", "rank": 6},
            {"productId": 90007, "productName": "알카라인 AAA건전지 40입", "productPrice": 8590, "isRocket": True, "isFreeShipping": False, "categoryName": "생활용품", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90007", "rank": 7},
            {"productId": 90008, "productName": "닥터지 블랙 스네일 세트", "productPrice": 25700, "isRocket": True, "isFreeShipping": False, "categoryName": "뷰티", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90008", "rank": 8},
            {"productId": 90009, "productName": "오레오 웨하스스틱 초코 10개", "productPrice": 22550, "isRocket": True, "isFreeShipping": True, "categoryName": "식품", "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=90009", "rank": 9},
        ]

        seen = set()
        slot1_pick = push_deals.select_goldbox_bait_items(raw_items, seen, max_items=4)
        self.assertEqual([x["productId"] for x in slot1_pick], [90004, 90005, 90006, 90007])
        for x in slot1_pick:
            push_deals.mark_product_seen(str(x["productId"]), seen)
        slot2_pick = push_deals.select_goldbox_bait_items(raw_items, seen, max_items=4)
        self.assertEqual([x["productId"] for x in slot2_pick], [90009, 90008, 90003])

        # run_goldbox_staggered 오전/저녁 2타임 통합 전단지 송출 검증
        sent_calls = []
        registered_sources = []

        def fake_send(_chat, png_bytes, caption, inline_kb):
            sent_calls.append((png_bytes, caption, inline_kb))
            return True

        def fake_register(url, **kwargs):
            registered_sources.append((kwargs.get("source"), kwargs.get("track"), kwargs.get("deal_id")))
            return f"https://hotpang-click.example.workers.dev/r/{kwargs.get('product_id')}"

        white_img = Image.new("RGB", (200, 200), (255, 255, 255))
        seen_gb = set()
        quota = {"date": "20261127", "community_sent": 0, "goldbox_sent": 0, "last_community_ts": 0}
        morning_dt = datetime(2026, 11, 27, 8, 30, tzinfo=push_deals.KST)
        evening_dt = datetime(2026, 11, 27, 18, 30, tzinfo=push_deals.KST)
        idle_dt = datetime(2026, 11, 27, 14, 0, tzinfo=push_deals.KST)

        with (
            patch.object(push_deals.coupang_api, "get_goldbox_products", return_value={"data": raw_items}),
            patch.object(push_deals, "enrich_product_spec", side_effect=lambda name, *_a: name),
            patch.object(push_deals, "register_tracked_link", side_effect=fake_register),
            patch.object(flyer_renderer, "resolve_clean_product_image", return_value=white_img),
            patch.object(push_deals, "send_telegram_photo_bytes", side_effect=fake_send),
            patch.object(push_deals.time, "sleep"),
        ):
            self.assertEqual(push_deals.run_goldbox_staggered(seen_gb, quota, now_dt=idle_dt), 0)

            n1 = push_deals.run_goldbox_staggered(seen_gb, quota, now_dt=morning_dt)
            self.assertEqual(n1, 4)
            self.assertEqual(quota["goldbox_sent"], 4)
            # 같은 오전 슬롯 재실행 시 중복 차단
            self.assertEqual(push_deals.run_goldbox_staggered(seen_gb, quota, now_dt=morning_dt), 0)

            n2 = push_deals.run_goldbox_staggered(seen_gb, quota, now_dt=evening_dt)
            self.assertEqual(n2, 3)
            self.assertEqual(quota["goldbox_sent"], 7)

        self.assertEqual(len(sent_calls), 2)
        png_m, cap_m, kb_m = sent_calls[0]
        self.assertTrue(png_m.startswith(b"\x89PNG"))
        self.assertEqual(len(kb_m), 4)
        self.assertIn("🍯 <b>[쿠팡 오늘의 골드박스 · 오전]</b>", cap_m)
        self.assertIn("일일 할인 품목 중 생필품·식품 위주", cap_m)
        self.assertNotIn("[골드박스 특가]", cap_m)
        self.assertNotIn("특가", cap_m)
        self.assertNotIn("· 골드박스", cap_m)
        self.assertEqual(cap_m.count("<b>"), cap_m.count("</b>"))
        self.assertTrue(all(src == "goldbox" and trk == "G" for src, trk, _ in registered_sources))

        _png_e, cap_e, kb_e = sent_calls[1]
        self.assertEqual(len(kb_e), 3)
        self.assertIn("🍯 <b>[쿠팡 오늘의 골드박스 · 저녁]</b>", cap_e)

    def test_ppomppu_comments_writer_body_and_smart_reaction_filter(self):
        import flyer_renderer
        from unittest.mock import MagicMock

        # 1) 뽐뿌 상세 + comment.php JSON 댓글 수집 (is_accuse 제외, HTML 태그 제거)
        ppom_html = """
        <html><body>
          <script>var G_CMT_CNT = 3;</script>
          <td class="board-contents">재질도 부드럽고 푹신하더라구요. 가격 메리트 있어서 재구매합니다.<br>https://www.coupang.com/vp/products/752343</td>
        </body></html>
        """
        ppom_cmt_json = {
            "comments": [
                {"is_accuse": 1, "memo": "<p>신고된 댓글</p>"},
                {"is_accuse": 0, "memo": "<p>쿠팡 아웃</p>"},
                {"is_accuse": 0, "memo": "<p>재구매했는데&nbsp;단가 저렴하고 좋네요</p>"},
            ]
        }

        def fake_get(url, **_kwargs):
            if "comment.php" in url:
                res = MagicMock(status_code=200)
                res.json.return_value = ppom_cmt_json
                return res
            res = MagicMock(status_code=200, text=ppom_html)
            return res

        with patch.object(push_deals.requests, "get", side_effect=fake_get):
            d_pp = push_deals.parse_ppomppu_detail("https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no=752343")
        self.assertEqual(d_pp["comment_texts"], ["쿠팡 아웃", "재구매했는데 단가 저렴하고 좋네요"])
        q_pp = push_deals.extract_best_reaction_quote(d_pp["comment_texts"], body_text=d_pp["body_text"], max_len=40)
        self.assertEqual(q_pp, "재구매했는데 단가 저렴하고 좋네요")

        # 2) 글쓴이 추천 본문 추출 (댓글이 비어있거나 밈/잡담뿐일 때 작성자 추천의 글이 채택되어야 함)
        q_body = push_deals.extract_best_reaction_quote(
            ["병아리에게 미안해지는구나...", "이거 어때요?", "ㅋㅋㅋ"],
            body_text="정제알약 잘 못먹는 사람들이 먹기 괜찮음\n가격 괜찮은 듯",
            max_len=40,
        )
        self.assertEqual(q_body, "정제알약 잘 못먹는 사람들이 먹기 괜찮음")

        # 3) 되팔이/반쿠팡/부정/질문/잡담 필터 검증 -> 신호 없으면 빈 문자열
        q_noise = push_deals.extract_best_reaction_quote(
            [
                "쿠팡은 다른의견",
                "의견 다름",
                "쓰다가 버리는 용도인감 가격은 겁나 싸네",
                "쌈? 맛있음?",
                "오곡 코코볼",
            ],
            body_text="필요는 없는데 당근하면 20은 받지 않을까해서",
            max_len=40,
        )
        self.assertEqual(q_noise, "")

        # 4) 4건 전단지 묶음 캡션에서도 커뮤니티 반응 한줄평이 최대한 보존되는지 확인
        base_item = {
            "source": "ppomppu",
            "orig_price": 30000,
            "sale_price": 19900,
            "discount_rate": 34,
            "unit_info": "100g당 500원",
            "shipping_label": "로켓 무료배송",
            "votes": 4,
            "comments": 7,
            "reaction_text": "가성비 좋고 맛있어서 쟁여둡니다",
        }
        batch4 = [
            {
                **base_item,
                "clean_title": f"프리미엄 닭가슴살 스테이크 소포장 {i}호 1kg",
                "short_title": f"닭가슴살 {i}호",
                "deeplink": f"https://link.coupang.com/a/d{i}",
            }
            for i in range(1, 5)
        ]
        cap4, _ = flyer_renderer.format_flyer_caption_and_buttons(batch4)
        self.assertLessEqual(len(cap4), 1020)
        self.assertIn("가성비 좋고 맛있어서 쟁여둡니다", cap4)

    def test_track_a_30_percent_auto_passes_without_votes(self):
        # 30~39% 정가 검증 할인은 추천/댓글 0이어도 즉시 통과 (비추 가드 유지)
        for disc, orig, final in ((30, 20000, 14000), (37, 29900, 18700)):
            with self.subTest(disc=disc):
                res = push_deals.evaluate_deal_quality(
                    {"votes": 0, "downvotes": 0, "comments": 0, "hits": 10},
                    "arca",
                    {
                        "orig_price": orig,
                        "final_price": final,
                        "discount_rate": disc,
                        "shipping_fee": 0,
                        "shipping_type": "free",
                        "shipping_condition": "",
                    },
                    "코카콜라 제로 350ml 24개",
                    "무료배송",
                )
                self.assertTrue(res["passed"])
                self.assertEqual(res["reason"], "ok")
                self.assertEqual(res["track"], "A")

        # 비추천 2건 이상 & 추천 0인 35% 딜은 비추 가드로 보류
        res_dw = push_deals.evaluate_deal_quality(
            {"votes": 0, "downvotes": 2, "comments": 0, "hits": 10},
            "arca",
            {
                "orig_price": 20000,
                "final_price": 13000,
                "discount_rate": 35,
                "shipping_fee": 0,
                "shipping_type": "free",
                "shipping_condition": "",
            },
            "테스트 식품 500g",
            "무료배송",
        )
        self.assertFalse(res_dw["passed"])
        self.assertEqual(res_dw["reason"], "wait")

        # 20~29% 구간은 기존처럼 추천/댓글 없으면 대기, 추천 1+면 통과
        p25 = {
            "orig_price": 20000,
            "final_price": 15000,
            "discount_rate": 25,
            "shipping_fee": 0,
            "shipping_type": "free",
            "shipping_condition": "",
        }
        res_wait = push_deals.evaluate_deal_quality(
            {"votes": 0, "downvotes": 0, "comments": 0, "hits": 10},
            "arca",
            p25,
            "테스트 생수 2L 12병",
            "무료배송",
        )
        self.assertFalse(res_wait["passed"])
        self.assertEqual(res_wait["reason"], "wait")

        res_ok = push_deals.evaluate_deal_quality(
            {"votes": 1, "downvotes": 0, "comments": 0, "hits": 10},
            "arca",
            p25,
            "테스트 생수 2L 12병",
            "무료배송",
        )
        self.assertTrue(res_ok["passed"])
        self.assertEqual(res_ok["track"], "A")

    def test_high_ticket_100k_10pct_and_community_hot_no_orig(self):
        import flyer_renderer

        # Option 2: 정가 30만+ & 10만원 이상 절약 & 10%+ (예: 186만 -> 166만, 20만원 절약) 즉시 통과
        ok_mini, _sm, q_mini = self._run_process_deal(
            deal_id="ppom_mini_200k",
            title="HP 2026 옴니 데스크 미니 PC 16GB 512GB",
            body_text="정가 1,860,000원 -> 특가 1,660,000원 무료배송",
            product_id="70001",
            extra_shipping="무료배송",
            votes=0,
            comments=0,
            enable_track_b2=False,
        )
        self.assertTrue(ok_mini)
        self.assertEqual(q_mini[0]["track"], "A")
        self.assertEqual(q_mini[0]["discount_rate"], 10)

        # 9만원 절약(9%)은 여전히 차단
        ok_mini_no, _smn, q_mini_no = self._run_process_deal(
            deal_id="ppom_mini_90k_reject",
            title="데스크 미니 PC 16GB 256GB",
            body_text="정가 1,000,000원 -> 특가 910,000원 무료배송",
            product_id="70002",
            extra_shipping="무료배송",
            votes=3,
            comments=5,
            enable_track_b2=False,
        )
        self.assertFalse(ok_mini_no)
        self.assertEqual(len(q_mini_no), 0)

        # Option 3: 정가 미표기 + 커뮤니티 반응 높음 + 가치신호 확인 시 '반응 높음' 배지로 송출
        ok_cable, _sc, q_cable = self._run_process_deal(
            deal_id="ppom_toocki_hot",
            title="Toocki PD 60W C타입 고속충전 케이블 1m 5,400원",
            body_text="이전 할인가 대비 더 할인중이라 확실히 메리트 있습니다. 무료배송",
            product_id="70003",
            extra_shipping="무료배송",
            votes=3,
            comments=21,
            enable_track_b2=False,
            source_name="ppomppu",
        )
        self.assertTrue(ok_cable)
        self.assertEqual(q_cable[0]["track"], "B")
        self.assertEqual(q_cable[0]["evidence_badge"], "뽐뿌 반응 높음")
        cap_c, _ = flyer_renderer.format_flyer_caption_and_buttons([q_cable[0]])
        self.assertIn("[뽐뿌 반응 높음 · 쿠팡 인기딜]", cap_c)
        self.assertIn("[뽐뿌 반응 높음]", cap_c)
        self.assertNotIn("특가", cap_c)

        # 루리웹 멀티탭 (추천 3 · 댓글 6 · 본문 가격 인하 코멘트)
        ok_tab, _st, q_tab = self._run_process_deal(
            deal_id="ruli_tab_hot",
            title="브레넨스툴 수퍼솔리드 멀티탭 8구 2.5m 53,000원",
            body_text="예전에 네이버로 8만원쯤에 산거같은데 많이 내렸음 무료배송",
            product_id="70004",
            extra_shipping="무료배송",
            votes=3,
            comments=6,
            enable_track_b2=False,
            source_name="ruliweb",
        )
        self.assertTrue(ok_tab)
        self.assertEqual(q_tab[0]["evidence_badge"], "루리웹 반응 높음")

        # 가드레일 검증: 비추 포함(downvotes=1), 댓글만 많은 무추천(v=0, c=20), 밈/잡담뿐(v=3, c=6)은 모두 차단
        p0 = {
            "orig_price": 0,
            "final_price": 12000,
            "discount_rate": 0,
            "shipping_fee": 0,
            "shipping_type": "free",
            "shipping_condition": "",
        }
        r_dw = push_deals.evaluate_deal_quality(
            {"votes": 3, "downvotes": 1, "comments": 10},
            "arca",
            p0,
            "잡다한 전자제품 12,000원",
            "가성비 좋고 저렴함 무료배송",
        )
        self.assertFalse(r_dw["passed"])

        r_c_only = push_deals.evaluate_deal_quality(
            {"votes": 0, "downvotes": 0, "comments": 20},
            "ppomppu",
            p0,
            "논란 중인 상품 12,000원",
            "가성비 좋고 저렴함 무료배송",
        )
        self.assertFalse(r_c_only["passed"])

        r_meme = push_deals.evaluate_deal_quality(
            {"votes": 3, "downvotes": 0, "comments": 6},
            "arca",
            p0,
            "특수 굿즈 피규어 12,000원",
            "오곡 코코볼 무료배송",
            comment_texts=["ㅇㅇ", "ㅋㅋㅋ"],
        )
        self.assertFalse(r_meme["passed"])


class TestUpgradesIntegration(unittest.TestCase):
    def test_clien_collector_and_detail(self):
        from unittest.mock import MagicMock, patch

        list_html = """
        <div class="list_content">
          <div class="list_item symph_row" data-role="list-row" data-board-sn="18999001" data-comment-count="7">
            <div class="list_symph"><span>12</span></div>
            <div class="list_title">
              <a class="list_subject" href="/service/board/jirum/18999001">
                <span data-role="list-title-text">[쿠팡] 코멧 생수 2L 24병 (11,900원/무료)</span>
              </a>
              <span class="rSymph05">7</span>
            </div>
          </div>
          <div class="list_item symph_row" data-role="list-row" data-board-sn="18999002" data-comment-count="2">
            <div class="list_symph"><span>3</span></div>
            <div class="list_title">
              <a class="list_subject" href="/service/board/jirum/18999002">
                <span data-role="list-title-text">[네이버] 무선 마우스 특가 19,000원</span>
              </a>
            </div>
          </div>
        </div>
        """
        detail_html = """
        <div class="post_subject"><span>[쿠팡] 코멧 생수 2L 24병</span></div>
        <div class="post_content">
          <article>
            <div class="post_article">
              <a href="https://www.coupang.com/vp/products/123456789?itemId=111">구매링크</a>
              <img src="https://img.example.com/water.jpg" />
            </div>
          </article>
        </div>
        <div class="comment_view">역대가네요 바로 샀습니다</div>
        """

        list_resp = MagicMock(status_code=200, text=list_html)
        empty_resp = MagicMock(status_code=200, text='<div class="list_content"></div>')
        detail_resp = MagicMock(status_code=200, text=detail_html)

        with patch.object(push_deals.requests, "get", side_effect=[list_resp, empty_resp]) as mock_list_get:
            items = push_deals.collect_clien_deals(max_pages=2)
        self.assertEqual(mock_list_get.call_count, 2)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "clien_18999001")
        self.assertEqual(items[0]["votes"], 12)
        self.assertEqual(items[0]["comments"], 7)

        with patch.object(push_deals.requests, "get", return_value=detail_resp):
            detail = push_deals.parse_clien_detail(items[0]["link"])
        self.assertIsNotNone(detail)
        self.assertIn("coupang.com/vp/products/123456789", detail["coupang_url"])
        self.assertIn("역대가네요 바로 샀습니다", detail["comment_texts"])

    def test_flyer_footer_30d_low_and_top_clicks_caption(self):
        import flyer_renderer

        sample_deals = [
            {
                "clean_title": "코멧 생수 2L 24병",
                "short_title": "코멧 생수 2L 24병",
                "source": "clien",
                "track": "A",
                "sale_price": 11900,
                "orig_price": 16900,
                "discount_rate": 30,
                "unit_info": "100ml당 24원",
                "shipping_label": "무료",
                "deeplink": "https://hotpang-click.produckyou.workers.dev/c/abcd1234",
                "is_30d_low": True,
                "clicks": 42,
            }
        ]
        cap_default, kb_default = flyer_renderer.format_flyer_caption_and_buttons(sample_deals)
        self.assertIn("🏆<b>[30일 최저가]</b>", cap_default)
        self.assertIn("클리앙", cap_default)
        self.assertEqual(len(kb_default), 1)

        cap_pub, kb_pub = flyer_renderer.format_flyer_caption_and_buttons(
            sample_deals,
            include_bot_footer=True,
            batch_mode="top_clicks",
        )
        self.assertIn("가장 많이 클릭한 쿠팡 핫딜 TOP", cap_pub)
        self.assertIn("👆 오늘 42회 클릭", cap_pub)
        self.assertEqual(len(kb_pub), 2)
        footer_texts = [b["text"] for b in kb_pub[-1]]
        self.assertTrue(any("키워드 알림" in t for t in footer_texts))
        self.assertTrue(any("단가 비교" in t for t in footer_texts))

    def test_apply_closed_markup_and_check_posts(self):
        from unittest.mock import patch

        caption = (
            "🔥 <b>특가 전단지</b>\n"
            "① <b>[30%] 코멧 생수 2L</b> · 11,900원 👉 <a href=\"https://ex.com/a\">링크</a>\n"
            "② <b>[25%] 무세제 세탁세제</b> · 9,900원 👉 <a href=\"https://ex.com/b\">링크</a>"
        )
        items = [
            {"source": "arca", "source_url": "https://arca.live/b/hotdeal/1", "deeplink": "https://ex.com/a", "clean_title": "코멧 생수 2L", "closed": True},
            {"source": "clien", "source_url": "https://clien.net/1", "deeplink": "https://ex.com/b", "clean_title": "무세제 세탁세제", "closed": False},
        ]
        kb = [
            [
                {"text": "코멧 생수 · 11,900원", "url": "https://ex.com/a", "style": "danger"},
                {"text": "무세제 세탁세제 · 9,900원", "url": "https://ex.com/b"},
            ]
        ]
        new_cap, new_kb = push_deals._apply_closed_markup(caption, items, kb)
        self.assertIn("<s><b>[30%] 코멧 생수 2L</b></s> [종료]", new_cap)
        self.assertNotIn("<s><b>[25%] 무세제 세탁세제</b></s>", new_cap)
        self.assertTrue(new_kb[0][0]["text"].startswith("⛔ 종료 ·"))
        self.assertNotIn("style", new_kb[0][0])
        self.assertEqual(new_kb[0][1]["text"], "무세제 세탁세제 · 9,900원")

        # 멱등성 확인
        new_cap2, new_kb2 = push_deals._apply_closed_markup(new_cap, items, new_kb)
        self.assertEqual(new_cap2, new_cap)
        self.assertEqual(new_kb2, new_kb)

        fake_posts = [
            {
                "message_id": 88801,
                "chat_id": "@HOTpang911",
                "caption": caption,
                "items": [
                    {"source": "arca", "source_url": "https://arca.live/b/hotdeal/1", "deeplink": "https://ex.com/a", "clean_title": "코멧 생수 2L", "closed": False},
                ],
                "inline_kb": [[{"text": "코멧 생수 · 11,900원", "url": "https://ex.com/a"}]],
            }
        ]
        with patch.object(push_deals, "fetch_active_posted_messages", return_value=fake_posts), \
             patch.object(push_deals, "_is_source_post_closed", return_value=True), \
             patch.object(push_deals, "edit_telegram_caption", return_value=True) as mock_edit, \
             patch.object(push_deals, "update_posted_message", return_value=True) as mock_upd:
            edited = push_deals.check_and_mark_closed_posts()
        self.assertEqual(edited, 1)
        mock_edit.assert_called_once()
        mock_upd.assert_called_once()

    def test_run_top_clicks_recap_if_evening(self):
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch

        kst = timezone(timedelta(hours=9))
        seen = set()
        quota = {}

        # 19시는 스킵
        with patch.object(push_deals, "fetch_click_stats") as mock_stats:
            res_skip = push_deals.run_top_clicks_recap_if_evening(
                seen, quota, now_dt=datetime(2026, 11, 28, 19, 10, tzinfo=kst)
            )
        self.assertEqual(res_skip, 0)
        mock_stats.assert_not_called()

        fake_stats = {
            "closed_deal_ids": ["closed_1"],
            "top_today": [
                {"deal_id": "g1", "code": "p0", "source": "goldbox", "title": "골드박스 제외", "clicks": 50, "short_url": "https://s/0"},
                {"deal_id": "closed_1", "code": "p1", "source": "arca", "title": "종료딜 제외", "clicks": 30, "short_url": "https://s/1"},
                {"deal_id": "d2", "code": "1001", "source": "arca", "title": "인기 상품 A", "clicks": 12, "sale_price": 15000, "discount_rate": 35, "short_url": "https://s/2"},
                {"deal_id": "d2_dup", "code": "1001", "source": "ruliweb", "title": "인기 상품 A 중복", "clicks": 9, "sale_price": 15000, "discount_rate": 35, "short_url": "https://s/2d"},
                {"deal_id": "d3", "code": "1002", "source": "clien", "title": "인기 상품 B", "clicks": 8, "sale_price": 22000, "discount_rate": 25, "short_url": "https://s/3"},
            ],
        }
        with patch.object(push_deals, "fetch_click_stats", return_value=fake_stats), \
             patch.object(push_deals, "render_hotdeal_flyer", return_value=b"PNG"), \
             patch.object(push_deals, "send_telegram_photo_bytes", return_value=True) as mock_send, \
             patch.object(push_deals, "save_seen"), \
             patch.object(push_deals.time, "sleep"):
            res_ok = push_deals.run_top_clicks_recap_if_evening(
                seen, quota, now_dt=datetime(2026, 11, 28, 20, 5, tzinfo=kst)
            )
        self.assertEqual(res_ok, 1)
        self.assertIn("top_clicks_20261128", seen)
        sent_caption = mock_send.call_args[0][2]
        self.assertIn("가장 많이 클릭한 쿠팡 핫딜 TOP", sent_caption)
        self.assertNotIn("골드박스 제외", sent_caption)
        self.assertNotIn("종료딜 제외", sent_caption)

    def test_price_history_prune_old_records(self):
        import tempfile
        from datetime import datetime, timezone, timedelta
        from pathlib import Path
        from price_history import PriceHistoryDB

        kst = timezone(timedelta(hours=9))
        now_dt = datetime(2026, 11, 28, 4, 5, tzinfo=kst)
        old_dt = now_dt - timedelta(days=150)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_prune.db")
            db = PriceHistoryDB(db_path)
            db.record_observation("pid:1001:vid:1", "1001", 10000, 10000, shipping_type="free", now=old_dt)
            db.record_observation("pid:1002:vid:2", "1002", 12000, 12000, shipping_type="free", now=now_dt)
            db.record_published("pid:1001:vid:1", "1001", 10000, 10000, now=old_dt)
            db.record_published("pid:1002:vid:2", "1002", 12000, 12000, now=now_dt)
            db.set_source_cooldown("arca", 403, 10, "old", now=old_dt)
            db.set_source_cooldown("clien", 429, 30, "active", now=now_dt)

            res = db.prune_old_records(days=120, now=now_dt)
            self.assertEqual(res["observations"], 1)
            self.assertEqual(res["published"], 1)
            self.assertEqual(res["cooldowns"], 1)
            self.assertIsNotNone(db.get_source_cooldown("clien", now=now_dt))
            self.assertIsNone(db.get_source_cooldown("arca", now=now_dt))

    def test_ci_403_ephemeral_run_block_and_ruliweb_timeout_break(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        import requests
        from price_history import PriceHistoryDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = PriceHistoryDB(str(Path(tmpdir) / "test_ci_cd.db"))
            # 1) Pre-existing cached 403/429 cooldowns are auto-cleared under GITHUB_ACTIONS
            db.set_source_cooldown("ppomppu", 403, 120, reason="http_403")
            db.set_source_cooldown("clien", 429, 30, reason="http_429")
            push_deals._RUN_BLOCKED_SOURCES.clear()
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
                self.assertIsNone(push_deals._is_source_blocked_this_run("ppomppu", db))
                self.assertIsNone(db.get_source_cooldown("ppomppu"))
                self.assertIsNone(push_deals._is_source_blocked_this_run("clien", db))
                self.assertIsNone(db.get_source_cooldown("clien"))

                # 2) New 403 & 429 under GITHUB_ACTIONS block only current run, NOT DB
                resp_403 = MagicMock(status_code=403)
                resp_429 = MagicMock(status_code=429)
                with patch.object(push_deals, "get_price_db", return_value=db):
                    push_deals._record_source_http_block("arca", resp_403)
                    push_deals._record_source_http_block("dealbada", resp_429)
                self.assertIsNotNone(push_deals._is_source_blocked_this_run("arca", db))
                self.assertIsNotNone(push_deals._is_source_blocked_this_run("dealbada", db))
                self.assertIsNone(db.get_source_cooldown("arca"))
                self.assertIsNone(db.get_source_cooldown("dealbada"))
                push_deals._RUN_BLOCKED_SOURCES.clear()
                self.assertIsNone(push_deals._is_source_blocked_this_run("arca", db))
                self.assertIsNone(push_deals._is_source_blocked_this_run("dealbada", db))

        # 3) Ruliweb ConnectTimeout breaks immediately on 1st URL instead of waiting all 4
        with patch.object(push_deals.requests, "get", side_effect=requests.exceptions.ConnectTimeout("timeout")) as mock_get:
            res = push_deals.collect_ruliweb_deals(max_pages=3)
        self.assertEqual(res, [])
        self.assertEqual(mock_get.call_count, 1)

    def test_honest_observed_low_badges_and_watch_deeplinks(self):
        import tempfile
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        import flyer_renderer
        from price_history import PriceHistoryDB

        kst = timezone(timedelta(hours=9))
        base_dt = datetime.now(kst)
        p_info = {
            "orig_price": 20000,
            "final_price": 10970,
            "sale_price": 10970,
            "shipping_fee": 0,
            "shipping_type": "free",
            "shipping_condition": "",
            "discount_rate": 45,
        }
        deal_meta = {"votes": 5, "comments": 3, "url": "https://www.coupang.com/vp/products/77777777"}

        with tempfile.TemporaryDirectory() as tmpdir:
            db = PriceHistoryDB(str(Path(tmpdir) / "obs_tier.db"))
            for i in range(2):
                db.record_observation("pid:77777777:vid:12", "77777777", 12170, 12170, shipping_type="free", now=base_dt - timedelta(days=i + 1))
            res_2d = push_deals.evaluate_deal_quality(
                deal_meta, "ppomppu", p_info, "파워에이드 900ml 12병", "", option_key="pid:77777777:vid:12", price_db=db
            )
            self.assertFalse(res_2d["is_30d_low"])
            self.assertEqual(res_2d["observed_low_label"], "직전 관측가보다 1,200원↓ (2일)")

            for i in range(2, 7):
                db.record_observation("pid:77777777:vid:12", "77777777", 12500, 12500, shipping_type="free", now=base_dt - timedelta(days=i + 1))
            res_7d = push_deals.evaluate_deal_quality(
                deal_meta, "ppomppu", p_info, "파워에이드 900ml 12병", "", option_key="pid:77777777:vid:12", price_db=db
            )
            self.assertTrue(res_7d["is_30d_low"])
            self.assertEqual(res_7d["observed_low_label"], "최근 30일 중 7일 최저가")

        single_short = [{
            "clean_title": "파워에이드 900ml 12병",
            "short_title": "파워에이드 900ml",
            "source": "ppomppu",
            "track": "A",
            "sale_price": 10970,
            "orig_price": 20000,
            "discount_rate": 45,
            "unit_info": "1L당 1,016원",
            "shipping_label": "무료",
            "deeplink": "https://hotpang-click.produckyou.workers.dev/r/pwr90012",
            "is_30d_low": False,
            "observed_low_label": "직전 관측가보다 1,200원↓ (2일)",
        }]
        cap1, kb1 = flyer_renderer.format_flyer_caption_and_buttons(single_short, include_bot_footer=True)
        self.assertIn("📉<b>[직전 관측가보다 1,200원↓ (2일)]</b>", cap1)
        flat_urls_1 = [b.get("url", "") for row in kb1 for b in row]
        self.assertTrue(any(u.endswith("?start=w_pwr90012") for u in flat_urls_1))

        two_short = [
            single_short[0],
            {**single_short[0], "clean_title": "토레타 500ml 24병", "deeplink": "https://hotpang-click.produckyou.workers.dev/r/trt50024", "is_30d_low": True, "observed_low_label": "최근 30일 중 7일 최저가"},
        ]
        cap2, kb2 = flyer_renderer.format_flyer_caption_and_buttons(two_short, include_bot_footer=True)
        self.assertIn("🏆<b>[최근 30일 중 7일 최저가]</b>", cap2)
        flat_urls_2 = [b.get("url", "") for row in kb2 for b in row]
        self.assertTrue(any(u.endswith("?start=wf_pwr90012_trt50024") for u in flat_urls_2))

        gb_item = [{**single_short[0], "source": "goldbox"}]
        _, kb_gb = flyer_renderer.format_flyer_caption_and_buttons(gb_item, include_bot_footer=True)
        self.assertFalse(any("?start=w" in b.get("url", "") for row in kb_gb for b in row))

    def test_track_b3_consumable_and_ppomppu_mobile_rss_fallbacks(self):
        from unittest.mock import MagicMock, patch

        p0_free = {
            "orig_price": 0,
            "final_price": 18900,
            "discount_rate": 0,
            "shipping_fee": 0,
            "shipping_type": "free",
            "shipping_condition": "",
        }
        # 1) Unit-priced consumables like Coca-Cola (v=1, c=6) and Quaker cereal (v=0, c=7) pass Track B3
        r_coke = push_deals.evaluate_deal_quality(
            {"votes": 1, "downvotes": 0, "comments": 6},
            "arca",
            p0_free,
            "코카콜라 제로 레몬라임, 190ml, 60개",
            "무료배송",
        )
        self.assertTrue(r_coke["passed"])
        self.assertEqual(r_coke["track"], "B")

        r_oat = push_deals.evaluate_deal_quality(
            {"votes": 0, "downvotes": 0, "comments": 7},
            "arca",
            {**p0_free, "final_price": 4900},
            "퀘이커 오트 그래놀라 시리얼 골든넛츠 350g",
            "무료배송",
        )
        self.assertTrue(r_oat["passed"])
        self.assertEqual(r_oat["track"], "B")

        # 2) Non-unit generic item with votes=0 still rejected
        r_gen = push_deals.evaluate_deal_quality(
            {"votes": 0, "downvotes": 0, "comments": 20},
            "ppomppu",
            {**p0_free, "final_price": 12000},
            "논란 중인 상품 12,000원",
            "가성비 좋고 저렴함 무료배송",
        )
        self.assertFalse(r_gen["passed"])

        # 3) collect_ppomppu_deals falls back to mobile + RSS when desktop returns 403
        mobile_html = (
            '<a href="/new/bbs_view.php?id=ppomppu&no=752601">[쿠팡] 코멧 생수 2L 12병 (6,900원/무료)</a>'
        )
        rss_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<rss><channel><item>"
            "<title>[쿠팡] 곰곰 백미 10kg (24,900원/무배)</title>"
            "<link>https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&amp;no=752602</link>"
            "</item></channel></rss>"
        )
        seen_urls = []

        def fake_get(url, **_kw):
            seen_urls.append(url)
            resp = MagicMock()
            resp.headers = {"content-type": "text/html; charset=utf-8"}
            if "zboard.php" in url:
                resp.status_code = 403
                resp.text = "Forbidden"
            elif "m.ppomppu.co.kr/new/bbs_list.php" in url:
                resp.status_code = 200
                resp.text = mobile_html
            elif "rss.php" in url:
                resp.status_code = 200
                resp.text = rss_xml
            else:
                resp.status_code = 404
                resp.text = ""
            return resp

        with patch.object(push_deals.requests, "get", side_effect=fake_get), \
             patch.object(push_deals.time, "sleep"):
            got = push_deals.collect_ppomppu_deals(max_pages=2)
        got_ids = {d["id"] for d in got}
        self.assertEqual(got_ids, {"ppomppu_752601", "ppomppu_752602"})

        # 4) parse_ppomppu_detail falls back to mobile view when desktop returns 403
        def fake_detail_get(url, **_kw):
            resp = MagicMock()
            resp.headers = {"content-type": "text/html; charset=utf-8"}
            if "zboard/view.php" in url:
                resp.status_code = 403
                resp.text = "Forbidden"
            elif "m.ppomppu.co.kr/new/bbs_view.php" in url:
                resp.status_code = 200
                resp.text = '<div class="cont">무료배송 https://www.coupang.com/vp/products/888999</div>'
            else:
                resp.status_code = 200
                resp.text = "[]"
            return resp

        try:
            with patch.object(push_deals.requests, "get", side_effect=fake_detail_get), \
                 patch.object(push_deals.time, "sleep"):
                detail = push_deals.parse_ppomppu_detail("https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no=752601")
            self.assertIsNotNone(detail)
            self.assertIn("888999", detail["coupang_url"])
        finally:
            push_deals._RUN_BLOCKED_SOURCES.clear()

        # 5) collect_ruliweb_deals requests plain board page before search URL
        ruli_urls = []

        def fake_ruli_get(url, **_kw):
            ruli_urls.append(url)
            resp = MagicMock(status_code=200, text="<html></html>")
            return resp

        with patch.object(push_deals.requests, "get", side_effect=fake_ruli_get):
            push_deals.collect_ruliweb_deals(max_pages=2)
        self.assertTrue(ruli_urls[0].endswith("?page=1"))
        self.assertIn("search_type=subject", ruli_urls[-1])


if __name__ == "__main__":
    unittest.main()
