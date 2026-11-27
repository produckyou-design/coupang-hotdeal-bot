"""Cloudflare Workers 실시간 클릭 트래커 클라이언트 및 push_deals 연동 테스트."""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["DEAL_PUSHER_LOG_FILE"] = ""

import click_tracker
import push_deals


class ClickTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = {
            "CLICK_TRACKER_BASE_URL": os.environ.get("CLICK_TRACKER_BASE_URL"),
            "CLICK_TRACKER_API_KEY": os.environ.get("CLICK_TRACKER_API_KEY"),
        }
        os.environ.pop("CLICK_TRACKER_BASE_URL", None)
        os.environ.pop("CLICK_TRACKER_API_KEY", None)

    def tearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_enabled_and_disabled_behavior(self):
        self.assertFalse(click_tracker.is_click_tracker_enabled())
        raw = "https://link.coupang.com/a/abc123"
        self.assertEqual(click_tracker.register_tracked_link(raw), raw)
        self.assertIsNone(click_tracker.fetch_click_stats())

        os.environ["CLICK_TRACKER_BASE_URL"] = "https://hotpang-click.example.workers.dev/"
        self.assertFalse(click_tracker.is_click_tracker_enabled())

        os.environ["CLICK_TRACKER_API_KEY"] = "secret-key"
        self.assertTrue(click_tracker.is_click_tracker_enabled())

    def test_register_tracked_link_success_and_fail_open(self):
        os.environ["CLICK_TRACKER_BASE_URL"] = "https://hotpang-click.example.workers.dev/"
        os.environ["CLICK_TRACKER_API_KEY"] = "secret-key"
        raw = "https://link.coupang.com/a/abc123"

        ok_res = MagicMock(status_code=200)
        ok_res.json.return_value = {
            "ok": True,
            "code": "Xy91abCD",
            "short_url": "https://hotpang-click.example.workers.dev/r/Xy91abCD",
        }
        with patch.object(click_tracker.requests, "post", return_value=ok_res) as mock_post:
            out = click_tracker.register_tracked_link(
                raw,
                deal_id="arca_100",
                product_id="999",
                source="arca",
                track="A",
                title="테스트 상품",
                sale_price=19900,
                discount_rate=35,
            )
        self.assertEqual(out, "https://hotpang-click.example.workers.dev/r/Xy91abCD")
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(
            mock_post.call_args.args[0],
            "https://hotpang-click.example.workers.dev/api/links",
        )
        self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer secret-key")
        self.assertEqual(call_kwargs["json"]["target_url"], raw)
        self.assertEqual(call_kwargs["json"]["source"], "arca")

        err_res = MagicMock(status_code=500)
        with patch.object(click_tracker.requests, "post", return_value=err_res):
            self.assertEqual(click_tracker.register_tracked_link(raw), raw)

        with patch.object(click_tracker.requests, "post", side_effect=RuntimeError("timeout")):
            self.assertEqual(click_tracker.register_tracked_link(raw), raw)

    def test_fetch_and_format_click_stats(self):
        os.environ["CLICK_TRACKER_BASE_URL"] = "https://hotpang-click.example.workers.dev"
        os.environ["CLICK_TRACKER_API_KEY"] = "secret-key"

        stats_payload = {
            "ok": True,
            "today_kst": "2026-11-27",
            "days": 7,
            "today": {"clicks": 18, "unique_links": 4},
            "period_total": {"clicks": 92, "total_links": 21},
            "by_source_today": [
                {"source": "arca", "clicks": 12},
                {"source": "ppomppu", "clicks": 6},
            ],
            "top_today": [
                {
                    "code": "Abc12345",
                    "title": "삼성전자 <무선> 이어폰 특가",
                    "source": "arca",
                    "sale_price": 86000,
                    "clicks": 10,
                }
            ],
        }
        get_res = MagicMock(status_code=200)
        get_res.json.return_value = stats_payload
        with patch.object(click_tracker.requests, "get", return_value=get_res) as mock_get:
            got = click_tracker.fetch_click_stats(days=7, limit=5)
        self.assertEqual(got, stats_payload)
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Authorization"],
            "Bearer secret-key",
        )

        msg = click_tracker.format_click_stats_telegram(got, top_n=3)
        self.assertIn("실시간 클릭 현황", msg)
        self.assertIn("18회", msg)
        self.assertIn("92회", msg)
        self.assertIn("아카라이브 12회", msg)
        self.assertIn("삼성전자 &lt;무선&gt; 이어폰 특가", msg)
        self.assertIn("86,000원", msg)

    def test_process_deal_click_tracker_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig_globals = {
                "SEEN_FILE": push_deals.SEEN_FILE,
                "QUOTA_FILE": push_deals.QUOTA_FILE,
                "QUEUE_FILE": push_deals.QUEUE_FILE,
                "TRACKER_FILE": push_deals.TRACKER_FILE,
                "OCR_CACHE_FILE": push_deals.OCR_CACHE_FILE,
                "PRICE_DB_FILE": push_deals.PRICE_DB_FILE,
                "LOCK_FILE_PATH": push_deals.LOCK_FILE_PATH,
                "BOT_TOKEN": push_deals.BOT_TOKEN,
            }
            try:
                push_deals.SEEN_FILE = os.path.join(tmp, "seen_deals.json")
                push_deals.QUOTA_FILE = os.path.join(tmp, "post_quota.json")
                push_deals.QUEUE_FILE = os.path.join(tmp, "flyer_queue.json")
                push_deals.TRACKER_FILE = os.path.join(tmp, "sales_tracker.json")
                push_deals.OCR_CACHE_FILE = os.path.join(tmp, "ocr_cache.json")
                push_deals.PRICE_DB_FILE = os.path.join(tmp, "price_history.db")
                push_deals.LOCK_FILE_PATH = os.path.join(tmp, "deal_pusher.lock")
                push_deals._PRICE_DB_CACHE.clear()
                push_deals.BOT_TOKEN = "test-token"

                def run_once(deal_id: str, pid: str):
                    seen = set()
                    queue = []
                    ocr_cache = {}
                    detail_fn = lambda _u: {
                        "coupang_url": f"https://www.coupang.com/vp/products/{pid}",
                        "body_text": "정가 20,000원 -> 특가 15,000원 무료배송",
                        "img_url": None,
                        "ocr_img_urls": [],
                        "extracted_title": "테스트 생수 500ml 20병",
                        "extracted_shipping": "무료배송",
                    }
                    deal = {
                        "id": deal_id,
                        "title": "테스트 생수 500ml 20병",
                        "link": f"https://arca.live/b/hotdeal/{pid}",
                        "votes": 5,
                        "downvotes": 0,
                        "comments": 2,
                        "hits": 0,
                    }
                    with (
                        patch.object(push_deals, "send_telegram_photo_bytes", return_value=True),
                        patch.object(
                            push_deals.coupang_api,
                            "create_deeplink",
                            return_value={"data": [{"shortenUrl": f"https://link.coupang.com/a/{pid}"}]},
                        ),
                    ):
                        ok = push_deals.process_deal(deal, detail_fn, "arca", seen, flyer_queue=queue, ocr_cache=ocr_cache)
                    return ok, queue

                # 1) 트래커 미설정 시 원본 쿠팡 딥링크 유지
                ok1, q1 = run_once("arca_trk_off", "71001")
                self.assertTrue(ok1)
                self.assertEqual(q1[0]["deeplink"], "https://link.coupang.com/a/71001")

                # 2) 트래커 설정 시 단축 트래킹 URL로 교체
                os.environ["CLICK_TRACKER_BASE_URL"] = "https://hotpang-click.example.workers.dev"
                os.environ["CLICK_TRACKER_API_KEY"] = "secret-key"
                ok_res = MagicMock(status_code=200)
                ok_res.json.return_value = {
                    "ok": True,
                    "code": "Tracked9",
                    "short_url": "https://hotpang-click.example.workers.dev/r/Tracked9",
                }
                with patch.object(click_tracker.requests, "post", return_value=ok_res):
                    ok2, q2 = run_once("arca_trk_on", "71002")
                self.assertTrue(ok2)
                self.assertEqual(q2[0]["deeplink"], "https://hotpang-click.example.workers.dev/r/Tracked9")
            finally:
                push_deals._PRICE_DB_CACHE.clear()
                for k, v in orig_globals.items():
                    setattr(push_deals, k, v)

    def test_bot_clicks_cmd_and_remote_keyboard(self):
        try:
            import bot
        except Exception as exc:
            self.skipTest(f"bot module unavailable in this checkout: {exc}")
        if not hasattr(bot, "clicks_cmd"):
            self.skipTest("bot.clicks_cmd not present in this checkout")

        kb_texts = [btn.text for row in bot.build_remote_keyboard().keyboard for btn in row]
        self.assertIn("⚡ 실시간 클릭", kb_texts)

        stats_payload = {
            "ok": True,
            "today_kst": "2026-11-27",
            "days": 7,
            "today": {"clicks": 12, "unique_links": 3},
            "period_total": {"clicks": 45, "total_links": 10},
            "by_source_today": [{"source": "arca", "clicks": 12}],
            "top_today": [
                {
                    "code": "Xy91abCD",
                    "title": "테스트 생수 2L",
                    "source": "arca",
                    "sale_price": 9900,
                    "clicks": 8,
                }
            ],
            "recent": [
                {
                    "clicked_at": "2026-11-27T07:20:00Z",
                    "code": "Xy91abCD",
                    "title": "테스트 생수 2L",
                    "source": "arca",
                    "sale_price": 9900,
                    "is_mobile": 1,
                }
            ],
        }

        async def _run():
            admin_msg = SimpleNamespace(reply_text=AsyncMock())
            admin_id = bot.ADMIN_CHAT_ID or 12345
            admin_update = SimpleNamespace(
                effective_user=SimpleNamespace(id=admin_id),
                effective_chat=SimpleNamespace(id=admin_id),
                effective_message=admin_msg,
            )
            with (
                patch.object(bot, "ADMIN_CHAT_ID", admin_id),
                patch.object(bot, "is_click_tracker_enabled", return_value=True),
                patch.object(bot, "fetch_click_stats", return_value=stats_payload),
            ):
                await bot.clicks_cmd(admin_update, SimpleNamespace())

            self.assertEqual(admin_msg.reply_text.await_count, 1)
            sent_text = admin_msg.reply_text.await_args.args[0]
            markup = admin_msg.reply_text.await_args.kwargs["reply_markup"]
            cb_data = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("실시간 클릭 현황", sent_text)
            self.assertIn("아카라이브 12회", sent_text)
            self.assertIn("16:20", sent_text)
            self.assertIn("📱 테스트 생수 2L", sent_text)
            self.assertIn("menu:clicks", cb_data)

            # 비관리자는 차단되어야 함
            non_admin_msg = SimpleNamespace(reply_text=AsyncMock())
            non_admin_update = SimpleNamespace(
                effective_user=SimpleNamespace(id=admin_id + 1),
                effective_chat=SimpleNamespace(id=admin_id + 1),
                effective_message=non_admin_msg,
            )
            with patch.object(bot, "ADMIN_CHAT_ID", admin_id):
                await bot.clicks_cmd(non_admin_update, SimpleNamespace())
            self.assertEqual(non_admin_msg.reply_text.await_count, 0)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
