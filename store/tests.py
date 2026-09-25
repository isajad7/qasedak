import json
import base64
import os
import random
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
from io import BytesIO, StringIO
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from PIL import Image

from .middleware import (
    CUSTOMER_COOKIE_NAME,
    CUSTOMER_COOKIE_SALT,
    LEGACY_CUSTOMER_COOKIE_NAME,
    LEGACY_CUSTOMER_COOKIE_SALT,
)
from .models import (
    BotConfiguration,
    BotAdminOrderMessage,
    BotEventLog,
    BotPendingAction,
    BotUser,
    BroadcastMessage,
    BroadcastRecipient,
    ConfigAllocation,
    ConfigInventoryAsset,
    ConfigInventoryPool,
    ConfigLink,
    CupFillerRule,
    CupFulfillmentRecipe,
    CupItem,
    Customer,
    DailyAdminReportLog,
    DiscountCode,
    ExternalSubscriptionFeed,
    FreeTrialRequest,
    Inbound,
    LegacyWizWizImportJob,
    LegacyWizWizImportMessageBatch,
    LegacyWizWizImportMessageRecipient,
    LegacyWizWizImportRow,
    Order,
    Operator,
    Panel,
    PanelClientUsageSnapshot,
    PanelDailyUsage,
    PanelHealthCheckLog,
    PanelHealthStatus,
    PanelUsageSnapshot,
    Plan,
    PlanDeliveryConfig,
    PlanDeliverySource,
    PlanInboundRoute,
    QasedakBackupJob,
    QasedakRestoreJob,
    Referral,
    ReferralRewardLedger,
    RevenueOfferLog,
    Store,
    SupportConversation,
    SupportMessage,
    SubscriptionCup,
    VPNClient,
    VPNClientActionLog,
    VPNClientReminderLog,
    WebTelegramLinkToken,
)
from .orchestrator_v2.models import ServerNode, TenantInstance
from .broadcast_services import (
    create_campaign_recipients,
    get_customers_for_audience,
    resolve_campaign_recipients,
    send_campaign,
)
from .legacy_wizwiz_import_services import (
    analyze_wizwiz_import_job,
    apply_wizwiz_import_job,
    create_legacy_import_message_batch,
    normalize_wizwiz_user_row,
    parse_mysql_insert_values,
    parse_wizwiz_users_from_sql_file,
    preview_legacy_import_message_batch,
    send_legacy_import_message_batch,
    wizwiz_simple_restore,
)
from .order_actions import activate_order, reject_order
from .bots import format_customer_analytics_report
from .customer_analytics import (
    PERIOD_LAST_30_DAYS,
    PERIOD_LAST_7_DAYS,
    PERIOD_TODAY,
    SEGMENT_GOOD,
    SEGMENT_INACTIVE,
    SEGMENT_LOYAL,
    SEGMENT_NO_ORDER,
    SEGMENT_TOP_BUYER,
    SEGMENT_TOP_REFERRER,
    get_customer_segment,
    get_customer_stats,
    get_customers_by_segment,
    get_period_range,
)
from .order_services import create_manual_payment_order, get_store_plans, select_inbound_for_plan, select_inbounds_for_plan
from .plan_route_services import (
    BULK_ROUTE_STRATEGY_REPLACE_ACTIVE,
    BULK_ROUTE_STRATEGY_SKIP_EXISTING,
    BULK_ROUTE_STRATEGY_UPDATE_EXISTING,
    apply_bulk_plan_routes,
    get_valid_sales_inbounds,
    preview_bulk_plan_routes,
)
from .referral_services import (
    apply_referral_code,
    build_telegram_referral_link,
    create_referral_reward_for_order,
    ensure_referral_code,
    get_available_referral_gb,
    get_referral_summary,
    redeem_referral_rewards,
)
from .receipt_analysis import analyze_receipt_text, extract_receipt_amount_candidates


class DummyBotResponse:
    def __init__(self, payload=None, content=b""):
        self.payload = payload or {"ok": True, "result": {}}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class DummyXUIResponse:
    def __init__(self, payload=None, *, status_code=200, text=""):
        self.payload = payload or {"success": True}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


class DummyPasarGuardResponse:
    def __init__(self, payload=None, *, status_code=200, text=""):
        self.payload = payload if payload is not None else {"ok": True}
        self.status_code = status_code
        self.text = text if text else json.dumps(self.payload)

    def json(self):
        return self.payload


class DummyPasarGuardSession:
    def __init__(self, responses=None, raw_responses=None):
        self.responses = list(responses or [])
        self.raw_responses = list(raw_responses or [])
        self.calls = []
        self.get_calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        return DummyPasarGuardResponse({})

    def get(self, url, **kwargs):
        self.get_calls.append({"method": "GET", "url": url, **kwargs})
        if self.raw_responses:
            return self.raw_responses.pop(0)
        return DummyPasarGuardResponse({}, text="")


def fake_client_result(uuid="11111111-1111-4111-8111-111111111111"):
    return {
        "uuid": uuid,
        "email": "bot_user_11111111",
        "sub_id": "sub123",
        "sub_link": "https://example.com/sub/sub123",
        "direct_link": "vless://example",
        "raw": {"id": uuid, "email": "bot_user_11111111"},
    }


def image_bytes(image_format="PNG"):
    output = BytesIO()
    Image.new("RGB", (1, 1), color="white").save(output, format=image_format)
    return output.getvalue()


class SubscriptionCupMVPTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            domain="vpn.example.com",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        self.customer = Customer.objects.create(username="alice", display_name="Alice")
        self.plan = Plan.objects.create(
            store=self.store,
            name="Basic",
            volume_gb=Decimal("10"),
            duration_days=30,
            price=100,
            device_limit=2,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="panel-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.MODERN_SINGLE_NODE,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Primary",
            protocol=Inbound.Protocol.VLESS,
            server_ip="vpn.example.com",
            port="443",
            config_params="{}",
            is_active=True,
        )
        self.order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            is_paid=True,
            username="alice",
            sub_link="https://panel.example.com/sub/panel-sub-token",
            direct_link=self.direct_link(1),
        )
        self.vpn_client = self.create_vpn_client(order=self.order, direct_link=self.order.direct_link)
        self.admin_user = get_user_model().objects.create_superuser(
            username="cup-admin",
            email="cup-admin@example.com",
            password="secret",
        )

    def direct_link(self, index, protocol="vless", host=None):
        host = host or f"node-{index}.example.com"
        return f"{protocol}://aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}@{host}:443#Client-{index}"

    def reality_link(self, index=301, host="reality.example.com"):
        return (
            f"vless://aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}@{host}:443"
            "?type=tcp"
            "&security=reality"
            "&pbk=PUBLICKEYVALUE"
            "&fp=chrome"
            "&sni=front.example.com"
            "&sid=abcd1234"
            "&spx=/spider"
            "&flow=xtls-rprx-vision"
            "#Reality-Client"
        )

    def create_vpn_client(self, *, order=None, direct_link=None, xui_raw=None, status=VPNClient.Status.ACTIVE):
        index = VPNClient.objects.count() + 1
        order = order or self.order
        return VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=self.inbound,
            username=f"client-{index}",
            xui_email=f"client-{index}",
            uuid=f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}",
            sub_id=f"sub-{index}",
            sub_link=f"https://panel.example.com/sub/panel-sub-token-{index}",
            direct_link=direct_link or self.direct_link(index),
            status=status,
            traffic_limit_bytes=self.plan.traffic_limit_bytes,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
            activated_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30),
            xui_raw=xui_raw or {},
        )

    def create_cup_with_links(self, *links, status=SubscriptionCup.Status.ACTIVE, expires_at=None):
        from .subscription_cups import create_config_link_from_raw

        cup = SubscriptionCup.objects.create(
            customer=self.customer,
            order=self.order,
            plan=self.plan,
            vpn_client=self.vpn_client,
            status=status,
            expires_at=expires_at,
        )
        for position, raw_link in enumerate(links, start=1):
            config_link = create_config_link_from_raw(
                raw_link,
                source_type=ConfigLink.SourceType.MANUAL,
                source_panel=self.panel,
                source_inbound=self.inbound,
                vpn_client=self.vpn_client,
            )
            CupItem.objects.create(cup=cup, config_link=config_link, position=position)
        return cup

    def create_inventory_pool_with_assets(self, title, links):
        pool = ConfigInventoryPool.objects.create(title=title, is_active=True)
        for raw_link in links:
            ConfigInventoryAsset.objects.create(
                pool=pool,
                raw_link=raw_link,
                status=ConfigInventoryAsset.Status.AVAILABLE,
            )
        return pool

    def create_pending_order(self, *, username="v2-order"):
        return Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
            amount=self.plan.price,
            original_amount=self.plan.price,
            username=username,
        )

    def test_plan_delivery_v2_direct_links_allocates_inventory_without_subscription_cup(self):
        from .provisioning_services import approve_and_provision_order

        links = [self.direct_link(401), self.direct_link(402)]
        pool = self.create_inventory_pool_with_assets("V2 direct pool", links)
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.INVENTORY_POOL,
            inventory_pool=pool,
            quantity=2,
            priority=1,
        )
        order = self.create_pending_order(username="v2-direct")

        result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.direct_link, links[0])
        self.assertEqual((order.metadata or {}).get("direct_delivery_links"), links)
        self.assertEqual(ConfigAllocation.objects.filter(order=order, status=ConfigAllocation.Status.ACTIVE).count(), 2)
        self.assertFalse(SubscriptionCup.objects.filter(order=order).exists())

    def test_plan_delivery_v2_locks_only_delivery_config_base_row(self):
        from .provisioning_services import approve_and_provision_order

        pool = self.create_inventory_pool_with_assets("V2 lock pool", [self.direct_link(451)])
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.INVENTORY_POOL,
            inventory_pool=pool,
            quantity=1,
            priority=1,
        )
        order = self.create_pending_order(username="v2-lock")

        with patch("store.plan_delivery_execution.select_for_update_self", side_effect=lambda queryset: queryset) as lock_mock:
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        lock_mock.assert_called_once()
        self.assertEqual(lock_mock.call_args.args[0].model, PlanDeliveryConfig)

    def test_plan_delivery_v2_subscription_allocates_inventory_into_one_cup(self):
        from .provisioning_services import approve_and_provision_order

        links = [self.direct_link(501), self.direct_link(502)]
        pool = self.create_inventory_pool_with_assets("V2 subscription pool", links)
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.INVENTORY_POOL,
            inventory_pool=pool,
            quantity=2,
            priority=1,
        )
        order = self.create_pending_order(username="v2-subscription")

        result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.direct_link, "")
        self.assertIn("/sub/", order.sub_link)
        cup = SubscriptionCup.objects.get(order=order, vpn_client__isnull=True)
        self.assertEqual(CupItem.objects.filter(cup=cup, is_active=True).count(), 2)
        self.assertEqual(ConfigAllocation.objects.filter(order=order, cup=cup, status=ConfigAllocation.Status.ACTIVE).count(), 2)

    def create_pasarguard_panel_and_groups(self):
        panel = Panel.objects.create(
            store=self.store,
            name="PasarGuard",
            family=Panel.Family.PASARGUARD,
            url="https://pasarguard.example.com",
            username="",
            password="pg-api-key",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.PASARGUARD_GROUPS,
        )
        groups = []
        for group_id, name in ((41, "Reality Group"), (42, "Backup Group")):
            groups.append(
                Inbound.objects.create(
                    panel=panel,
                    inbound_id=group_id,
                    remark=name,
                    protocol=Inbound.Protocol.VLESS,
                    server_ip="pasarguard-native",
                    port="0",
                    config_params="{}",
                    security=Inbound.Security.REALITY,
                    is_active=True,
                    available_for_new_orders=True,
                    xui_source=Inbound.XUISource.PASARGUARD_GROUP,
                    xui_remote_key=f"pasarguard_group:{group_id}",
                    metadata={
                        "remote_kind": "pasarguard_group",
                        "group_id": group_id,
                        "group_name": name,
                        "native_raw_delivery": True,
                    },
                )
            )
        return panel, groups

    def pasarguard_adapter(self, raw_links):
        adapter = Mock()
        adapter.family = "pasarguard"
        adapter.get_capability_report.return_value = SimpleNamespace(
            supported=True,
            supports_create_client=True,
            supports_multi_inbound_create=True,
            supports_multi_group_users=True,
            supports_subscription=True,
            supports_native_raw_configs=True,
            errors=(),
            warnings=(),
        )
        adapter.create_enabled_multi_inbound_client.return_value = {
            "email": "pg_qasedak_user",
            "uuid": "bbbbbbbb-bbbb-4bbb-8bbb-000000000610",
            "sub_id": "pg-sub-local",
            "sub_link": "https://pasarguard.example.com/s/private-token",
            "direct_link": raw_links[0],
            "raw_links": raw_links,
            "raw": {"family": "pasarguard", "native_raw_delivery": True, "raw_link_count": len(raw_links)},
        }
        return adapter

    def create_simple_pasarguard_group_source(self, *, group_id=29, verified=False, active=False, available=False):
        panel = Panel.objects.create(
            store=self.store,
            name=f"PasarGuard simple {group_id}",
            family=Panel.Family.PASARGUARD,
            url="https://pasarguard.example.com",
            username="",
            password="pg-api-key-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.PASARGUARD_GROUPS,
        )
        source = Inbound.objects.create(
            panel=panel,
            inbound_id=group_id,
            remark="Seller",
            protocol=Inbound.Protocol.VLESS,
            server_ip="pasarguard-native",
            port="0",
            config_params="{}",
            security=Inbound.Security.REALITY,
            is_active=active,
            available_for_new_orders=available,
            xui_source=Inbound.XUISource.PASARGUARD_GROUP,
            xui_remote_key=f"pasarguard_group:{group_id}",
            metadata={
                "remote_kind": "pasarguard_group",
                "group_id": group_id,
                "group_name": "Seller",
                "inbound_tags": [],
                "inbound_tag_count": 0,
                "is_disabled": None,
                "disabled_known": False,
                "inbound_tags_known": False,
                "remote_source": "groups_simple",
                "native_raw_delivery": True,
            },
        )
        if verified:
            source.verification_status = Inbound.VerificationStatus.VERIFIED_SELLABLE
            source.verified_at = timezone.now()
            source.verification_method = "provisioning_probe"
            source.last_verified_config_count = 1
            source.is_active = True
            source.available_for_new_orders = True
            source.save(
                update_fields=[
                    "verification_status",
                    "verified_at",
                    "verification_method",
                    "last_verified_config_count",
                    "is_active",
                    "available_for_new_orders",
                    "updated_at",
                ]
            )
        return panel, source

    def vmess_link(self, *, remark="VMess", host="vmess.example.com", port="443", net="ws", tls="tls"):
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "v": "2",
                    "ps": remark,
                    "add": host,
                    "port": str(port),
                    "id": "bbbbbbbb-bbbb-4bbb-8bbb-000000000777",
                    "aid": "0",
                    "net": net,
                    "type": "none",
                    "host": host,
                    "path": "/ws",
                    "tls": tls,
                }
            ).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"vmess://{payload}"

    def create_dynamic_feed_with_links(self, links, *, policy=None, panel=None, source=None, cup=None):
        from .external_subscription_sources import filter_native_configs, register_external_subscription_feed_snapshot
        from .subscription_cups import create_config_link_from_raw

        if not panel or not source:
            panel, groups = self.create_pasarguard_panel_and_groups()
            config = PlanDeliveryConfig.objects.create(
                plan=self.plan,
                delivery_mode=PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
            )
            source = PlanDeliverySource.objects.create(
                delivery_config=config,
                source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
                panel=panel,
                inbound=groups[0],
                quantity=1,
                priority=1,
                metadata={"dynamic_subscription_policy": policy or {}},
            )
        else:
            source.metadata = {"dynamic_subscription_policy": policy or {}}
            source.save(update_fields=["metadata", "updated_at"])
        cup = cup or SubscriptionCup.objects.create(customer=self.customer, order=self.order, plan=self.plan)
        filter_result = filter_native_configs(links, policy or {})
        config_links = []
        for position, parsed in enumerate(filter_result.selected_configs, start=1):
            config_link = create_config_link_from_raw(
                parsed.raw_link,
                source_type=ConfigLink.SourceType.PANEL_GENERATED,
                source_panel=panel,
                vpn_client=self.vpn_client,
            )
            CupItem.objects.create(cup=cup, config_link=config_link, position=position, is_active=True)
            config_links.append(config_link)
        feed = register_external_subscription_feed_snapshot(
            cup=cup,
            source=source,
            panel=panel,
            vpn_client=self.vpn_client,
            protected_subscription_url="https://pasarguard.example.com/s/private-feed-token",
            remote_identity_ref="pg_qasedak_user",
            raw_links=links,
            config_links=config_links,
            filter_result=filter_result,
            provider=Panel.Family.PASARGUARD,
        )
        return feed, cup, source, panel

    def refresh_adapter_for_links(self, links=None, *, error=None):
        client = SimpleNamespace(fetch_native_links=Mock(side_effect=error) if error else Mock(return_value=list(links or [])))
        return SimpleNamespace(client=client)

    def capture_bot_client(self):
        class CaptureBotClient:
            config = SimpleNamespace(provider=BotConfiguration.Provider.TELEGRAM)

            def __init__(self):
                self.messages = []

            def send_message(self, text, **kwargs):
                self.messages.append({"text": text, **kwargs})
                return object()

        return CaptureBotClient()

    def set_customer_cookie(self, customer=None):
        response = HttpResponse()
        response.set_signed_cookie(
            CUSTOMER_COOKIE_NAME,
            str((customer or self.customer).public_id),
            salt=CUSTOMER_COOKIE_SALT,
        )
        self.client.cookies[CUSTOMER_COOKIE_NAME] = response.cookies[CUSTOMER_COOKIE_NAME].value

    def ready_stats(self, client=None):
        client = client or self.vpn_client
        total = getattr(client, "traffic_limit_bytes", 0) or self.plan.traffic_limit_bytes
        return {
            "is_enabled": True,
            "is_expired": False,
            "total_traffic_bytes": total,
            "used_traffic_bytes": 0,
            "remaining_traffic_bytes": total,
            "panel_available": True,
            "expiry_at": getattr(client, "expires_at", None) or timezone.now() + timedelta(days=30),
        }

    def assert_subscription_copy_cta_is_readable(self, response):
        rendered = response.content.decode()
        match = re.search(
            r'<button\b(?=[^>]*data-copy-access-link)(?=[^>]*data-copy-success="Ù„ÛŒÙ†Ú© Ø§Ø´ØªØ±Ø§Ú© Ú©Ù¾ÛŒ Ø´Ø¯")[^>]*class="([^"]+)"',
            rendered,
            re.S,
        )
        self.assertIsNotNone(match)
        classes = match.group(1).split()
        for class_name in (
            "cursor-pointer",
            "border-sky-300/50",
            "bg-sky-50",
            "font-semibold",
            "text-sky-700",
            "hover:border-sky-400/60",
            "hover:bg-white",
            "hover:text-sky-800",
            "focus:ring-sky-300/50",
        ):
            self.assertIn(class_name, classes)
        self.assertNotIn("bg-slate-950/35", classes)
        self.assertNotIn("text-sky-50", classes)

    def test_plan_delivery_v2_pasarguard_groups_create_one_user_and_preserve_raw_direct_links(self):
        from .provisioning_services import approve_and_provision_order

        panel, groups = self.create_pasarguard_panel_and_groups()
        raw_links = [
            self.reality_link(610, host="native-reality.example.com"),
            "trojan://password@native-trojan.example.com:443#Native-Trojan",
        ]
        adapter = self.pasarguard_adapter(raw_links)
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS,
        )
        for priority, group in enumerate(groups, start=1):
            PlanDeliverySource.objects.create(
                delivery_config=config,
                source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
                panel=panel,
                inbound=group,
                quantity=1,
                priority=priority,
            )
        order = self.create_pending_order(username="v2-pg-direct")

        with patch("store.plan_delivery_execution.get_safe_panel_adapter", return_value=adapter):
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        adapter.create_enabled_multi_inbound_client.assert_called_once()
        request = adapter.create_enabled_multi_inbound_client.call_args.args[0]
        self.assertEqual([inbound.inbound_id for inbound in request.inbounds], [41, 42])
        order.refresh_from_db()
        self.assertEqual(order.direct_link, raw_links[0])
        self.assertEqual((order.metadata or {}).get("direct_delivery_links"), raw_links)
        self.assertEqual(list(ConfigLink.objects.filter(source_panel=panel).order_by("pk").values_list("raw_link", flat=True)), raw_links)
        vpn_client = VPNClient.objects.get(order=order, inbound=groups[0])
        self.assertEqual(vpn_client.direct_link, raw_links[0])
        self.assertEqual((vpn_client.xui_raw or {}).get("native_raw_delivery"), True)
        from .telegram_bot.order_delivery import order_config_link_groups

        groups_for_customer = order_config_link_groups(order)
        self.assertEqual([group["direct_link"] for group in groups_for_customer], raw_links)
        self.assertTrue(all(not group["subscription_link"] for group in groups_for_customer))

    def test_plan_delivery_v2_pasarguard_subscription_cup_uses_native_raw_links(self):
        from .plan_delivery_execution import execute_plan_delivery
        from .provisioning_services import approve_and_provision_order
        from .subscription_cups import build_subscription_cup_url
        from .telegram_bot.order_delivery import (
            approved_order_detail_lines,
            order_config_link_groups,
            order_config_links,
            send_customer_order_event_message,
        )

        panel, groups = self.create_pasarguard_panel_and_groups()
        raw_links = [
            self.direct_link(620, host="pg-native-one.example.com"),
            self.vmess_link(remark="PG VMess", host="pg-native-two.example.com"),
        ]
        adapter = self.pasarguard_adapter(raw_links)
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
        )
        for priority, group in enumerate(groups, start=1):
            PlanDeliverySource.objects.create(
                delivery_config=config,
                source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
                panel=panel,
                inbound=group,
                quantity=1,
                priority=priority,
            )
        order = self.create_pending_order(username="v2-pg-sub")

        with patch("store.plan_delivery_execution.get_safe_panel_adapter", return_value=adapter):
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        order.refresh_from_db()
        self.assertEqual(order.direct_link, "")
        self.assertIn("/sub/", order.sub_link)
        cup = SubscriptionCup.objects.get(order=order, vpn_client__isnull=True)
        qasedak_subscription_url = build_subscription_cup_url(cup, store=self.store)
        self.assertEqual(order.sub_link, qasedak_subscription_url)
        self.assertNotIn("pasarguard.example.com", order.sub_link)
        self.assertEqual(
            list(CupItem.objects.filter(cup=cup, is_active=True).order_by("position").values_list("config_link__raw_link", flat=True)),
            raw_links,
        )
        feed = ExternalSubscriptionFeed.objects.get(cup=cup)
        self.assertEqual(feed.provider, Panel.Family.PASARGUARD)
        self.assertEqual(feed.status, ExternalSubscriptionFeed.Status.HEALTHY)
        self.assertTrue(feed.protected_subscription_url)
        self.assertEqual(feed.last_seen_upstream_count, 2)
        self.assertEqual(feed.last_good_config_count, 2)
        self.assertEqual(
            ConfigLink.objects.filter(external_feed=feed, source_type=ConfigLink.SourceType.EXTERNAL_SUBSCRIPTION).count(),
            2,
        )
        groups_for_customer = order_config_link_groups(order)
        self.assertEqual(groups_for_customer, [{
            "label": "",
            "subscription_link": qasedak_subscription_url,
            "direct_link": "",
            "project_subscription_link": "",
            "project_client_link": "",
        }])
        self.assertEqual(order_config_links(order), [("Ú©Ø§Ù†ÙÛŒÚ¯ - Ù„ÛŒÙ†Ú© Ø§Ø´ØªØ±Ø§Ú©", qasedak_subscription_url)])
        self.assertIn("ØªØ¹Ø¯Ø§Ø¯ Ú©Ø§Ù†ÙÛŒÚ¯: Û²", approved_order_detail_lines(order))

        from .customer_delivery import (
            customer_delivery_link_groups_for_client,
            resolve_customer_order_delivery,
        )

        delivery = resolve_customer_order_delivery(order)
        self.assertEqual(delivery.delivery_mode, PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION)
        self.assertEqual(delivery.customer_subscription_url, qasedak_subscription_url)
        self.assertEqual(delivery.customer_direct_links, ())
        self.assertEqual(
            delivery.config_count,
            CupItem.objects.filter(cup=cup, is_active=True, config_link__is_active=True).count(),
        )
        self.assertTrue(delivery.is_ready)
        safe_payload = json.dumps(delivery.to_safe_dict(), ensure_ascii=False)
        self.assertNotIn("pasarguard.example.com", safe_payload)
        self.assertNotIn("private-token", safe_payload)
        self.assertNotIn("vless://", safe_payload)
        vpn_client = VPNClient.objects.get(order=order, inbound=groups[0])
        self.assertEqual(customer_delivery_link_groups_for_client(vpn_client)[0]["subscription_link"], qasedak_subscription_url)

        bot_client = self.capture_bot_client()
        sent = send_customer_order_event_message(
            bot_client,
            order,
            event_type="approved",
            chat_id="100",
            format_customer_order_event_func=lambda order, event_type: "approved",
        )
        rendered_message = "\n".join(message["text"] for message in bot_client.messages)
        rendered_keyboard = json.dumps([message.get("reply_markup") for message in bot_client.messages], ensure_ascii=False)
        self.assertEqual(sent, 1)
        self.assertIn(qasedak_subscription_url, rendered_message)
        self.assertIn(qasedak_subscription_url, rendered_keyboard)
        self.assertIn("ØªØ¹Ø¯Ø§Ø¯ Ú©Ø§Ù†ÙÛŒÚ¯: Û²", rendered_message)
        self.assertNotIn("pasarguard.example.com", rendered_message)
        self.assertNotIn("pasarguard.example.com", rendered_keyboard)
        self.assertNotIn("private-token", rendered_message)
        self.assertNotIn("private-token", rendered_keyboard)
        self.assertNotIn("vless://", rendered_message)
        self.assertNotIn("vless://", rendered_keyboard)
        self.assertNotIn("vmess://", rendered_message)
        self.assertNotIn("vmess://", rendered_keyboard)

        original_cup_token = cup.token
        original_customer_url = order.sub_link
        with patch("store.plan_delivery_execution.get_safe_panel_adapter", return_value=adapter):
            retry_result = execute_plan_delivery(order, config)
        order.refresh_from_db()
        cup.refresh_from_db()

        self.assertTrue(retry_result.ok)
        self.assertEqual(cup.token, original_cup_token)
        self.assertEqual(order.sub_link, original_customer_url)
        self.assertEqual(retry_result.customer_subscription_url, original_customer_url)
        self.assertEqual(retry_result.customer_config_count, 2)
        self.assertEqual(retry_result.protected_upstream_subscription_urls, ["https://pasarguard.example.com/s/private-token"])
        self.assertEqual(SubscriptionCup.objects.filter(order=order, vpn_client__isnull=True).count(), 1)

    def test_website_pasarguard_subscription_delivery_uses_qasedak_cup_only(self):
        from .provisioning_services import approve_and_provision_order
        from .subscription_cups import build_subscription_cup_url

        panel, groups = self.create_pasarguard_panel_and_groups()
        raw_links = [
            self.direct_link(621, host="pg-web-one.example.com"),
            self.vmess_link(remark="PG Web VMess", host="pg-web-two.example.com"),
        ]
        adapter = self.pasarguard_adapter(raw_links)
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
        )
        for priority, group in enumerate(groups, start=1):
            PlanDeliverySource.objects.create(
                delivery_config=config,
                source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
                panel=panel,
                inbound=group,
                quantity=1,
                priority=priority,
            )
        order = self.create_pending_order(username="v2-pg-web-sub")

        with patch("store.plan_delivery_execution.get_safe_panel_adapter", return_value=adapter):
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        order.refresh_from_db()
        cup = SubscriptionCup.objects.get(order=order, vpn_client__isnull=True)
        qasedak_subscription_url = build_subscription_cup_url(cup, store=self.store)
        self.set_customer_cookie(order.customer)

        order_response = self.client.get(reverse("order_detail", kwargs={"order_id": order.public_id}))
        self.assertContains(order_response, qasedak_subscription_url)
        self.assertContains(order_response, "Û² Ú©Ø§Ù†ÙÛŒÚ¯")
        self.assert_subscription_copy_cta_is_readable(order_response)
        self.assertInHTML(
            '<div class="rounded-2xl border border-white/10 bg-slate-950/35 px-4 py-3 text-sm font-bold text-emerald-50/70">Ù„ÛŒÙ†Ú© Ù…Ø³ØªÙ‚ÛŒÙ… Ø¬Ø¯Ø§Ú¯Ø§Ù†Ù‡ Ù†Ø¯Ø§Ø±Ø¯</div>',
            order_response.content.decode(),
        )
        self.assertNotContains(order_response, "pasarguard.example.com")
        self.assertNotContains(order_response, "private-token")
        self.assertNotContains(order_response, "vless://")
        self.assertNotContains(order_response, "vmess://")

        vpn_client = VPNClient.objects.get(order=order)
        with patch("store.views.sync_vpn_client_stats", return_value=self.ready_stats(vpn_client)):
            detail_response = self.client.get(
                reverse("config_detail", args=[order.order_tracking_code, vpn_client.public_id])
            )
        self.assertContains(detail_response, qasedak_subscription_url)
        self.assertNotContains(detail_response, "pasarguard.example.com")
        self.assertNotContains(detail_response, "private-token")
        self.assertNotContains(detail_response, "vless://")
        self.assertNotContains(detail_response, "vmess://")

        with patch("store.views.sync_vpn_client_stats", return_value=self.ready_stats(vpn_client)):
            dashboard_response = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard_response, qasedak_subscription_url)
        self.assertNotContains(dashboard_response, "pasarguard.example.com")
        self.assertNotContains(dashboard_response, "private-token")
        self.assertNotContains(dashboard_response, "vless://")
        self.assertNotContains(dashboard_response, "vmess://")

    def test_website_v2_direct_links_preserve_direct_outputs_without_subscription_leak(self):
        from .provisioning_services import approve_and_provision_order

        panel, groups = self.create_pasarguard_panel_and_groups()
        raw_links = [
            self.reality_link(622, host="pg-web-direct-one.example.com"),
            "trojan://password@pg-web-direct-two.example.com:443#PG-Web-Trojan",
        ]
        adapter = self.pasarguard_adapter(raw_links)
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS,
        )
        for priority, group in enumerate(groups, start=1):
            PlanDeliverySource.objects.create(
                delivery_config=config,
                source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
                panel=panel,
                inbound=group,
                quantity=1,
                priority=priority,
            )
        order = self.create_pending_order(username="v2-pg-web-direct")

        with patch("store.plan_delivery_execution.get_safe_panel_adapter", return_value=adapter):
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        self.set_customer_cookie(order.customer)
        response = self.client.get(reverse("order_detail", kwargs={"order_id": order.public_id}))

        self.assertContains(response, "pg-web-direct-one.example.com")
        self.assertContains(response, "pg-web-direct-two.example.com")
        self.assertNotContains(response, "pasarguard.example.com/s/private-token")
        self.assertNotContains(response, "Ú©Ù¾ÛŒ Ù„ÛŒÙ†Ú© Ø§Ø´ØªØ±Ø§Ú©")

        vpn_client = VPNClient.objects.get(order=order)
        with patch("store.views.sync_vpn_client_stats", return_value=self.ready_stats(vpn_client)):
            detail_response = self.client.get(
                reverse("config_detail", args=[order.order_tracking_code, vpn_client.public_id])
            )
        self.assertContains(detail_response, "pg-web-direct-one.example.com")
        self.assertContains(detail_response, "pg-web-direct-two.example.com")
        self.assertNotContains(detail_response, "pasarguard.example.com/s/private-token")

    def test_new_v2_subscription_missing_cup_never_falls_back_to_provider_links(self):
        from .customer_delivery import (
            customer_delivery_link_groups,
            resolve_customer_order_delivery,
        )

        upstream_subscription = "https://pasarguard.example.com/s/private-missing-cup-token"
        provider_direct = self.direct_link(623, host="pg-missing-cup-direct.example.com")
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            is_paid=True,
            username="v2-missing-cup",
            sub_link=upstream_subscription,
            direct_link=provider_direct,
            metadata={
                "plan_delivery_v2": {
                    "status": "provisioned",
                    "delivery_mode": PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
                    "config_link_count": 1,
                }
            },
        )
        vpn_client = self.create_vpn_client(order=order, direct_link=provider_direct)
        vpn_client.sub_link = upstream_subscription
        vpn_client.save(update_fields=["sub_link", "updated_at"])

        delivery = resolve_customer_order_delivery(order)

        self.assertFalse(delivery.is_ready)
        self.assertEqual(delivery.customer_subscription_url, "")
        self.assertEqual(delivery.customer_direct_links, ())
        self.assertEqual(delivery.config_count, 0)
        self.assertEqual(delivery.diagnostic, "v2_subscription_cup_missing")
        self.assertEqual(customer_delivery_link_groups(order), [])

        self.set_customer_cookie(order.customer)
        response = self.client.get(reverse("order_detail", kwargs={"order_id": order.public_id}))
        self.assertContains(response, "Ù„ÛŒÙ†Ú© Ø§ÛŒÙ† Ø³ÙØ§Ø±Ø´ Ù‡Ù†ÙˆØ² Ø¢Ù…Ø§Ø¯Ù‡ Ù†ÛŒØ³Øª")
        self.assertNotContains(response, "pasarguard.example.com")
        self.assertNotContains(response, "private-missing-cup-token")
        self.assertNotContains(response, "vless://")

        with patch("store.views.sync_vpn_client_stats", return_value=self.ready_stats(vpn_client)):
            detail_response = self.client.get(
                reverse("config_detail", args=[order.order_tracking_code, vpn_client.public_id])
            )
        self.assertNotContains(detail_response, "pasarguard.example.com")
        self.assertNotContains(detail_response, "private-missing-cup-token")
        self.assertNotContains(detail_response, "vless://")

    def test_legacy_website_delivery_keeps_existing_links_explicitly(self):
        from .customer_delivery import resolve_customer_order_delivery

        delivery = resolve_customer_order_delivery(self.order)

        self.assertTrue(delivery.legacy)
        self.assertEqual(delivery.customer_subscription_url, self.order.sub_link)
        self.assertEqual(delivery.customer_direct_links, (self.order.direct_link,))
        self.set_customer_cookie(self.order.customer)
        response = self.client.get(reverse("order_detail", kwargs={"order_id": self.order.public_id}))

        self.assertContains(response, self.vpn_client.sub_link)
        self.assertContains(response, "node-1.example.com")
        self.assert_subscription_copy_cta_is_readable(response)

    def test_dynamic_subscription_refresh_keeps_customer_cup_url_and_updates_count(self):
        from .customer_delivery import resolve_customer_order_delivery
        from .external_subscription_sources import refresh_external_subscription_feed
        from .provisioning_services import approve_and_provision_order
        from .subscription_cups import build_subscription_cup_url

        panel, groups = self.create_pasarguard_panel_and_groups()
        initial_links = [self.direct_link(624, host="pg-dynamic-initial.example.com")]
        adapter = self.pasarguard_adapter(initial_links)
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
            panel=panel,
            inbound=groups[0],
            quantity=1,
            priority=1,
            metadata={"dynamic_subscription_policy": {"max_configs": 3}},
        )
        order = self.create_pending_order(username="v2-pg-dynamic-web")

        with patch("store.plan_delivery_execution.get_safe_panel_adapter", return_value=adapter):
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        order.refresh_from_db()
        cup = SubscriptionCup.objects.get(order=order, vpn_client__isnull=True)
        original_url = build_subscription_cup_url(cup, store=self.store)
        feed = ExternalSubscriptionFeed.objects.get(cup=cup)

        replacement_links = [
            self.direct_link(625, host="pg-dynamic-refresh-one.example.com"),
            self.direct_link(626, host="pg-dynamic-refresh-two.example.com"),
        ]
        summary = refresh_external_subscription_feed(
            feed.pk,
            force=True,
            candidate_raw_links=replacement_links,
        )
        order.refresh_from_db()
        cup.refresh_from_db()
        delivery = resolve_customer_order_delivery(order)

        self.assertTrue(summary.ok)
        self.assertEqual(order.sub_link, original_url)
        self.assertEqual(delivery.customer_subscription_url, original_url)
        self.assertEqual(delivery.config_count, 2)
        self.assertEqual(
            list(CupItem.objects.filter(cup=cup, is_active=True).values_list("config_link__raw_link", flat=True)),
            replacement_links,
        )

    def test_dynamic_native_parser_preserves_raw_and_reads_reality_metadata(self):
        from .external_subscription_sources import parse_native_config

        raw_link = self.reality_link(650, host="pg-parser.example.com")

        parsed = parse_native_config(raw_link)

        self.assertEqual(parsed.raw_link, raw_link)
        self.assertTrue(parsed.valid)
        self.assertEqual(parsed.protocol, ConfigLink.Protocol.VLESS)
        self.assertEqual(parsed.security, "reality")
        self.assertEqual(parsed.transport, "tcp")
        self.assertEqual(parsed.host, "pg-parser.example.com")
        self.assertEqual(parsed.port, 443)
        self.assertTrue(parsed.has_pbk)
        self.assertTrue(parsed.semantic_fingerprint)

    def test_dynamic_native_parser_rejects_reality_without_pbk(self):
        from .external_subscription_sources import parse_native_config

        raw_link = (
            "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000651@pg-parser.example.com:443"
            "?security=reality&type=tcp&sni=front.example.com#MissingPBK"
        )

        parsed = parse_native_config(raw_link)

        self.assertEqual(parsed.raw_link, raw_link)
        self.assertFalse(parsed.valid)
        self.assertEqual(parsed.validation_reason, "reality_missing_pbk")

    def test_dynamic_native_parser_reads_vmess_without_reconstruction(self):
        from .external_subscription_sources import parse_native_config

        raw_link = self.vmess_link(remark="VMess Meta", host="vmess-meta.example.com", net="grpc", tls="tls")

        parsed = parse_native_config(raw_link)

        self.assertEqual(parsed.raw_link, raw_link)
        self.assertTrue(parsed.valid)
        self.assertEqual(parsed.protocol, ConfigLink.Protocol.VMESS)
        self.assertEqual(parsed.security, "tls")
        self.assertEqual(parsed.transport, "grpc")
        self.assertEqual(parsed.remark, "VMess Meta")
        self.assertEqual(parsed.host, "vmess-meta.example.com")

    def test_sellability_verification_service_persists_success_without_raw_configs(self):
        from .source_sellability import verify_panel_source_sellability

        panel, source = self.create_simple_pasarguard_group_source(group_id=29)
        raw_link = self.reality_link(660, host="verified-source.example.com")
        adapter = SimpleNamespace(
            family="pasarguard",
            supports_sellability_probe=True,
            probe_source_sellability=Mock(
                return_value={
                    "ok": True,
                    "observed_config_count": 1,
                    "protocol_counts": {"vless": 1},
                    "reality_count": 1,
                    "pbk_validation_ok": True,
                    "cleanup_succeeded": True,
                    "safe_details": {
                        "subscription_url": "https://pasarguard.example.com/s/privateProbeToken123456",
                        "sample_config": raw_link,
                    },
                }
            ),
        )

        result = verify_panel_source_sellability(source.pk, adapter_factory=lambda _panel: adapter)

        self.assertTrue(result.ok)
        source.refresh_from_db()
        self.assertEqual(source.verification_status, Inbound.VerificationStatus.VERIFIED_SELLABLE)
        self.assertTrue(source.is_active)
        self.assertTrue(source.available_for_new_orders)
        self.assertIsNotNone(source.verified_at)
        self.assertEqual(source.last_verified_config_count, 1)
        self.assertEqual(source.last_verification_error_code, "")
        rendered_source = json.dumps(source.metadata, ensure_ascii=False)
        rendered_audit = json.dumps(BotEventLog.objects.latest("pk").raw_payload, ensure_ascii=False)
        self.assertNotIn("privateProbeToken123456", rendered_source)
        self.assertNotIn("privateProbeToken123456", rendered_audit)
        self.assertNotIn("vless://", rendered_source)
        self.assertNotIn("vless://", rendered_audit)

    def test_sellability_verification_cleanup_failure_persists_failed_not_sellable(self):
        from .source_sellability import verify_panel_source_sellability

        _panel, source = self.create_simple_pasarguard_group_source(
            group_id=29,
            verified=True,
            active=True,
            available=True,
        )
        previous_attempt = source.verification_attempted_at
        adapter = SimpleNamespace(
            family="pasarguard",
            supports_sellability_probe=True,
            probe_source_sellability=Mock(
                return_value={
                    "ok": True,
                    "observed_config_count": 1,
                    "protocol_counts": {"vless": 1},
                    "reality_count": 1,
                    "pbk_validation_ok": True,
                    "cleanup_succeeded": False,
                }
            ),
        )

        result = verify_panel_source_sellability(source.pk, adapter_factory=lambda _panel: adapter)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "cleanup_failed")
        source.refresh_from_db()
        self.assertEqual(source.verification_status, Inbound.VerificationStatus.VERIFICATION_FAILED)
        self.assertFalse(source.is_active)
        self.assertFalse(source.available_for_new_orders)
        self.assertIsNone(source.verified_at)
        self.assertEqual(source.last_verified_config_count, 0)
        self.assertEqual(source.last_verification_error_code, "cleanup_failed")
        self.assertNotEqual(source.verification_attempted_at, previous_attempt)

    def test_unverified_simple_source_is_listed_for_verification_but_blocked_for_new_sales(self):
        from django.http import QueryDict

        from .admin_catalog import CatalogPlanForm
        from .plan_route_services import get_valid_sales_inbounds, sales_inbound_issues

        _panel, source = self.create_simple_pasarguard_group_source(group_id=29, active=True, available=True)

        self.assertTrue(source.requires_sellability_verification)
        self.assertNotIn(source, list(get_valid_sales_inbounds(self.store)))
        self.assertIn(self.inbound, list(get_valid_sales_inbounds(self.store)))
        errors, warnings = sales_inbound_issues(source, store=self.store)
        self.assertTrue(errors)
        self.assertIn(
            "PasarGuard route uses native raw subscription delivery; local direct link reconstruction is bypassed.",
            warnings,
        )
        form = CatalogPlanForm(store=self.store, plan=self.plan)
        option = next(item for item in form.source_inbound_options if item["value"] == str(source.pk))
        self.assertEqual(option["verification_state"], "UNVERIFIED")
        self.assertTrue(option["verification_blocking"])

        data = QueryDict("", mutable=True)
        data.update(
            {
                "name": self.plan.name,
                "volume_gb": str(self.plan.volume_gb),
                "duration_days": str(self.plan.duration_days),
                "price": str(self.plan.price),
                "currency": self.plan.currency,
                "device_limit": str(self.plan.device_limit),
                "sort_order": str(self.plan.sort_order),
                "delivery_mode": PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
                "failure_policy": PlanDeliveryConfig.FailurePolicy.STRICT,
                "source-TOTAL_FORMS": "1",
                "source-0-source_type": PlanDeliverySource.SourceType.PANEL_INBOUND,
                "source-0-inbound": str(source.pk),
                "source-0-label": "Unverified PG",
                "source-0-quantity": "1",
                "source-0-priority": "10",
                "source-0-required": "on",
            }
        )
        bound_form = CatalogPlanForm(data, store=self.store, plan=self.plan)

        self.assertFalse(bound_form.is_valid())
        self.assertIn("unverified", " ".join(str(error) for error in bound_form.source_errors).lower())
        with self.assertRaises(ValidationError):
            PlanInboundRoute(plan=self.plan, inbound=source, is_active=True).full_clean()

    def test_verified_stale_simple_source_remains_selectable_with_warning(self):
        from .plan_route_services import get_valid_sales_inbounds, sales_inbound_issues
        from .source_sellability import source_sellability_is_stale, source_verification_ui_state

        _panel, source = self.create_simple_pasarguard_group_source(
            group_id=29,
            verified=True,
            active=True,
            available=True,
        )
        source.verified_at = timezone.now() - timedelta(days=8)
        source.save(update_fields=["verified_at", "updated_at"])

        self.assertTrue(source_sellability_is_stale(source))
        self.assertIn(source, list(get_valid_sales_inbounds(self.store)))
        errors, warnings = sales_inbound_issues(source, store=self.store)
        self.assertEqual(errors, [])
        self.assertIn("Sellability verification is older than 7 days.", warnings)
        self.assertFalse(source_verification_ui_state(source)["blocking"])
        PlanInboundRoute(plan=self.plan, inbound=source, is_active=True).full_clean()

    def test_dynamic_filter_protocol_security_transport_and_remark(self):
        from .external_subscription_sources import filter_native_configs

        kept = self.reality_link(652, host="keep.example.com").replace("#Reality-Client", "#VIP-Reality")
        wrong_protocol = "trojan://password@trojan.example.com:443#VIP-Trojan"
        wrong_remark = self.reality_link(653, host="drop-remark.example.com").replace("#Reality-Client", "#Basic-Reality")
        wrong_transport = self.reality_link(654, host="drop-ws.example.com").replace("type=tcp", "type=ws").replace("#Reality-Client", "#VIP-WS")
        policy = {
            "protocols": ["vless"],
            "security": ["reality"],
            "transport": ["tcp"],
            "remark_include": "VIP",
            "remark_exclude": "Blocked",
        }

        result = filter_native_configs([kept, wrong_protocol, wrong_remark, wrong_transport], policy)

        self.assertEqual(result.upstream_count, 4)
        self.assertEqual(result.filtered_count, 1)
        self.assertEqual(result.selected_count, 1)
        self.assertEqual(result.selected_configs[0].raw_link, kept)

    def test_dynamic_filter_max_configs_is_deterministic_and_exact_dedupes_only(self):
        from .external_subscription_sources import filter_native_configs

        first = self.direct_link(655, host="dedupe.example.com")
        duplicate = first
        same_metadata_different_raw = self.direct_link(655, host="dedupe.example.com") + "&extra=1"
        second = self.direct_link(656, host="dedupe-two.example.com")
        policy = {"max_configs": 2, "deduplicate_exact": True}

        first_result = filter_native_configs([first, duplicate, same_metadata_different_raw, second], policy)
        second_result = filter_native_configs([first, duplicate, same_metadata_different_raw, second], policy)

        self.assertEqual(first_result.deduplicated_count, 1)
        self.assertEqual(
            [item.raw_link for item in first_result.selected_configs],
            [first, same_metadata_different_raw],
        )
        self.assertEqual(
            [item.raw_link for item in second_result.selected_configs],
            [first, same_metadata_different_raw],
        )

    def test_dynamic_feed_refresh_updates_only_source_owned_items(self):
        from .external_subscription_sources import refresh_external_subscription_feed
        from .subscription_cups import create_config_link_from_raw

        old_link = self.direct_link(657, host="old-feed.example.com")
        new_link = self.direct_link(658, host="new-feed.example.com")
        inventory_link = self.direct_link(659, host="inventory-stays.example.com")
        feed, cup, _source, _panel = self.create_dynamic_feed_with_links([old_link])
        inventory_config = create_config_link_from_raw(
            inventory_link,
            source_type=ConfigLink.SourceType.IMPORTED_SUBSCRIPTION,
        )
        CupItem.objects.create(cup=cup, config_link=inventory_config, position=20, is_active=True)

        adapter = self.refresh_adapter_for_links([new_link])
        summary = refresh_external_subscription_feed(feed.pk, adapter_factory=lambda panel: adapter)

        self.assertTrue(summary.ok)
        active_links = list(
            CupItem.objects.filter(cup=cup, is_active=True)
            .order_by("position")
            .values_list("config_link__raw_link", flat=True)
        )
        self.assertIn(new_link, active_links)
        self.assertIn(inventory_link, active_links)
        self.assertNotIn(old_link, active_links)
        self.assertTrue(ConfigLink.objects.filter(raw_link=inventory_link, external_feed__isnull=True, is_active=True).exists())

    def test_dynamic_feed_failed_fetch_keeps_last_known_good(self):
        from .external_subscription_sources import ExternalSubscriptionRefreshError, refresh_external_subscription_feed

        good_link = self.direct_link(660, host="lkg.example.com")
        feed, cup, _source, _panel = self.create_dynamic_feed_with_links([good_link])
        adapter = self.refresh_adapter_for_links(error=ExternalSubscriptionRefreshError("timeout https://secret.example.com/sub/token", code="network_timeout"))

        summary = refresh_external_subscription_feed(feed.pk, adapter_factory=lambda panel: adapter)

        feed.refresh_from_db()
        self.assertFalse(summary.ok)
        self.assertTrue(summary.kept_last_good)
        self.assertEqual(feed.status, ExternalSubscriptionFeed.Status.DEGRADED)
        self.assertEqual(feed.consecutive_failures, 1)
        self.assertEqual(
            list(CupItem.objects.filter(cup=cup, is_active=True).values_list("config_link__raw_link", flat=True)),
            [good_link],
        )
        self.assertNotIn("secret.example.com", json.dumps(summary.to_safe_dict()))
        self.assertNotIn("token", json.dumps(summary.to_safe_dict()).lower())

    def test_dynamic_feed_empty_fetch_and_filter_zero_keep_last_known_good(self):
        from .external_subscription_sources import refresh_external_subscription_feed

        good_link = self.direct_link(661, host="empty-lkg.example.com")
        feed, cup, _source, _panel = self.create_dynamic_feed_with_links([good_link], policy={"protocols": ["vless"]})

        empty_summary = refresh_external_subscription_feed(
            feed.pk,
            adapter_factory=lambda panel: self.refresh_adapter_for_links([]),
        )
        zero_filter_summary = refresh_external_subscription_feed(
            feed.pk,
            force=True,
            candidate_raw_links=["trojan://password@excluded.example.com:443#Excluded"],
        )

        feed.refresh_from_db()
        self.assertEqual(empty_summary.error_code, "upstream_empty")
        self.assertEqual(zero_filter_summary.error_code, "filter_result_empty")
        self.assertEqual(feed.status, ExternalSubscriptionFeed.Status.ERROR)
        self.assertEqual(
            list(CupItem.objects.filter(cup=cup, is_active=True).values_list("config_link__raw_link", flat=True)),
            [good_link],
        )

    def test_dynamic_feed_suspicious_drop_keeps_last_known_good(self):
        from .external_subscription_sources import refresh_external_subscription_feed

        initial_links = [self.direct_link(670 + index, host=f"drop-{index}.example.com") for index in range(10)]
        feed, cup, _source, _panel = self.create_dynamic_feed_with_links(initial_links)
        replacement = [self.direct_link(690, host="too-small.example.com")]

        summary = refresh_external_subscription_feed(feed.pk, candidate_raw_links=replacement)

        feed.refresh_from_db()
        self.assertFalse(summary.ok)
        self.assertEqual(summary.error_code, "suspicious_config_drop")
        self.assertEqual(feed.status, ExternalSubscriptionFeed.Status.DEGRADED)
        self.assertEqual(
            list(
                CupItem.objects.filter(cup=cup, is_active=True)
                .order_by("position")
                .values_list("config_link__raw_link", flat=True)
            ),
            initial_links,
        )

    def test_dynamic_feed_successful_refresh_replaces_owned_items_and_recovery_resets_health(self):
        from .external_subscription_sources import refresh_external_subscription_feed

        first = self.direct_link(691, host="first.example.com")
        second = self.direct_link(692, host="second.example.com")
        feed, cup, _source, _panel = self.create_dynamic_feed_with_links([first])
        original_cup_token = cup.token
        refresh_external_subscription_feed(feed.pk, candidate_raw_links=[])
        feed.refresh_from_db()
        self.assertEqual(feed.consecutive_failures, 1)

        summary = refresh_external_subscription_feed(feed.pk, force=True, candidate_raw_links=[second])

        cup.refresh_from_db()
        feed.refresh_from_db()
        self.assertTrue(summary.ok)
        self.assertEqual(cup.token, original_cup_token)
        self.assertEqual(feed.status, ExternalSubscriptionFeed.Status.HEALTHY)
        self.assertEqual(feed.consecutive_failures, 0)
        self.assertEqual(feed.last_good_config_count, 1)
        self.assertEqual(
            list(CupItem.objects.filter(cup=cup, is_active=True).values_list("config_link__raw_link", flat=True)),
            [second],
        )

    def test_dynamic_feed_refresh_idempotency_reuses_existing_config_link(self):
        from .external_subscription_sources import refresh_external_subscription_feed

        link = self.direct_link(693, host="idempotent.example.com")
        feed, cup, _source, _panel = self.create_dynamic_feed_with_links([link])
        before_link_ids = list(ConfigLink.objects.filter(external_feed=feed).values_list("pk", flat=True))

        first = refresh_external_subscription_feed(feed.pk, candidate_raw_links=[link])
        second = refresh_external_subscription_feed(feed.pk, candidate_raw_links=[link])

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(list(ConfigLink.objects.filter(external_feed=feed).values_list("pk", flat=True)), before_link_ids)
        self.assertEqual(CupItem.objects.filter(cup=cup, config_link__external_feed=feed).count(), 1)

    def test_dynamic_feed_dry_run_does_not_mutate_cup(self):
        from .external_subscription_sources import refresh_external_subscription_feed

        old_link = self.direct_link(694, host="dry-old.example.com")
        new_link = self.direct_link(695, host="dry-new.example.com")
        feed, cup, _source, _panel = self.create_dynamic_feed_with_links([old_link])

        summary = refresh_external_subscription_feed(feed.pk, dry_run=True, candidate_raw_links=[new_link])

        self.assertTrue(summary.ok)
        self.assertTrue(summary.dry_run)
        self.assertEqual(
            list(CupItem.objects.filter(cup=cup, is_active=True).values_list("config_link__raw_link", flat=True)),
            [old_link],
        )

    def test_dynamic_source_management_command_outputs_safe_summary_only(self):
        from .external_subscription_sources import refresh_external_subscription_feed

        link = self.direct_link(696, host="safe-command.example.com")
        feed, _cup, _source, _panel = self.create_dynamic_feed_with_links([link])
        output = StringIO()

        with patch(
            "store.external_subscription_sources.PasarGuardSubscriptionFetcher.fetch_native_links",
            return_value=[link],
        ):
            call_command("refresh_external_subscription_feeds", "--feed-id", str(feed.pk), "--force", "--dry-run", stdout=output)

        rendered = output.getvalue()
        self.assertIn(f"feed_id={feed.pk}", rendered)
        self.assertIn("selected=1", rendered)
        self.assertNotIn("vless://", rendered)
        self.assertNotIn("private-feed-token", rendered)

    def test_dynamic_source_admin_changelist_masks_upstream_url_and_raw_configs(self):
        link = self.direct_link(697, host="admin-secret.example.com")
        feed, _cup, _source, _panel = self.create_dynamic_feed_with_links([link])
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin:store_externalsubscriptionfeed_changelist"))
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn(str(feed.pk), body)
        self.assertNotIn("pg_qasedak_user", body)
        self.assertIn("pg_q...user", body)
        self.assertNotIn("private-feed-token", body)
        self.assertNotIn("vless://", body)
        self.assertNotIn("admin-secret.example.com", body)

    def test_catalog_plan_editor_saves_dynamic_subscription_policy_on_pasarguard_source(self):
        from django.http import QueryDict
        from .admin_catalog import CatalogPlanForm

        panel, groups = self.create_pasarguard_panel_and_groups()
        data = QueryDict("", mutable=True)
        data.update(
            {
                "name": self.plan.name,
                "volume_gb": str(self.plan.volume_gb),
                "duration_days": str(self.plan.duration_days),
                "price": str(self.plan.price),
                "currency": self.plan.currency,
                "device_limit": str(self.plan.device_limit),
                "sort_order": str(self.plan.sort_order),
                "delivery_mode": PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
                "failure_policy": PlanDeliveryConfig.FailurePolicy.STRICT,
                "source-TOTAL_FORMS": "1",
                "source-0-source_type": PlanDeliverySource.SourceType.PANEL_INBOUND,
                "source-0-inbound": str(groups[0].pk),
                "source-0-label": "Dynamic PG",
                "source-0-quantity": "1",
                "source-0-priority": "10",
                "source-0-required": "on",
                "source-0-dynamic_require_reality_pbk": "on",
                "source-0-dynamic_deduplicate_exact": "on",
                "source-0-dynamic_remark_include": "VIP",
                "source-0-dynamic_max_configs": "3",
                "source-0-dynamic_refresh_interval_hours": "6",
            }
        )
        data.setlist("source-0-dynamic_protocols", ["vless", "vmess"])
        data.setlist("source-0-dynamic_security", ["reality", "tls"])
        data.setlist("source-0-dynamic_transport", ["tcp", "grpc"])

        form = CatalogPlanForm(data, store=self.store, plan=self.plan)
        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()

        source = PlanDeliverySource.objects.get(delivery_config__plan=self.plan, inbound=groups[0])
        policy = source.metadata["dynamic_subscription_policy"]
        self.assertEqual(policy["protocols"], ["vless", "vmess"])
        self.assertEqual(policy["security"], ["reality", "tls"])
        self.assertEqual(policy["transport"], ["tcp", "grpc"])
        self.assertEqual(policy["remark_include"], "VIP")
        self.assertEqual(policy["max_configs"], 3)
        self.assertEqual(policy["refresh_interval_hours"], 6)

    def test_plan_delivery_v2_pasarguard_and_inventory_subscription_hybrid_preserves_all_links(self):
        from .customer_delivery import resolve_customer_order_delivery
        from .provisioning_services import approve_and_provision_order
        from .subscription_cups import build_subscription_cup_url
        from .telegram_bot.order_delivery import approved_order_detail_lines, order_config_link_groups

        panel, groups = self.create_pasarguard_panel_and_groups()
        pg_links = [self.reality_link(630, host="pg-hybrid.example.com")]
        inventory_links = [self.direct_link(631, host="inventory-hybrid.example.com")]
        adapter = self.pasarguard_adapter(pg_links)
        pool = self.create_inventory_pool_with_assets("PG inventory hybrid", inventory_links)
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
            panel=panel,
            inbound=groups[0],
            quantity=1,
            priority=1,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.INVENTORY_POOL,
            inventory_pool=pool,
            quantity=1,
            priority=2,
        )
        order = self.create_pending_order(username="v2-pg-inventory-hybrid")

        with patch("store.plan_delivery_execution.get_safe_panel_adapter", return_value=adapter):
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        order.refresh_from_db()
        self.assertEqual(order.direct_link, "")
        self.assertIn("/sub/", order.sub_link)
        cup = SubscriptionCup.objects.get(order=order, vpn_client__isnull=True)
        self.assertEqual(
            list(CupItem.objects.filter(cup=cup, is_active=True).order_by("position").values_list("config_link__raw_link", flat=True)),
            pg_links + inventory_links,
        )
        self.assertEqual(ConfigAllocation.objects.filter(order=order, cup=cup, status=ConfigAllocation.Status.ACTIVE).count(), 1)
        self.assertIn("ØªØ¹Ø¯Ø§Ø¯ Ú©Ø§Ù†ÙÛŒÚ¯: Û²", approved_order_detail_lines(order))
        self.assertEqual(resolve_customer_order_delivery(order).config_count, 2)
        self.assertEqual(order_config_link_groups(order)[0]["subscription_link"], build_subscription_cup_url(cup, store=self.store))
        self.assertEqual(order_config_link_groups(order)[0]["direct_link"], "")

    def test_plan_delivery_v2_xui_subscription_customer_delivery_uses_cup_url(self):
        from .provisioning_services import approve_and_provision_order
        from .subscription_cups import build_subscription_cup_url
        from .telegram_bot.order_delivery import order_config_link_groups, send_customer_order_event_message

        xui_link = self.direct_link(632, host="xui-subscription.example.com")
        xui_result = {
            "email": "xui_subscription_user",
            "uuid": "bbbbbbbb-bbbb-4bbb-8bbb-000000000632",
            "sub_id": "xui-provider-sub",
            "sub_link": "https://panel.example.com/sub/xui-provider-sub",
            "direct_link": xui_link,
            "raw": {"family": "xui"},
        }
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.SUBSCRIPTION,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
            panel=self.panel,
            inbound=self.inbound,
            quantity=1,
            priority=1,
        )
        order = self.create_pending_order(username="v2-xui-sub")

        with patch(
            "store.provisioning_services.lookup_existing_remote_client",
            side_effect=[None, xui_result],
        ), patch(
            "store.provisioning_services.create_enabled_client_details",
            return_value=xui_result,
        ):
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        order.refresh_from_db()
        cup = SubscriptionCup.objects.get(order=order, vpn_client__isnull=True)
        qasedak_subscription_url = build_subscription_cup_url(cup, store=self.store)
        self.assertEqual(order.sub_link, qasedak_subscription_url)
        self.assertEqual(order.direct_link, "")
        self.assertEqual(order_config_link_groups(order)[0]["subscription_link"], qasedak_subscription_url)
        self.assertEqual(order_config_link_groups(order)[0]["direct_link"], "")

        bot_client = self.capture_bot_client()
        send_customer_order_event_message(
            bot_client,
            order,
            event_type="approved",
            chat_id="100",
            format_customer_order_event_func=lambda order, event_type: "approved",
        )
        rendered = "\n".join(message["text"] for message in bot_client.messages)
        self.assertIn(qasedak_subscription_url, rendered)
        self.assertIn("ØªØ¹Ø¯Ø§Ø¯ Ú©Ø§Ù†ÙÛŒÚ¯: Û±", rendered)
        self.assertNotIn("panel.example.com/sub/xui-provider-sub", rendered)
        self.assertNotIn("vless://", rendered)

        self.set_customer_cookie(order.customer)
        response = self.client.get(reverse("order_detail", kwargs={"order_id": order.public_id}))
        self.assertContains(response, qasedak_subscription_url)
        self.assertNotContains(response, "panel.example.com/sub/xui-provider-sub")
        self.assertNotContains(response, "vless://")

    def test_plan_delivery_v2_xui_and_pasarguard_direct_hybrid_keeps_both_panel_outputs(self):
        from .provisioning_services import approve_and_provision_order

        panel, groups = self.create_pasarguard_panel_and_groups()
        xui_link = self.direct_link(640, host="xui-hybrid.example.com")
        pg_links = [self.reality_link(641, host="pg-xui-hybrid.example.com")]
        adapter = self.pasarguard_adapter(pg_links)
        xui_result = {
            "email": "xui_hybrid_user",
            "uuid": "bbbbbbbb-bbbb-4bbb-8bbb-000000000640",
            "sub_id": "xui-hybrid-sub",
            "sub_link": "https://panel.example.com/sub/xui-hybrid-sub",
            "direct_link": xui_link,
            "raw": {"family": "xui"},
        }
        config = PlanDeliveryConfig.objects.create(
            plan=self.plan,
            delivery_mode=PlanDeliveryConfig.DeliveryMode.DIRECT_LINKS,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
            panel=self.panel,
            inbound=self.inbound,
            quantity=1,
            priority=1,
        )
        PlanDeliverySource.objects.create(
            delivery_config=config,
            source_type=PlanDeliverySource.SourceType.PANEL_INBOUND,
            panel=panel,
            inbound=groups[0],
            quantity=1,
            priority=2,
        )
        order = self.create_pending_order(username="v2-xui-pg-hybrid")

        with patch("store.plan_delivery_execution.get_safe_panel_adapter", return_value=adapter), patch(
            "store.provisioning_services.lookup_existing_remote_client",
            side_effect=[None, xui_result],
        ) as lookup_remote, patch(
            "store.provisioning_services.create_enabled_client_details",
            return_value=xui_result,
        ) as create_remote:
            result = approve_and_provision_order(order, notify=False)

        self.assertTrue(result.ok)
        create_remote.assert_called_once()
        self.assertEqual(lookup_remote.call_count, 2)
        adapter.create_enabled_multi_inbound_client.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.direct_link, xui_link)
        self.assertEqual((order.metadata or {}).get("direct_delivery_links"), [xui_link] + pg_links)
        self.assertEqual(
            list(ConfigLink.objects.filter(source_panel__in=[self.panel, panel]).order_by("pk").values_list("raw_link", flat=True)),
            [xui_link] + pg_links,
        )

    def browser_headers(self):
        return {
            "HTTP_ACCEPT": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "HTTP_USER_AGENT": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        }

    def create_staff_user_with_perms(self, *codenames):
        user = get_user_model().objects.create_user(
            username=f"cup-staff-{get_user_model().objects.count()}",
            password="secret",
            is_staff=True,
        )
        permissions = Permission.objects.filter(content_type__app_label="store", codename__in=codenames)
        user.user_permissions.add(*permissions)
        return user

    def quick_builder_adapter(self, *, multi=False, direct_link=None, bundle_results=None, fail_single=False):
        adapter = Mock()
        adapter.get_capability_report.return_value = SimpleNamespace(
            supported=True,
            supports_create_client=True,
            supports_multi_inbound_create=multi,
            errors=(),
            warnings=(),
        )
        if fail_single:
            adapter.create_enabled_client.side_effect = Exception("panel timeout")
        else:
            adapter.create_enabled_client.return_value = {
                "email": "quick-client@example.test",
                "direct_link": direct_link or self.direct_link(90),
            }
        adapter.create_enabled_multi_inbound_client.return_value = {
            "email": "quick-client@example.test",
            "bundle_inbound_results": bundle_results or [{"direct_link": direct_link or self.direct_link(91), "email": "quick-client@example.test"}],
        }
        return adapter

    def test_config_link_parser_detects_supported_protocols_and_unknown(self):
        from .subscription_cups import parse_config_link

        vmess_payload = base64.b64encode(
            json.dumps({"ps": "VMess", "add": "vmess.example.com", "port": "443"}).encode("utf-8")
        ).decode("ascii")
        cases = {
            "vless": "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000001@vless.example.com:443#VLESS",
            "vmess": f"vmess://{vmess_payload}",
            "trojan": "trojan://password@trojan.example.com:443#Trojan",
            "ss": "ss://method:password@ss.example.com:8388#SS",
            "hysteria2": "hysteria2://password@hy.example.com:443#HY2",
            "hy2": "hy2://password@hy2.example.com:443#HY2",
            "tuic": "tuic://uuid:password@tuic.example.com:443#TUIC",
            "ssr": "ssr://server.example.com:443:origin:aes-128-gcm:plain:password#SSR",
        }

        for protocol, raw_link in cases.items():
            with self.subTest(protocol=protocol):
                parsed = parse_config_link(raw_link)
                self.assertEqual(parsed.protocol, protocol)
                self.assertTrue(parsed.normalized_hash)

        parsed = parse_config_link("not-a-config-link")
        self.assertEqual(parsed.protocol, ConfigLink.Protocol.UNKNOWN)
        self.assertEqual(parsed.raw_link, "not-a-config-link")

    def test_reality_raw_link_is_preserved_and_parser_only_sets_metadata(self):
        from .subscription_cups import create_config_link_from_raw, parse_config_link

        raw_link = self.reality_link()
        parsed = parse_config_link(raw_link)
        config_link = create_config_link_from_raw(raw_link, source_type=ConfigLink.SourceType.MANUAL)
        cup = SubscriptionCup.objects.create(customer=self.customer, order=self.order, plan=self.plan)
        CupItem.objects.create(cup=cup, config_link=config_link, position=1)

        self.assertEqual(parsed.raw_link, raw_link)
        self.assertEqual(config_link.raw_link, raw_link)
        self.assertEqual(config_link.normalized_link, raw_link)
        self.assertEqual(config_link.protocol, ConfigLink.Protocol.VLESS)
        for required in ("security=reality", "pbk=", "fp=chrome", "sni=", "sid=", "spx=", "flow=", "type=tcp"):
            self.assertIn(required, config_link.raw_link)

        encoded = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "base64"})
        self.assertEqual(base64.b64decode(encoded.content).decode("utf-8"), f"{raw_link}\n")

    def test_xui_build_direct_link_keeps_reality_required_params(self):
        from urllib.parse import parse_qs, urlsplit

        from .xui_api import XUIService

        client_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-000000000399"
        inbound_data = {
            "protocol": "vless",
            "port": "443",
            "streamSettings": json.dumps(
                {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "settings": {"publicKey": "PUBLICKEYVALUE"},
                        "fingerprint": "chrome",
                        "serverNames": ["front.example.com"],
                        "shortIds": ["abcd1234"],
                        "spiderX": "/spider",
                    },
                }
            ),
        }

        link = XUIService(self.panel).build_direct_link(
            inbound=self.inbound,
            inbound_data=inbound_data,
            client_uuid=client_uuid,
            client_data={"id": client_uuid, "flow": "xtls-rprx-vision", "email": "Reality-Client"},
            email="Reality-Client",
            hosts=[],
        )
        query = parse_qs(urlsplit(link).query)

        self.assertTrue(link.startswith(f"vless://{client_uuid}@vpn.example.com:443?"))
        self.assertEqual(query["security"], ["reality"])
        self.assertEqual(query["pbk"], ["PUBLICKEYVALUE"])
        self.assertEqual(query["fp"], ["chrome"])
        self.assertEqual(query["sni"], ["front.example.com"])
        self.assertEqual(query["sid"], ["abcd1234"])
        self.assertEqual(query["spx"], ["/spider"])
        self.assertEqual(query["flow"], ["xtls-rprx-vision"])
        self.assertEqual(query["type"], ["tcp"])

    def test_xui_build_direct_link_uses_inbound_reality_public_key_fallback(self):
        from urllib.parse import parse_qs, urlsplit

        from .xui_api import XUIService

        client_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-000000000397"
        self.inbound.security = Inbound.Security.REALITY
        self.inbound.network_type = Inbound.NetworkType.TCP
        self.inbound.pbk = "LOCALPUBLICKEYVALUE"
        self.inbound.fingerprint = "chrome"
        self.inbound.sni = "front.example.com"
        self.inbound.sid = "abcd1234"
        self.inbound.save(update_fields=["security", "network_type", "pbk", "fingerprint", "sni", "sid", "updated_at"])
        inbound_data = {
            "protocol": "vless",
            "port": "443",
            "streamSettings": json.dumps(
                {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "fingerprint": "chrome",
                        "serverNames": ["front.example.com"],
                        "shortIds": ["abcd1234"],
                        "spiderX": "/spider",
                    },
                }
            ),
        }

        link = XUIService(self.panel).build_direct_link(
            inbound=self.inbound,
            inbound_data=inbound_data,
            client_uuid=client_uuid,
            client_data={"id": client_uuid, "flow": "xtls-rprx-vision", "email": "Reality-Client"},
            email="Reality-Client",
            hosts=[],
        )
        query = parse_qs(urlsplit(link).query)

        self.assertEqual(query["security"], ["reality"])
        self.assertEqual(query["pbk"], ["LOCALPUBLICKEYVALUE"])
        self.assertEqual(query["fp"], ["chrome"])
        self.assertEqual(query["sni"], ["front.example.com"])
        self.assertEqual(query["sid"], ["abcd1234"])

    def test_xui_build_vless_query_params_reads_nested_reality_json_strings(self):
        from urllib.parse import parse_qs

        from .xui_api import build_vless_query_params

        query = parse_qs(
            build_vless_query_params(
                {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": json.dumps(
                        {
                            "settings": json.dumps(
                                {
                                    "publicKey": "NESTEDPUBLICKEY",
                                    "fingerprint": "firefox",
                                }
                            ),
                            "serverNames": json.dumps(["nested-sni.example.com"]),
                            "shortIds": json.dumps(["ef567890"]),
                            "spiderX": "/nested-spider",
                        }
                    ),
                },
                {"flow": "xtls-rprx-vision"},
            )
        )

        self.assertEqual(query["security"], ["reality"])
        self.assertEqual(query["pbk"], ["NESTEDPUBLICKEY"])
        self.assertEqual(query["fp"], ["firefox"])
        self.assertEqual(query["sni"], ["nested-sni.example.com"])
        self.assertEqual(query["sid"], ["ef567890"])
        self.assertEqual(query["spx"], ["/nested-spider"])
        self.assertEqual(query["type"], ["tcp"])
        self.assertEqual(query["flow"], ["xtls-rprx-vision"])

    def test_xui_build_vless_query_params_rejects_reality_without_public_key(self):
        from .xui_api import REALITY_PUBLIC_KEY_MISSING_MESSAGE, XUIError, build_vless_query_params

        with self.assertRaises(XUIError) as raised:
            build_vless_query_params(
                {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "fingerprint": "chrome",
                        "serverNames": ["front.example.com"],
                    },
                },
                {"flow": "xtls-rprx-vision"},
            )

        self.assertEqual(raised.exception.category, "reality_public_key_missing")
        self.assertIn(REALITY_PUBLIC_KEY_MISSING_MESSAGE, str(raised.exception))

    def test_xui_build_direct_link_prefers_native_panel_link(self):
        from .xui_api import XUIService

        native_link = self.reality_link(index=398, host="native-reality.example.com")

        link = XUIService(self.panel).build_direct_link(
            inbound=self.inbound,
            inbound_data={
                "protocol": "vless",
                "port": "443",
                "streamSettings": json.dumps(
                    {
                        "network": "tcp",
                        "security": "reality",
                        "realitySettings": {"fingerprint": "chrome"},
                    }
                ),
            },
            client_uuid="aaaaaaaa-aaaa-4aaa-8aaa-000000000398",
            client_data={"id": "aaaaaaaa-aaaa-4aaa-8aaa-000000000398", "directLink": native_link},
            email="Reality-Client",
            hosts=[],
        )

        self.assertEqual(link, native_link)

    def test_cup_token_is_generated_and_duplicate_config_links_are_allowed(self):
        from .subscription_cups import create_config_link_from_raw

        cup = SubscriptionCup.objects.create(customer=self.customer, order=self.order)
        raw_link = self.direct_link(7)
        first = create_config_link_from_raw(raw_link)
        second = create_config_link_from_raw(raw_link)
        CupItem.objects.create(cup=cup, config_link=first, position=1)
        CupItem.objects.create(cup=cup, config_link=second, position=2)

        self.assertTrue(cup.token)
        self.assertEqual(ConfigLink.objects.filter(raw_link=raw_link).count(), 2)
        self.assertEqual(cup.items.count(), 2)

    def test_subscription_endpoint_returns_raw_by_default_and_base64_when_requested(self):
        links = [self.direct_link(11), "trojan://password@trojan.example.com:443#Trojan"]
        cup = self.create_cup_with_links(*links)

        response = self.client.get(reverse("subscription_cup", args=[cup.token]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.content.decode("utf-8"), "\n".join(links))

        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertEqual(raw_response.status_code, 200)
        self.assertTrue(raw_response["Content-Type"].startswith("text/plain"))
        self.assertEqual(raw_response["Cache-Control"], "no-store")
        self.assertEqual(raw_response.content.decode("utf-8"), "\n".join(links))

        encoded = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "base64"})
        self.assertEqual(base64.b64decode(encoded.content).decode("utf-8"), "\n".join(links) + "\n")

    def test_subscription_endpoint_explicit_formats_and_client_detection(self):
        links = [self.direct_link(15), self.direct_link(16)]
        cup = self.create_cup_with_links(*links)
        url = reverse("subscription_cup", args=[cup.token])

        encoded = self.client.get(url, {"format": "base64"}, **self.browser_headers())
        self.assertTrue(encoded["Content-Type"].startswith("text/plain"))
        self.assertEqual(base64.b64decode(encoded.content).decode("utf-8"), "\n".join(links) + "\n")

        raw_response = self.client.get(url, {"format": "raw"}, **self.browser_headers())
        self.assertTrue(raw_response["Content-Type"].startswith("text/plain"))
        self.assertEqual(raw_response.content.decode("utf-8"), "\n".join(links))

        dashboard = self.client.get(url, {"view": "dashboard"})
        self.assertEqual(dashboard.status_code, 200)
        self.assertTrue(dashboard["Content-Type"].startswith("text/html"))
        self.assertContains(dashboard, "Ø¯Ø§Ø´Ø¨ÙˆØ±Ø¯ Ø§Ø´ØªØ±Ø§Ú©")

        client_response = self.client.get(
            url,
            HTTP_ACCEPT="text/html",
            HTTP_USER_AGENT="Hiddify/1.0",
        )
        self.assertTrue(client_response["Content-Type"].startswith("text/plain"))
        self.assertEqual(base64.b64decode(client_response.content).decode("utf-8"), "\n".join(links) + "\n")

        curl_response = self.client.get(
            url,
            HTTP_ACCEPT="*/*",
            HTTP_USER_AGENT="curl/8.0",
        )
        self.assertTrue(curl_response["Content-Type"].startswith("text/plain"))
        self.assertEqual(curl_response.content.decode("utf-8"), "\n".join(links))

        json_response = self.client.get(url, {"format": "json"})
        json_body = json.loads(json_response.content.decode("utf-8"))
        self.assertEqual(json_response.status_code, 200)
        self.assertTrue(json_response["Content-Type"].startswith("application/json"))
        self.assertEqual(json_body["active_item_count"], 2)
        self.assertIn("masked_link", json_body["items"][0])
        self.assertNotIn("vless://aaaaaaaa", json_response.content.decode("utf-8"))

    def test_subscription_endpoint_blocks_invalid_disabled_and_expired_cups(self):
        disabled = self.create_cup_with_links(self.direct_link(12), status=SubscriptionCup.Status.DISABLED)
        expired = self.create_cup_with_links(
            self.direct_link(13),
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        self.assertEqual(self.client.get("/sub/missing-token").status_code, 404)
        self.assertEqual(self.client.get(reverse("subscription_cup", args=[disabled.token])).status_code, 403)
        self.assertEqual(self.client.get(reverse("subscription_cup", args=[expired.token])).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("subscription_cup", args=[disabled.token]), {"format": "raw"}).content.decode("utf-8"),
            "Subscription is not active.\n",
        )

    def test_subscription_dashboard_renders_for_browser_and_hides_admin_secrets(self):
        links = [self.direct_link(17), "trojan://password@trojan.example.com:443#Trojan"]
        cup = self.create_cup_with_links(*links)
        cup.title = "Alice dashboard"
        cup.traffic_limit_bytes = self.plan.traffic_limit_bytes
        cup.expires_at = timezone.now() + timedelta(days=7)
        cup.save(update_fields=["title", "traffic_limit_bytes", "expires_at", "updated_at"])

        response = self.client.get(reverse("subscription_cup", args=[cup.token]), **self.browser_headers())
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertContains(response, "Alice dashboard")
        self.assertContains(response, "ÙØ¹Ø§Ù„")
        self.assertContains(response, "ØªØ¹Ø¯Ø§Ø¯ Ú©Ø§Ù†ÙÛŒÚ¯")
        self.assertContains(response, "2")
        self.assertContains(response, "Ù„ÛŒÙ†Ú© Ù…Ø¯ÛŒØ±ÛŒØª Ùˆ ÙˆØ±ÙˆØ¯ Ø¨Ù‡ Ø¨Ø±Ù†Ø§Ù…Ù‡")
        self.assertContains(response, "Ú©Ù¾ÛŒ Ú©Ø§Ù†ÙÛŒÚ¯")
        self.assertNotIn("panel-secret", body)
        self.assertNotIn("panel-sub-token", body)
        self.assertNotIn(self.panel.password, body)

        explicit = self.client.get(reverse("subscription_cup_dashboard", args=[cup.token]))
        self.assertEqual(explicit.status_code, 200)
        self.assertContains(explicit, "Ø¯Ø§Ø´Ø¨ÙˆØ±Ø¯ Ø§Ø´ØªØ±Ø§Ú©")

    def test_disabled_subscription_dashboard_is_friendly_without_config_links(self):
        link = self.direct_link(18)
        disabled = self.create_cup_with_links(link, status=SubscriptionCup.Status.DISABLED)

        response = self.client.get(reverse("subscription_cup", args=[disabled.token]), **self.browser_headers())
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ØºÛŒØ±ÙØ¹Ø§Ù„")
        self.assertContains(response, "Ù‚Ø§Ø¨Ù„ Ø§Ø³ØªÙØ§Ø¯Ù‡ Ù†ÛŒØ³Øª")
        self.assertNotIn(link, body)

    def test_subscription_endpoint_requires_no_login_and_does_not_log_links(self):
        secret_link = self.direct_link(14, host="secret.example.com")
        cup = self.create_cup_with_links(secret_link)
        self.client.logout()

        with self.assertNoLogs("store.views", level="INFO"):
            response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode("utf-8"), secret_link)

    def test_completed_vpn_client_order_can_rebuild_subscription_cup(self):
        from .subscription_cups import rebuild_subscription_cup_for_vpn_client, rebuild_subscription_cups_for_order

        cup = rebuild_subscription_cup_for_vpn_client(self.vpn_client, force_active=True)
        order_cups = rebuild_subscription_cups_for_order(self.order, force_active=True)

        self.assertEqual(order_cups[0].pk, cup.pk)
        self.assertEqual(cup.customer, self.customer)
        self.assertEqual(cup.order, self.order)
        self.assertEqual(cup.plan, self.plan)
        self.assertEqual(cup.vpn_client, self.vpn_client)
        self.assertEqual(cup.items.filter(is_active=True).count(), 1)
        self.assertEqual(cup.items.get().config_link.raw_link, self.order.direct_link)

    def test_multi_inbound_direct_links_produce_multiple_cup_items(self):
        from .subscription_cups import rebuild_subscription_cup_for_vpn_client

        second_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=2,
            remark="Secondary",
            protocol=Inbound.Protocol.TROJAN,
            server_ip="vpn-b.example.com",
            port="443",
            config_params="{}",
            is_active=True,
        )
        links = [
            self.direct_link(21, host="alpha.example.com"),
            "trojan://password@beta.example.com:443#Beta",
        ]
        vpn_client = self.create_vpn_client(
            direct_link=links[0],
            xui_raw={
                "bundle_inbound_pks": [self.inbound.pk, second_inbound.pk],
                "bundle_inbound_results": [
                    {"direct_link": links[0]},
                    {"direct_link": links[1]},
                ],
            },
        )

        cup = rebuild_subscription_cup_for_vpn_client(vpn_client, force_active=True)
        item_links = list(
            cup.items.filter(is_active=True)
            .select_related("config_link")
            .order_by("position")
            .values_list("config_link__raw_link", flat=True)
        )

        self.assertEqual(item_links, links)

    def test_order_delivery_groups_include_project_subscription_link_when_cup_exists(self):
        from .subscription_cups import rebuild_subscription_cup_for_vpn_client
        from .telegram_bot.order_delivery import order_config_link_groups

        cup = rebuild_subscription_cup_for_vpn_client(self.vpn_client, force_active=True)

        groups = order_config_link_groups(self.order)

        self.assertEqual(groups[0]["subscription_link"], self.vpn_client.sub_link)
        self.assertEqual(groups[0]["direct_link"], self.vpn_client.direct_link)
        self.assertEqual(groups[0]["project_subscription_link"], f"https://vpn.example.com/sub/{cup.token}")
        self.assertEqual(groups[0]["project_client_link"], f"https://vpn.example.com/sub/{cup.token}?format=base64")

    def test_order_delivery_message_sends_dashboard_and_client_import_links(self):
        from .subscription_cups import rebuild_subscription_cup_for_vpn_client
        from .telegram_bot.config_delivery import format_config_links_text
        from .telegram_bot.order_delivery import send_customer_order_event_message

        cup = rebuild_subscription_cup_for_vpn_client(self.vpn_client, force_active=True)
        client = Mock()

        with patch("store.telegram_bot.order_delivery.send_config_links_message", return_value=object()) as send_message:
            sent = send_customer_order_event_message(
                client,
                self.order,
                event_type="approved",
                chat_id="100",
                format_customer_order_event_func=lambda order, event_type: "approved",
            )

        self.assertEqual(sent, 2)
        project_kwargs = send_message.call_args_list[-1].kwargs
        self.assertEqual(project_kwargs["dashboard_link"], f"https://vpn.example.com/sub/{cup.token}")
        self.assertEqual(project_kwargs["client_link"], f"https://vpn.example.com/sub/{cup.token}?format=base64")
        self.assertEqual(project_kwargs["title"], "âœ… Ù„ÛŒÙ†Ú© Ø³Ø±ÙˆÛŒØ³ Ø´Ù…Ø§ Ø¢Ù…Ø§Ø¯Ù‡ Ø´Ø¯")
        text = format_config_links_text(
            dashboard_link=project_kwargs["dashboard_link"],
            client_link=project_kwargs["client_link"],
            title=project_kwargs["title"],
        )
        self.assertIn("Ù„ÛŒÙ†Ú© Ù…Ø¯ÛŒØ±ÛŒØª Ùˆ ÙˆØ±ÙˆØ¯ Ø¨Ù‡ Ø¨Ø±Ù†Ø§Ù…Ù‡", text)
        self.assertIn("Ù„ÛŒÙ†Ú© Ø³Ø§Ø²Ú¯Ø§Ø± Ø¬Ø§ÛŒÚ¯Ø²ÛŒÙ†", text)

    def test_rebuild_management_command_prints_safe_summary_only(self):
        output = StringIO()

        call_command(
            "rebuild_subscription_cup",
            "--vpn-client-id",
            str(self.vpn_client.pk),
            "--base-url",
            "https://vpn.example.com",
            stdout=output,
        )

        cup = SubscriptionCup.objects.get(vpn_client=self.vpn_client)
        summary = output.getvalue()
        self.assertIn(f"cup id={cup.pk}", summary)
        self.assertIn("item_count=1", summary)
        self.assertIn("protocols=vless", summary)
        self.assertIn("https://vpn.example.com/sub/", summary)
        self.assertNotIn(cup.token, summary)
        self.assertNotIn("vless://", summary)
        self.assertNotIn(self.vpn_client.direct_link, summary)

    def test_cup_center_access_staff_allowed_and_non_staff_blocked(self):
        staff = self.create_staff_user_with_perms("view_subscriptioncup")
        self.client.force_login(staff)

        response = self.client.get(reverse("admin_store_cup_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ù…Ø¯ÛŒØ±ÛŒØª Ù„ÛŒÙ†Ú©â€ŒÙ‡Ø§ÛŒ Ø§Ø´ØªØ±Ø§Ú©")

        regular_user = get_user_model().objects.create_user(username="cup-regular", password="secret")
        self.client.force_login(regular_user)
        response = self.client.get(reverse("admin_store_cup_center"))
        self.assertNotEqual(response.status_code, 200)

    def test_cup_center_manual_cup_creation_and_empty_detail(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_new"),
            {
                "title": "Manual Cup",
                "customer": self.customer.pk,
                "order": self.order.pk,
                "plan": self.plan.pk,
                "status": SubscriptionCup.Status.ACTIVE,
                "expires_at": "",
                "metadata": "{}",
            },
        )

        cup = SubscriptionCup.objects.get(title="Manual Cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_detail", args=[cup.pk]))
        detail = self.client.get(reverse("admin_store_cup_center_detail", args=[cup.pk]))
        self.assertContains(detail, "Ù‡Ù†ÙˆØ² Ù„ÛŒÙ†Ú©ÛŒ Ø¯Ø§Ø®Ù„ Ø§ÛŒÙ† Cup Ù†ÛŒØ³Øª.")
        self.assertContains(detail, "Active items")

    def test_cup_center_detail_shows_counts_and_masked_config_preview(self):
        cup = self.create_cup_with_links(self.direct_link(31))
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin_store_cup_center_detail", args=[cup.pk]))
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vless://&lt;hidden&gt;")
        self.assertContains(response, "Active items")
        self.assertContains(response, "Ù„ÛŒÙ†Ú© Ù…Ø¯ÛŒØ±ÛŒØª Ùˆ ÙˆØ±ÙˆØ¯ Ø¨Ù‡ Ø¨Ø±Ù†Ø§Ù…Ù‡")
        self.assertContains(response, "Ù„ÛŒÙ†Ú© Ø³Ø§Ø²Ú¯Ø§Ø± Ø¬Ø§ÛŒÚ¯Ø²ÛŒÙ†")
        self.assertContains(response, "Ø®Ø±ÙˆØ¬ÛŒ Ø®Ø§Ù…")
        self.assertContains(response, "?format=base64")
        self.assertContains(response, "?format=raw")
        self.assertNotIn("vless://aaaaaaaa", body)

    def test_cup_center_add_existing_config_links_allows_duplicate_items(self):
        from .subscription_cups import create_config_link_from_raw

        cup = SubscriptionCup.objects.create(customer=self.customer, order=self.order, plan=self.plan)
        config_link = create_config_link_from_raw(
            self.direct_link(32),
            source_type=ConfigLink.SourceType.MANUAL,
        )
        CupItem.objects.create(cup=cup, config_link=config_link, position=1)
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_add_existing", args=[cup.pk]),
            {"config_link_ids": [str(config_link.pk)]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cup.items.filter(config_link=config_link).count(), 2)
        self.assertContains(response, "Duplicate warnings")

    def test_cup_center_add_manual_links_parses_multiple_and_skips_unknown(self):
        cup = SubscriptionCup.objects.create(customer=self.customer, order=self.order, plan=self.plan)
        raw_links = "\n".join(
            [
                self.direct_link(33),
                "trojan://password@manual.example.com:443#Manual",
                "not-a-config-link",
            ]
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_add_manual", args=[cup.pk]),
            {"raw_links": raw_links},
        )
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cup.items.count(), 2)
        self.assertEqual(ConfigLink.objects.filter(cup_items__cup=cup, source_type=ConfigLink.SourceType.MANUAL).count(), 2)
        self.assertContains(response, "Skipped invalid")
        self.assertNotIn("vless://aaaaaaaa", body)
        self.assertNotIn("trojan://password", body)

    def test_subscription_endpoint_omits_inactive_cup_items(self):
        cup = self.create_cup_with_links(self.direct_link(34), self.direct_link(35))
        inactive_item = cup.items.order_by("position").last()
        inactive_item.is_active = False
        inactive_item.save(update_fields=["is_active", "updated_at"])

        response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Client-34", body)
        self.assertNotIn("Client-35", body)

    def test_cup_center_create_from_inbound_get_does_not_create_remote_client(self):
        cup = SubscriptionCup.objects.create(customer=self.customer, order=self.order, plan=self.plan)
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter") as adapter_factory:
            response = self.client.get(reverse("admin_store_cup_center_create_from_inbound", args=[cup.pk]))

        self.assertEqual(response.status_code, 200)
        adapter_factory.assert_not_called()

    def test_cup_center_create_from_inbound_posts_remote_client_without_order_or_telegram(self):
        cup = SubscriptionCup.objects.create(customer=self.customer, order=self.order, plan=self.plan)
        before_order_count = Order.objects.count()
        before_client_count = VPNClient.objects.count()
        adapter = Mock()
        adapter.get_capability_report.return_value = SimpleNamespace(supports_create_client=True, errors=(), warnings=())
        adapter.create_enabled_client.return_value = {
            "email": "cup-client@example.test",
            "sub_link": "https://panel.example.com/sub/generated-sub",
            "direct_link": self.direct_link(36),
        }
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter), patch(
            "store.telegram_bot.client.BotClient"
        ) as bot_client:
            response = self.client.post(
                reverse("admin_store_cup_center_create_from_inbound", args=[cup.pk]),
                {
                    "panel": self.panel.pk,
                    "inbound": self.inbound.pk,
                    "total_gb": "1",
                    "duration_days": "30",
                    "device_limit": "2",
                    "email_prefix": f"qasedak-cup-{cup.pk}-test",
                    "confirm_remote_create": "on",
                },
            )

        self.assertRedirects(response, reverse("admin_store_cup_center_detail", args=[cup.pk]))
        self.assertEqual(Order.objects.count(), before_order_count)
        self.assertEqual(VPNClient.objects.count(), before_client_count)
        self.assertEqual(ConfigLink.objects.filter(cup_items__cup=cup, source_type=ConfigLink.SourceType.PANEL_GENERATED).count(), 1)
        self.assertEqual(cup.items.count(), 1)
        adapter.create_enabled_client.assert_called_once()
        bot_client.assert_not_called()

    def test_cup_center_create_from_inbound_failure_shows_structured_safe_error(self):
        cup = SubscriptionCup.objects.create(customer=self.customer, order=self.order, plan=self.plan)
        adapter = Mock()
        adapter.get_capability_report.return_value = SimpleNamespace(supports_create_client=True, errors=(), warnings=())
        adapter.create_enabled_client.side_effect = RuntimeError(
            "Traceback (most recent call last): failed vless://11111111-1111-4111-8111-111111111111@example.com:443 csrf-token panel-secret"
        )
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter):
            response = self.client.post(
                reverse("admin_store_cup_center_create_from_inbound", args=[cup.pk]),
                {
                    "panel": self.panel.pk,
                    "inbound": self.inbound.pk,
                    "total_gb": "1",
                    "duration_days": "30",
                    "device_limit": "2",
                    "email_prefix": f"qasedak-cup-{cup.pk}-test",
                    "confirm_remote_create": "on",
                },
            )

        body = response.content.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cup_remote_create_failed")
        self.assertContains(response, "create_client")
        self.assertContains(response, self.panel.name)
        self.assertNotIn("Traceback (most recent call last)", body)
        self.assertNotIn("vless://", body)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", body)
        self.assertNotIn("panel-secret", body)

    def test_manual_cup_form_does_not_load_order_customer_dropdowns(self):
        from .admin_cup_center.forms import ManualCupForm

        form = ManualCupForm()

        self.assertNotIn("order", form.fields)
        self.assertNotIn("customer", form.fields)
        self.assertNotIn("plan", form.fields)

    def test_quick_builder_page_loads_for_admin_and_blocks_non_staff(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin_store_cup_center_quick_build"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ø³Ø§Ø®Øª Ø³Ø±ÛŒØ¹ Ù„ÛŒÙ†Ú© Ø§Ø´ØªØ±Ø§Ú©")
        self.assertContains(response, self.panel.name)
        self.assertContains(response, self.inbound.remark)

        regular_user = get_user_model().objects.create_user(username="quick-regular", password="secret")
        self.client.force_login(regular_user)
        response = self.client.get(reverse("admin_store_cup_center_quick_build"))
        self.assertNotEqual(response.status_code, 200)

    def test_quick_builder_shows_only_active_sellable_supported_inbounds(self):
        hidden = Inbound.objects.create(
            panel=self.panel,
            inbound_id=44,
            remark="Hidden unavailable inbound",
            protocol=Inbound.Protocol.VLESS,
            server_ip="hidden.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=False,
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin_store_cup_center_quick_build"))
        body = response.content.decode("utf-8")

        self.assertContains(response, self.inbound.remark)
        self.assertNotIn(hidden.remark, body)

    def test_quick_builder_page_groups_multiple_panels_and_inbounds(self):
        other_panel = Panel.objects.create(
            store=self.store,
            name="Panel B",
            url="https://panel-b.example.com",
            username="admin",
            password="panel-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.MODERN_MULTI_NODE,
            detected_xui_version="3.4.0",
        )
        PanelHealthStatus.objects.create(panel=other_panel, status=PanelHealthStatus.Status.OK)
        other_inbound = Inbound.objects.create(
            panel=other_panel,
            inbound_id=5,
            remark="Panel B inbound",
            protocol=Inbound.Protocol.TROJAN,
            server_ip="panel-b.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin_store_cup_center_quick_build"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.panel.name)
        self.assertContains(response, other_panel.name)
        self.assertContains(response, other_inbound.remark)
        self.assertContains(response, "Modern multi-node")
        self.assertContains(response, "3.4.0")
        self.assertContains(response, "Ø³Ù„Ø§Ù…Øª: ok")

    def test_quick_builder_validation_requires_source(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_quick_build"),
            {
                "title": "No source",
                "panel": self.panel.pk,
                "volume_gb": "10",
                "duration_days": "30",
                "device_limit": "2",
                "remark_prefix": "qasedak-cup-no-inbound",
                "confirm_remote_create": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ø­Ø¯Ø§Ù‚Ù„ ÛŒÚ© Ø§ÛŒÙ†Ø¨Ø§Ù†Ø¯ ÛŒØ§ ÛŒÚ© Ù…Ø®Ø²Ù†")
        self.assertFalse(SubscriptionCup.objects.filter(title="No source").exists())

    def test_quick_builder_rejects_unsupported_inbound_protocol(self):
        unsupported_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=66,
            remark="Unsupported protocol inbound",
            protocol="ss",
            server_ip="ss.example.com",
            port="8388",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        adapter = self.quick_builder_adapter()
        self.client.force_login(self.admin_user)

        get_response = self.client.get(reverse("admin_store_cup_center_quick_build"))
        self.assertNotContains(get_response, unsupported_inbound.remark)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Unsupported protocol",
                    "inbounds": [unsupported_inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-unsupported-protocol",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Unsupported protocol")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.metadata["status"], "failed")
        adapter.create_enabled_client.assert_not_called()
        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertContains(result_response, "Protocol")
        self.assertContains(result_response, "inbound_unsupported_protocol")
        self.assertContains(result_response, "Ø§ÛŒÙ† Cup Ù„ÛŒÙ†Ú© Ù‚Ø§Ø¨Ù„ ØªØ­ÙˆÛŒÙ„ Ù†Ø¯Ø§Ø±Ø¯.")

    def test_quick_builder_rejects_duplicate_remote_inbound_id_inside_same_panel(self):
        duplicate_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=self.inbound.inbound_id,
            xui_node_id="node-beta",
            remark="Duplicate remote ID",
            protocol=Inbound.Protocol.VLESS,
            server_ip="duplicate.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        adapter = self.quick_builder_adapter(multi=True)
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Duplicate remote",
                    "inbounds": [self.inbound.pk, duplicate_inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-duplicate",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Duplicate remote")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.metadata["status"], "failed")
        adapter.create_enabled_multi_inbound_client.assert_not_called()
        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertContains(result_response, "ØªÚ©Ø±Ø§Ø±ÛŒ")
        self.assertContains(result_response, "duplicate_remote_inbound_id")
        self.assertContains(result_response, "Ø§ÛŒÙ† Cup Ù„ÛŒÙ†Ú© Ù‚Ø§Ø¨Ù„ ØªØ­ÙˆÛŒÙ„ Ù†Ø¯Ø§Ø±Ø¯.")

    def test_quick_builder_allows_same_remote_inbound_id_across_panels(self):
        other_panel = Panel.objects.create(
            store=self.store,
            name="Panel with same remote id",
            url="https://same-id.example.com",
            username="admin",
            password="panel-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.MODERN_SINGLE_NODE,
        )
        other_inbound = Inbound.objects.create(
            panel=other_panel,
            inbound_id=self.inbound.inbound_id,
            remark="Same remote ID on other panel",
            protocol=Inbound.Protocol.VLESS,
            server_ip="same-id.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        first_link = self.direct_link(61, host="same-a.example.com")
        second_link = self.direct_link(62, host="same-b.example.com")
        first_adapter = self.quick_builder_adapter(direct_link=first_link)
        second_adapter = self.quick_builder_adapter(direct_link=second_link)
        adapters = {self.panel.pk: first_adapter, other_panel.pk: second_adapter}
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", side_effect=lambda panel: adapters[panel.pk]):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Same remote cross panel",
                    "inbounds": [self.inbound.pk, other_inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-same-remote",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Same remote cross panel")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(ConfigLink.objects.filter(cup_items__cup=cup).count(), 2)
        first_adapter.create_enabled_client.assert_called_once()
        second_adapter.create_enabled_client.assert_called_once()

    def test_quick_builder_panel_validation_failure_does_not_drop_other_panel(self):
        duplicate_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=self.inbound.inbound_id,
            xui_node_id="node-duplicate",
            remark="Duplicate in same panel",
            protocol=Inbound.Protocol.VLESS,
            server_ip="duplicate-same-panel.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        other_panel = Panel.objects.create(
            store=self.store,
            name="Validation survivor panel",
            url="https://validation-survivor.example.com",
            username="admin",
            password="panel-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.MODERN_SINGLE_NODE,
        )
        other_inbound = Inbound.objects.create(
            panel=other_panel,
            inbound_id=8,
            remark="Validation survivor inbound",
            protocol=Inbound.Protocol.VLESS,
            server_ip="validation-survivor.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        survivor_link = self.direct_link(245, host="validation-survivor.example.com")
        duplicate_group_adapter = self.quick_builder_adapter(multi=True)
        survivor_adapter = self.quick_builder_adapter(direct_link=survivor_link)
        adapters = {self.panel.pk: duplicate_group_adapter, other_panel.pk: survivor_adapter}
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", side_effect=lambda panel: adapters[panel.pk]):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Validation partial quick",
                    "inbounds": [self.inbound.pk, duplicate_inbound.pk, other_inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-validation-partial",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Validation partial quick")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.metadata["status"], "partial_with_warnings")
        self.assertEqual(cup.items.count(), 1)
        self.assertEqual(ConfigLink.objects.get(cup_items__cup=cup).raw_link, survivor_link)
        duplicate_group_adapter.create_enabled_multi_inbound_client.assert_not_called()
        survivor_adapter.create_enabled_client.assert_called_once()
        reconciliation = cup.metadata["reconciliation"]
        self.assertEqual(reconciliation["selected_sources_count"], 3)
        self.assertEqual(reconciliation["attempted_panel_groups"], 2)
        self.assertEqual(reconciliation["failed_panel_groups"], 1)
        self.assertEqual(reconciliation["failed_inbounds"], 2)
        self.assertEqual(reconciliation["panel_links_created"], 1)

        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "duplicate_remote_inbound_id")
        self.assertContains(result_response, "Validation survivor panel")
        self.assertContains(result_response, "Ø§Ø² Û³ Ù…Ù†Ø¨Ø¹ Ø§Ù†ØªØ®Ø§Ø¨â€ŒØ´Ø¯Ù‡ØŒ ÙÙ‚Ø· Û± Ú©Ø§Ù†ÙÛŒÚ¯ ÙˆØ§Ø±Ø¯ Cup Ø´Ø¯.")
        self.assertNotIn(survivor_link, result_body)

    def test_quick_builder_multi_panel_selection_creates_one_cup(self):
        other_panel = Panel.objects.create(
            store=self.store,
            name="Other Panel",
            url="https://other-panel.example.com",
            username="admin",
            password="panel-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.MODERN_SINGLE_NODE,
        )
        other_inbound = Inbound.objects.create(
            panel=other_panel,
            inbound_id=2,
            remark="Other",
            protocol=Inbound.Protocol.VLESS,
            server_ip="other.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        before_order_count = Order.objects.count()
        before_client_count = VPNClient.objects.count()
        first_link = self.direct_link(51, host="panel-a.example.com")
        second_link = self.direct_link(52, host="panel-b.example.com")
        first_adapter = self.quick_builder_adapter(direct_link=first_link)
        second_adapter = self.quick_builder_adapter(direct_link=second_link)
        adapters = {self.panel.pk: first_adapter, other_panel.pk: second_adapter}
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", side_effect=lambda panel: adapters[panel.pk]), patch(
            "store.telegram_bot.client.BotClient"
        ) as bot_client:
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Mixed panels",
                    "panel": self.panel.pk,
                    "inbounds": [self.inbound.pk, other_inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-mixed",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Mixed panels")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(Order.objects.count(), before_order_count)
        self.assertEqual(VPNClient.objects.count(), before_client_count)
        self.assertEqual(ConfigLink.objects.filter(cup_items__cup=cup).count(), 2)
        self.assertEqual(cup.items.count(), 2)
        first_adapter.create_enabled_client.assert_called_once()
        second_adapter.create_enabled_client.assert_called_once()
        bot_client.assert_not_called()

        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "Ù…ÙˆÙÙ‚")
        self.assertContains(result_response, "Other Panel")
        self.assertNotIn(first_link, result_body)
        self.assertNotIn(second_link, result_body)

        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertCountEqual(raw_response.content.decode("utf-8").splitlines(), [first_link, second_link])
        encoded_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "base64"})
        decoded = base64.b64decode(encoded_response.content).decode("utf-8")
        self.assertCountEqual(decoded.splitlines(), [first_link, second_link])

    def test_quick_builder_stores_exact_panel_returned_reality_link(self):
        panel_link = self.reality_link(index=302, host="quick-reality.example.com")
        adapter = self.quick_builder_adapter(direct_link=panel_link)
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Reality quick cup",
                    "inbounds": [self.inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-reality",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Reality quick cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        config_link = ConfigLink.objects.get(cup_items__cup=cup)
        self.assertEqual(config_link.raw_link, panel_link)
        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertEqual(raw_response.content.decode("utf-8"), panel_link)
        decoded = base64.b64decode(
            self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "base64"}).content
        ).decode("utf-8")
        self.assertEqual(decoded, f"{panel_link}\n")
        for required in ("security=reality", "pbk=", "fp=chrome", "sni=", "sid=", "spx=", "flow=", "type=tcp"):
            self.assertIn(required, config_link.raw_link)

    def test_quick_builder_reality_missing_pbk_fails_inbound_but_keeps_inventory(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Reality Fallback Inventory")
        inventory_link = self.direct_link(243, host="reality-fallback-inventory.example.com")
        import_config_assets(pool, inventory_link)
        bad_reality_link = (
            "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000244@reality-missing-pbk.example.com:443"
            "?type=tcp&security=reality&fp=chrome&sni=front.example.com#MissingPBK"
        )
        adapter = self.quick_builder_adapter(direct_link=bad_reality_link)
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Reality missing pbk hybrid",
                    "inbounds": [self.inbound.pk],
                    "inventory_pools": [pool.pk],
                    "inventory_quantity": "1",
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-reality-missing-pbk",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Reality missing pbk hybrid")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.metadata["status"], "partial_with_warnings")
        self.assertEqual(cup.items.count(), 1)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup).count(), 1)
        config_link = ConfigLink.objects.get(cup_items__cup=cup)
        self.assertEqual(config_link.raw_link, inventory_link)
        self.assertFalse(ConfigLink.objects.filter(raw_link=bad_reality_link, cup_items__cup=cup).exists())
        reconciliation = cup.metadata["reconciliation"]
        self.assertEqual(reconciliation["selected_sources_count"], 2)
        self.assertEqual(reconciliation["cup_items_created"], 1)
        self.assertEqual(reconciliation["failed_inbounds"], 1)
        self.assertEqual(reconciliation["inventory_auto_allocated"], 1)

        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "reality_public_key_missing")
        self.assertContains(result_response, "Ø§ÛŒÙ† Cup Ù†Ø§Ù‚Øµ Ø³Ø§Ø®ØªÙ‡ Ø´Ø¯Ù‡ Ø§Ø³Øª.")
        self.assertNotIn(bad_reality_link, result_body)
        self.assertNotIn(inventory_link, result_body)

        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertEqual(raw_response.content.decode("utf-8"), inventory_link)

    def test_quick_builder_surfaces_panel_integration_error_details(self):
        from .panels.errors import PanelCreateClientFailedError

        adapter = self.quick_builder_adapter()
        adapter.create_enabled_client.side_effect = PanelCreateClientFailedError(
            "Reality public key Ø¨Ø±Ø§ÛŒ Ø³Ø§Ø®Øª Ù„ÛŒÙ†Ú© Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯. Ù„ÛŒÙ†Ú© ØªÙˆÙ„ÛŒØ¯Ø´Ø¯Ù‡ Ù…Ù…Ú©Ù† Ø§Ø³Øª Ù‚Ø§Ø¨Ù„ Ø§Ø³ØªÙØ§Ø¯Ù‡ Ù†Ø¨Ø§Ø´Ø¯.",
            error_code="reality_public_key_missing",
            layer="subscription_render",
            action="create_client",
            technical_detail="Reality public key Ø¨Ø±Ø§ÛŒ Ø³Ø§Ø®Øª Ù„ÛŒÙ†Ú© Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯. Ù„ÛŒÙ†Ú© ØªÙˆÙ„ÛŒØ¯Ø´Ø¯Ù‡ Ù…Ù…Ú©Ù† Ø§Ø³Øª Ù‚Ø§Ø¨Ù„ Ø§Ø³ØªÙØ§Ø¯Ù‡ Ù†Ø¨Ø§Ø´Ø¯.",
            remediation="Reality inbound streamSettings.realitySettings.settings.publicKey ÛŒØ§ Ù„ÛŒÙ†Ú© native Ù¾Ù†Ù„ Ø±Ø§ Ø¨Ø±Ø±Ø³ÛŒ Ú©Ù†ÛŒØ¯.",
            panel=self.panel,
            inbound=self.inbound,
        )
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Reality adapter error cup",
                    "inbounds": [self.inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-reality-adapter-error",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Reality adapter error cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        structured_error = cup.metadata["panel_results"][0]["structured_errors"][0]
        self.assertEqual(structured_error["error_code"], "reality_public_key_missing")
        self.assertEqual(structured_error["message"], "Reality public key Ø¨Ø±Ø§ÛŒ Ø³Ø§Ø®Øª Ù„ÛŒÙ†Ú© Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯. Ù„ÛŒÙ†Ú© ØªÙˆÙ„ÛŒØ¯Ø´Ø¯Ù‡ Ù…Ù…Ú©Ù† Ø§Ø³Øª Ù‚Ø§Ø¨Ù„ Ø§Ø³ØªÙØ§Ø¯Ù‡ Ù†Ø¨Ø§Ø´Ø¯.")
        self.assertIn("streamSettings.realitySettings.settings.publicKey", structured_error["remediation"])
        self.assertEqual(cup.metadata["reconciliation"]["inbound_details"][0]["error_code"], "reality_public_key_missing")

    def test_quick_builder_partial_success_keeps_successful_panel_links(self):
        other_panel = Panel.objects.create(
            store=self.store,
            name="Failing Panel",
            url="https://failing-panel.example.com",
            username="admin",
            password="panel-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.MODERN_SINGLE_NODE,
        )
        other_inbound = Inbound.objects.create(
            panel=other_panel,
            inbound_id=7,
            remark="Failing inbound",
            protocol=Inbound.Protocol.VLESS,
            server_ip="failing.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        first_link = self.direct_link(71, host="partial-success.example.com")
        first_adapter = self.quick_builder_adapter(direct_link=first_link)
        failing_adapter = self.quick_builder_adapter(fail_single=True)
        adapters = {self.panel.pk: first_adapter, other_panel.pk: failing_adapter}
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", side_effect=lambda panel: adapters[panel.pk]):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Partial quick",
                    "inbounds": [self.inbound.pk, other_inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-partial",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Partial quick")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.metadata["status"], "partial_with_warnings")
        self.assertEqual(ConfigLink.objects.filter(cup_items__cup=cup).count(), 1)
        self.assertEqual(cup.items.count(), 1)
        first_adapter.create_enabled_client.assert_called_once()
        failing_adapter.create_enabled_client.assert_called_once()

        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "Ù…ÙˆÙÙ‚ Ø¨Ø§ Ù‡Ø´Ø¯Ø§Ø±")
        self.assertContains(result_response, "Ø§Ø² Û² Ù…Ù†Ø¨Ø¹ Ø§Ù†ØªØ®Ø§Ø¨â€ŒØ´Ø¯Ù‡ØŒ ÙÙ‚Ø· Û± Ú©Ø§Ù†ÙÛŒÚ¯ ÙˆØ§Ø±Ø¯ Cup Ø´Ø¯.")
        self.assertContains(result_response, "Ø§ÛŒÙ† Cup Ù†Ø§Ù‚Øµ Ø³Ø§Ø®ØªÙ‡ Ø´Ø¯Ù‡ Ø§Ø³Øª.")
        self.assertContains(result_response, "Failing Panel")
        self.assertContains(result_response, "panel timeout")
        self.assertContains(result_response, "cup_remote_create_failed")
        self.assertContains(result_response, "create_client")
        self.assertNotIn(first_link, result_body)

        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertEqual(raw_response.content.decode("utf-8"), first_link)

    def test_quick_builder_rejects_unsupported_panel_family(self):
        unsupported_panel = Panel.objects.create(
            store=self.store,
            name="Unsupported Panel",
            family=Panel.Family.UNKNOWN,
            url="https://unsupported.example.com",
            username="admin",
            password="panel-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.UNKNOWN_SAFE,
        )
        unsupported_inbound = Inbound.objects.create(
            panel=unsupported_panel,
            inbound_id=1,
            remark="Unsupported inbound",
            protocol=Inbound.Protocol.VLESS,
            server_ip="unsupported.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_quick_build"),
            {
                "title": "Unsupported quick",
                "panel": unsupported_panel.pk,
                "inbounds": [unsupported_inbound.pk],
                "volume_gb": "10",
                "duration_days": "30",
                "device_limit": "2",
                "remark_prefix": "qasedak-cup-unsupported",
                "confirm_remote_create": "on",
            },
        )

        cup = SubscriptionCup.objects.get(title="Unsupported quick")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.metadata["status"], "failed")
        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertContains(result_response, "Ù¾Ø´ØªÛŒØ¨Ø§Ù†ÛŒ")
        self.assertContains(result_response, "unsupported_panel_family")
        self.assertContains(result_response, "Sync capabilities")
        self.assertContains(result_response, "Ø§ÛŒÙ† Cup Ù„ÛŒÙ†Ú© Ù‚Ø§Ø¨Ù„ ØªØ­ÙˆÛŒÙ„ Ù†Ø¯Ø§Ø±Ø¯.")

    def test_quick_builder_rejects_legacy_multi_selection(self):
        second_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=2,
            remark="Second",
            protocol=Inbound.Protocol.VLESS,
            server_ip="second.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        adapter = Mock()
        adapter.get_capability_report.return_value = SimpleNamespace(
            supported=True,
            supports_create_client=True,
            supports_multi_inbound_create=False,
            errors=(),
            warnings=(),
        )
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Legacy multi",
                    "panel": self.panel.pk,
                    "inbounds": [self.inbound.pk, second_inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-legacy-multi",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Legacy multi")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.metadata["status"], "failed")
        adapter.create_enabled_multi_inbound_client.assert_not_called()
        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertContains(result_response, "Ú†Ù†Ø¯ inbound")
        self.assertContains(result_response, "supports_multi_inbound_create")
        self.assertContains(result_response, "panel_capability_missing")
        self.assertContains(result_response, "Ø§ÛŒÙ† Cup Ù„ÛŒÙ†Ú© Ù‚Ø§Ø¨Ù„ ØªØ­ÙˆÛŒÙ„ Ù†Ø¯Ø§Ø±Ø¯.")

    def test_quick_builder_modern_multi_creates_cup_without_order_vpnclient_or_telegram(self):
        second_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=2,
            remark="Second modern",
            protocol=Inbound.Protocol.TROJAN,
            server_ip="second-modern.example.com",
            port="443",
            config_params="{}",
            is_active=True,
            available_for_new_orders=True,
        )
        before_order_count = Order.objects.count()
        before_client_count = VPNClient.objects.count()
        first_link = self.direct_link(41, host="first-modern.example.com")
        second_link = "trojan://password@second-modern.example.com:443#Second"
        adapter = Mock()
        adapter.get_capability_report.return_value = SimpleNamespace(
            supported=True,
            supports_create_client=True,
            supports_multi_inbound_create=True,
            errors=(),
            warnings=(),
        )
        adapter.create_enabled_multi_inbound_client.return_value = {
            "email": "quick-client@example.test",
            "bundle_inbound_results": [
                {"direct_link": first_link, "email": "quick-client@example.test"},
                {"direct_link": second_link, "email": "quick-client@example.test"},
            ],
        }
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter), patch(
            "store.telegram_bot.client.BotClient"
        ) as bot_client:
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Quick modern multi",
                    "panel": self.panel.pk,
                    "inbounds": [self.inbound.pk, second_inbound.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-modern-multi",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Quick modern multi")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(Order.objects.count(), before_order_count)
        self.assertEqual(VPNClient.objects.count(), before_client_count)
        self.assertEqual(ConfigLink.objects.filter(cup_items__cup=cup).count(), 2)
        self.assertEqual(cup.items.count(), 2)
        adapter.create_enabled_multi_inbound_client.assert_called_once()
        request_arg = adapter.create_enabled_multi_inbound_client.call_args.args[0]
        self.assertEqual([inbound.inbound_id for inbound in request_arg.inbounds], [self.inbound.inbound_id, second_inbound.inbound_id])
        bot_client.assert_not_called()

        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "Quick modern multi")
        self.assertContains(result_response, "vless")
        self.assertContains(result_response, "trojan")
        self.assertNotIn(first_link, result_body)
        self.assertNotIn(second_link, result_body)

        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertEqual(raw_response.content.decode("utf-8"), "\n".join([first_link, second_link]))

    def test_quick_builder_inventory_pool_is_visible_and_preselected_from_import_flow(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Quick Pool Stock")
        raw_link = self.direct_link(214, host="quick-pool-secret.example.com")
        import_config_assets(pool, raw_link)
        self.client.force_login(self.admin_user)

        response = self.client.get(f"{reverse('admin_store_cup_center_quick_build')}?inventory_pool={pool.pk}")
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ø¨Ø±Ø¯Ø§Ø´Øª Ø§Ø² Ù…Ø®Ø²Ù† Ú©Ø§Ù†ÙÛŒÚ¯ Ø¢Ù…Ø§Ø¯Ù‡")
        self.assertContains(response, pool.title)
        self.assertIn(f'name="inventory_pools" value="{pool.pk}"', body)
        self.assertIn("checked", body)
        self.assertNotIn(raw_link, body)

    def test_quick_builder_inventory_only_creates_cup_without_panel_order_vpnclient_or_telegram(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Inventory Only Quick")
        inventory_link = self.direct_link(215, host="inventory-only.example.com")
        import_config_assets(pool, inventory_link)
        before_order_count = Order.objects.count()
        before_client_count = VPNClient.objects.count()
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter") as adapter_factory, patch(
            "store.telegram_bot.client.BotClient"
        ) as bot_client:
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Inventory only quick cup",
                    "inventory_pools": [pool.pk],
                    "inventory_quantity": "1",
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-inventory-only",
                },
            )

        cup = SubscriptionCup.objects.get(title="Inventory only quick cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(Order.objects.count(), before_order_count)
        self.assertEqual(VPNClient.objects.count(), before_client_count)
        self.assertEqual(cup.items.count(), 1)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup).count(), 1)
        self.assertEqual(ConfigLink.objects.get(cup_items__cup=cup).source_type, ConfigLink.SourceType.IMPORTED_SUBSCRIPTION)
        adapter_factory.assert_not_called()
        bot_client.assert_not_called()

        asset = ConfigInventoryAsset.objects.get(pool=pool)
        self.assertEqual(asset.status, ConfigInventoryAsset.Status.ASSIGNED)
        self.assertEqual(asset.current_allocations, 1)
        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "Ø¨Ø±Ø¯Ø§Ø´ØªÙ‡â€ŒØ´Ø¯Ù‡ Ø§Ø² Ù…Ø®Ø²Ù†")
        self.assertContains(result_response, pool.title)
        self.assertNotIn(inventory_link, result_body)

        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertEqual(raw_response.content.decode("utf-8"), inventory_link)

    def test_quick_builder_manual_asset_picker_adds_selected_assets_to_cup(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Manual Picker Pool")
        first_link = self.direct_link(219, host="manual-first.example.com")
        selected_link = self.direct_link(220, host="manual-selected.example.com")
        import_config_assets(pool, "\n".join([first_link, selected_link]))
        selected_asset = ConfigInventoryAsset.objects.get(pool=pool, raw_link=selected_link)
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_quick_build"),
            {
                "title": "Manual inventory quick cup",
                "inventory_pools": [pool.pk],
                f"inventory_mode_{pool.pk}": "manual_select_assets",
                f"inventory_asset_ids_{pool.pk}": [selected_asset.pk],
                "volume_gb": "10",
                "duration_days": "30",
                "device_limit": "2",
                "remark_prefix": "qasedak-cup-manual-inventory",
            },
        )

        cup = SubscriptionCup.objects.get(title="Manual inventory quick cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.items.count(), 1)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup, asset=selected_asset).count(), 1)
        self.assertEqual(ConfigLink.objects.get(cup_items__cup=cup).raw_link, selected_link)
        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "Ú©Ø§Ù†ÙÛŒÚ¯â€ŒÙ‡Ø§ÛŒ Ø§Ù†ØªØ®Ø§Ø¨â€ŒØ´Ø¯Ù‡ Ø§Ø² Ù…Ø®Ø²Ù†")
        self.assertContains(result_response, "Ø§Ù†ØªØ®Ø§Ø¨ Ø¯Ø³ØªÛŒ")
        self.assertNotIn(selected_link, result_body)
        self.assertNotIn(first_link, result_body)

    def test_quick_builder_manual_asset_picker_accepts_generic_asset_ids_alias(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Manual Picker Alias Pool")
        selected_link = self.direct_link(233, host="manual-alias-selected.example.com")
        import_config_assets(pool, selected_link)
        selected_asset = ConfigInventoryAsset.objects.get(pool=pool, raw_link=selected_link)
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_quick_build"),
            {
                "title": "Manual inventory alias quick cup",
                "inventory_pools": [pool.pk],
                f"inventory_mode_{pool.pk}": "manual_select_assets",
                "asset_ids": [selected_asset.pk],
                "volume_gb": "10",
                "duration_days": "30",
                "device_limit": "2",
                "remark_prefix": "qasedak-cup-manual-alias",
            },
        )

        cup = SubscriptionCup.objects.get(title="Manual inventory alias quick cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.items.count(), 1)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup, asset=selected_asset).count(), 1)
        self.assertEqual(ConfigLink.objects.get(cup_items__cup=cup).raw_link, selected_link)

    def test_quick_builder_manual_asset_without_pool_checkbox_is_not_dropped(self):
        from .admin_cup_center.forms import QuickSubscriptionBuilderForm
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Manual Inferred Pool")
        selected_link = self.direct_link(242, host="manual-inferred.example.com")
        import_config_assets(pool, selected_link)
        selected_asset = ConfigInventoryAsset.objects.get(pool=pool, raw_link=selected_link)

        form = QuickSubscriptionBuilderForm(
            data={
                "title": "Manual inferred form",
                f"inventory_mode_{pool.pk}": "manual_select_assets",
                f"inventory_asset_ids_{pool.pk}": [selected_asset.pk],
                "volume_gb": "10",
                "duration_days": "30",
                "device_limit": "2",
                "remark_prefix": "qasedak-cup-manual-inferred-form",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([selection["pool"].pk for selection in form.cleaned_data["inventory_selections"]], [pool.pk])
        self.assertEqual(form.cleaned_data["inventory_selections"][0]["mode"], "manual_select_assets")
        self.assertEqual(form.cleaned_data["inventory_selections"][0]["asset_ids"], [selected_asset.pk])

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("admin_store_cup_center_quick_build"),
            {
                "title": "Manual inferred quick cup",
                f"inventory_mode_{pool.pk}": "manual_select_assets",
                f"inventory_asset_ids_{pool.pk}": [selected_asset.pk],
                "volume_gb": "10",
                "duration_days": "30",
                "device_limit": "2",
                "remark_prefix": "qasedak-cup-manual-inferred",
            },
        )

        cup = SubscriptionCup.objects.get(title="Manual inferred quick cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.items.count(), 1)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup, asset=selected_asset).count(), 1)
        self.assertEqual(ConfigLink.objects.get(cup_items__cup=cup).raw_link, selected_link)
        reconciliation = cup.metadata["reconciliation"]
        self.assertEqual(reconciliation["selected_inventory_pool_ids"], [pool.pk])
        self.assertEqual(reconciliation["selected_manual_asset_ids"], [selected_asset.pk])
        self.assertEqual(reconciliation["attempted_manual_assets"], 1)
        self.assertEqual(reconciliation["inventory_manual_allocated"], 1)

    def test_quick_builder_manual_inventory_fails_if_no_cupitem_is_created(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Manual No CupItem Pool")
        selected_link = self.direct_link(234, host="manual-no-cupitem.example.com")
        import_config_assets(pool, selected_link)
        selected_asset = ConfigInventoryAsset.objects.get(pool=pool, raw_link=selected_link)
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services._create_cup_items_from_inventory_allocation", return_value=([], [])):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Manual inventory no cupitem",
                    "inventory_pools": [pool.pk],
                    f"inventory_mode_{pool.pk}": "manual_select_assets",
                    f"inventory_asset_ids_{pool.pk}": [selected_asset.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-manual-no-cupitem",
                },
            )

        cup = SubscriptionCup.objects.get(title="Manual inventory no cupitem")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.items.count(), 0)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup, asset=selected_asset).count(), 0)
        self.assertEqual(cup.metadata["status"], "failed")
        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertContains(result_response, "Ù‡ÛŒÚ†â€ŒÚ©Ø¯Ø§Ù… Ø§Ø² Ú©Ø§Ù†ÙÛŒÚ¯â€ŒÙ‡Ø§ÛŒ Ø§Ù†ØªØ®Ø§Ø¨â€ŒØ´Ø¯Ù‡ Ø§Ø² Ù…Ø®Ø²Ù† ÙˆØ§Ø±Ø¯ Cup Ù†Ø´Ø¯Ù†Ø¯.")
        self.assertContains(result_response, "config_inventory_no_cup_item_created")

    def test_quick_builder_manual_asset_picker_renders_selectable_checkboxes(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Manual Picker Render Pool")
        usable_link = self.direct_link(317, host="manual-render-usable.example.com")
        disabled_link = self.direct_link(318, host="manual-render-disabled.example.com")
        import_config_assets(pool, "\n".join([usable_link, disabled_link]))
        disabled_asset = ConfigInventoryAsset.objects.get(raw_link=disabled_link)
        disabled_asset.status = ConfigInventoryAsset.Status.DISABLED
        disabled_asset.save(update_fields=["status", "updated_at"])
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("admin_store_cup_center_quick_build"),
            {
                "inventory_pool": pool.pk,
                f"inventory_mode_{pool.pk}": "manual_select_assets",
            },
        )
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cup-asset-card")
        self.assertIn(f'name="inventory_asset_ids_{pool.pk}"', body)
        self.assertIn('type="checkbox"', body)
        self.assertIn(f'data-inventory-select-all="{pool.pk}"', body)
        self.assertIn("updateInventorySelectedSummary", body)
        self.assertContains(response, "ØºÛŒØ±ÙØ¹Ø§Ù„")
        self.assertIn("disabled", body)
        self.assertNotIn(usable_link, body)
        self.assertNotIn(disabled_link, body)

    def test_quick_builder_manual_rejects_exclusive_assigned_asset(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Exclusive Manual Pool")
        raw_link = self.direct_link(221, host="exclusive-manual.example.com")
        import_config_assets(pool, raw_link)
        asset = ConfigInventoryAsset.objects.get(pool=pool)
        first_cup = SubscriptionCup.objects.create(title="Already allocated")
        from .config_inventory_services import allocate_selected_assets_from_pool

        allocate_selected_assets_from_pool(pool, [asset.pk], cup=first_cup)
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_quick_build"),
            {
                "title": "Exclusive duplicate manual",
                "inventory_pools": [pool.pk],
                f"inventory_mode_{pool.pk}": "manual_select_assets",
                f"inventory_asset_ids_{pool.pk}": [asset.pk],
                "volume_gb": "10",
                "duration_days": "30",
                "device_limit": "2",
                "remark_prefix": "qasedak-cup-exclusive-duplicate",
            },
        )

        cup = SubscriptionCup.objects.get(title="Exclusive duplicate manual")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.items.count(), 0)
        self.assertEqual(ConfigAllocation.objects.filter(asset=asset, status=ConfigAllocation.Status.ACTIVE).count(), 1)
        self.assertEqual(cup.metadata["status"], "failed")
        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "exclusive_asset_assigned")
        self.assertNotIn(raw_link, result_body)

    def test_quick_builder_manual_allows_shared_unlimited_asset_multiple_times(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(
            title="Shared Unlimited Manual Pool",
            allocation_mode=ConfigInventoryPool.AllocationMode.SHARED_UNLIMITED,
        )
        raw_link = self.direct_link(222, host="shared-unlimited-manual.example.com")
        import_config_assets(pool, raw_link)
        asset = ConfigInventoryAsset.objects.get(pool=pool)
        self.client.force_login(self.admin_user)

        for index in range(2):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": f"Shared unlimited manual {index}",
                    "inventory_pools": [pool.pk],
                    f"inventory_mode_{pool.pk}": "manual_select_assets",
                    f"inventory_asset_ids_{pool.pk}": [asset.pk],
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": f"qasedak-cup-shared-{index}",
                },
            )
            cup = SubscriptionCup.objects.get(title=f"Shared unlimited manual {index}")
            self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
            self.assertEqual(cup.items.count(), 1)

        asset.refresh_from_db()
        self.assertEqual(asset.current_allocations, 2)
        self.assertEqual(ConfigAllocation.objects.filter(asset=asset, status=ConfigAllocation.Status.ACTIVE).count(), 2)

    def test_quick_builder_manual_picker_displays_unlimited_capacity_as_selectable(self):
        from .config_inventory_services import allocate_selected_assets_from_pool, import_config_assets

        pool = self.create_inventory_pool(
            title="Shared Unlimited Display Pool",
            allocation_mode=ConfigInventoryPool.AllocationMode.SHARED_UNLIMITED,
        )
        raw_link = self.direct_link(235, host="shared-unlimited-display.example.com")
        import_config_assets(pool, raw_link)
        asset = ConfigInventoryAsset.objects.get(pool=pool)
        allocate_selected_assets_from_pool(pool, [asset.pk], cup=SubscriptionCup.objects.create(title="Display first use"))
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("admin_store_cup_center_quick_build"),
            {
                "inventory_pool": pool.pk,
                f"inventory_mode_{pool.pk}": "manual_select_assets",
            },
        )
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ø¸Ø±ÙÛŒØª: Ù†Ø§Ù…Ø­Ø¯ÙˆØ¯")
        self.assertContains(response, "Ù‚Ø§Ø¨Ù„ Ø§Ø³ØªÙØ§Ø¯Ù‡: Ø¨Ù„Ù‡")
        self.assertIn(f'name="inventory_asset_ids_{pool.pk}"', body)
        self.assertNotIn(raw_link, body)

    def test_quick_builder_hybrid_panel_and_inventory_pool_creates_one_cup(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Hybrid Quick Pool")
        inventory_link = self.direct_link(216, host="hybrid-inventory.example.com")
        panel_link = self.direct_link(217, host="hybrid-panel.example.com")
        import_config_assets(pool, inventory_link)
        adapter = self.quick_builder_adapter(direct_link=panel_link)
        self.client.force_login(self.admin_user)

        with patch("store.admin_cup_center.services.get_safe_panel_adapter", return_value=adapter):
            response = self.client.post(
                reverse("admin_store_cup_center_quick_build"),
                {
                    "title": "Hybrid quick cup",
                    "inbounds": [self.inbound.pk],
                    "inventory_pools": [pool.pk],
                    "inventory_quantity": "1",
                    "volume_gb": "10",
                    "duration_days": "30",
                    "device_limit": "2",
                    "remark_prefix": "qasedak-cup-hybrid",
                    "confirm_remote_create": "on",
                },
            )

        cup = SubscriptionCup.objects.get(title="Hybrid quick cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.items.count(), 2)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup).count(), 1)
        self.assertCountEqual(
            list(ConfigLink.objects.filter(cup_items__cup=cup).values_list("source_type", flat=True)),
            [ConfigLink.SourceType.PANEL_GENERATED, ConfigLink.SourceType.IMPORTED_SUBSCRIPTION],
        )
        adapter.create_enabled_client.assert_called_once()

        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, self.panel.name)
        self.assertContains(result_response, pool.title)
        self.assertNotIn(panel_link, result_body)
        self.assertNotIn(inventory_link, result_body)

        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertCountEqual(raw_response.content.decode("utf-8").splitlines(), [panel_link, inventory_link])

    def test_quick_builder_inventory_shortage_returns_structured_safe_result(self):
        pool = self.create_inventory_pool(title="Empty Quick Pool")
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_quick_build"),
            {
                "title": "Shortage quick cup",
                "inventory_pools": [pool.pk],
                "inventory_quantity": "2",
                "volume_gb": "10",
                "duration_days": "30",
                "device_limit": "2",
                "remark_prefix": "qasedak-cup-shortage",
            },
        )

        cup = SubscriptionCup.objects.get(title="Shortage quick cup")
        self.assertRedirects(response, reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        self.assertEqual(cup.items.count(), 0)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup).count(), 0)
        self.assertEqual(cup.metadata["status"], "failed")

        result_response = self.client.get(reverse("admin_store_cup_center_quick_result", args=[cup.pk]))
        result_body = result_response.content.decode("utf-8")
        self.assertContains(result_response, "config_inventory_insufficient_stock")
        self.assertContains(result_response, "Ù…ÙˆØ¬ÙˆØ¯ÛŒ Ù…Ø®Ø²Ù† Ú©Ø§Ù†ÙÛŒÚ¯")
        self.assertNotIn("vless://", result_body)

    def create_inventory_pool(self, **kwargs):
        defaults = {
            "title": f"Pool {ConfigInventoryPool.objects.count() + 1}",
            "allocation_mode": ConfigInventoryPool.AllocationMode.EXCLUSIVE,
            "is_active": True,
        }
        defaults.update(kwargs)
        return ConfigInventoryPool.objects.create(**defaults)

    def create_fulfillment_order(self, *, plan=None):
        return Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=plan or self.plan,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
            is_paid=True,
            username="alice-recipe",
        )

    def create_inventory_recipe(self, pool, *, quantity=1, failure_policy=None, plan=None):
        recipe = CupFulfillmentRecipe.objects.create(
            plan=plan or self.plan,
            title="Inventory recipe",
            failure_policy=failure_policy or CupFulfillmentRecipe.FailurePolicy.STRICT,
        )
        CupFillerRule.objects.create(
            recipe=recipe,
            position=1,
            source_type=CupFillerRule.SourceType.INVENTORY_POOL,
            quantity=quantity,
            inventory_pool=pool,
            required=True,
        )
        return recipe

    def test_inventory_import_allows_duplicate_config_assets(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool()
        raw_link = self.direct_link(201)

        result = import_config_assets(pool, "\n".join([raw_link, raw_link, "not-a-config"]))

        self.assertEqual(result.created_count, 2)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(ConfigInventoryAsset.objects.filter(pool=pool, raw_link=raw_link).count(), 2)
        self.assertEqual(ConfigInventoryAsset.objects.filter(pool=pool).values("normalized_hash").distinct().count(), 1)

    def test_imported_assets_default_capacity_by_pool_mode(self):
        from .config_inventory_services import import_config_assets

        exclusive_pool = self.create_inventory_pool(title="Exclusive Defaults")
        limited_pool = self.create_inventory_pool(
            title="Limited Defaults",
            allocation_mode=ConfigInventoryPool.AllocationMode.SHARED_LIMITED,
            max_allocations_per_asset=3,
        )
        unlimited_pool = self.create_inventory_pool(
            title="Unlimited Defaults",
            allocation_mode=ConfigInventoryPool.AllocationMode.SHARED_UNLIMITED,
        )

        import_config_assets(exclusive_pool, self.direct_link(237, host="exclusive-default.example.com"))
        import_config_assets(limited_pool, self.direct_link(238, host="limited-default.example.com"))
        import_config_assets(unlimited_pool, self.direct_link(239, host="unlimited-default.example.com"))

        exclusive_asset = ConfigInventoryAsset.objects.get(pool=exclusive_pool)
        limited_asset = ConfigInventoryAsset.objects.get(pool=limited_pool)
        unlimited_asset = ConfigInventoryAsset.objects.get(pool=unlimited_pool)
        self.assertEqual(exclusive_asset.status, ConfigInventoryAsset.Status.AVAILABLE)
        self.assertEqual(exclusive_asset.max_allocations, 1)
        self.assertEqual(exclusive_asset.current_allocations, 0)
        self.assertEqual(limited_asset.max_allocations, 3)
        self.assertEqual(limited_asset.current_allocations, 0)
        self.assertIsNone(unlimited_asset.max_allocations)
        self.assertEqual(unlimited_asset.current_allocations, 0)

    def test_shared_limited_null_capacity_behaves_as_unlimited(self):
        from .config_inventory_services import allocate_assets_from_pool, get_pool_stock_summary, import_config_assets

        pool = self.create_inventory_pool(
            title="Limited Null Defaults",
            allocation_mode=ConfigInventoryPool.AllocationMode.SHARED_LIMITED,
            max_allocations_per_asset=None,
        )
        import_config_assets(pool, self.direct_link(240, host="limited-null-default.example.com"))
        asset = ConfigInventoryAsset.objects.get(pool=pool)

        result = allocate_assets_from_pool(pool, 2, order=self.order)

        self.assertEqual(result.allocated_count, 2)
        self.assertEqual(len({allocated_asset.pk for allocated_asset in result.assets}), 1)
        asset.refresh_from_db()
        self.assertIsNone(asset.max_allocations)
        self.assertEqual(asset.current_allocations, 2)
        self.assertIsNone(get_pool_stock_summary(pool)["available_capacity"])

    def test_shared_limited_respects_imported_capacity(self):
        from .config_inventory_services import InsufficientInventoryStock, allocate_assets_from_pool, import_config_assets

        pool = self.create_inventory_pool(
            title="Limited Capacity Defaults",
            allocation_mode=ConfigInventoryPool.AllocationMode.SHARED_LIMITED,
            max_allocations_per_asset=2,
        )
        import_config_assets(pool, self.direct_link(241, host="limited-capacity-default.example.com"))
        asset = ConfigInventoryAsset.objects.get(pool=pool)

        result = allocate_assets_from_pool(pool, 2, order=self.order)

        self.assertEqual(result.allocated_count, 2)
        asset.refresh_from_db()
        self.assertEqual(asset.max_allocations, 2)
        self.assertEqual(asset.current_allocations, 2)
        with self.assertRaises(InsufficientInventoryStock):
            allocate_assets_from_pool(pool, 1, order=self.order)

    def test_subscription_url_import_decodes_base64_and_preserves_raw_links(self):
        from .config_inventory_services import import_config_assets_from_subscription_url

        pool = self.create_inventory_pool()
        links = [
            self.reality_link(index=303, host="supplier-reality.example.com"),
            self.direct_link(304, host="supplier-vless.example.com"),
        ]
        encoded = base64.b64encode(("\n".join(links) + "\n").encode("utf-8")).decode("ascii")

        with patch("store.config_inventory_services._fetch_subscription_content", return_value=encoded):
            result = import_config_assets_from_subscription_url(
                pool,
                "https://supplier.example.com/sub/private-supplier-token?token=secret-token",
                source_batch="supplier-b64",
                timeout=10,
            )

        self.assertTrue(result.fetched)
        self.assertTrue(result.decoded_as_base64)
        self.assertEqual(result.created_count, 2)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(list(ConfigInventoryAsset.objects.filter(pool=pool).order_by("pk").values_list("raw_link", flat=True)), links)
        self.assertNotIn("private-supplier-token", result.source_url_masked)
        self.assertNotIn("secret-token", result.source_url_masked)

    def test_subscription_url_import_accepts_raw_subscription_and_allows_duplicates(self):
        from .config_inventory_services import import_config_assets_from_subscription_url

        pool = self.create_inventory_pool()
        raw_link = self.direct_link(305, host="supplier-raw.example.com")
        payload = "\n".join(["# supplier comment", raw_link, raw_link, "not-a-config"])

        with patch("store.config_inventory_services._fetch_subscription_content", return_value=payload):
            result = import_config_assets_from_subscription_url(pool, "https://supplier.example.com/sub/raw-token")

        self.assertTrue(result.fetched)
        self.assertFalse(result.decoded_as_base64)
        self.assertEqual(result.created_count, 2)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(ConfigInventoryAsset.objects.filter(pool=pool, raw_link=raw_link).count(), 2)

    def test_subscription_url_import_accepts_urlsafe_base64_without_padding(self):
        from .config_inventory_services import import_config_assets_from_subscription_url

        pool = self.create_inventory_pool()
        links = [
            self.direct_link(306, host="supplier-url-safe-a.example.com"),
            self.direct_link(307, host="supplier-url-safe-b.example.com"),
        ]
        encoded = base64.urlsafe_b64encode(("\n".join(links) + "\n").encode("utf-8") + b"\xff\xff").decode("ascii").rstrip("=")

        with patch("store.config_inventory_services._fetch_subscription_content", return_value=encoded):
            result = import_config_assets_from_subscription_url(pool, "https://supplier.example.com/sub/url-safe-token")

        self.assertTrue(result.decoded_as_base64)
        self.assertEqual(result.response_type, "base64_urlsafe")
        self.assertEqual(result.configs_found, 2)
        self.assertEqual(list(ConfigInventoryAsset.objects.filter(pool=pool).order_by("pk").values_list("raw_link", flat=True)), links)

    def test_subscription_url_import_extracts_links_embedded_in_html(self):
        from .config_inventory_services import import_config_assets_from_subscription_url

        pool = self.create_inventory_pool()
        raw_link = self.reality_link(index=308, host="supplier-html.example.com")
        html_payload = f'<html><body><a href="{raw_link.replace("&", "&amp;")}">import</a></body></html>'

        with patch("store.config_inventory_services._fetch_subscription_content", return_value=html_payload):
            result = import_config_assets_from_subscription_url(pool, "https://supplier.example.com/sub/html-token")

        self.assertEqual(result.response_type, "html_embedded")
        self.assertEqual(result.configs_found, 1)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(ConfigInventoryAsset.objects.get(pool=pool).raw_link, raw_link)

    def test_subscription_url_fetch_retries_browser_html_with_client_headers(self):
        from .config_inventory_services import import_config_assets_from_subscription_url

        class FakeHTTPResponse:
            def __init__(self, text):
                self.payload = text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, _limit):
                return self.payload

        pool = self.create_inventory_pool()
        raw_link = self.direct_link(309, host="retry-client-headers.example.com")

        with patch(
            "store.config_inventory_services.urlopen",
            side_effect=[FakeHTTPResponse("<html><body>dashboard</body></html>"), FakeHTTPResponse(raw_link)],
        ) as mocked_urlopen:
            result = import_config_assets_from_subscription_url(pool, "https://supplier.example.com/sub/retry-token")

        self.assertEqual(mocked_urlopen.call_count, 2)
        first_request = mocked_urlopen.call_args_list[0].args[0]
        second_request = mocked_urlopen.call_args_list[1].args[0]
        self.assertIn("v2rayNG", first_request.get_header("User-agent") or first_request.get_header("User-Agent"))
        self.assertIn("Hiddify", second_request.get_header("User-agent") or second_request.get_header("User-Agent"))
        self.assertEqual(result.created_count, 1)
        self.assertEqual(ConfigInventoryAsset.objects.get(pool=pool).raw_link, raw_link)

    def test_subscription_url_import_browser_html_without_configs_warns_safely(self):
        from .config_inventory_services import import_config_assets_from_subscription_url

        pool = self.create_inventory_pool()
        supplier_url = "https://supplier.example.com/sub/shorttok?token=hidden-token"

        with patch("store.config_inventory_services._fetch_subscription_content", return_value="<html><body>dashboard only</body></html>"):
            result = import_config_assets_from_subscription_url(pool, supplier_url)

        self.assertTrue(result.fetched)
        self.assertEqual(result.response_type, "html_no_configs")
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.configs_found, 0)
        self.assertIn("HTML", result.warnings[0])
        self.assertNotIn("shorttok", result.source_url_masked)
        self.assertNotIn("hidden-token", result.source_url_masked)

    def test_subscription_url_import_invalid_url_returns_safe_error(self):
        from .config_inventory_services import import_config_assets_from_subscription_url

        pool = self.create_inventory_pool()

        result = import_config_assets_from_subscription_url(pool, "not-a-subscription-url")

        self.assertFalse(result.fetched)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(ConfigInventoryAsset.objects.filter(pool=pool).count(), 0)
        self.assertIn("http or https", result.errors[0])
        self.assertNotIn("not-a-subscription-url", result.errors[0])

    def test_exclusive_inventory_allocation_marks_asset_assigned(self):
        from .config_inventory_services import allocate_assets_from_pool, import_config_assets

        pool = self.create_inventory_pool()
        import_config_assets(pool, self.direct_link(202))
        cup = SubscriptionCup.objects.create(order=self.order, plan=self.plan)

        result = allocate_assets_from_pool(pool, 1, cup=cup, order=self.order)

        asset = result.assets[0]
        asset.refresh_from_db()
        self.assertEqual(result.allocated_count, 1)
        self.assertEqual(asset.status, ConfigInventoryAsset.Status.ASSIGNED)
        self.assertEqual(asset.current_allocations, 1)
        self.assertEqual(ConfigAllocation.objects.get(asset=asset).cup, cup)

    def test_shared_unlimited_inventory_can_allocate_same_asset_multiple_times(self):
        from .config_inventory_services import allocate_assets_from_pool, import_config_assets

        pool = self.create_inventory_pool(allocation_mode=ConfigInventoryPool.AllocationMode.SHARED_UNLIMITED)
        import_config_assets(pool, self.direct_link(203))

        result = allocate_assets_from_pool(pool, 2, order=self.order)

        self.assertEqual(result.allocated_count, 2)
        self.assertEqual(len({asset.pk for asset in result.assets}), 1)
        asset = result.assets[0]
        asset.refresh_from_db()
        self.assertEqual(asset.current_allocations, 2)

    def test_inventory_allocation_insufficient_stock_gives_safe_error(self):
        from .config_inventory_services import InsufficientInventoryStock, allocate_assets_from_pool, import_config_assets

        pool = self.create_inventory_pool()
        import_config_assets(pool, self.direct_link(204))

        with self.assertRaises(InsufficientInventoryStock) as raised:
            allocate_assets_from_pool(pool, 2)

        message = raised.exception.safe_message
        self.assertIn("insufficient stock", message)
        self.assertNotIn("vless://", message)
        self.assertNotIn("aaaaaaaa-aaaa", message)

    def test_plan_without_recipe_is_not_intercepted_and_old_activation_still_runs(self):
        legacy_panel = Panel.objects.create(
            store=self.store,
            name="Legacy Panel",
            url="https://legacy.example.com",
            username="admin",
            password="panel-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.LEGACY_SINGLE_NODE,
        )
        legacy_inbound = Inbound.objects.create(
            panel=legacy_panel,
            inbound_id=8,
            remark="Legacy",
            protocol=Inbound.Protocol.VLESS,
            server_ip="legacy.example.com",
            port="443",
            config_params="{}",
            is_active=True,
        )
        order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=legacy_inbound,
            status=Order.Status.PENDING_VERIFICATION,
            verification_status=Order.VerificationStatus.PENDING,
            is_paid=True,
            username="legacy-order",
            uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            sub_link="https://legacy.example.com/sub/legacy",
            direct_link=self.direct_link(205, host="legacy.example.com"),
        )
        VPNClient.objects.create(
            store=self.store,
            order=order,
            plan=self.plan,
            inbound=legacy_inbound,
            username=order.username,
            xui_email=order.username,
            uuid=order.uuid,
            sub_id="legacy",
            sub_link=order.sub_link,
            direct_link=order.direct_link,
            status=VPNClient.Status.INACTIVE,
            traffic_limit_bytes=self.plan.traffic_limit_bytes,
            duration_days=self.plan.duration_days,
            device_limit=self.plan.device_limit,
        )

        with patch("store.order_actions.enable_client", return_value=True):
            result = activate_order(order, notify=False)

        order.refresh_from_db()
        self.assertTrue(result.success)
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.PROVISIONED)
        self.assertNotIn("cup_fulfillment_recipe", order.metadata)

    def test_inventory_only_recipe_creates_cup_and_cup_items(self):
        from .config_inventory_services import import_config_assets
        from .cup_fulfillment_services import fulfill_order_with_recipe
        from .telegram_bot.order_delivery import order_config_link_groups

        pool = self.create_inventory_pool()
        links = [self.direct_link(206), self.direct_link(207)]
        import_config_assets(pool, "\n".join(links))
        self.create_inventory_recipe(pool, quantity=2)
        order = self.create_fulfillment_order()

        result = fulfill_order_with_recipe(order)

        order.refresh_from_db()
        self.assertEqual(result.status, "success")
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(result.cup.items.count(), 2)
        self.assertCountEqual(
            list(result.cup.items.values_list("config_link__raw_link", flat=True)),
            links,
        )
        groups = order_config_link_groups(order)
        self.assertEqual(groups[0]["subscription_link"], "")
        self.assertIn("/sub/", groups[0]["project_subscription_link"])
        self.assertIn("?format=base64", groups[0]["project_client_link"])

    def test_hybrid_recipe_creates_panel_and_inventory_links_in_one_cup(self):
        from .config_inventory_services import import_config_assets
        from .cup_fulfillment_services import fulfill_order_with_recipe

        pool = self.create_inventory_pool()
        inventory_link = self.direct_link(208, host="inventory.example.com")
        panel_link = self.direct_link(209, host="panel-generated.example.com")
        import_config_assets(pool, inventory_link)
        recipe = CupFulfillmentRecipe.objects.create(plan=self.plan, title="Hybrid recipe")
        panel_rule = CupFillerRule.objects.create(
            recipe=recipe,
            position=1,
            source_type=CupFillerRule.SourceType.PANEL_INBOUNDS,
            quantity=1,
            panel=self.panel,
            required=True,
        )
        panel_rule.inbounds.add(self.inbound)
        CupFillerRule.objects.create(
            recipe=recipe,
            position=2,
            source_type=CupFillerRule.SourceType.INVENTORY_POOL,
            quantity=1,
            inventory_pool=pool,
            required=True,
        )
        adapter = self.quick_builder_adapter(direct_link=panel_link)
        order = self.create_fulfillment_order()

        result = fulfill_order_with_recipe(order, adapter_factory=lambda panel: adapter)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.cup.items.count(), 2)
        self.assertEqual(result.panel_generated_count, 1)
        self.assertEqual(result.inventory_allocation_count, 1)
        self.assertCountEqual(
            list(result.cup.items.values_list("config_link__raw_link", flat=True)),
            [panel_link, inventory_link],
        )
        adapter.create_enabled_client.assert_called_once()

    def test_required_rule_failure_strict_marks_fulfillment_failed(self):
        from .cup_fulfillment_services import fulfill_order_with_recipe

        pool = self.create_inventory_pool()
        self.create_inventory_recipe(pool, quantity=1)
        order = self.create_fulfillment_order()

        result = fulfill_order_with_recipe(order)

        order.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.FAILED)
        self.assertIn("insufficient stock", order.last_provisioning_error)
        self.assertEqual(result.cup.items.count(), 0)

    def test_config_inventory_admin_pages_load_for_staff(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool()
        import_config_assets(pool, self.direct_link(210))
        recipe = self.create_inventory_recipe(pool, quantity=1)
        self.client.force_login(self.admin_user)

        urls = [
            reverse("admin_store_config_inventory"),
            reverse("admin_store_config_inventory_import"),
            reverse("admin:store_configinventorypool_changelist"),
            reverse("admin:store_configinventoryasset_changelist"),
            reverse("admin:store_configallocation_changelist"),
            reverse("admin:store_cupfulfillmentrecipe_changelist"),
            reverse("admin:store_cupfillerrule_changelist"),
            reverse("admin:store_configinventorypool_import"),
            reverse("admin:store_cupfulfillmentrecipe_preview", args=[recipe.pk]),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_config_inventory_jazzmin_menu_has_clear_labels(self):
        menu_items = settings.JAZZMIN_SETTINGS["custom_links"]["Ø§Ù†Ø¨Ø§Ø± Ú©Ø§Ù†ÙÛŒÚ¯â€ŒÙ‡Ø§"]
        labels = {str(item.get("name")) for item in menu_items}

        self.assertIn("Ø¯Ø§Ø´Ø¨ÙˆØ±Ø¯ Ø§Ù†Ø¨Ø§Ø±", labels)
        self.assertIn("Ù…Ø®Ø²Ù†â€ŒÙ‡Ø§ÛŒ Ú©Ø§Ù†ÙÛŒÚ¯", labels)
        self.assertIn("ÙˆØ§Ø±Ø¯ Ú©Ø±Ø¯Ù† Ú©Ø§Ù†ÙÛŒÚ¯", labels)
        self.assertIn("Ú©Ø§Ù†ÙÛŒÚ¯â€ŒÙ‡Ø§ÛŒ Ø¢Ù…Ø§Ø¯Ù‡", labels)
        self.assertIn("ØªØ®ØµÛŒØµâ€ŒÙ‡Ø§ÛŒ Ú©Ø§Ù†ÙÛŒÚ¯", labels)
        self.assertIn("Ø¯Ø³ØªÙˆØ±Ù‡Ø§ÛŒ Ù¾Ø± Ú©Ø±Ø¯Ù† Cup", labels)
        self.assertIn("Ù‚ÙˆØ§Ù†ÛŒÙ† Ù¾Ø±Ú©Ù†Ù†Ø¯Ù‡ Cup", labels)
        self.assertNotIn("Ø§Ù†Ø¨Ø§Ø± Ú©Ø§Ù†ÙÛŒÚ¯â€ŒÙ‡Ø§", labels)

    def test_config_inventory_dashboard_shows_action_cards(self):
        pool = self.create_inventory_pool(title="Dashboard Stock")
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin_store_config_inventory"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ø§Ù†Ø¨Ø§Ø± Ú©Ø§Ù†ÙÛŒÚ¯â€ŒÙ‡Ø§")
        self.assertContains(response, "Ø³Ø§Ø®Øª Ù…Ø®Ø²Ù† Ø¬Ø¯ÛŒØ¯")
        self.assertContains(response, "ÙˆØ§Ø±Ø¯ Ú©Ø±Ø¯Ù† Ú©Ø§Ù†ÙÛŒÚ¯")
        self.assertContains(response, "Ú©Ø§Ù†ÙÛŒÚ¯â€ŒÙ‡Ø§ÛŒ Ø¢Ù…Ø§Ø¯Ù‡")
        self.assertContains(response, "Ø³Ø§Ø®Øª Ø³Ø±ÛŒØ¹ Cup Ø§Ø² Ù…Ø®Ø²Ù†/Ù¾Ù†Ù„")
        self.assertContains(response, "Ø³Ø§Ø®Øª Recipe Ø¨Ø±Ø§ÛŒ Ù¾Ù„Ù†")
        self.assertContains(response, pool.title)

    def test_config_inventory_import_page_creates_assets_and_masks_result(self):
        pool = self.create_inventory_pool(title="Import Page Stock")
        raw_link = self.direct_link(211, host="admin-import-secret.example.com")
        duplicate_link = self.direct_link(212, host="admin-import-duplicate.example.com")
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_config_inventory_import"),
            {
                "pool": pool.pk,
                "source_batch": "admin-import-test",
                "raw_text": "\n".join([raw_link, duplicate_link, duplicate_link, "not-a-config"]),
            },
        )
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ConfigInventoryAsset.objects.filter(pool=pool).count(), 3)
        self.assertContains(response, "ÙˆØ§Ø±Ø¯ Ú©Ø±Ø¯Ù† Ù„ÛŒÙ†Ú©â€ŒÙ‡Ø§ÛŒ Ú©Ø§Ù†ÙÛŒÚ¯")
        self.assertContains(response, "ÙˆØ§Ø±Ø¯ Ú©Ø±Ø¯Ù† Ø§Ø² Ù„ÛŒÙ†Ú© Subscription")
        self.assertContains(response, "Ø®Ù„Ø§ØµÙ‡ ÙˆØ§Ø±Ø¯ Ú©Ø±Ø¯Ù†")
        self.assertContains(response, "ØªØ¹Ø¯Ø§Ø¯ Ø®Ø·ÙˆØ·")
        self.assertContains(response, "Ø§ÛŒØ¬Ø§Ø¯ Ø´Ø¯Ù‡")
        self.assertContains(response, "ØªÚ©Ø±Ø§Ø±ÛŒ ØªØ´Ø®ÛŒØµ Ø¯Ø§Ø¯Ù‡ Ø´Ø¯Ù‡")
        self.assertContains(response, "Ù…ÙˆØ¬ÙˆØ¯ÛŒ ÙØ¹Ù„ÛŒ Ù…Ø®Ø²Ù†")
        self.assertContains(response, "Ø³Ø§Ø®Øª Ø³Ø±ÛŒØ¹ Cup Ø¨Ø§ Ø§ÛŒÙ† Ù…Ø®Ø²Ù†")
        self.assertContains(response, f"{reverse('admin_store_cup_center_quick_build')}?inventory_pool={pool.pk}")
        self.assertNotIn(raw_link, body)
        self.assertNotIn(duplicate_link, body)
        self.assertNotIn("vless://aaaaaaaa", body)
        self.assertNotIn("admin-import-secret.example.com", body)

    def test_config_inventory_admin_imports_supplier_subscription_url_safely(self):
        pool = self.create_inventory_pool(title="Admin Supplier Stock")
        raw_link = self.direct_link(218, host="admin-supplier-secret.example.com")
        supplier_url = "https://supplier.example.com/sub/admin-private-token?token=admin-secret-token"
        self.client.force_login(self.admin_user)

        with patch("store.config_inventory_services._fetch_subscription_content", return_value=raw_link):
            response = self.client.post(
                reverse("admin_store_config_inventory_import"),
                {
                    "pool": pool.pk,
                    "source_batch": "admin-supplier",
                    "import_mode": "subscription_url",
                    "subscription_url": supplier_url,
                    "fetch_timeout": "10",
                },
            )
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ConfigInventoryAsset.objects.filter(pool=pool, raw_link=raw_link).count(), 1)
        self.assertContains(response, "Ø¯Ø±ÛŒØ§ÙØª Ø´Ø¯")
        self.assertContains(response, "Ú©Ø§Ù†ÙÛŒÚ¯ Ù¾ÛŒØ¯Ø§ Ø´Ø¯Ù‡")
        self.assertContains(response, "Ø³Ø§Ø®Øª Ø³Ø±ÛŒØ¹ Cup Ø¨Ø§ Ø§ÛŒÙ† Ù…Ø®Ø²Ù†")
        self.assertContains(response, "Ø§ØªØµØ§Ù„ Ù…Ø®Ø²Ù† Ø¨Ù‡ Recipe Ù¾Ù„Ù†")
        self.assertContains(response, "Ù…Ø´Ø§Ù‡Ø¯Ù‡ Ú©Ø§Ù†ÙÛŒÚ¯â€ŒÙ‡Ø§ÛŒ Ø§ÛŒÙ† Ù…Ø®Ø²Ù†")
        self.assertNotIn(raw_link, body)
        self.assertNotIn("admin-private-token", body)
        self.assertNotIn("admin-secret-token", body)

    def test_config_inventory_pool_admin_has_import_links_on_list_and_detail(self):
        pool = self.create_inventory_pool(title="Pool Admin Stock")
        self.client.force_login(self.admin_user)

        changelist = self.client.get(reverse("admin:store_configinventorypool_changelist"))
        detail = self.client.get(reverse("admin:store_configinventorypool_change", args=[pool.pk]))

        self.assertEqual(changelist.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(changelist, "ÙˆØ§Ø±Ø¯ Ú©Ø±Ø¯Ù† Ú©Ø§Ù†ÙÛŒÚ¯")
        self.assertContains(changelist, reverse("admin_store_config_inventory_import"))
        self.assertContains(detail, "ÙˆØ§Ø±Ø¯ Ú©Ø±Ø¯Ù† Ú©Ø§Ù†ÙÛŒÚ¯")
        self.assertContains(detail, f"{reverse('admin_store_config_inventory_import')}?pool={pool.pk}")

    def test_config_inventory_asset_admin_masks_links(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Masked Asset Stock")
        raw_link = self.direct_link(213, host="masked-admin-secret.example.com")
        import_config_assets(pool, raw_link)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin:store_configinventoryasset_changelist"))
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vless://&lt;hidden&gt;")
        self.assertNotIn(raw_link, body)
        self.assertNotIn("vless://aaaaaaaa", body)

    def test_config_link_list_masks_full_raw_link_and_detail_can_reveal_for_staff(self):
        from .subscription_cups import create_config_link_from_raw

        raw_link = self.direct_link(223, host="configlink-list-secret.example.com")
        config_link = create_config_link_from_raw(raw_link, source_type=ConfigLink.SourceType.MANUAL)
        self.client.force_login(self.admin_user)

        changelist = self.client.get(reverse("admin:store_configlink_changelist"))
        detail = self.client.get(reverse("admin:store_configlink_change", args=[config_link.pk]))
        list_body = changelist.content.decode("utf-8")
        detail_body = detail.content.decode("utf-8")

        self.assertEqual(changelist.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(changelist, "vless://&lt;hidden&gt;")
        self.assertNotIn(raw_link, list_body)
        self.assertContains(detail, "Ù†Ù…Ø§ÛŒØ´ Ù„ÛŒÙ†Ú© Ú©Ø§Ù…Ù„")
        self.assertContains(detail, "Ú©Ù¾ÛŒ Ù„ÛŒÙ†Ú© Ú©Ø§Ù…Ù„")
        self.assertIn(raw_link, detail_body)

    def test_config_inventory_asset_detail_can_reveal_full_link_for_staff(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Asset Reveal Stock")
        raw_link = self.direct_link(224, host="asset-detail-secret.example.com")
        import_config_assets(pool, raw_link)
        asset = ConfigInventoryAsset.objects.get(pool=pool)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin:store_configinventoryasset_change", args=[asset.pk]))
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ù†Ù…Ø§ÛŒØ´ Ù„ÛŒÙ†Ú© Ú©Ø§Ù…Ù„")
        self.assertContains(response, "Ú©Ù¾ÛŒ Ù„ÛŒÙ†Ú© Ù…Ø§Ø³Ú©â€ŒØ´Ø¯Ù‡")
        self.assertIn(raw_link, body)

    def test_config_link_raw_link_admin_edit_updates_subscription_output(self):
        cup = self.create_cup_with_links(self.direct_link(225, host="raw-edit-old.example.com"))
        config_link = ConfigLink.objects.get(cup_items__cup=cup)
        new_link = self.direct_link(226, host="raw-edit-new.example.com")
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin:store_configlink_change", args=[config_link.pk]),
            {
                "raw_link": f"  {new_link}  ",
                "remark": "Edited Display",
                "source_type": config_link.source_type,
                "source_panel": config_link.source_panel_id or "",
                "source_inbound": config_link.source_inbound_id or "",
                "vpn_client": config_link.vpn_client_id or "",
                "is_active": "on",
                "metadata": "{}",
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        config_link.refresh_from_db()
        self.assertEqual(config_link.raw_link, new_link)
        self.assertEqual(config_link.host, "raw-edit-new.example.com")
        self.assertEqual(config_link.remark, "Edited Display")
        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertEqual(raw_response.content.decode("utf-8"), new_link)

    def test_cup_detail_display_name_edit_appears_in_subscription_dashboard(self):
        raw_link = self.direct_link(227, host="display-name-dashboard.example.com")
        cup = self.create_cup_with_links(raw_link)
        item = cup.items.select_related("config_link").get()
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_detail", args=[cup.pk]),
            {
                "action": "update_item_display_name",
                "item_id": item.pk,
                "display_name": "Dashboard Visible Name",
            },
        )

        self.assertRedirects(response, reverse("admin_store_cup_center_detail", args=[cup.pk]))
        dashboard = self.client.get(reverse("subscription_cup", args=[cup.token]), **self.browser_headers())
        self.assertContains(dashboard, "Dashboard Visible Name")

    def test_non_staff_cannot_access_inventory_picker_or_link_editor(self):
        cup = self.create_cup_with_links(self.direct_link(228, host="nonstaff-editor.example.com"))
        config_link = ConfigLink.objects.get(cup_items__cup=cup)
        user = get_user_model().objects.create_user(username="not-staff", password="secret")
        self.client.force_login(user)

        picker = self.client.get(reverse("admin_store_cup_center_add_inventory", args=[cup.pk]))
        quick_builder = self.client.get(reverse("admin_store_cup_center_quick_build"))
        editor = self.client.get(reverse("admin:store_configlink_change", args=[config_link.pk]))

        self.assertNotEqual(picker.status_code, 200)
        self.assertNotEqual(quick_builder.status_code, 200)
        self.assertNotEqual(editor.status_code, 200)

    def test_existing_cup_inventory_picker_adds_selected_asset_as_cupitem(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Existing Cup Picker Stock")
        raw_link = self.direct_link(236, host="existing-cup-picker.example.com")
        import_config_assets(pool, raw_link)
        asset = ConfigInventoryAsset.objects.get(pool=pool)
        cup = SubscriptionCup.objects.create(title="Existing cup picker")
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_add_inventory", args=[cup.pk]),
            {
                "pool": pool.pk,
                "asset_ids": [asset.pk],
            },
        )

        self.assertRedirects(response, reverse("admin_store_cup_center_detail", args=[cup.pk]))
        self.assertEqual(cup.items.count(), 1)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup, asset=asset).count(), 1)
        self.assertEqual(ConfigLink.objects.get(cup_items__cup=cup).raw_link, raw_link)

    def test_inventory_picker_rejects_disabled_and_expired_assets(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Rejected Asset Stock")
        disabled_link = self.direct_link(229, host="disabled-asset.example.com")
        expired_link = self.direct_link(230, host="expired-asset.example.com")
        import_config_assets(pool, "\n".join([disabled_link, expired_link]))
        disabled_asset = ConfigInventoryAsset.objects.get(raw_link=disabled_link)
        expired_asset = ConfigInventoryAsset.objects.get(raw_link=expired_link)
        disabled_asset.status = ConfigInventoryAsset.Status.DISABLED
        disabled_asset.save(update_fields=["status", "updated_at"])
        expired_asset.expires_at = timezone.now() - timedelta(days=1)
        expired_asset.save(update_fields=["expires_at", "updated_at"])
        cup = SubscriptionCup.objects.create(title="Reject disabled expired")
        self.client.force_login(self.admin_user)

        for asset, code in ((disabled_asset, "asset_not_usable"), (expired_asset, "asset_expired")):
            response = self.client.post(
                reverse("admin_store_cup_center_add_inventory", args=[cup.pk]),
                {
                    "pool": pool.pk,
                    f"inventory_asset_ids_{pool.pk}": [asset.pk],
                },
            )
            body = response.content.decode("utf-8")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, code)
            self.assertNotIn(asset.raw_link, body)

        self.assertEqual(cup.items.count(), 0)
        self.assertEqual(ConfigAllocation.objects.filter(cup=cup).count(), 0)

    def test_cup_item_disable_removes_it_from_subscription_output(self):
        first_link = self.direct_link(231, host="disable-first.example.com")
        second_link = self.direct_link(232, host="disable-second.example.com")
        cup = self.create_cup_with_links(first_link, second_link)
        item = cup.items.order_by("position").first()
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_cup_center_detail", args=[cup.pk]),
            {"action": "disable_item", "item_id": item.pk},
        )

        self.assertRedirects(response, reverse("admin_store_cup_center_detail", args=[cup.pk]))
        raw_response = self.client.get(reverse("subscription_cup", args=[cup.token]), {"format": "raw"})
        self.assertEqual(raw_response.content.decode("utf-8"), second_link)

    def test_config_inventory_recipe_preview_page_loads(self):
        pool = self.create_inventory_pool()
        recipe = self.create_inventory_recipe(pool, quantity=1)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin:store_cupfulfillmentrecipe_preview", args=[recipe.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ù¾ÛŒØ´â€ŒÙ†Ù…Ø§ÛŒØ´ Ø¯Ø³ØªÙˆØ± ØªØ­ÙˆÛŒÙ„")
        self.assertContains(response, "Ø§Ú¯Ø± Ø§ÛŒÙ† Ù¾Ù„Ù† Ø§Ù„Ø§Ù† ÙØ±ÙˆØ®ØªÙ‡ Ø´ÙˆØ¯")

    def test_config_inventory_custom_pages_block_non_staff(self):
        pool = self.create_inventory_pool()
        recipe = self.create_inventory_recipe(pool, quantity=1)
        regular_user = get_user_model().objects.create_user(username="inventory-regular", password="secret")
        self.client.force_login(regular_user)

        urls = [
            reverse("admin_store_config_inventory"),
            reverse("admin_store_config_inventory_import"),
            reverse("admin:store_cupfulfillmentrecipe_preview", args=[recipe.pk]),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertNotEqual(response.status_code, 200)

    def test_plan_fulfillment_admin_pages_load_for_staff(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Fulfillment Admin Stock")
        import_config_assets(pool, self.direct_link(310, host="fulfillment-admin.example.com"))
        recipe = self.create_inventory_recipe(pool, quantity=1)
        self.client.force_login(self.admin_user)

        urls = [
            reverse("admin_store_plan_fulfillment"),
            reverse("admin_store_plan_fulfillment_recipe", args=[recipe.pk]),
            reverse("admin_store_plan_fulfillment_recipe_preview", args=[recipe.pk]),
            reverse("admin_store_plan_fulfillment_recipe_simulate", args=[recipe.pk]),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

        dashboard = self.client.get(reverse("admin_store_plan_fulfillment"))
        self.assertContains(dashboard, "Ø¯Ø§Ø´Ø¨ÙˆØ±Ø¯ ØªØ­ÙˆÛŒÙ„ Ù¾Ù„Ù†â€ŒÙ‡Ø§")
        self.assertContains(dashboard, "ÙˆÛŒØ±Ø§ÛŒØ´ ØªØ­ÙˆÛŒÙ„ Ø¯Ø± Products / Plans")
        self.assertContains(dashboard, "ØªØ³Øª Ø´Ø¨ÛŒÙ‡â€ŒØ³Ø§Ø²ÛŒ ØªØ­ÙˆÛŒÙ„")
        self.assertEqual(self.client.get(reverse("admin_store_plan_fulfillment_plans")).status_code, 302)
        self.assertEqual(self.client.get(reverse("admin_store_plan_fulfillment_plan", args=[self.plan.pk])).status_code, 302)

    def test_plan_fulfillment_plan_url_redirects_to_catalog_editor(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("admin_store_plan_fulfillment_plan", args=[self.plan.pk]),
            {"fulfillment_type": "cup"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('admin_store_catalog_plan_edit', args=[self.plan.pk])}#delivery")
        self.assertFalse(CupFulfillmentRecipe.objects.filter(plan=self.plan, title="Wizard recipe").exists())

    def test_plan_fulfillment_preview_flags_reality_missing_pbk(self):
        self.inbound.security = Inbound.Security.REALITY
        self.inbound.pbk = ""
        self.inbound.save(update_fields=["security", "pbk", "updated_at"])
        recipe = CupFulfillmentRecipe.objects.create(plan=self.plan, title="Reality missing pbk recipe")
        rule = CupFillerRule.objects.create(
            recipe=recipe,
            position=1,
            source_type=CupFillerRule.SourceType.PANEL_INBOUNDS,
            quantity=1,
            panel=self.panel,
            required=True,
        )
        rule.inbounds.add(self.inbound)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin_store_plan_fulfillment_recipe_preview", args=[recipe.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ú©Ù„ÛŒØ¯ Ø¹Ù…ÙˆÙ…ÛŒ Reality")
        self.assertContains(response, "ØºÛŒØ±Ù‚Ø§Ø¨Ù„ ÙØ±ÙˆØ´")

    def test_plan_fulfillment_simulation_is_dry_run(self):
        from .config_inventory_services import import_config_assets

        pool = self.create_inventory_pool(title="Simulation Stock")
        import_config_assets(pool, self.direct_link(311, host="simulate-safe.example.com"))
        recipe = self.create_inventory_recipe(pool, quantity=1)
        self.client.force_login(self.admin_user)
        allocation_count = ConfigAllocation.objects.count()
        item_count = CupItem.objects.count()

        response = self.client.get(reverse("admin_store_plan_fulfillment_recipe_simulate", args=[recipe.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dry-run")
        self.assertContains(response, "ConfigAllocation ÙˆØ§Ù‚Ø¹ÛŒ")
        self.assertEqual(ConfigAllocation.objects.count(), allocation_count)
        self.assertEqual(CupItem.objects.count(), item_count)

    def test_plan_fulfillment_pages_block_non_staff(self):
        pool = self.create_inventory_pool(title="Blocked Fulfillment Stock")
        recipe = self.create_inventory_recipe(pool, quantity=1)
        regular_user = get_user_model().objects.create_user(username="fulfillment-regular", password="secret")
        self.client.force_login(regular_user)

        urls = [
            reverse("admin_store_plan_fulfillment"),
            reverse("admin_store_plan_fulfillment_plan", args=[self.plan.pk]),
            reverse("admin_store_plan_fulfillment_recipe_preview", args=[recipe.pk]),
            reverse("admin_store_plan_fulfillment_recipe_simulate", args=[recipe.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertNotEqual(response.status_code, 200)

    def test_plan_admin_contains_fulfillment_buttons_and_badge(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin:store_plan_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ØªØ­ÙˆÛŒÙ„ Ø®ÙˆØ¯Ú©Ø§Ø± Ø³Ø§Ø¨")
        self.assertContains(response, "ØªÙ†Ø¸ÛŒÙ… ØªØ­ÙˆÛŒÙ„")
        self.assertContains(response, "Ø¨Ø¯ÙˆÙ† ØªÙ†Ø¸ÛŒÙ…")

    def test_plan_admin_change_page_shows_service_delivery_cards(self):
        from .config_inventory_services import import_config_assets

        raw_link = self.direct_link(312, host="admin-plan-safe.example.com")
        pool = self.create_inventory_pool(title="Plan Admin Stock")
        import_config_assets(pool, raw_link)
        recipe = self.create_inventory_recipe(pool, quantity=1)
        PlanInboundRoute.objects.create(store=self.store, plan=self.plan, inbound=self.inbound, is_active=True)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin:store_plan_change", args=[self.plan.pk]))
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ØªØ­ÙˆÛŒÙ„ Ø³Ø±ÙˆÛŒØ³")
        self.assertContains(response, "Ø§ØªØµØ§Ù„ Ù…Ø³ØªÙ‚ÛŒÙ… Ø¨Ù‡ Ø§ÛŒÙ†Ø¨Ø§Ù†Ø¯Ù‡Ø§")
        self.assertContains(response, "ØªØ­ÙˆÛŒÙ„ Ø¨Ø§ Ø³Ø§Ø¨ Ø§Ø®ØªØµØ§ØµÛŒ Ù‚Ø§ØµØ¯Ú©")
        self.assertContains(response, "Ø§ÛŒÙ† Ù¾Ù„Ù† Ø¨Ø§ Ø³Ø§Ø¨ Ø§Ø®ØªØµØ§ØµÛŒ Ù‚Ø§ØµØ¯Ú© ØªØ­ÙˆÛŒÙ„ Ø¯Ø§Ø¯Ù‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯.")
        self.assertContains(response, "Ù…Ø¯ÛŒØ±ÛŒØª Ø§ÛŒÙ†Ø¨Ø§Ù†Ø¯Ù‡Ø§ÛŒ Ù¾Ù„Ù†")
        self.assertContains(response, "ØªØ³Øª Ù…Ø³ÛŒØ± Ø§ÛŒÙ†Ø¨Ø§Ù†Ø¯Ù‡Ø§")
        self.assertContains(response, "ÙˆÛŒØ±Ø§ÛŒØ´ ØªØ­ÙˆÛŒÙ„ Ø¯Ø± Plan editor")
        self.assertContains(response, "Ù¾ÛŒØ´â€ŒÙ†Ù…Ø§ÛŒØ´ Ø¢Ù…Ø§Ø¯Ú¯ÛŒ")
        self.assertContains(response, "ØªØ³Øª Ø´Ø¨ÛŒÙ‡â€ŒØ³Ø§Ø²ÛŒ")
        self.assertContains(response, "Ù…Ø´Ø§Ù‡Ø¯Ù‡ Recipe")
        self.assertContains(response, f"{reverse('admin_store_catalog_plan_edit', args=[self.plan.pk])}#delivery")
        self.assertContains(response, reverse("admin_store_plan_fulfillment_recipe", args=[recipe.pk]))
        self.assertContains(response, reverse("admin_store_plan_fulfillment_recipe_preview", args=[recipe.pk]))
        self.assertContains(response, reverse("admin_store_plan_fulfillment_recipe_simulate", args=[recipe.pk]))
        self.assertNotIn(raw_link, body)
        self.assertNotIn("vless://", body)

    def test_plan_admin_change_page_without_recipe_preserves_inbound_path(self):
        PlanInboundRoute.objects.create(store=self.store, plan=self.plan, inbound=self.inbound, is_active=True)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("admin:store_plan_change", args=[self.plan.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ø¨Ø±Ø§ÛŒ Ø§ÛŒÙ† Ù¾Ù„Ù† Ù‡Ù†ÙˆØ² Ø¯Ø³ØªÙˆØ± ØªØ­ÙˆÛŒÙ„ Ø³Ø§Ø¨ ØªØ¹Ø±ÛŒÙ Ù†Ø´Ø¯Ù‡ Ø§Ø³Øª.")
        self.assertContains(response, "Ø§ÛŒÙ† Ù¾Ù„Ù† Ø¨Ø§ Ù…Ø³ÛŒØ± Ù‚Ø¯ÛŒÙ…ÛŒ Ø§ÛŒÙ†Ø¨Ø§Ù†Ø¯ ØªØ­ÙˆÛŒÙ„ Ø¯Ø§Ø¯Ù‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯.")
        self.assertContains(response, "ÙˆÛŒØ±Ø§ÛŒØ´ ØªØ­ÙˆÛŒÙ„ Ø¯Ø± Plan editor")
        self.assertContains(response, reverse("admin_store_panel_center_routing_detail", args=[self.plan.pk]))

    def test_plan_admin_add_page_shows_next_step_and_redirect_button(self):
        self.client.force_login(self.admin_user)
        add_url = reverse("admin:store_plan_add")

        response = self.client.get(add_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ù…Ø±Ø­Ù„Ù‡ Ø¨Ø¹Ø¯ Ø§Ø² Ø³Ø§Ø®Øª Ù¾Ù„Ù†")
        self.assertContains(response, "Ø°Ø®ÛŒØ±Ù‡ Ùˆ ØªÙ†Ø¸ÛŒÙ… ØªØ­ÙˆÛŒÙ„")

        post_response = self.client.post(
            add_url,
            {
                "store": str(self.store.pk),
                "name": "After Save Fulfillment",
                "slug": "after-save-fulfillment",
                "description": "",
                "volume_gb": "5.000",
                "duration_days": "30",
                "price": "120000",
                "currency": Plan.Currency.TOMAN,
                "device_limit": "2",
                "is_active": "on",
                "is_public": "on",
                "sort_order": "0",
                "inbound_routes-TOTAL_FORMS": "0",
                "inbound_routes-INITIAL_FORMS": "0",
                "inbound_routes-MIN_NUM_FORMS": "0",
                "inbound_routes-MAX_NUM_FORMS": "1000",
                "_save_and_configure_fulfillment": "1",
            },
        )

        plan = Plan.objects.get(slug="after-save-fulfillment")
        self.assertRedirects(post_response, f"{reverse('admin_store_catalog_plan_edit', args=[plan.pk])}#delivery", fetch_redirect_response=False)

    def test_plan_admin_list_delivery_columns_and_filters(self):
        pool = self.create_inventory_pool(title="Plan List Stock")
        self.create_inventory_recipe(pool, quantity=1)
        PlanInboundRoute.objects.create(store=self.store, plan=self.plan, inbound=self.inbound, is_active=True)
        self.client.force_login(self.admin_user)
        changelist_url = reverse("admin:store_plan_changelist")

        response = self.client.get(changelist_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ØªØ­ÙˆÛŒÙ„ Ø§ÛŒÙ†Ø¨Ø§Ù†Ø¯")
        self.assertContains(response, "ØªØ­ÙˆÛŒÙ„ Ø³Ø§Ø¨")
        self.assertContains(response, "ÙˆØ¶Ø¹ÛŒØª ØªØ­ÙˆÛŒÙ„")
        self.assertContains(response, "Ù…Ù†Ø§Ø¨Ø¹ ØªØ­ÙˆÛŒÙ„")
        self.assertContains(response, "Recipe ÙØ¹Ø§Ù„")

        for query in (
            {"cup_delivery": "active"},
            {"cup_delivery": "none"},
            {"cup_readiness": "error"},
            {"inbound_delivery": "has"},
            {"inbound_delivery": "none"},
        ):
            with self.subTest(query=query):
                filtered = self.client.get(changelist_url, query)
                self.assertEqual(filtered.status_code, 200)

    def test_reality_missing_pbk_fails_before_panel_create(self):
        from .cup_fulfillment_services import fulfill_order_with_recipe

        self.inbound.security = Inbound.Security.REALITY
        self.inbound.pbk = ""
        self.inbound.save(update_fields=["security", "pbk", "updated_at"])
        recipe = CupFulfillmentRecipe.objects.create(plan=self.plan, title="Reality guarded")
        rule = CupFillerRule.objects.create(
            recipe=recipe,
            position=1,
            source_type=CupFillerRule.SourceType.PANEL_INBOUNDS,
            quantity=1,
            panel=self.panel,
            required=True,
        )
        rule.inbounds.add(self.inbound)
        adapter = self.quick_builder_adapter(direct_link=self.direct_link(312, host="should-not-create.example.com"))
        order = self.create_fulfillment_order()

        result = fulfill_order_with_recipe(order, adapter_factory=lambda panel: adapter)

        self.assertEqual(result.status, "failed")
        adapter.create_enabled_client.assert_not_called()
        self.assertEqual(CupItem.objects.filter(cup=result.cup).count(), 0)


class TelegramProxyTests(TestCase):
    proxy_url = "http://proxy.example:7880"
    expected_proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }

    def test_bot_event_log_redacts_config_links(self):
        from .bots import log_event

        store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        config = BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:test",
            admin_user_id="42",
        )
        vless_link = "vless://11111111-1111-4111-8111-111111111111@example.com:443?type=tcp#private"
        ss_link = "ss://secret@example.com:443#private"
        sub_link = "https://example.com/sub/private-sub-token?secret=1"

        event = log_event(
            config,
            event_type=BotEventLog.EventType.WEBHOOK,
            status=BotEventLog.Status.RECEIVED,
            message=f"User sent {vless_link} and {sub_link}",
            raw_payload={"message": {"text": vless_link}, "links": [ss_link, sub_link]},
        )

        self.assertIn("<config-link-redacted>", event.message)
        self.assertNotIn("vless://", event.message)
        self.assertNotIn("/sub/private-sub-token", event.message)
        payload_text = json.dumps(event.raw_payload, ensure_ascii=False)
        self.assertNotIn("vless://", payload_text)
        self.assertNotIn("ss://", payload_text)
        self.assertNotIn("/sub/private-sub-token", payload_text)
        self.assertIn("<config-link-redacted>", payload_text)

    def test_bot_redaction_sanitizes_keys_nested_values_and_link_tokens(self):
        from .bots import (
            sanitize_bot_event_log_value,
            sanitize_bot_text_for_logging,
            sanitize_bot_update_for_logging,
        )

        vless_link = "vless://11111111-1111-4111-8111-111111111111@example.com:443?type=tcp#private"
        vmess_link = "vmess://encoded-private-payload"
        trojan_link = "trojan://secret@example.com:443#private"
        sub_link = "https://example.com/sub/private-sub-token?secret=1"
        start_link = "https://t.me/vpn_store_bot?start=link_SUPER_SECRET_TOKEN"
        email = "alice.private@example.com"

        event_payload = sanitize_bot_event_log_value(
            {
                vless_link: [
                    {
                        "text": vmess_link,
                        "caption": f"{trojan_link} {sub_link} {start_link}",
                        "metadata": {
                            "email": email,
                            "proxy_password": "proxy-pass-secret",
                            "api_key": "api-key-secret",
                            "nested": [{"session_key": "session-key-secret"}],
                        },
                    }
                ],
                f"callback:{start_link}": "ok",
            }
        )
        update_payload = sanitize_bot_update_for_logging(
            {
                "callback_query": {"data": f"user:lookup:{start_link}"},
                "message": {
                    "text": f"{vmess_link} {start_link}",
                    "entities": [{"url": sub_link}],
                    "document": {
                        "file_id": "telegram-file-id-secret",
                        "file_unique_id": "telegram-file-unique-secret",
                        "file_path": "photos/private.jpg",
                    },
                },
            }
        )

        event_text = json.dumps(event_payload, ensure_ascii=False)
        update_text = json.dumps(update_payload, ensure_ascii=False)
        self.assertIn("<config-link-redacted>", event_payload)
        for raw in [
            "vless://",
            "vmess://",
            "trojan://",
            "/sub/private-sub-token",
            "SUPER_SECRET_TOKEN",
            email,
            "proxy-pass-secret",
            "api-key-secret",
            "session-key-secret",
        ]:
            self.assertNotIn(raw, event_text)
        self.assertIn("link_<redacted>", event_text)
        for raw in [
            "vmess://",
            "/sub/private-sub-token",
            "SUPER_SECRET_TOKEN",
            "telegram-file-id-secret",
            "telegram-file-unique-secret",
            "private.jpg",
        ]:
            self.assertNotIn(raw, update_text)
        self.assertIn("<receipt-file-redacted>", update_text)
        self.assertNotIn("SUPER_SECRET_TOKEN", sanitize_bot_text_for_logging(start_link))
        self.assertNotIn(email, sanitize_bot_text_for_logging(email))

    @override_settings(
        TELEGRAM_PROXY_URL="",
        TELEGRAM_PROXY_PROTOCOL="http",
        TELEGRAM_PROXY_HOST="proxy.example",
        TELEGRAM_PROXY_PORT="7880",
        TELEGRAM_PROXY_USERNAME="",
        TELEGRAM_PROXY_PASSWORD="",
    )
    def test_structured_proxy_settings_build_proxy_url(self):
        from .bot_proxy import sanitized_telegram_proxy_url, telegram_proxy_url

        proxy_url = telegram_proxy_url()

        self.assertEqual(proxy_url, "http://proxy.example:7880")
        self.assertEqual(sanitized_telegram_proxy_url(proxy_url), "http://proxy.example:7880")

    @override_settings(TELEGRAM_PROXY_URL=proxy_url)
    def test_proxy_kwargs_are_limited_to_telegram_provider(self):
        from .bot_proxy import bot_request_kwargs

        self.assertEqual(
            bot_request_kwargs(BotConfiguration.Provider.TELEGRAM),
            {"proxies": self.expected_proxies},
        )
        self.assertEqual(bot_request_kwargs(BotConfiguration.Provider.BALE), {})

    @override_settings(TELEGRAM_PROXY_URL=proxy_url)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_bot_api_calls_use_configured_proxy(self, post_mock):
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        BotClient(config).send_message("hello", chat_id="42")

        self.assertEqual(post_mock.call_args.kwargs["proxies"], self.expected_proxies)

    @override_settings(TELEGRAM_PROXY_URL=proxy_url, BOT_API_CONNECT_TIMEOUT_SECONDS=4, BOT_API_READ_TIMEOUT_SECONDS=1)
    @patch("store.bots.requests.post", return_value=DummyBotResponse(payload={"ok": True, "result": []}))
    def test_telegram_get_updates_uses_configured_proxy(self, post_mock):
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        BotClient(config).get_updates(timeout=2, limit=1)

        self.assertEqual(post_mock.call_args.args[0], "https://api.telegram.org/bottelegram-token/getUpdates")
        self.assertEqual(post_mock.call_args.kwargs["proxies"], self.expected_proxies)
        self.assertEqual(post_mock.call_args.kwargs["json"]["timeout"], 2)
        self.assertEqual(post_mock.call_args.kwargs["timeout"], (4, 7))

    @override_settings(TELEGRAM_PROXY_URL="http://" + "proxy-user" + ":" + "proxy-secret" + "@proxy.example:7880")
    @patch("store.telegram_bot.client.requests.post", return_value=DummyBotResponse({"ok": True, "result": {"id": 42}}))
    def test_telegram_get_me_uses_telegram_proxy_url_and_masks_proxy_secret(self, post_mock):
        from .bot_proxy import sanitized_telegram_proxy_url
        from .telegram_bot.client import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        BotClient(config).get_me()

        self.assertEqual(post_mock.call_args.args[0], "https://api.telegram.org/bottelegram-token/getMe")
        self.assertEqual(
            post_mock.call_args.kwargs["proxies"],
            {
                "http": "http://proxy-user:proxy-secret@proxy.example:7880",
                "https": "http://proxy-user:proxy-secret@proxy.example:7880",
            },
        )
        safe_proxy = sanitized_telegram_proxy_url()
        self.assertEqual(safe_proxy, "http://****@proxy.example:7880")
        self.assertNotIn("proxy-secret", safe_proxy)
        self.assertNotIn("proxy-user", safe_proxy)

    @override_settings(
        TELEGRAM_PROXY_URL="http://proxy-user:proxy-secret@proxy.example:7880",
        TELEGRAM_API_IP="149.154.166.110",
    )
    @patch("store.telegram_bot.client.requests.post")
    @patch("store.telegram_bot.client.telegram_api_request", return_value=DummyBotResponse({"ok": True, "result": {"username": "vpn_bot"}}))
    def test_telegram_api_ip_override_uses_custom_transport(self, transport_mock, post_mock):
        from .telegram_bot.client import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        payload = BotClient(config).get_me()

        self.assertEqual(payload["result"]["username"], "vpn_bot")
        post_mock.assert_not_called()
        self.assertEqual(transport_mock.call_args.args[:3], (config, "POST", "https://api.telegram.org/bottelegram-token/getMe"))
        self.assertEqual(transport_mock.call_args.kwargs["json_payload"], {})

    @override_settings(TELEGRAM_PROXY_URL="http://" + "proxy-user" + ":" + "proxy-secret" + "@proxy.example:7880")
    @patch(
        "store.telegram_bot.client.requests.post",
        side_effect=requests.exceptions.ProxyError(
            "failed via http://" + "proxy-user" + ":" + "proxy-secret" + "@proxy.example:7880"
        ),
    )
    def test_telegram_delivery_errors_mask_proxy_credentials(self, _post_mock):
        from .telegram_bot.client import BotClient, BotDeliveryError

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        with self.assertRaises(BotDeliveryError) as error:
            BotClient(config).get_me()

        message = str(error.exception)
        self.assertIn("http://****@proxy.example:7880", message)
        self.assertNotIn("proxy-secret", message)
        self.assertNotIn("proxy-user", message)
        self.assertNotIn("telegram-token", message)

    @override_settings(TELEGRAM_PROXY_URL=proxy_url)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_delete_webhook_keeps_pending_updates(self, post_mock):
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        BotClient(config).delete_webhook(drop_pending_updates=False)

        self.assertEqual(post_mock.call_args.args[0], "https://api.telegram.org/bottelegram-token/deleteWebhook")
        self.assertEqual(post_mock.call_args.kwargs["json"], {"drop_pending_updates": False})
        self.assertEqual(post_mock.call_args.kwargs["proxies"], self.expected_proxies)

    @override_settings(TELEGRAM_PROXY_URL=proxy_url)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_non_telegram_bot_api_calls_do_not_use_proxy(self, post_mock):
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.BALE,
            name="Bale",
            bot_token="bale-token",
            admin_user_id="42",
        )

        BotClient(config).send_message("hello", chat_id="42")

        self.assertNotIn("proxies", post_mock.call_args.kwargs)

    @override_settings(TELEGRAM_WEBHOOK_RESPONSE_ENABLED=True)
    @patch("store.bots.requests.post", return_value=DummyBotResponse())
    def test_telegram_webhook_response_can_capture_send_message(self, post_mock):
        from .bot_proxy import telegram_webhook_response_context
        from .bots import BotClient

        config = BotConfiguration(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram",
            bot_token="telegram-token",
            admin_user_id="42",
        )

        with telegram_webhook_response_context(BotConfiguration.Provider.TELEGRAM) as response_context:
            BotClient(config).send_message("hello", chat_id="42")

        self.assertEqual(
            response_context.payload,
            {
                "method": "sendMessage",
                "chat_id": "42",
                "text": "hello",
                "disable_web_page_preview": True,
                "parse_mode": "HTML",
            },
        )
        post_mock.assert_not_called()


class VPNClientManagementServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        self.plan = Plan.objects.create(
            store=self.store,
            name="10 GB",
            slug="10gb-management",
            volume_gb=Decimal("10.000"),
            duration_days=30,
            price=100000,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            server_ip="vpn.example.com",
            port="443",
            config_params="type=tcp&security=none",
        )
        self.customer = Customer.objects.create(display_name="Alice")
        self.order = Order.objects.create(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            amount=self.plan.price,
            original_amount=self.plan.price,
            currency=Plan.Currency.TOMAN,
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
            uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            sub_link="https://example.com/sub/private-token",
            direct_link="vless://aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@vpn.example.com:443#Alice",
        )
        self.vpn_client = VPNClient.objects.create(
            store=self.store,
            order=self.order,
            plan=self.plan,
            inbound=self.inbound,
            username="alice_config",
            xui_email="alice_config",
            uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            sub_id="private-token",
            sub_link=self.order.sub_link,
            direct_link=self.order.direct_link,
            status=VPNClient.Status.ACTIVE,
            traffic_limit_bytes=10 * (1024 ** 3),
            used_traffic_bytes=1024 ** 3,
            expires_at=timezone.now() + timedelta(days=20),
        )

    @patch("store.vpn_client_management_services.delete_client_from_inbound")
    def test_user_delete_remote_success_soft_deletes_local_and_audits(self, delete_mock):
        from .vpn_client_management_services import delete_vpn_client_for_user

        delete_mock.return_value = {
            "deleted": True,
            "stats_deleted": True,
            "stats_remaining": False,
            "matched_field": "id",
        }

        result = delete_vpn_client_for_user(self.customer, self.vpn_client, actor_telegram_id="42")

        self.assertTrue(result["success"])
        delete_mock.assert_called_once()
        self.vpn_client.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.vpn_client.status, VPNClient.Status.DELETED)
        self.assertIsNotNone(self.vpn_client.deleted_at)
        self.assertEqual(self.vpn_client.deleted_by_customer, self.customer)
        self.assertEqual(self.vpn_client.direct_link, "")
        self.assertEqual(self.vpn_client.sub_link, "")
        self.assertEqual(self.order.direct_link, "")
        self.assertEqual(self.order.sub_link, "")

        log = VPNClientActionLog.objects.get()
        self.assertEqual(log.action, VPNClientActionLog.Action.USER_DELETE)
        self.assertEqual(log.status, VPNClientActionLog.Status.SUCCESS)
        self.assertEqual(log.actor_telegram_id, "42")
        self.assertNotIn(self.vpn_client.uuid, log.xui_identifier_masked)
        self.assertNotIn("vless://", json.dumps(log.metadata))

    @patch("store.vpn_client_management_services.delete_client_from_inbound", side_effect=Exception("panel timeout"))
    def test_user_delete_remote_failure_does_not_soft_delete_local(self, _delete_mock):
        from .vpn_client_management_services import VPNClientManagementError, delete_vpn_client_for_user

        with self.assertRaises(VPNClientManagementError):
            delete_vpn_client_for_user(self.customer, self.vpn_client, actor_telegram_id="42")

        self.vpn_client.refresh_from_db()
        self.assertEqual(self.vpn_client.status, VPNClient.Status.ACTIVE)
        self.assertIsNone(self.vpn_client.deleted_at)
        log = VPNClientActionLog.objects.get()
        self.assertEqual(log.status, VPNClientActionLog.Status.FAILED)

    @patch("store.vpn_client_management_services.update_client_traffic_and_expiry")
    @patch("store.vpn_client_management_services.find_client_by_identifier")
    def test_admin_traffic_update_syncs_local_and_audits(self, find_mock, update_mock):
        from .vpn_client_management_services import update_vpn_client_limits_by_admin

        find_mock.return_value = {
            "panel": self.panel,
            "inbound": self.inbound,
            "identifier": self.vpn_client.uuid,
            "email": self.vpn_client.xui_email,
            "total_bytes": 10 * (1024 ** 3),
            "used_bytes": 1024 ** 3,
            "expiry_time": self.vpn_client.expires_at,
            "client": {"id": self.vpn_client.uuid, "email": self.vpn_client.xui_email},
        }
        update_mock.return_value = {
            "updated": True,
            "new_total_bytes": 15 * (1024 ** 3),
            "new_expiry_time": self.vpn_client.expires_at,
            "enabled": True,
            "raw": {"client": {"email": self.vpn_client.xui_email}},
        }

        result = update_vpn_client_limits_by_admin(
            "999",
            {
                "panel_id": self.panel.pk,
                "inbound_id": self.inbound.inbound_id,
                "identifier": self.vpn_client.uuid,
                "vpn_client_id": self.vpn_client.pk,
            },
            traffic_gb=5,
            mode="add",
        )

        self.assertEqual(result["new_total_bytes"], 15 * (1024 ** 3))
        self.vpn_client.refresh_from_db()
        self.assertEqual(self.vpn_client.traffic_limit_bytes, 15 * (1024 ** 3))
        log = VPNClientActionLog.objects.get(action=VPNClientActionLog.Action.ADMIN_UPDATE_TRAFFIC)
        self.assertEqual(log.status, VPNClientActionLog.Status.SUCCESS)
        self.assertEqual(log.actor_telegram_id, "999")


class TelegramPollingTests(TestCase):
    def test_polling_command_discovers_new_bot_configs_while_running(self):
        from store.management.commands.run_telegram_polling import Command

        first_config = BotConfiguration.objects.create(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram polling first",
            bot_token="telegram-token-1",
            admin_user_id="42",
        )
        second_config = None
        started_config_ids = []

        class FakeThread:
            def __init__(self, target, name, daemon):
                self.name = name
                self.started = False

            def start(self):
                self.started = True
                started_config_ids.append(int(self.name.rsplit("-", 1)[-1]))

            def is_alive(self):
                return self.started

            def join(self, timeout=None):
                return None

        class FakeStopEvent:
            def __init__(self):
                self.wait_calls = 0
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                nonlocal second_config
                self.wait_calls += 1
                if self.wait_calls == 1:
                    second_config = BotConfiguration.objects.create(
                        provider=BotConfiguration.Provider.TELEGRAM,
                        name="Telegram polling second",
                        bot_token="telegram-token-2",
                        admin_user_id="42",
                    )
                else:
                    self.stopped = True
                return self.stopped

        command = Command()
        command._log = lambda *args, **kwargs: None
        options = {
            "config_id": None,
            "timeout": 0,
            "limit": 10,
            "idle_sleep": 0,
            "retry_sleep": 0,
            "discovery_interval": 0.1,
            "skip_delete_webhook": False,
        }

        with patch("store.management.commands.run_telegram_polling.threading.Thread", FakeThread):
            command._run_discovery_loop(FakeStopEvent(), options)

        self.assertIsNotNone(second_config)
        self.assertEqual(started_config_ids, [first_config.pk, second_config.pk])

    def test_polling_worker_dispatches_callback_query_updates(self):
        from store.management.commands.run_telegram_polling import TelegramPollingWorker

        config = BotConfiguration.objects.create(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram polling",
            bot_token="telegram-token",
            admin_user_id="42",
        )
        command = SimpleNamespace(_log=lambda *args, **kwargs: None)
        options = {
            "timeout": 0,
            "limit": 10,
            "idle_sleep": 0,
            "retry_sleep": 0,
            "skip_delete_webhook": False,
        }
        callback_update = {
            "update_id": 100,
            "callback_query": {
                "id": "callback-id",
                "from": {"id": 42},
                "message": {"message_id": 5, "chat": {"id": 42}},
                "data": "order:detail:ABC123",
            },
        }

        with (
            patch("store.management.commands.run_telegram_polling.BotClient") as client_mock,
            patch("store.management.commands.run_telegram_polling.handle_bot_update") as handle_mock,
        ):
            client_mock.return_value.get_updates.return_value = {"ok": True, "result": [callback_update]}
            worker = TelegramPollingWorker(command, config.pk, threading.Event(), options)

            self.assertEqual(worker.poll_once(), 1)

        client_mock.return_value.get_updates.assert_called_once_with(
            offset=None,
            timeout=0,
            limit=10,
            allowed_updates=["message", "callback_query"],
        )
        handle_mock.assert_called_once_with(
            BotConfiguration.Provider.TELEGRAM,
            config.webhook_secret,
            callback_update,
            source="polling",
        )
        self.assertEqual(worker.offset, 101)

    def test_polling_worker_continues_after_delivery_failure(self):
        from store.bots import BotDeliveryError
        from store.management.commands.run_telegram_polling import TelegramPollingWorker

        config = BotConfiguration.objects.create(
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram polling",
            bot_token="telegram-token",
            admin_user_id="42",
        )
        logs = []
        command = SimpleNamespace(_log=lambda *args, **kwargs: logs.append(args[0]))
        options = {
            "timeout": 0,
            "limit": 10,
            "idle_sleep": 0,
            "retry_sleep": 0,
            "skip_delete_webhook": False,
        }
        updates = [
            {"update_id": 100, "message": {"message_id": 1, "chat": {"id": 42}, "text": "/start"}},
            {"update_id": 101, "callback_query": {"id": "callback-id", "from": {"id": 42}, "data": "noop"}},
        ]

        with (
            patch("store.management.commands.run_telegram_polling.BotClient") as client_mock,
            patch("store.management.commands.run_telegram_polling.handle_bot_update") as handle_mock,
        ):
            client_mock.return_value.get_updates.return_value = {"ok": True, "result": updates}
            handle_mock.side_effect = [BotDeliveryError("Forbidden: bot was blocked by the user"), None]
            worker = TelegramPollingWorker(command, config.pk, threading.Event(), options)

            self.assertEqual(worker.poll_once(), 2)

        self.assertEqual(handle_mock.call_count, 2)
        self.assertEqual(worker.offset, 102)
        self.assertTrue(any("delivery failure" in message for message in logs))


class IntegrationCheckCommandTests(TestCase):
    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_incomplete_configuration_reports_errors(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        store = Store.objects.create(
            name="Broken store",
            english_name="Broken store",
            card_number="",
            card_owner="",
        )
        BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Broken bot",
            bot_token="",
            admin_user_id="",
            force_telegram_channel_join=True,
            is_active=True,
        )
        panel = Panel.objects.create(
            store=store,
            name="Broken panel",
            url="",
            username="",
            password="",
            is_active=True,
        )
        Inbound.objects.create(
            panel=panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        stdout = StringIO()

        with self.assertRaises(CommandError):
            call_command("check_integrations", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("[ERROR]", output)
        self.assertIn("Card number is missing", output)
        self.assertIn("Bot token is missing", output)
        self.assertIn("Admin user IDs are missing", output)
        self.assertIn("channel id/username is missing", output)
        self.assertIn("Panel URL is missing or invalid", output)
        self.assertIn("SMSFORWARDER_WEBHOOK_TOKEN is missing", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_fresh_minimal_install_reports_setup_warnings_not_errors(self):
        from io import StringIO

        from django.core.management import call_command

        Store.objects.create(
            name="Qasedak",
            english_name="Qasedak",
            card_number="0000000000000000",
            card_owner="Configure Payment Owner",
        )
        stdout = StringIO()

        call_command("check_integrations", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Setup incomplete: no active operational panel exists yet", output)
        self.assertIn("Setup incomplete: no active inbound exists yet", output)
        self.assertIn("Setup incomplete: no active Panel and no active Plan exist yet", output)
        self.assertIn("SMSForwarder webhook token is not configured yet", output)
        self.assertIn("ERROR=0", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    @patch("store.bots.requests.post", return_value=DummyBotResponse({"ok": True, "result": {"username": "vpn_store_bot"}}))
    def test_complete_configuration_passes(self, _post_mock):
        from io import StringIO

        from django.core.management import call_command

        store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        store.set_smsforwarder_webhook_token("sms-secret")
        store.save()
        Plan.objects.create(
            store=store,
            name="1 GB",
            slug="check-integrations-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram sales",
            bot_token="test-bot-token:placeholder",
            telegram_bot_username="vpn_store_bot",
            admin_user_id="999",
            force_telegram_channel_join=True,
            telegram_required_channel_username="vpn_store_channel",
            telegram_required_channel_invite_link="https://t.me/vpn_store_channel",
            is_active=True,
        )
        panel = Panel.objects.create(
            store=store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        Inbound.objects.create(
            panel=panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        stdout = StringIO()

        class FakeXUIService:
            def __init__(self, panel):
                self.panel = panel

            def get_inbound(self, inbound_id, *, use_cache=True):
                return {"id": inbound_id, "protocol": "vless", "remark": "ok", "enable": True}

        with (
            patch("store.xui_api.login_to_panel", return_value=object()),
            patch("store.xui_api.XUIService", FakeXUIService),
        ):
            call_command(
                "check_integrations",
                "--live-bot",
                "--live-xui",
                "--send-telegram-test-message",
                "--telegram-chat-id",
                "999",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("[OK]", output)
        self.assertIn("Bot getMe succeeded", output)
        self.assertIn("Telegram test message sent", output)
        self.assertIn("X-UI login succeeded", output)
        self.assertIn("Telegram bot username is configured in BotConfiguration", output)
        self.assertIn("SMSForwarder webhook token is configured in Store", output)
        self.assertIn("ERROR=0", output)

    def create_integration_store_with_panel(self, *, inbound_available=True, inbound_health=True):
        store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        store.set_smsforwarder_webhook_token("sms-secret")
        store.save()
        Plan.objects.create(
            store=store,
            name="1 GB",
            slug=f"check-integrations-plan-{store.pk}",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram sales",
            bot_token="test-bot-token:placeholder",
            telegram_bot_username="vpn_store_bot",
            admin_user_id="999",
            is_active=True,
        )
        panel = Panel.objects.create(
            store=store,
            name="Panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        inbound = Inbound.objects.create(
            panel=panel,
            inbound_id=1,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            available_for_new_orders=inbound_available,
            health_monitor_enabled=inbound_health,
        )
        return store, panel, inbound

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_check_integrations_ignores_legacy_inbound_live_xui_missing(self):
        from io import StringIO

        from django.core.management import call_command
        from store.xui_api import XUIError

        _store, _panel, legacy_inbound = self.create_integration_store_with_panel(
            inbound_available=False,
            inbound_health=False,
        )
        Inbound.objects.create(
            panel=legacy_inbound.panel,
            inbound_id=2,
            server_ip="127.0.0.1",
            port="8443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

        class FakeXUIService:
            def __init__(self, panel):
                self.panel = panel

            def get_inbound(self, inbound_or_id, *, use_cache=True):
                inbound_id = getattr(inbound_or_id, "inbound_id", inbound_or_id)
                if inbound_id == legacy_inbound.inbound_id:
                    raise XUIError("Obtain (record not found)")
                return {"id": inbound_id, "protocol": "vless", "remark": "ok", "enable": True}

        stdout = StringIO()
        with (
            patch("store.xui_api.login_to_panel", return_value=object()),
            patch("store.xui_api.XUIService", FakeXUIService),
        ):
            call_command("check_integrations", "--live-xui", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("legacy inbound(s) ignored from health monitor and new orders", output)
        self.assertIn("Legacy inbound is missing/unreadable in X-UI and ignored from health monitor", output)
        self.assertIn("ERROR=0", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_check_integrations_errors_when_sales_inbound_missing_live_xui(self):
        from io import StringIO

        from django.core.management import call_command
        from store.xui_api import XUIError

        self.create_integration_store_with_panel(inbound_available=True, inbound_health=True)

        class MissingXUIService:
            def __init__(self, panel):
                self.panel = panel

            def get_inbound(self, inbound_or_id, *, use_cache=True):
                raise XUIError("Obtain (record not found)")

        stdout = StringIO()
        with (
            patch("store.xui_api.login_to_panel", return_value=object()),
            patch("store.xui_api.XUIService", MissingXUIService),
        ):
            call_command("check_integrations", "--live-xui", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("available for new orders but missing/unreadable in X-UI", output)
        self.assertIn("No inbound available for new orders could be verified in X-UI", output)
        self.assertIn("ERROR=", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="", TELEGRAM_BOT_USERNAME="")
    def test_check_integrations_live_xui_outputs_safe_structured_login_error(self):
        from io import StringIO

        from django.core.management import call_command
        from store.xui_api import XUIError

        _store, panel, _inbound = self.create_integration_store_with_panel()

        def fail_login(_panel, *, raise_errors=False):
            exc = XUIError(
                "Panel login failed with HTTP 403 secret csrf-secret-token",
                category="http_403_csrf_required",
                http_status=403,
                endpoint="login",
                response_snippet="Forbidden secret csrf-secret-token",
                remediation_hint="CSRF/login flow mismatch",
            )
            if raise_errors:
                raise exc
            return None

        stdout = StringIO()
        with patch("store.xui_api.login_to_panel", side_effect=fail_login):
            call_command("check_integrations", "--live-xui", "--no-fail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn(f"Panel #{panel.pk}", output)
        self.assertIn("error_code=http_403_csrf_required", output)
        self.assertIn("http_status=HTTP 403", output)
        self.assertIn("CSRF/login flow mismatch", output)
        self.assertNotIn(" secret ", output)
        self.assertNotIn("csrf-secret-token", output)

    @override_settings(SMSFORWARDER_WEBHOOK_TOKEN="sms-secret", TELEGRAM_BOT_USERNAME="vpn_store_bot")
    def test_free_trial_configuration_errors_are_reported(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
            free_trial_enabled=True,
            free_trial_traffic_gb=Decimal("0.000"),
            free_trial_duration_hours=0,
        )
        trial_panel = Panel.objects.create(
            store=store,
            name="Inactive trial panel",
            url="https://trial-panel.example.com",
            username="admin",
            password="secret",
            is_active=False,
        )
        other_panel = Panel.objects.create(
            store=store,
            name="Other panel",
            url="https://other-panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        trial_inbound = Inbound.objects.create(
            panel=other_panel,
            inbound_id=9,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            current_users=1,
            max_clients=1,
        )
        store.free_trial_panel = trial_panel
        store.free_trial_inbound = trial_inbound
        store.save(
            update_fields=[
                "free_trial_panel",
                "free_trial_inbound",
                "updated_at",
            ]
        )
        Plan.objects.create(
            store=store,
            name="1 GB",
            slug="free-trial-check-1gb",
            volume_gb=Decimal("1.000"),
            duration_days=30,
            price=100000,
            currency=Plan.Currency.TOMAN,
            is_active=True,
            is_public=True,
        )
        BotConfiguration.objects.create(
            store=store,
            provider=BotConfiguration.Provider.TELEGRAM,
            name="Telegram sales",
            bot_token="test-bot-token:placeholder",
            admin_user_id="999",
            is_active=True,
        )
        stdout = StringIO()

        with self.assertRaises(CommandError):
            call_command("check_integrations", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Free Trial panel is inactive", output)
        self.assertIn("Free Trial inbound does not belong to the selected panel", output)
        self.assertIn("Free Trial traffic must be positive", output)
        self.assertIn("Free Trial duration must be positive", output)


class XUIPanelProxyTests(TestCase):
    proxy_url = "http://panel-proxy.example:8080"
    expected_proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }

    @override_settings(XUI_PANEL_PROXY_URL=proxy_url)
    def test_xui_service_uses_configured_panel_proxy(self):
        from .xui_api import XUIService

        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
        )

        service = XUIService(panel)

        self.assertFalse(service.session.trust_env)
        self.assertEqual(service.session.proxies, self.expected_proxies)

    @override_settings(XUI_PANEL_PROXY_URL="http://env-proxy.example:8080")
    def test_panel_proxy_overrides_environment_panel_proxy(self):
        from .xui_api import XUIService

        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
            proxy_url=self.proxy_url,
        )

        service = XUIService(panel)

        self.assertEqual(service.session.proxies, self.expected_proxies)

    @override_settings(XUI_PANEL_PROXY_URL="")
    def test_xui_service_ignores_environment_proxies_by_default(self):
        from .xui_api import XUIService

        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
        )

        service = XUIService(panel)

        self.assertFalse(service.session.trust_env)
        self.assertEqual(service.session.proxies, {})

    @override_settings(XUI_PANEL_PROXY_URL="", TELEGRAM_PROXY_URL="http://telegram-proxy.example:7880")
    @patch.dict(os.environ, {"HTTP_PROXY": "http://telegram-proxy.example:7880", "HTTPS_PROXY": "http://telegram-proxy.example:7880"})
    def test_xui_service_ignores_global_telegram_proxy_environment(self):
        from .xui_api import XUIService

        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
        )

        service = XUIService(panel)

        self.assertFalse(service.session.trust_env)
        self.assertEqual(service.session.proxies, {})

    @override_settings(XUI_PANEL_PROXY_URL="", TELEGRAM_PROXY_URL="http://telegram-proxy.example:7880")
    @patch.dict(os.environ, {"HTTP_PROXY": "http://telegram-proxy.example:7880", "HTTPS_PROXY": "http://telegram-proxy.example:7880"})
    def test_panel_specific_proxy_still_works_with_global_telegram_proxy_environment(self):
        from .xui_api import XUIService

        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
            proxy_url=self.proxy_url,
        )

        service = XUIService(panel)

        self.assertFalse(service.session.trust_env)
        self.assertEqual(service.session.proxies, self.expected_proxies)

    @patch("store.xui_api.requests.Session")
    def test_xui_login_retries_transient_connection_errors(self, session_class_mock):
        from .xui_api import XUIService

        session = Mock()
        session.proxies = {}
        session.post.side_effect = [
            requests.ConnectionError("temporary disconnect"),
            DummyXUIResponse(),
        ]
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
        )

        XUIService(panel).login()

        self.assertEqual(session.post.call_count, 2)

    @patch("store.xui_api.requests.Session")
    def test_xui_json_request_retries_transient_connection_errors(self, session_class_mock):
        from .xui_api import XUIService

        session = Mock()
        session.proxies = {}
        session.request.side_effect = [
            requests.ReadTimeout("temporary slow panel"),
            DummyXUIResponse({"success": True, "obj": []}),
        ]
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
        )

        payload = XUIService(panel).request_json("GET", "/panel/api/inbounds/list")

        self.assertEqual(payload, {"success": True, "obj": []})
        self.assertEqual(session.request.call_count, 2)

    @patch("store.xui_api.requests.Session")
    def test_xui_legacy_login_still_uses_existing_flow(self, session_class_mock):
        from .xui_api import XUIService

        session = Mock()
        session.proxies = {}
        session.post.return_value = DummyXUIResponse({"success": True})
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin/",
            username="user",
            password="pass",
        )

        XUIService(panel).login()

        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.args[0], "http://panel.example:1111/admin/login")
        self.assertEqual(session.post.call_args.kwargs["data"], {"username": "user", "password": "pass"})
        session.get.assert_not_called()

    @patch("store.xui_api.requests.Session")
    def test_xui_35_csrf_login_with_base_path_and_authenticated_list(self, session_class_mock):
        from .xui_api import XUIService

        session = Mock()
        session.proxies = {}
        session.post.side_effect = [
            DummyXUIResponse({"success": False}, status_code=403, text="Forbidden"),
            DummyXUIResponse({"success": True, "msg": "", "obj": False}),
            DummyXUIResponse({"success": True, "msg": "ok", "obj": None}),
        ]
        session.get.side_effect = [
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-secret-token"}),
        ]
        session.request.return_value = DummyXUIResponse({"success": True, "obj": []})
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/secret",
            username="user",
            password="pass",
        )

        service = XUIService(panel)
        service.login()
        payload = service.authenticated_json("GET", "/panel/api/inbounds/list")

        self.assertEqual(payload, {"success": True, "obj": []})
        self.assertEqual(session.post.call_args_list[0].args[0], "http://panel.example:1111/secret/login")
        self.assertEqual(session.get.call_args_list[0].args[0], "http://panel.example:1111/secret/")
        self.assertEqual(session.get.call_args_list[1].args[0], "http://panel.example:1111/secret/csrf-token")
        self.assertEqual(session.post.call_args_list[1].args[0], "http://panel.example:1111/secret/getTwoFactorEnable")
        self.assertEqual(session.post.call_args_list[2].args[0], "http://panel.example:1111/secret/login")
        self.assertEqual(session.request.call_args.args[1], "http://panel.example:1111/secret/panel/api/inbounds/list")
        login_kwargs = session.post.call_args_list[2].kwargs
        self.assertEqual(login_kwargs["data"]["twoFactorCode"], "")
        self.assertEqual(login_kwargs["headers"]["X-CSRF-Token"], "csrf-secret-token")
        self.assertEqual(login_kwargs["headers"]["Origin"], "http://panel.example:1111")
        self.assertEqual(login_kwargs["headers"]["Referer"], "http://panel.example:1111/secret/")

    @patch("store.xui_api.requests.Session")
    def test_xui_35_unsafe_post_includes_csrf_ajax_headers(self, session_class_mock):
        from .xui_api import XUIService

        session = Mock()
        session.proxies = {}
        session.post.return_value = DummyXUIResponse({"success": True})
        session.get.side_effect = [
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-secret-token"}),
        ]
        session.request.return_value = DummyXUIResponse({"success": True, "obj": None})
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/secret",
            username="user",
            password="pass",
            capability_profile=Panel.CapabilityProfile.MODERN_MULTI_NODE,
        )

        payload = XUIService(panel).authenticated_json(
            "POST",
            "/panel/api/clients/add",
            json={"client": {"email": "safe-test"}, "inboundIds": [1, 6]},
            headers={"Accept": "application/json"},
        )

        self.assertEqual(payload, {"success": True, "obj": None})
        self.assertEqual(session.get.call_args_list[0].args[0], "http://panel.example:1111/secret/")
        self.assertEqual(session.get.call_args_list[1].args[0], "http://panel.example:1111/secret/csrf-token")
        headers = session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-CSRF-Token"], "csrf-secret-token")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(headers["Accept"], "application/json, text/plain, */*")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Origin"], "http://panel.example:1111")
        self.assertEqual(headers["Referer"], "http://panel.example:1111/secret/")
        self.assertFalse(session.trust_env)

    @patch("store.xui_api.requests.Session")
    def test_xui_35_unsafe_post_refreshes_csrf_and_retries_once_on_403(self, session_class_mock):
        from .xui_api import XUIService

        session = Mock()
        session.proxies = {}
        session.post.return_value = DummyXUIResponse({"success": True})
        session.get.side_effect = [
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-old-token"}),
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-new-token"}),
        ]
        session.request.side_effect = [
            DummyXUIResponse({"success": False}, status_code=403, text="forbidden csrf-old-token"),
            DummyXUIResponse({"success": True, "obj": None}),
        ]
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/secret",
            username="user",
            password="pass",
            capability_profile=Panel.CapabilityProfile.MODERN_MULTI_NODE,
        )

        payload = XUIService(panel).authenticated_json(
            "POST",
            "/panel/api/clients/add",
            json={"client": {"email": "safe-test"}, "inboundIds": [1, 6]},
        )

        self.assertEqual(payload, {"success": True, "obj": None})
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual(session.request.call_args_list[0].kwargs["headers"]["X-CSRF-Token"], "csrf-old-token")
        self.assertEqual(session.request.call_args_list[1].kwargs["headers"]["X-CSRF-Token"], "csrf-new-token")

    @patch("store.xui_api.requests.Session")
    def test_xui_35_unsafe_post_403_after_retry_is_write_api_forbidden_and_redacted(self, session_class_mock):
        from .xui_api import XUIError, XUIService

        session = Mock()
        session.proxies = {}
        session.post.return_value = DummyXUIResponse({"success": True})
        session.get.side_effect = [
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-old-token"}),
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-new-token"}),
        ]
        session.request.side_effect = [
            DummyXUIResponse({"success": False}, status_code=403, text="forbidden csrf-old-token panel-password"),
            DummyXUIResponse({"success": False}, status_code=403, text="still forbidden csrf-new-token panel-password"),
        ]
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/secret",
            username="user",
            password="panel-password",
            capability_profile=Panel.CapabilityProfile.MODERN_MULTI_NODE,
        )

        with self.assertRaises(XUIError) as caught:
            XUIService(panel).authenticated_json(
                "POST",
                "/panel/api/clients/add",
                json={"client": {"email": "safe-test"}, "inboundIds": [1, 6]},
            )

        self.assertEqual(caught.exception.category, "write_api_forbidden")
        self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(caught.exception.endpoint, "panel/api/clients/add")
        metadata = json.dumps(caught.exception.safe_metadata())
        self.assertIn("write permissions", metadata)
        self.assertNotIn("csrf-new-token", metadata)
        self.assertNotIn("panel-password", metadata)

    @patch("store.xui_api.requests.Session")
    def test_xui_legacy_unsafe_post_does_not_require_csrf_endpoint(self, session_class_mock):
        from .xui_api import XUIService

        cache.clear()
        session = Mock()
        session.proxies = {}
        session.request.return_value = DummyXUIResponse({"success": True})
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/admin",
            username="user",
            password="pass",
            capability_profile=Panel.CapabilityProfile.LEGACY_SINGLE_NODE,
        )

        payload = XUIService(panel).request_json("POST", "/panel/api/inbounds/addClient", json={"settings": "{}"})

        self.assertEqual(payload, {"success": True})
        session.get.assert_not_called()
        self.assertEqual(session.request.call_count, 1)
        self.assertNotIn("headers", session.request.call_args.kwargs)

    @patch("store.xui_api.requests.Session")
    def test_xui_35_clients_add_multi_inbound_payload_keeps_inbound_ids(self, session_class_mock):
        from .xui_api import XUIService

        session = Mock()
        session.proxies = {}
        session.post.return_value = DummyXUIResponse({"success": True})
        session.get.side_effect = [
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-secret-token"}),
        ]
        session.request.return_value = DummyXUIResponse({"success": True, "obj": None})
        session_class_mock.return_value = session
        panel = Panel(
            name="XUI",
            url="http://panel.example:1111/secret",
            username="user",
            password="pass",
            capability_profile=Panel.CapabilityProfile.MODERN_MULTI_NODE,
        )
        inbound = Inbound(panel=panel, inbound_id=1, server_ip="node.example", port="443", config_params="")

        XUIService(panel)._post_modern_client_create(
            inbound,
            {"id": "client-id", "email": "safe-test", "enable": True},
            inbound_ids=[1, 6, 8, 9],
        )

        self.assertEqual(session.request.call_args.kwargs["json"]["inboundIds"], [1, 6, 8, 9])

    @patch("store.xui_api.requests.Session")
    def test_xui_35_base_path_normalization_without_duplicate_path(self, session_class_mock):
        from .xui_api import XUIService

        for raw_url in ("http://panel.example:1111/secret", "http://panel.example:1111/secret/"):
            session = Mock()
            session.proxies = {}
            session.post.return_value = DummyXUIResponse({"success": True})
            session.request.return_value = DummyXUIResponse({"success": True, "obj": []})
            session_class_mock.return_value = session

            XUIService(Panel(name="XUI", url=raw_url, username="user", password="pass")).request_json(
                "GET",
                "/panel/api/inbounds/list",
            )

            self.assertEqual(session.request.call_args.args[1], "http://panel.example:1111/secret/panel/api/inbounds/list")
            self.assertNotIn("/secret/secret/", session.request.call_args.args[1])

    @patch("store.xui_api.requests.Session")
    def test_xui_403_without_csrf_is_classified_safely(self, session_class_mock):
        from .xui_api import XUIError, XUIService

        session = Mock()
        session.proxies = {}
        session.post.side_effect = [
            DummyXUIResponse({"success": False}, status_code=403, text="Forbidden"),
            DummyXUIResponse({"success": False}, status_code=403, text="still forbidden csrf-secret-token pass"),
        ]
        session.get.side_effect = [
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-secret-token"}),
        ]
        session_class_mock.return_value = session
        panel = Panel(name="XUI", url="http://panel.example/admin", username="user", password="pass")

        with self.assertRaises(XUIError) as caught:
            XUIService(panel).login()

        self.assertEqual(caught.exception.category, "http_403_csrf_required")
        self.assertEqual(caught.exception.http_status, 403)
        metadata = caught.exception.safe_metadata()
        self.assertNotIn("pass", json.dumps(metadata))
        self.assertIn("remediation_hint", metadata)

    @patch("store.xui_api.requests.Session")
    def test_xui_35_two_factor_enabled_is_classified(self, session_class_mock):
        from .xui_api import XUIError, XUIService

        session = Mock()
        session.proxies = {}
        session.post.side_effect = [
            DummyXUIResponse({"success": False}, status_code=403),
            DummyXUIResponse({"success": True, "msg": "", "obj": True}),
        ]
        session.get.side_effect = [
            DummyXUIResponse({"success": True}),
            DummyXUIResponse({"success": True, "obj": "csrf-secret-token"}),
        ]
        session_class_mock.return_value = session
        panel = Panel(name="XUI", url="http://panel.example/admin", username="user", password="pass")

        with self.assertRaises(XUIError) as caught:
            XUIService(panel).login()

        self.assertEqual(caught.exception.category, "two_factor_required")
        self.assertNotIn("csrf-secret-token", json.dumps(caught.exception.safe_metadata()))


class XUICompatibilityFacadeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Modern panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Main",
            protocol=Inbound.Protocol.VLESS,
            server_ip="vpn.example.com",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

    def mark_modern(self, profile=Panel.CapabilityProfile.MODERN_MULTI_NODE):
        self.panel.capability_profile = profile
        self.panel.detected_xui_version = "3.4.0"
        self.panel.save(update_fields=["capability_profile", "detected_xui_version", "updated_at"])
        cache.clear()

    def modern_api_mock(self, calls):
        def api(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/panel/api/clients/add":
                return {"success": True, "obj": {"pendingNodes": False}}
            if path.startswith("/panel/api/clients/update/"):
                return {"success": True, "obj": {"pendingNodes": False}}
            if path == "/panel/api/inbounds/get/1":
                created_clients = [
                    call[2]["json"]["client"]
                    for call in calls
                    if call[1] == "/panel/api/clients/add" and call[2].get("json", {}).get("client")
                ]
                return {
                    "success": True,
                    "obj": {
                        "id": self.inbound.inbound_id,
                        "protocol": "vless",
                        "remark": "Main",
                        "port": self.inbound.port,
                        "settings": json.dumps({"clients": created_clients}),
                        "streamSettings": "{}",
                    },
                }
            if path == "/panel/api/hosts/byInbound/1":
                return {"success": True, "obj": []}
            return {"success": True, "obj": []}

        return api

    def test_modern_enabled_create_uses_clients_add_payload(self):
        from .xui_api import XUIService

        self.mark_modern()
        service = XUIService(self.panel)
        calls = []
        with (
            patch.object(service, "login", return_value=service.session),
            patch.object(service, "authenticated_json", side_effect=self.modern_api_mock(calls)),
        ):
            result = service.create_enabled_client(
                email_prefix="alice",
                total_gb=1,
                duration_hours=1,
                inbound=self.inbound,
            )

        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/panel/api/clients/add")
        self.assertEqual(calls[0][2]["json"]["inboundIds"], [self.inbound.inbound_id])
        self.assertTrue(calls[0][2]["json"]["client"]["enable"])
        self.assertEqual(calls[0][2]["json"]["client"]["tgId"], 0)
        self.assertEqual(result["xui_node_id"], "")
        self.assertTrue(result["remote_client_key"])

    def test_modern_inactive_create_is_refused(self):
        from .xui_api import XUIService
        from .xui_compat.errors import XUICompatibilityError

        self.mark_modern()
        service = XUIService(self.panel)
        with (
            patch.object(service, "login", return_value=service.session),
            self.assertRaises(XUICompatibilityError),
        ):
            service.create_inactive_client(
                email_prefix="alice",
                total_gb=1,
                expire_days=30,
                inbound=self.inbound,
            )

    def test_modern_update_uses_email_endpoint_with_inbound_filter(self):
        from .xui_api import XUIService

        self.mark_modern(Panel.CapabilityProfile.MODERN_SINGLE_NODE)
        service = XUIService(self.panel)
        found = {
            "client": {"id": "client-alpha", "email": "alice@example.com", "totalGB": 100, "expiryTime": 0, "enable": True},
            "client_stats": {},
            "matched_field": "id",
        }
        verified = {
            "client": {"id": "client-alpha", "email": "alice@example.com", "totalGB": 2048, "expiryTime": 0, "enable": True},
            "client_stats": {},
            "matched_field": "id",
        }
        calls = []

        with (
            patch.object(service, "_find_client_in_inbound", side_effect=[found, verified]),
            patch.object(service, "authenticated_json", side_effect=self.modern_api_mock(calls)),
        ):
            result = service.update_client_traffic_and_expiry(
                self.inbound,
                "client-alpha",
                total_bytes=2048,
            )

        update_call = next(call for call in calls if call[1].startswith("/panel/api/clients/update/"))
        self.assertIn("/panel/api/clients/update/alice%40example.com", update_call[1])
        self.assertIn("inboundIds=1", update_call[1])
        self.assertEqual(update_call[2]["json"]["totalGB"], 2048)
        self.assertEqual(result["new_total_bytes"], 2048)

    def test_modern_delete_refuses_multi_attachment_without_explicit_scope(self):
        from .xui_api import XUIService
        from .xui_compat.errors import XUIAmbiguousScopeError

        self.mark_modern()
        service = XUIService(self.panel)
        found = {
            "client": {"id": "client-alpha", "email": "alice@example.com"},
            "client_stats": {},
            "matched_field": "id",
        }
        with (
            patch.object(service, "_find_client_in_inbound", return_value=found),
            patch.object(service, "_modern_client_attachments", return_value=[1, 2]),
            self.assertRaises(XUIAmbiguousScopeError),
        ):
            service.delete_client_from_inbound(self.inbound, "client-alpha")

    def test_audit_command_no_write_keeps_panel_metadata_unchanged(self):
        stdout = StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "xui-audit.json"
            call_command(
                "audit_xui_compatibility",
                "--panel-id",
                str(self.panel.pk),
                "--no-write",
                "--export-json",
                str(report_path),
                stdout=stdout,
            )
            self.panel.refresh_from_db()

            self.assertEqual(self.panel.capability_profile, "")
            self.assertTrue(report_path.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["panels"], 1)
            self.assertIn("profiles", payload["summary"])


class PanelAdapterFactoryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Modern panel",
            url="https://panel.example.com",
            username="admin",
            password="secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.MODERN_MULTI_NODE,
            detected_xui_version="3.4.0",
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Main",
            protocol=Inbound.Protocol.VLESS,
            server_ip="vpn.example.com",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

    def reality_link(self, index=701, host="reality.example.com"):
        return (
            f"vless://aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}@{host}:443"
            "?type=tcp&security=reality&pbk=PUBLICKEYVALUE&fp=chrome"
            "&sni=front.example.com&sid=abcd1234&flow=xtls-rprx-vision#Reality"
        )

    def create_simple_pasarguard_group_source(self, *, group_id=29):
        panel = Panel.objects.create(
            store=self.store,
            name=f"PasarGuard simple {group_id}",
            family=Panel.Family.PASARGUARD,
            url="https://pasarguard.example.com",
            username="",
            password="pg-api-key-secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.PASARGUARD_GROUPS,
        )
        source = Inbound.objects.create(
            panel=panel,
            inbound_id=group_id,
            remark="Seller",
            protocol=Inbound.Protocol.VLESS,
            server_ip="pasarguard-native",
            port="0",
            config_params="{}",
            security=Inbound.Security.REALITY,
            is_active=False,
            available_for_new_orders=False,
            xui_source=Inbound.XUISource.PASARGUARD_GROUP,
            xui_remote_key=f"pasarguard_group:{group_id}",
            metadata={
                "remote_kind": "pasarguard_group",
                "group_id": group_id,
                "remote_source": "groups_simple",
                "disabled_known": False,
                "inbound_tags_known": False,
                "native_raw_delivery": True,
            },
        )
        return panel, source

    def test_factory_defaults_existing_panel_model_to_xui_adapter(self):
        from .panels import CapabilityFlag, get_panel_adapter
        from .panels.xui import XUIPanelAdapter

        self.assertEqual(self.panel.family, Panel.Family.XUI)
        adapter = get_panel_adapter(self.panel)
        report = adapter.detect_capabilities()

        self.assertIsInstance(adapter, XUIPanelAdapter)
        self.assertEqual(report.family, "xui")
        self.assertEqual(report.profile.profile, Panel.CapabilityProfile.MODERN_MULTI_NODE)
        self.assertTrue(report.profile.supports(CapabilityFlag.MULTI_INBOUND_CLIENTS))
        self.assertTrue(report.profile.supports(CapabilityFlag.PRECISE_CLIENT_SCOPE))
        report_dict = report.to_dict()
        self.assertEqual(report_dict["detected_version"], "3.4.0")
        self.assertTrue(report_dict["supports_create_client"])
        self.assertTrue(report_dict["supports_delete_client"])
        self.assertTrue(report_dict["supports_multi_inbound_create"])
        self.assertTrue(report_dict["supports_subscription"])
        self.assertTrue(report_dict["requires_csrf_for_login"])
        self.assertTrue(report_dict["requires_csrf_for_write"])
        self.assertEqual(report_dict["supported_protocols"], ["vless", "vmess", "trojan"])

    def test_factory_returns_xui_adapter_for_xui_legacy_profile(self):
        from .panels import get_panel_adapter
        from .panels.xui import XUIPanelAdapter

        self.panel.capability_profile = Panel.CapabilityProfile.LEGACY_SINGLE_NODE
        self.panel.detected_xui_version = "2.4.0"
        self.panel.save(update_fields=["capability_profile", "detected_xui_version", "updated_at"])
        cache.clear()

        adapter = get_panel_adapter(self.panel)
        report = adapter.get_capability_report()

        self.assertIsInstance(adapter, XUIPanelAdapter)
        self.assertEqual(report.family, "xui")
        self.assertEqual(report.capability_profile, Panel.CapabilityProfile.LEGACY_SINGLE_NODE)
        self.assertTrue(report.supports_create_client)
        self.assertFalse(report.supports_multi_inbound_create)

    def test_factory_returns_pasarguard_adapter_with_native_raw_capabilities(self):
        from .panels import CapabilityFlag, get_panel_adapter
        from .panels.pasarguard import PasarGuardPanelAdapter

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.username = ""
        self.panel.password = "pg-api-key"
        self.panel.capability_profile = Panel.CapabilityProfile.PASARGUARD_GROUPS
        self.panel.save(update_fields=["family", "username", "password", "capability_profile", "updated_at"])

        adapter = get_panel_adapter(self.panel)
        report = adapter.get_capability_report()
        report_dict = report.to_dict()

        self.assertIsInstance(adapter, PasarGuardPanelAdapter)
        self.assertEqual(report.family, "pasarguard")
        self.assertEqual(report.capability_profile, Panel.CapabilityProfile.PASARGUARD_GROUPS)
        self.assertTrue(report.supports_create_client)
        self.assertTrue(report.supports_multi_inbound_create)
        self.assertTrue(report.supports_multi_group_users)
        self.assertTrue(report.supports_subscription)
        self.assertTrue(report.supports_native_raw_configs)
        self.assertTrue(report.uses_api_key_auth)
        self.assertFalse(report.requires_csrf_for_login)
        self.assertIn(CapabilityFlag.NATIVE_RAW_CONFIGS, report.profile.flags)
        self.assertIn(CapabilityFlag.SELLABILITY_PROBE, report.profile.flags)
        self.assertEqual(report_dict["supported_protocols"], ["native_raw"])
        self.assertFalse(report_dict["metadata"]["local_link_reconstruction"])

    def test_factory_returns_safe_unsupported_marzban_adapter(self):
        from .panels import get_panel_adapter
        from .panels.errors import PanelOperationUnsupportedError

        self.panel.family = Panel.Family.MARZBAN
        self.panel.save(update_fields=["family", "updated_at"])
        adapter = get_panel_adapter(self.panel)
        report = adapter.get_capability_report()

        self.assertEqual(adapter.family, "marzban")
        self.assertFalse(report.supported)
        self.assertEqual(report.profile.profile, "unsupported_safe")
        self.assertIn("not implemented", report.warnings[0])
        with self.assertRaises(PanelOperationUnsupportedError):
            adapter.test_connection()

    def test_unknown_and_unsupported_family_return_safe_unsupported_report(self):
        from .panels import get_panel_adapter

        self.panel.family = Panel.Family.UNKNOWN
        self.panel.save(update_fields=["family", "updated_at"])
        unknown_adapter = get_panel_adapter(self.panel)
        unknown_report = unknown_adapter.get_capability_report()

        self.assertEqual(unknown_report.family, "unknown")
        self.assertFalse(unknown_report.supported)
        self.assertFalse(unknown_report.supports_login)
        self.assertFalse(unknown_report.supports_create_client)
        self.assertIn("unknown", " ".join(unknown_report.errors).lower())

        unsupported_adapter = get_panel_adapter(SimpleNamespace(family="wireguard"))
        unsupported_report = unsupported_adapter.get_capability_report()

        self.assertEqual(unsupported_report.family, "wireguard")
        self.assertFalse(unsupported_report.supported)
        self.assertIn("not supported", " ".join(unsupported_report.errors))

    def test_structured_panel_error_serializes_safely(self):
        from .panels.errors import PanelIntegrationError

        error = PanelIntegrationError(
            "Ø³Ø§Ø®Øª client Ù†Ø§Ù…ÙˆÙÙ‚ Ø¨ÙˆØ¯ token=secret-token",
            error_code="panel_create_client_failed",
            layer="panel_write",
            action="create_client",
            technical_detail="failed for vless://11111111-1111-4111-8111-111111111111@example.com:443 csrf-token secret-token",
            remediation="Run Sync capabilities.",
            panel=self.panel,
            inbound=self.inbound,
            safe_context={
                "password": "panel-secret",
                "uuid": "11111111-1111-4111-8111-111111111111",
                "direct_link": self.panel.url + "/sub/private-sub-id",
            },
        )

        payload = error.to_safe_dict()
        text = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["error_code"], "panel_create_client_failed")
        self.assertEqual(payload["layer"], "panel_write")
        self.assertEqual(payload["action"], "create_client")
        self.assertEqual(payload["panel_id"], self.panel.pk)
        self.assertEqual(payload["inbound_id"], self.inbound.pk)
        self.assertIn("Run Sync capabilities", payload["remediation"])
        self.assertNotIn("secret-token", text)
        self.assertNotIn("panel-secret", text)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", text)
        self.assertNotIn("vless://", text)
        self.assertNotIn("private-sub-id", text)

    def test_pasarguard_client_uses_x_api_key_and_redacts_errors(self):
        from .panels.pasarguard.client import PasarGuardClient
        from .panels.pasarguard.errors import PasarGuardAuthenticationError, PasarGuardWriteForbiddenError

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.username = ""
        self.panel.password = "pg-secret-key"
        self.panel.save(update_fields=["family", "username", "password", "updated_at"])
        session = DummyPasarGuardSession(responses=[DummyPasarGuardResponse({"version": "1.0.0"})])
        client = PasarGuardClient(self.panel, session=session)

        client.get_system()

        self.assertEqual(session.calls[0]["headers"]["X-Api-Key"], "pg-secret-key")
        self.assertNotIn("Authorization", session.calls[0]["headers"])

        token_url = "https://pasarguard.example.com/subpath/privateSubscriptionToken123456/raw"
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse(
                    {},
                    status_code=401,
                    text=f"bad key pg-secret-key vless://11111111-1111-4111-8111-111111111111@example.com:443 {token_url}",
                )
            ]
        )
        client = PasarGuardClient(self.panel, session=session)

        with self.assertRaises(PasarGuardAuthenticationError) as caught:
            client.get_system()

        payload = caught.exception.to_safe_dict()
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["error_code"], "pasarguard_auth_failed")
        self.assertNotIn("pg-secret-key", rendered)
        self.assertNotIn("vless://", rendered)
        self.assertNotIn("privateSubscriptionToken123456", rendered)

        session = DummyPasarGuardSession(
            responses=[DummyPasarGuardResponse({}, status_code=403, text="forbidden pg-secret-key")]
        )
        client = PasarGuardClient(self.panel, session=session)
        with self.assertRaises(PasarGuardWriteForbiddenError) as caught:
            client.create_user({"username": "blocked"})
        self.assertEqual(caught.exception.error_code, "pasarguard_write_forbidden")
        self.assertNotIn("pg-secret-key", json.dumps(caught.exception.to_safe_dict(), ensure_ascii=False))

    def test_pasarguard_adapter_group_listing_falls_back_to_simple_groups_on_full_read_forbidden(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.username = ""
        self.panel.password = "pg-secret-key"
        self.panel.save(update_fields=["family", "username", "password", "updated_at"])
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({}, status_code=403, text='{"detail":"Permission denied: groups.read"}'),
                DummyPasarGuardResponse([{"id": 29, "name": "Seller", "is_disabled": False}]),
            ]
        )
        adapter = PasarGuardPanelAdapter(self.panel, client=PasarGuardClient(self.panel, session=session))

        groups = adapter.list_inbounds()

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["id"], 29)
        self.assertEqual(groups[0]["name"], "Seller")
        self.assertEqual(groups[0]["inbound_tags"], [])
        self.assertEqual(groups[0]["is_disabled"], False)
        self.assertTrue(groups[0]["disabled_known"])
        self.assertTrue(session.calls[0]["url"].endswith("/api/groups"))
        self.assertTrue(session.calls[1]["url"].endswith("/api/groups/simple"))

    def test_pasarguard_simple_groups_do_not_assume_disabled_or_inbound_tags(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.username = ""
        self.panel.password = "pg-secret-key"
        self.panel.save(update_fields=["family", "username", "password", "updated_at"])
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({}, status_code=403, text='{"detail":"Permission denied: groups.read"}'),
                DummyPasarGuardResponse([{"id": 29, "name": "Seller"}]),
            ]
        )
        adapter = PasarGuardPanelAdapter(self.panel, client=PasarGuardClient(self.panel, session=session))

        groups = adapter.list_inbounds()

        self.assertEqual(groups[0]["id"], 29)
        self.assertIsNone(groups[0]["is_disabled"])
        self.assertFalse(groups[0]["disabled_known"])
        self.assertFalse(groups[0]["inbound_tags_known"])
        self.assertEqual(groups[0]["source"], "groups_simple")

    def test_pasarguard_adapter_creates_user_with_group_ids_and_native_raw_links(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter
        from .panels.pasarguard.schemas import PASARGUARD_USERNAME_RE
        from .panels.xui.adapter import XUIProvisioningRequest

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.username = ""
        self.panel.password = "pg-api-key"
        self.panel.capability_profile = Panel.CapabilityProfile.PASARGUARD_GROUPS
        self.panel.save(update_fields=["family", "username", "password", "capability_profile", "updated_at"])
        self.inbound.xui_source = Inbound.XUISource.PASARGUARD_GROUP
        self.inbound.xui_remote_key = "pasarguard_group:1"
        self.inbound.metadata = {"remote_kind": "pasarguard_group", "group_id": 1, "native_raw_delivery": True}
        self.inbound.security = Inbound.Security.REALITY
        self.inbound.pbk = ""
        self.inbound.save(update_fields=["xui_source", "xui_remote_key", "metadata", "security", "pbk", "updated_at"])
        raw_links = [
            "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000701@native.example.com:443?security=reality&pbk=PUBLICKEYVALUE&sni=front.example.com&sid=abcd1234&fp=chrome&flow=xtls-rprx-vision&type=tcp#Native",
            "trojan://password@native.example.com:443#Native-Trojan",
        ]
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"id": 1, "name": "Primary", "inbound_tags": ["tag-a"], "is_disabled": False}),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
                DummyPasarGuardResponse(
                    {"id": 10, "username": "filled-by-panel", "subscription_url": "https://pasarguard.example.com/s/privateToken123456"}
                ),
            ],
            raw_responses=[DummyPasarGuardResponse({}, text="\n".join(raw_links))],
        )
        adapter = PasarGuardPanelAdapter(self.panel, client=PasarGuardClient(self.panel, session=session))

        result = adapter.create_enabled_client(
            XUIProvisioningRequest(
                email_prefix="Alice Client",
                total_gb=Decimal("10"),
                duration_days=30,
                inbound=self.inbound,
                limit_ip=2,
                client_uuid="cccccccc-cccc-4ccc-8ccc-000000000701",
                sub_id="local-sub-id",
                email="alice-client-remote",
            )
        )

        create_call = session.calls[2]
        self.assertEqual(create_call["method"], "POST")
        self.assertTrue(create_call["url"].endswith("/api/user"))
        self.assertEqual(create_call["json"]["group_ids"], [1])
        self.assertEqual(create_call["json"]["status"], "active")
        self.assertEqual(create_call["json"]["data_limit"], 10 * 1024**3)
        self.assertEqual(create_call["json"]["data_limit_reset_strategy"], "no_reset")
        self.assertEqual(create_call["json"]["hwid_limit"], 2)
        self.assertNotEqual(create_call["json"]["expire"], 0)
        self.assertIn("qasedak:", create_call["json"]["note"])
        self.assertTrue(PASARGUARD_USERNAME_RE.match(create_call["json"]["username"]))
        self.assertEqual(session.get_calls[0]["url"], "https://pasarguard.example.com/s/privateToken123456/links")
        self.assertEqual(result["raw_links"], raw_links)
        self.assertEqual(result["direct_link"], raw_links[0])
        self.assertEqual(result["sub_link"], "https://pasarguard.example.com/s/privateToken123456")
        self.assertTrue(result["raw"]["native_raw_delivery"])

    def test_pasarguard_sellability_probe_success_creates_fetches_and_cleans_up(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter

        panel, source = self.create_simple_pasarguard_group_source(group_id=1)
        raw_links = [
            self.reality_link(720, host="probe.example.com"),
            "trojan://password@probe.example.com:443#Probe-Trojan",
        ]
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"id": 1, "name": "Seller", "inbound_tags": ["tag-a"], "is_disabled": False}),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
                DummyPasarGuardResponse({"id": 10, "subscription_url": "https://pasarguard.example.com/s/probeToken123456"}),
                DummyPasarGuardResponse({}, status_code=204, text=""),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
            ],
            raw_responses=[DummyPasarGuardResponse({}, text="\n".join(raw_links))],
        )
        adapter = PasarGuardPanelAdapter(panel, client=PasarGuardClient(panel, session=session))

        result = adapter.probe_source_sellability(source)

        self.assertTrue(result["ok"])
        self.assertEqual(result["observed_config_count"], 2)
        self.assertEqual(result["protocol_counts"]["vless"], 1)
        self.assertEqual(result["protocol_counts"]["trojan"], 1)
        self.assertEqual(result["reality_count"], 1)
        self.assertTrue(result["pbk_validation_ok"])
        self.assertTrue(result["cleanup_succeeded"])
        created_username = session.calls[2]["json"]["username"]
        self.assertTrue(created_username.startswith("qasedak_verify"))
        methods_paths = [(call["method"], urlsplit(call["url"]).path) for call in session.calls]
        self.assertEqual(
            methods_paths,
            [
                ("GET", "/api/group/1"),
                ("GET", f"/api/user/{created_username}"),
                ("POST", "/api/user"),
                ("DELETE", f"/api/user/{created_username}"),
                ("GET", f"/api/user/{created_username}"),
            ],
        )
        self.assertEqual(session.calls[2]["json"]["group_ids"], [1])
        self.assertEqual(session.calls[2]["json"]["data_limit"], 1073741)
        self.assertEqual(session.calls[2]["json"]["hwid_limit"], 1)
        self.assertTrue(session.get_calls[0]["url"].endswith("/s/probeToken123456/links"))
        self.assertNotIn("raw_links", result["safe_details"])
        self.assertNotIn("subscription_url", result["safe_details"])

    def test_pasarguard_sellability_probe_fails_reality_without_pbk_and_cleans_up(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter

        panel, source = self.create_simple_pasarguard_group_source(group_id=1)
        raw_link = (
            "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000721@probe.example.com:443"
            "?type=tcp&security=reality&sni=front.example.com#MissingPBK"
        )
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"id": 1, "name": "Seller", "inbound_tags": [], "is_disabled": False}),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
                DummyPasarGuardResponse({"id": 10, "subscription_url": "https://pasarguard.example.com/s/probeToken123456"}),
                DummyPasarGuardResponse({}, status_code=204, text=""),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
            ],
            raw_responses=[DummyPasarGuardResponse({}, text=raw_link)],
        )
        adapter = PasarGuardPanelAdapter(panel, client=PasarGuardClient(panel, session=session))

        result = adapter.probe_source_sellability(source)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "reality_pbk_missing")
        self.assertTrue(result["cleanup_succeeded"])
        self.assertEqual(result["safe_details"]["invalid_reasons"]["reality_missing_pbk"], 1)

    def test_pasarguard_sellability_probe_zero_configs_and_create_failure_are_safe_failures(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter

        panel, source = self.create_simple_pasarguard_group_source(group_id=1)
        empty_session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"id": 1, "name": "Seller", "inbound_tags": [], "is_disabled": False}),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
                DummyPasarGuardResponse({"id": 10, "subscription_url": "https://pasarguard.example.com/s/probeToken123456"}),
                DummyPasarGuardResponse({}, status_code=204, text=""),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
            ],
            raw_responses=[
                DummyPasarGuardResponse({}, text=""),
                DummyPasarGuardResponse({"body": {"links": []}}),
            ],
        )
        empty_adapter = PasarGuardPanelAdapter(panel, client=PasarGuardClient(panel, session=empty_session))

        empty_result = empty_adapter.probe_source_sellability(source)

        self.assertFalse(empty_result["ok"])
        self.assertEqual(empty_result["error_code"], "native_links_empty")
        self.assertTrue(empty_result["cleanup_succeeded"])

        create_failure_session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"id": 1, "name": "Seller", "inbound_tags": [], "is_disabled": False}),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
                DummyPasarGuardResponse({}, status_code=500, text="create failed pg-api-key-secret"),
            ]
        )
        create_failure_adapter = PasarGuardPanelAdapter(
            panel,
            client=PasarGuardClient(panel, session=create_failure_session),
        )

        create_failure_result = create_failure_adapter.probe_source_sellability(source)

        self.assertFalse(create_failure_result["ok"])
        self.assertEqual(create_failure_result["error_code"], "remote_create_failed")
        self.assertFalse(create_failure_result["cleanup_succeeded"])
        self.assertEqual(create_failure_adapter.client.session.get_calls, [])

    def test_pasarguard_sellability_probe_cleanup_failure_blocks_success(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter

        panel, source = self.create_simple_pasarguard_group_source(group_id=1)
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"id": 1, "name": "Seller", "inbound_tags": [], "is_disabled": False}),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
                DummyPasarGuardResponse({"id": 10, "subscription_url": "https://pasarguard.example.com/s/probeToken123456"}),
                DummyPasarGuardResponse({}, status_code=500, text="delete failed pg-api-key-secret"),
            ],
            raw_responses=[DummyPasarGuardResponse({}, text=self.reality_link(722, host="probe.example.com"))],
        )
        adapter = PasarGuardPanelAdapter(panel, client=PasarGuardClient(panel, session=session))

        result = adapter.probe_source_sellability(source)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "cleanup_failed")
        self.assertFalse(result["cleanup_succeeded"])
        self.assertTrue(result["safe_details"]["cleanup_attempted"])
        self.assertNotIn("pg-api-key-secret", json.dumps(result, ensure_ascii=False))

    def test_pasarguard_links_primary_succeeds_when_raw_browser_links_are_empty(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter
        from .panels.xui.adapter import XUIProvisioningRequest

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.username = ""
        self.panel.password = "pg-api-key"
        self.panel.capability_profile = Panel.CapabilityProfile.PASARGUARD_GROUPS
        self.panel.save(update_fields=["family", "username", "password", "capability_profile", "updated_at"])
        self.inbound.xui_source = Inbound.XUISource.PASARGUARD_GROUP
        self.inbound.xui_remote_key = "pasarguard_group:1"
        self.inbound.save(update_fields=["xui_source", "xui_remote_key", "updated_at"])
        raw_links = [
            "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000711@native.example.com:443?security=reality&pbk=PUBLICKEYVALUE&type=tcp#Native",
        ]
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"id": 1, "name": "Primary", "is_disabled": False}),
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
                DummyPasarGuardResponse({"id": 11, "subscription_url": "https://pasarguard.example.com/s/linksPrimary123456"}),
            ],
            raw_responses=[DummyPasarGuardResponse({}, text="\n".join(raw_links))],
        )
        adapter = PasarGuardPanelAdapter(self.panel, client=PasarGuardClient(self.panel, session=session))

        result = adapter.create_enabled_client(
            XUIProvisioningRequest(
                email_prefix="Links Primary",
                total_gb=Decimal("1"),
                duration_days=1,
                inbound=self.inbound,
                client_uuid="cccccccc-cccc-4ccc-8ccc-000000000711",
                sub_id="links-primary-sub",
                email="links-primary-remote",
            )
        )

        self.assertEqual(result["raw_links"], raw_links)
        self.assertEqual(len(session.get_calls), 1)
        self.assertTrue(session.get_calls[0]["url"].endswith("/links"))

    def test_pasarguard_links_exact_reality_line_preserved(self):
        from .panels.pasarguard import PasarGuardClient

        raw_link = (
            "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000712@native.example.com:443"
            "?security=reality&pbk=PUBLICKEYVALUE&sni=front.example.com&sid=abcd1234"
            "&fp=chrome&flow=xtls-rprx-vision&type=tcp#Reality"
        )
        self.panel.family = Panel.Family.PASARGUARD
        self.panel.password = "pg-api-key"
        self.panel.save(update_fields=["family", "password", "updated_at"])
        session = DummyPasarGuardSession(raw_responses=[DummyPasarGuardResponse({}, text=raw_link)])
        client = PasarGuardClient(self.panel, session=session)

        links = client.fetch_native_links("https://pasarguard.example.com/s/exactToken123456")

        self.assertEqual(links, [raw_link])
        self.assertIn("pbk=PUBLICKEYVALUE", links[0])
        self.assertTrue(session.get_calls[0]["url"].endswith("/links"))

    def test_pasarguard_links_disabled_returns_structured_error_without_raw_empty_misclassification(self):
        from .panels.pasarguard import PasarGuardClient
        from .panels.pasarguard.errors import PasarGuardReadError

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.password = "pg-api-key"
        self.panel.save(update_fields=["family", "password", "updated_at"])
        session = DummyPasarGuardSession(
            raw_responses=[
                DummyPasarGuardResponse({}, status_code=406, text="format disabled"),
                DummyPasarGuardResponse({"body": {"links": []}}),
            ]
        )
        client = PasarGuardClient(self.panel, session=session)

        with self.assertRaises(PasarGuardReadError) as caught:
            client.fetch_native_links("https://pasarguard.example.com/s/disabledToken123456")

        self.assertEqual(caught.exception.error_code, "pasarguard_links_format_disabled")
        self.assertEqual((caught.exception.safe_context or {}).get("raw_fallback", {}).get("error_code"), "pasarguard_raw_browser_links_disabled")
        self.assertNotEqual(caught.exception.error_code, "pasarguard_raw_empty")

    def test_pasarguard_raw_body_links_fallback_works_when_links_unavailable(self):
        from .panels.pasarguard import PasarGuardClient

        raw_link = "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000713@fallback.example.com:443#Fallback"
        self.panel.family = Panel.Family.PASARGUARD
        self.panel.password = "pg-api-key"
        self.panel.save(update_fields=["family", "password", "updated_at"])
        session = DummyPasarGuardSession(
            raw_responses=[
                DummyPasarGuardResponse({}, status_code=404, text="not found"),
                DummyPasarGuardResponse({"body": {"links": [raw_link]}}),
            ]
        )
        client = PasarGuardClient(self.panel, session=session)

        links = client.fetch_native_links("https://pasarguard.example.com/s/fallbackToken123456")

        self.assertEqual(links, [raw_link])
        self.assertTrue(session.get_calls[0]["url"].endswith("/links"))
        self.assertTrue(session.get_calls[1]["url"].endswith("/raw"))

    def test_pasarguard_adapter_reuses_existing_user_with_matching_context_marker(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter
        from .panels.pasarguard.schemas import pasarguard_note_marker
        from .panels.xui.adapter import XUIProvisioningRequest

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.username = ""
        self.panel.password = "pg-api-key"
        self.panel.capability_profile = Panel.CapabilityProfile.PASARGUARD_GROUPS
        self.panel.save(update_fields=["family", "username", "password", "capability_profile", "updated_at"])
        self.inbound.xui_source = Inbound.XUISource.PASARGUARD_GROUP
        self.inbound.xui_remote_key = "pasarguard_group:1"
        self.inbound.save(update_fields=["xui_source", "xui_remote_key", "updated_at"])
        request = XUIProvisioningRequest(
            email_prefix="Retry Client",
            total_gb=Decimal("10"),
            duration_days=30,
            inbound=self.inbound,
            limit_ip=1,
            client_uuid="dddddddd-dddd-4ddd-8ddd-000000000702",
            sub_id="retry-sub-id",
            email="retry-client-remote",
        )
        context = "|".join(
            [
                str(self.panel.pk),
                request.client_uuid,
                request.sub_id,
                request.email,
                "1",
            ]
        )
        marker = pasarguard_note_marker(context)
        raw_link = "vless://aaaaaaaa-aaaa-4aaa-8aaa-000000000702@native.example.com:443#Retry"
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"id": 1, "name": "Primary", "inbound_tags": [], "is_disabled": False}),
                DummyPasarGuardResponse(
                    {"id": 10, "username": "existing", "note": marker, "subscription_url": "https://pasarguard.example.com/s/retryToken123456"}
                ),
                DummyPasarGuardResponse(
                    {"id": 10, "username": "existing", "note": marker, "subscription_url": "https://pasarguard.example.com/s/retryToken123456"}
                ),
            ],
            raw_responses=[DummyPasarGuardResponse({}, text=raw_link)],
        )
        adapter = PasarGuardPanelAdapter(self.panel, client=PasarGuardClient(self.panel, session=session))

        result = adapter.create_enabled_client(request)

        methods = [call["method"] for call in session.calls]
        self.assertEqual(methods, ["GET", "GET", "PUT"])
        self.assertEqual(result["raw_links"], [raw_link])
        self.assertEqual(result["email"], session.calls[2]["json"]["username"])

    def test_pasarguard_adapter_lifecycle_methods_map_to_user_api(self):
        from .panels.pasarguard import PasarGuardClient, PasarGuardPanelAdapter

        self.panel.family = Panel.Family.PASARGUARD
        self.panel.username = ""
        self.panel.password = "pg-api-key"
        self.panel.save(update_fields=["family", "username", "password", "updated_at"])
        session = DummyPasarGuardSession(
            responses=[
                DummyPasarGuardResponse({"username": "pg_user"}),
                DummyPasarGuardResponse({"username": "pg_user", "status": "active"}),
                DummyPasarGuardResponse({"disabled": True}),
                DummyPasarGuardResponse({"disabled": False}),
                DummyPasarGuardResponse({"used_traffic": 0}),
                DummyPasarGuardResponse({"subscription_url": "https://pg.example.com/s/newToken123456"}),
                DummyPasarGuardResponse({}, status_code=204, text=""),
            ]
        )
        adapter = PasarGuardPanelAdapter(self.panel, client=PasarGuardClient(self.panel, session=session))

        adapter.get_user("pg_user")
        adapter.modify_user("pg_user", {"username": "pg_user", "data_limit": 1024})
        adapter.disable_user("pg_user")
        adapter.enable_user("pg_user")
        adapter.reset_user_usage("pg_user")
        adapter.revoke_subscription("pg_user")
        adapter.delete_client(self.inbound, "pg_user")

        methods_paths = [(call["method"], urlsplit(call["url"]).path) for call in session.calls]
        self.assertEqual(
            methods_paths,
            [
                ("GET", "/api/user/pg_user"),
                ("PUT", "/api/user/pg_user"),
                ("PUT", "/api/user/pg_user/disabled"),
                ("PUT", "/api/user/pg_user/disabled"),
                ("POST", "/api/user/pg_user/reset"),
                ("POST", "/api/user/pg_user/revoke_sub"),
                ("DELETE", "/api/user/pg_user"),
            ],
        )

    def test_unsupported_family_operation_raises_structured_error(self):
        from .panels import get_panel_adapter
        from .panels.errors import UnsupportedPanelFamilyError
        from .panels.xui.adapter import XUIProvisioningRequest

        self.panel.family = Panel.Family.UNKNOWN
        self.panel.save(update_fields=["family", "updated_at"])
        adapter = get_panel_adapter(self.panel)

        with self.assertRaises(UnsupportedPanelFamilyError) as caught:
            adapter.create_enabled_client(XUIProvisioningRequest("prefix", Decimal("1"), 30, inbound=self.inbound))

        payload = caught.exception.to_safe_dict()
        self.assertEqual(payload["error_code"], "unsupported_panel_family")
        self.assertEqual(payload["layer"], "adapter_factory")
        self.assertEqual(payload["action"], "create_client")
        self.assertEqual(payload["panel_id"], self.panel.pk)
        self.assertEqual(payload["remote_inbound_id"], self.inbound.inbound_id)
        self.assertIn("Sync capabilities", payload["remediation"])

    def test_capability_report_redacts_sensitive_metadata(self):
        from .panels.capabilities import CapabilityFlag, CapabilityProfile, PanelCapabilityReport

        report = PanelCapabilityReport(
            family="xui",
            profile=CapabilityProfile(
                family="xui",
                profile=Panel.CapabilityProfile.MODERN_MULTI_NODE,
                version="3.5.0",
                flags=frozenset({CapabilityFlag.LOGIN}),
                metadata={
                    "csrf_token": "csrf-secret-token",
                    "nested": {
                        "uuid": "11111111-1111-4111-8111-111111111111",
                        "note": "vless://11111111-1111-4111-8111-111111111111@example.com:443",
                        "url": "https://admin:password@panel.example.com/secret",
                    },
                },
            ),
            metadata={
                "cookie": "session-secret",
                "subscription_link": "https://panel.example.com/sub/private-sub-id",
            },
        )

        text = json.dumps(report.to_dict(), ensure_ascii=False)

        self.assertNotIn("csrf-secret-token", text)
        self.assertNotIn("session-secret", text)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", text)
        self.assertNotIn("vless://", text)
        self.assertNotIn("admin:password", text)
        self.assertNotIn("private-sub-id", text)

    def test_xui_adapter_delegates_read_and_write_to_injected_service(self):
        from .panels.xui.adapter import XUIPanelAdapter, XUIProvisioningRequest

        service = Mock()
        service.authenticated_json.return_value = {"success": True, "obj": [{"id": self.inbound.inbound_id}]}
        service.create_enabled_client.return_value = {"uuid": "client-id"}

        adapter = XUIPanelAdapter(self.panel, service=service)

        self.assertEqual(adapter.list_inbounds(), [{"id": self.inbound.inbound_id}])
        result = adapter.create_enabled_client(
            XUIProvisioningRequest(
                email_prefix="alice",
                total_gb=Decimal("1"),
                duration_days=30,
                inbound=self.inbound,
                limit_ip=2,
                client_uuid="client-id",
                sub_id="sub-id",
                email="alice_client",
            )
        )

        self.assertEqual(result["uuid"], "client-id")
        service.authenticated_json.assert_called_once_with("GET", "/panel/api/inbounds/list")
        service.create_enabled_client.assert_called_once()
        self.assertEqual(service.create_enabled_client.call_args.kwargs["duration_hours"], 720)
        self.assertEqual(service.create_enabled_client.call_args.kwargs["inbound"], self.inbound)

    def test_xui_adapter_delegates_multi_inbound_write_to_injected_service(self):
        from .panels.xui.adapter import XUIPanelAdapter, XUIProvisioningRequest

        second_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=2,
            remark="Second",
            protocol=Inbound.Protocol.VLESS,
            server_ip="vpn2.example.com",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        service = Mock()
        service.create_enabled_multi_inbound_client.return_value = {"bundle_inbound_ids": [1, 2]}

        adapter = XUIPanelAdapter(self.panel, service=service)
        result = adapter.create_enabled_multi_inbound_client(
            XUIProvisioningRequest(
                email_prefix="route-test",
                total_gb=Decimal("1"),
                duration_days=30,
                inbounds=[self.inbound, second_inbound],
                limit_ip=2,
                client_uuid="client-id",
                sub_id="sub-id",
                email="route-test",
            )
        )

        self.assertEqual(result["bundle_inbound_ids"], [1, 2])
        service.create_enabled_multi_inbound_client.assert_called_once()
        self.assertEqual(service.create_enabled_multi_inbound_client.call_args.kwargs["inbounds"], [self.inbound, second_inbound])


class ModernPaidProvisioningTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
        )
        self.customer = Customer.objects.create(display_name="Alice", username="alice")
        self.plan = Plan.objects.create(
            store=self.store,
            name="30 days",
            slug="30-days-modern",
            price=100000,
            volume_gb=Decimal("10"),
            duration_days=30,
            device_limit=2,
            is_active=True,
            is_public=True,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Modern panel",
            url="https://modern.example.com",
            username="admin",
            password="secret",
            is_active=True,
            capability_profile=Panel.CapabilityProfile.MODERN_MULTI_NODE,
            detected_xui_version="3.4.0",
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=7,
            xui_node_id="node-alpha",
            xui_node_name="Node Alpha",
            remark="Modern node inbound",
            protocol=Inbound.Protocol.VLESS,
            server_ip="vpn.example.com",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
            available_for_new_orders=True,
        )
        PlanInboundRoute.objects.create(store=self.store, plan=self.plan, inbound=self.inbound, priority=1)

    def create_paid_order(self):
        return create_manual_payment_order(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            sender_card_name="Alice Buyer",
            sender_card_last4="1234",
            payment_time=time(14, 35),
            metadata={"source": "modern_paid_test"},
        )

    def remote_result_for_order(self, order, *, index=1):
        from .provisioning_services import order_identity

        identity = order_identity(order, self.inbound, index=index)
        result = fake_client_result(identity["uuid"])
        result.update(
            {
                "email": identity["email"],
                "sub_id": identity["sub_id"],
                "xui_node_id": self.inbound.xui_node_id,
                "remote_client_key": f"{self.panel.pk}:{self.inbound.xui_node_id}:{self.inbound.inbound_id}:{identity['uuid']}",
                "raw": {"id": identity["uuid"], "email": identity["email"], "enable": True},
            }
        )
        return result

    def remote_result_for_bundle(self, order, inbound, inbounds, *, index=1):
        from .provisioning_services import multi_inbound_order_identity

        identity = multi_inbound_order_identity(order, inbounds, index=index)
        result = fake_client_result(identity["uuid"])
        result.update(
            {
                "email": identity["email"],
                "sub_id": identity["sub_id"],
                "sub_link": f"https://modern.example.com:2096/sub/{identity['sub_id']}",
                "direct_link": f"vless://{identity['uuid']}@{inbound.server_ip}:{inbound.port}#bundle-{inbound.inbound_id}",
                "xui_node_id": inbound.xui_node_id,
                "remote_client_key": f"{self.panel.pk}:{inbound.xui_node_id}:{inbound.inbound_id}:{identity['uuid']}",
                "raw": {"id": identity["uuid"], "email": identity["email"], "enable": True},
            }
        )
        return result

    @patch("store.order_services.create_inactive_client_details")
    def test_modern_paid_checkout_defers_remote_create_and_freezes_scope(self, inactive_create):
        result = self.create_paid_order()

        self.assertTrue(result.success)
        inactive_create.assert_not_called()
        order = result.order
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.PENDING)
        self.assertFalse(order.uuid)
        self.assertFalse(order.vpn_clients.exists())
        self.assertEqual(order.metadata["provisioning_strategy"], "deferred_create_enabled")
        self.assertEqual(order.metadata["provisioning_scope"]["panel_id"], self.panel.pk)
        self.assertEqual(order.metadata["provisioning_scope"]["node_id"], "node-alpha")
        self.assertEqual(order.metadata["provisioning_scope"]["xui_inbound_id"], self.inbound.inbound_id)

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_modern_paid_approval_creates_enabled_after_payment_and_completes_after_verify(self, lookup_remote, create_enabled):
        order = self.create_paid_order().order
        remote = self.remote_result_for_order(order)
        lookup_remote.side_effect = [None, remote]
        create_enabled.return_value = remote

        result = activate_order(order, notify=False)

        self.assertTrue(result.success)
        create_enabled.assert_called_once()
        self.assertEqual(lookup_remote.call_count, 2)
        order.refresh_from_db()
        client = order.vpn_clients.get()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.PROVISIONED)
        self.assertEqual(client.status, VPNClient.Status.ACTIVE)
        self.assertEqual(client.inbound, self.inbound)
        self.assertEqual(client.xui_node_id, "node-alpha")
        self.assertEqual(order.uuid, remote["uuid"])

    @patch("store.provisioning_services.create_enabled_client_details", return_value=None)
    @patch("store.provisioning_services.lookup_existing_remote_client", return_value=None)
    def test_modern_remote_create_failure_does_not_complete_or_create_active_client(self, lookup_remote, create_enabled):
        order = self.create_paid_order().order

        result = activate_order(order, notify=False)

        self.assertFalse(result.success)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.FAILED)
        self.assertFalse(order.vpn_clients.filter(status=VPNClient.Status.ACTIVE).exists())
        create_enabled.assert_called_once()
        self.assertEqual(lookup_remote.call_count, 1)

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_central_approval_refuses_terminal_or_rejected_payment_states(self, lookup_remote, create_enabled):
        cases = (
            (Order.Status.REJECTED, Order.VerificationStatus.PENDING),
            (Order.Status.CANCELLED, Order.VerificationStatus.PENDING),
            (Order.Status.PENDING_VERIFICATION, Order.VerificationStatus.REJECTED),
        )
        for status, verification_status in cases:
            with self.subTest(status=status, verification_status=verification_status):
                order = self.create_paid_order().order
                Order.objects.filter(pk=order.pk).update(
                    status=status,
                    verification_status=verification_status,
                )
                order.refresh_from_db()
                lookup_remote.reset_mock()
                create_enabled.reset_mock()

                result = activate_order(order, notify=False)

                self.assertFalse(result.success)
                lookup_remote.assert_not_called()
                create_enabled.assert_not_called()
                order.refresh_from_db()
                self.assertEqual(order.status, status)
                self.assertEqual(order.verification_status, verification_status)
                self.assertFalse(order.vpn_clients.exists())

    def test_nullable_order_relation_lock_query_is_postgres_compatible(self):
        from .db_locking import select_for_update_self

        order = self.create_paid_order().order
        Order.objects.filter(pk=order.pk).update(store=None, customer=None, inbound=None)

        with transaction.atomic():
            locked = select_for_update_self(
                Order.objects.select_related("store", "customer", "plan", "inbound", "inbound__panel")
            ).get(pk=order.pk)

        self.assertEqual(locked.pk, order.pk)
        self.assertIsNone(locked.customer_id)
        self.assertIsNone(locked.inbound_id)

    def test_nullable_vpn_client_relation_lock_query_is_postgres_compatible(self):
        from .db_locking import select_for_update_self

        vpn_client = VPNClient.objects.create(
            store=None,
            order=None,
            plan=None,
            inbound=None,
            username="orphan-client",
            xui_email="orphan-client",
            uuid="00000000-0000-4000-8000-000000000001",
            status=VPNClient.Status.INACTIVE,
        )

        with transaction.atomic():
            locked = select_for_update_self(
                VPNClient.objects.select_related(
                    "store",
                    "order",
                    "order__customer",
                    "plan",
                    "inbound",
                    "inbound__panel",
                )
            ).get(pk=vpn_client.pk)

        self.assertEqual(locked.pk, vpn_client.pk)
        self.assertIsNone(locked.order_id)
        self.assertIsNone(locked.inbound_id)

    def test_approval_lock_path_handles_nullable_relations_before_remote_lookup(self):
        order = self.create_paid_order().order
        Order.objects.filter(pk=order.pk).update(
            store=None,
            customer=None,
            inbound=None,
            status=Order.Status.REJECTED,
        )
        order.refresh_from_db()

        with patch("store.provisioning_services.lookup_existing_remote_client") as lookup_remote, patch(
            "store.provisioning_services.create_enabled_client_details"
        ) as create_enabled:
            result = activate_order(order, notify=False)

        self.assertFalse(result.success)
        lookup_remote.assert_not_called()
        create_enabled.assert_not_called()

    def test_approval_lock_path_handles_existing_related_rows(self):
        order = self.create_paid_order().order
        Order.objects.filter(pk=order.pk).update(
            status=Order.Status.COMPLETED,
            verification_status=Order.VerificationStatus.VERIFIED,
        )
        order.refresh_from_db()

        with patch("store.provisioning_services.lookup_existing_remote_client") as lookup_remote, patch(
            "store.provisioning_services.create_enabled_client_details"
        ) as create_enabled:
            result = activate_order(order, notify=False)

        self.assertTrue(result.success)
        lookup_remote.assert_not_called()
        create_enabled.assert_not_called()

    def test_admin_notification_claim_lock_handles_nullable_order_relations(self):
        from .admin_notifications import _claim_notification

        order = self.create_paid_order().order
        Order.objects.filter(pk=order.pk).update(store=None, customer=None, inbound=None)

        claimed = _claim_notification(order.pk, "admin_notified_at")

        self.assertEqual(claimed.pk, order.pk)
        claimed.refresh_from_db()
        self.assertIsNotNone(claimed.admin_notified_at)

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_modern_approval_refuses_changed_frozen_node_scope_before_remote_call(self, lookup_remote, create_enabled):
        order = self.create_paid_order().order
        self.inbound.xui_node_id = "node-beta"
        self.inbound.xui_node_name = "Node Beta"
        self.inbound.save(update_fields=["xui_node_id", "xui_node_name", "updated_at"])

        result = activate_order(order, notify=False)

        self.assertFalse(result.success)
        lookup_remote.assert_not_called()
        create_enabled.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.FAILED)
        self.assertFalse(order.vpn_clients.exists())
        self.assertIn("frozen_provisioning_scope", order.last_provisioning_error)

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_modern_remote_verify_failure_does_not_complete_or_create_local_client(self, lookup_remote, create_enabled):
        order = self.create_paid_order().order
        remote = self.remote_result_for_order(order)
        lookup_remote.side_effect = [None, None]
        create_enabled.return_value = remote

        result = activate_order(order, notify=False)

        self.assertFalse(result.success)
        create_enabled.assert_called_once()
        self.assertEqual(lookup_remote.call_count, 2)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.FAILED)
        self.assertFalse(order.vpn_clients.exists())

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_modern_admin_direct_purchase_uses_direct_create_enabled_strategy(self, lookup_remote, create_enabled):
        result = create_manual_payment_order(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            sender_card_name="Admin Buyer",
            sender_card_last4="",
            payment_time=time(15, 0),
            metadata={"admin_direct_purchase": True, "source": "test_admin_direct"},
        )
        self.assertTrue(result.success)
        order = result.order
        remote = self.remote_result_for_order(order)
        lookup_remote.side_effect = [None, remote]
        create_enabled.return_value = remote

        activation = activate_order(order, notify=False)

        self.assertTrue(activation.success)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.metadata["provisioning_strategy"], "direct_create_enabled")
        create_enabled.assert_called_once()

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_modern_retry_reuses_existing_remote_and_does_not_duplicate_local_client(self, lookup_remote, create_enabled):
        order = self.create_paid_order().order
        remote = self.remote_result_for_order(order)
        lookup_remote.side_effect = [None, remote]
        create_enabled.return_value = remote
        first = activate_order(order, notify=False)
        self.assertTrue(first.success)

        second = activate_order(order, notify=False)

        self.assertTrue(second.success)
        self.assertEqual(VPNClient.objects.filter(order=order).count(), 1)
        create_enabled.assert_called_once()

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_modern_bulk_paid_approval_creates_requested_client_count(self, lookup_remote, create_enabled):
        result = create_manual_payment_order(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            inbound=self.inbound,
            sender_card_name="Bulk Buyer",
            sender_card_last4="1234",
            payment_time=time(16, 0),
            quantity=2,
            metadata={"source": "modern_bulk"},
        )
        self.assertTrue(result.success)
        order = result.order
        first_remote = self.remote_result_for_order(order, index=1)
        second_remote = self.remote_result_for_order(order, index=2)
        lookup_remote.side_effect = [None, first_remote, None, second_remote]
        create_enabled.side_effect = [first_remote, second_remote]

        activation = activate_order(order, notify=False)

        self.assertTrue(activation.success)
        order.refresh_from_db()
        clients = list(order.vpn_clients.order_by("created_at", "pk"))
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(len(clients), 2)
        self.assertNotEqual(clients[0].uuid, clients[1].uuid)
        self.assertEqual(create_enabled.call_count, 2)

    @patch("store.provisioning_services.create_enabled_multi_inbound_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_modern_multi_inbound_bundle_creates_one_remote_client_attached_to_all_routes(
        self,
        lookup_remote,
        create_multi,
    ):
        self.plan.multi_inbound_bundle = True
        self.plan.save(update_fields=["multi_inbound_bundle", "updated_at"])
        second_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=8,
            xui_node_id="node-beta",
            xui_node_name="Node Beta",
            remark="Second node inbound",
            protocol=Inbound.Protocol.VLESS,
            server_ip="vpn-beta.example.com",
            port="8443",
            config_params="type=tcp&security=none",
            is_active=True,
            available_for_new_orders=True,
        )
        PlanInboundRoute.objects.create(store=self.store, plan=self.plan, inbound=second_inbound, priority=2)
        result = create_manual_payment_order(
            store=self.store,
            customer=self.customer,
            plan=self.plan,
            sender_card_name="Bundle Buyer",
            sender_card_last4="1234",
            payment_time=time(17, 0),
            metadata={"source": "modern_bundle"},
        )
        self.assertTrue(result.success)
        order = result.order
        bundle_inbounds = [self.inbound, second_inbound]
        first_remote = self.remote_result_for_bundle(order, self.inbound, bundle_inbounds)
        second_remote = self.remote_result_for_bundle(order, second_inbound, bundle_inbounds)
        first_remote["expires_at"] = timezone.now() + timedelta(days=30)
        second_remote["expires_at"] = timezone.now() + timedelta(days=30)
        lookup_remote.side_effect = [None, None, first_remote, second_remote]
        create_multi.return_value = {"bundle_inbound_results": [first_remote, second_remote]}

        activation = activate_order(order, notify=False)

        self.assertTrue(activation.success)
        order.refresh_from_db()
        clients = list(order.vpn_clients.order_by("inbound_id"))
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertTrue(order.metadata["multi_inbound_bundle"])
        self.assertEqual(len(order.metadata["provisioning_scopes"]), 2)
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].inbound_id, self.inbound.pk)
        self.assertEqual(clients[0].uuid, first_remote["uuid"])
        self.assertEqual(clients[0].sub_id, first_remote["sub_id"])
        self.assertEqual(len(clients[0].xui_raw["bundle_inbound_results"]), 2)
        self.assertIsInstance(clients[0].xui_raw["bundle_inbound_results"][0]["expires_at"], str)
        create_multi.assert_called_once()
        self.assertEqual([inbound.pk for inbound in create_multi.call_args.kwargs["inbounds"]], [self.inbound.pk, second_inbound.pk])

    @patch("store.provisioning_services.create_enabled_client_details")
    @patch("store.provisioning_services.lookup_existing_remote_client")
    def test_modern_existing_remote_on_retry_is_reused_after_prior_failure(self, lookup_remote, create_enabled):
        order = self.create_paid_order().order
        remote = self.remote_result_for_order(order)
        lookup_remote.side_effect = [Exception("timeout"), remote, remote]
        create_enabled.return_value = remote

        first = activate_order(order, notify=False)
        self.assertFalse(first.success)
        order.refresh_from_db()
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.FAILED)

        second = activate_order(order, notify=False)

        self.assertTrue(second.success)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(VPNClient.objects.filter(order=order).count(), 1)
        create_enabled.assert_not_called()

    @patch("store.management.commands.reconcile_order_provisioning.lookup_existing_remote_client", return_value=None)
    def test_reconcile_order_provisioning_dry_run_does_not_write(self, _lookup_remote):
        order = self.create_paid_order().order
        stdout = StringIO()

        call_command("reconcile_order_provisioning", "--order-id", str(order.pk), "--dry-run", stdout=stdout)

        order.refresh_from_db()
        self.assertEqual(order.provisioning_status, Order.ProvisioningStatus.PENDING)
        self.assertIn("state=remote_missing", stdout.getvalue())


class PanelHealthServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
            panel_monitor_alert_cooldown_minutes=30,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Germany 1",
            url="https://panel.example.com/secret",
            username="admin",
            password="panel-password",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Main",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

    def service_mock(self, *, login_side_effect=None, inbound_payload=None, inbound_side_effect=None):
        service = Mock()
        service.login.side_effect = login_side_effect
        service.get_inbound.return_value = inbound_payload or {
            "id": self.inbound.inbound_id,
            "protocol": self.inbound.protocol,
            "remark": self.inbound.remark,
            "enable": True,
        }
        if inbound_side_effect is not None:
            service.get_inbound.side_effect = inbound_side_effect
        service.authenticated_json.side_effect = lambda method, path, **kwargs: {
            "/panel/api/server/getPanelUpdateInfo": {"success": True, "obj": {"version": "2.9.4"}},
            "/panel/api/server/status": {"success": True, "obj": {"panelVersion": "2.9.4"}},
            "/panel/api/inbounds/list": {
                "success": True,
                "obj": [
                    {
                        "id": self.inbound.inbound_id,
                        "remark": self.inbound.remark,
                        "protocol": self.inbound.protocol,
                    }
                ],
            },
            "/panel/api/nodes/list": {"success": True, "obj": []},
            "/panel/api/hosts/list": {"success": True, "obj": []},
        }.get(path, {"success": True, "obj": []})
        return service

    def enable_panel_health_alerts(self, *, threshold=2, repeat_interval=60, recovery=True, quiet=False, quiet_start=None, quiet_end=None):
        self.store.panel_health_alerts_enabled = True
        self.store.panel_health_alert_failure_threshold_count = threshold
        self.store.panel_health_alert_repeat_interval_minutes = repeat_interval
        self.store.panel_health_recovery_alert_enabled = recovery
        self.store.panel_health_quiet_hours_enabled = quiet
        self.store.panel_health_quiet_hours_start = quiet_start
        self.store.panel_health_quiet_hours_end = quiet_end
        self.store.save(
            update_fields=[
                "panel_health_alerts_enabled",
                "panel_health_alert_failure_threshold_count",
                "panel_health_alert_repeat_interval_minutes",
                "panel_health_recovery_alert_enabled",
                "panel_health_quiet_hours_enabled",
                "panel_health_quiet_hours_start",
                "panel_health_quiet_hours_end",
                "updated_at",
            ]
        )

    def test_panel_ok_records_status_and_log(self):
        from .panel_health_services import check_panel_health

        service = self.service_mock()
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        health = PanelHealthStatus.objects.get(panel=self.panel)
        self.assertEqual(health.status, PanelHealthStatus.Status.OK)
        log = PanelHealthCheckLog.objects.get(panel=self.panel)
        self.assertTrue(log.login_ok)
        self.assertEqual(log.inbounds_ok, 1)

    def test_panel_login_fail_records_error(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        service = self.service_mock(login_side_effect=XUIError("Panel login failed for panel-password"))
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.ERROR)
        self.assertEqual(result["error_code"], "auth_failed")
        self.assertNotIn("panel-password", str(result))
        self.assertEqual(PanelHealthCheckLog.objects.get(panel=self.panel).status, PanelHealthStatus.Status.ERROR)

    def test_panel_login_403_persists_safe_structured_error(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        service = self.service_mock(
            login_side_effect=XUIError(
                "Panel login failed with HTTP 403.",
                category="http_403_csrf_required",
                http_status=403,
                endpoint="login",
                response_snippet="Forbidden csrf-secret-token panel-password",
                remediation_hint="CSRF/login flow mismatch; use the 3X-UI 3.5 CSRF login flow.",
            )
        )
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.ERROR)
        self.assertEqual(result["error_code"], "http_403_csrf_required")
        self.assertIn("HTTP 403", result["error_message"])
        health = PanelHealthStatus.objects.get(panel=self.panel)
        log = PanelHealthCheckLog.objects.get(panel=self.panel)
        self.assertEqual(health.metadata["http_status"], 403)
        self.assertEqual(log.error_code, "http_403_csrf_required")
        payload = json.dumps({"result": result, "health": health.metadata, "log": log.metadata}, ensure_ascii=False, default=str)
        self.assertNotIn("panel-password", payload)
        self.assertNotIn("csrf-secret-token", payload)

    def test_panel_two_factor_persists_safe_structured_error(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        service = self.service_mock(
            login_side_effect=XUIError(
                "Two-factor login is enabled for this panel account.",
                category="two_factor_required",
                endpoint="getTwoFactorEnable",
                remediation_hint="Disable two-factor login for this panel account.",
            )
        )
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["error_code"], "two_factor_required")
        self.assertEqual(
            result["error_message"],
            "ÙˆØ±ÙˆØ¯ Ø¯Ùˆ Ù…Ø±Ø­Ù„Ù‡â€ŒØ§ÛŒ Ø¨Ø±Ø§ÛŒ Ø§ÛŒÙ† Ù¾Ù†Ù„ ÙØ¹Ø§Ù„ Ø§Ø³Øª Ùˆ Ø§ØªØµØ§Ù„ Ø®ÙˆØ¯Ú©Ø§Ø± Ù¾Ø´ØªÛŒØ¨Ø§Ù†ÛŒ Ù†Ù…ÛŒâ€ŒØ´ÙˆØ¯.",
        )
        self.assertEqual(PanelHealthStatus.objects.get(panel=self.panel).error_code, "two_factor_required")

    def test_panel_timeout_is_caught_and_sanitized(self):
        from .panel_health_services import check_panel_health

        service = self.service_mock(
            login_side_effect=requests.Timeout("timeout for https://panel.example.com/secret with panel-password")
        )
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.ERROR)
        self.assertEqual(result["error_code"], "network_timeout")
        payload = json.dumps(PanelHealthCheckLog.objects.get(panel=self.panel).metadata, ensure_ascii=False)
        self.assertNotIn("https://panel.example.com/secret", payload)
        self.assertNotIn("panel-password", payload)

    def test_check_panel_health_verbose_outputs_safe_error_details(self):
        from .xui_api import XUIError

        service = self.service_mock(
            login_side_effect=XUIError(
                "Panel login failed with HTTP 403.",
                category="http_403_csrf_required",
                http_status=403,
                endpoint="login",
                response_snippet="Forbidden csrf-secret-token panel-password",
                remediation_hint="CSRF/login flow mismatch",
            )
        )
        stdout = StringIO()
        with patch("store.panel_health_services.XUIService", return_value=service):
            call_command("check_panel_health", "--panel-id", str(self.panel.pk), "--verbose", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn(f"panel={self.panel.pk}", output)
        self.assertIn("error_code=http_403_csrf_required", output)
        self.assertIn("http_status=403", output)
        self.assertIn("CSRF/login flow mismatch", output)
        self.assertNotIn("panel-password", output)
        self.assertNotIn("csrf-secret-token", output)

    def test_panel_admin_change_page_displays_safe_health_error_details(self):
        User = get_user_model()
        User.objects.create_superuser("owner", "owner@example.com", "password")
        self.client.login(username="owner", password="password")
        PanelHealthStatus.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.ERROR,
            last_checked_at=timezone.now(),
            error_code="http_403_csrf_required",
            error_message="ÙˆØ±ÙˆØ¯ Ø¨Ù‡ Ù¾Ù†Ù„ Ø¨Ø§ Ø®Ø·Ø§ÛŒ HTTP 403 Ù†Ø§Ù…ÙˆÙÙ‚ Ø´Ø¯.",
            summary="ÙˆØ±ÙˆØ¯ Ø¨Ù‡ Ù¾Ù†Ù„ Ø¨Ø§ Ø®Ø·Ø§ÛŒ HTTP 403 Ù†Ø§Ù…ÙˆÙÙ‚ Ø´Ø¯.",
            metadata={
                "http_status": 403,
                "endpoint": "login",
                "remediation_hint": "CSRF/login flow mismatch",
                "response_snippet": "Forbidden <csrf-redacted>",
            },
        )

        response = self.client.get(reverse("admin:store_panel_change", args=[self.panel.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "http_403_csrf_required")
        self.assertContains(response, "HTTP 403")
        self.assertContains(response, "CSRF/login flow mismatch")
        self.assertNotContains(response, "csrf-secret-token")

    def test_store_admin_shows_alert_settings_with_masked_recipients(self):
        User = get_user_model()
        User.objects.create_superuser("owner", "owner@example.com", "password")
        self.client.login(username="owner", password="password")
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:secret-token",
            admin_user_id="1234567890",
            additional_admin_user_ids="9876543210",
        )

        response = self.client.get(reverse("admin:store_store_change", args=[self.store.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ù‡Ø´Ø¯Ø§Ø± Ø³Ù„Ø§Ù…Øª Ù¾Ù†Ù„â€ŒÙ‡Ø§")
        self.assertContains(response, "ÙØ¹Ø§Ù„â€ŒØ³Ø§Ø²ÛŒ Ù‡Ø´Ø¯Ø§Ø± Ø®Ø±Ø§Ø¨ÛŒ Ù¾Ù†Ù„")
        self.assertContains(response, "1234...7890")
        self.assertContains(response, "9876...3210")
        self.assertNotContains(response, "1234567890")
        self.assertNotContains(response, "9876543210")
        self.assertNotContains(response, "123:secret-token")

    def test_panel_health_alert_settings_admin_page_loads(self):
        User = get_user_model()
        User.objects.create_superuser("owner", "owner@example.com", "password")
        self.client.login(username="owner", password="password")

        changelist = self.client.get(reverse("admin:store_panelhealthalertsettings_changelist"))
        change = self.client.get(reverse("admin:store_panelhealthalertsettings_change", args=[self.store.pk]))

        self.assertEqual(changelist.status_code, 200)
        self.assertEqual(change.status_code, 200)
        self.assertContains(changelist, "ØªÙ†Ø¸ÛŒÙ…Ø§Øª Ù‡Ø´Ø¯Ø§Ø± Ø³Ù„Ø§Ù…Øª Ù¾Ù†Ù„â€ŒÙ‡Ø§")
        self.assertContains(change, "ØªÙ†Ø¸ÛŒÙ…Ø§Øª Ù‡Ø´Ø¯Ø§Ø± Ø³Ù„Ø§Ù…Øª Ù¾Ù†Ù„â€ŒÙ‡Ø§")
        self.assertContains(change, "Ø§ÛŒÙ† ØªÙ†Ø¸ÛŒÙ…Ø§Øª ÙÙ‚Ø· Ø¨Ø±Ø§ÛŒ Ù¾ÛŒØ§Ù…â€ŒÙ‡Ø§ÛŒ Ø§Ø¯Ù…ÛŒÙ† Ø§Ø³ØªÙØ§Ø¯Ù‡ Ù…ÛŒâ€ŒØ´ÙˆØ¯")
        self.assertContains(change, "ÙØ¹Ø§Ù„â€ŒØ³Ø§Ø²ÛŒ Ø¨Ø±Ø±Ø³ÛŒ Ø³Ù„Ø§Ù…Øª Ù¾Ù†Ù„")
        self.assertContains(change, "ØªÙ†Ø¸ÛŒÙ…Ø§Øª Ø±Ø¨Ø§Øª")

    def test_panel_health_alert_settings_hidden_from_jazzmin_sidebar(self):
        infrastructure_items = settings.JAZZMIN_SETTINGS["custom_links"]["Infrastructure"]

        self.assertNotIn({"model": "store.PanelHealthAlertSettings"}, infrastructure_items)
        self.assertIn("store.PanelHealthAlertSettings", settings.JAZZMIN_SETTINGS["hide_models"])
        self.assertIn({"model": "store.Panel"}, infrastructure_items)
        self.assertIn({"model": "store.Inbound"}, infrastructure_items)
        self.assertIn({"model": "store.BotConfiguration"}, infrastructure_items)

    def test_alert_settings_validate_positive_intervals(self):
        self.store.panel_health_alert_check_interval_minutes = 0
        self.store.panel_health_alert_repeat_interval_minutes = 0
        self.store.panel_health_alert_failure_threshold_count = 0

        with self.assertRaises(ValidationError) as ctx:
            self.store.full_clean()

        self.assertIn("panel_health_alert_check_interval_minutes", ctx.exception.message_dict)
        self.assertIn("panel_health_alert_repeat_interval_minutes", ctx.exception.message_dict)
        self.assertIn("panel_health_alert_failure_threshold_count", ctx.exception.message_dict)

    def test_alert_settings_quiet_hours_require_both_times(self):
        self.store.panel_health_quiet_hours_enabled = True
        self.store.panel_health_quiet_hours_start = time(23, 0)
        self.store.panel_health_quiet_hours_end = None

        with self.assertRaises(ValidationError) as ctx:
            self.store.full_clean()

        self.assertIn("panel_health_quiet_hours_enabled", ctx.exception.message_dict)

    def test_panel_admin_alert_buttons_toggle_panel(self):
        User = get_user_model()
        User.objects.create_superuser("owner", "owner@example.com", "password")
        self.client.login(username="owner", password="password")

        disable_response = self.client.get(reverse("admin:store_panel_health_alert_disable", args=[self.panel.pk]))
        self.panel.refresh_from_db()
        self.assertEqual(disable_response.status_code, 302)
        self.assertFalse(self.panel.health_alert_enabled)

        enable_response = self.client.get(reverse("admin:store_panel_health_alert_enable", args=[self.panel.pk]))
        self.panel.refresh_from_db()
        self.assertEqual(enable_response.status_code, 302)
        self.assertTrue(self.panel.health_alert_enabled)

    def test_panel_admin_test_message_uses_admin_alert_service(self):
        User = get_user_model()
        User.objects.create_superuser("owner", "owner@example.com", "password")
        self.client.login(username="owner", password="password")

        confirm = self.client.get(reverse("admin:store_panel_health_alert_test", args=[self.panel.pk]))
        self.assertEqual(confirm.status_code, 200)
        self.assertContains(confirm, "ØªØ£ÛŒÛŒØ¯ Ø§Ø±Ø³Ø§Ù„ Ù¾ÛŒØ§Ù… ØªØ³ØªÛŒ Ù‡Ø´Ø¯Ø§Ø± Ø³Ù„Ø§Ù…Øª")

        with patch(
            "store.panel_health_services.send_admin_message_to_telegram_admins",
            return_value={"attempted": 1, "sent": 1, "failed": 0},
        ) as send_mock:
            response = self.client.post(
                reverse("admin:store_panel_health_alert_test", args=[self.panel.pk]),
                {"confirm": "yes"},
            )

        self.assertEqual(response.status_code, 302)
        send_mock.assert_called_once()

    def test_panel_admin_health_action_message_uses_safe_reason(self):
        from .xui_api import XUIError

        User = get_user_model()
        User.objects.create_superuser("owner", "owner@example.com", "password")
        self.client.login(username="owner", password="password")
        service = self.service_mock(
            login_side_effect=XUIError(
                "Panel login failed with HTTP 403 panel-password csrf-secret-token",
                category="http_403_csrf_required",
                http_status=403,
                endpoint="login",
                response_snippet="Forbidden panel-password csrf-secret-token",
                remediation_hint="CSRF/login flow mismatch",
            )
        )

        with patch("store.panel_health_services.XUIService", return_value=service):
            response = self.client.post(
                reverse("admin:store_panel_changelist"),
                {
                    "action": "run_health_check",
                    "_selected_action": [str(self.panel.pk)],
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Health check failed")
        self.assertContains(response, "HTTP 403 - CSRF/login flow mismatch")
        self.assertNotContains(response, "panel-password")
        self.assertNotContains(response, "csrf-secret-token")

    def test_missing_inbound_becomes_warning(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        service = self.service_mock(inbound_side_effect=XUIError("Inbound was not found on panel."))
        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.WARNING)
        self.assertEqual(result["inbounds_error"], 1)
        self.assertEqual(PanelHealthStatus.objects.get(panel=self.panel).status, PanelHealthStatus.Status.WARNING)

    def test_node_offline_becomes_health_warning_metadata(self):
        from .panel_health_services import check_panel_health

        service = self.service_mock()

        def api_response(method, path, **kwargs):
            if path == "/panel/api/server/getPanelUpdateInfo":
                return {"success": True, "obj": {"version": "3.4.0"}}
            if path == "/panel/api/server/status":
                return {"success": True, "obj": {"panelVersion": "3.4.0"}}
            if path == "/panel/api/inbounds/list":
                return {
                    "success": True,
                    "obj": [
                        {
                            "id": self.inbound.inbound_id,
                            "remark": self.inbound.remark,
                            "protocol": self.inbound.protocol,
                            "nodeId": "node-guid-alpha",
                        }
                    ],
                }
            if path == "/panel/api/nodes/list":
                return {
                    "success": True,
                    "obj": [
                        {
                            "guid": "node-guid-alpha",
                            "name": "Node A",
                            "status": "offline",
                        }
                    ],
                }
            return {"success": True, "obj": []}

        service.authenticated_json.side_effect = api_response

        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.WARNING)
        self.assertEqual(result["metadata"]["node_issue_count"], 1)
        self.assertEqual(result["metadata"]["compatibility"]["profile"], Panel.CapabilityProfile.MODERN_MULTI_NODE)

    def test_health_monitor_disabled_inbound_is_ignored(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        self.inbound.available_for_new_orders = False
        self.inbound.health_monitor_enabled = False
        self.inbound.legacy_note = "Legacy inbound kept for old clients."
        self.inbound.save(
            update_fields=[
                "available_for_new_orders",
                "health_monitor_enabled",
                "legacy_note",
                "updated_at",
            ]
        )
        service = self.service_mock(inbound_side_effect=XUIError("Inbound was not found on panel."))

        with patch("store.panel_health_services.XUIService", return_value=service):
            result = check_panel_health(self.panel)

        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        self.assertEqual(result["inbounds_checked"], 0)
        self.assertEqual(result["metadata"]["ignored_inbounds"], 1)
        self.assertEqual(result["metadata"]["ignored_inbound_ids"], [self.inbound.inbound_id])
        service.get_inbound.assert_not_called()

    def test_status_transition_ok_to_error_sends_alert(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        self.store.panel_health_alerts_enabled = True
        self.store.panel_health_alert_failure_threshold_count = 1
        self.store.save(
            update_fields=[
                "panel_health_alerts_enabled",
                "panel_health_alert_failure_threshold_count",
                "updated_at",
            ]
        )
        BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:test",
            admin_user_id="42",
        )
        PanelHealthStatus.objects.create(panel=self.panel, status=PanelHealthStatus.Status.OK, last_ok_at=timezone.now())
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))

        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch(
                "store.panel_health_services.send_admin_message_to_telegram_admins",
                return_value={"attempted": 1, "sent": 1, "failed": 0},
            ) as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        self.assertEqual(result["alert_sent_count"], 1)
        send_mock.assert_called_once()
        health = PanelHealthStatus.objects.get(panel=self.panel)
        self.assertIsNotNone(health.last_alert_sent_at)
        self.assertEqual(health.last_alert_error_code, "auth_failed")
        self.assertTrue(health.last_alert_message_hash)
        self.assertTrue(PanelHealthCheckLog.objects.get(panel=self.panel).alert_sent)

    def test_alerts_disabled_never_sends(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        send_mock.assert_not_called()
        self.assertTrue(result["alert_skipped"])
        self.assertEqual(result["alert_skip_reason"], "alerts_disabled")

    def test_failure_below_threshold_does_not_alert(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        self.enable_panel_health_alerts(threshold=2)
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        send_mock.assert_not_called()
        self.assertEqual(result["consecutive_failure_count"], 1)
        self.assertEqual(result["alert_skip_reason"], "failure_threshold")

    def test_failure_reaching_threshold_sends_alert(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        self.enable_panel_health_alerts(threshold=2)
        PanelHealthStatus.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.ERROR,
            last_error_at=timezone.now() - timedelta(minutes=5),
            consecutive_failures=1,
        )
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch(
                "store.panel_health_services.send_admin_message_to_telegram_admins",
                return_value={"attempted": 1, "sent": 1, "failed": 0},
            ) as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        self.assertEqual(result["consecutive_failure_count"], 2)
        self.assertEqual(result["alert_sent_count"], 1)
        send_mock.assert_called_once()

    def test_repeated_error_before_cooldown_does_not_alert(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        self.store.panel_health_alerts_enabled = True
        self.store.panel_health_alert_failure_threshold_count = 1
        self.store.save(
            update_fields=[
                "panel_health_alerts_enabled",
                "panel_health_alert_failure_threshold_count",
                "updated_at",
            ]
        )
        PanelHealthStatus.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.ERROR,
            last_error_at=timezone.now(),
            last_alert_sent_at=timezone.now(),
            consecutive_failures=2,
        )
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        send_mock.assert_not_called()
        self.assertTrue(result["alert_skipped"])
        self.assertEqual(result["alert_skip_reason"], "repeat_interval")

    def test_repeated_error_after_repeat_interval_sends_alert(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        self.enable_panel_health_alerts(threshold=1, repeat_interval=60)
        PanelHealthStatus.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.ERROR,
            last_error_at=timezone.now() - timedelta(minutes=80),
            last_alert_sent_at=timezone.now() - timedelta(minutes=61),
            consecutive_failures=3,
        )
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch(
                "store.panel_health_services.send_admin_message_to_telegram_admins",
                return_value={"attempted": 1, "sent": 1, "failed": 0},
            ) as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        self.assertEqual(result["alert_sent_count"], 1)
        send_mock.assert_called_once()

    def test_recovery_does_not_send_when_disabled(self):
        from .panel_health_services import check_panel_health

        self.enable_panel_health_alerts(recovery=False)
        PanelHealthStatus.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.ERROR,
            last_error_at=timezone.now() - timedelta(minutes=21),
            last_alert_sent_at=timezone.now() - timedelta(minutes=20),
        )
        service = self.service_mock()
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        send_mock.assert_not_called()
        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        self.assertEqual(result["alert_skip_reason"], "recovery_disabled")

    def test_quiet_hours_suppress_alerts(self):
        from .jalali import TEHRAN_TZ
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        local_now = timezone.localtime(timezone.now(), TEHRAN_TZ)
        self.enable_panel_health_alerts(
            threshold=1,
            quiet=True,
            quiet_start=(local_now - timedelta(minutes=5)).time(),
            quiet_end=(local_now + timedelta(minutes=5)).time(),
        )
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        send_mock.assert_not_called()
        self.assertEqual(result["alert_skip_reason"], "quiet_hours")

    def test_panel_alert_disabled_skips_alert(self):
        from .panel_health_services import check_panel_health
        from .xui_api import XUIError

        self.enable_panel_health_alerts(threshold=1)
        self.panel.health_alert_enabled = False
        self.panel.save(update_fields=["health_alert_enabled", "updated_at"])
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        send_mock.assert_not_called()
        self.assertEqual(result["alert_skip_reason"], "panel_alert_disabled")

    def test_alert_message_masks_secrets_and_links(self):
        from .panel_health_services import format_panel_health_alert_message

        result = {
            "checked_at": timezone.now(),
            "status": PanelHealthStatus.Status.ERROR,
            "error_code": "auth_failed",
            "error_message": (
                "failed panel-password https://panel.example.com/secret "
                "vless://uuid@example.com /sub/panel-sub-token"
            ),
            "consecutive_failure_count": 2,
        }

        message = format_panel_health_alert_message(self.panel, result)

        self.assertNotIn("panel-password", message)
        self.assertNotIn("https://panel.example.com/secret", message)
        self.assertNotIn("vless://", message)
        self.assertNotIn("panel-sub-token", message)

    def test_error_to_ok_sends_recovery_alert(self):
        from .panel_health_services import check_panel_health

        self.store.panel_health_alerts_enabled = True
        self.store.save(update_fields=["panel_health_alerts_enabled", "updated_at"])
        PanelHealthStatus.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.ERROR,
            last_error_at=timezone.now() - timedelta(minutes=21),
            last_alert_sent_at=timezone.now() - timedelta(minutes=20),
        )
        service = self.service_mock()
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch(
                "store.panel_health_services.send_admin_message_to_telegram_admins",
                return_value={"attempted": 1, "sent": 1, "failed": 0},
            ) as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True)

        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        self.assertEqual(result["alert_sent_count"], 1)
        self.assertGreaterEqual(result["downtime_minutes"], 20)
        send_mock.assert_called_once()
        self.assertIsNotNone(PanelHealthStatus.objects.get(panel=self.panel).last_recovery_alert_sent_at)

    def test_disabled_panel_is_not_connected_to(self):
        from .panel_health_services import check_panel_health

        self.panel.is_active = False
        self.panel.save(update_fields=["is_active", "updated_at"])
        with patch("store.panel_health_services.XUIService") as service_cls:
            result = check_panel_health(self.panel)

        service_cls.assert_not_called()
        self.assertEqual(result["status"], PanelHealthStatus.Status.DISABLED)
        self.assertEqual(PanelHealthCheckLog.objects.get(panel=self.panel).status, PanelHealthStatus.Status.DISABLED)

    def test_dry_run_does_not_log_or_alert(self):
        from .panel_health_services import check_panel_health

        service = self.service_mock()
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            result = check_panel_health(self.panel, send_alerts=True, dry_run=True)

        self.assertEqual(result["status"], PanelHealthStatus.Status.OK)
        self.assertEqual(PanelHealthStatus.objects.count(), 0)
        self.assertEqual(PanelHealthCheckLog.objects.count(), 0)
        send_mock.assert_not_called()

    def test_command_dry_run_summary(self):
        service = self.service_mock()
        stdout = StringIO()
        with patch("store.panel_health_services.XUIService", return_value=service):
            call_command("check_panel_health", "--dry-run", "--verbose", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("total_panels=1", output)
        self.assertIn("checked=1", output)
        self.assertIn("ok=1", output)
        self.assertEqual(PanelHealthCheckLog.objects.count(), 0)

    def test_panel_health_alerts_command_dry_run_sends_nothing(self):
        from .xui_api import XUIError

        self.enable_panel_health_alerts(threshold=1)
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        stdout = StringIO()
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.panel_health_services.send_admin_message_to_telegram_admins") as send_mock,
        ):
            call_command("panel_health_alerts", "--panel-id", str(self.panel.pk), "--dry-run", "--verbose", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Panel health alerts summary", output)
        self.assertIn("would_send=1", output)
        self.assertIn("dry_run=True", output)
        send_mock.assert_not_called()
        self.assertEqual(PanelHealthCheckLog.objects.count(), 0)

    def test_panel_health_alerts_command_sends_only_admin_recipients(self):
        from .xui_api import XUIError

        self.enable_panel_health_alerts(threshold=1)
        bot_config = BotConfiguration.objects.create(
            store=self.store,
            provider=BotConfiguration.Provider.TELEGRAM,
            bot_token="123:test",
            admin_user_id="42",
            additional_admin_user_ids="43",
        )
        customer = Customer.objects.create(username="bob", display_name="Bob")
        BotUser.objects.create(
            bot_config=bot_config,
            customer=customer,
            provider_user_id="customer-999",
            chat_id="999",
        )
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        stdout = StringIO()
        with (
            patch("store.panel_health_services.XUIService", return_value=service),
            patch("store.telegram_bot.client.BotClient.send_message", return_value={}) as send_mock,
        ):
            call_command("panel_health_alerts", "--panel-id", str(self.panel.pk), stdout=stdout)

        sent_chat_ids = [call.kwargs["chat_id"] for call in send_mock.call_args_list]
        self.assertEqual(sent_chat_ids, ["42", "43"])
        self.assertNotIn("999", sent_chat_ids)
        self.assertIn("alerts_sent=2", stdout.getvalue())

    def test_panel_health_alerts_command_panel_id_limits_checks(self):
        from .xui_api import XUIError

        other_panel = Panel.objects.create(
            store=self.store,
            name="France 1",
            url="https://fr.example.com",
            username="admin",
            password="other-password",
            is_active=True,
        )
        Inbound.objects.create(
            panel=other_panel,
            inbound_id=2,
            remark="Other",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        self.enable_panel_health_alerts(threshold=1)
        service = self.service_mock(login_side_effect=XUIError("Panel login failed."))
        stdout = StringIO()
        with patch("store.panel_health_services.XUIService", return_value=service):
            call_command("panel_health_alerts", "--panel-id", str(self.panel.pk), "--dry-run", stdout=stdout)

        self.assertIn("total_panels=1", stdout.getvalue())

    def test_cleanup_old_logs(self):
        from .panel_health_services import cleanup_old_panel_health_logs

        old_log = PanelHealthCheckLog.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.OK,
            checked_at=timezone.now() - timedelta(days=40),
            login_ok=True,
        )
        new_log = PanelHealthCheckLog.objects.create(
            panel=self.panel,
            status=PanelHealthStatus.Status.OK,
            checked_at=timezone.now(),
            login_ok=True,
        )

        summary = cleanup_old_panel_health_logs(self.store)

        self.assertEqual(summary["deleted"], 1)
        self.assertFalse(PanelHealthCheckLog.objects.filter(pk=old_log.pk).exists())
        self.assertTrue(PanelHealthCheckLog.objects.filter(pk=new_log.pk).exists())


class PanelUsageServiceTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="VPN Store",
            english_name="VPN Store",
            card_number="0000000000000000",
            card_owner="VPN Store",
            panel_usage_active_user_method=Store.PanelUsageActiveUserMethod.MIXED,
        )
        self.panel = Panel.objects.create(
            store=self.store,
            name="Germany 1",
            url="https://panel.example.com/secret",
            username="admin",
            password="panel-password",
            is_active=True,
        )
        self.inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=1,
            remark="Main",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="443",
            config_params="type=tcp&security=none",
            is_active=True,
        )
        self.report_date = date(2026, 6, 5)
        self.tz = ZoneInfo("Asia/Tehran")
        self.period_start = datetime(2026, 6, 5, 0, 0, tzinfo=self.tz)
        self.period_end = datetime(2026, 6, 6, 0, 0, tzinfo=self.tz)

    def inbound_payload(self, clients=None, stats=None):
        return {
            "id": self.inbound.inbound_id,
            "protocol": "vless",
            "remark": "Main",
            "settings": json.dumps({"clients": clients or []}),
            "clientStats": stats or [],
        }

    def service_mock(self, *, login_side_effect=None, inbound_side_effect=None, online_clients=None):
        service = Mock()
        service.login.side_effect = login_side_effect
        service.get_online_clients.return_value = set(online_clients or [])
        if inbound_side_effect is not None:
            service.get_inbound.side_effect = inbound_side_effect
        else:
            service.get_inbound.return_value = self.inbound_payload(
                clients=[
                    {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "email": "alice@example.com",
                        "enable": True,
                    }
                ],
                stats=[
                    {
                        "email": "alice@example.com",
                        "up": 100,
                        "down": 200,
                        "total": 1000,
                        "expiryTime": 0,
                        "enable": True,
                    }
                ],
            )
        return service

    def create_panel_snapshot(
        self,
        captured_at,
        *,
        total_upload=0,
        total_download=0,
        status=PanelUsageSnapshot.Status.OK,
        clients_count=0,
    ):
        return PanelUsageSnapshot.objects.create(
            panel=self.panel,
            captured_at=captured_at,
            status=status,
            total_upload_bytes=total_upload,
            total_download_bytes=total_download,
            total_used_bytes=total_upload + total_download,
            clients_count=clients_count,
            checked_inbounds_count=1,
            active_inbounds_count=1,
        )

    def create_client_snapshot(self, captured_at, identifier_hash, used, *, online=None):
        return PanelClientUsageSnapshot.objects.create(
            panel=self.panel,
            inbound=self.inbound,
            captured_at=captured_at,
            client_identifier_hash=identifier_hash,
            client_identifier_masked=f"{identifier_hash[:2]}***",
            email_masked=f"{identifier_hash[:2]}***",
            upload_bytes=used,
            download_bytes=0,
            used_bytes=used,
            online=online,
            source="clientStats",
        )

    def test_collect_panel_usage_snapshot_with_healthy_xui_response(self):
        from .panel_usage_services import collect_panel_usage_snapshot

        service = self.service_mock(online_clients={"alice@example.com"})
        with patch("store.xui_api.XUIService", return_value=service):
            result = collect_panel_usage_snapshot(self.panel)

        self.assertEqual(result["status"], PanelUsageSnapshot.Status.OK)
        snapshot = PanelUsageSnapshot.objects.get(panel=self.panel)
        self.assertEqual(snapshot.total_used_bytes, 300)
        self.assertEqual(snapshot.clients_count, 1)
        self.assertEqual(snapshot.online_clients_count, 1)
        client_snapshot = PanelClientUsageSnapshot.objects.get(panel=self.panel)
        self.assertEqual(client_snapshot.used_bytes, 300)
        self.assertTrue(client_snapshot.online)
        payload = json.dumps(
            {
                "identifier": client_snapshot.client_identifier_hash,
                "masked": client_snapshot.client_identifier_masked,
                "email": client_snapshot.email_masked,
                "metadata": client_snapshot.metadata,
            },
            ensure_ascii=False,
        )
        self.assertNotIn("alice@example.com", payload)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", payload)

    def test_collect_panel_usage_snapshot_dedupes_same_client_across_inbounds(self):
        from .panel_usage_services import collect_panel_usage_snapshot

        Inbound.objects.create(
            panel=self.panel,
            inbound_id=2,
            remark="Node mirror",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="8443",
            config_params="type=tcp&security=none",
            is_active=True,
            xui_node_id="node-a-guid",
            xui_node_name="Node A",
            xui_source=Inbound.XUISource.SYNCHRONIZED_NODE,
        )
        payload = self.inbound_payload(
            clients=[
                {
                    "id": "client-alpha",
                    "email": "alice@example.com",
                    "enable": True,
                }
            ],
            stats=[
                {
                    "email": "alice@example.com",
                    "up": 100,
                    "down": 200,
                    "total": 1000,
                    "enable": True,
                }
            ],
        )
        service = self.service_mock(inbound_side_effect=lambda inbound, *, use_cache=True: payload)

        with patch("store.xui_api.XUIService", return_value=service):
            result = collect_panel_usage_snapshot(self.panel)

        snapshot = PanelUsageSnapshot.objects.get(panel=self.panel)
        self.assertEqual(result["status"], PanelUsageSnapshot.Status.OK)
        self.assertEqual(snapshot.total_used_bytes, 300)
        self.assertEqual(snapshot.clients_count, 1)
        self.assertEqual(snapshot.metadata["duplicate_usage_count"], 1)

    def test_collect_panel_usage_snapshot_partial_when_one_inbound_fails(self):
        from .panel_usage_services import collect_panel_usage_snapshot
        from .xui_api import XUIError

        second_inbound = Inbound.objects.create(
            panel=self.panel,
            inbound_id=2,
            remark="Backup",
            protocol=Inbound.Protocol.VLESS,
            server_ip="127.0.0.1",
            port="8443",
            config_params="type=tcp&security=none",
            is_active=True,
        )

        def get_inbound(inbound_or_id, *, use_cache=True):
            remote_id = getattr(inbound_or_id, "inbound_id", inbound_or_id)
            if remote_id == second_inbound.inbound_id:
                raise XUIError("Inbound was not found on https://panel.example.com/secret with panel-password")
            return self.inbound_payload(
                clients=[{"id": "client-1", "email": "alice@example.com"}],
                stats=[{"email": "alice@example.com", "up": 10, "down": 5}],
            )

        service = self.service_mock(inbound_side_effect=get_inbound)
        with patch("store.xui_api.XUIService", return_value=service):
            result = collect_ë]xÓÆòµë(š+myÓƒrrÂu&Wf–WrrÂw&Wf–Wuö6†V6²rÂrrÂÂrrÂrrÂrrÂrrÂrrÂÂrrÂrrÂÂÂrrÂrr’%Ò¢¢ÆVv7•v—¥v—¤–×÷'E&÷ræö&¦V7G2æ7&VFR€¢¦ö#Ö¦ö"À¢FVÆVw&Õ÷W6W%ö–CÒ#sƒsƒsƒsƒr"À¢FVÆVw&Õ÷W6W%ö–EöÖ6¶VCÒ#s‚¢¢£sƒr"À¢7FGW3ÔÆVv7•v—¥v—¤–×÷'E&÷rå7FGW2äU„•5D”ärÀ¢7W7FöÖW#Ö7W7FöÖW"À¢¢÷WBÒ7G&–æt”ò‚ ¢6ÆÅö6öÖÖæB‚&6†V6µö–çFVw&F–öç2"Â"ÒÖæòÖf–Â"Â7FF÷WCÖ÷WB ¢6VÆbæ76W'D–â‚#–×÷'FVBÆVv7’7W7FöÖW"‡2’"Â÷WBævWGfÇVR‚’  ¦6Æ72'&öF67D6×–våFW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ%eâ7F÷&R"À¢VævÆ—6…öæÖSÒ%eâ7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò%eâ7F÷&R"À¢&æµöæÖSÒ%FW7B&æ²"À¢F÷ö7W7FöÖW'5öÆ–Ö—CÓÀ¢'&öF67E÷&FUöÆ–Ö—E÷W%÷6V6öæCÓÀ¢'&öF67EöÖ…÷&V6—–VçG5÷W%ö6×–vãÓÀ¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ$'&öF67BRt""À¢6ÇVsÒ&'&öF67BÓVv""À¢föÇVÖUöv#ÔFV6–ÖÂ‚#Rã"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5ö7F—fSÕG'VRÀ¢—5÷V&Æ–3ÕG'VRÀ¢¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò'&öF67B"À¢&÷E÷Fö¶VãÒ'FVÆVw&Ò×Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢—5ö7F—fSÕG'VRÀ¢¢6VÆbçW&ÂÒ&WfW'6R‚&&÷E÷vV&†öö²"Â&w3Õ·6VÆbæ&÷Eö6öæf–rç&÷f–FW"Â6VÆbæ&÷Eö6öæf–rçvV&†ööµ÷6V7&WEÒ¢6VÆbææ÷rÒF–ÖW¦öæRææ÷r‚¢6VÆbæ7W7FöÖW%ö6÷VçFW"Ò ¢6VÆbçWV–Eö6÷VçFW"Ò  ¢FVb7W7FöÖW"‡6VÆbÂæÖRÂ¢Â—5ö7F—fSÕG'VR“ ¢6VÆbæ7W7FöÖW%ö6÷VçFW"³Ò¢&WGW&â7W7FöÖW"æö&¦V7G2æ7&VFR€¢F—7Æ•öæÖSÖæÖRÀ¢W6W&æÖSÖb&'&öF67E÷W6W%÷·6VÆbæ7W7FöÖW%ö6÷VçFW'Ò"À¢—5ö7F—fSÖ—5ö7F—fRÀ¢ ¢FVb÷&FW"‡6VÆbÂ7W7FöÖW"Â¢ÂÖ÷VçCÓÂF—5övóÓ“ ¢W&6†6VEöBÒ6VÆbææ÷rÒF–ÖVFVÇF†F—3ÖF—5övò¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢Ö÷VçCÖÖ÷VçBÀ¢÷&–v–æÅöÖ÷VçCÖÖ÷VçBÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢fW&–f–VEöC×W&6†6VEöBÀ¢¢÷&FW"æö&¦V7G2æf–ÇFW"‡³Ö÷&FW"ç²’çWFFR†7&VFVEöC×W&6†6VEöBÂfW&–f–VEöC×W&6†6VEöB¢÷&FW"æ7&VFVEöBÒW&6†6VEö@¢÷&FW"çfW&–f–VEöBÒW&6†6VEö@¢&WGW&â÷&FW  ¢FVb&÷E÷W6W"‡6VÆbÂ7W7FöÖW"Â¢Â6†Eö–CÒ#C""Â&÷Eö6öæf–sÔæöæR“ ¢&÷Eö6öæf–rÒ&÷Eö6öæf–r÷"6VÆbæ&÷Eö6öæf–p¢&WGW&â&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–sÖ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–C×7G"†6†Eö–B’À¢6†Eö–C×7G"†6†Eö–B’À¢W6W&æÖSÖb&'&öF67E÷¶6†Eö–GÒ"À¢F—7Æ•öæÖSÖb$'&öF67B¶6†Eö–GÒ"À¢ ¢FVb6×–vâ‡6VÆbÂ¢ÂVF–Væ6U÷G—SÔ'&öF67DÖW76vRäVF–Væ6UG—RäÄÂÂ6†ææVÃÔ'&öF67DÖW76vRä6†ææVÂåDTÄTu$Ò“ ¢&WGW&â'&öF67DÖW76vRæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢F—FÆSÒ%FW7B'&öF67B"À¢ÖW76vU÷FW‡CÒ-‹=˜MŠ}˜R˜]‹MŠ­‹¸Â"À¢VF–Væ6U÷G—SÖVF–Væ6U÷G—RÀ¢6†ææVÃÖ6†ææVÂÀ¢ ¢FVb÷7E÷WFFR‡6VÆbÂ–ÆöB“ ¢&WGW&â6VÆbæ6Æ–VçBç÷7B€¢6VÆbçW&ÂÀ¢FFÖ§6öâæGV×2‡–ÆöB’À¢6öçFVçE÷G—SÒ&Æ–6F–öâö§6öâ"À¢ ¢FVbÖW76vR‡6VÆbÂFW‡BÂ¢ÂÖW76vUö–CÓÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"Âf—'7EöæÖSÒ$FÖ–â"“ ¢&WGW&â°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢ÖW76vUö–BÀ¢&g&öÒ#¢²&–B#¢W6W%ö–BÂ'W6W&æÖR#¢W6W&æÖRÂ&f—'7EöæÖR#¢f—'7EöæÖWÒÀ¢&6†B#¢²&–B#¢W6W%ö–BÂ'G—R#¢'&—fFR'ÒÀ¢'FW‡B#¢FW‡BÀ¢Ð¢Ð ¢FVb6ÆÆ&6²‡6VÆbÂFFÂ¢ÂÖW76vUö–CÓÂ6ÆÆ&6µö–CÒ&&2Ö6""ÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"Âf—'7EöæÖSÒ$FÖ–â"“ ¢&WGW&â°¢&6ÆÆ&6µ÷VW'’#¢°¢&–B#¢6ÆÆ&6µö–BÀ¢&g&öÒ#¢²&–B#¢W6W%ö–BÂ'W6W&æÖR#¢W6W&æÖRÂ&f—'7EöæÖR#¢f—'7EöæÖWÒÀ¢&ÖW76vR#¢²&ÖW76vUö–B#¢ÖW76vUö–BÂ&6†B#¢²&–B#¢W6W%ö–BÂ'G—R#¢'&—fFR'×ÒÀ¢&FF#¢FFÀ¢Ð¢Ð ¢FVbFW7E÷&W6öÇfUöVF–Væ6U÷&WGW&ç5öÆÅö7F—fUö7W7FöÖW'2‡6VÆb“ ¢f—'7BÒ6VÆbæ7W7FöÖW"‚$f—'7B"¢6V6öæBÒ6VÆbæ7W7FöÖW"‚%6V6öæB"¢–æ7F—fRÒ6VÆbæ7W7FöÖW"‚$–æ7F—fR66÷VçB"Â—5ö7F—fSÔfÇ6R¢6VÆbæ&÷E÷W6W"†f—'7BÂ6†Eö–CÒ#"¢6VÆbæ&÷E÷W6W"‡6V6öæBÂ6†Eö–CÒ#""¢6VÆbæ&÷E÷W6W"†–æ7F—fRÂ6†Eö–CÒ#2" ¢7W7FöÖW'2ÒÆ—7B†vWEö7W7FöÖW'5öf÷%öVF–Væ6R„'&öF67DÖW76vRäVF–Væ6UG—RäÄÂÂ7F÷&S×6VÆbç7F÷&R’ ¢6VÆbæ76W'D–â†f—'7BÂ7W7FöÖW'2¢6VÆbæ76W'D–â‡6V6öæBÂ7W7FöÖW'2¢6VÆbæ76W'Dæ÷D–â†–æ7F—fRÂ7W7FöÖW'2 ¢FVbFW7EöÆ÷–ÅöVF–Væ6U÷W6W5ö7W7FöÖW%öæÇ—F–72‡6VÆb“ ¢Æ÷–ÂÒ6VÆbæ7W7FöÖW"‚$Æ÷–Â"¢6VÆbæ÷&FW"†Æ÷–Â¢6VÆbæ÷&FW"†Æ÷–ÂÂF—5övóÓ¢6VÆbæ&÷E÷W6W"†Æ÷–Â ¢7W7FöÖW'2ÒÆ—7B†vWEö7W7FöÖW'5öf÷%öVF–Væ6R„'&öF67DÖW76vRäVF–Væ6UG—RäÄõ”ÂÂ7F÷&S×6VÆbç7F÷&R’ ¢6VÆbæ76W'D–â†Æ÷–ÂÂ7W7FöÖW'2 ¢FVbFW7E÷F÷ö'W–W%öVF–Væ6U÷W6W5ö7W7FöÖW%öæÇ—F–72‡6VÆb“ ¢v†ÆRÒ6VÆbæ7W7FöÖW"‚%v†ÆR"¢6ÖÆÂÒ6VÆbæ7W7FöÖW"‚%6ÖÆÂ"¢6VÆbæ÷&FW"‡v†ÆRÂÖ÷VçCÓ¢6VÆbæ÷&FW"‡6ÖÆÂÂÖ÷VçCÓ ¢7W7FöÖW'2ÒÆ—7B†vWEö7W7FöÖW'5öf÷%öVF–Væ6R„'&öF67DÖW76vRäVF–Væ6UG—RåDõô%U”U"Â7F÷&S×6VÆbç7F÷&R’ ¢6VÆbæ76W'DWVÂ†7W7FöÖW'2Â·v†ÆUÒ ¢FVbFW7Eö–æ7F—fUöVF–Væ6U÷W6W5ö7W7FöÖW%öæÇ—F–72‡6VÆb“ ¢–æ7F—fRÒ6VÆbæ7W7FöÖW"‚$–æ7F—fR"¢&V6VçBÒ6VÆbæ7W7FöÖW"‚%&V6VçB"¢6VÆbæ÷&FW"†–æ7F—fRÂF—5övóÓCR¢6VÆbæ÷&FW"‡&V6VçBÂF—5övóÓR ¢7W7FöÖW'2ÒÆ—7B†vWEö7W7FöÖW'5öf÷%öVF–Væ6R„'&öF67DÖW76vRäVF–Væ6UG—Rä”ä5D•dRÂ7F÷&S×6VÆbç7F÷&R’ ¢6VÆbæ76W'D–â†–æ7F—fRÂ7W7FöÖW'2¢6VÆbæ76W'Dæ÷D–â‡&V6VçBÂ7W7FöÖW'2 ¢FVbFW7E÷&V6—–VçE÷Væ—VVæW75ö—5ö–FV×÷FVçB‡6VÆb“ ¢7W7FöÖW"Ò6VÆbæ7W7FöÖW"‚$Æ–6R"¢6VÆbæ&÷E÷W6W"†7W7FöÖW"¢6×–vâÒ6VÆbæ6×–vâ‚ ¢7&VFUö6×–vå÷&V6—–VçG2†6×–vâ¢7&VFUö6×–vå÷&V6—–VçG2†6×–vâ ¢6VÆbæ76W'DWVÂ„'&öF67E&V6—–VçBæö&¦V7G2æf–ÇFW"†6×–vãÖ6×–vâÂ7W7FöÖW#Ö7W7FöÖW"’æ6÷VçB‚’Â ¢FVbFW7E÷W6W'5÷v—F†÷WEö&÷E÷F&vWEö&U÷6¶—VB‡6VÆb“ ¢7W7FöÖW"Ò6VÆbæ7W7FöÖW"‚$æò&÷B"¢6VÆbæ÷&FW"†7W7FöÖW"¢6×–vâÒ6VÆbæ6×–vâ†VF–Væ6U÷G—SÔ'&öF67DÖW76vRäVF–Væ6UG—Rä5D•dUô5U5DôÔU%2 ¢7&VFUö6×–vå÷&V6—–VçG2†6×–vâ ¢&V6—–VçBÒ'&öF67E&V6—–VçBæö&¦V7G2ævWB†6×–vãÖ6×–vâÂ7W7FöÖW#Ö7W7FöÖW"¢6VÆbæ76W'DWVÂ‡&V6—–VçBç7FGW2Â'&öF67E&V6—–VçBå7FGW2å4´•TB¢6VÆbæ76W'D–â‚$æò7F—fR&÷BW6W""Â&V6—–VçBæW'&÷%öÖW76vR ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷6VæEö6×–vå÷6VæG5öV6…÷VæF–æu÷&V6—–VçB‡6VÆbÂ÷7EöÖö6²“ ¢f—'7BÒ6VÆbæ7W7FöÖW"‚$f—'7B"¢6V6öæBÒ6VÆbæ7W7FöÖW"‚%6V6öæB"¢6VÆbæ÷&FW"†f—'7B¢6VÆbæ÷&FW"‡6V6öæB¢6VÆbæ&÷E÷W6W"†f—'7BÂ6†Eö–CÒ##"¢6VÆbæ&÷E÷W6W"‡6V6öæBÂ6†Eö–CÒ##""¢6×–vâÒ6VÆbæ6×–vâ†VF–Væ6U÷G—SÔ'&öF67DÖW76vRäVF–Væ6UG—Rä5D•dUô5U5DôÔU%2 ¢6÷VçG2Ò6VæEö6×–vâ†6×–vâ ¢6×–vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†6×–vâç7FGW2Â'&öF67DÖW76vRå7FGW2å4TåB¢6VÆbæ76W'DWVÂ†6÷VçG5²'7V66W72%ÒÂ"¢6VÆbæ76W'DWVÂ†6×–vâç7V66W75ö6÷VçBÂ"¢6VÆbæ76W'DWVÂ„'&öF67E&V6—–VçBæö&¦V7G2æf–ÇFW"‡7FGW3Ô'&öF67E&V6—–VçBå7FGW2å4TåB’æ6÷VçB‚’Â"¢6VçEö6†Eö–G2Ò°¢6ÆÂæ·v&w5²&§6öâ%Õ²&6†Eö–B%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚'FW‡B"’ÓÒ-‹=˜MŠ}˜R˜]‹MŠ­‹¸Â ¢Ð¢6VÆbæ76W'DWVÂ‡6VçEö6†Eö–G2Â²##"Â##"'Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöæöåöFÖ–åö6ææ÷E÷7F'Eö'&öF67Eög&öÕö&÷B‡6VÆbÂ÷7EöÖö6²“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚-Š}‹‹=Š}˜B›í¸ÍŠ}˜R	ù:2"ÂW6W%ö–CÓC"ÂW6W&æÖSÒ&Æ–6R"Âf—'7EöæÖSÒ$Æ–6R"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R„'&öF67DÖW76vRæö&¦V7G2æW†—7G2‚’¢6VçE÷FW‡G2Ò°¢6ÆÂæ·v&w5²&§6öâ%ÒævWB‚'FW‡B"Â""¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢Ð¢6VÆbæ76W'DfÇ6R†ç’‚-ªý‹˜˜r˜]ŠíŠ}‹}ŠŠ}˜b‹ŠrŠ}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö&÷E÷&Wf–Wu÷6†÷w5÷&W6öÇfVE÷&V6—–VçEö6÷VçB‡6VÆbÂ÷7EöÖö6²“ ¢7W7FöÖW"Ò6VÆbæ7W7FöÖW"‚$7F—fR7W7FöÖW""¢6VÆbæ÷&FW"†7W7FöÖW"¢6VÆbæ&÷E÷W6W"†7W7FöÖW"Â6†Eö–CÒ#3" ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚&FÖ–ã¦&3¦VC¦7F—fUö7W7FöÖW'2"’¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚-‹=˜MŠ}˜R˜]‹MŠ­‹¸Â"ÂÖW76vUö–CÓ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#““’"¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%$ôD45Eô4ôäd•$Ò¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-›í¸Í‹N(Í˜m˜]Š}¸Í‹BŠ}‹‹=Š}˜B›í¸ÍŠ}˜R"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-»˜]ŠíŠ}‹}Š‚›í¸ÍŠýŠr‹MŠò"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-˜-Š}Š˜BŠ}‹‹=Š}˜C¢»"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eö&÷Eöf–æÅ÷6VæEö7&VFW5ö6×–våöæE÷&W÷'G5ö6÷VçG2‡6VÆbÂ÷7EöÖö6²“ ¢7W7FöÖW"Ò6VÆbæ7W7FöÖW"‚$7F—fR7W7FöÖW""¢6VÆbæ÷&FW"†7W7FöÖW"¢6VÆbæ&÷E÷W6W"†7W7FöÖW"Â6†Eö–CÒ#C"¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ¢¦·v&w7Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&ÖW76vUö–B#¢×Ò ¢÷7EöÖö6²ç6–FUöVffV7BÒ÷7E÷6–FUöVffV7@ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚&FÖ–ã¦&3¦VC¦7F—fUö7W7FöÖW'2"’¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚-‹=˜MŠ}˜R˜]‹MŠ­‹¸Â"ÂÖW76vUö–CÓ"’¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚&FÖ–ã¦&3§6VæB"Â6ÆÆ&6µö–CÒ&&2×6VæB"ÂÖW76vUö–CÓ#’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6×–vâÒ'&öF67DÖW76vRæö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†6×–vâç7FGW2Â'&öF67DÖW76vRå7FGW2å4TåB¢6VÆbæ76W'DWVÂ†6×–vâç7V66W75ö6÷VçBÂ¢6VÆbæ76W'DWVÂ†6×–vâç&V6—–VçG2ævWB‚’ç7FGW2Â'&öF67E&V6—–VçBå7FGW2å4TåB¢6VÆbæ76W'EG'VR€¢ç’€¢6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"¢æB6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C ¢æB6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚'FW‡B"’ÓÒ-‹=˜MŠ}˜R˜]‹MŠ­‹¸Â ¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢¢¢6VÆbæ76W'EG'VR€¢ç’€¢6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"¢æB6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#““’ ¢æB-˜]˜˜˜#¢»"–â6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚'FW‡B"Â""¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢¢  ¦6Æ726×–väFÖ–åU…FW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ$FÖ–âU‚7F÷&R"À¢VævÆ—6…öæÖSÒ$FÖ–âU‚7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$FÖ–âU‚7F÷&R"À¢&æµöæÖSÒ%FW7B&æ²"À¢'&öF67E÷&FUöÆ–Ö—E÷W%÷6V6öæCÓÀ¢'&öF67EöÖ…÷&V6—–VçG5÷W%ö6×–vãÓÀ¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ$FÖ–âU‚Æâ"À¢6ÇVsÒ&FÖ–â×W‚×Æâ"À¢föÇVÖUöv#ÔFV6–ÖÂ‚#Rã"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5ö7F—fSÕG'VRÀ¢—5÷V&Æ–3ÕG'VRÀ¢¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&ÒFÖ–âU‚"À¢&÷E÷Fö¶VãÒ'FVÆVw&Ò×Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢—5ö7F—fSÕG'VRÀ¢¢6VÆbçW6W"ÒvWE÷W6W%öÖöFVÂ‚’æö&¦V7G2æ7&VFU÷7WW'W6W"€¢W6W&æÖSÒ&6×–vâÖFÖ–â"À¢VÖ–ÃÒ&6×–vâÖFÖ–äW†×ÆRæ6öÒ"À¢77v÷&CÒ'72"À¢¢6VÆbæ6Æ–VçBæf÷&6UöÆöv–â‡6VÆbçW6W" ¢FVb7W7FöÖW"‡6VÆbÂæÖRÂ¢ÂW6W&æÖSÔæöæRÂ†öæSÒ""“ ¢&WGW&â7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÖæÖRÂW6W&æÖS×W6W&æÖR÷"""Â†öæUöçVÖ&W#×†öæR ¢FVb÷&FW"‡6VÆbÂ7W7FöÖW"“ ¢&WGW&â÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢Ö÷VçCÓÀ¢÷&–v–æÅöÖ÷VçCÓÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢fW&–f–VEöC×F–ÖW¦öæRææ÷r‚’À¢ ¢FVb&÷E÷W6W"‡6VÆbÂ7W7FöÖW"Â¢Â6†Eö–B“ ¢&WGW&â&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–C×7G"†6†Eö–B’À¢6†Eö–C×7G"†6†Eö–B’À¢W6W&æÖSÖb&FÖ–çW…÷¶6†Eö–GÒ"À¢F—7Æ•öæÖSÖb$FÖ–âU‚¶6†Eö–GÒ"À¢ ¢FVb6×–vâ‡6VÆbÂ¢Â7FGW3Ô'&öF67DÖW76vRå7FGW2äE$eBÂVF–Væ6U÷G—SÔ'&öF67DÖW76vRäVF–Væ6UG—Rä5D•dUô5U5DôÔU%2“ ¢&WGW&â'&öF67DÖW76vRæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢F—FÆSÒ$FÖ–â6×–vâ"À¢ÖW76vU÷FW‡CÒ-‹=˜MŠ}˜R˜]‹MŠ­‹¸Â"À¢VF–Væ6U÷G—SÖVF–Væ6U÷G—RÀ¢6†ææVÃÔ'&öF67DÖW76vRä6†ææVÂåDTÄTu$ÒÀ¢7FGW3×7FGW2À¢ ¢FVbFW7E÷v÷&¶&Væ6…÷&WV—&W5öFÖ–åöæEö†æFÆW5öV×G•öF"‡6VÆb“ ¢&W7öç6RÒ6VÆbæ6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö6×–vå÷v÷&¶&Væ6‚"’¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ-ª˜]›í¸Í˜n(Í˜}Šr˜‚›í¸ÍŠ}˜^(Í‹‹=Š}˜m¸Â" ¢6VÆbæ6Æ–VçBæÆöv÷WB‚¢&W7öç6RÒ6VÆbæ6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö6×–vå÷v÷&¶&Væ6‚"’¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3" ¢FVbFW7E÷v—¦&Eö7&VFW5öG&gE÷v—F†÷WE÷&V6—–VçG2‡6VÆb“ ¢&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö6×–våöæWr"’À¢°¢'7F÷&R#¢6VÆbç7F÷&Rç²À¢'F—FÆR#¢-ª˜]›í¸Í˜bŠ­‹=Š¢"À¢&ÖW76vU÷FW‡B#¢-‹=˜MŠ}˜]ˆÂŠ­‹=Š¢ª˜]›í¸Í˜b"À¢'66†VGVÆVEöB#¢""À¢&FÖ–åöæ÷FR#¢&æ÷FR"À¢ÒÀ¢ ¢6×–vâÒ'&öF67DÖW76vRæö&¦V7G2ævWB‚¢6VÆbæ76W'E&VF—&V7G2‡&W7öç6RÂ&WfW'6R‚&FÖ–å÷7F÷&Uö6×–våöVF–Væ6R"Â&w3Õ¶6×–vâçµÒ’¢6VÆbæ76W'DWVÂ†6×–vâç7FGW2Â'&öF67DÖW76vRå7FGW2äE$eB¢6VÆbæ76W'DWVÂ†6×–vâç&V6—–VçG2æ6÷VçB‚’Â ¢FVbFW7EöÖW76vU÷fÆ–FF–öå÷&V¦V7G5öV×G•öæE÷6Vç6—F—fUö6öçFVçB‡6VÆb“ ¢&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö6×–våöæWr"’À¢°¢'7F÷&R#¢6VÆbç7F÷&Rç²À¢'F—FÆR#¢$&B6×–vâ"À¢&ÖW76vU÷FW‡B#¢""À¢'66†VGVÆVEöB#¢""À¢ÒÀ¢¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R„'&öF67DÖW76vRæö&¦V7G2æW†—7G2‚’ ¢&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö6×–våöæWr"’À¢°¢'7F÷&R#¢6VÆbç7F÷&Rç²À¢'F—FÆR#¢$&B6×–vâ"À¢&ÖW76vU÷FW‡B#¢'Fö¶Vâ"²##3CSc¢"²&&6FVfv†–¦¶ÆÖæ÷'7GWgw‡—¤$4DR"À¢'66†VGVÆVEöB#¢""À¢ÒÀ¢¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R„'&öF67DÖW76vRæö&¦V7G2æW†—7G2‚’ ¢F6‚‚'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷&Wf–Wuö6÷VçG5öGWÆ–6FW5öÖ—76–æu÷F&vWG5öæEö†5öæõ÷6–FUöVffV7G2‡6VÆbÂ÷7EöÖö6²“ ¢f—'7BÒ6VÆbæ7W7FöÖW"‚$f—'7B"¢6V6öæBÒ6VÆbæ7W7FöÖW"‚%6V6öæB"¢6VÆbæ÷&FW"†f—'7B¢6VÆbæ÷&FW"‡6V6öæB¢6VÆbæ&÷E÷W6W"†f—'7BÂ6†Eö–CÒ#s"¢6VÆbæ&÷E÷W6W"†f—'7BÂ6†Eö–CÒ#s""¢6×–vâÒ6VÆbæ6×–vâ‚ ¢&W7öç6RÒ6VÆbæ6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö6×–vå÷&Wf–Wr"Â&w3Õ¶6×–vâçµÒ’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ$GWÆ–6FW2&VÖ÷fVB"¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ$Ö—76–ærF&vWB"¢6VÆbæ76W'DWVÂ„'&öF67E&V6—–VçBæö&¦V7G2æ6÷VçB‚’Â¢÷7EöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf—&Õ÷&WV—&W5÷‡&6UöæEööæÇ•÷VWVW5÷v—F†÷WE÷6VæF–ær‡6VÆbÂ÷7EöÖö6²“ ¢7W7FöÖW"Ò6VÆbæ7W7FöÖW"‚%F&vWB"¢6VÆbæ÷&FW"†7W7FöÖW"¢6VÆbæ&÷E÷W6W"†7W7FöÖW"Â6†Eö–CÒ#s"¢6×–vâÒ6VÆbæ6×–vâ‚¢W&ÂÒ&WfW'6R‚&FÖ–å÷7F÷&Uö6×–våö6öæf—&Ò"Â&w3Õ¶6×–vâçµÒ ¢vWE÷&W7öç6RÒ6VÆbæ6Æ–VçBævWB‡W&Â¢6VÆbæ76W'DWVÂ†vWE÷&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DWVÂ„'&öF67E&V6—–VçBæö&¦V7G2æ6÷VçB‚’Â ¢&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B‡W&ÂÂ²&6öæf—&ÖF–öâ#¢%u$ôär'Ò¢6VÆbæ76W'E&VF—&V7G2‡&W7öç6RÂW&Â¢6×–vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†6×–vâç7FGW2Â'&öF67DÖW76vRå7FGW2äE$eB¢6VÆbæ76W'DWVÂ„'&öF67E&V6—–VçBæö&¦V7G2æ6÷VçB‚’Â ¢&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B‡W&ÂÂ²&6öæf—&ÖF–öâ#¢b%4TäEô4Õ”tå÷¶6×–vâç·Ò'Ò¢6VÆbæ76W'E&VF—&V7G2‡&W7öç6RÂ&WfW'6R‚&FÖ–å÷7F÷&Uö6×–vå÷&Wf–Wr"Â&w3Õ¶6×–vâçµÒ’¢6×–vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†6×–vâç7FGW2Â'&öF67DÖW76vRå7FGW2åTUTTB¢6VÆbæ76W'DWVÂ†6×–vâç&V6—–VçG2æf–ÇFW"‡7FGW3Ô'&öF67E&V6—–VçBå7FGW2åTäD”är’æ6÷VçB‚’Â¢÷7EöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷VWVU÷&ö6W76÷%öFöW5öæ÷E÷&W6VæE÷6VçE÷&V6—–VçB‡6VÆbÂ÷7EöÖö6²“ ¢f—'7BÒ6VÆbæ7W7FöÖW"‚$f—'7B"¢6V6öæBÒ6VÆbæ7W7FöÖW"‚%6V6öæB"¢6VÆbæ÷&FW"†f—'7B¢6VÆbæ÷&FW"‡6V6öæB¢6VÆbæ&÷E÷W6W"†f—'7BÂ6†Eö–CÒ#s#"¢6VÆbæ&÷E÷W6W"‡6V6öæBÂ6†Eö–CÒ#s#""¢6×–vâÒ6VÆbæ6×–vâ‡7FGW3Ô'&öF67DÖW76vRå7FGW2åTUTTB¢7&VFUö6×–vå÷&V6—–VçG2†6×–vâ¢6VçBÒ6×–vâç&V6—–VçG2ævWB†7W7FöÖW#Öf—'7B¢6VçBç7FGW2Ò'&öF67E&V6—–VçBå7FGW2å4Tå@¢6VçBç6VçEöBÒF–ÖW¦öæRææ÷r‚¢6VçBç6fR‡WFFUöf–VÆG3Õ²'7FGW2"Â'6VçEöB"Â'WFFVEöB%Ò ¢6ÆÅö6öÖÖæB‚'&ö6W75ö'&öF67E÷VWVR"Â"ÒÖ6×–vâÖ–B"Â7G"†6×–vâç²’Â"ÒÖ&F6‚×6—¦R"Â#" ¢6VçBç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡6VçBç7FGW2Â'&öF67E&V6—–VçBå7FGW2å4TåB¢6VçEö6†Eö–G2Ò¶6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7EÐ¢6VÆbæ76W'DWVÂ‡6VçEö6†Eö–G2Â²#s#"%Ò ¢FVbFW7E÷&WG'•ö7F–öå÷6¶—5ö&Æö6¶VEöæE÷&WVWVW5÷F–ÖV÷WB‡6VÆb“ ¢7W7FöÖW"Ò6VÆbæ7W7FöÖW"‚%&WG'’"¢6×–vâÒ6VÆbæ6×–vâ‡7FGW3Ô'&öF67DÖW76vRå7FGW2å4TåB¢&Æö6¶VBÒ'&öF67E&V6—–VçBæö&¦V7G2æ7&VFR€¢6×–vãÖ6×–vâÀ¢7W7FöÖW#Ö7W7FöÖW"À¢6†ææVÃÔ'&öF67DÖW76vRä6†ææVÂåDTÄTu$ÒÀ¢F&vWEö–FVçF–f–W#Ò#s3"À¢7FGW3Ô'&öF67E&V6—–VçBå7FGW2äd”ÄTBÀ¢W'&÷%öÖW76vSÒ$f÷&&–FFVã¢&÷Bv2&Æö6¶VB'’F†RW6W""À¢¢F–ÖV÷WBÒ'&öF67E&V6—–VçBæö&¦V7G2æ7&VFR€¢6×–vãÖ6×–vâÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"‚%F–ÖV÷WB"’À¢6†ææVÃÔ'&öF67DÖW76vRä6†ææVÂåDTÄTu$ÒÀ¢F&vWEö–FVçF–f–W#Ò#s3""À¢7FGW3Ô'&öF67E&V6—–VçBå7FGW2äd”ÄTBÀ¢W'&÷%öÖW76vSÒ'F–ÖV÷WB"À¢ ¢&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö6×–vå÷&Wf–Wr"Â&w3Õ¶6×–vâçµÒ’À¢²&7F–öâ#¢'&WG'’"Â&6öæf—&ÖF–öâ#¢b%$UE%•ô4Õ”tå÷¶6×–vâç·Ò'ÒÀ¢ ¢6VÆbæ76W'E&VF—&V7G2‡&W7öç6RÂ&WfW'6R‚&FÖ–å÷7F÷&Uö6×–vå÷&Wf–Wr"Â&w3Õ¶6×–vâçµÒ’¢&Æö6¶VBç&Vg&W6…ög&öÕöF"‚¢F–ÖV÷WBç&Vg&W6…ög&öÕöF"‚¢6×–vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&Æö6¶VBç7FGW2Â'&öF67E&V6—–VçBå7FGW2äd”ÄTB¢6VÆbæ76W'DWVÂ‡F–ÖV÷WBç7FGW2Â'&öF67E&V6—–VçBå7FGW2åTäD”är¢6VÆbæ76W'DWVÂ†6×–vâç7FGW2Â'&öF67DÖW76vRå7FGW2åTUTTB ¢FVbFW7E÷6fUö77eöW‡÷'EöFöW5öæ÷EöÆVµ÷F&vWEö÷%÷–’‡6VÆb“ ¢7W7FöÖW"Ò6VÆbæ7W7FöÖW"‚%”’"ÂW6W&æÖSÒ'W'6öäW†×ÆRæ6öÒ"Â†öæSÒ"³“ƒ“##3CScr"¢6×–vâÒ6VÆbæ6×–vâ‡7FGW3Ô'&öF67DÖW76vRå7FGW2å4TåB¢'&öF67E&V6—–VçBæö&¦V7G2æ7&VFR€¢6×–vãÖ6×–vâÀ¢7W7FöÖW#Ö7W7FöÖW"À¢6†ææVÃÔ'&öF67DÖW76vRä6†ææVÂåDTÄTu$ÒÀ¢F&vWEö–FVçF–f–W#Ò#“ƒscSC3#"À¢7FGW3Ô'&öF67E&V6—–VçBå7FGW2äd”ÄTBÀ¢W'&÷%öÖW76vSÒ'F–ÖV÷WBv—F‚‡GG3¢òöW†×ÆRæ6öÒ÷7V"÷6V7&WB"À¢ ¢&W7öç6RÒ6VÆbæ6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö6×–våöW‡÷'B"Â&w3Õ¶6×–vâçµÒ’¢&öG’Ò&W7öç6Ræ6öçFVçBæFV6öFR‚'WFbÓ‚" ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D–â‚'&V6—–VçEö–B"Â&öG’¢6VÆbæ76W'Dæ÷D–â‚#“ƒscSC3#"Â&öG’¢6VÆbæ76W'Dæ÷D–â‚"³“ƒ“##3CScr"Â&öG’¢6VÆbæ76W'Dæ÷D–â‚'W'6öäW†×ÆRæ6öÒ"Â&öG’¢6VÆbæ76W'Dæ÷D–â‚'7V"÷6V7&WB"Â&öG’  ¦6Æ727W÷'D6†EFW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ%eâ7F÷&R"À¢VævÆ—6…öæÖSÒ%eâ7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò%eâ7F÷&R"À¢¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò7W÷'B"À¢&÷E÷Fö¶VãÒ'FVÆVw&Ò×Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢—5ö7F—fSÕG'VRÀ¢¢6VÆbçvV&†ööµ÷W&ÂÒ&WfW'6R‚&&÷E÷vV&†öö²"Â&w3Õ·6VÆbæ&÷Eö6öæf–rç&÷f–FW"Â6VÆbæ&÷Eö6öæf–rçvV&†ööµ÷6V7&WEÒ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö7W7FöÖW%öÖW76vUö7&VFW5öG–æÖ–5÷7W÷'Eö6öçfW'6F–öâ‡6VÆbÂ÷7EöÖö6²“ ¢&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B€¢&WfW'6R‚'7W÷'E÷6VæEöÖW76vR"’À¢FF×²&6öçF7E÷fÇVR#¢$Æ–6R"Â&&öG’#¢-‹=˜MŠ}˜]ˆÂªŠ}˜m˜¸Íªý˜R˜‹]˜B˜m˜]¸Î(Í‹M˜Šòâ'ÒÀ¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ&W7öç6Ræ§6öâ‚¢6VÆbæ76W'EG'VR‡–ÆöE²&ö²%Ò¢6öçfW'6F–öâÒ7W÷'D6öçfW'6F–öâæö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†6öçfW'6F–öâæ6öçF7E÷fÇVRÂ$Æ–6R"¢6VÆbæ76W'DWVÂ†6öçfW'6F–öâç7FGW2Â7W÷'D6öçfW'6F–öâå7FGW2åt•D”äuôDÔ”â ¢7W÷'EöÖW76vRÒ7W÷'DÖW76vRæö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ‡7W÷'EöÖW76vRç6VæFW%÷G—RÂ7W÷'DÖW76vRå6VæFW%G—Rä5U5DôÔU"¢6VÆbæ76W'DWVÂ‡7W÷'EöÖW76vRæ&öG’Â-‹=˜MŠ}˜]ˆÂªŠ}˜m˜¸Íªý˜R˜‹]˜B˜m˜]¸Î(Í‹M˜Šòâ" ¢6VæE÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-›í¸ÍŠ}˜RŠÍŠý¸ÍŠò›í‹MŠ­¸ÍŠŠ}˜m¸Â"Â6VæE÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚$Æ–6R"Â6VæE÷–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â6VæE÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b'7W÷'C§&WÇ“§¶6öçfW'6F–öâç·Ò"Â6ÆÆ&6µ÷fÇVW2 ¢ÖW76vW5÷&W7öç6RÒ6VÆbæ6Æ–VçBævWB‡&WfW'6R‚'7W÷'EöÖW76vW2"’¢6VÆbæ76W'DWVÂ†ÖW76vW5÷&W7öç6Rç7FGW5ö6öFRÂ#¢ÖW76vW5÷–ÆöBÒÖW76vW5÷&W7öç6Ræ§6öâ‚¢6VÆbæ76W'DWVÂ†ÖW76vW5÷–ÆöE²&6öçfW'6F–öâ%Õ²&–B%ÒÂ6öçfW'6F–öâç²¢6VÆbæ76W'DWVÂ†ÖW76vW5÷–ÆöE²&ÖW76vW2%Õ³Õ²&&öG’%ÒÂ7W÷'EöÖW76vRæ&öG’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöFÖ–å÷&WÇ•ög&öÕö&÷Eö—5÷6fVEöf÷%÷7W÷'E÷vR‡6VÆbÂ÷÷7EöÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢6öçfW'6F–öâÒ7W÷'D6öçfW'6F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢6öçF7E÷fÇVSÒ$Æ–6R"À¢7FGW3Õ7W÷'D6öçfW'6F–öâå7FGW2åt•D”äuôDÔ”âÀ¢¢7W÷'DÖW76vRæö&¦V7G2æ7&VFR€¢6öçfW'6F–öãÖ6öçfW'6F–öâÀ¢6VæFW%÷G—SÕ7W÷'DÖW76vRå6VæFW%G—Rä5U5DôÔU"À¢7W7FöÖW#Ö7W7FöÖW"À¢&öG“Ò-‹=˜MŠ}˜R"À¢ ¢6ÆÆ&6µ÷&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B€¢6VÆbçvV&†ööµ÷W&ÂÀ¢FFÖ§6öâæGV×2€¢°¢&6ÆÆ&6µ÷VW'’#¢°¢&–B#¢'7W÷'BÖ6""À¢&g&öÒ#¢²&–B#¢““’Â'W6W&æÖR#¢&FÖ–â'ÒÀ¢&ÖW76vR#¢²&ÖW76vUö–B#¢Â&6†B#¢²&–B#¢““’Â'G—R#¢'&—fFR'×ÒÀ¢&FF#¢b'7W÷'C§&WÆ“§¶6öçfW'6F–öâç·Ò"À¢Ð¢Ð¢’À¢6öçFVçE÷G—SÒ&Æ–6F–öâö§6öâ"À¢¢6VÆbæ76W'DWVÂ†6ÆÆ&6µ÷&W7öç6Rç7FGW5ö6öFRÂ#¢VæF–ærÒ&÷EVæF–æt7F–öâæö&¦V7G2ævWB‡7W÷'Eö6öçfW'6F–öãÖ6öçfW'6F–öâ¢6VÆbæ76W'DWVÂ‡VæF–æræ7F–öâÂ&÷EVæF–æt7F–öâä7F–öâå5Uõ%Eõ$UÅ’¢6VÆbæ76W'DWVÂ‡VæF–ærç7FGW2Â&÷EVæF–æt7F–öâå7FGW2åTäD”är ¢&WÇ•÷&W7öç6RÒ6VÆbæ6Æ–VçBç÷7B€¢6VÆbçvV&†ööµ÷W&ÂÀ¢FFÖ§6öâæGV×2€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢À¢&g&öÒ#¢²&–B#¢““’Â'W6W&æÖR#¢&FÖ–â'ÒÀ¢&6†B#¢²&–B#¢““’Â'G—R#¢'&—fFR'ÒÀ¢'FW‡B#¢-˜M‹}˜Šr¸Íª’ŠŠ}‹˜M¸Í˜mª’‹ŠrŠ‹˜‹-‹‹=Š}˜m¸Âª˜bâ"À¢Ð¢Ð¢’À¢6öçFVçE÷G—SÒ&Æ–6F–öâö§6öâ"À¢ ¢6VÆbæ76W'DWVÂ‡&WÇ•÷&W7öç6Rç7FGW5ö6öFRÂ#¢6öçfW'6F–öâç&Vg&W6…ög&öÕöF"‚¢VæF–ærç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†6öçfW'6F–öâç7FGW2Â7W÷'D6öçfW'6F–öâå7FGW2äå5tU$TB¢6VÆbæ76W'DWVÂ‡VæF–ærç7FGW2Â&÷EVæF–æt7F–öâå7FGW2ä4ôÕÄUDTB¢FÖ–åöÖW76vRÒ7W÷'DÖW76vRæö&¦V7G2ævWB‡6VæFW%÷G—SÕ7W÷'DÖW76vRå6VæFW%G—RäDÔ”â¢6VÆbæ76W'DWVÂ†FÖ–åöÖW76vRæ&öG’Â-˜M‹}˜Šr¸Íª’ŠŠ}‹˜M¸Í˜mª’‹ŠrŠ‹˜‹-‹‹=Š}˜m¸Âª˜bâ"  ¦6Æ72FVÆVw&ÕW&6†6TfÆ÷uFW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢6VÆbæÖVF–÷F×ÒFV×f–ÆRåFV×÷&'”F—&V7F÷'’‚¢6VÆbç6WGF–æw5ö÷fW'&–FRÒ÷fW'&–FU÷6WGF–æw2„ÔTD”õ$ôõC×6VÆbæÖVF–÷F×ææÖR¢6VÆbç6WGF–æw5ö÷fW'&–FRæVæ&ÆR‚¢6VÆbæFD6ÆVçW‡6VÆbç6WGF–æw5ö÷fW'&–FRæF—6&ÆR¢6VÆbæFD6ÆVçW‡6VÆbæÖVF–÷F×æ6ÆVçW ¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ%eâ7F÷&R"À¢VævÆ—6…öæÖSÒ%eâ7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò%eâ7F÷&R"À¢&æµöæÖSÒ%FW7B&æ²"À¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ#t""À¢6ÇVsÒ#v""À¢föÇVÖUöv#Ò#ã"À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5ö7F—fSÕG'VRÀ¢—5÷V&Æ–3ÕG'VRÀ¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢—5ö7F—fSÕG'VRÀ¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢6W'fW%ö—Ò##rããã"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'G—S×F7g6V7W&—G“ÖæöæR"À¢—5ö7F—fSÕG'VRÀ¢¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò6ÆW2"À¢&÷E÷Fö¶VãÒ'FVÆVw&Ò×Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢—5ö7F—fSÕG'VRÀ¢¢6VÆbçW&ÂÒ&WfW'6R‚&&÷E÷vV&†öö²"Â&w3Õ·6VÆbæ&÷Eö6öæf–rç&÷f–FW"Â6VÆbæ&÷Eö6öæf–rçvV&†ööµ÷6V7&WEÒ¢66†Ræ6ÆV"‚ ¢FVb÷7E÷WFFR‡6VÆbÂ–ÆöB“ ¢&WGW&â6VÆbæ6Æ–VçBç÷7B€¢6VÆbçW&ÂÀ¢FFÖ§6öâæGV×2‡–ÆöB’À¢6öçFVçE÷G—SÒ&Æ–6F–öâö§6öâ"À¢ ¢FVbÖW76vR‡6VÆbÂFW‡BÂ¢ÂÖW76vUö–CÓÂW6W%ö–CÓC"ÂW6W&æÖSÒ&Æ–6R"Âf—'7EöæÖSÒ$Æ–6R"“ ¢&WGW&â°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢ÖW76vUö–BÀ¢&g&öÒ#¢²&–B#¢W6W%ö–BÂ'W6W&æÖR#¢W6W&æÖRÂ&f—'7EöæÖR#¢f—'7EöæÖWÒÀ¢&6†B#¢²&–B#¢W6W%ö–BÂ'G—R#¢'&—fFR'ÒÀ¢'FW‡B#¢FW‡BÀ¢Ð¢Ð ¢FVb6öçF7EöÖW76vR‡6VÆbÂ†öæUöçVÖ&W"Â¢ÂÖW76vUö–CÓ"ÂW6W%ö–CÓC"ÂW6W&æÖSÒ&Æ–6R"Âf—'7EöæÖSÒ$Æ–6R"“ ¢&WGW&â°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢ÖW76vUö–BÀ¢&g&öÒ#¢²&–B#¢W6W%ö–BÂ'W6W&æÖR#¢W6W&æÖRÂ&f—'7EöæÖR#¢f—'7EöæÖWÒÀ¢&6†B#¢²&–B#¢W6W%ö–BÂ'G—R#¢'&—fFR'ÒÀ¢&6öçF7B#¢°¢'†öæUöçVÖ&W"#¢†öæUöçVÖ&W"À¢'W6W%ö–B#¢W6W%ö–BÀ¢&f—'7EöæÖR#¢f—'7EöæÖRÀ¢ÒÀ¢Ð¢Ð ¢FVb6ÆÆ&6²‡6VÆbÂFFÂ¢ÂÖW76vUö–CÓÂ6ÆÆ&6µö–CÒ&6""ÂW6W%ö–CÓC"ÂW6W&æÖSÒ&Æ–6R"Âf—'7EöæÖSÒ$Æ–6R"“ ¢&WGW&â°¢&6ÆÆ&6µ÷VW'’#¢°¢&–B#¢6ÆÆ&6µö–BÀ¢&g&öÒ#¢²&–B#¢W6W%ö–BÂ'W6W&æÖR#¢W6W&æÖRÂ&f—'7EöæÖR#¢f—'7EöæÖWÒÀ¢&ÖW76vR#¢²&ÖW76vUö–B#¢ÖW76vUö–BÂ&6†B#¢²&–B#¢W6W%ö–BÂ'G—R#¢'&—fFR'×ÒÀ¢&FF#¢FFÀ¢Ð¢Ð ¢FVbÖ¶Uö&÷E÷W6W"€¢6VÆbÀ¢¢À¢W6W%ö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢7W7FöÖW#ÔæöæRÀ¢7FFSÔ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•BÀ¢7FFUöFFÔæöæRÀ¢“ ¢7W7FöÖW"Ò7W7FöÖW"÷"7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÖF—7Æ•öæÖRÂW6W&æÖS×W6W&æÖR¢&WGW&â&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–C×7G"‡W6W%ö–B’À¢6†Eö–C×7G"‡W6W%ö–B’À¢W6W&æÖS×W6W&æÖRÀ¢F—7Æ•öæÖSÖF—7Æ•öæÖRÀ¢7FFS×7FFRÀ¢7FFUöFF×7FFUöFF÷"·ÒÀ¢ ¢FVb&V6V—Eöf–ÆUö–æfò‡6VÆbÂ¢Âf–ÆUö–CÒ'&V6V—BÖf–ÆR"ÂVæ—VUö–CÒ'&V6V—B"ÂÖW76vUö–CÓ#“ ¢&WGW&â°¢&¶–æB#¢'†÷Fò"À¢&f–ÆUö–B#¢f–ÆUö–BÀ¢&f–ÆU÷Væ—VUö–B#¢Væ—VUö–BÀ¢&f–ÆUöæÖR#¢'FVÆVw&Ò×&V6V—Bæ§r"À¢&ÖW76vUö–B#¢ÖW76vUö–BÀ¢Ð ¢FVb&÷E÷÷7E÷6–FUöVffV7B‡6VÆbÂ6ÆÇ3ÔæöæRÂ¢Âf–ÆU÷FƒÒ'†÷F÷2÷&V6V—Bæ§r"ÂÖW76vUö–E÷7F'CÓ“ ¢6ÆÇ2Ò6ÆÇ2–b6ÆÇ2—2æ÷BæöæRVÇ6RµÐ¢æW‡EöÖW76vUö–BÒ²'fÇVR#¢ÖW76vUö–E÷7F'GÐ ¢FVb6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂFFÔæöæRÂf–ÆW3ÔæöæRÂ¢¦·v&w2“ ¢6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ&FF#¢FFÂ&f–ÆW2#¢f–ÆW2Â¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢f–ÆU÷F‡×Ò¢–bW&ÂæVæG7v—F‚‚"÷6VæE†÷Fò"’÷"W&ÂæVæG7v—F‚‚"÷6VæDÖW76vR"“ ¢æW‡EöÖW76vUö–E²'fÇVR%Ò³Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&ÖW76vUö–B#¢æW‡EöÖW76vUö–E²'fÇVR%××Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢&WGW&â6–FUöVffV7@ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷6WGW÷&WV—&VE÷7F÷&Uö&Æö6·5öæöåöFÖ–åö&÷E÷7F'B‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒç6WGW÷&VF–æW72–×÷'B4UEUôäõEõ$TE•ôÔU54tP ¢6VÆbç7F÷&Rç6WGW÷7FGW2Ò7F÷&Rå6WGW7FGW2å4UEUõ$UT•$T@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'6WGW÷7FGW2%Ò ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'DWVÂ‡–ÆöE²&6†Eö–B%ÒÂ#C""¢6VÆbæ76W'DWVÂ‡–ÆöE²'FW‡B%ÒÂ4UEUôäõEõ$TE•ôÔU54tR¢6VÆbæ76W'DfÇ6R„&÷EW6W"æö&¦V7G2æW†—7G2‚’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷Æç5ö6öæf–wW&VE÷7F÷&Uö&Æö6·5öæöåöFÖ–åö&÷E÷7F'E÷v—F…öFV'Vu÷&V6öâ‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒç6WGW÷&VF–æW72–×÷'B4UEUôäõEõ$TE•ôÔU54tP ¢6VÆbç7F÷&Rç6WGW÷7FGW2Ò7F÷&Rå6WGW7FGW2åÄå5ô4ôäd”uU$T@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'6WGW÷7FGW2%Ò ¢v—F‚6VÆbæ76W'DÆöw2‚'7F÷&Ræ&÷G2"ÂÆWfVÃÒ%t$ä”är"’2Æöw3 ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'DWVÂ‡–ÆöE²'FW‡B%ÒÂ4UEUôäõEõ$TE•ôÔU54tR¢Æöuö÷WGWBÒ%Æâ"æ¦ö–â†Æöw2æ÷WGWB¢6VÆbæ76W'D–â‚'6WGW÷7FGW3×Æç5ö6öæf–wW&VB"ÂÆöuö÷WGWB¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷Eö6öæf–ræ&÷E÷Fö¶VâÂÆöuö÷WGWB¢6VÆbæ76W'DfÇ6R„&÷EW6W"æö&¦V7G2æW†—7G2‚’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷&VG•÷7F÷&U÷V&Æ–5÷Æå÷v—F…÷&VG•÷&÷WFUöV'5ö–åö&÷E÷ÆåöÆ—7B‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç7F÷&Rç6WGW÷7FGW2Ò7F÷&Rå6WGW7FGW2å$TE¢6VÆbç7F÷&Rç6ÆW5öÖöFRÒ7F÷&Rå6ÆW4ÖöFRåETääTÀ¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'6WGW÷7FGW2"Â'6ÆW5öÖöFR%Ò¢Æä–æ&÷VæE&÷WFRæö&¦V7G2æ7&VFR‡7F÷&S×6VÆbç7F÷&RÂÆã×6VÆbçÆâÂ–æ&÷VæC×6VÆbæ–æ&÷VæBÂ—5ö7F—fSÕG'VR ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢–b&6ÆÆ&6µöFF"–â'WGFöà¢Ð¢6VÆbæ76W'D–â†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"Â6ÆÆ&6µ÷fÇVW2¢6VÆbæ76W'Dæ÷D–â‚-Šý‹ŠÝŠ}˜BŠÝŠ}‹m‹›í˜M˜b˜‹Š}˜M¸ÂŠ‹Š}¸ÂŠí‹¸ÍŠò˜ŠÍ˜Šò˜mŠýŠ}‹Šòâ"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöV×G•ö&÷E÷ÆåöÆ—7F–æuöÆöw5÷6fUöFV'Vu÷&V6öâ‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbçÆâæ—5ö7F—fRÒfÇ6P¢6VÆbçÆâç6fR‡WFFUöf–VÆG3Õ²&—5ö7F—fR"Â'WFFVEöB%Ò ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢v—F‚6VÆbæ76W'DÆöw2‚'7F÷&RçFVÆVw&Õö&÷Bæ'W•öfÆ÷r"ÂÆWfVÃÒ%t$ä”är"’2Æöw3 ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Šý‹ŠÝŠ}˜BŠÝŠ}‹m‹›í˜M˜b˜‹Š}˜M¸ÂŠ‹Š}¸ÂŠí‹¸ÍŠò˜ŠÍ˜Šò˜mŠýŠ}‹Šòâ"Â–ÆöE²'FW‡B%Ò¢Æöuö÷WGWBÒ%Æâ"æ¦ö–â†Æöw2æ÷WGWB¢6VÆbæ76W'D–â‚&æõö7F—fU÷V&Æ–5÷Æç2"ÂÆöuö÷WGWB¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷Eö6öæf–ræ&÷E÷Fö¶VâÂÆöuö÷WGWB ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#ÓÓCÓƒÓ"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7Eöf–æÆ—¦Uö&÷E÷W&6†6Uö7&VFW5÷VæF–æuö÷&FW%÷W6–æu÷Æå÷&÷WFR‡6VÆbÂövWEöÖö6²Â‡V•öÖö6²“ ¢g&öÒâ–×÷'B&÷G0 ¢&÷WFVE÷æVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%&÷WFVBæVÂ"À¢W&ÃÒ&‡GG3¢ò÷&÷WFVBæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢—5ö7F—fSÕG'VRÀ¢¢&÷WFVEö–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×&÷WFVE÷æVÂÀ¢–æ&÷VæEö–CÓ"À¢6W'fW%ö—Ò'gâ×&÷WFVBæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'G—S×F7g6V7W&—G“ÖæöæR"À¢—5ö7F—fSÕG'VRÀ¢¢Æä–æ&÷VæE&÷WFRæö&¦V7G2æ7&VFR‡7F÷&S×6VÆbç7F÷&RÂÆã×6VÆbçÆâÂ–æ&÷VæC×&÷WFVEö–æ&÷VæBÂ&–÷&—G“Ó¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢7FFUöFF×°¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'6VæFW%ö6&EöæÖR#¢$Æ–6RÆF÷"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢Ð¢¢÷7Eö6ÆÇ2ÒµÐ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2’“ ¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦Uö&÷E÷W&6†6R€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò‚’À¢6†Eö–CÒ#C""À¢ ¢6VÆbæ76W'EG'VR‡&W7VÇE²'7V66W72%Ò¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ¢6VÆbæ76W'DWVÂ†÷&FW"æ–æ&÷VæBÂ&÷WFVEö–æ&÷VæB¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöæÖRÂ$Æ–6RÆF÷"¢6VÆbæ76W'EG'VR†÷&FW"ç–ÖVçE÷&V6V—Eö–ÖvRææÖRæVæG7v—F‚‚"æ§r"’¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'6÷W&6R%ÒÂ'FVÆVw&Õö&÷B"¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'&V6V—B%Õ²&f–ÆUö–B%ÒÂ'&V6V—BÖf–ÆR"¢6VÆbæ76W'DWVÂ‡‡V•öÖö6²æ6ÆÅö&w2æ·v&w5²&–æ&÷VæB%ÒÂ&÷WFVEö–æ&÷VæB¢6VÆbæ76W'DWVÂ„&÷DFÖ–ä÷&FW$ÖW76vRæö&¦V7G2æf–ÇFW"†÷&FW#Ö÷&FW"’æ6÷VçB‚’Â¢÷&FW"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'D—4æ÷DæöæR†÷&FW"æFÖ–åöæ÷F–f–VEöB¢6VÆbæ76W'D—4æ÷DæöæR†÷&FW"æFÖ–å÷&V6V—Eöæ÷F–f–VEöB ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7Eöf–æÆ—¦Uö&÷E÷W&6†6Uöf–Ç5÷6fVÇ•÷v†Vå÷Æå÷&÷WFUöÖ—76–æuöæEöfÆÆ&6µöF—6&ÆVB‡6VÆbÂövWEöÖö6²Â‡V•öÖö6²“ ¢g&öÒâ–×÷'B&÷G0¢g&öÒæ÷&FW%÷6W'f–6W2–×÷'BÄåô”ä$õTäEõ$õUDUôÔ•54”äuôÔU54tP ¢6VÆbç7F÷&RæÆÆ÷uövÆö&Åö–æ&÷VæEöfÆÆ&6²ÒfÇ6P¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²&ÆÆ÷uövÆö&Åö–æ&÷VæEöfÆÆ&6²"Â'WFFVEöB%Ò¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢7FFUöFF×°¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢Ð¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦Uö&÷E÷W&6†6R€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò‚’À¢6†Eö–CÒ#C""À¢ ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²'7V66W72%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²&W'&÷"%ÒÂÄåô”ä$õTäEõ$õUDUôÔ•54”äuôÔU54tR¢6VÆbæ76W'DfÇ6R„÷&FW"æö&¦V7G2æW†—7G2‚’¢‡V•öÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#####Ó##ÓC#Óƒ#Ó######"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7Eöf–æÆ—¦Uö&÷E÷W&6†6U÷&W6W'fW5ö7W7FöÕ÷föÇVÖU÷VçF—G•öF—66÷VçEöæEö6ÆVåöæÖR‡6VÆbÂövWEöÖö6²Â‡V•öÖö6²“ ¢g&öÒâ–×÷'B&÷G0¢g&öÒæ÷&FW%÷6W'f–6W2–×÷'BvWEö÷%ö7&VFUö7W7FöÕ÷föÇVÖU÷Æà ¢6VÆbç7F÷&Ræ7W7FöÕ÷föÇVÖU÷&–6U÷W%öv"ÒFV6–ÖÂ‚#"¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²&7W7FöÕ÷föÇVÖU÷&–6U÷W%öv""Â'WFFVEöB%Ò¢7W7FöÕ÷ÆâÒvWEö÷%ö7&VFUö7W7FöÕ÷föÇVÖU÷Æâ‡6VÆbç7F÷&RÂ#r"¢F—66÷VçD6öFRæö&¦V7G2æ7&VFR€¢6öFSÒ%4dS"À¢F—66÷VçE÷G—SÔF—66÷VçD6öFRäF—66÷VçEG—Räd•„TBÀ¢fÇVSÓÀ¢¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢7FFUöFF×°¢'Æåö–B#¢7W7FöÕ÷Æâç²À¢'VçF—G’#¢"À¢'6VæFW%ö6&EöæÖR#¢$Æ–6RÆF÷"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢&F—66÷VçEö6öFR#¢'6fS"À¢&F—66÷VçEöÖ÷VçB#¢À¢Ð¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦Uö&÷E÷W&6†6R€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò‚’À¢6†Eö–CÒ#C""À¢ ¢6VÆbæ76W'EG'VR‡&W7VÇE²'7V66W72%Ò¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"çÆâÂ7W7FöÕ÷Æâ¢6VÆbæ76W'DWVÂ†÷&FW"çVçF—G’Â"¢6VÆbæ76W'DWVÂ†÷&FW"æ÷&–v–æÅöÖ÷VçBÂC¢6VÆbæ76W'DWVÂ†÷&FW"æF—66÷VçEö6öFU÷FW‡BÂ%4dS"¢6VÆbæ76W'DWVÂ†÷&FW"æF—66÷VçEöÖ÷VçBÂ¢6VÆbæ76W'DWVÂ†÷&FW"æÖ÷VçBÂ3“¢6VÆbæ76W'EG'VR†÷&FW"æÖWFFF²&7W7FöÕ÷föÇVÖR%Ò¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²&7W7FöÕ÷föÇVÖUöv"%ÒÂ#rã"¢6VÆbæ76W'DWVÂ†÷&FW"çgåö6Æ–VçG2æ6÷VçB‚’Â¢6VÆbæ76W'E&VvW‚‡‡V•öÖö6²æ6ÆÅö&w2æ·v&w5²&VÖ–Å÷&Vf—‚%ÒÂ"%æÆ–6UöÆF÷õ³Ó–×¥×³‡ÒB" ¢F6‚‚'7F÷&Ræ&÷G2ç7–æ5÷gåö6Æ–VçE÷7FG2"Â&WGW&å÷fÇVS×·Ò¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#3333Ó33ÓC3Óƒ3Ó333333"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7Eöf–æÆ—¦Uö&÷E÷W&6†6UöGWÆ–6FU÷&WW6W5÷VæF–æuö÷&FW%÷v—F†÷WE÷&W&÷f—6–öæ–ær‡6VÆbÂövWEöÖö6²Â‡V•öÖö6²Â÷7FG5öÖö6²“ ¢g&öÒâ–×÷'B&÷G0 ¢7FFUöFFÒ°¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'6VæFW%ö6&EöæÖR#¢$Æ–6RÆF÷"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢Ð¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"‡7FFUöFFÖF–7B‡7FFUöFF’ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢f—'7BÒ&÷G2æf–æÆ—¦Uö&÷E÷W&6†6R€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò†f–ÆUö–CÒ'&V6V—BÖ"ÂVæ—VUö–CÒ'&V6V—BÖ"ÂÖW76vUö–CÓ#’À¢6†Eö–CÒ#C""À¢¢&÷E÷W6W"ç7FFRÒ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•@¢&÷E÷W6W"ç7FFUöFFÒF–7B‡7FFUöFF¢&÷E÷W6W"ç6fR‡WFFUöf–VÆG3Õ²'7FFR"Â'7FFUöFF"Â'WFFVEöB%Ò¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢GWÆ–6FRÒ&÷G2æf–æÆ—¦Uö&÷E÷W&6†6R€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò†f–ÆUö–CÒ'&V6V—BÖ""ÂVæ—VUö–CÒ'&V6V—BÖ""ÂÖW76vUö–CÓ#’À¢6†Eö–CÒ#C""À¢ ¢6VÆbæ76W'EG'VR†f—'7E²'7V66W72%Ò¢6VÆbæ76W'EG'VR†GWÆ–6FU²'7V66W72%Ò¢6VÆbæ76W'DWVÂ„÷&FW"æö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ‡‡V•öÖö6²æ6ÆÅö6÷VçBÂ¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'EG'VR†÷&FW"æÖWFFF²&GWÆ–6FU÷v&æ–ær%Õ²&FWFV7FVB%Ò¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²&GWÆ–6FU÷v&æ–ær%Õ²&GFV×Eö6÷VçB%ÒÂ ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ"&æ÷Bâ–ÖvR"’¢FVbFW7Eöf–æÆ—¦Uö&÷E÷W&6†6Uö–çfÆ–EöF÷væÆöFVE÷&V6V—EöFöW5öæ÷Eö7&VFUö÷&FW"‡6VÆbÂövWEöÖö6²Â‡V•öÖö6²“ ¢g&öÒâ–×÷'B&÷G0 ¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢7FFUöFF×°¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢Ð¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦Uö&÷E÷W&6†6R€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò‚’À¢6†Eö–CÒ#C""À¢ ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²'7V66W72%Ò¢6VÆbæ76W'DfÇ6R„÷&FW"æö&¦V7G2æW†—7G2‚’¢‡V•öÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7Eöf–æÆ—¦Uö&÷E÷&VæWvÅö7&VFW5÷VæF–æuö÷&FW%ööåöW†—7F–æuö6Æ–VçEöæEö&Æö6·5öGWÆ–6FR‡6VÆbÂövWEöÖö6²“ ¢g&öÒâ–×÷'B&÷G0 ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6R"¢÷&–v–æÅö÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢WV–CÒ#CCCCÓCCÓCCÓƒCÓCCCCCC"À¢7V%öÆ–æ³Ò&‡GG3¢òöW†×ÆRæ6öÒ÷7V"ööÆB"À¢F—&V7EöÆ–æ³Ò'fÆW73¢òööÆB"À¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&–v–æÅö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢‡V•öVÖ–ÃÒ&Æ–6Uóv""À¢WV–CÖ÷&–v–æÅö÷&FW"çWV–BÀ¢7V%ö–CÒ&öÆB"À¢7V%öÆ–æ³Ö÷&–v–æÅö÷&FW"ç7V%öÆ–æ²À¢F—&V7EöÆ–æ³Ö÷&–v–æÅö÷&FW"æF—&V7EöÆ–æ²À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢GW&F–öåöF—3×6VÆbçÆâæGW&F–öåöF—2À¢FWf–6UöÆ–Ö—C×6VÆbçÆâæFWf–6UöÆ–Ö—BÀ¢¢F—66÷VçD6öFRæö&¦V7G2æ7&VFR€¢6öFSÒ%$TäUs"À¢F—66÷VçE÷G—SÔF—66÷VçD6öFRäF—66÷VçEG—Räd•„TBÀ¢fÇVSÓÀ¢¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢7W7FöÖW#Ö7W7FöÖW"À¢7FFUöFF×°¢&fÆ÷r#¢'&VæWvÂ"À¢'&VæWvÅö6Æ–VçE÷V&Æ–5ö–B#¢7G"‡gåö6Æ–VçBçV&Æ–5ö–B’À¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'6VæFW%ö6&EöæÖR#¢$Æ–6R&VæWvÂ"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢&F—66÷VçEö6öFR#¢'&VæWs"À¢&F—66÷VçEöÖ÷VçB#¢À¢ÒÀ¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢f—'7BÒ&÷G2æf–æÆ—¦Uö&÷E÷&VæWvÂ€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò†f–ÆUö–CÒ'&VæWvÂÖ"ÂVæ—VUö–CÒ'&VæWvÂÖ"ÂÖW76vUö–CÓ3’À¢6†Eö–CÒ#C""À¢¢&÷E÷W6W"ç7FFRÒ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•@¢&÷E÷W6W"ç7FFUöFFÒ°¢&fÆ÷r#¢'&VæWvÂ"À¢'&VæWvÅö6Æ–VçE÷V&Æ–5ö–B#¢7G"‡gåö6Æ–VçBçV&Æ–5ö–B’À¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'6VæFW%ö6&EöæÖR#¢$Æ–6R&VæWvÂ"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢Ð¢&÷E÷W6W"ç6fR‡WFFUöf–VÆG3Õ²'7FFR"Â'7FFUöFF"Â'WFFVEöB%Ò¢GWÆ–6FRÒ&÷G2æf–æÆ—¦Uö&÷E÷&VæWvÂ€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò†f–ÆUö–CÒ'&VæWvÂÖ""ÂVæ—VUö–CÒ'&VæWvÂÖ""ÂÖW76vUö–CÓ3’À¢6†Eö–CÒ#C""À¢ ¢6VÆbæ76W'EG'VR†f—'7E²'7V66W72%Ò¢6VÆbæ76W'EG'VR†GWÆ–6FU²'VæF–ær%Ò¢&VæWvÂÒ÷&FW"æö&¦V7G2æW†6ÇVFR‡³Ö÷&–v–æÅö÷&FW"ç²’ævWB‚¢6VÆbæ76W'DWVÂ‡&VæWvÂç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ¢6VÆbæ76W'DWVÂ‡&VæWvÂæ–æ&÷VæBÂ6VÆbæ–æ&÷VæB¢6VÆbæ76W'DWVÂ‡&VæWvÂæÖWFFF²'&VæWvÅö6Æ–VçE÷²%ÒÂgåö6Æ–VçBç²¢6VÆbæ76W'DWVÂ‡&VæWvÂæF—66÷VçEö6öFU÷FW‡BÂ%$TäUs"¢6VÆbæ76W'DWVÂ‡&VæWvÂæF—66÷VçEöÖ÷VçBÂ¢6VÆbæ76W'Dæ÷D–â‚'7W&W75öæWuö÷&FW%öæ÷F–f–6F–öâ"Â&VæWvÂæÖWFFF¢6VÆbæ76W'DWVÂ…eä6Æ–VçBæö&¦V7G2æ6÷VçB‚’Â ¢FVbFW7Eöf–æÆ—¦Uö&÷E÷&VæWvÅ÷&V¦V7G5öFVÆWFVEö6Æ–VçB‡6VÆb“ ¢g&öÒâ–×÷'B&÷G0 ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6R"¢÷&–v–æÅö÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ&Æ–6UöFVÆWFVB"À¢WV–CÒ#SSSSÓSSÓCSÓƒSÓSSSSSS"À¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&–v–æÅö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&Æ–6UöFVÆWFVB"À¢‡V•öVÖ–ÃÒ&Æ–6UöFVÆWFVB"À¢WV–CÖ÷&–v–æÅö÷&FW"çWV–BÀ¢7FGW3Õeä6Æ–VçBå7FGW2äDTÄUDTBÀ¢FVÆWFVEöC×F–ÖW¦öæRææ÷r‚’À¢¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢7W7FöÖW#Ö7W7FöÖW"À¢7FFUöFF×°¢&fÆ÷r#¢'&VæWvÂ"À¢'&VæWvÅö6Æ–VçE÷V&Æ–5ö–B#¢7G"‡gåö6Æ–VçBçV&Æ–5ö–B’À¢'Æåö–B#¢6VÆbçÆâç²À¢ÒÀ¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦Uö&÷E÷&VæWvÂ€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢·ÒÀ¢6†Eö–CÒ#C""À¢ ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²'7V66W72%Ò¢6VÆbæ76W'DWVÂ„÷&FW"æö&¦V7G2æW†6ÇVFR‡³Ö÷&–v–æÅö÷&FW"ç²’æ6÷VçB‚’Â¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR ¢F6‚‚'7F÷&Ræ÷&FW%ö7F–öç2æVæ&ÆUö6Æ–VçB"Â&WGW&å÷fÇVSÕG'VR¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#ccccÓccÓCcÓƒcÓcccccc"’¢FVbFW7Eöf–æÆ—¦UöFÖ–åöF—&V7E÷W&6†6Uö7F—fFW5ööå÷Æå÷&÷WFR‡6VÆbÂ‡V•öÖö6²ÂöVæ&ÆUöÖö6²“ ¢g&öÒâ–×÷'B&÷G0 ¢&÷WFVE÷æVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ$FÖ–â&÷WFVBæVÂ"À¢W&ÃÒ&‡GG3¢òöFÖ–â×&÷WFVBæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢—5ö7F—fSÕG'VRÀ¢¢&÷WFVEö–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×&÷WFVE÷æVÂÀ¢–æ&÷VæEö–CÓ2À¢6W'fW%ö—Ò'gâÖFÖ–â×&÷WFVBæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'G—S×F7g6V7W&—G“ÖæöæR"À¢—5ö7F—fSÕG'VRÀ¢¢Æä–æ&÷VæE&÷WFRæö&¦V7G2æ7&VFR‡7F÷&S×6VÆbç7F÷&RÂÆã×6VÆbçÆâÂ–æ&÷VæC×&÷WFVEö–æ&÷VæBÂ&–÷&—G“Ó¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢W6W%ö–CÒ#““’"À¢W6W&æÖSÒ&FÖ–â"À¢F—7Æ•öæÖSÒ$FÖ–â"À¢7FFSÔ&÷EW6W"å7FFRä%U•õt•EôäÔRÀ¢7FFUöFF×°¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'6VæFW%ö6&EöæÖR#¢$FÖ–â6öæf–r"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢ÒÀ¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦UöFÖ–åöF—&V7E÷W&6†6R‡6VÆbæ&÷Eö6öæf–rÂ&÷E÷W6W"Â6†Eö–CÒ#““’" ¢6VÆbæ76W'EG'VR‡&W7VÇE²'7V66W72%Ò¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2ä4ôÕÄUDTB¢6VÆbæ76W'DWVÂ†÷&FW"æ–æ&÷VæBÂ&÷WFVEö–æ&÷VæB¢6VÆbæ76W'EG'VR†÷&FW"æÖWFFF²&FÖ–åöF—&V7E÷W&6†6R%Ò¢6VÆbæ76W'DWVÂ‡‡V•öÖö6²æ6ÆÅö&w2æ·v&w5²&–æ&÷VæB%ÒÂ&÷WFVEö–æ&÷VæB ¢F6‚‚'7F÷&Ræ÷&FW%ö7F–öç2æVæ&ÆUö6Æ–VçB"Â&WGW&å÷fÇVSÔfÇ6R¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#ssssÓssÓCsÓƒsÓssssss"’¢FVbFW7Eöf–æÆ—¦UöFÖ–åöF—&V7E÷W&6†6U÷‡V•öVæ&ÆUöf–ÇW&UöFöW5öæ÷Eöf¶Uö6ö×ÆWFR‡6VÆbÂ÷‡V•öÖö6²ÂöVæ&ÆUöÖö6²“ ¢g&öÒâ–×÷'B&÷G0 ¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢W6W%ö–CÒ#““’"À¢W6W&æÖSÒ&FÖ–â"À¢F—7Æ•öæÖSÒ$FÖ–â"À¢7FFSÔ&÷EW6W"å7FFRä%U•õt•EôäÔRÀ¢7FFUöFF×°¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'6VæFW%ö6&EöæÖR#¢$FÖ–â6öæf–r"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢ÒÀ¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦UöFÖ–åöF—&V7E÷W&6†6R‡6VÆbæ&÷Eö6öæf–rÂ&÷E÷W6W"Â6†Eö–CÒ#““’" ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²'7V66W72%Ò¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ¢6VÆbæ76W'DWVÂ†÷&FW"çfW&–f–6F–öå÷7FGW2Â÷&FW"åfW&–f–6F–öå7FGW2åTäD”är¢6VÆbæ76W'DfÇ6R†÷&FW"çgåö6Æ–VçG2æf–ÇFW"‡7FGW3Õeä6Æ–VçBå7FGW2ä5D•dR’æW†—7G2‚’ ¢F6‚‚'7F÷&Ræ÷&FW%ö7F–öç2ç&VæWuö6Æ–VçB"¢FVbFW7Eöf–æÆ—¦UöFÖ–åöF—&V7E÷&VæWvÅöW‡FVæG5öW†—7F–æuö6Æ–VçE÷v—F†÷WEö7&VF–æuöæWuö6Æ–VçB‡6VÆbÂ&VæWuöÖö6²“ ¢g&öÒâ–×÷'B&÷G0 ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$FÖ–â"ÂW6W&æÖSÒ&FÖ–â"¢÷&–v–æÅö÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ&FÖ–åóv""À¢WV–CÒ#ƒƒƒƒÓƒƒÓCƒÓƒƒÓƒƒƒƒƒƒ"À¢7V%öÆ–æ³Ò&‡GG3¢òöW†×ÆRæ6öÒ÷7V"öFÖ–â"À¢F—&V7EöÆ–æ³Ò'fÆW73¢òöFÖ–â"À¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&–v–æÅö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&FÖ–åóv""À¢‡V•öVÖ–ÃÒ&FÖ–åóv""À¢WV–CÖ÷&–v–æÅö÷&FW"çWV–BÀ¢7V%ö–CÒ&FÖ–â"À¢7V%öÆ–æ³Ö÷&–v–æÅö÷&FW"ç7V%öÆ–æ²À¢F—&V7EöÆ–æ³Ö÷&–v–æÅö÷&FW"æF—&V7EöÆ–æ²À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢GW&F–öåöF—3×6VÆbçÆâæGW&F–öåöF—2À¢FWf–6UöÆ–Ö—C×6VÆbçÆâæFWf–6UöÆ–Ö—BÀ¢¢&VæWuöÖö6²ç&WGW&å÷fÇVRÒ°¢&W‡—'•öB#¢F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Óc’À¢'&r#¢²'&VæWvVB#¢G'VWÒÀ¢Ð¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢W6W%ö–CÒ#““’"À¢W6W&æÖSÒ&FÖ–â"À¢F—7Æ•öæÖSÒ$FÖ–â"À¢7W7FöÖW#Ö7W7FöÖW"À¢7FFSÔ&÷EW6W"å7FFRä%U•õt•EôäÔRÀ¢7FFUöFF×°¢&fÆ÷r#¢'&VæWvÂ"À¢'&VæWvÅö6Æ–VçE÷V&Æ–5ö–B#¢7G"‡gåö6Æ–VçBçV&Æ–5ö–B’À¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'6VæFW%ö6&EöæÖR#¢$FÖ–â&VæWvÂ"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢ÒÀ¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦UöFÖ–åöF—&V7E÷&VæWvÂ‡6VÆbæ&÷Eö6öæf–rÂ&÷E÷W6W"Â6†Eö–CÒ#““’" ¢6VÆbæ76W'EG'VR‡&W7VÇE²'7V66W72%Ò¢&VæWvÂÒ÷&FW"æö&¦V7G2æW†6ÇVFR‡³Ö÷&–v–æÅö÷&FW"ç²’ævWB‚¢6VÆbæ76W'DWVÂ‡&VæWvÂç7FGW2Â÷&FW"å7FGW2ä4ôÕÄUDTB¢6VÆbæ76W'DWVÂ‡&VæWvÂæ–æ&÷VæBÂ6VÆbæ–æ&÷VæB¢6VÆbæ76W'DWVÂ‡&VæWvÂæÖWFFF²'&VæWvÅö6Æ–VçE÷²%ÒÂgåö6Æ–VçBç²¢6VÆbæ76W'DWVÂ…eä6Æ–VçBæö&¦V7G2æ6÷VçB‚’Â¢gåö6Æ–VçBç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡gåö6Æ–VçBç7FGW2Âeä6Æ–VçBå7FGW2ä5D•dR¢6VÆbæ76W'DWVÂ‡gåö6Æ–VçBç‡V•÷&rÂ²'&VæWvVB#¢G'VWÒ¢&VæWuöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷&VwVÆ%÷W6W%ö6öæf–uöæÖUöfÆ÷u÷v—G5öf÷%÷&V6V—Eö–ç7FVEööeöFÖ–åöF—&V7Eö7F—fF–öâ‡6VÆbÂ÷÷7EöÖö6²Â‡V•öÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚$æ÷&ÖÂ6öæf–r"ÂÖW76vUö–CÓ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R„÷&FW"æö&¦V7G2æW†—7G2‚’¢‡V•öÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFUöFF²'6VæFW%ö6&EöæÖR%ÒÂ$æ÷&ÖÂ6öæf–r" ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#““““Ó““ÓC“Óƒ“Ó““““““"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7Eöf–æÆ—¦Uö&÷E÷W&6†6Uöæ÷F–f–6F–öåöf–ÇW&UöFöW5öæ÷Eö&÷'Eö÷&FW%ö7&VF–öâ‡6VÆbÂövWEöÖö6²Â÷‡V•öÖö6²“ ¢g&öÒâ–×÷'B&÷G0 ¢&÷E÷W6W"Ò6VÆbæÖ¶Uö&÷E÷W6W"€¢7FFUöFF×°¢'Æåö–B#¢6VÆbçÆâç²À¢'VçF—G’#¢À¢'6VæFW%ö6&EöæÖR#¢$Æ–6RÆF÷"À¢'–ÖVçE÷F–ÖR#¢#C£3R"À¢Ð¢ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×6VÆbæ&÷E÷÷7E÷6–FUöVffV7B‚’“ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç6VæEöæWuö÷&FW%÷Fõö6öæf–r"Â6–FUöVffV7CÔW†6WF–öâ‚&æ÷F–f–6F–öâ&ööÒ"’“ ¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢&W7VÇBÒ&÷G2æf–æÆ—¦Uö&÷E÷W&6†6R€¢6VÆbæ&÷Eö6öæf–rÀ¢&÷E÷W6W"À¢·ÒÀ¢6VÆbç&V6V—Eöf–ÆUö–æfò‚’À¢6†Eö–CÒ#C""À¢ ¢6VÆbæ76W'EG'VR‡&W7VÇE²'7V66W72%Ò¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ¢6VÆbæ76W'EG'VR„&÷DWfVçDÆöræö&¦V7G2æf–ÇFW"†÷&FW#Ö÷&FW"Â7FGW3Ô&÷DWfVçDÆörå7FGW2äd”ÄTB’æW†—7G2‚’ ¢FVbFW7Eö&÷EöWfVçEöÆöu÷&VF7G5÷&V6V—Eöf–ÆUö–G5÷Fö¶Vç5öæEö6öæf–uöÆ–æ·2‡6VÆb“ ¢g&öÒæ&÷G2–×÷'BÆöuöWfVç@ ¢ÆöuöWfVçB€¢6VÆbæ&÷Eö6öæf–rÀ¢WfVçE÷G—SÔ&÷DWfVçDÆöräWfVçEG—RäU%$õ"À¢7FGW3Ô&÷DWfVçDÆörå7FGW2äd”ÄTBÀ¢ÖW76vSÒ$f–ÆVBf÷"fÆW73¢òóÓÓCÓƒÓW†×ÆRæ6öÒÆ–æµõ4T5$UB"À¢&u÷–ÆöC×°¢'&V6V—B#¢°¢&f–ÆUö–B#¢'FVÆVw&ÒÖf–ÆRÖ–B×6V7&WB"À¢&f–ÆU÷Væ—VUö–B#¢'FVÆVw&ÒÖf–ÆR×Væ—VR×6V7&WB"À¢&f–ÆU÷F‚#¢'†÷F÷2÷&—fFR×&V6V—Bæ§r"À¢ÒÀ¢&&÷E÷Fö¶Vâ#¢6VÆbæ&÷Eö6öæf–ræ&÷E÷Fö¶VâÀ¢'W&Â#¢&‡GG3¢òöW†×ÆRæ6öÒ÷7V"÷&—fFR×7V'67&—F–öâ×Fö¶Vâ"À¢ÒÀ¢ ¢WfVçBÒ&÷DWfVçDÆöræö&¦V7G2ævWB‚¢6W&–Æ—¦VBÒ§6öâæGV×2†WfVçBç&u÷–ÆöBÂVç7W&Uö66–“ÔfÇ6R¢6VÆbæ76W'Dæ÷D–â‚'FVÆVw&ÒÖf–ÆRÖ–B×6V7&WB"Â6W&–Æ—¦VB¢6VÆbæ76W'Dæ÷D–â‚'FVÆVw&ÒÖf–ÆR×Væ—VR×6V7&WB"Â6W&–Æ—¦VB¢6VÆbæ76W'Dæ÷D–â‚'&—fFR×&V6V—Bæ§r"Â6W&–Æ—¦VB¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷Eö6öæf–ræ&÷E÷Fö¶VâÂ6W&–Æ—¦VB¢6VÆbæ76W'Dæ÷D–â‚'&—fFR×7V'67&—F–öâ×Fö¶Vâ"Â6W&–Æ—¦VB¢6VÆbæ76W'D–â‚#Ç&V6V—BÖf–ÆR×&VF7FVCâ"Â6W&–Æ—¦VB¢6VÆbæ76W'D–â‚#Ç&VF7FVB×Fö¶Vãâ"Â6W&–Æ—¦VB¢6VÆbæ76W'D–â‚#Æ6öæf–rÖÆ–æ²×&VF7FVCâ"Â6W&–Æ—¦VB¢6VÆbæ76W'D–â‚#Æ6öæf–rÖÆ–æ²×&VF7FVCâ"ÂWfVçBæÖW76vR ¢FVbVæ&ÆUöf÷&6Uö¦ö–â‡6VÆbÂ¢Â6†ææVÅö–CÒ""ÂW6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"Â–çf—FUöÆ–æ³Ò&‡GG3¢ò÷BæÖR÷gå÷7F÷&Uö6†ææVÂ"“ ¢6VÆbæ&÷Eö6öæf–ræf÷&6U÷FVÆVw&Õö6†ææVÅö¦ö–âÒG'VP¢6VÆbæ&÷Eö6öæf–rçFVÆVw&Õ÷&WV—&VEö6†ææVÅö–BÒ6†ææVÅö–@¢6VÆbæ&÷Eö6öæf–rçFVÆVw&Õ÷&WV—&VEö6†ææVÅ÷W6W&æÖRÒW6W&æÖP¢6VÆbæ&÷Eö6öæf–rçFVÆVw&Õ÷&WV—&VEö6†ææVÅö–çf—FUöÆ–æ²Ò–çf—FUöÆ–æ°¢6VÆbæ&÷Eö6öæf–rçFVÆVw&Õö¦ö–åö6†V6µöÖW76vRÒ-Š‹Š}¸ÂŠ}‹=Š­˜Š}Šý˜rŠ}‹"‹ŠŠ}Š¢Š}ŠŠ­ŠýŠr‹‹m˜‚ªŠ}˜mŠ}˜B‹M˜¸ÍŠòâ ¢6VÆbæ&÷Eö6öæf–rç6fR€¢WFFUöf–VÆG3Õ°¢&f÷&6U÷FVÆVw&Õö6†ææVÅö¦ö–â"À¢'FVÆVw&Õ÷&WV—&VEö6†ææVÅö–B"À¢'FVÆVw&Õ÷&WV—&VEö6†ææVÅ÷W6W&æÖR"À¢'FVÆVw&Õ÷&WV—&VEö6†ææVÅö–çf—FUöÆ–æ²"À¢'FVÆVw&Õö¦ö–åö6†V6µöÖW76vR"À¢'WFFVEöB"À¢Ð¢ ¢FVbVæ&ÆUög&VU÷G&–Â‡6VÆbÂ¢ÂVæ&ÆVCÕG'VR“ ¢6VÆbç7F÷&Ræg&VU÷G&–ÅöVæ&ÆVBÒVæ&ÆV@¢6VÆbç7F÷&Ræg&VU÷G&–Å÷æVÂÒ6VÆbçæVÀ¢6VÆbç7F÷&Ræg&VU÷G&–Åö–æ&÷VæBÒ6VÆbæ–æ&÷Væ@¢6VÆbç7F÷&Ræg&VU÷G&–Å÷G&ff–5öv"ÒFV6–ÖÂ‚#ã"¢6VÆbç7F÷&Ræg&VU÷G&–ÅöGW&F–öåö†÷W'2Ò#@¢6VÆbç7F÷&Ræg&VU÷G&–Åö6ööÆF÷våöF—2Ò3 ¢6VÆbç7F÷&Rç6fR€¢WFFUöf–VÆG3Õ°¢&g&VU÷G&–ÅöVæ&ÆVB"À¢&g&VU÷G&–Å÷æVÂ"À¢&g&VU÷G&–Åö–æ&÷VæB"À¢&g&VU÷G&–Å÷G&ff–5öv""À¢&g&VU÷G&–ÅöGW&F–öåö†÷W'2"À¢&g&VU÷G&–Åö6ööÆF÷våöF—2"À¢'WFFVEöB"À¢Ð¢ ¢FVbÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡6VÆbÂ÷7Eö6ÆÇ2Â¢Â7FGW3Ò&ÖVÖ&W""Â•öf–ÇW&UöFW67&—F–öãÒ""“ ¢FVb6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂFFÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ&FF#¢FFÂ¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWD6†DÖVÖ&W""“ ¢–b•öf–ÇW&UöFW67&—F–öã ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢fÇ6RÂ&FW67&—F–öâ#¢•öf–ÇW&UöFW67&—F–öçÒ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²'7FGW2#¢7FGW7×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&ÖW76vUö–B#¢²ÆVâ‡÷7Eö6ÆÇ2—×Ò ¢&WGW&â6–FUöVffV7@ ¢FVb6VçEöÖW76vU÷–ÆöG2‡6VÆbÂ÷7Eö6ÆÇ2“ ¢&WGW&â°¢6ÆÅ²&§6öâ%Ð¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"’æB6ÆÂævWB‚&§6öâ"¢Ð ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷7F'Eö7&VFW5ö&÷E÷W6W%öæEö7W7FöÖW"‡6VÆbÂ÷÷7B“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"æ6†Eö–BÂ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"çW6W&æÖRÂ&Æ–6R"¢6VÆbæ76W'D—4æ÷DæöæR†&÷E÷W6W"æ7W7FöÖW"¢6VÆbæ76W'DWVÂ†&÷E÷W6W"æ7W7FöÖW"æF—7Æ•öæÖRÂ$Æ–6R"¢6VÆbæ76W'DWVÂ†&÷E÷W6W"æ7W7FöÖW"çW6W&æÖRÂ&Æ–6R"¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR¢6VÆbæ76W'DWVÂ„7W7FöÖW"æö&¦V7G2æ6÷VçB‚’Â¢g&öÒæ&÷E÷F&vWG2–×÷'BvWE÷&–Ö'•ö7W7FöÖW%÷FVÆVw&Õ÷F&vW@ ¢F&vWBÒvWE÷&–Ö'•ö7W7FöÖW%÷FVÆVw&Õ÷F&vWB†&÷E÷W6W"æ7W7FöÖW"Â7F÷&S×6VÆbç7F÷&R¢6VÆbæ76W'D—4æ÷DæöæR‡F&vWB¢6VÆbæ76W'DWVÂ‡F&vWBæ6†Eö–BÂ#C""¢6VÆbæ76W'DWVÂ‡F&vWBçFVÆVw&Õ÷W6W%ö–BÂ#C""¢6VÆbæ76W'DWVÂ‡F&vWBç6÷W&6RÂ&&÷E÷W6W""¢–ÆöBÒ÷÷7Bæ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'DWVÂ€¢6ÆÆ&6µ÷fÇVW2À¢°¢'W6W#¦'W’"À¢'W6W#§7V'2"À¢'W6W#¦g&VU÷G&–Â"À¢'W6W#§&VæWr"À¢'W6W#¦÷&FW'2"À¢'W6W#¦6öæf–uöÆöö·W"À¢'W6W#§&VfW'&Ç2"À¢'W6W#§7W÷'B"À¢'W6W#¦†VÇ"À¢'W6W#§&öf–ÆR"À¢ÒÀ¢¢6VÆbæ76W'D–â‚-Š}‹"˜]˜m˜¸Â‹-¸Í‹Š}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷7F'E÷–ÆöE÷6WG5ö7W7FöÖW%÷&VfW'&W"‡6VÆbÂ÷÷7B“ ¢–çf—FW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$–çf—FW"" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæÖW76vR€¢b"÷7F'B&Ve÷¶–çf—FW"ç&VfW'&Åö6öFWÒ"À¢W6W%ö–CÓC2À¢W6W&æÖSÒ&&ö""À¢f—'7EöæÖSÒ$&ö""À¢¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C2"¢&÷E÷W6W"æ7W7FöÖW"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"æ7W7FöÖW"ç&VfW'&VEö'’Â–çf—FW"¢6VÆbæ76W'EG'VR€¢&VfW'&Âæö&¦V7G2æf–ÇFW"‡&VfW'&W#Ö–çf—FW"Â&VfW'&VEö7W7FöÖW#Ö&÷E÷W6W"æ7W7FöÖW"’æW†—7G2‚¢ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷7F'EöÆ–æµ÷–ÆöEöÆ–æ·5ö&÷E÷W6W%÷Fõ÷vV%ö7W7FöÖW"‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Và ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ%vV"7W7FöÖW""¢&u÷Fö¶VâÂFö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ†7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"æ7W7FöÖW"Â7W7FöÖW"¢Fö¶Vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡Fö¶Vâç7FGW2ÂvV%FVÆVw&ÔÆ–æµFö¶Vâå7FGW2åU4TB¢6VÆbæ76W'DWVÂ‡Fö¶Vâæ&÷E÷W6W"Â&÷E÷W6W"¢6VÆbæ76W'D—4æ÷DæöæR‡Fö¶VâçW6VEöB¢6VçE÷FW‡G2Ò¶6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Òf÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7EÐ¢6VÆbæ76W'EG'VR†ç’‚.)ÈRŠÝ‹=Š}Š‚‹M˜]ŠrŠ˜r‹ŠŠ}Š¢˜‹]˜B‹MŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'EG'VR†ç’‚-Š}‹"˜]˜m˜¸Â‹-¸Í‹Š}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'DWVÂ„7W7FöÖW"æö&¦V7G2æ6÷VçB‚’Â ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6VE÷7F'EöÆ–æµ÷–ÆöEö6ææ÷Eö&U÷&WW6VB‡6VÆbÂ÷÷7B“ ¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Và ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ%vV"7W7FöÖW""¢&u÷Fö¶VâÂFö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ†7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B"¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"ÂW6W%ö–CÓC"’ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"ÂW6W%ö–CÓC2ÂW6W&æÖSÒ&&ö""Âf—'7EöæÖSÒ$&ö""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢Fö¶Vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡Fö¶Vâç7FGW2ÂvV%FVÆVw&ÔÆ–æµFö¶Vâå7FGW2åU4TB¢6V6öæEö&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C2"¢6VÆbæ76W'D—4æöæR‡6V6öæEö&÷E÷W6W"æ7W7FöÖW" ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6VE÷7F'EöÆ–æµ÷–ÆöEö—5ö–FV×÷FVçEöf÷%÷6ÖUö&÷E÷W6W"‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Và ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ%vV"7W7FöÖW""¢&u÷Fö¶VâÂFö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ†7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B"¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"ÂW6W%ö–CÓC"’¢÷7EöÖö6²ç&W6WEöÖö6²‚ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"ÂW6W%ö–CÓC"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢Fö¶Vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡Fö¶Vâç7FGW2ÂvV%FVÆVw&ÔÆ–æµFö¶Vâå7FGW2åU4TB¢6VÆbæ76W'DWVÂ„&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""’æ7W7FöÖW"Â7W7FöÖW"¢6VçE÷FW‡G2Ò¶6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Òf÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7EÐ¢6VÆbæ76W'EG'VR†ç’‚-˜-Š˜MŠ}˜²Š˜r‹ŠŠ}Š¢˜‹]˜B‹MŠý˜rŠ}‹=Š¢"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'EG'VR†ç’‚-Š}‹"˜]˜m˜¸Â‹-¸Í‹Š}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöW‡—&VE÷7F'EöÆ–æµ÷–ÆöEö—5÷&V¦V7FVB‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Và ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ%vV"7W7FöÖW""¢&u÷Fö¶VâÂFö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ†7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B"¢vV%FVÆVw&ÔÆ–æµFö¶Vâæö&¦V7G2æf–ÇFW"‡³×Fö¶Vâç²’çWFFR†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF†Ö–çWFW3Ó’ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢Fö¶Vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡Fö¶Vâç7FGW2ÂvV%FVÆVw&ÔÆ–æµFö¶Vâå7FGW2äU…•$TB¢6VçE÷FW‡BÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢6VÆbæ76W'D–â‚-˜M¸Í˜mª’Š}Š­‹]Š}˜B˜mŠ}˜]‹Š­Š‹¸ÍŠr˜]˜m˜-‹m¸Â‹MŠý˜rŠ}‹=Š¢"Â6VçE÷FW‡B¢6VÆbæ76W'D—4æöæR„&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""’æ7W7FöÖW" ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö–çfÆ–E÷7F'EöÆ–æµ÷–ÆöEö—5÷&V¦V7FVEöæE÷&VF7FVEö–åöWfVçEöÆör‡6VÆbÂ÷7EöÖö6²“ ¢&u÷Fö¶VâÒ'6†÷'G6V7&WB  ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VçE÷FW‡BÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢6VÆbæ76W'D–â‚-˜M¸Í˜mª’Š}Š­‹]Š}˜B˜mŠ}˜]‹Š­Š‹¸ÍŠr˜]˜m˜-‹m¸Â‹MŠý˜rŠ}‹=Š¢"Â6VçE÷FW‡B¢Æöw5÷FW‡BÒ§6öâæGV×2†Æ—7B„&÷DWfVçDÆöræö&¦V7G2çfÇVW2‚&ÖW76vR"Â'&u÷–ÆöB"’’ÂVç7W&Uö66–“ÔfÇ6R¢6VÆbæ76W'Dæ÷D–â‡&u÷Fö¶VâÂÆöw5÷FW‡B¢6VÆbæ76W'Dæ÷D–â†b&Æ–æµ÷·&u÷Fö¶VçÒ"ÂÆöw5÷FW‡B¢6VÆbæ76W'D–â‚&Æ–æµóÇ&VF7FVCâ"ÂÆöw5÷FW‡B ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷7F'EöÆ–æµ÷–ÆöEöf÷%÷6ÖUö7W7FöÖW%÷&WGW&ç5öÇ&VG•öÆ–æ¶VB‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Và ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ%vV"7W7FöÖW""¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢&u÷Fö¶VâÂFö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ†7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢Fö¶Vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡Fö¶Vâç7FGW2ÂvV%FVÆVw&ÔÆ–æµFö¶Vâå7FGW2åU4TB¢6VçE÷FW‡G2Ò¶6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Òf÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7EÐ¢6VÆbæ76W'EG'VR†ç’‚-˜-Š˜MŠ}˜²Š˜r‹ŠŠ}Š¢˜‹]˜B‹MŠý˜rŠ}‹=Š¢"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷7F'EöÆ–æµ÷–ÆöEöFöW5öæ÷EöÖ÷fUö&÷E÷W6W%ög&öÕö÷F†W%ö7W7FöÖW"‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Và ¢F&vWEö7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ%vV"7W7FöÖW""¢W†—7F–æuö7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$W†—7F–ær&÷B7W7FöÖW""¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#ÖW†—7F–æuö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢&u÷Fö¶VâÂFö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ‡F&vWEö7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"æ7W7FöÖW"ÂW†—7F–æuö7W7FöÖW"¢Fö¶Vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡Fö¶Vâç7FGW2ÂvV%FVÆVw&ÔÆ–æµFö¶Vâå7FGW2ä5D•dR¢6VçE÷FW‡BÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢6VÆbæ76W'D–â‚-Š}¸Í˜bŠÝ‹=Š}Š‚Š­˜Mªý‹Š}˜R˜-Š˜MŠ}˜²Š˜r¸Íª’ŠÝ‹=Š}Š‚Šý¸Íªý‹˜‹]˜B‹MŠý˜rŠ}‹=Š¢"Â6VçE÷FW‡B ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eöf÷&6Uö¦ö–å÷7F'EöÆ–æµö¶VW5öÆ–æµögFW%öÖVÖ&W'6†—öwV&B‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Và ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‚¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÆVgB"¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ%vV"7W7FöÖW""¢&u÷Fö¶VâÂFö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ†7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†b"÷7F'BÆ–æµ÷·&u÷Fö¶VçÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢Fö¶Vâç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡Fö¶Vâç7FGW2ÂvV%FVÆVw&ÔÆ–æµFö¶Vâå7FGW2åU4TB¢6VÆbæ76W'DWVÂ„&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""’æ7W7FöÖW"Â7W7FöÖW"¢6VçE÷FW‡G2Ò·–ÆöE²'FW‡B%Òf÷"–ÆöB–â6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•Ð¢6VÆbæ76W'EG'VR†ç’‚.)ÈRŠÝ‹=Š}Š‚‹M˜]ŠrŠ˜r‹ŠŠ}Š¢˜‹]˜B‹MŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'EG'VR†ç’‚-Š‹Š}¸ÂŠ}‹=Š­˜Š}Šý˜rŠ}‹"‹ŠŠ}Š¢Š}ŠŠ­ŠýŠr‹‹m˜‚ªŠ}˜mŠ}˜B‹M˜¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'DfÇ6R†ç’‚-Š}‹"˜]˜m˜¸Â‹-¸Í‹Š}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eöf÷&6Uö¦ö–åöF—6&ÆVEöFöW5öæ÷Eö6†V6µöÖVÖ&W'6†—‡6VÆbÂ÷7EöÖö6²“ ¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÆVgB" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R†ç’†6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"övWD6†DÖVÖ&W""’f÷"6ÆÂ–â÷7Eö6ÆÇ2’¢6VÆbæ76W'D–â‚-Ší‹¸ÍŠò‹=‹˜¸Í‹2"Â6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•²ÓÕ²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eöf÷&6Uö¦ö–åöVæ&ÆVEöÆÆ÷w5ö6†ææVÅöÖVÖ&W"‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÖVÖ&W"" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢ÖVÖ&W'6†—÷–ÆöG2Ò°¢6ÆÅ²&§6öâ%Ð¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"övWD6†DÖVÖ&W""¢Ð¢6VÆbæ76W'DWVÂ†ÖVÖ&W'6†—÷–ÆöG5³Õ²&6†Eö–B%ÒÂ$gå÷7F÷&Uö6†ææVÂ"¢6VÆbæ76W'DWVÂ†ÖVÖ&W'6†—÷–ÆöG5³Õ²'W6W%ö–B%ÒÂC"¢6VÆbæ76W'D–â‚-Ší‹¸ÍŠò‹=‹˜¸Í‹2"Â6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•²ÓÕ²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eöf÷&6Uö¦ö–åöVæ&ÆVEö&Æö6·5öæöåöÖVÖ&W"‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÆVgB" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢ÖW76vU÷–ÆöBÒ6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•²ÓÐ¢6VÆbæ76W'D–â‚-Š‹Š}¸ÂŠ}‹=Š­˜Š}Šý˜rŠ}‹"‹ŠŠ}Š¢Š}ŠŠ­ŠýŠr‹‹m˜‚ªŠ}˜mŠ}˜B‹M˜¸ÍŠò"ÂÖW76vU÷–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöâævWB‚&6ÆÆ&6µöFF"¢f÷"&÷r–âÖW76vU÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢W&Ç2Ò°¢'WGFöâævWB‚'W&Â"¢f÷"&÷r–âÖW76vU÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â‚&6†V6µöÖVÖ&W'6†—"Â6ÆÆ&6µ÷fÇVW2¢6VÆbæ76W'D–â‚&‡GG3¢ò÷BæÖR÷gå÷7F÷&Uö6†ææVÂ"ÂW&Ç2¢6VÆbæ76W'DfÇ6R†ç’‚-Ší‹¸ÍŠò‹=‹˜¸Í‹2"–â–ÆöE²'FW‡B%Òf÷"–ÆöB–â6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2’’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eö6†V6µöÖVÖ&W'6†—ö6ÆÆ&6µ÷6†÷w5öÖVçUögFW%ö¦ö–â‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÖVÖ&W"" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚&6†V6µöÖVÖ&W'6†—"Â6ÆÆ&6µö–CÒ&ÖVÖ&W'6†—Ö6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢ÖW76vU÷–ÆöBÒ6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•²ÓÐ¢6VÆbæ76W'D–â‚-Š}‹"˜]˜m˜¸Â‹-¸Í‹Š}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"ÂÖW76vU÷–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–âÖW76vU÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â‚'W6W#¦'W’"Â6ÆÆ&6µ÷fÇVW2 ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eöf÷&6Uö¦ö–åöFÖ–åö'—75÷6¶—5öÖVÖ&W'6†—ö6†V6²‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÆVgB" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"ÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"Âf—'7EöæÖSÒ$FÖ–â"¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R†ç’†6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"övWD6†DÖVÖ&W""’f÷"6ÆÂ–â÷7Eö6ÆÇ2’¢6VÆbæ76W'D–â‚-Ší‹¸ÍŠò‹=‹˜¸Í‹2"Â6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•²ÓÕ²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eöf÷&6Uö¦ö–åö–çfÆ–E÷6WGF–æw5ö&Æö6·5÷v—F†÷WEö7&6†–ær‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ""Â–çf—FUöÆ–æ³Ò""¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÖVÖ&W"" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦÷&FW'2"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R†ç’†6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"övWD6†DÖVÖ&W""’f÷"6ÆÂ–â÷7Eö6ÆÇ2’¢ÖW76vU÷–ÆöBÒ6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•²ÓÐ¢6VÆbæ76W'D–â‚-Š‹Š}¸ÂŠ}‹=Š­˜Š}Šý˜rŠ}‹"‹ŠŠ}Š¢Š}ŠŠ­ŠýŠr‹‹m˜‚ªŠ}˜mŠ}˜B‹M˜¸ÍŠò"ÂÖW76vU÷–ÆöE²'FW‡B%Ò¢'WGFöç2Ò°¢'WGFöà¢f÷"&÷r–âÖW76vU÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'DWVÂ…¶'WGFöâævWB‚&6ÆÆ&6µöFF"’f÷"'WGFöâ–â'WGFöç5ÒÂ²&6†V6µöÖVÖ&W'6†—%Ò¢6VÆbæ76W'DfÇ6R†ç’‚'W&Â"–â'WGFöâf÷"'WGFöâ–â'WGFöç2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eöf÷&6Uö¦ö–å÷FVÆVw&Õö•öf–ÇW&Uö&Æö6·5÷v—F†÷WEö7&6†–ær‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B€¢÷7Eö6ÆÇ2À¢•öf–ÇW&UöFW67&—F–öãÒ$f÷&&–FFVã¢&÷B—2æ÷BFÖ–â"À¢ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦÷&FW'2"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢ÖW76vU÷–ÆöBÒ6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•²ÓÐ¢6VÆbæ76W'D–â‚-Š‹Š}¸ÂŠ}‹=Š­˜Š}Šý˜rŠ}‹"‹ŠŠ}Š¢Š}ŠŠ­ŠýŠr‹‹m˜‚ªŠ}˜mŠ}˜B‹M˜¸ÍŠò"ÂÖW76vU÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'EG'VR†ç’†6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"övWD6†DÖVÖ&W""’f÷"6ÆÂ–â÷7Eö6ÆÇ2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uöÆöö·Wö6ÆÆ&6µ÷&ö×G5öf÷%ö6öæf–uöÆ–æ²‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒæ&÷G2–×÷'B$õEõ5DDUô4ôäd”uôÄôôµUõt•EôÄ”ä° ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6öæf–uöÆöö·W"Â6ÆÆ&6µö–CÒ&Æöö·WÖ6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ$õEõ5DDUô4ôäd”uôÄôôµUõt•EôÄ”ä²¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-˜M¸Í˜mª’ªŠ}˜m˜¸ÍªòŠí˜Šò‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠò"Â–ÆöE²'FW‡B%Ò¢'WGFöå÷FW‡G2Ò°¢'WGFöå²'FW‡B%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'DWVÂ†'WGFöå÷FW‡G2Â²-˜M‹­˜‚"Â-ŠŠ}‹-ªý‹MŠ¢Š˜r˜]˜m˜‚%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eö6öæf–uöÆöö·Wöf÷&6Uö¦ö–åöwV&Eö&Æö6·5öæöåöÖVÖ&W"‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÆVgB" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6öæf–uöÆöö·W"Â6ÆÆ&6µö–CÒ&Æöö·WÖ6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR¢6VçE÷FW‡G2Ò·–ÆöE²'FW‡B%Òf÷"–ÆöB–â6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•Ð¢6VÆbæ76W'EG'VR†ç’‚-Š‹Š}¸ÂŠ}‹=Š­˜Š}Šý˜rŠ}‹"‹ŠŠ}Š¢Š}ŠŠ­ŠýŠr‹‹m˜‚ªŠ}˜mŠ}˜B‹M˜¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'DfÇ6R†ç’‚-˜M¸Í˜mª’ªŠ}˜m˜¸ÍªòŠí˜Šò‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eög&VU÷G&–Åö6ÆÆ&6µ÷6†÷w5÷&Wf–Wu÷v†VåöVæ&ÆVB‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbæVæ&ÆUög&VU÷G&–Â‚ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦g&VU÷G&–Â"Â6ÆÆ&6µö–CÒ'G&–ÂÖ6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Šý‹¸ÍŠ}˜Š¢Š­‹=Š¢‹Š}¸ÍªýŠ}˜b"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-ŠÝŠÍ˜RŠ­‹=Š£¢»ªý¸ÍªýŠ}ŠŠ}¸ÍŠ¢"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-˜]ŠýŠ¢Š}‹Š­ŠŠ}‹¢»-»B‹=Š}‹Š¢"Â–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'DWVÂ†6ÆÆ&6µ÷fÇVW2Â²'W6W#¦g&VU÷G&–Åö6öæf—&Ò"Â'W6W#¦g&VU÷G&–Åö6æ6VÂ%Ò ¢F6‚‚'7F÷&Ræg&VU÷G&–Å÷6W'f–6W2æ7&VFU÷G&–Åö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#CSCSCSCRÓCSCRÓCSCRÓƒSCRÓCSCSCSCSCSCR"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eög&VU÷G&–Åö6öæf—&Õö7&VFW5ö6öæf–uöæE÷6VæG5öÆ–æ²‡6VÆbÂ÷7EöÖö6²Â‡V•öÖö6²“ ¢6VÆbæVæ&ÆUög&VU÷G&–Â‚ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦g&VU÷G&–Åö6öæf—&Ò"Â6ÆÆ&6µö–CÒ'G&–ÂÖ6öæf—&Ò"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢G&–Å÷&WVW7BÒg&VUG&–Å&WVW7Bæö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ‡G&–Å÷&WVW7Bç7FGW2Âg&VUG&–Å&WVW7Bå7FGW2äDTÄ•dU$TB¢6VÆbæ76W'DWVÂ‡G&–Å÷&WVW7BçFVÆVw&Õ÷W6W%ö–BÂ#C""¢6VÆbæ76W'D—4æ÷DæöæR‡G&–Å÷&WVW7Bæ7W7FöÖW"¢6VÆbæ76W'D—4æ÷DæöæR‡G&–Å÷&WVW7Bçgåö6Æ–VçB¢6VÆbæ76W'DWVÂ‡G&–Å÷&WVW7Bçgåö6Æ–VçBç7FGW2Âeä6Æ–VçBå7FGW2ä5D•dR¢g&öÒæ&÷E÷F&vWG2–×÷'BvWE÷gåö6Æ–VçE÷FVÆVw&Õ÷F&vWG0 ¢F&vWG2ÒvWE÷gåö6Æ–VçE÷FVÆVw&Õ÷F&vWG2‡G&–Å÷&WVW7Bçgåö6Æ–VçBÂ7F÷&S×6VÆbç7F÷&R¢6VÆbæ76W'DWVÂ†ÆVâ‡F&vWG2’Â¢6VÆbæ76W'DWVÂ‡F&vWG5³Òæ6†Eö–BÂ#C""¢–ÆöG2Ò°¢6ÆÂæ·v&w5²&§6öâ%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢æB'FW‡B"–â6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò¢Ð¢6VÆbæ76W'EG'VR†ç’‚-Š­‹=Š¢‹Š}¸ÍªýŠ}˜b‹M˜]ŠrŠ-˜]Š}Šý˜r‹MŠò"–â–ÆöE²'FW‡B%Òf÷"–ÆöB–â–ÆöG2’¢6öæf–u÷–ÆöBÒæW‡B‡–ÆöBf÷"–ÆöB–â–ÆöG2–b'fÆW73¢òöW†×ÆR"–â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'DWVÂ†6öæf–u÷–ÆöE²''6UöÖöFR%ÒÂ$…DÔÂ"¢6VÆbæ76W'D–â‚#Ç&Sâ"Â6öæf–u÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚&‡GG3¢òöW†×ÆRæ6öÒ÷7V"÷7V##2"Â6öæf–u÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚/	ùIr˜M¸Í˜mª’Š}‹MŠ­‹Š}ª’"Â6öæf–u÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚.)ª˜M¸Í˜mª’˜]‹=Š­˜-¸Í˜R"Â6öæf–u÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'EG'VR€¢ç’€¢'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ'fÆW73¢òöW†×ÆR ¢f÷"&÷r–â6öæf–u÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢¢¢6VÆbæ76W'EG'VR€¢ç’€¢'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ&‡GG3¢òöW†×ÆRæ6öÒ÷7V"÷7V##2 ¢f÷"&÷r–â6öæf–u÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢¢¢‡V•öÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚€¢'7F÷&Ræg&VU÷G&–Å÷6W'f–6W2æ7&VFU÷G&–Åö6Æ–VçEöFWF–Ç2"À¢&WGW&å÷fÇVS×°¢¢¦f¶Uö6Æ–VçE÷&W7VÇB‚#CcCcCcCbÓCcCbÓCcCbÓƒcCbÓCcCcCcCcCcCb"’À¢'7V%öÆ–æ²#¢&‡GG3¢ò÷6&wV&BæW†×ÆRæ6öÒ÷7V"÷&—fFR×Fö¶Vâ"À¢&F—&V7EöÆ–æ²#¢'fÆW73¢ò÷6&wV&BÖF—&V7BæW†×ÆRæ6öÒ"À¢'&r#¢²&fÖ–Ç’#¢'6&wV&B"Â&æF—fU÷&uöFVÆ—fW'’#¢G'VRÂ'7V'67&—F–öå÷W&Å÷6fVB#¢G'VWÒÀ¢ÒÀ¢¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eög&VU÷G&–Åö6öæf—&Õö†–FW5÷6&wV&EöæF—fU÷7V'67&—F–öåöÆ–æ²‡6VÆbÂ÷7EöÖö6²ÂG&–ÅöÖö6²“ ¢6VÆbæVæ&ÆUög&VU÷G&–Â‚ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦g&VU÷G&–Åö6öæf—&Ò"Â6ÆÆ&6µö–CÒ'G&–ÂÖ6öæf—&Ò×r"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢G&–Å÷&WVW7BÒg&VUG&–Å&WVW7Bæö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ‡G&–Å÷&WVW7Bç7FGW2Âg&VUG&–Å&WVW7Bå7FGW2äDTÄ•dU$TB¢–ÆöG2Ò°¢6ÆÂæ·v&w5²&§6öâ%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢æB'FW‡B"–â6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò¢Ð¢6öæf–u÷–ÆöBÒæW‡B‡–ÆöBf÷"–ÆöB–â–ÆöG2–b'fÆW73¢ò÷6&wV&BÖF—&V7BæW†×ÆRæ6öÒ"–â–ÆöE²'FW‡B%Ò¢&VæFW&VE÷–ÆöBÒ§6öâæGV×2†6öæf–u÷–ÆöBÂVç7W&Uö66–“ÔfÇ6R¢6VÆbæ76W'D–â‚.)ª˜M¸Í˜mª’˜]‹=Š­˜-¸Í˜R"Â6öæf–u÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚/	ùIr˜M¸Í˜mª’Š}‹MŠ­‹Š}ª’"Â6öæf–u÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚&‡GG3¢ò÷6&wV&BæW†×ÆRæ6öÒ÷7V"÷&—fFR×Fö¶Vâ"Â&VæFW&VE÷–ÆöB¢6VÆbæ76W'DfÇ6R€¢ç’€¢'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ&‡GG3¢ò÷6&wV&BæW†×ÆRæ6öÒ÷7V"÷&—fFR×Fö¶Vâ ¢f÷"&÷r–â6öæf–u÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢¢¢G&–ÅöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&Ræg&VU÷G&–Å÷6W'f–6W2æ7&VFU÷G&–Åö6Æ–VçEöFWF–Ç2"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eög&VU÷G&–Åöf÷&6Uö¦ö–åöwV&Eö&Æö6·5öæöåöÖVÖ&W"‡6VÆbÂ÷7EöÖö6²Â‡V•öÖö6²“ ¢6VÆbæVæ&ÆUög&VU÷G&–Â‚¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÆVgB" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦g&VU÷G&–Â"Â6ÆÆ&6µö–CÒ'G&–ÂÖ6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VçE÷FW‡G2Ò·–ÆöE²'FW‡B%Òf÷"–ÆöB–â6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•Ð¢6VÆbæ76W'EG'VR†ç’‚-Š‹Š}¸ÂŠ}‹=Š­˜Š}Šý˜rŠ}‹"‹ŠŠ}Š¢Š}ŠŠ­ŠýŠr‹‹m˜‚ªŠ}˜mŠ}˜B‹M˜¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'DfÇ6R†ç’‚-Šý‹¸ÍŠ}˜Š¢Š­‹=Š¢‹Š}¸ÍªýŠ}˜b"–âFW‡BæB-ŠÝŠÍ˜RŠ­‹=Š¢"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'DfÇ6R„g&VUG&–Å&WVW7Bæö&¦V7G2æW†—7G2‚’¢‡V•öÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Ræ&÷G2æ6†V6µö6öæf–u÷W6vR"Â&WGW&å÷fÇVS×²&f÷VæB#¢fÇ6RÂ&ÖW76vR#¢-Š}¸Í˜bªŠ}˜m˜¸ÍªòŠý‹›í˜m˜N(Í˜}Š}¸Â˜]Šr›í¸ÍŠýŠr˜m‹MŠòâ'Ò¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uöÆöö·W÷&FUöÆ–Ö—Eö&Æö6·5÷6—‡F…öGFV×B‡6VÆbÂ÷7EöÖö6²Â6†V6µöÖö6²“ ¢6Æ–VçEö–BÒ#ÓÓCÓƒÓ  ¢f÷"–æFW‚–â&ævRƒb“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6öæf–uöÆöö·W"Â6ÆÆ&6µö–CÖb&Æöö·W×¶–æFW‡Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†6Æ–VçEö–BÂÖW76vUö–CÓ#²–æFW‚’ ¢6VÆbæ76W'DWVÂ†6†V6µöÖö6²æ6ÆÅö6÷VçBÂR¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Š­‹ŠýŠ}ŠòŠý‹Ší˜Š}‹=Š®(Í˜}Š}¸ÂŠ‹‹‹=¸Â‹M˜]Šr‹-¸ÍŠ}Šò‹MŠý˜r"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ6öæf–uöÆöö·Wæf–æEö6Æ–VçEö'•ö–FVçF–f–W""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uöÆöö·W÷&W7öç6UöæEöÆöw5öFõöæ÷Eö–æ6ÇVFUögVÆÅö6öæf–uöÆ–æ²‡6VÆbÂ÷7EöÖö6²Âf–æFW%öÖö6²“ ¢6Æ–VçEö–BÒ#ÓÓCÓƒÓ ¢gVÆÅöÆ–æ²Òb'fÆW73¢ò÷¶6Æ–VçEö–GÔgâæW†×ÆRæ6öÓ£CC3÷G—S×F7g6V7W&—G“ÖæöæR7&—fFR×&VÖ&² ¢F÷FÂÒ3¢ƒ#B¢¢2¢W6VBÒ–çBƒ"ãR¢ƒ#B¢¢2’¢f–æFW%öÖö6²ç&WGW&å÷fÇVRÒ°¢'æVÂ#¢6VÆbçæVÂÀ¢&–æ&÷VæB#¢6VÆbæ–æ&÷VæBÀ¢'&÷Fö6öÂ#¢'fÆW72"À¢&6Æ–VçB#¢²&–B#¢6Æ–VçEö–BÂ&VÖ–Â#¢&Æ–6Uö6öæf–r"Â'&VÖ&²#¢$Æ–6R'ÒÀ¢&6Æ–VçE÷7FG2#¢²&VÖ–Â#¢&Æ–6Uö6öæf–r'ÒÀ¢'F÷FÅ÷G&ff–5ö'—FW2#¢F÷FÂÀ¢'W6VE÷G&ff–5ö'—FW2#¢W6VBÀ¢'W6VE÷WÆöEö'—FW2#¢W6VBÀ¢'W6VEöF÷væÆöEö'—FW2#¢À¢'&VÖ–æ–æu÷G&ff–5ö'—FW2#¢F÷FÂÒW6VBÀ¢&W‡—'•öB#¢F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó"’À¢&Æ7EööæÆ–æUöB#¢F–ÖW¦öæRææ÷r‚’À¢&—5öVæ&ÆVB#¢G'VRÀ¢Ð ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6öæf–uöÆöö·W"Â6ÆÆ&6µö–CÒ&Æöö·WÖ6""’¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†gVÆÅöÆ–æ²ÂÖW76vUö–CÓ#"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚/	ù8¢˜‹m‹¸ÍŠ¢ªŠ}˜m˜¸Íªò‹M˜]Šr"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-ŠŠ}˜-¸Î(Í˜]Š}˜mŠý˜s¢»»rí»Rªý¸Íªò"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â†gVÆÅöÆ–æ²Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚'fÆW73¢òò"Â–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢WFFUö6ÆÆ&6²ÒæW‡B‡fÇVRf÷"fÇVR–â6ÆÆ&6µ÷fÇVW2–bfÇVRç7F'G7v—F‚‚'W6W#¦6öæf–uöÆöö·W÷WFFS¢"’¢6VÆbæ76W'Dæ÷D–â†gVÆÅöÆ–æ²ÂWFFUö6ÆÆ&6²¢6VÆbæ76W'Dæ÷D–â†6Æ–VçEö–BÂWFFUö6ÆÆ&6²¢ÆövvVE÷–ÆöG2Ò%Æâ"æ¦ö–â€¢§6öâæGV×2†WfVçBç&u÷–ÆöBÂVç7W&Uö66–“ÔfÇ6R¢f÷"WfVçB–â&÷DWfVçDÆöræö&¦V7G2æÆÂ‚¢¢6VÆbæ76W'Dæ÷D–â†gVÆÅöÆ–æ²ÂÆövvVE÷–ÆöG2¢6VÆbæ76W'Dæ÷D–â‚'fÆW73¢òò"ÂÆövvVE÷–ÆöG2¢6VÆbæ76W'D–â‚#Æ6öæf–rÖÆ–æ²×&VF7FVCâ"ÂÆövvVE÷–ÆöG2 ¢FVb6VæEöÆöö·WöæEövWE÷WFFUö6ÆÆ&6²‡6VÆbÂ÷7EöÖö6²Â¢Â6Æ–VçEö–CÒ#ÓÓCÓƒÓ"“ ¢gVÆÅöÆ–æ²Òb'fÆW73¢ò÷¶6Æ–VçEö–GÔgâæW†×ÆRæ6öÓ£CC3÷G—S×F7g6V7W&—G“ÖæöæR7&—fFR×&VÖ&² ¢F÷FÂÒ3¢ƒ#B¢¢2¢v—F‚F6‚€¢'7F÷&Ræ&÷G2æ6†V6µö6öæf–u÷W6vR"À¢&WGW&å÷fÇVS×°¢&f÷VæB#¢G'VRÀ¢&ÖW76vR#¢/	ù8¢˜‹m‹¸ÍŠ¢ªŠ}˜m˜¸Íªò‹M˜]ŠuÆåÆí˜]‹]‹˜(Í‹MŠý˜s¢»ªý¸Íªò"À¢'æVÂ#¢6VÆbçæVÂÀ¢'æVÅö–B#¢6VÆbçæVÂç²À¢&–æ&÷VæB#¢6VÆbæ–æ&÷VæBÀ¢&–æ&÷VæEö–B#¢6VÆbæ–æ&÷VæBæ–æ&÷VæEö–BÀ¢&–FVçF–f–W"#¢6Æ–VçEö–BÀ¢'&÷Fö6öÂ#¢'fÆW72"À¢&VÖ–Â#¢&Æ–6Uö6öæf–r"À¢'F÷FÅö'—FW2#¢F÷FÂÀ¢'W6VEö'—FW2#¢#B¢¢2À¢'&VÖ–æ–æuö'—FW2#¢F÷FÂÒƒ#B¢¢2’À¢ÒÀ¢“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6öæf–uöÆöö·W"Â6ÆÆ&6µö–CÒ&Æöö·WÖ6""’¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†gVÆÅöÆ–æ²ÂÖW76vUö–CÓ#"’ ¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢&WGW&âæW‡B‡fÇVRf÷"fÇVR–â6ÆÆ&6µ÷fÇVW2–bfÇVRç7F'G7v—F‚‚'W6W#¦6öæf–uöÆöö·W÷WFFS¢"’ ¢FVb6VæEöFÖ–åöÆöö·WöæEövWEö6ÆÆ&6·2‡6VÆbÂ÷7EöÖö6²Â¢Â6Æ–VçEö–CÒ#ÓÓCÓƒÓ"“ ¢gVÆÅöÆ–æ²Òb'fÆW73¢ò÷¶6Æ–VçEö–GÔgâæW†×ÆRæ6öÓ£CC3÷G—S×F7g6V7W&—G“ÖæöæR7&—fFR×&VÖ&² ¢F÷FÂÒ3¢ƒ#B¢¢2¢v—F‚F6‚€¢'7F÷&Ræ&÷G2æ6†V6µö6öæf–u÷W6vR"À¢&WGW&å÷fÇVS×°¢&f÷VæB#¢G'VRÀ¢&ÖW76vR#¢/	ù8¢˜‹m‹¸ÍŠ¢ªŠ}˜m˜¸Íªò‹M˜]ŠuÆåÆí˜]‹]‹˜(Í‹MŠý˜s¢»ªý¸Íªò"À¢'æVÂ#¢6VÆbçæVÂÀ¢'æVÅö–B#¢6VÆbçæVÂç²À¢'æVÅöæÖR#¢6VÆbçæVÂææÖRÀ¢&–æ&÷VæB#¢6VÆbæ–æ&÷VæBÀ¢&–æ&÷VæEö–B#¢6VÆbæ–æ&÷VæBæ–æ&÷VæEö–BÀ¢&–æ&÷VæE÷&VÖ&²#¢6VÆbæ–æ&÷VæBç&VÖ&²À¢&–FVçF–f–W"#¢6Æ–VçEö–BÀ¢&Ö6¶VEö–FVçF–f–W"#¢#ââã"À¢'&÷Fö6öÂ#¢'fÆW72"À¢&VÖ–Â#¢&Æ–6Uö6öæf–r"À¢&Væ&ÆVB#¢G'VRÀ¢'F÷FÅö'—FW2#¢F÷FÂÀ¢'W6VEö'—FW2#¢#B¢¢2À¢'&VÖ–æ–æuö'—FW2#¢F÷FÂÒƒ#B¢¢2’À¢ÒÀ¢“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6öæf–uöÆöö·W"Â6ÆÆ&6µö–CÒ&FÖ–âÖÆöö·WÖ6""ÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"’¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR†gVÆÅöÆ–æ²ÂÖW76vUö–CÓ#2ÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"Âf—'7EöæÖSÒ$FÖ–â"’ ¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢&WGW&â6ÆÆ&6·0 ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöFÖ–åö6öæf–uöÆöö·W÷6†÷w5öÖævVÖVçEö'WGFöç5÷v—F†÷WEö–FVçF–f–W%öÆV²‡6VÆbÂ÷7EöÖö6²“ ¢6Æ–VçEö–BÒ#ÓÓCÓƒÓ ¢6ÆÆ&6·2Ò6VÆbç6VæEöFÖ–åöÆöö·WöæEövWEö6ÆÆ&6·2‡÷7EöÖö6²Â6Æ–VçEö–CÖ6Æ–VçEö–B ¢6VÆbæ76W'EG'VR†ç’‡fÇVRç7F'G7v—F‚‚&FÖ–ã¦6öæf–uöFVÆWFS¢"’f÷"fÇVR–â6ÆÆ&6·2’¢6VÆbæ76W'EG'VR†ç’‡fÇVRç7F'G7v—F‚‚&FÖ–ã¦6öæf–uöVF—E÷G&ff–3¢"’f÷"fÇVR–â6ÆÆ&6·2’¢6VÆbæ76W'EG'VR†ç’‡fÇVRç7F'G7v—F‚‚&FÖ–ã¦6öæf–uöVF—EöW‡—'“¢"’f÷"fÇVR–â6ÆÆ&6·2’¢6VÆbæ76W'EG'VR†ç’‡fÇVRç7F'G7v—F‚‚&FÖ–ã¦6öæf–u÷&Vg&W6…öÆ–æ³¢"’f÷"fÇVR–â6ÆÆ&6·2’¢6VÆbæ76W'DfÇ6R†ç’‡fÇVRç7F'G7v—F‚‚'W6W#¦6öæf–uöÆöö·W÷WFFS¢"’f÷"fÇVR–â6ÆÆ&6·2’¢f÷"6ÆÆ&6µöFF–â6ÆÆ&6·3 ¢6VÆbæ76W'Dæ÷D–â†6Æ–VçEö–BÂ6ÆÆ&6µöFF¢6VÆbæ76W'Dæ÷D–â‚'fÆW73¢òò"Â6ÆÆ&6µöFF ¢F6‚‚'7F÷&Ræ&÷G2æFVÆWFU÷gåö6Æ–VçEö'•öFÖ–åöÆöö·W"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöFÖ–åö6öæf–uöFVÆWFUö6öæf—&ÖF–öåö6ÆÇ5÷6W'f–6R‡6VÆbÂ÷7EöÖö6²ÂFVÆWFUöÖö6²“ ¢6ÆÆ&6·2Ò6VÆbç6VæEöFÖ–åöÆöö·WöæEövWEö6ÆÆ&6·2‡÷7EöÖö6²¢FVÆWFUö6ÆÆ&6²ÒæW‡B‡fÇVRf÷"fÇVR–â6ÆÆ&6·2–bfÇVRç7F'G7v—F‚‚&FÖ–ã¦6öæf–uöFVÆWFS¢"’¢FVÆWFUöÖö6²ç&WGW&å÷fÇVRÒ²'7V66W72#¢G'VRÂ&Æö6ÅöÖF6…÷7FGW2#¢&æ÷Eöf÷VæB'Ð ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†FVÆWFUö6ÆÆ&6²Â6ÆÆ&6µö–CÒ&FÖ–âÖFVÆWFR"ÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"’¢6öæf—&Õ÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚.)ªûˆòŠÝ‹˜ªŠ}˜m˜¸ÍªòŠ}‹"›í˜m˜B"Â6öæf—&Õ÷–ÆöE²'FW‡B%Ò¢6öæf—&Õö6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â6öæf—&Õ÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6öæf—&Õö6ÆÆ&6²ÒæW‡B‡fÇVRf÷"fÇVR–â6öæf—&Õö6ÆÆ&6·2–bfÇVRç7F'G7v—F‚‚&FÖ–ã¦6öæf–uöFVÆWFUö6öæf—&Ó¢"’ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²†6öæf—&Õö6ÆÆ&6²Â6ÆÆ&6µö–CÒ&FÖ–âÖFVÆWFRÖ6öæf—&Ò"ÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢FVÆWFUöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢6VÆbæ76W'DWVÂ†FVÆWFUöÖö6²æ6ÆÅö&w2æ&w5³ÒÂ#““’"¢6VÆbæ76W'D–â‚.)ÈRªŠ}˜m˜¸ÍªòŠ}‹"›í˜m˜BŠÝ‹˜‹MŠò"Â÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2æ'V–ÆEö6öæf–uöÆ–æµöf÷%ö–FVçF–f–W""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uöÆöö·W÷WFFUö6ÆÆ&6µ÷6VæG5÷WFFVEöÆ–æµ÷v—F†÷WEöÆV¶–æuö6ÆÆ&6²‡6VÆbÂ÷7EöÖö6²Â'V–ÆFW%öÖö6²“ ¢6Æ–VçEö–BÒ#ÓÓCÓƒÓ ¢WFFVEöÆ–æ²Òb'fÆW73¢ò÷¶6Æ–VçEö–GÔæWræW†×ÆRæ6öÓ£CC3÷G—S×w2f†÷7CÖæWræW†×ÆRæ6öÒ4Æ–6R ¢6ÆÆ&6µöFFÒ6VÆbç6VæEöÆöö·WöæEövWE÷WFFUö6ÆÆ&6²‡÷7EöÖö6²Â6Æ–VçEö–CÖ6Æ–VçEö–B¢'V–ÆFW%öÖö6²ç&WGW&å÷fÇVRÒ°¢'WFFVEö6öæf–uöÆ–æ²#¢WFFVEöÆ–æ²À¢'&÷Fö6öÂ#¢'fÆW72"À¢'&VÖ&²#¢$Æ–6R"À¢&VÖ–Â#¢&Æ–6Uö6öæf–r"À¢Ð ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†6ÆÆ&6µöFFÂ6ÆÆ&6µö–CÒ'WFFRÖ6""ÂÖW76vUö–CÓ32’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢'V–ÆFW%öÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢òÂ–æ&÷VæEö–BÂ–FVçF–f–W"Ò'V–ÆFW%öÖö6²æ6ÆÅö&w2æ&w0¢6VÆbæ76W'DWVÂ†–æ&÷VæEö–BÂ6VÆbæ–æ&÷VæBæ–æ&÷VæEö–B¢6VÆbæ76W'DWVÂ†–FVçF–f–W"Â6Æ–VçEö–B¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‡WFFVEöÆ–æ²ç&WÆ6R‚"b"Â"f×²"’Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'DWVÂ‡–ÆöE²''6UöÖöFR%ÒÂ$…DÔÂ"¢6VÆbæ76W'EG'VR€¢ç’€¢'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒWFFVEöÆ–æ°¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢¢¢6VÆbæ76W'Dæ÷D–â‡WFFVEöÆ–æ²Â6ÆÆ&6µöFF¢6VÆbæ76W'Dæ÷D–â†6Æ–VçEö–BÂ6ÆÆ&6µöFF¢ÆövvVE÷–ÆöG2Ò%Æâ"æ¦ö–â€¢§6öâæGV×2†WfVçBç&u÷–ÆöBÂVç7W&Uö66–“ÔfÇ6R¢f÷"WfVçB–â&÷DWfVçDÆöræö&¦V7G2æÆÂ‚¢¢6VÆbæ76W'Dæ÷D–â‡WFFVEöÆ–æ²ÂÆövvVE÷–ÆöG2¢6VÆbæ76W'Dæ÷D–â‚&æWræW†×ÆRæ6öÒ"ÂÆövvVE÷–ÆöG2 ¢F6‚‚'7F÷&Ræ&÷G2æ'V–ÆEö6öæf–uöÆ–æµöf÷%ö–FVçF–f–W""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uöÆöö·W÷WFFUö6ÆÆ&6µ÷&W÷'G5öæõ÷WFFU÷v†VåöÆ–æµ÷Væ6†ævVB‡6VÆbÂ÷7EöÖö6²Â'V–ÆFW%öÖö6²“ ¢6Æ–VçEö–BÒ#ÓÓCÓƒÓ ¢Væ6†ævVEöÆ–æ²Òb'fÆW73¢ò÷¶6Æ–VçEö–GÔgâæW†×ÆRæ6öÓ£CC3÷G—S×F7g6V7W&—G“ÖæöæR7&VæÖVBÖöæÇ’ ¢6ÆÆ&6µöFFÒ6VÆbç6VæEöÆöö·WöæEövWE÷WFFUö6ÆÆ&6²‡÷7EöÖö6²Â6Æ–VçEö–CÖ6Æ–VçEö–B¢'V–ÆFW%öÖö6²ç&WGW&å÷fÇVRÒ°¢'WFFVEö6öæf–uöÆ–æ²#¢Væ6†ævVEöÆ–æ²À¢'&÷Fö6öÂ#¢'fÆW72"À¢'&VÖ&²#¢$Æ–6R"À¢&VÖ–Â#¢&Æ–6Uö6öæf–r"À¢&Væ&ÆVB#¢G'VRÀ¢Ð ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†6ÆÆ&6µöFFÂ6ÆÆ&6µö–CÒ'WFFRÖ6""ÂÖW76vUö–CÓ32’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Š}¸Í˜bªŠ}˜m˜¸ÍªòŠ-›íŠý¸ÍŠ¢˜mŠýŠ}‹Šò"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚'fÆW73¢òò"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2æ'V–ÆEö6öæf–uöÆ–æµöf÷%ö–FVçF–f–W""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uöÆöö·W÷WFFUö6ÆÆ&6µ÷&W÷'G5öæõ÷WFFU÷v†Våö6Æ–VçEö–æ7F—fR‡6VÆbÂ÷7EöÖö6²Â'V–ÆFW%öÖö6²“ ¢6Æ–VçEö–BÒ#ÓÓCÓƒÓ ¢6ÆÆ&6µöFFÒ6VÆbç6VæEöÆöö·WöæEövWE÷WFFUö6ÆÆ&6²‡÷7EöÖö6²Â6Æ–VçEö–CÖ6Æ–VçEö–B¢'V–ÆFW%öÖö6²ç&WGW&å÷fÇVRÒ°¢'WFFVEö6öæf–uöÆ–æ²#¢b'fÆW73¢ò÷¶6Æ–VçEö–GÔ6†ævVBæW†×ÆRæ6öÓ£CC3÷G—S×w24Æ–6R"À¢'&÷Fö6öÂ#¢'fÆW72"À¢'&VÖ&²#¢$Æ–6R"À¢&VÖ–Â#¢&Æ–6Uö6öæf–r"À¢&Væ&ÆVB#¢fÇ6RÀ¢Ð ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†6ÆÆ&6µöFFÂ6ÆÆ&6µö–CÒ'WFFRÖ6""ÂÖW76vUö–CÓ32’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Š}¸Í˜bªŠ}˜m˜¸ÍªòŠ-›íŠý¸ÍŠ¢˜mŠýŠ}‹Šò"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚'fÆW73¢òò"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2æ'V–ÆEö6öæf–uöÆ–æµöf÷%ö–FVçF–f–W""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uöÆöö·W÷WFFU÷&FUöÆ–Ö—Eö&Æö6·5÷6—‡F…öGFV×B‡6VÆbÂ÷7EöÖö6²Â'V–ÆFW%öÖö6²“ ¢6ÆÆ&6µöFFÒ6VÆbç6VæEöÆöö·WöæEövWE÷WFFUö6ÆÆ&6²‡÷7EöÖö6²¢'V–ÆFW%öÖö6²ç&WGW&å÷fÇVRÒ°¢'WFFVEö6öæf–uöÆ–æ²#¢'fÆW73¢ò÷WFFVBæW†×ÆR"À¢'&VÖ&²#¢$Æ–6R"À¢Ð ¢f÷"–æFW‚–â&ævRƒb“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†6ÆÆ&6µöFFÂ6ÆÆ&6µö–CÖb'WFFR×¶–æFW‡Ò"ÂÖW76vUö–CÓC²–æFW‚’ ¢6VÆbæ76W'DWVÂ†'V–ÆFW%öÖö6²æ6ÆÅö6÷VçBÂR¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Š­‹ŠýŠ}ŠòŠý‹Ší˜Š}‹=Š®(Í˜}Š}¸ÂŠ-›íŠý¸ÍŠ¢ªŠ}˜m˜¸Íªò‹-¸ÍŠ}Šò‹MŠý˜r"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2æ'V–ÆEö6öæf–uöÆ–æµöf÷%ö–FVçF–f–W""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7Eö6öæf–uöÆöö·W÷WFFUöf÷&6Uö¦ö–åöwV&Eö&Æö6·5öæöåöÖVÖ&W"‡6VÆbÂ÷7EöÖö6²Â'V–ÆFW%öÖö6²“ ¢6VÆbæVæ&ÆUöf÷&6Uö¦ö–â‡W6W&æÖSÒ'gå÷7F÷&Uö6†ææVÂ"¢÷7Eö6ÆÇ2ÒµÐ¢÷7EöÖö6²ç6–FUöVffV7BÒ6VÆbæÖVÖ&W'6†—÷÷7E÷6–FUöVffV7B‡÷7Eö6ÆÇ2Â7FGW3Ò&ÆVgB" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6öæf–uöÆöö·W÷WFFS§6fR×Fö¶Vâ"Â6ÆÆ&6µö–CÒ'WFFRÖ6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R†'V–ÆFW%öÖö6²æ6ÆÆVB¢6VçE÷FW‡G2Ò·–ÆöE²'FW‡B%Òf÷"–ÆöB–â6VÆbç6VçEöÖW76vU÷–ÆöG2‡÷7Eö6ÆÇ2•Ð¢6VÆbæ76W'EG'VR†ç’‚-Š‹Š}¸ÂŠ}‹=Š­˜Š}Šý˜rŠ}‹"‹ŠŠ}Š¢Š}ŠŠ­ŠýŠr‹‹m˜‚ªŠ}˜mŠ}˜B‹M˜¸ÍŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢÷fW'&–FU÷6WGF–æw2…DTÄTu$Õô$õEõU4U$äÔSÒ&¦FæWE÷FW7Eö&÷B"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷&VfW'&ÅöÖVçUöF—7Æ—5ö–çf—FU÷7FG2‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§&VfW'&Ç2"Â6ÆÆ&6µö–CÒ'&VbÖ6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Šý‹˜Š¢Šý˜‹=Š­Š}˜b"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-ªŠòŠý‹˜Š¢"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚&‡GG3¢ò÷BæÖRö¦FæWE÷FW7Eö&÷C÷7F'C×&Veò"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-˜]Š­˜bŠ-˜]Š}Šý˜rŠý‹˜Š¢"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-Š‹=Š­˜~(Í˜}Š}¸ÂŠ-˜]Š}Šý˜rŠý‹¸ÍŠ}˜Š¢"Â–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢–b&6ÆÆ&6µöFF"–â'WGFöà¢Ð¢6VÆbæ76W'D–â‚'W6W#§&VfW'&Åö–çf—FU÷FW‡B"Â6ÆÆ&6µ÷fÇVW2¢6†&U÷W&Ç2Ò°¢'WGFöå²'W&Â%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢–b'W&Â"–â'WGFöà¢Ð¢6VÆbæ76W'EG'VR†ç’‡W&Âç7F'G7v—F‚‚&‡GG3¢ò÷BæÖR÷6†&R÷W&Ãò"’f÷"W&Â–â6†&U÷W&Ç2’ ¢F6‚æF–7B‚&÷2æVçf—&öâ"Â²%DTÄTu$Õô$õEõU4U$äÔR#¢"'Ò¢÷fW'&–FU÷6WGF–æw2…DTÄTu$Õô$õEõU4U$äÔSÒ""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷&VfW'&ÅöÖVçU÷6†÷w5öÖ—76–æuö&÷E÷W6W&æÖUöÖW76vR‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§&VfW'&Ç2"Â6ÆÆ&6µö–CÒ'&VbÖÖ—76–ærÖ6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-˜mŠ}˜RªŠ}‹Š‹¸Â‹ŠŠ}Š¢Š­˜m‹¸Í˜R˜m‹MŠý˜rŠ}‹=Š¢â"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚&‡GG3¢ò÷BæÖRó÷7F'C×&Veò"Â–ÆöE²'FW‡B%Ò ¢÷fW'&–FU÷6WGF–æw2…DTÄTu$Õô$õEõU4U$äÔSÒ&¦FæWE÷FW7Eö&÷B"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷&VfW'&Åö–çf—FU÷FW‡Eö6ÆÆ&6µ÷6VæG5÷&W&VE÷FW‡B‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§&VfW'&Åö–çf—FU÷FW‡B"Â6ÆÆ&6µö–CÒ'&Vb×FW‡BÖ6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-˜]˜bŠ}‹"Š}¸Í˜b‹ŠŠ}Š¢eâªý‹˜Š­˜R"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚&‡GG3¢ò÷BæÖRö¦FæWE÷FW7Eö&÷C÷7F'C×&Veò"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚''6UöÖöFR"Â–ÆöB ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6W%÷&öf–ÆUö66WG5÷FVÆVw&Õö6öçF7E÷†öæR‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§&öf–ÆU÷†öæR"Â6ÆÆ&6µö–CÒ'&öf–ÆR×†öæR"’ ¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRå$ôd”ÄUõt•Eõ„ôäR ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6öçF7EöÖW76vR‚"³“ƒ“##3CScr"ÂÖW76vUö–CÓ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢&÷E÷W6W"æ7W7FöÖW"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR¢6VÆbæ76W'DWVÂ†&÷E÷W6W"æ7W7FöÖW"ç†öæUöçVÖ&W"Â#“##3CScr"¢6VçE÷FW‡G2Ò°¢6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C""æB'FW‡B"–â6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò¢Ð¢6VÆbæ76W'EG'VR†ç’‚-‹M˜]Š}‹˜r˜]˜ŠŠ}¸Í˜BŠý‹›í‹˜˜Š}¸Í˜B‹M˜]Šr‹Ší¸Í‹˜r‹MŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'EG'VR†ç’‚-›í‹˜˜Š}¸Í˜B‹M˜]Šr"–âFW‡BæB#“##3CScr"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"¢FVbFW7E÷W6W%ö6ÆÆ&6µöFVÆWFW5÷&Wf–÷W5ö–æÆ–æUöÖW76vUö&Vf÷&UöæW‡E÷&ö×B‡6VÆbÂ÷7EöÖö6²“ ¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ¢¦·v&w7Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢÷7EöÖö6²ç6–FUöVffV7BÒ÷7E÷6–FUöVffV7@ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"ÂÖW76vUö–CÓsr’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢ÖWF†öG2Ò¶6ÆÅ²'W&Â%Òç'7Æ—B‚"ò"Â•²ÓÒf÷"6ÆÂ–â÷7Eö6ÆÇ5Ð¢6VÆbæ76W'D–â‚&ç7vW$6ÆÆ&6µVW'’"ÂÖWF†öG2¢6VÆbæ76W'D–â‚&FVÆWFTÖW76vR"ÂÖWF†öG2¢6VÆbæ76W'D–â‚'6VæDÖW76vR"ÂÖWF†öG2¢FVÆWFUö–æFW‚ÒÖWF†öG2æ–æFW‚‚&FVÆWFTÖW76vR"¢æW‡E÷&ö×Eö–æFW‚ÒæW‡B€¢–æFW€¢f÷"–æFW‚Â6ÆÂ–âVçVÖW&FR‡÷7Eö6ÆÇ2¢–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"’æB-Ší‹¸ÍŠò‹=‹˜¸Í‹2"–â6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚'FW‡B"Â""¢¢6VÆbæ76W'DÆW72†FVÆWFUö–æFW‚ÂæW‡E÷&ö×Eö–æFW‚¢FVÆWFU÷–ÆöBÒ÷7Eö6ÆÇ5¶FVÆWFUö–æFW…Õ²&§6öâ%Ð¢6VÆbæ76W'DWVÂ†FVÆWFU÷–ÆöE²&6†Eö–B%ÒÂ#C""¢6VÆbæ76W'DWVÂ†FVÆWFU÷–ÆöE²&ÖW76vUö–B%ÒÂsr ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷GVææVÅ÷ÆåöÆ—7E÷FW‡Eö—5÷6–×ÆUöæEö'WGFöç5ö†fU÷ÆåöFWF–Ç2‡6VÆbÂ÷7EöÖö6²“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Ší‹¸ÍŠò‹=‹˜¸Í‹2"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-¸Íª¸ÂŠ}‹"›í˜M˜n(Í˜}Š}¸Â‹-¸Í‹‹ŠrŠ}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‡6VÆbçÆâææÖRÂ–ÆöE²'FW‡B%Ò¢Æåö'WGFöç2Ò°¢'WGFöà¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢–b'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’ç7F'G7v—F‚‚'W6W#¦'W—Æã¢"¢Ð¢6VÆbæ76W'EG'VR‡Æåö'WGFöç2¢6VÆbæ76W'D–â‚-»ªý¸ÍªýŠ}ŠŠ}¸ÍŠ¢"ÂÆåö'WGFöç5³Õ²'FW‡B%Ò¢6VÆbæ76W'D–â‚-»=»‹˜‹-˜r"ÂÆåö'WGFöç5³Õ²'FW‡B%Ò¢6VÆbæ76W'D–â‚-»»»Í»»»Š­˜˜]Š}˜b"ÂÆåö'WGFöç5³Õ²'FW‡B%Ò ¢÷fW'&–FU÷6WGF–æw2„$õEô4õ•õDU…EôD•4$ÄTCÕG'VR¢FVbFW7Eö6÷•÷FW‡Eö'WGFöåöfÆÇ5ö&6µ÷v†Våö6÷•÷FW‡Eö—5öF—6&ÆVB‡6VÆb“ ¢g&öÒæ&÷G2–×÷'B'V–ÆEö6÷•÷FW‡Eö'WGFöà ¢'WGFöâÒ'V–ÆEö6÷•÷FW‡Eö'WGFöâ€¢-ª›í¸Â˜]Š˜M‹¢"À¢##S"À¢6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢fÆÆ&6µö6ÆÆ&6µöFFÒ'W6W#¦6÷“§–ÖVçEöÖ÷VçB"À¢ ¢6VÆbæ76W'DWVÂ†'WGFöå²'FW‡B%ÒÂ-˜m˜]Š}¸Í‹B˜]Š˜M‹¢"¢6VÆbæ76W'DWVÂ†'WGFöå²&6ÆÆ&6µöFF%ÒÂ'W6W#¦6÷“§–ÖVçEöÖ÷VçB"¢6VÆbæ76W'Dæ÷D–â‚&6÷•÷FW‡B"Â'WGFöâ ¢FVbFW7Eö6÷•÷FW‡Eö'WGFöå÷6†÷'EöæEöÆöæuöfÆÆ&6µö&V†f–÷W"‡6VÆb“ ¢g&öÒæ&÷G2–×÷'B'V–ÆEö6÷•÷FW‡Eö'WGFöà ¢6†÷'Eö'WGFöâÒ'V–ÆEö6÷•÷FW‡Eö'WGFöâ‚-ª›í¸Â"Â'6†÷'B×fÇVR"Â6öæf–s×6VÆbæ&÷Eö6öæf–r¢ÆöæuöÆ–æ²Ò'fÆW73¢òò"²‚&"¢3¢fÆÆ&6µö'WGFöâÒ'V–ÆEö6÷•÷FW‡Eö'WGFöâ€¢-ª›í¸Â˜M¸Í˜mª’"À¢ÆöæuöÆ–æ²À¢6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢fÆÆ&6µö6ÆÆ&6µöFFÒ'W6W#¦6÷•ö6öæf–s¦F—&V7C§6fR×Fö¶Vâ"À¢ ¢6VÆbæ76W'DWVÂ‡6†÷'Eö'WGFöå²&6÷•÷FW‡B%Õ²'FW‡B%ÒÂ'6†÷'B×fÇVR"¢6VÆbæ76W'DWVÂ†fÆÆ&6µö'WGFöå²&6ÆÆ&6µöFF%ÒÂ'W6W#¦6÷•ö6öæf–s¦F—&V7C§6fR×Fö¶Vâ"¢6VÆbæ76W'Dæ÷D–â‚&6÷•÷FW‡B"ÂfÆÆ&6µö'WGFöâ¢6VÆbæ76W'Dæ÷D–â†ÆöæuöÆ–æ²ÂfÆÆ&6µö'WGFöå²&6ÆÆ&6µöFF%Ò ¢FVbFW7E÷–ÖVçEö¶W–&ö&Eö6öçF–ç5ööæÇ•ö6÷•ö&6µöæEö6æ6VÅö7F–öç2‡6VÆb“ ¢g&öÒæ&÷G2–×÷'B'V–ÆE÷–ÖVçEö¶W–&ö&@ ¢¶W–&ö&BÒ'V–ÆE÷–ÖVçEö¶W–&ö&B‚#"Â3Â6öæf–s×6VÆbæ&÷Eö6öæf–r¢&÷w2Ò¶W–&ö&E²&–æÆ–æUö¶W–&ö&B%Ð¢6VÆbæ76W'DWVÂ€¢µ¶'WGFöå²'FW‡B%Òf÷"'WGFöâ–â&÷uÒf÷"&÷r–â&÷w5ÒÀ¢µ²-ª›í¸Â‹M˜]Š}‹˜rªŠ}‹Š¢"Â-ª›í¸Â˜]Š˜M‹¢%ÒÂ²-Š‹ªý‹MŠ¢%ÒÂ²-˜M‹­˜‚%ÕÒÀ¢ ¢'WGFöç2Ò¶'WGFöâf÷"&÷r–â&÷w2f÷"'WGFöâ–â&÷uÐ¢FW‡G2Ò¶'WGFöå²'FW‡B%Òf÷"'WGFöâ–â'WGFöç5Ð¢6ÆÆ&6·2Ò¶'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’f÷"'WGFöâ–â'WGFöç5Ð¢6VÆbæ76W'Dæ÷D–â‚-Š}‹‹=Š}˜B‹‹=¸ÍŠò"ÂFW‡G2¢6VÆbæ76W'Dæ÷D–â‚-Š­‹¸Í¸Í˜b˜mŠ}˜RªŠ}˜m˜¸Íªò"ÂFW‡G2¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçEöæÖS§7F'B"Â6ÆÆ&6·2¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçE÷&V6V—EööæÇ’"Â6ÆÆ&6·2¢6VÆbæ76W'Dæ÷D–â‚-Š}‹‹=Š}˜B‹‹=¸ÍŠòª‹Šý˜Rò‹Š}˜}˜m˜]Šr"ÂFW‡G2¢6VÆbæ76W'Dæ÷D–â‚-‹Š}˜}˜m˜]Š}¸Â›í‹ŠýŠ}ŠíŠ¢)Ù2"ÂFW‡G2¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçE÷&V6V—Eö†VÇ"Â6ÆÆ&6·2¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ#"f÷"'WGFöâ–â'WGFöç2’¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ#3"f÷"'WGFöâ–â'WGFöç2’ ¢FVbFW7Eöf÷&ÖGF–æuöæEö¶W–&ö&Eö†VÇW'5÷&VÖ–åö–×÷'Eö6ö×F–&ÆUög&öÕ÷7F÷&Uö&÷G2‡6VÆb“ ¢g&öÒæ&÷G2–×÷'B€¢'V–ÆE÷–ÖVçEö¶W–&ö&BÀ¢f÷&ÖEö6&Eöf÷%ö6÷’À¢f÷&ÖEöÖöæW•öf÷%ö6÷’À¢6æ—F—¦Uö&÷EöWfVçEöÆöu÷fÇVRÀ¢FVÆVw&Õö6öFRÀ¢ ¢6VÆbæ76W'DWVÂ†f÷&ÖEöÖöæW•öf÷%ö6÷’„FV6–ÖÂ‚#"’’Â#"¢6VÆbæ76W'DWVÂ†f÷&ÖEö6&Eöf÷%ö6÷’‚-»»»»»»»»»»»»»»»»"’Â#"¢¶W–&ö&BÒ'V–ÆE÷–ÖVçEö¶W–&ö&B‚-»»»»»»»»»»»»»»»»"ÂFV6–ÖÂ‚#"’Â6öæf–s×6VÆbæ&÷Eö6öæf–r¢'WGFöç2Ò¶'WGFöâf÷"&÷r–â¶W–&ö&E²&–æÆ–æUö¶W–&ö&B%Òf÷"'WGFöâ–â&÷uÐ¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ#"f÷"'WGFöâ–â'WGFöç2’¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ#"f÷"'WGFöâ–â'WGFöç2’¢6VÆbæ76W'DWVÂ€¢FVÆVw&Õö6öFR‚'fÆW73¢òö†÷7B÷FƒöÓf#ÓÇFsâ"Â&Æö6³ÕG'VR’À¢#Ç&SçfÆW73¢òö†÷7B÷FƒöÓf×¶#ÒfÇC·FrfwC³Â÷&Sâ"À¢¢6VÆbæ76W'DWVÂ€¢6æ—F—¦Uö&÷EöWfVçEöÆöu÷fÇVR‡²&Æ–æµõ4T5$UB#¢²'G&ö¦ã¢ò÷6V7&WDW†×ÆRæ6öÒ%×Ò’À¢²&Æ–æµóÇ&VF7FVCâ#¢²#Æ6öæf–rÖÆ–æ²×&VF7FVCâ%×ÒÀ¢ ¢FVbFW7Eö6öæf–uöFVÆ—fW'•öæE÷–ÖVçEö†VÇW'5÷&VÖ–åö–×÷'Eö6ö×F–&ÆUög&öÕ÷7F÷&Uö&÷G2‡6VÆb“ ¢g&öÒâ–×÷'B&÷G0¢g&öÒçFVÆVw&Õö&÷B–×÷'B6öæf–uöFVÆ—fW'’Â–ÖVçG2Â6W'f–6W5öfÆ÷rÂW6W%öÖVçP ¢6VÆbæ76W'D—2†&÷G2ç6VæEö6öæf–uöÆ–æ·5öÖW76vRÂ6öæf–uöFVÆ—fW'’ç6VæEö6öæf–uöÆ–æ·5öÖW76vR¢6VÆbæ76W'D—2†&÷G2ç6VæEö6÷–&ÆUö6öæf–uöÖW76vRÂ6öæf–uöFVÆ—fW'’ç6VæEö6÷–&ÆUö6öæf–uöÖW76vR¢6VÆbæ76W'D—2†&÷G2æ†æFÆUö6öæf–uö6÷•ö6ÆÆ&6²Â6öæf–uöFVÆ—fW'’æ†æFÆUö6öæf–uö6÷•ö6ÆÆ&6²¢6VÆbæ76W'D—2†&÷G2æÖ–åöÖVçUö¶W–&ö&BÂW6W%öÖVçRæÖ–åöÖVçUö¶W–&ö&B¢6VÆbæ76W'D—2†&÷G2æ†VÇ÷FW‡BÂW6W%öÖVçRæ†VÇ÷FW‡B¢6VÆbæ76W'D—2†&÷G2ç&öf–ÆUö¶W–&ö&BÂW6W%öÖVçRç&öf–ÆUö¶W–&ö&B¢6VÆbæ76W'D—2†&÷G2æf÷&ÖE÷&öf–ÆRÂW6W%öÖVçRæf÷&ÖE÷&öf–ÆR¢6VÆbæ76W'D—2†&÷G2ç6VæE÷&öf–ÆRÂW6W%öÖVçRç6VæE÷&öf–ÆR¢6VÆbæ76W'D—2†&÷G2æ&÷Eö6Æ–VçEöÆ&VÂÂ6W'f–6W5öfÆ÷ræ&÷Eö6Æ–VçEöÆ&VÂ¢6VÆbæ76W'D—2†&÷G2æ&÷Eö6Æ–VçE÷7FGW2Â6W'f–6W5öfÆ÷ræ&÷Eö6Æ–VçE÷7FGW2¢6VÆbæ76W'D—2†&÷G2æ&÷E÷7V'67&—F–öåö6Æ–VçG2Â6W'f–6W5öfÆ÷ræ&÷E÷7V'67&—F–öåö6Æ–VçG2¢6VÆbæ76W'D—2†&÷G2ç7V'67&—F–öåöÖævVÖVçEö¶W–&ö&BÂ6W'f–6W5öfÆ÷rç7V'67&—F–öåöÖævVÖVçEö¶W–&ö&B¢6VÆbæ76W'D—2†&÷G2æ6Æ–VçEö6öæf–uö¶W–&ö&BÂ6W'f–6W5öfÆ÷ræ6Æ–VçEö6öæf–uö¶W–&ö&B¢6VÆbæ76W'D—2†&÷G2æ6Æ–VçEö6öæf–uöÆ–æ·2Â6W'f–6W5öfÆ÷ræ6Æ–VçEö6öæf–uöÆ–æ·2¢6VÆbæ76W'D—2†&÷G2çW6W%ö6Æ–VçEöFVÆWFUö'WGFöâÂ6W'f–6W5öfÆ÷rçW6W%ö6Æ–VçEöFVÆWFUö'WGFöâ¢6VÆbæ76W'D—2†&÷G2çW6W%ö6Æ–VçEöFVÆWFUö6öæf—&ÖF–öåö¶W–&ö&BÂ6W'f–6W5öfÆ÷rçW6W%ö6Æ–VçEöFVÆWFUö6öæf—&ÖF–öåö¶W–&ö&B¢6VÆbæ76W'D—2†&÷G2ç7F÷&U÷–ÖVçEöÆ–æW2Â–ÖVçG2ç7F÷&U÷–ÖVçEöÆ–æW2¢6VÆbæ76W'D—2†&÷G2æf÷&ÖE÷–ÖVçE÷&ö×BÂ–ÖVçG2æf÷&ÖE÷–ÖVçE÷&ö×B¢6VÆbæ76W'D—2†&÷G2æ&÷E÷–ÖVçE÷6VæFW%öæÖRÂ–ÖVçG2æ&÷E÷–ÖVçE÷6VæFW%öæÖR¢6VÆbæ76W'D—2†&÷G2ç–ÖVçE÷7FWö¶W–&ö&BÂ–ÖVçG2ç–ÖVçE÷7FWö¶W–&ö&B¢6VÆbæ76W'D—2†&÷G2æ÷F–öæÅö6öæf–uöæÖUö¶W–&ö&BÂ–ÖVçG2æ÷F–öæÅö6öæf–uöæÖUö¶W–&ö&B¢6VÆbæ76W'D—2†&÷G2æ&÷Eö÷&FW%öÖWFFFÂ–ÖVçG2æ&÷Eö÷&FW%öÖWFFF¢6VÆbæ76W'D—2†&÷G2æW‡G&7E÷&V6V—Eöf–ÆRÂ–ÖVçG2æW‡G&7E÷&V6V—Eöf–ÆR¢6VÆbæ76W'D—2†&÷G2ç&V6V—Eöf–ÆU÷G—UöW'&÷"Â–ÖVçG2ç&V6V—Eöf–ÆU÷G—UöW'&÷"¢6VÆbæ76W'D—2†&÷G2ç6fU÷&V6V—Eöf–ÆVæÖRÂ–ÖVçG2ç6fU÷&V6V—Eöf–ÆVæÖR¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2æ6÷•÷–ÖVçE÷fÇVUög&öÕ÷7FFR’¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2æGF6…ö&÷E÷&V6V—B’¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2æF÷væÆöE÷&V6V—Eö6öçFVçB’¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2ç6VæEöÖ–åöÖVçR’¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2æ7F—fU÷7V'67&—F–öåöÆ–æW2’¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2æf÷&ÖEö6Æ–VçEö6öæf–r’¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2ç6VæEö6Æ–VçEö6öæf–uöÖW76vW2’¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2ç7F'E÷W6W%ö6Æ–VçEöFVÆWFUöfÆ÷r’¢6VÆbæ76W'EG'VR†6ÆÆ&ÆR†&÷G2æ6öæf—&Õ÷W6W%ö6Æ–VçEöFVÆWFUöfÆ÷r’ ¢FVbFW7E÷–ÖVçE÷7FFUö†VÇW'5ö¶VWöW†—7F–æuö÷WGWG2‡6VÆb“ ¢g&öÒæ&÷G2–×÷'B€¢&÷Eö÷&FW%öÖWFFFÀ¢&÷E÷–ÖVçE÷6VæFW%öæÖRÀ¢6÷•÷–ÖVçE÷fÇVUög&öÕ÷7FFRÀ¢÷F–öæÅö6öæf–uöæÖUö¶W–&ö&BÀ¢–ÖVçE÷7FWö¶W–&ö&BÀ¢ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R'W–W""À¢7FFUöFF×²'Æåö–B#¢6VÆbçÆâç²Â'VçF—G’#¢"Â'6VæFW%ö6&EöæÖR#¢%v÷&²ÆF÷'ÒÀ¢ ¢6VÆbæ76W'DWVÂ†&÷E÷–ÖVçE÷6VæFW%öæÖR†&÷E÷W6W"’Â%v÷&²ÆF÷"¢&÷E÷W6W"ç7FFUöFFÒ²'Æåö–B#¢6VÆbçÆâç²Â'VçF—G’#¢'Ð¢&÷E÷W6W"ç6fR‡WFFUöf–VÆG3Õ²'7FFUöFF"Â'WFFVEöB%Ò ¢6VÆbæ76W'DWVÂ†6÷•÷–ÖVçE÷fÇVUög&öÕ÷7FFR‡6VÆbæ&÷Eö6öæf–rÂ&÷E÷W6W"Â'–ÖVçEö6&B"’Â#"¢6VÆbæ76W'DWVÂ†6÷•÷–ÖVçE÷fÇVUög&öÕ÷7FFR‡6VÆbæ&÷Eö6öæf–rÂ&÷E÷W6W"Â'–ÖVçEöÖ÷VçB"’Â##" ¢–ÖVçEö¶W–&ö&BÒ–ÖVçE÷7FWö¶W–&ö&B‡6VÆbç7F÷&Ræ6&EöçVÖ&W"ÂFV6–ÖÂ‚##"’Â6öæf–s×6VÆbæ&÷Eö6öæf–r¢–ÖVçEö6ÆÆ&6·2Ò°¢'WGFöâævWB‚&6ÆÆ&6µöFF"Â""¢f÷"&÷r–â–ÖVçEö¶W–&ö&E²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçEöæÖS§7F'B"Â–ÖVçEö6ÆÆ&6·2¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçE÷&V6V—EööæÇ’"Â–ÖVçEö6ÆÆ&6·2¢6VÆbæ76W'D–â‚'W6W#¦'W•ö&6µ÷7VÖÖ'’"Â–ÖVçEö6ÆÆ&6·2¢6VÆbæ76W'D–â‚'W6W#¦6æ6VÂ"Â–ÖVçEö6ÆÆ&6·2¢6VÆbæ76W'EG'VR€¢ç’€¢'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ## ¢f÷"&÷r–â–ÖVçEö¶W–&ö&E²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢¢ ¢æÖUö¶W–&ö&BÒ÷F–öæÅö6öæf–uöæÖUö¶W–&ö&B‚¢æÖUö6ÆÆ&6·2Ò°¢'WGFöâævWB‚&6ÆÆ&6µöFF"Â""¢f÷"&÷r–âæÖUö¶W–&ö&E²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'DWVÂ†æÖUö6ÆÆ&6·2Â²'W6W#§–ÖVçE÷&V6V—EööæÇ’"Â'W6W#¦'W•ö&6µ÷7VÖÖ'’"Â'W6W#¦6æ6VÂ%Ò ¢ÖWFFFÒ&÷Eö÷&FW%öÖWFFF‡6VÆbæ&÷Eö6öæf–rÂ&÷E÷W6W"Â6÷W&6SÒ&&÷E÷W&6†6R"ÂW‡G&×²&fÆ÷r#¢'W&6†6R'Ò¢6VÆbæ76W'DWVÂ†ÖWFFF²'6÷W&6R%ÒÂ&&÷E÷W&6†6R"¢6VÆbæ76W'DWVÂ†ÖWFFF²&fÆ÷r%ÒÂ'W&6†6R"¢6VÆbæ76W'DWVÂ†ÖWFFF²&&÷B%Õ²'&÷f–FW%÷W6W%ö–B%ÒÂ#C""¢6VÆbæ76W'Dæ÷D–â‚&&÷E÷Fö¶Vâ"ÂÖWFFF²&&÷B%Ò¢6VÆbæ76W'Dæ÷D–â‚&6öæf–uöÆ–æ²"Â§6öâæGV×2†ÖWFFFÂVç7W&Uö66–“ÔfÇ6R’ ¢FVbFW7E÷&V6V—Eö†VÇW'5öFWFV7E÷†÷FõöæEö–ÖvUöFö7VÖVçB‡6VÆb“ ¢g&öÒæ&÷G2–×÷'BW‡G&7E÷&V6V—Eöf–ÆRÂ&V6V—Eöf–ÆU÷G—UöW'&÷  ¢†÷Fõö–æfòÒW‡G&7E÷&V6V—Eöf–ÆR€¢°¢&ÖW76vUö–B#¢À¢'†÷Fò#¢°¢²&f–ÆUö–B#¢'6ÖÆÂÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'6ÖÆÂ'ÒÀ¢²&f–ÆUö–B#¢&Æ&vRÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢&Æ&vR'ÒÀ¢ÒÀ¢Ð¢ ¢6VÆbæ76W'DWVÂ‡†÷Fõö–æfõ²&¶–æB%ÒÂ'†÷Fò"¢6VÆbæ76W'DWVÂ‡†÷Fõö–æfõ²&f–ÆUö–B%ÒÂ&Æ&vRÖf–ÆR"¢6VÆbæ76W'DWVÂ‡†÷Fõö–æfõ²&ÖW76vUö–B%ÒÂ¢6VÆbæ76W'DWVÂ‡&V6V—Eöf–ÆU÷G—UöW'&÷"‡†÷Fõö–æfò’Â"" ¢Fö7VÖVçEö–æfòÒW‡G&7E÷&V6V—Eöf–ÆR€¢°¢&ÖW76vT–B#¢"À¢&Fö7VÖVçB#¢°¢&f–ÆT–B#¢&Fö2Öf–ÆR"À¢&f–ÆUVæ—VT–B#¢&Fö2×Væ—VR"À¢&f–ÆTæÖR#¢'&V6V—Båär"À¢&Ö–ÖUG—R#¢&–ÖvR÷ær"À¢ÒÀ¢Ð¢ ¢6VÆbæ76W'DWVÂ†Fö7VÖVçEö–æfõ²&¶–æB%ÒÂ&Fö7VÖVçB"¢6VÆbæ76W'DWVÂ†Fö7VÖVçEö–æfõ²&f–ÆUö–B%ÒÂ&Fö2Öf–ÆR"¢6VÆbæ76W'DWVÂ†Fö7VÖVçEö–æfõ²&f–ÆU÷Væ—VUö–B%ÒÂ&Fö2×Væ—VR"¢6VÆbæ76W'DWVÂ†Fö7VÖVçEö–æfõ²&f–ÆUöæÖR%ÒÂ'&V6V—Båär"¢6VÆbæ76W'DWVÂ†Fö7VÖVçEö–æfõ²&Ö–ÖU÷G—R%ÒÂ&–ÖvR÷ær"¢6VÆbæ76W'DWVÂ†Fö7VÖVçEö–æfõ²&ÖW76vUö–B%ÒÂ"¢6VÆbæ76W'DWVÂ‡&V6V—Eöf–ÆU÷G—UöW'&÷"†Fö7VÖVçEö–æfò’Â"" ¢FW‡EöFö7VÖVçBÒW‡G&7E÷&V6V—Eöf–ÆR€¢°¢&Fö7VÖVçB#¢°¢&f–ÆUö–B#¢'FW‡BÖf–ÆR"À¢&f–ÆUöæÖR#¢'&V6V—BçG‡B"À¢&Ö–ÖU÷G—R#¢'FW‡B÷Æ–â"À¢ÒÀ¢Ð¢¢6VÆbæ76W'D–â‚-˜Š}¸Í˜B‹‹=¸ÍŠòŠŠ}¸ÍŠòŠ­‹]˜¸Í‹"Â&V6V—Eöf–ÆU÷G—UöW'&÷"‡FW‡EöFö7VÖVçB’ ¢÷fW'&–FU÷6WGF–æw2„$õEô4õ•õDU…EôD•4$ÄTCÕG'VR¢FVbFW7E÷–ÖVçEö¶W–&ö&Eö6÷•öfÆÆ&6·5öFõöæ÷Eö–æ6ÇVFUö†VÇö6ÆÆ&6·2‡6VÆb“ ¢g&öÒæ&÷G2–×÷'B'V–ÆE÷–ÖVçEö¶W–&ö&@ ¢'WGFöç2Ò°¢'WGFöà¢f÷"&÷r–â'V–ÆE÷–ÖVçEö¶W–&ö&B‚#"Â3Â6öæf–s×6VÆbæ&÷Eö6öæf–r•²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð ¢6ÆÆ&6·2Ò¶'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’f÷"'WGFöâ–â'WGFöç5Ð¢6VÆbæ76W'D–â‚'W6W#¦6÷“§–ÖVçEö6&B"Â6ÆÆ&6·2¢6VÆbæ76W'D–â‚'W6W#¦6÷“§–ÖVçEöÖ÷VçB"Â6ÆÆ&6·2¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçE÷&V6V—Eö†VÇ"Â6ÆÆ&6·2¢6VÆbæ76W'DfÇ6R†ç’‚-‹Š}˜}˜m˜]Šr"–â'WGFöâævWB‚'FW‡B"Â""’f÷"'WGFöâ–â'WGFöç2’ ¢FVbFW7E÷–ÖVçE÷FW‡E÷W6W5÷&WV—&VEöÆ&VÇ5öæEö†–FW5öV×G•ö÷F–öæÅö&æµöf–VÆG2‡6VÆb“ ¢g&öÒæ&÷G2–×÷'B7F÷&U÷–ÖVçEöÆ–æW0 ¢6VÆbç7F÷&Rç6†V&öçVÖ&W"Ò$•##3CScsƒ“ ¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'6†V&öçVÖ&W""Â'WFFVEöB%Ò ¢FW‡BÒ7F÷&U÷–ÖVçEöÆ–æW2‡6VÆbç7F÷&RÂ6VÆbçÆâ ¢6VÆbæ76W'D–â‚-˜]Š˜M‹¢˜-Š}Š˜B›í‹ŠýŠ}ŠíŠ£¢"ÂFW‡B¢6VÆbæ76W'D–â‚#Æ6öFSãÂö6öFSâ"ÂFW‡B¢6VÆbæ76W'D–â‚-‹M˜]Š}‹˜rªŠ}‹Š£¢"ÂFW‡B¢6VÆbæ76W'D–â‚#Æ6öFSãÂö6öFSâ"ÂFW‡B¢6VÆbæ76W'D–â‚-Š˜r˜mŠ}˜S¢"ÂFW‡B¢6VÆbæ76W'D–â‚-ŠŠ}˜mª“¢FW7B&æ²"ÂFW‡B¢6VÆbæ76W'D–â‚-‹MŠŠs¢Æ6öFSä•##3CScsƒ“Âö6öFSâ"ÂFW‡B ¢6VÆbç7F÷&Ræ&æµöæÖRÒ" ¢6VÆbç7F÷&Rç6†V&öçVÖ&W"Ò" ¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²&&æµöæÖR"Â'6†V&öçVÖ&W""Â'WFFVEöB%Ò ¢FW‡BÒ7F÷&U÷–ÖVçEöÆ–æW2‡6VÆbç7F÷&RÂ6VÆbçÆâ ¢6VÆbæ76W'Dæ÷D–â‚-ŠŠ}˜mª“¢"ÂFW‡B¢6VÆbæ76W'Dæ÷D–â‚-‹MŠŠs¢"ÂFW‡B ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6÷–&ÆUö6öæf–uöÖW76vU÷W6W5ö‡FÖÅ÷&UöæEö6÷•ö'WGFöåöf÷%÷6†÷'EöÆ–æ·2‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒæ&÷G2–×÷'B&÷D6Æ–VçBÂ6VæEö6÷–&ÆUö6öæf–uöÖW76vP ¢Æ–æ²Ò'fÆW73¢òö&5öFVdW†×ÆRæ6öÓ£CC3÷G—S×w2g6V7W&—G“×FÇ24Æ–6RÓ ¢6VæEö6÷–&ÆUö6öæf–uöÖW76vR„&÷D6Æ–VçB‡6VÆbæ&÷Eö6öæf–r’Â#C""ÂÆ–æ²ÂF—FÆSÒ.)ÈRŠ­‹=Š¢ªŠ}˜m˜¸Íªò" ¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'DWVÂ‡–ÆöE²''6UöÖöFR%ÒÂ$…DÔÂ"¢6VÆbæ76W'D–â‚#Ç&Sâ"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚.)ª˜M¸Í˜mª’˜]‹=Š­˜-¸Í˜R"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚'G—S×w2f×·6V7W&—G“×FÇ2"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'EG'VR€¢ç’€¢'WGFöâævWB‚'FW‡B"’ÓÒ-ª›í¸Â˜M¸Í˜mª’˜]‹=Š­˜-¸Í˜R)ª ¢æB'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒÆ–æ°¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢¢ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöÆöæuö6öæf–uöÖW76vU÷W6W5÷Fö¶Væ—¦VEöfÆÆ&6µö6ÆÆ&6²‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒæ&÷G2–×÷'B&÷D6Æ–VçBÂ6VæEö6÷–&ÆUö6öæf–uöÖW76vP ¢ÆöæuöÆ–æ²Ò'fÖW73¢òò"²‚&"¢3¢6VæEö6÷–&ÆUö6öæf–uöÖW76vR„&÷D6Æ–VçB‡6VÆbæ&÷Eö6öæf–r’Â#C""ÂÆöæuöÆ–æ² ¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚#Ç&Sâ"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'DfÇ6R€¢ç’€¢&6÷•÷FW‡B"–â'WGFöà¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢¢¢fÆÆ&6µö6ÆÆ&6·2Ò°¢'WGFöâævWB‚&6ÆÆ&6µöFF"Â""¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢–b'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’ç7F'G7v—F‚‚'W6W#¦6÷•ö6öæf–s¦F—&V7C¢"¢Ð¢6VÆbæ76W'DWVÂ†ÆVâ†fÆÆ&6µö6ÆÆ&6·2’Â¢6VÆbæ76W'Dæ÷D–â†ÆöæuöÆ–æ²ÂfÆÆ&6µö6ÆÆ&6·5³Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uöÆ–æ·5öÖW76vUö6öÖ&–æW5÷7V'67&—F–öåöæEöF—&V7EöÆ–æ·2‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒæ&÷G2–×÷'B&÷D6Æ–VçBÂ6VæEö6öæf–uöÆ–æ·5öÖW76vP ¢7V%öÆ–æ²Ò&‡GG3¢òöW†×ÆRæ6öÒ÷7V"öÆ–6R ¢F—&V7EöÆ–æ²Ò'fÆW73¢òöÆ–6TW†×ÆRæ6öÓ£CC3÷G—S×w2g6V7W&—G“×FÇ24Æ–6R ¢6VæEö6öæf–uöÆ–æ·5öÖW76vR€¢&÷D6Æ–VçB‡6VÆbæ&÷Eö6öæf–r’À¢#C""À¢7V'67&—F–öåöÆ–æ³×7V%öÆ–æ²À¢F—&V7EöÆ–æ³ÖF—&V7EöÆ–æ²À¢F—FÆSÒ.)ÈR‹=‹˜¸Í‹2Š­‹=Š¢"À¢ ¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'DWVÂ‡–ÆöE²''6UöÖöFR%ÒÂ$…DÔÂ"¢6VÆbæ76W'D–â‚/	ùIr˜M¸Í˜mª’Š}‹MŠ­‹Š}ª’"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚.)ª˜M¸Í˜mª’˜]‹=Š­˜-¸Í˜R"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‡7V%öÆ–æ²Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚'G—S×w2f×·6V7W&—G“×FÇ2"Â–ÆöE²'FW‡B%Ò¢'WGFöç2Ò°¢'WGFöà¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚'FW‡B"’ÓÒ-ª›í¸Â˜M¸Í˜mª’Š}‹MŠ­‹Š}ª’	ùIr"æB'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ7V%öÆ–æ²f÷"'WGFöâ–â'WGFöç2’¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚'FW‡B"’ÓÒ-ª›í¸Â˜M¸Í˜mª’˜]‹=Š­˜-¸Í˜R)ª"æB'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒF—&V7EöÆ–æ²f÷"'WGFöâ–â'WGFöç2’¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚&6ÆÆ&6µöFF"’ÓÒ'W6W#§7V'2"f÷"'WGFöâ–â'WGFöç2’¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚&6ÆÆ&6µöFF"’ÓÒ'W6W#¦†VÇ"f÷"'WGFöâ–â'WGFöç2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö6öæf–uö6÷•öfÆÆ&6µö6ÆÆ&6µ÷6VæG5ö66†VEöÆ–æµ÷v—F†÷WEöFVÆWF–æu÷6÷W&6R‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒæ&÷G2–×÷'B&÷D6Æ–VçBÂ6VæEö6÷–&ÆUö6öæf–uöÖW76vP ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢ÆöæuöÆ–æ²Ò'fÖW73¢òò"²‚&""¢3¢6VæEö6÷–&ÆUö6öæf–uöÖW76vR„&÷D6Æ–VçB‡6VÆbæ&÷Eö6öæf–r’Â#C""ÂÆöæuöÆ–æ²¢6öæf–u÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6ÆÆ&6µöFFÒæW‡B€¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â6öæf–u÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢–b'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’ç7F'G7v—F‚‚'W6W#¦6÷•ö6öæf–s¦F—&V7C¢"¢¢6VÆbæ76W'Dæ÷D–â†ÆöæuöÆ–æ²Â6ÆÆ&6µöFF¢÷7EöÖö6²ç&W6WEöÖö6²‚ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†6ÆÆ&6µöFFÂÖW76vUö–CÓsrÂ6ÆÆ&6µö–CÒ&6÷’Ö6""’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢ÖWF†öG2Ò¶6ÆÂæ&w5³Òç'7Æ—B‚"ò"Â•²ÓÒf÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7EÐ¢6VÆbæ76W'D–â‚&ç7vW$6ÆÆ&6µVW'’"ÂÖWF†öG2¢6VÆbæ76W'Dæ÷D–â‚&FVÆWFTÖW76vR"ÂÖWF†öG2¢–ÆöBÒæW‡B€¢6ÆÂæ·v&w5²&§6öâ%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ&w5³ÒæVæG7v—F‚‚"÷6VæDÖW76vR"¢¢6VÆbæ76W'D–â†ÆöæuöÆ–æ²Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'DWVÂ‡–ÆöE²''6UöÖöFR%ÒÂ$…DÔÂ"¢ÆövvVE÷–ÆöG2Ò%Æâ"æ¦ö–â€¢§6öâæGV×2†WfVçBç&u÷–ÆöBÂVç7W&Uö66–“ÔfÇ6R¢f÷"WfVçB–â&÷DWfVçDÆöræö&¦V7G2æÆÂ‚¢¢6VÆbæ76W'Dæ÷D–â†ÆöæuöÆ–æ²ÂÆövvVE÷–ÆöG2 ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöW‡—&VEö6öæf–uö6÷•öfÆÆ&6µö6ÆÆ&6µ÷6†÷w5÷&WG'•öÖW76vR‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢÷7EöÖö6²ç&W6WEöÖö6²‚ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6÷•ö6öæf–s¦F—&V7C¦Ö—76–ær×Fö¶Vâ"ÂÖW76vUö–CÓsrÂ6ÆÆ&6µö–CÒ&6÷’ÖW‡—&VB"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢ÖWF†öG2Ò¶6ÆÂæ&w5³Òç'7Æ—B‚"ò"Â•²ÓÒf÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7EÐ¢6VÆbæ76W'Dæ÷D–â‚&FVÆWFTÖW76vR"ÂÖWF†öG2¢–ÆöBÒæW‡B€¢6ÆÂæ·v&w5²&§6öâ%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ&w5³ÒæVæG7v—F‚‚"÷6VæDÖW76vR"¢¢6VÆbæ76W'D–â‚-Š}¸Í˜b˜M¸Í˜mª’˜]˜m˜-‹m¸Â‹MŠý˜}ˆÂŠý˜ŠŠ}‹˜rŠ}‹"ŠŠí‹B‹=‹˜¸Í‹>(Í˜}Š}¸Â˜]˜bŠý‹¸ÍŠ}˜Š¢ª˜m¸ÍŠòâ"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W&6†6UöfÆ÷uö6·5öf÷%÷VçF—G•ögFW%÷Æâ‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’ ¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•EõTåD•E’¢VçF—G•÷&ö×BÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Š­‹ŠýŠ}ŠòªŠ}˜m˜¸Íªò"ÂVçF—G•÷&ö×E²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–âVçF—G•÷&ö×E²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â‚'W6W#¦'W—G“£2"Â6ÆÆ&6µ÷fÇVW2 ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£2"Â6ÆÆ&6µö–CÒ'G’Ö6""’ ¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•EôäÔR¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFUöFF²'VçF—G’%ÒÂ2¢–ÖVçE÷&ö×BÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢6VÆbæ76W'D–â‚-Š­‹ŠýŠ}ŠòªŠ}˜m˜¸Íªó¢»2"Â–ÖVçE÷&ö×B¢6VÆbæ76W'D–â‚-˜]Š˜M‹£¢»=»»Í»»»Š­˜˜]Š}˜b"Â–ÖVçE÷&ö×B ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚####"Ó#"ÓC#"Óƒ#"Ó#####""’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7E÷W&6†6UöfÆ÷uöÆ–W5öF—66÷VçEö6öFUö&Vf÷&U÷&V6V—B‡6VÆbÂövWEöÖö6²Â‡V•öÖö6²“ ¢F—66÷VçD6öFRæö&¦V7G2æ7&VFR€¢6öFSÒ%4dS"À¢F—66÷VçE÷G—SÔF—66÷VçD6öFRäF—66÷VçEG—Räd•„TBÀ¢fÇVSÓÀ¢¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂFFÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ&FF#¢FFÂ¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢'†÷F÷2÷&V6V—Bæ§r'×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦F—66÷VçC§7F'B"Â6ÆÆ&6µö–CÒ&F—66÷VçBÖ6""’¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚'6fS"ÂÖW76vUö–CÓ"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢2À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢·²&f–ÆUö–B#¢'&V6V—BÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'&V6V—B'ÕÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"æF—66÷VçEö6öFU÷FW‡BÂ%4dS"¢6VÆbæ76W'DWVÂ†÷&FW"æF—66÷VçEöÖ÷VçBÂ¢6VÆbæ76W'DWVÂ†÷&FW"æÖ÷VçBÂ“¢6VÆbæ76W'E&VvW‚‡‡V•öÖö6²æ6ÆÅö&w2æ·v&w5²&VÖ–Å÷&Vf—‚%ÒÂ"%æÆ–6Uõ³Ó–Öe×³‡ÒB"¢W6W%÷FW‡G2Ò°¢6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚'FW‡B"Â""¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"’æB6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢Ð¢6VÆbæ76W'EG'VR†ç’‚-ªŠòŠ­Ší˜¸Í˜4dSŠ}‹˜]Š}˜B‹MŠò"–âFW‡Bf÷"FW‡B–âW6W%÷FW‡G2’¢6VÆbæ76W'EG'VR†ç’‚-˜]Š˜M‹¢˜m˜}Š}¸Í¸Ã¢»»Í»»»Š­˜˜]Š}˜b"–âFW‡Bf÷"FW‡B–âW6W%÷FW‡G2’ ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#3332Ó32ÓC32Óƒ32Ó333332"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7E÷W&6†6UöfÆ÷uö66WG5÷&V6V—E÷†÷Fõ÷v—F†÷WEö6öæf–uöæÖR‡6VÆbÂövWEöÖö6²Â÷‡V•öÖö6²“ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂFFÔæöæRÂ¢¦·v&w2“ ¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢'†÷F÷2÷&V6V—Bæ§r'×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢2À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢·²&f–ÆUö–B#¢'&V6V—BÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'&V6V—B'ÕÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöæÖRÂ$Æ–6R"¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ¢6VÆbæ76W'EG'VR†÷&FW"ç–ÖVçE÷&V6V—Eö–ÖvRææÖRæVæG7v—F‚‚"æ§r"’¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#CCCBÓCBÓCCBÓƒCBÓCCCCCB"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7E÷W&6†6UöfÆ÷uö6å÷W6Uö÷F–öæÅö6öæf–uöæÖUö&Vf÷&U÷&V6V—B‡6VÆbÂövWEöÖö6²Â÷‡V•öÖö6²“ ¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂFFÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ&FF#¢FFÂ¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢'†÷F÷2÷&V6V—Bæ§r'×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§–ÖVçEöæÖS§7F'B"Â6ÆÆ&6µö–CÒ&æÖRÖ6""’¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•EôäÔR¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFUöFF²'7FW%ÒÂ&6öæf–uöæÖR" ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚%v÷&²ÆF÷"ÂÖW76vUö–CÓ"’¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B¢æÖU÷&ö×BÒ÷7Eö6ÆÇ5²ÓÕ²&§6öâ%Õ²'FW‡B%Ð¢6VÆbæ76W'D–â‚-˜]Š˜M‹¢˜-Š}Š˜B›í‹ŠýŠ}ŠíŠ£¢"ÂæÖU÷&ö×B¢6VÆbæ76W'D–â‚#Æ6öFSãÂö6öFSâ"ÂæÖU÷&ö×B¢6VÆbæ76W'D–â‚-‹M˜]Š}‹˜rªŠ}‹Š£¢"ÂæÖU÷&ö×B¢6VÆbæ76W'D–â‚#Æ6öFSãÂö6öFSâ"ÂæÖU÷&ö×B¢6VÆbæ76W'D–â‚-˜mŠ}˜RªŠ}˜m˜¸ÍªòŠ½ŠŠ¢‹MŠò"ÂæÖU÷&ö×B¢6VÆbæ76W'D–â‚-Š­‹]˜¸Í‹‹‹=¸ÍŠò›í‹ŠýŠ}ŠíŠ¢"ÂæÖU÷&ö×B ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢2À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢·²&f–ÆUö–B#¢'&V6V—BÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'&V6V—B'ÕÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöæÖRÂ%v÷&²ÆF÷"¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W&6†6UöfÆ÷uö6å÷6¶—öF—66÷VçE÷Fõ÷–ÖVçB‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦F—66÷VçC§6¶—"Â6ÆÆ&6µö–CÒ'6¶—ÖF—66÷VçB"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFUöFF²'7FW%ÒÂ'&V6V—B"¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-›í‹ŠýŠ}ŠíŠ¢ªŠ}‹Š®(ÍŠ˜~(ÍªŠ}‹Š¢"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-˜]Š˜M‹¢˜-Š}Š˜B›í‹ŠýŠ}ŠíŠ£¢"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚#Æ6öFSãÂö6öFSâ"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-‹M˜]Š}‹˜rªŠ}‹Š£¢"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚#Æ6öFSãÂö6öFSâ"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-Š˜r˜mŠ}˜S¢"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-ŠŠ}˜mª“¢FW7B&æ²"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚-‹MŠŠs¢"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-Š‹ŠòŠ}‹"›í‹ŠýŠ}ŠíŠ­ˆÂ‹ª‹2‹‹=¸ÍŠò‹Šr˜}˜]¸Í˜mŠÍŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòâ"Â–ÆöE²'FW‡B%Ò¢'WGFöç2Ò°¢'WGFöà¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'DWVÂ€¢¶'WGFöå²'FW‡B%Òf÷"'WGFöâ–â'WGFöç5ÒÀ¢²-ª›í¸Â‹M˜]Š}‹˜rªŠ}‹Š¢"Â-ª›í¸Â˜]Š˜M‹¢"Â-Š‹ªý‹MŠ¢"Â-˜M‹­˜‚%ÒÀ¢¢6VÆbæ76W'Dæ÷D–â‚-Š}‹‹=Š}˜B‹‹=¸ÍŠò"Â¶'WGFöå²'FW‡B%Òf÷"'WGFöâ–â'WGFöç5Ò¢6VÆbæ76W'Dæ÷D–â‚-Š­‹¸Í¸Í˜b˜mŠ}˜RªŠ}˜m˜¸Íªò"Â¶'WGFöå²'FW‡B%Òf÷"'WGFöâ–â'WGFöç5Ò¢6VÆbæ76W'Dæ÷D–â‚-Š}‹‹=Š}˜B‹‹=¸ÍŠòª‹Šý˜Rò‹Š}˜}˜m˜]Šr"Â¶'WGFöå²'FW‡B%Òf÷"'WGFöâ–â'WGFöç5Ò¢6VÆbæ76W'Dæ÷D–â‚-‹Š}˜}˜m˜]Š}¸Â›í‹ŠýŠ}ŠíŠ¢)Ù2"Â¶'WGFöå²'FW‡B%Òf÷"'WGFöâ–â'WGFöç5Ò¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçE÷&V6V—Eö†VÇ"Â¶'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’f÷"'WGFöâ–â'WGFöç5Ò¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçEöæÖS§7F'B"Â¶'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’f÷"'WGFöâ–â'WGFöç5Ò¢6VÆbæ76W'Dæ÷D–â‚'W6W#§–ÖVçE÷&V6V—EööæÇ’"Â¶'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’f÷"'WGFöâ–â'WGFöç5Ò¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ#"f÷"'WGFöâ–â'WGFöç2’¢6VÆbæ76W'EG'VR†ç’†'WGFöâævWB‚&6÷•÷FW‡B"Â·Ò’ævWB‚'FW‡B"’ÓÒ#"f÷"'WGFöâ–â'WGFöç2’ ¢÷fW'&–FU÷6WGF–æw2„$õEô4õ•õDU…EôD•4$ÄTCÕG'VR¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷–ÖVçEö6÷•öfÆÆ&6µö6ÆÆ&6·5ö¶VW÷v—F–æuöf÷%÷&V6V—B‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’ ¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6÷“§–ÖVçEö6&B"Â6ÆÆ&6µö–CÒ&6÷’Ö6&B"’¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B¢6VÆbæ76W'D–â‚#Æ6öFSãÂö6öFSâ"Â÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ò ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦6÷“§–ÖVçEöÖ÷VçB"Â6ÆÆ&6µö–CÒ&6÷’ÖÖ÷VçB"’¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B¢6VÆbæ76W'D–â‚#Æ6öFSãÂö6öFSâ"Â÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ò ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚-‹‹=¸ÍŠò˜]Š­˜m¸Â˜m¸Í‹=Š¢"ÂÖW76vUö–CÓ"’¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B¢6VÆbæ76W'DWVÂ‡÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%ÒÂ-˜M‹}˜Š}˜²‹ª‹2‹‹=¸ÍŠò›í‹ŠýŠ}ŠíŠ¢‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòâ" ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷FVÆVw&Õ÷W&6†6U÷&V¦V7G5öæöåö–ÖvU÷&V6V—Eöf–ÆR‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢"À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢&Fö7VÖVçB#¢°¢&f–ÆUö–B#¢'&V6V—B×FW‡B"À¢&f–ÆU÷Væ—VUö–B#¢'&V6V—B×FW‡B"À¢&f–ÆUöæÖR#¢'&V6V—BçG‡B"À¢&Ö–ÖU÷G—R#¢'FW‡B÷Æ–â"À¢ÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DfÇ6R„÷&FW"æö&¦V7G2æW†—7G2‚’¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B¢6VÆbæ76W'D–â‚-˜Š}¸Í˜B‹‹=¸ÍŠòŠŠ}¸ÍŠòŠ­‹]˜¸Í‹"Â÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷6W'f–6W5öÖVçUöÆ—7G5÷gåö6Æ–VçG5öæE÷&VfW'&Å÷&Wv&Eö'WGFöâ‡6VÆbÂ÷7EöÖö6²Â7FG5öÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢–çf—FVBÒ7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$–çf—FVB"Â&VfW'&VEö'“Ö7W7FöÖW"¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢‡V•öVÖ–ÃÒ&Æ–6Uóv""À¢WV–CÒ&&&&"Ö&"ÓF&Ó†&Ö&&&&&""À¢7V%ö–CÒ'7V""À¢7V%öÆ–æ³Ò&‡GG3¢òöW†×ÆRæ6öÒ÷7V"öÆ–6R"À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢W6VE÷G&ff–5ö'—FW3Ó#‚¢#B¢#BÀ¢GW&F–öåöF—3Ó3À¢¢&VfW'&Å&Wv&DÆVFvW"æö&¦V7G2æ7&VFR€¢–çf—FW#Ö7W7FöÖW"À¢–çf—FVCÖ–çf—FVBÀ¢÷&FW#Ö÷&FW"À¢&Wv&Eöv#ÔFV6–ÖÂ‚#"ã"’À¢7FGW3Õ&VfW'&Å&Wv&DÆVFvW"å7FGW2äd”Ä$ÄRÀ¢f–Æ&ÆUöC×F–ÖW¦öæRææ÷r‚’À¢¢7FG5öÖö6²ç&WGW&å÷fÇVRÒ°¢'æVÅöf–Æ&ÆR#¢G'VRÀ¢'F÷FÅ÷G&ff–5ö'—FW2#¢#B¢¢2À¢'W6VE÷G&ff–5ö'—FW2#¢#‚¢#B¢#BÀ¢'&VÖ–æ–æu÷G&ff–5ö'—FW2#¢ƒ#B¢¢2’Òƒ#‚¢#B¢#B’À¢&W‡—'•öB#¢F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó#’À¢Ð ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§7V'2"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-ŠÝŠÍ˜Rª˜B"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-ŠÝŠÍ˜R˜]‹]‹˜(Í‹MŠý˜r"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-Š‹Š}¸ÂŠý‹¸ÍŠ}˜Š¢˜M¸Í˜mª’"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚&‡GG3¢òöW†×ÆRæ6öÒ÷7V"öÆ–6R"Â–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b'W6W#§&VfW'&Å÷&VFVVÓ§·gåö6Æ–VçBçV&Æ–5ö–GÒ"Â6ÆÆ&6µ÷fÇVW2¢FVÆWFUö6ÆÆ&6²ÒæW‡B‡fÇVRf÷"fÇVR–â6ÆÆ&6µ÷fÇVW2–bfÇVRç7F'G7v—F‚‚'W6W#¦6Æ–VçEöFVÆWFS¢"’¢6VÆbæ76W'Dæ÷D–â‡7G"‡gåö6Æ–VçBçV&Æ–5ö–B’ÂFVÆWFUö6ÆÆ&6²¢6VÆbæ76W'Dæ÷D–â‡gåö6Æ–VçBçWV–BÂFVÆWFUö6ÆÆ&6² ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6W%öÖVçUö6ÆÆ&6µö¶VW5öÖ–åö'WGFöç2‡6VÆbÂ÷7EöÖö6²“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦ÖVçR"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Š}‹"˜]˜m˜¸Â‹-¸Í‹Š}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"Â–ÆöE²'FW‡B%Ò¢6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â‚'W6W#¦'W’"Â6ÆÆ&6·2¢6VÆbæ76W'D–â‚'W6W#§7V'2"Â6ÆÆ&6·2¢6VÆbæ76W'D–â‚'W6W#¦†VÇ"Â6ÆÆ&6·2¢6VÆbæ76W'D–â‚'W6W#§&öf–ÆR"Â6ÆÆ&6·2¢6VÆbæ76W'Dæ÷D–â‚&FÖ–ã¦÷&FW'3§VæF–ær"Â6ÆÆ&6·2 ¢F6‚‚'7F÷&Ræ&÷G2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷6W'f–6W5öÖVçUö†–FW5öFVÆWFVEöæEöf÷&V–våö6Æ–VçG2‡6VÆbÂ÷7EöÖö6²Â7FG5öÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢÷F†W%ö7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$&ö""¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢f—6–&ÆU÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%f—6–&ÆRÆâ"À¢6ÇVsÒ'f—6–&ÆR×Æâ"À¢föÇVÖUöv#Ò#ã"À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5ö7F—fSÕG'VRÀ¢—5÷V&Æ–3ÕG'VRÀ¢¢FVÆWFVE÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ$FVÆWFVBÆâ"À¢6ÇVsÒ&FVÆWFVB×Æâ"À¢föÇVÖUöv#Ò#"ã"À¢GW&F–öåöF—3Ó3À¢&–6SÓ#À¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5ö7F—fSÕG'VRÀ¢—5÷V&Æ–3ÕG'VRÀ¢¢÷F†W%÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ$÷F†W"Æâ"À¢6ÇVsÒ&÷F†W"×Æâ"À¢föÇVÖUöv#Ò#2ã"À¢GW&F–öåöF—3Ó3À¢&–6SÓ3À¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5ö7F—fSÕG'VRÀ¢—5÷V&Æ–3ÕG'VRÀ¢¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×f—6–&ÆU÷ÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×f—6–&ÆU÷Æâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×f—6–&ÆU÷Æâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢¢÷F†W%ö÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö÷F†W%ö7W7FöÖW"À¢ÆãÖ÷F†W%÷ÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçCÖ÷F†W%÷Æâç&–6RÀ¢÷&–v–æÅöÖ÷VçCÖ÷F†W%÷Æâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢¢f—6–&ÆUö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×f—6–&ÆU÷ÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ'f—6–&ÆR"À¢‡V•öVÖ–ÃÒ'f—6–&ÆR"À¢WV–CÒ#ÓÓCÓƒÓ"À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢¢FVÆWFVEö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢ÆãÖFVÆWFVE÷ÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&FVÆWFVB"À¢‡V•öVÖ–ÃÒ&FVÆWFVB"À¢WV–CÒ########"Ó###"ÓC##"Óƒ##"Ó###########""À¢7FGW3Õeä6Æ–VçBå7FGW2äDTÄUDTBÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó"¢#B¢¢2À¢FVÆWFVEöC×F–ÖW¦öæRææ÷r‚’À¢¢÷F†W%ö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷F†W%ö÷&FW"À¢ÆãÖ÷F†W%÷ÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&÷F†W""À¢‡V•öVÖ–ÃÒ&÷F†W""À¢WV–CÒ#33333332Ó3332ÓC332Óƒ332Ó333333333332"À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó2¢#B¢¢2À¢¢7FG5öÖö6²ç&WGW&å÷fÇVRÒ°¢'æVÅöf–Æ&ÆR#¢G'VRÀ¢'F÷FÅ÷G&ff–5ö'—FW2#¢#B¢¢2À¢'W6VE÷G&ff–5ö'—FW2#¢À¢'&VÖ–æ–æu÷G&ff–5ö'—FW2#¢#B¢¢2À¢&W‡—'•öB#¢F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó#’À¢Ð ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§7V'2"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚%f—6–&ÆRÆâ"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚$FVÆWFVBÆâ"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚$÷F†W"Æâ"Â–ÆöE²'FW‡B%Ò¢6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'EG'VR†ç’‡7G"‡f—6–&ÆUö6Æ–VçBçV&Æ–5ö–B’–âfÇVRf÷"fÇVR–â6ÆÆ&6·2’¢6VÆbæ76W'DfÇ6R†ç’‡7G"†FVÆWFVEö6Æ–VçBçV&Æ–5ö–B’–âfÇVRf÷"fÇVR–â6ÆÆ&6·2’¢6VÆbæ76W'DfÇ6R†ç’‡7G"†÷F†W%ö6Æ–VçBçV&Æ–5ö–B’–âfÇVRf÷"fÇVR–â6ÆÆ&6·2’ ¢F6‚‚'7F÷&Ræ&÷G2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6W%ö6Æ–VçE÷W6vUö6ÆÆ&6µö¶VW5÷7FGW5öÖW76vR‡6VÆbÂ÷7EöÖö6²Â7FG5öÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢‡V•öVÖ–ÃÒ&Æ–6Uóv""À¢WV–CÒ#CCCCCCCBÓCCCBÓCCCBÓƒCCBÓCCCCCCCCCCCB"À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢W6VE÷G&ff–5ö'—FW3Ó#‚¢#B¢#BÀ¢GW&F–öåöF—3Ó3À¢¢7FG5öÖö6²ç&WGW&å÷fÇVRÒ°¢'æVÅöf–Æ&ÆR#¢G'VRÀ¢'F÷FÅ÷G&ff–5ö'—FW2#¢#B¢¢2À¢'W6VE÷G&ff–5ö'—FW2#¢#‚¢#B¢#BÀ¢'&VÖ–æ–æu÷G&ff–5ö'—FW2#¢ƒ#B¢¢2’Òƒ#‚¢#B¢#B’À¢&W‡—'•öB#¢F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó#’À¢Ð ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦6Æ–VçE÷W6vS§·gåö6Æ–VçBçV&Æ–5ö–GÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢7FG5öÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-ªŠ}˜m˜¸Íªò‹M˜]Šr"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-ŠÝŠÍ˜RŠŠ}˜-¸Î(Í˜]Š}˜mŠý˜r"Â–ÆöE²'FW‡B%Ò¢6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b'W6W#¦6Æ–VçE÷&Vg&W6ƒ§·gåö6Æ–VçBçV&Æ–5ö–GÒ"Â6ÆÆ&6·2¢6VÆbæ76W'D–â†b'W6W#¦6Æ–VçE÷&VæWs§·gåö6Æ–VçBçV&Æ–5ö–GÒ"Â6ÆÆ&6·2 ¢F6‚‚'7F÷&Ræ&÷G2ç7–æ5÷gåö6Æ–VçE÷7FG2"Â&WGW&å÷fÇVS×·Ò¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷6W'f–6W5övWEö6öæf–u÷6VæG5÷7V'67&—F–öåöæEöF—&V7Eö–åööæUöÖW76vR‡6VÆbÂ÷7EöÖö6²Â7FG5öÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢‡V•öVÖ–ÃÒ&Æ–6Uóv""À¢WV–CÒ&6F6F6F6BÖ6F6BÓF6F2Ó†6F2Ö6F6F6F6F6F6B"À¢7V%ö–CÒ'7V""À¢7V%öÆ–æ³Ò&‡GG3¢òöW†×ÆRæ6öÒ÷7V"öÆ–6R"À¢F—&V7EöÆ–æ³Ò'fÆW73¢òöÆ–6RÖ6öæf–r"À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢W6VE÷G&ff–5ö'—FW3Ó#‚¢#B¢#BÀ¢GW&F–öåöF—3Ó3À¢ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦6Æ–VçEö6öæf–s§·gåö6Æ–VçBçV&Æ–5ö–GÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚&‡GG3¢òöW†×ÆRæ6öÒ÷7V"öÆ–6R"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚'fÆW73¢òöÆ–6RÖ6öæf–r"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚/	ùIr˜M¸Í˜mª’Š}‹MŠ­‹Š}ª’"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚.)ª˜M¸Í˜mª’˜]‹=Š­˜-¸Í˜R"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2æFVÆWFU÷gåö6Æ–VçEöf÷%÷W6W""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6W%öFVÆWFUö6öæf–u÷&WV—&W5ö6öæf—&ÖF–öåöæEö6ÆÇ5÷6W'f–6R‡6VÆbÂ÷7EöÖö6²ÂFVÆWFUöÖö6²Â7FG5öÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢‡V•öVÖ–ÃÒ&Æ–6Uóv""À¢WV–CÒ&VFVFVFVBÖVFVBÓFVFRÓ†VFRÖVFVFVFVFVFVB"À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢W‡—&W5öC×F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó#’À¢¢7FG5öÖö6²ç&WGW&å÷fÇVRÒ°¢'æVÅöf–Æ&ÆR#¢G'VRÀ¢'F÷FÅ÷G&ff–5ö'—FW2#¢#B¢¢2À¢'W6VE÷G&ff–5ö'—FW2#¢À¢'&VÖ–æ–æu÷G&ff–5ö'—FW2#¢#B¢¢2À¢&W‡—'•öB#¢gåö6Æ–VçBæW‡—&W5öBÀ¢Ð¢FVÆWFUöÖö6²ç&WGW&å÷fÇVRÒ²'7V66W72#¢G'VWÐ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§7V'2"’¢Æ—7E÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–âÆ—7E÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢FVÆWFUö6ÆÆ&6²ÒæW‡B‡fÇVRf÷"fÇVR–â6ÆÆ&6·2–bfÇVRç7F'G7v—F‚‚'W6W#¦6Æ–VçEöFVÆWFS¢"’ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†FVÆWFUö6ÆÆ&6²Â6ÆÆ&6µö–CÒ&FVÆWFR×7F'B"’¢6öæf—&Õ÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚.)ªûˆòŠÝ‹˜ªŠ}˜m˜¸Íªò"Â6öæf—&Õ÷–ÆöE²'FW‡B%Ò¢6öæf—&Õö6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â6öæf—&Õ÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6öæf—&Õö6ÆÆ&6²ÒæW‡B‡fÇVRf÷"fÇVR–â6öæf—&Õö6ÆÆ&6·2–bfÇVRç7F'G7v—F‚‚'W6W#¦6Æ–VçEöFVÆWFUö6öæf—&Ó¢"’¢6VÆbæ76W'Dæ÷D–â‡gåö6Æ–VçBçWV–BÂ6öæf—&Õö6ÆÆ&6² ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†6öæf—&Õö6ÆÆ&6²Â6ÆÆ&6µö–CÒ&FVÆWFRÖ6öæf—&Ò"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢FVÆWFUöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢&w2Â·v&w2ÒFVÆWFUöÖö6²æ6ÆÅö&w0¢6VÆbæ76W'DWVÂ†&w5³ÒÂ7W7FöÖW"¢6VÆbæ76W'DWVÂ†&w5³Òç²Âgåö6Æ–VçBç²¢6VÆbæ76W'DWVÂ†·v&w5²&7F÷%÷FVÆVw&Õö–B%ÒÂ#C""¢6VÆbæ76W'D–â‚.)ÈRªŠ}˜m˜¸ÍªòŠÝ‹˜‹MŠò"Â÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2æFVÆWFU÷gåö6Æ–VçEöf÷%÷W6W""¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6W%öFVÆWFUö6öæf–uö6öæf—&Õ÷&V¦V7G5öf÷&V–våö6Æ–VçB‡6VÆbÂ÷7EöÖö6²ÂFVÆWFUöÖö6²“ ¢g&öÒæ&÷G2–×÷'B7&VFU÷W6W%ö6Æ–VçEöFVÆWFU÷Fö¶Và ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢÷F†W%ö7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$&ö""¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢÷F†W%ö÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö÷F†W%ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢¢÷F†W%ö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷F†W%ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&÷F†W%óv""À¢‡V•öVÖ–ÃÒ&÷F†W%óv""À¢WV–CÒ#“““““““’Ó“““’ÓC““’Óƒ““’Ó“““““““““““’"À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢¢Fö¶VâÒ7&VFU÷W6W%ö6Æ–VçEöFVÆWFU÷Fö¶Vâ†&÷E÷W6W"Â÷F†W%ö6Æ–VçB ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦6Æ–VçEöFVÆWFUö6öæf—&Ó§·Fö¶VçÒ"Â6ÆÆ&6µö–CÒ&f÷&V–vâÖFVÆWFR"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢FVÆWFUöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢6VÆbæ76W'D–â‚-Š}¸Í˜bªŠ}˜m˜¸Íªò›í¸ÍŠýŠr˜m‹MŠò"Â÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö†VÇö6ÆÆ&6µ÷6†÷w5÷W'6–åö†VÇ‡6VÆbÂ÷7EöÖö6²“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦†VÇ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-‹Š}˜}˜m˜]Šr"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-Ší‹¸ÍŠò"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-Š­˜]Šý¸ÍŠò"Â–ÆöE²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷7W÷'EöfÆ÷uö7&VFW5÷F–6¶WEög&öÕö&÷B‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§7W÷'B"’¢6FVv÷'•÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6FVv÷'•ö6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â6FVv÷'•÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â‚'W6W#§7W÷'Eö6C§–ÖVçB"Â6FVv÷'•ö6ÆÆ&6·2 ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#§7W÷'Eö6C§–ÖVçB"Â6ÆÆ&6µö–CÒ'7W÷'BÖ6B"’¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ'7W÷'E÷v—EöÖW76vR" ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚-‹‹=¸ÍŠý˜RŠ‹‹‹=¸Â˜m‹MŠý˜rŠ}‹=Š¢â"ÂÖW76vUö–CÓ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6öçfW'6F–öâÒ7W÷'D6öçfW'6F–öâæö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†6öçfW'6F–öâç7V&¦V7BÂ-˜]‹Mª˜B›í‹ŠýŠ}ŠíŠ¢"¢6VÆbæ76W'DWVÂ†6öçfW'6F–öâç7FGW2Â7W÷'D6öçfW'6F–öâå7FGW2åt•D”äuôDÔ”â¢6VÆbæ76W'DWVÂ…7W÷'DÖW76vRæö&¦V7G2ævWB†6öçfW'6F–öãÖ6öçfW'6F–öâ’æ&öG’Â-‹‹=¸ÍŠý˜RŠ‹‹‹=¸Â˜m‹MŠý˜rŠ}‹=Š¢â"¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR¢W6W%öÖW76vW2Ò°¢6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C""æB'FW‡B"–â6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò¢Ð¢6VÆbæ76W'EG'VR†ç’‚-‹M˜]Š}‹˜rŠ­¸ÍªŠ¢"–âFW‡Bf÷"FW‡B–âW6W%öÖW76vW2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöFÖ–åöÖVçUö—5÷f—6–&ÆUööæÇ•÷FõöFÖ–åöæEöÆ—7G5÷VæF–æuö÷&FW'2‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢W6W%÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢W6W%ö6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–âW6W%÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'Dæ÷D–â‚&FÖ–ã¦÷&FW'3§VæF–ær"ÂW6W%ö6ÆÆ&6·2 ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"ÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"Âf—'7EöæÖSÒ$FÖ–â"’¢FÖ–å÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢FÖ–åö6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–âFÖ–å÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â‚&FÖ–ã¦÷&FW'3§VæF–ær"ÂFÖ–åö6ÆÆ&6·2¢6VÆbæ76W'D–â‚&FÖ–ã§6ÆW5÷&W÷'B"ÂFÖ–åö6ÆÆ&6·2 ¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢Æã×6VÆbçÆâÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2åTäD”ärÀ¢6VæFW%ö6&EöæÖSÒ$Æ–6R'W–W""À¢ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²‚&FÖ–ã¦÷&FW'3§VæF–ær"ÂW6W%ö–CÓ““’ÂW6W&æÖSÒ&FÖ–â"Âf—'7EöæÖSÒ$FÖ–â"¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢VæF–æu÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-‹=˜Š}‹‹N(Í˜}Š}¸ÂVæF–ær"ÂVæF–æu÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â†÷&FW"æ÷&FW%÷G&6¶–æuö6öFRÂVæF–æu÷–ÆöE²'FW‡B%Ò¢VæF–æuö6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–âVæF–æu÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b&&÷fS§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"ÂVæF–æuö6ÆÆ&6·2¢6VÆbæ76W'D–â†b'&V¦V7C§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"ÂVæF–æuö6ÆÆ&6·2 ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöFÖ–åö÷&FW%öFWF–Åö6ÆÆ&6µ÷6VæG5ö÷&FW%öFWF–Ç2‡6VÆbÂ÷7EöÖö6²“ ¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢Æã×6VÆbçÆâÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2åTäD”ärÀ¢6VæFW%ö6&EöæÖSÒ$Æ–6R'W–W""À¢ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²€¢b&÷&FW#¦FWF–Ã§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"À¢W6W%ö–CÓ““’À¢W6W&æÖSÒ&FÖ–â"À¢f—'7EöæÖSÒ$FÖ–â"À¢¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-ŠÍ‹-Šm¸ÍŠ}Š¢‹=˜Š}‹‹B"Â–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â†÷&FW"æ÷&FW%÷G&6¶–æuö6öFRÂ–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b&&÷fS§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"Â6ÆÆ&6µ÷fÇVW2¢6VÆbæ76W'D–â†b'&V¦V7C§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"Â6ÆÆ&6µ÷fÇVW2¢6VÆbæ76W'D–â†b&÷&FW#¦FWF–Ã§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"Â6ÆÆ&6µ÷fÇVW2 ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöFÖ–å÷&V¦V7Eö6ÆÆ&6µ÷&V¦V7G5ö÷&FW%ögFW%÷&V6öâ‡6VÆbÂ÷7EöÖö6²“ ¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢Æã×6VÆbçÆâÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2åTäD”ärÀ¢6VæFW%ö6&EöæÖSÒ$Æ–6R'W–W""À¢ ¢6ÆÆ&6µ÷&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²€¢b'&V¦V7C§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"À¢W6W%ö–CÓ““’À¢W6W&æÖSÒ&FÖ–â"À¢f—'7EöæÖSÒ$FÖ–â"À¢¢¢6VÆbæ76W'DWVÂ†6ÆÆ&6µ÷&W7öç6Rç7FGW5ö6öFRÂ#¢VæF–ærÒ&÷EVæF–æt7F–öâæö&¦V7G2ævWB†÷&FW#Ö÷&FW"¢6VÆbæ76W'DWVÂ‡VæF–æræ7F–öâÂ&÷EVæF–æt7F–öâä7F–öâå$T¤T5Eôõ$DU" ¢&V6öå÷&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæÖW76vR€¢-‹Šò‹MŠý˜rŠ­˜‹=‹rŠ}Šý˜]¸Í˜b"À¢ÖW76vUö–CÓÀ¢W6W%ö–CÓ““’À¢W6W&æÖSÒ&FÖ–â"À¢f—'7EöæÖSÒ$FÖ–â"À¢¢ ¢6VÆbæ76W'DWVÂ‡&V6öå÷&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"ç&Vg&W6…ög&öÕöF"‚¢VæF–ærç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2å$T¤T5DTB¢6VÆbæ76W'DWVÂ†÷&FW"ç&V¦V7F–öå÷&V6öâÂ-‹Šò‹MŠý˜rŠ­˜‹=‹rŠ}Šý˜]¸Í˜b"¢6VÆbæ76W'DWVÂ‡VæF–ærç7FGW2Â&÷EVæF–æt7F–öâå7FGW2ä4ôÕÄUDTB¢6VçE÷FW‡G2Ò°¢6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#““’"æB'FW‡B"–â6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò¢Ð¢6VÆbæ76W'EG'VR†ç’‚$÷&FW"&V¦V7FVB"–âFW‡B÷"-‹Šò‹MŠý˜r"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö÷W&F÷%ö&6VEö&÷EöfÆ÷u÷6VÆV7G5ö÷W&F÷%ö&Vf÷&Uöf–ÇFW&VE÷Æç2‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç7F÷&Rç6ÆW5öÖöFRÒ7F÷&Rå6ÆW4ÖöFRäõU$Dõ%ô$4T@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'6ÆW5öÖöFR"Â'WFFVEöB%Ò¢÷W&F÷%öÒ÷W&F÷"æö&¦V7G2æ7&VFR‡7F÷&S×6VÆbç7F÷&RÂæÖSÒ-˜}˜]‹Š}˜rŠ}˜˜B"Â6ÇVsÒ'FVÆVw&ÒÖÖ6’"¢÷W&F÷%ö"Ò÷W&F÷"æö&¦V7G2æ7&VFR‡7F÷&S×6VÆbç7F÷&RÂæÖSÒ-Š}¸Í‹Š}˜m‹=˜B"Â6ÇVsÒ'FVÆVw&ÒÖ—&æ6VÆÂ"¢6VÆbçÆâæ÷W&F÷'2æFB†÷W&F÷%ö¢÷F†W%÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ$—&æ6VÆÂ&÷BÆâ"À¢6ÇVsÒ'FVÆVw&ÒÖ—&æ6VÆÂ×Æâ"À¢föÇVÖUöv#ÔFV6–ÖÂ‚#"ã"’À¢GW&F–öåöF—3Ó3À¢&–6SÓƒÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5ö7F—fSÕG'VRÀ¢—5÷V&Æ–3ÕG'VRÀ¢¢÷F†W%÷Æâæ÷W&F÷'2æFB†÷W&F÷%ö" ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"’ ¢÷W&F÷%÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-Š}›í‹Š}Š­˜‹Ší˜Šò‹ŠrŠ}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"Â÷W&F÷%÷–ÆöE²'FW‡B%Ò¢÷W&F÷%ö6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â÷W&F÷%÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b'W6W#¦'W–÷§¶÷W&F÷%öç·Ò"Â÷W&F÷%ö6ÆÆ&6·2¢6VÆbæ76W'DfÇ6R†ç’‡fÇVRç7F'G7v—F‚‚'W6W#¦'W—Æã¢"’f÷"fÇVR–â÷W&F÷%ö6ÆÆ&6·2’ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W–÷§¶÷W&F÷%öç·Ò"Â6ÆÆ&6µö–CÒ&÷W&F÷"Ö6""’ ¢Æå÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â†b-›í˜M˜n(Í˜}Š}¸Â¶÷W&F÷%öææÖWÒ"ÂÆå÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â‚-¸Íª¸ÂŠ}‹"›í˜M˜n(Í˜}Š}¸Â‹-¸Í‹‹ŠrŠ}˜mŠ­ŠíŠ}Š‚ª˜m¸ÍŠò"ÂÆå÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‡6VÆbçÆâææÖRÂÆå÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â†÷F†W%÷ÆâææÖRÂÆå÷–ÆöE²'FW‡B%Ò¢Æåö'WGFöå÷FW‡G2Ò°¢'WGFöå²'FW‡B%Ð¢f÷"&÷r–âÆå÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢–b'WGFöâævWB‚&6ÆÆ&6µöFF"Â""’ç7F'G7v—F‚‚'W6W#¦'W—Æã¢"¢Ð¢6VÆbæ76W'EG'VR†ç’‚-»ªý¸ÍªýŠ}ŠŠ}¸ÍŠ¢"–âFW‡BæB-»=»‹˜‹-˜r"–âFW‡BæB-»»»Í»»»Š­˜˜]Š}˜b"–âFW‡Bf÷"FW‡B–âÆåö'WGFöå÷FW‡G2’¢Æåö6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–âÆå÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆV7FVE÷Æåö6ÆÆ&6²Òb'W6W#¦'W—Æã§·6VÆbçÆâç·Ó¦÷§¶÷W&F÷%öç·Ò ¢6VÆbæ76W'D–â‡6VÆV7FVE÷Æåö6ÆÆ&6²ÂÆåö6ÆÆ&6·2 ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‡6VÆV7FVE÷Æåö6ÆÆ&6²Â6ÆÆ&6µö–CÒ'ÆâÖ6""’ ¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•EõTåD•E’¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFUöFF²&÷W&F÷%ö–B%ÒÂ÷W&F÷%öç² ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7Eö7W7FöÕ÷föÇVÖU÷W&6†6Uö6·5÷föÇVÖU÷F†Vå÷VçF—G’‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç7F÷&Ræ7W7FöÕ÷föÇVÖU÷&–6U÷W%öv"ÒFV6–ÖÂ‚#"¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²&7W7FöÕ÷föÇVÖU÷&–6U÷W%öv""Â'WFFVEöB%Ò ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W’"’¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â‚'W6W#¦'W–7W7FöÒ"Â6ÆÆ&6µ÷fÇVW2 ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W–7W7FöÒ"Â6ÆÆ&6µö–CÒ&7W7FöÒÖ6""’¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eô5U5DôÕõdôÅTÔR¢6VÆbæ76W'D–â‚-ŠÝŠÍ˜RŠý˜MŠí˜Š}˜r"Â÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ò ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚#r"ÂÖW76vUö–CÓ"’ ¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•EõTåD•E’¢7W7FöÕ÷ÆâÒÆâæö&¦V7G2ævWB‡³Ö&÷E÷W6W"ç7FFUöFF²'Æåö–B%Ò¢6VÆbæ76W'EG'VR†7W7FöÕ÷Æâæ—5ö7W7FöÕ÷föÇVÖR¢6VÆbæ76W'DWVÂ†7W7FöÕ÷ÆâçföÇVÖUöv"ÂFV6–ÖÂ‚#rã"’¢6VÆbæ76W'DWVÂ†7W7FöÕ÷ÆâæGW&F–öåöF—2Â3¢VçF—G•÷&ö×BÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢6VÆbæ76W'D–â‚-ŠÝŠÍ˜RŠ}˜mŠ­ŠíŠ}Š¸Ã¢»rªý¸ÍªýŠ}ŠŠ}¸ÍŠ¢"ÂVçF—G•÷&ö×B¢6VÆbæ76W'D–â‚-˜-¸Í˜]Š¢˜}‹ªŠ}˜m˜¸Íªó¢»}»»Í»»»Š­˜˜]Š}˜b"ÂVçF—G•÷&ö×B ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷FVÆVw&Õ÷W&6†6U÷&WV—&W5÷&V6V—Eö–ÖvR‡6VÆbÂ÷7EöÖö6²“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷6¶—"ÂÖW76vUö–CÓ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B¢6VÆbæ76W'DfÇ6R„÷&FW"æö&¦V7G2æW†—7G2‚’¢Æ7EöÖW76vRÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢6VÆbæ76W'DWVÂ†Æ7EöÖW76vRÂ-˜M‹}˜Š}˜²‹ª‹2‹‹=¸ÍŠò›í‹ŠýŠ}ŠíŠ¢‹ŠrŠ}‹‹=Š}˜Bª˜m¸ÍŠòâ"¢6VÆbæ76W'Dæ÷D–â‚#Â"ÂÆ7EöÖW76vR¢6VÆbæ76W'Dæ÷D–â‚-»B‹˜-˜R"ÂÆ7EöÖW76vR¢6VÆbæ76W'Dæ÷D–â‚-‹=Š}‹Š¢›í‹ŠýŠ}ŠíŠ¢"ÂÆ7EöÖW76vR ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#33333332Ó3332ÓC332Óƒ332Ó333333333332"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7Eö&ÆU÷W&6†6UöfÆ÷uö—5öVæ&ÆVEöf÷%öæöåöFÖ–å÷W6W'2‡6VÆbÂövWBÂ÷‡V’“ ¢6VÆbæ&÷Eö6öæf–rç&÷f–FW"Ò&÷D6öæf–wW&F–öâå&÷f–FW"ä$ÄP¢6VÆbæ&÷Eö6öæf–rç6fR‡WFFUöf–VÆG3Õ²'&÷f–FW""Â'WFFVEöB%Ò¢6VÆbçW&ÂÒ&WfW'6R‚&&÷E÷vV&†öö²"Â&w3Õ·6VÆbæ&÷Eö6öæf–rç&÷f–FW"Â6VÆbæ&÷Eö6öæf–rçvV&†ööµ÷6V7&WEÒ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂ¢¦·v&w2“ ¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢'†÷F÷2÷&V6V—Bæ§r'×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢"À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢·²&f–ÆUö–B#¢'&V6V—BÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'&V6V—B'ÕÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'6÷W&6R%ÒÂ&&ÆUö&÷B"¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöæÖRÂ$Æ–6R"¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöÆ7CBÂ""¢6VÆbæ76W'DWVÂ†÷&FW"æ&æµ÷G&6¶–æuö6öFRÂ""¢6VÆbæ76W'D—4æ÷DæöæR†÷&FW"ç–ÖVçE÷F–ÖR¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB‡&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"æ&÷Eö6öæf–rç&÷f–FW"Â&÷D6öæf–wW&F–öâå&÷f–FW"ä$ÄR ¢F6‚‚'7F÷&Ræ&÷G2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷vV&†ööµ÷G&–vvW'5öGVU÷6ÆW5÷&W÷'Eööæ6R‡6VÆbÂ÷÷7BÂ6VæE÷Fõö6öæf–uöÖö6²“ ¢6VÆbæ&÷Eö6öæf–ræÆ7E÷&W÷'E÷6VçEöBÒF–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ór¢6VÆbæ&÷Eö6öæf–rç&W÷'Eö–çFW'fÅö†÷W'2Ò`¢6VÆbæ&÷Eö6öæf–rç6fR‡WFFUöf–VÆG3Õ²&Æ7E÷&W÷'E÷6VçEöB"Â'&W÷'Eö–çFW'fÅö†÷W'2"Â'WFFVEöB%Ò ¢f—'7E÷&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6V6öæE÷&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷Æç2"ÂÖW76vUö–CÓ"’ ¢6VÆbæ76W'DWVÂ†f—'7E÷&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DWVÂ‡6V6öæE÷&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'DWVÂ‡6VæE÷Fõö6öæf–uöÖö6²æ6ÆÅö6÷VçBÂ¢6VÆbæ&÷Eö6öæf–rç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'Dw&VFW"‡6VÆbæ&÷Eö6öæf–ræÆ7E÷&W÷'E÷6VçEöBÂF–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF†Ö–çWFW3Ó’ ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚########"Ó###"ÓC##"Óƒ##"Ó###########""’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7E÷FVÆVw&Õ÷&V6V—E÷†÷Fõö—5÷6fVEöæEöf–ÆUö–Eö—5÷&W6W'fVB‡6VÆbÂövWBÂ÷‡V’“ ¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢'†÷F÷2÷&V6V—Bæ§r'×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢"À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢°¢²&f–ÆUö–B#¢'6ÖÆÂÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'6ÖÆÂ'ÒÀ¢²&f–ÆUö–B#¢&Æ&vRÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢&Æ&vR'ÒÀ¢ÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†÷&FW"æ7W7FöÖW"Â&÷E÷W6W"æ7W7FöÖW"¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2ævWB†÷&FW#Ö÷&FW"¢g&öÒæ&÷E÷F&vWG2–×÷'BvWE÷gåö6Æ–VçE÷FVÆVw&Õ÷F&vWG0 ¢F&vWG2ÒvWE÷gåö6Æ–VçE÷FVÆVw&Õ÷F&vWG2‡gåö6Æ–VçBÂ7F÷&S×6VÆbç7F÷&R¢6VÆbæ76W'DWVÂ†ÆVâ‡F&vWG2’Â¢6VÆbæ76W'DWVÂ‡F&vWG5³Òæ6†Eö–BÂ#C""¢6VÆbæ76W'EG'VR†÷&FW"ç–ÖVçE÷&V6V—Eö–ÖvRææÖR¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöæÖRÂ$Æ–6R"¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöÆ7CBÂ""¢6VÆbæ76W'DWVÂ†÷&FW"æ&æµ÷G&6¶–æuö6öFRÂ""¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'&V6V—B%Õ²&f–ÆUö–B%ÒÂ&Æ&vRÖf–ÆR"¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'&V6V—B%Õ²&f–ÆU÷Væ—VUö–B%ÒÂ&Æ&vR"¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'&V6V—B%Õ²&f–ÆU÷F‚%ÒÂ'†÷F÷2÷&V6V—Bæ§r" ¢6VæE÷†÷Fõö6ÆÇ2Ò¶6ÆÂf÷"6ÆÂ–â÷7Eö6ÆÇ2–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæE†÷Fò"•Ð¢6VÆbæ76W'DWVÂ†ÆVâ‡6VæE÷†÷Fõö6ÆÇ2’Â¢†÷FõöFFÒ6VæE÷†÷Fõö6ÆÇ5³Õ²&FF%Ð¢6VÆbæ76W'D–â‚-‹=˜Š}‹‹BŠÍŠý¸ÍŠòeâ"Â†÷FõöFF²&6F–öâ%Ò¢6VÆbæ76W'D–â†÷&FW"æ÷&FW%÷G&6¶–æuö6öFRÂ†÷FõöFF²&6F–öâ%Ò¢&WÇ•öÖ&·WÒ§6öâæÆöG2‡†÷FõöFF²'&WÇ•öÖ&·W%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â&WÇ•öÖ&·W²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b&&÷fS§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"Â6ÆÆ&6µ÷fÇVW2¢6VÆbæ76W'D–â†b'&V¦V7C§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"Â6ÆÆ&6µ÷fÇVW2¢6VÆbæ76W'DfÇ6R†ç’†6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"öf÷'v&DÖW76vR"’f÷"6ÆÂ–â÷7Eö6ÆÇ2’ ¢W6W%öÖW76vW2Ò°¢6ÆÂævWB‚&§6öâ"Â·Ò¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"’æB6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢Ð¢6VÆbæ76W'EG'VR€¢ç’€¢-›í‹ŠýŠ}ŠíŠ¢ªŠ}‹Š®(ÍŠ˜~(ÍªŠ}‹Š¢"–â—FVÒævWB‚'FW‡B"Â""¢æB#Æ6öFSãÂö6öFSâ"–â—FVÒævWB‚'FW‡B"Â""¢æB—FVÒævWB‚''6UöÖöFR"’ÓÒ$…DÔÂ ¢f÷"—FVÒ–âW6W%öÖW76vW0¢¢¢6VÆbæ76W'EG'VR†ç’‚-‹=˜Š}‹‹B‹M˜]ŠrŠ½ŠŠ¢‹MŠò"–â—FVÒævWB‚'FW‡B"Â""’f÷"—FVÒ–âW6W%öÖW76vW2’ ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#cccccccbÓcccbÓCccbÓƒccbÓcccccccccccb"’¢FVbFW7E÷&V6V—EöF÷væÆöEöf–ÇW&U÷7F–ÆÅö7&VFW5ö÷&FW%öæEöf÷'v&G5ö÷&–v–æÅöÖW76vR‡6VÆbÂ÷‡V’“ ¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢·×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢"À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢·²&f–ÆUö–B#¢&Ö—76–ær×F‚"Â&f–ÆU÷Væ—VUö–B#¢&Ö—76–ær'ÕÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ¢6VÆbæ76W'DfÇ6R†÷&FW"ç–ÖVçE÷&V6V—Eö–ÖvR¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'&V6V—B%Õ²&F÷væÆöEöW'&÷"%ÒÂ&f–ÆUöF÷væÆöE÷Væf–Æ&ÆR"¢6VÆbæ76W'EG'VR†ç’†6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"öf÷'v&DÖW76vR"’f÷"6ÆÂ–â÷7Eö6ÆÇ2’¢W6W%÷FW‡G2Ò°¢6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚'FW‡B"Â""¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"’æB6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢Ð¢6VÆbæ76W'EG'VR†ç’‚-‹=˜Š}‹‹B‹M˜]ŠrŠ½ŠŠ¢‹MŠò"–âFW‡Bf÷"FW‡B–âW6W%÷FW‡G2’¢6VÆbæ76W'DfÇ6R†ç’‚-ŠýŠ}˜m˜M˜Šò‹ª‹2‹‹=¸ÍŠò"–âFW‡Bf÷"FW‡B–âW6W%÷FW‡G2’ ¢F6‚‚'7F÷&Ræ÷&FW%ö7F–öç2æVæ&ÆUö6Æ–VçB"Â&WGW&å÷fÇVSÕG'VR¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#sssssssrÓsssrÓCssrÓƒssrÓsssssssssssr"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöFÖ–å÷W6W%ö6ÆÆ&6·5÷W6Uö7W7FöÖW%öfÆ÷uöæEöF—&V7FÇ•ö7F—fFU÷v—F†÷WE÷&V6V—B‡6VÆbÂ÷7EöÖö6²Â÷‡V’ÂöVæ&ÆR“ ¢6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²€¢b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"À¢W6W%ö–CÓ““’À¢W6W&æÖSÒ&FÖ–â"À¢f—'7EöæÖSÒ$FÖ–â"À¢¢ ¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#““’"¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•EõTåD•E’ ¢6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²€¢'W6W#¦'W—G“£"À¢6ÆÆ&6µö–CÒ&FÖ–â×G’Ö6""À¢W6W%ö–CÓ““’À¢W6W&æÖSÒ&FÖ–â"À¢f—'7EöæÖSÒ$FÖ–â"À¢¢ ¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•EôäÔR ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæÖW76vR€¢$FÖ–â6öæf–r"À¢ÖW76vUö–CÓ"À¢W6W%ö–CÓ““’À¢W6W&æÖSÒ&FÖ–â"À¢f—'7EöæÖSÒ$FÖ–â"À¢¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöæÖRÂ$FÖ–â6öæf–r"¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2ä4ôÕÄUDTB¢6VÆbæ76W'EG'VR†÷&FW"æ—5÷–B¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'6÷W&6R%ÒÂ'FVÆVw&ÕöFÖ–åö&÷EöF—&V7B"¢6VÆbæ76W'EG'VR†÷&FW"æÖWFFF²'7W&W75öæWuö÷&FW%öæ÷F–f–6F–öâ%Ò¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2ævWB†÷&FW#Ö÷&FW"¢6VÆbæ76W'DWVÂ‡gåö6Æ–VçBç7FGW2Âeä6Æ–VçBå7FGW2ä5D•dR¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR¢6VçE÷FW‡G2Ò°¢6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#““’"æB'FW‡B"–â6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò¢Ð¢6VÆbæ76W'EG'VR†ç’‚-‹=‹˜¸Í‹2‹M˜]ŠrŠ-˜]Š}Šý˜r‹MŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'EG'VR†ç’‚'fÆW73¢òöW†×ÆR"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6VÆbæ76W'DfÇ6R†ç’‚$ÖÆf÷&ÖVB6ÆÆ&6²FF"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’ ¢F6‚‚'7F÷&Ræ÷&FW%ö7F–öç2æVæ&ÆUö6Æ–VçB"Â&WGW&å÷fÇVSÕG'VR¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#ƒƒƒƒƒƒƒ‚Óƒƒƒ‚ÓCƒƒ‚Óƒƒƒ‚Óƒƒƒƒƒƒƒƒƒƒƒ‚"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7EöFF—F–öæÅöFÖ–åö6åöF—&V7FÇ•ö7F—fFU÷v—F†÷WE÷&V6V—B‡6VÆbÂ÷7EöÖö6²Â÷‡V’ÂöVæ&ÆR“ ¢6VÆbæ&÷Eö6öæf–ræFF—F–öæÅöFÖ–å÷W6W%ö–G2Ò#ssr ¢6VÆbæ&÷Eö6öæf–rç6fR‡WFFUöf–VÆG3Õ²&FF—F–öæÅöFÖ–å÷W6W%ö–G2"Â'WFFVEöB%Ò ¢6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²€¢b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"À¢W6W%ö–CÓssrÀ¢W6W&æÖSÒ&†VÇW""À¢f—'7EöæÖSÒ$†VÇW""À¢¢¢6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²€¢'W6W#¦'W—G“£"À¢6ÆÆ&6µö–CÒ&†VÇW"×G’Ö6""À¢W6W%ö–CÓssrÀ¢W6W&æÖSÒ&†VÇW""À¢f—'7EöæÖSÒ$†VÇW""À¢¢¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæÖW76vR€¢$†VÇW"6öæf–r"À¢ÖW76vUö–CÓ"À¢W6W%ö–CÓssrÀ¢W6W&æÖSÒ&†VÇW""À¢f—'7EöæÖSÒ$†VÇW""À¢¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†÷&FW"ç6VæFW%ö6&EöæÖRÂ$†VÇW"6öæf–r"¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2ä4ôÕÄUDTB¢6VÆbæ76W'EG'VR†÷&FW"æÖWFFF²&FÖ–åöF—&V7E÷W&6†6R%Ò¢6VÆbæ76W'DWVÂ†÷&FW"æÖWFFF²'6÷W&6R%ÒÂ'FVÆVw&ÕöFÖ–åö&÷EöF—&V7B"¢6VÆbæ76W'DfÇ6R†÷&FW"ç–ÖVçE÷&V6V—Eö–ÖvR¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#ssr"¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR¢†VÇW%÷FW‡G2Ò°¢6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#ssr"æB'FW‡B"–â6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò¢Ð¢6VÆbæ76W'EG'VR†ç’‚-‹=‹˜¸Í‹2‹M˜]ŠrŠ-˜]Š}Šý˜r‹MŠò"–âFW‡Bf÷"FW‡B–â†VÇW%÷FW‡G2’¢6öæf–u÷FW‡BÒæW‡B‡FW‡Bf÷"FW‡B–â†VÇW%÷FW‡G2–b'fÆW73¢òöW†×ÆR"–âFW‡B¢6VÆbæ76W'D–â‚&‡GG3¢òöW†×ÆRæ6öÒ÷7V"÷7V##2"Â6öæf–u÷FW‡B ¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#“““““““’Ó“““’ÓC““’Óƒ““’Ó“““““““““““’"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7E÷&V6V—Eöæ÷F–f–6F–öåö—5÷6VçE÷FõöÆÅö&÷EöFÖ–ç2‡6VÆbÂövWBÂ÷‡V’“ ¢6VÆbæ&÷Eö6öæf–ræFF—F–öæÅöFÖ–å÷W6W%ö–G2Ò#ssr ¢6VÆbæ&÷Eö6öæf–rç6fR‡WFFUöf–VÆG3Õ²&FF—F–öæÅöFÖ–å÷W6W%ö–G2"Â'WFFVEöB%Ò¢÷7Eö6ÆÇ2ÒµÐ¢ÖW76vUö–BÒ  ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂFFÔæöæRÂ¢¦·v&w2“ ¢æöæÆö6ÂÖW76vUö–@¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ&FF#¢FFÂ¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢'†÷F÷2÷&V6V—Bæ§r'×Ò¢–bW&ÂæVæG7v—F‚‚"÷6VæE†÷Fò"’÷"W&ÂæVæG7v—F‚‚"÷6VæDÖW76vR"“ ¢ÖW76vUö–B³Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&ÖW76vUö–B#¢ÖW76vUö–G×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢"À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢·²&f–ÆUö–B#¢'&V6V—BÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'&V6V—B'ÕÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢6VæE÷†÷Fõö6†G2Ò°¢6ÆÅ²&FF%Õ²&6†Eö–B%Ð¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæE†÷Fò"¢Ð¢6VÆbæ76W'D6÷VçDWVÂ‡6VæE÷†÷Fõö6†G2Â²#““’"Â#ssr%Ò¢6VÆbæ76W'DWVÂ„&÷DFÖ–ä÷&FW$ÖW76vRæö&¦V7G2æf–ÇFW"†÷&FW#Ö÷&FW"’æ6÷VçB‚’Â"¢6VÆbæ76W'D6÷VçDWVÂ€¢Æ—7B„&÷DFÖ–ä÷&FW$ÖW76vRæö&¦V7G2æf–ÇFW"†÷&FW#Ö÷&FW"’çfÇVW5öÆ—7B‚&FÖ–å÷W6W%ö–B"ÂfÆCÕG'VR’’À¢²#““’"Â#ssr%ÒÀ¢ ¢F6‚‚'7F÷&Ræ÷&FW%ö7F–öç2æVæ&ÆUö6Æ–VçB"Â&WGW&å÷fÇVSÕG'VR¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚&ÖÓFÓ†Ö"’¢FVbFW7Eö&÷fÅö'•ööæUöFÖ–å÷WFFW5öÆÅöFÖ–åö÷&FW%öÖW76vW2‡6VÆbÂ÷‡V’ÂöVæ&ÆR“ ¢6VÆbæ&÷Eö6öæf–ræFF—F–öæÅöFÖ–å÷W6W%ö–G2Ò#ssr ¢6VÆbæ&÷Eö6öæf–rç6fR‡WFFUöf–VÆG3Õ²&FF—F–öæÅöFÖ–å÷W6W%ö–G2"Â'WFFVEöB%Ò¢&W7VÇBÒ7&VFUöÖçVÅ÷–ÖVçEö÷&FW"€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#ÔæöæRÀ¢ÆãÕÆâæö&¦V7G2ævWB‡³×6VÆbçÆâç²’À¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢6VæFW%ö6&EöæÖSÒ$Æ–6R'W–W""À¢6VæFW%ö6&EöÆ7CCÒ""À¢–ÖVçE÷F–ÖS×F–ÖRƒBÂ3R’À¢ÖWFFF×²'6÷W&6R#¢'FW7B'ÒÀ¢¢6VÆbæ76W'EG'VR‡&W7VÇBç7V66W72¢÷&FW"Ò&W7VÇBæ÷&FW ¢&÷DFÖ–ä÷&FW$ÖW76vRæö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢÷&FW#Ö÷&FW"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢6†Eö–CÒ#““’"À¢ÖW76vUö–CÒ##"À¢ÖW76vUö¶–æCÔ&÷DFÖ–ä÷&FW$ÖW76vRäÖW76vT¶–æBåDU…BÀ¢¢&÷DFÖ–ä÷&FW$ÖW76vRæö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢÷&FW#Ö÷&FW"À¢FÖ–å÷W6W%ö–CÒ#ssr"À¢6†Eö–CÒ#ssr"À¢ÖW76vUö–CÒ##""À¢ÖW76vUö¶–æCÔ&÷DFÖ–ä÷&FW$ÖW76vRäÖW76vT¶–æBåDU…BÀ¢¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ¢¦·v&w7Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢6VÆbæ6ÆÆ&6²€¢b&&÷fS§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"À¢ÖW76vUö–CÓ#À¢W6W%ö–CÓ““’À¢W6W&æÖSÒ&FÖ–â"À¢f—'7EöæÖSÒ$FÖ–â"À¢¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2ä4ôÕÄUDTB¢VF—Eö6ÆÇ2Ò¶6ÆÂf÷"6ÆÂ–â÷7Eö6ÆÇ2–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"öVF—DÖW76vUFW‡B"•Ð¢VF—FVEö6†Eö–G2Ò·7G"†6ÆÅ²&§6öâ%Õ²&6†Eö–B%Ò’f÷"6ÆÂ–âVF—Eö6ÆÇ7Ð¢6VÆbæ76W'D–â‚#““’"ÂVF—FVEö6†Eö–G2¢6VÆbæ76W'D–â‚#ssr"ÂVF—FVEö6†Eö–G2¢f÷"6ÆÂ–âVF—Eö6ÆÇ3 ¢–b7G"†6ÆÅ²&§6öâ%Õ²&6†Eö–B%Ò’–â²#““’"Â#ssr'Ó ¢6VÆbæ76W'DWVÂ†6ÆÅ²&§6öâ%Õ²'&WÇ•öÖ&·W%ÒÂ²&–æÆ–æUö¶W–&ö&B#¢µ×Ò¢6VÆbæ76W'D–â‚$÷&FW"&÷fVB"Â6ÆÅ²&§6öâ%Õ²'FW‡B%Ò ¢F6‚‚'7F÷&Ræ÷&FW%ö7F–öç2æVæ&ÆUö6Æ–VçB"Â&WGW&å÷fÇVSÕG'VR¢F6‚‚'7F÷&Ræ÷&FW%÷6W'f–6W2æ7&VFUö–æ7F—fUö6Æ–VçEöFWF–Ç2"Â&WGW&å÷fÇVSÖf¶Uö6Æ–VçE÷&W7VÇB‚#CCCCCCCBÓCCCBÓCCCBÓƒCCBÓCCCCCCCCCCCB"’¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7Eö&ÆUö&÷fÅ÷6VæG5ö6öæf–u÷Fõ÷6ÖUö&ÆUö&÷B‡6VÆbÂövWBÂ÷‡V’ÂöVæ&ÆUö6Æ–VçB“ ¢6VÆbæ&÷Eö6öæf–rç&÷f–FW"Ò&÷D6öæf–wW&F–öâå&÷f–FW"ä$ÄP¢6VÆbæ&÷Eö6öæf–rç6fR‡WFFUöf–VÆG3Õ²'&÷f–FW""Â'WFFVEöB%Ò¢6VÆbçW&ÂÒ&WfW'6R‚&&÷E÷vV&†öö²"Â&w3Õ·6VÆbæ&÷Eö6öæf–rç&÷f–FW"Â6VÆbæ&÷Eö6öæf–rçvV&†ööµ÷6V7&WEÒ¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢'†÷F÷2÷&V6V—Bæ§r'×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"÷7F'B"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦'W—Æã§·6VÆbçÆâç·Ò"’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W—G“£"Â6ÆÆ&6µö–CÒ'G’Ö6""’¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²‚'W6W#¦'W•ö6öæf—&Ò"Â6ÆÆ&6µö–CÒ&6öæf—&ÒÖ6""’¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢"À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢·²&f–ÆUö–B#¢'&V6V—BÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'&V6V—B'ÕÒÀ¢Ð¢Ð¢ ¢÷&FW"Ò÷&FW"æö&¦V7G2ævWB‚¢FVÆVw&Õö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò7W7FöÖW"Ö—'&÷""À¢&÷E÷Fö¶VãÒ'FVÆVw&Ò×Fö¶VâÓ""À¢FÖ–å÷W6W%ö–CÒ#ƒƒ‚"À¢—5ö7F—fSÕG'VRÀ¢æ÷F–g•ö÷&FW%÷WFFW3ÔfÇ6RÀ¢¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×FVÆVw&Õö6öæf–rÀ¢7W7FöÖW#Ö÷&FW"æ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ%FVÆVw&ÒÆ–6R"À¢ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&6ÆÆ&6µ÷VW'’#¢°¢&–B#¢&&÷fRÖ6""À¢&g&öÒ#¢²&–B#¢““’Â'W6W&æÖR#¢&FÖ–â'ÒÀ¢&ÖW76vR#¢²&ÖW76vUö–B#¢#Â&6†B#¢²&–B#¢““’Â'G—R#¢'&—fFR'×ÒÀ¢&FF#¢b&&÷fS§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"À¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢÷&FW"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†÷&FW"ç7FGW2Â÷&FW"å7FGW2ä4ôÕÄUDTB¢7W7FöÖW%öÖW76vW2Ò°¢6ÆÀ¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢–b6ÆÅ²'W&Â%Òç7F'G7v—F‚‚&‡GG3¢ò÷F’æ&ÆRæ’"¢æB6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"¢æB6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢Ð¢6VÆbæ76W'EG'VR†ç’‚-‹=‹˜¸Í‹2‹M˜]ŠrŠ-˜]Š}Šý˜r‹MŠò"–â6ÆÅ²&§6öâ%Õ²'FW‡B%Òf÷"6ÆÂ–â7W7FöÖW%öÖW76vW2’¢6öæf–uöÖW76vW2Ò¶6ÆÂf÷"6ÆÂ–â7W7FöÖW%öÖW76vW2–b'fÆW73¢òöW†×ÆR"–â6ÆÅ²&§6öâ%Õ²'FW‡B%ÕÐ¢6VÆbæ76W'DWVÂ†ÆVâ†6öæf–uöÖW76vW2’Â¢6öæf–u÷–ÆöBÒ6öæf–uöÖW76vW5³Õ²&§6öâ%Ð¢6VÆbæ76W'D–â‚&‡GG3¢òöW†×ÆRæ6öÒ÷7V"÷7V##2"Â6öæf–u÷–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöâævWB‚&6ÆÆ&6µöFF"Â""¢f÷"&÷r–â6öæf–u÷–ÆöBævWB‚'&WÇ•öÖ&·W"Â·Ò’ævWB‚&–æÆ–æUö¶W–&ö&B"ÂµÒ¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'EG'VR†ç’‡fÇVRç7F'G7v—F‚‚'W6W#¦6÷•ö6öæf–s§7V#¢"’f÷"fÇVR–â6ÆÆ&6µ÷fÇVW2’¢6VÆbæ76W'EG'VR†ç’‡fÇVRç7F'G7v—F‚‚'W6W#¦6÷•ö6öæf–s¦F—&V7C¢"’f÷"fÇVR–â6ÆÆ&6µ÷fÇVW2’¢6VÆbæ76W'DfÇ6R†ç’‚'fÆW73¢òöW†×ÆR"–âfÇVRf÷"fÇVR–â6ÆÆ&6µ÷fÇVW2’¢6VÆbæ76W'DfÇ6R†ç’‚&‡GG3¢òöW†×ÆRæ6öÒ÷7V"÷7V##2"–âfÇVRf÷"fÇVR–â6ÆÆ&6µ÷fÇVW2’¢6VÆbæ76W'DfÇ6R€¢ç’€¢&6÷•÷FW‡B"–â'WGFöà¢f÷"6ÆÂ–â7W7FöÖW%öÖW76vW0¢f÷"&÷r–â6ÆÅ²&§6öâ%ÒævWB‚'&WÇ•öÖ&·W"Â·Ò’ævWB‚&–æÆ–æUö¶W–&ö&B"ÂµÒ¢f÷"'WGFöâ–â&÷p¢¢¢6VÆbæ76W'DfÇ6R†ç’†6ÆÅ²'W&Â%Òç7F'G7v—F‚‚&‡GG3¢òö’çFVÆVw&Òæ÷&r"’f÷"6ÆÂ–â÷7Eö6ÆÇ2’ ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6W%ö6åöÆ—7EöæEö÷Våö&÷Eö÷&FW'2‡6VÆbÂ÷7EöÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2åTäD”ärÀ¢ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚"ö÷&FW'2"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-‹=˜Š}‹‹N(Í˜}Š}¸Â˜]˜b"Â–ÆöE²'FW‡B%Ò¢6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b'W6W#¦÷&FW#§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"Â6ÆÆ&6µ÷fÇVW2 ¢FWF–Å÷&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦÷&FW#§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"’ ¢6VÆbæ76W'DWVÂ†FWF–Å÷&W7öç6Rç7FGW5ö6öFRÂ#¢FWF–Å÷–ÆöBÒ÷7EöÖö6²æ6ÆÅö&w2æ·v&w5²&§6öâ%Ð¢6VÆbæ76W'D–â‚-ŠÍ‹-Šm¸ÍŠ}Š¢‹=˜Š}‹‹B"ÂFWF–Å÷–ÆöE²'FW‡B%Ò¢6VÆbæ76W'D–â†÷&FW"æ÷&FW%÷G&6¶–æuö6öFRÂFWF–Å÷–ÆöE²'FW‡B%Ò¢FWF–Åö6ÆÆ&6µ÷fÇVW2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–âFWF–Å÷–ÆöE²'&WÇ•öÖ&·W%Õ²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b'W6W#¦÷&FW%ö6æ6VÃ§¶÷&FW"æ÷&FW%÷G&6¶–æuö6öFWÒ"ÂFWF–Åö6ÆÆ&6µ÷fÇVW2 ¢F6‚‚'7F÷&Ræ&÷G2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‚’¢FVbFW7E÷W6W%ö6å÷&Vg&W6…ö6öæf–uög&öÕ÷‡V•ö–åö&÷B‡6VÆbÂ÷7EöÖö6²Â7FG5öÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢WV–CÒ#SSSSSSSRÓSSSRÓCSSRÓƒSSRÓSSSSSSSSSSSR"À¢7V%öÆ–æ³Ò&‡GG3¢òööÆBæW†×ÆR÷7V"ööÆB"À¢F—&V7EöÆ–æ³Ò'fÆW73¢òööÆB"À¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢‡V•öVÖ–ÃÒ&Æ–6Uóv""À¢WV–CÖ÷&FW"çWV–BÀ¢7V%ö–CÒ&öÆB"À¢7V%öÆ–æ³Ö÷&FW"ç7V%öÆ–æ²À¢F—&V7EöÆ–æ³Ö÷&FW"æF—&V7EöÆ–æ²À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢GW&F–öåöF—3×6VÆbçÆâæGW&F–öåöF—2À¢FWf–6UöÆ–Ö—C×6VÆbçÆâæFWf–6UöÆ–Ö—BÀ¢¢7FG5öÖö6²ç&WGW&å÷fÇVRÒ°¢'æVÅöf–Æ&ÆR#¢G'VRÀ¢'F÷FÅ÷G&ff–5ö'—FW2#¢#B¢¢2À¢'W6VE÷G&ff–5ö'—FW2#¢#Sb¢#B¢#BÀ¢'&VÖ–æ–æu÷G&ff–5ö'—FW2#¢ƒ#B¢¢2’Òƒ#Sb¢#B¢#B’À¢&W‡—'•öB#¢F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó#’À¢Ð ¢FVb&Vg&W6…÷6–FUöVffV7B†6Æ–VçB“ ¢6Æ–VçBç7V%öÆ–æ²Ò&‡GG3¢òöæWræW†×ÆR÷7V"ög&W6‚ ¢6Æ–VçBæF—&V7EöÆ–æ²Ò'fÆW73¢òög&W6‚Ö6öæf–r ¢6Æ–VçBç6fR‡WFFUöf–VÆG3Õ²'7V%öÆ–æ²"Â&F—&V7EöÆ–æ²"Â'WFFVEöB%Ò¢6Æ–VçBæ÷&FW"ç7V%öÆ–æ²Ò6Æ–VçBç7V%öÆ–æ°¢6Æ–VçBæ÷&FW"æF—&V7EöÆ–æ²Ò6Æ–VçBæF—&V7EöÆ–æ°¢6Æ–VçBæ÷&FW"ç6fR‡WFFUöf–VÆG3Õ²'7V%öÆ–æ²"Â&F—&V7EöÆ–æ²"Â'WFFVEöB%Ò¢&WGW&â°¢'7V%öÆ–æ²#¢6Æ–VçBç7V%öÆ–æ²À¢&F—&V7EöÆ–æ²#¢6Æ–VçBæF—&V7EöÆ–æ²À¢Ð ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&Vg&W6…÷gåö6Æ–VçEöÆ–æ·2"Â6–FUöVffV7C×&Vg&W6…÷6–FUöVffV7B’2&Vg&W6…öÖö6³ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦6Æ–VçE÷&Vg&W6ƒ§·gåö6Æ–VçBçV&Æ–5ö–GÒ"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&Vg&W6…öÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢7FG5öÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢6VçE÷FW‡G2Ò°¢6ÆÂæ·v&w5²&§6öâ%Õ²'FW‡B%Ð¢f÷"6ÆÂ–â÷7EöÖö6²æ6ÆÅö&w5öÆ—7@¢–b6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢æB'FW‡B"–â6ÆÂæ·v&w2ævWB‚&§6öâ"Â·Ò¢Ð¢6VÆbæ76W'EG'VR†ç’‚-ªŠ}˜m˜¸ÍªòŠ‹˜‹-‹‹=Š}˜m¸Â‹MŠò"–âFW‡Bf÷"FW‡B–â6VçE÷FW‡G2’¢6öæf–u÷FW‡BÒæW‡B‡FW‡Bf÷"FW‡B–â6VçE÷FW‡G2–b&‡GG3¢òöæWræW†×ÆR÷7V"ög&W6‚"–âFW‡B¢6VÆbæ76W'D–â‚'fÆW73¢òög&W6‚Ö6öæf–r"Â6öæf–u÷FW‡B ¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ævWB"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R†6öçFVçCÖ–ÖvUö'—FW2‚$¥Tr"’’¢FVbFW7E÷W6W%ö6åö7&VFU÷&VæWvÅö÷&FW%ög&öÕö&÷B‡6VÆbÂövWEöÖö6²“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6R"¢&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢WV–CÒ#“““““““’Ó“““’ÓC““’Óƒ““’Ó“““““““““““’"À¢7V%öÆ–æ³Ò&‡GG3¢òöW†×ÆRæ6öÒ÷7V"ööÆB"À¢F—&V7EöÆ–æ³Ò'fÆW73¢òööÆB"À¢¢gåö6Æ–VçBÒeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&Æ–6Uóv""À¢‡V•öVÖ–ÃÒ&Æ–6Uóv""À¢WV–CÖ÷&FW"çWV–BÀ¢7V%ö–CÒ&öÆB"À¢7V%öÆ–æ³Ö÷&FW"ç7V%öÆ–æ²À¢F—&V7EöÆ–æ³Ö÷&FW"æF—&V7EöÆ–æ²À¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ó#B¢¢2À¢GW&F–öåöF—3×6VÆbçÆâæGW&F–öåöF—2À¢FWf–6UöÆ–Ö—C×6VÆbçÆâæFWf–6UöÆ–Ö—BÀ¢¢÷7Eö6ÆÇ2ÒµÐ ¢FVb÷7E÷6–FUöVffV7B‡W&ÂÂ§6öãÔæöæRÂFFÔæöæRÂ¢¦·v&w2“ ¢÷7Eö6ÆÇ2æVæB‡²'W&Â#¢W&ÂÂ&§6öâ#¢§6öâÂ&FF#¢FFÂ¢¦·v&w7Ò¢–bW&ÂæVæG7v—F‚‚"övWDf–ÆR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&f–ÆU÷F‚#¢'†÷F÷2÷&VæWvÂæ§r'×Ò¢–bW&ÂæVæG7v—F‚‚"÷6VæE†÷Fò"’÷"W&ÂæVæG7v—F‚‚"÷6VæDÖW76vR"“ ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&ÖW76vUö–B#¢3#×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‚ ¢v—F‚F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"Â6–FUöVffV7C×÷7E÷6–FUöVffV7B“ ¢6VÆbç÷7E÷WFFR‡6VÆbæ6ÆÆ&6²†b'W6W#¦6Æ–VçE÷&VæWs§·gåö6Æ–VçBçV&Æ–5ö–GÒ"’¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2ævWB†&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÂ&÷f–FW%÷W6W%ö–CÒ#C""¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•EôäÔR¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFUöFF²&fÆ÷r%ÒÂ'&VæWvÂ" ¢6VÆbç÷7E÷WFFR‡6VÆbæÖW76vR‚$Æ–6R'W–W""ÂÖW76vUö–CÓ"’¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä%U•õt•Eõ$T4T•B ¢v—F‚6VÆbæ6GW&Töä6öÖÖ—D6ÆÆ&6·2†W†V7WFSÕG'VR“ ¢&W7öç6RÒ6VÆbç÷7E÷WFFR€¢°¢&ÖW76vR#¢°¢&ÖW76vUö–B#¢2À¢&g&öÒ#¢²&–B#¢C"Â'W6W&æÖR#¢&Æ–6R"Â&f—'7EöæÖR#¢$Æ–6R'ÒÀ¢&6†B#¢²&–B#¢C"Â'G—R#¢'&—fFR'ÒÀ¢'†÷Fò#¢·²&f–ÆUö–B#¢'&VæWvÂÖf–ÆR"Â&f–ÆU÷Væ—VUö–B#¢'&VæWvÂ'ÕÒÀ¢Ð¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢&VæWvÂÒ÷&FW"æö&¦V7G2æW†6ÇVFR‡³Ö÷&FW"ç²’ævWB‚¢6VÆbæ76W'DWVÂ‡&VæWvÂæ7W7FöÖW"Â7W7FöÖW"¢6VÆbæ76W'DWVÂ‡&VæWvÂç7FGW2Â÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâ¢6VÆbæ76W'DWVÂ‡&VæWvÂç6VæFW%ö6&EöæÖRÂ$Æ–6R'W–W""¢6VÆbæ76W'DWVÂ‡&VæWvÂç6VæFW%ö6&EöÆ7CBÂ""¢6VÆbæ76W'DWVÂ‡&VæWvÂæÖWFFF²'6÷W&6R%ÒÂ'FVÆVw&Õö&÷E÷&VæWvÂ"¢6VÆbæ76W'DWVÂ‡&VæWvÂæÖWFFF²'&VæWvÅö6Æ–VçE÷²%ÒÂgåö6Æ–VçBç²¢6VÆbæ76W'Dæ÷D–â‚'7W&W75öæWuö÷&FW%öæ÷F–f–6F–öâ"Â&VæWvÂæÖWFFF¢6VÆbæ76W'EG'VR‡&VæWvÂç–ÖVçE÷&V6V—Eö–ÖvRææÖRæVæG7v—F‚‚"æ§r"’¢6VÆbæ76W'DWVÂ…eä6Æ–VçBæö&¦V7G2æ6÷VçB‚’Â¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†&÷E÷W6W"ç7FFRÂ&÷EW6W"å7FFRä”DÄR¢6VÆbæ76W'EG'VR†ç’†6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæE†÷Fò"’f÷"6ÆÂ–â÷7Eö6ÆÇ2’¢W6W%öÖW76vW2Ò°¢6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚'FW‡B"Â""¢f÷"6ÆÂ–â÷7Eö6ÆÇ0¢–b6ÆÅ²'W&Â%ÒæVæG7v—F‚‚"÷6VæDÖW76vR"’æB6ÆÂævWB‚&§6öâ"Â·Ò’ævWB‚&6†Eö–B"’ÓÒ#C" ¢Ð¢6VÆbæ76W'EG'VR†ç’‚-Šý‹Ší˜Š}‹=Š¢Š­˜]Šý¸ÍŠò‹M˜]ŠrŠ½ŠŠ¢‹MŠò"–âFW‡Bf÷"FW‡B–âW6W%öÖW76vW2’  ¦6Æ72&VæWvÅ&VÖ–æFW%6W'f–6UFW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢66†Ræ6ÆV"‚¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ%eâ7F÷&R"À¢VævÆ—6…öæÖSÒ%eâ7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ#t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚#"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢FWf–6UöÆ–Ö—CÓ"À¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢&VÖ&³Ò&Ö–â"À¢&÷Fö6öÃÔ–æ&÷VæBå&÷Fö6öÂådÄU52À¢6W'fW%ö—Ò'gâæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'6V7W&—G“×&VÆ—G’"À¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6R"¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò"À¢&÷E÷Fö¶VãÒ##3§Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢¢6VÆbæ&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢¢6VÆbçgåö6Æ–VçBÒ6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó2’ ¢FVbÖ¶Uö6Æ–VçB‡6VÆbÂ¢Â7W7FöÖW#ÔæöæRÂW‡—&W5öCÔæöæRÂ7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÂW6VEöv#ÓÂF÷FÅöv#Ó“ ¢7W7FöÖW"Ò7W7FöÖW"÷"6VÆbæ7W7FöÖW ¢6Æ–VçEö–æFW‚Òeä6Æ–VçBæö&¦V7G2æ6÷VçB‚’²¢WV–BÒb#ÓÓCÓƒ×¶6Æ–VçEö–æFWƒ£&GÒ ¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#Ö7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÖb&Æ–6U÷¶6Æ–VçEö–æFW‡Ò"À¢WV–C×WV–BÀ¢¢F÷FÂÒ–çB„FV6–ÖÂ‡7G"‡F÷FÅöv"’’¢FV6–ÖÂƒ#B¢¢2’¢W6VBÒ–çB„FV6–ÖÂ‡7G"‡W6VEöv"’’¢FV6–ÖÂƒ#B¢¢2’¢&WGW&âeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÖb&Æ–6U÷¶6Æ–VçEö–æFW‡Ò"À¢‡V•öVÖ–ÃÖb&Æ–6U÷¶6Æ–VçEö–æFW‡Ò"À¢WV–C×WV–BÀ¢7FGW3×7FGW2À¢G&ff–5öÆ–Ö—Eö'—FW3×F÷FÂÀ¢W6VE÷G&ff–5ö'—FW3×W6VBÀ¢GW&F–öåöF—3×6VÆbçÆâæGW&F–öåöF—2À¢FWf–6UöÆ–Ö—C×6VÆbçÆâæFWf–6UöÆ–Ö—BÀ¢W‡—&W5öCÖW‡—&W5öB÷"F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó3’À¢Æ7E÷7–æ6VEöC×F–ÖW¦öæRææ÷r‚’À¢ ¢FVb‡V•÷7FG2‡6VÆbÂ¢Â6Æ–VçCÔæöæRÂF÷FÅöv#ÓÂ&VÖ–æ–æuöv#Ó’ÂW‡—'•öCÔæöæR“ ¢6Æ–VçBÒ6Æ–VçB÷"6VÆbçgåö6Æ–Vç@¢F÷FÂÒ–çB„FV6–ÖÂ‡7G"‡F÷FÅöv"’’¢FV6–ÖÂƒ#B¢¢2’¢&VÖ–æ–ærÒ–çB„FV6–ÖÂ‡7G"‡&VÖ–æ–æuöv"’’¢FV6–ÖÂƒ#B¢¢2’¢W6VBÒÖ‚‡F÷FÂÒ&VÖ–æ–ærÂ¢&WGW&â°¢'WV–B#¢6Æ–VçBçWV–BÀ¢&VÖ–Â#¢6Æ–VçBç‡V•öVÖ–ÂÀ¢'F÷FÅ÷G&ff–5ö'—FW2#¢F÷FÂÀ¢'W6VE÷WÆöEö'—FW2#¢À¢'W6VEöF÷væÆöEö'—FW2#¢W6VBÀ¢'W6VE÷G&ff–5ö'—FW2#¢W6VBÀ¢'&VÖ–æ–æu÷G&ff–5ö'—FW2#¢&VÖ–æ–ærÀ¢&W‡—'•öB#¢W‡—'•öB÷"6Æ–VçBæW‡—&W5öBÀ¢&—5öVæ&ÆVB#¢G'VRÀ¢'æVÅöf–Æ&ÆR#¢G'VRÀ¢'&r#¢°¢'G&ff–2#¢²'W#¢Â&F÷vâ#¢W6VBÂ'F÷FÂ#¢F÷FÇÒÀ¢&6Æ–VçB#¢²&–B#¢6Æ–VçBçWV–BÂ&VÖ–Â#¢6Æ–VçBç‡V•öVÖ–ÂÂ'F÷FÄt"#¢F÷FÇÒÀ¢ÒÀ¢Ð ¢FVbÆ—fU÷W6vR‡6VÆbÂ¢Â6Æ–VçCÔæöæRÂF÷FÅöv#ÓÂ&VÖ–æ–æuöv#Ó’ÂW‡—'•öCÔæöæRÂW6vUö¶æ÷vãÕG'VR“ ¢7FG2Ò6VÆbç‡V•÷7FG2†6Æ–VçCÖ6Æ–VçBÂF÷FÅöv#×F÷FÅöv"Â&VÖ–æ–æuöv#×&VÖ–æ–æuöv"ÂW‡—'•öCÖW‡—'•öB¢7FG5²'W6vUö¶æ÷vâ%ÒÒW6vUö¶æ÷và¢7FG5²'6÷W&6R%ÒÒ'‡V’"–bW6vUö¶æ÷vâVÇ6R'Væ¶æ÷vâ ¢&WGW&â7FG0 ¢FVbFV6—6–öç5öf÷"‡6VÆbÂ6Æ–VçBÂ¢Âæ÷sÔæöæRÂÆ—fU÷W6vSÔæöæR“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B6Æ7VÆFUö6Æ–VçE÷&VÖ–æFW%÷7FGW2ÂvWE÷&VÖ–æFW%÷6WGF–æw0 ¢&WGW&â°¢†FV6—6–öâç&VÖ–æFW%÷G—RÂFV6—6–öâçG&–vvW%ö¶W’¢f÷"FV6—6–öâ–â6Æ7VÆFUö6Æ–VçE÷&VÖ–æFW%÷7FGW2€¢6Æ–VçBÀ¢Æ—fU÷W6vR÷"6VÆbæÆ—fU÷W6vR†6Æ–VçCÖ6Æ–VçB’À¢6WGF–æw3ÖvWE÷&VÖ–æFW%÷6WGF–æw2‡6VÆbç7F÷&R’À¢æ÷sÖæ÷r÷"F–ÖW¦öæRææ÷r‚’À¢¢Ð ¢FVbFW7E÷7F÷&U÷&VÖ–æFW%÷6WGF–æw5÷fÆ–FF–öâ‡6VÆb“ ¢7F÷&RÒ7F÷&R€¢æÖSÒ$&B"À¢VævÆ—6…öæÖSÒ$&B"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢&VÖ–æFW%öF—5ö&Vf÷&UöW‡—'“Õ²'6ööâ%ÒÀ¢Æ÷u÷G&ff–5÷W&6VçE÷F‡&W6†öÆCÓÀ¢Æ÷u÷G&ff–5öv%÷F‡&W6†öÆCÔFV6–ÖÂ‚#"’À¢&VÖ–æFW%ö6ööÆF÷våö†÷W'3ÓÀ¢¢v—F‚6VÆbæ76W'E&—6W2…fÆ–FF–öäW'&÷"’27Gƒ ¢7F÷&RægVÆÅö6ÆVâ‚ ¢6VÆbæ76W'D–â‚'&VÖ–æFW%öF—5ö&Vf÷&UöW‡—'’"Â7G‚æW†6WF–öâæW'&÷%öF–7B¢6VÆbæ76W'D–â‚&Æ÷u÷G&ff–5÷W&6VçE÷F‡&W6†öÆB"Â7G‚æW†6WF–öâæW'&÷%öF–7B¢6VÆbæ76W'D–â‚&Æ÷u÷G&ff–5öv%÷F‡&W6†öÆB"Â7G‚æW†6WF–öâæW'&÷%öF–7B¢6VÆbæ76W'D–â‚'&VÖ–æFW%ö6ööÆF÷våö†÷W'2"Â7G‚æW†6WF–öâæW'&÷%öF–7B ¢FVbFW7EöFVÆWFVEö6Æ–VçG5ö&Uöæ÷E÷&VÖ–æFW%ö6æF–FFW2‡6VÆb“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'BvWEö7F—fUö6Æ–VçG5öf÷%÷&VÖ–æFW'0 ¢6VÆbçgåö6Æ–VçBæÖ&µöFVÆWFVB†7W7FöÖW#×6VÆbæ7W7FöÖW"Â&V6öãÒ'FW7B6ÆVçW"¢6VÆbçgåö6Æ–VçBç6fR€¢WFFUöf–VÆG3Õ°¢'7FGW2"À¢&FVÆWFVEöB"À¢&FVÆWFVEö'•ö7W7FöÖW""À¢&FVÆWFU÷&V6öâ"À¢'&VÖ÷FUöFVÆWFVEöB"À¢&F—6&ÆVEöB"À¢'7V%öÆ–æ²"À¢&F—&V7EöÆ–æ²"À¢'WFFVEöB"À¢Ð¢ ¢6VÆbæ76W'DfÇ6R†vWEö7F—fUö6Æ–VçG5öf÷%÷&VÖ–æFW'2‡7F÷&S×6VÆbç7F÷&R’æf–ÇFW"‡³×6VÆbçgåö6Æ–VçBç²’æW†—7G2‚’ ¢FVbFW7E÷6VÆV7G5öW‡—'•÷&VÖ–æFW%÷F‡&VUöF—5ö&Vf÷&R‡6VÆb“ ¢æ÷rÒF–ÖW¦öæRææ÷r‚¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÒæ÷r²F–ÖVFVÇF†F—3Ó2¢6VÆbæ76W'D–â€¢…eä6Æ–VçE&VÖ–æFW$Æörå&VÖ–æFW%G—RäU…•%•ô$Tdõ$RÂ&&Vf÷&Uó6B"’À¢6VÆbæFV6—6–öç5öf÷"‡6VÆbçgåö6Æ–VçBÂæ÷sÖæ÷rÂÆ—fU÷W6vS×6VÆbæÆ—fU÷W6vR†W‡—'•öC×6VÆbçgåö6Æ–VçBæW‡—&W5öB’’À¢ ¢FVbFW7E÷6VÆV7G5öW‡—'•÷&VÖ–æFW%ööæUöF•ö&Vf÷&R‡6VÆb“ ¢æ÷rÒF–ÖW¦öæRææ÷r‚¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÒæ÷r²F–ÖVFVÇF†F—3Ó¢6VÆbæ76W'D–â€¢…eä6Æ–VçE&VÖ–æFW$Æörå&VÖ–æFW%G—RäU…•%•ô$Tdõ$RÂ&&Vf÷&UóB"’À¢6VÆbæFV6—6–öç5öf÷"‡6VÆbçgåö6Æ–VçBÂæ÷sÖæ÷rÂÆ—fU÷W6vS×6VÆbæÆ—fU÷W6vR†W‡—'•öC×6VÆbçgåö6Æ–VçBæW‡—&W5öB’’À¢ ¢FVbFW7E÷6VÆV7G5öW‡—'•÷&VÖ–æFW%ööåöW‡—'•öF’‡6VÆb“ ¢æ÷rÒF–ÖW¦öæRæÖ¶Uöv&R†FFWF–ÖRƒ##bÂbÂBÂ"Â’ÂF–ÖW¦öæRævWEö7W'&VçE÷F–ÖW¦öæR‚’¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÒæ÷r²F–ÖVFVÇF††÷W'3Ó"¢6VÆbæ76W'D–â€¢…eä6Æ–VçE&VÖ–æFW$Æörå&VÖ–æFW%G—RäU…•%•õDôD’Â'FöF’"’À¢6VÆbæFV6—6–öç5öf÷"‡6VÆbçgåö6Æ–VçBÂæ÷sÖæ÷rÂÆ—fU÷W6vS×6VÆbæÆ—fU÷W6vR†W‡—'•öC×6VÆbçgåö6Æ–VçBæW‡—&W5öB’’À¢ ¢FVbFW7E÷6VÆV7G5öW‡—'•÷&VÖ–æFW%ögFW%öW‡—'’‡6VÆb“ ¢æ÷rÒF–ÖW¦öæRææ÷r‚¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÒæ÷rÒF–ÖVFVÇF†F—3Ó¢6VÆbæ76W'D–â€¢…eä6Æ–VçE&VÖ–æFW$Æörå&VÖ–æFW%G—RäU…•%•ôeDU"Â&gFW%óB"’À¢6VÆbæFV6—6–öç5öf÷"‡6VÆbçgåö6Æ–VçBÂæ÷sÖæ÷rÂÆ—fU÷W6vS×6VÆbæÆ—fU÷W6vR†W‡—'•öC×6VÆbçgåö6Æ–VçBæW‡—&W5öB’’À¢ ¢FVbFW7E÷6VÆV7G5öÆ÷u÷G&ff–5ö'•÷W&6VçE÷F‡&W6†öÆB‡6VÆb“ ¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÒF–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó¢6VÆbæ76W'D–â€¢…eä6Æ–VçE&VÖ–æFW$Æörå&VÖ–æFW%G—RäÄõuõE$dd”2Â&Æ÷u÷G&ff–5ó#7B"’À¢6VÆbæFV6—6–öç5öf÷"‡6VÆbçgåö6Æ–VçBÂÆ—fU÷W6vS×6VÆbæÆ—fU÷W6vR‡F÷FÅöv#ÓÂ&VÖ–æ–æuöv#Ó’’À¢ ¢FVbFW7E÷6VÆV7G5öÆ÷u÷G&ff–5ö'•öv%÷F‡&W6†öÆB‡6VÆb“ ¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÒF–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó¢6VÆbæ76W'D–â€¢…eä6Æ–VçE&VÖ–æFW$Æörå&VÖ–æFW%G—RäÄõuõE$dd”2Â&Æ÷u÷G&ff–5ó&v""’À¢6VÆbæFV6—6–öç5öf÷"‡6VÆbçgåö6Æ–VçBÂÆ—fU÷W6vS×6VÆbæÆ—fU÷W6vR‡F÷FÅöv#ÓRÂ&VÖ–æ–æuöv#ÔFV6–ÖÂ‚#ãR"’’’À¢ ¢FVbFW7E÷Væ¶æ÷våö6Æ–VçE÷7FG5öFõöæ÷E÷6VæEöÆ÷u÷G&ff–5÷&VÖ–æFW"‡6VÆb“ ¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÒF–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó¢W6vRÒ6VÆbæÆ—fU÷W6vR‡F÷FÅöv#ÓÂ&VÖ–æ–æuöv#ÓÂW6vUö¶æ÷vãÔfÇ6R¢W6vU²'&VÖ–æ–æu÷G&ff–5ö'—FW2%ÒÒæöæP¢6VÆbæ76W'DWVÂ‡6VÆbæFV6—6–öç5öf÷"‡6VÆbçgåö6Æ–VçBÂÆ—fU÷W6vS×W6vR’ÂµÒ ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â&WGW&å÷fÇVS×²&ö²#¢G'VWÒ¢FVbFW7E÷Væ¶æ÷våöÆ—fU÷7FG5öfÆÅö&6µ÷Fõ÷fÆ–EöÆö6Å÷W6vR‡6VÆbÂ÷6VæEöÖö6²Â7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÒF–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó¢6VÆbçgåö6Æ–VçBçW6VE÷G&ff–5ö'—FW2Ò–çB„FV6–ÖÂ‚#’"’¢FV6–ÖÂƒ#B¢¢2’¢6VÆbçgåö6Æ–VçBæÆ7E÷7–æ6VEöBÒF–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó¢6VÆbçgåö6Æ–VçBç6fR‡WFFUöf–VÆG3Õ²&W‡—&W5öB"Â'W6VE÷G&ff–5ö'—FW2"Â&Æ7E÷7–æ6VEöB"Â'WFFVEöB%Ò¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ°¢'F÷FÅ÷G&ff–5ö'—FW2#¢–çB„FV6–ÖÂ‚#"’¢FV6–ÖÂƒ#B¢¢2’’À¢'W6VE÷G&ff–5ö'—FW2#¢À¢'&VÖ–æ–æu÷G&ff–5ö'—FW2#¢–çB„FV6–ÖÂ‚#"’¢FV6–ÖÂƒ#B¢¢2’’À¢&W‡—'•öB#¢6VÆbçgåö6Æ–VçBæW‡—&W5öBÀ¢&—5öVæ&ÆVB#¢G'VRÀ¢'æVÅöf–Æ&ÆR#¢G'VRÀ¢'&r#¢²'G&ff–2#¢·ÒÂ&6Æ–VçB#¢·×ÒÀ¢Ð ¢7VÖÖ'’Ò'Vå÷&VæWvÅ÷&VÖ–æFW'2‡&VÖ–æFW%÷G—SÒ'G&ff–2" ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢ÆörÒeä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†Æörç&VÖ–æFW%÷G—RÂeä6Æ–VçE&VÖ–æFW$Æörå&VÖ–æFW%G—RäÄõuõE$dd”2 ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â&WGW&å÷fÇVS×²&ö²#¢G'VWÒ¢FVbFW7EöGWÆ–6FU÷G&–vvW%ö—5öæ÷E÷6VçE÷Gv–6R‡6VÆbÂ6VæEöÖö6²Â7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’¢f—'7BÒ'Vå÷&VæWvÅ÷&VÖ–æFW'2‚¢6V6öæBÒ'Vå÷&VæWvÅ÷&VÖ–æFW'2‚ ¢6VÆbæ76W'DWVÂ†f—'7E²'6VçB%ÒÂ¢6VÆbæ76W'DWVÂ‡6V6öæE²'6¶—VB%ÒÂ¢6VÆbæ76W'DWVÂ‡6VæEöÖö6²æ6ÆÅö6÷VçBÂ¢6VÆbæ76W'DWVÂ…eä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2æf–ÇFW"‡7FGW3Õeä6Æ–VçE&VÖ–æFW$Æörå7FGW2å4TåB’æ6÷VçB‚’Â ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢FVbFW7Eöf–ÆVEöFVÆ—fW'•÷&WG&–W5ögFW%ö6ööÆF÷vâ‡6VÆbÂ7–æ5öÖö6²“ ¢g&öÒæ&÷G2–×÷'B&÷DFVÆ—fW'”W'&÷ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’¢v—F‚F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â6–FUöVffV7CÔ&÷DFVÆ—fW'”W'&÷"‚&&ööÒ"’’26VæEöÖö6³ ¢f—'7BÒ'Vå÷&VæWvÅ÷&VÖ–æFW'2‚¢6V6öæBÒ'Vå÷&VæWvÅ÷&VÖ–æFW'2‚ ¢6VÆbæ76W'DWVÂ†f—'7E²&f–ÆVB%ÒÂ¢6VÆbæ76W'DWVÂ‡6V6öæE²'6¶—VB%ÒÂ¢6VÆbæ76W'DWVÂ‡6VæEöÖö6²æ6ÆÅö6÷VçBÂ¢ÆörÒeä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†Æörç7FGW2Âeä6Æ–VçE&VÖ–æFW$Æörå7FGW2äd”ÄTB¢6VÆbæ76W'DWVÂ†ÆöræW'&÷%öÖW76vRÂ&&ööÒ" ¢eä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2æf–ÇFW"‡³ÖÆörç²’çWFFR‡WFFVEöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó#R’¢v—F‚F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â&WGW&å÷fÇVS×²&ö²#¢G'VWÒ’2&WG'•öÖö6³ ¢F†—&BÒ'Vå÷&VæWvÅ÷&VÖ–æFW'2‚ ¢6VÆbæ76W'DWVÂ‡F†—&E²'6VçB%ÒÂ¢6VÆbæ76W'DWVÂ‡&WG'•öÖö6²æ6ÆÅö6÷VçBÂ¢Æörç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†Æörç7FGW2Âeä6Æ–VçE&VÖ–æFW$Æörå7FGW2å4TåB ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â&WGW&å÷fÇVS×²&ö²#¢G'VWÒ¢FVbFW7Eö7W7FöÖW%÷v—F†÷WE÷FVÆVw&Õö–Eö—5÷6¶—VB‡6VÆbÂ6VæEöÖö6²Â7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢&÷EW6W"æö&¦V7G2æÆÂ‚’æFVÆWFR‚¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’¢7VÖÖ'’Ò'Vå÷&VæWvÅ÷&VÖ–æFW'2‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6¶—VB%ÒÂ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢ÆörÒeä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†Æörç7FGW2Âeä6Æ–VçE&VÖ–æFW$Æörå7FGW2å4´•TB¢6VÆbæ76W'D–â‚$æò7F—fRFVÆVw&Ò"ÂÆöræW'&÷%öÖW76vR ¢FVbFW7E÷vV%÷FVÆVw&ÕöÆ–æµöFG5÷F&vWEöf÷%ö7W7FöÖW%÷gåö6Æ–VçB‡6VÆb“ ¢g&öÒæ&÷E÷F&vWG2–×÷'BvWE÷gåö6Æ–VçE÷FVÆVw&Õ÷F&vWG0¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶VâÂÆ–æµö&÷E÷W6W%÷Fõö7W7FöÖW  ¢&÷EW6W"æö&¦V7G2æÆÂ‚’æFVÆWFR‚¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢&÷f–FW%÷W6W%ö–CÒ#ƒB"À¢6†Eö–CÒ#ƒB"À¢W6W&æÖSÒ'vV'W6W""À¢F—7Æ•öæÖSÒ%vV"W6W""À¢¢6VÆbæ76W'DWVÂ†vWE÷gåö6Æ–VçE÷FVÆVw&Õ÷F&vWG2‡6VÆbçgåö6Æ–VçBÂ7F÷&S×6VÆbç7F÷&R’ÂµÒ¢&u÷Fö¶VâÂ÷Fö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ‡6VÆbæ7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B" ¢&W7VÇBÒÆ–æµö&÷E÷W6W%÷Fõö7W7FöÖW"‡&u÷Fö¶VâÂ&÷E÷W6W" ¢6VÆbæ76W'EG'VR‡&W7VÇBç7V66W72¢F&vWG2ÒvWE÷gåö6Æ–VçE÷FVÆVw&Õ÷F&vWG2‡6VÆbçgåö6Æ–VçBÂ7F÷&S×6VÆbç7F÷&R¢6VÆbæ76W'DWVÂ†ÆVâ‡F&vWG2’Â¢6VÆbæ76W'DWVÂ‡F&vWG5³Òæ6†Eö–BÂ#ƒB" ¢FVbFW7EöÆ–æ¶VE÷vV%ö7W7FöÖW%öÆÅ÷gåö6Æ–VçG5ö&U÷f—6–&ÆU÷Fõö&÷B‡6VÆb“ ¢g&öÒæ&÷G2–×÷'B&÷E÷7V'67&—F–öåö6Æ–VçG0¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶VâÂÆ–æµö&÷E÷W6W%÷Fõö7W7FöÖW  ¢6V6öæEö6Æ–VçBÒ6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó’¢&÷EW6W"æö&¦V7G2æÆÂ‚’æFVÆWFR‚¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢&÷f–FW%÷W6W%ö–CÒ#ƒB"À¢6†Eö–CÒ#ƒB"À¢W6W&æÖSÒ'vV'W6W""À¢F—7Æ•öæÖSÒ%vV"W6W""À¢¢&u÷Fö¶VâÂ÷Fö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ‡6VÆbæ7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B" ¢Æ–æµö&÷E÷W6W%÷Fõö7W7FöÖW"‡&u÷Fö¶VâÂ&÷E÷W6W"¢&÷E÷W6W"ç&Vg&W6…ög&öÕöF"‚ ¢f—6–&ÆUö6Æ–VçEö–G2Ò6WB†&÷E÷7V'67&—F–öåö6Æ–VçG2†&÷E÷W6W"’çfÇVW5öÆ—7B‚'²"ÂfÆCÕG'VR’¢6VÆbæ76W'DWVÂ‡f—6–&ÆUö6Æ–VçEö–G2Â·6VÆbçgåö6Æ–VçBç²Â6V6öæEö6Æ–VçBç·Ò ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â&WGW&å÷fÇVS×²&ö²#¢G'VWÒ¢FVbFW7EöÆ–æ¶VE÷vV%ö7W7FöÖW%÷&VÖ–æFW%ö—5öæ÷E÷6¶—VE÷v—F†÷WE÷F&vWB‡6VÆbÂ6VæEöÖö6²Â7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0¢g&öÒçFVÆVw&ÕöÆ–æµ÷6W'f–6W2–×÷'B7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶VâÂÆ–æµö&÷E÷W6W%÷Fõö7W7FöÖW  ¢&÷EW6W"æö&¦V7G2æÆÂ‚’æFVÆWFR‚¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢&÷f–FW%÷W6W%ö–CÒ#ƒB"À¢6†Eö–CÒ#ƒB"À¢W6W&æÖSÒ'vV'W6W""À¢F—7Æ•öæÖSÒ%vV"W6W""À¢¢&u÷Fö¶VâÂ÷Fö¶VâÒ7&VFU÷vV%÷FVÆVw&ÕöÆ–æµ÷Fö¶Vâ‡6VÆbæ7W7FöÖW"Â6÷W&6SÒ&F6†&ö&B"¢Æ–æµö&÷E÷W6W%÷Fõö7W7FöÖW"‡&u÷Fö¶VâÂ&÷E÷W6W"¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’ ¢7VÖÖ'’Ò'Vå÷&VæWvÅ÷&VÖ–æFW'2‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6¶—VB%ÒÂ¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢6VÆbæ76W'DWVÂ…eä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2ævWB‚’ç6VçE÷Fõ÷FVÆVw&Õö–BÂ#ƒB" ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢FVbFW7E÷7F'Eö&÷VæF'•ö–væ÷&W5ööÆEö6Æ–VçEö&Vf÷&Uö6æF–FFW2‡6VÆbÂ7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢7F'EöBÒF–ÖW¦öæRææ÷r‚¢eä6Æ–VçBæö&¦V7G2æf–ÇFW"‡³×6VÆbçgåö6Æ–VçBç²’çWFFR†7&VFVEöC×7F'EöBÒF–ÖVFVÇF†F—3Ó’¢6VÆbç7F÷&Rç&VæWvÅ÷&VÖ–æFW'5÷7F'EöBÒ7F'Eö@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&VæWvÅ÷&VÖ–æFW'5÷7F'EöB"Â'WFFVEöB%Ò ¢7VÖÖ'’Ò'Vå÷&VæWvÅ÷&VÖ–æFW'2†G'•÷'VãÕG'VR ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'F÷FÅö6Æ–VçG5÷6VVâ%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&–væ÷&VEö&Vf÷&U÷7F'EöB%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&6æF–FFW2%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&GVR%ÒÂ¢7–æ5öÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢FVbFW7E÷7F'Eö&÷VæF'•öÆÆ÷w5öæWuö6Æ–VçEögFW%÷7F'EöB‡6VÆbÂ7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢7F'EöBÒF–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF†Ö–çWFW3Ó¢eä6Æ–VçBæö&¦V7G2æf–ÇFW"‡³×6VÆbçgåö6Æ–VçBç²’çWFFR†7&VFVEöC×7F'EöB²F–ÖVFVÇF‡6V6öæG3Ó’¢6VÆbç7F÷&Rç&VæWvÅ÷&VÖ–æFW'5÷7F'EöBÒ7F'Eö@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&VæWvÅ÷&VÖ–æFW'5÷7F'EöB"Â'WFFVEöB%Ò ¢FVb7FG5öf÷%ö6Æ–VçB‡gåö6Æ–VçBÂ¦&w2Â¢¦·v&w2“ ¢&WGW&â6VÆbç‡V•÷7FG2†6Æ–VçC×gåö6Æ–VçBÂ&VÖ–æ–æuöv#Ó’ÂW‡—'•öC×gåö6Æ–VçBæW‡—&W5öB ¢7–æ5öÖö6²ç6–FUöVffV7BÒ7FG5öf÷%ö6Æ–Vç@¢7VÖÖ'’Ò'Vå÷&VæWvÅ÷&VÖ–æFW'2†G'•÷'VãÕG'VR ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&–væ÷&VEö&Vf÷&U÷7F'EöB%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&6æF–FFW2%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&GVR%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'v÷VÆE÷6VæB%ÒÂ ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢FVbFW7EöG'•÷'Vå÷v—F†÷WE÷FVÆVw&Õ÷F&vWEö—5÷6¶—VE÷v—F†÷WEöÆör‡6VÆbÂ7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢&÷EW6W"æö&¦V7G2æÆÂ‚’æFVÆWFR‚¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’ ¢7VÖÖ'’Ò'Vå÷&VæWvÅ÷&VÖ–æFW'2†G'•÷'VãÕG'VR ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6¶—VB%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'v÷VÆE÷6VæB%ÒÂ¢6VÆbæ76W'DfÇ6R…eä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2æW†—7G2‚’ ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢FVbFW7E÷FVÆVw&Õ÷6VæEöf–ÇW&UöFöW5öæ÷Eöf–Åö6öÖÖæB‡6VÆbÂ7–æ5öÖö6²“ ¢g&öÒæ&÷G2–×÷'B&÷DFVÆ—fW'”W'&÷  ¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’¢÷WBÒ7G&–æt”ò‚¢v—F‚F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â6–FUöVffV7CÔ&÷DFVÆ—fW'”W'&÷"‚&&Æö6¶VB"’“ ¢6ÆÅö6öÖÖæB‚'6VæE÷&VæWvÅ÷&VÖ–æFW'2"Â7FF÷WCÖ÷WB ¢6VÆbæ76W'D–â‚&f–ÆVCÓ"Â÷WBævWGfÇVR‚’¢6VÆbæ76W'DWVÂ…eä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2ævWB‚’ç7FGW2Âeä6Æ–VçE&VÖ–æFW$Æörå7FGW2äd”ÄTB ¢FVbFW7E÷&VÖ–æFW%ö¶W–&ö&Eö6öææV7G5÷FõöW†—7F–æuö6ÆÆ&6·2‡6VÆb“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'V–ÆE÷&VÖ–æFW%ö¶W–&ö&@ ¢¶W–&ö&BÒ'V–ÆE÷&VÖ–æFW%ö¶W–&ö&B‡6VÆbçgåö6Æ–VçB¢6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â¶W–&ö&E²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'D–â†b'W6W#¦6Æ–VçE÷&VæWs§·6VÆbçgåö6Æ–VçBçV&Æ–5ö–GÒ"Â6ÆÆ&6·2¢6VÆbæ76W'D–â†b'W6W#¦6Æ–VçE÷W6vS§·6VÆbçgåö6Æ–VçBçV&Æ–5ö–GÒ"Â6ÆÆ&6·2¢6VÆbæ76W'D–â‚'W6W#¦'W’"Â6ÆÆ&6·2 ¢FVbFW7E÷&VÖ–æFW%ö¶W–&ö&EöæWu÷W&6†6Uö6ÆÆ&6µ÷W6W5ö'W•öfÆ÷r‡6VÆb“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'V–ÆE÷&VÖ–æFW%ö¶W–&ö&@ ¢¶W–&ö&BÒ'V–ÆE÷&VÖ–æFW%ö¶W–&ö&B‡6VÆbçgåö6Æ–VçB¢6ÆÆ&6·2Ò°¢'WGFöå²&6ÆÆ&6µöFF%Ð¢f÷"&÷r–â¶W–&ö&E²&–æÆ–æUö¶W–&ö&B%Ð¢f÷"'WGFöâ–â&÷p¢Ð¢6VÆbæ76W'DWVÂ†6ÆÆ&6·5²ÓÒÂ'W6W#¦'W’" ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â&WGW&å÷fÇVS×²&ö²#¢G'VWÒ¢FVbFW7EöG'•÷'VåöFöW5öæ÷E÷6VæEö÷%÷w&—FUöÆöw2‡6VÆbÂ6VæEöÖö6²Â7–æ5öÖö6²“ ¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚'6VæE÷&VæWvÅ÷&VÖ–æFW'2"Â"ÒÖG'’×'Vâ"Â7FF÷WCÖ÷WB ¢6VÆbæ76W'D–â‚'v÷VÆE÷6VæCÓ"Â÷WBævWGfÇVR‚’¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢6VÆbæ76W'DfÇ6R…eä6Æ–VçE&VÖ–æFW$Æöræö&¦V7G2æW†—7G2‚’ ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â&WGW&å÷fÇVS×²&ö²#¢G'VWÒ¢FVbFW7Eö6öÖÖæE÷7VÖÖ'•÷&W÷'G5ö6÷VçG2‡6VÆbÂ÷6VæEöÖö6²Â7–æ5öÖö6²“ ¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚'6VæE÷&VæWvÅ÷&VÖ–æFW'2"Â"ÒÖÆ–Ö—B"Â#"Â"Ò×G—R"Â&ÆÂ"Â7FF÷WCÖ÷WB ¢÷WGWBÒ÷WBævWGfÇVR‚¢6VÆbæ76W'D–â‚&6æF–FFW3Ó"Â÷WGWB¢6VÆbæ76W'D–â‚&GVSÓ"Â÷WGWB¢6VÆbæ76W'D–â‚'6VçCÓ"Â÷WGWB ¢FVbFW7Eö6öÖÖæE÷7VÖÖ'•÷&W÷'G5÷7F'Eö&÷VæF'•ö6÷VçG2‡6VÆb“ ¢7F'EöBÒF–ÖW¦öæRææ÷r‚¢eä6Æ–VçBæö&¦V7G2æf–ÇFW"‡³×6VÆbçgåö6Æ–VçBç²’çWFFR†7&VFVEöC×7F'EöBÒF–ÖVFVÇF†F—3Ó’¢6VÆbç7F÷&Rç&VæWvÅ÷&VÖ–æFW'5÷7F'EöBÒ7F'Eö@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&VæWvÅ÷&VÖ–æFW'5÷7F'EöB"Â'WFFVEöB%Ò ¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚'6VæE÷&VæWvÅ÷&VÖ–æFW'2"Â"ÒÖG'’×'Vâ"Â7FF÷WCÖ÷WB ¢÷WGWBÒ÷WBævWGfÇVR‚¢6VÆbæ76W'D–â‚'F÷FÅö6Æ–VçG5÷6VVãÓ"Â÷WGWB¢6VÆbæ76W'D–â‚&–væ÷&VEö&Vf÷&U÷7F'EöCÓ"Â÷WGWB¢6VÆbæ76W'D–â‚&6æF–FFW3Ó"Â÷WGWB ¢FVbFW7E÷6WE÷&VæWvÅ÷&VÖ–æFW'5÷7F'Eöæ÷uö6öÖÖæE÷6WG5ö7F—fU÷7F÷&R‡6VÆb“ ¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚'6WE÷&VæWvÅ÷&VÖ–æFW'5÷7F'Eöæ÷r"Â7FF÷WCÖ÷WB ¢6VÆbç7F÷&Rç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'D—4æ÷DæöæR‡6VÆbç7F÷&Rç&VæWvÅ÷&VÖ–æFW'5÷7F'EöB¢6VÆbæ76W'D–â‚'WFFVE÷7F÷&W3Ó"Â÷WBævWGfÇVR‚’ ¢FVbFW7Eö6†V6µö–çFVw&F–öç5÷&W÷'G5÷&VÖ–æFW%÷7FGW2‡6VÆb“ ¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚&6†V6µö–çFVw&F–öç2"Â"ÒÖæòÖf–Â"Â7FF÷WCÖ÷WB ¢÷WGWBÒ÷WBævWGfÇVR‚¢6VÆbæ76W'D–â‚%&VæWvÂ&VÖ–æFW"6WGF–æw2Æöö²fÆ–Bâ"Â÷WGWB¢6VÆbæ76W'D–â‚'6VæE÷&VæWvÅ÷&VÖ–æFW'26öÖÖæB—2f–Æ&ÆRâ"Â÷WGWB¢6VÆbæ76W'D–â‚'&VæWvÅ÷&VÖ–æFW'5÷7F'EöCÖæ÷B6WB"Â÷WGWB ¢FVbFW7Eö6†V6µö–çFVw&F–öç5÷v—F…÷7F'EöEöFöW5öæ÷E÷v&åöf÷%ö–væ÷&VEööÆEö6Æ–VçG2‡6VÆb“ ¢&÷EW6W"æö&¦V7G2æÆÂ‚’æFVÆWFR‚¢7F'EöBÒF–ÖW¦öæRææ÷r‚¢eä6Æ–VçBæö&¦V7G2æf–ÇFW"‡³×6VÆbçgåö6Æ–VçBç²’çWFFR†7&VFVEöC×7F'EöBÒF–ÖVFVÇF†F—3Ó’¢6VÆbç7F÷&Rç&VæWvÅ÷&VÖ–æFW'5÷7F'EöBÒ7F'Eö@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&VæWvÅ÷&VÖ–æFW'5÷7F'EöB"Â'WFFVEöB%Ò ¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚&6†V6µö–çFVw&F–öç2"Â"ÒÖæòÖf–Â"Â7FF÷WCÖ÷WB ¢÷WGWBÒ÷WBævWGfÇVR‚¢6VÆbæ76W'D–â‚&–væ÷&VEö&Vf÷&U÷7F'EöCÓ"Â÷WGWB¢6VÆbæ76W'D–â‚&6æF–FFW5ögFW%÷7F'EöCÓ"Â÷WGWB¢6VÆbæ76W'D–â‚&öÆB&VÖ–æFW"6Æ–VçB‡2’&R–væ÷&VB"Â÷WGWB¢6VÆbæ76W'Dæ÷D–â‚'&VæWvÅ÷&VÖ–æFW'5÷7F'EöB‹ŠrŠ­˜m‹¸Í˜Rª˜m¸ÍŠò"Â÷WGWB ¢FVbFW7Eö6†V6µö–çFVw&F–öç5÷v—F†÷WE÷7F'EöE÷v&ç5ö&÷WEööÆE÷F&vWFÆW75ö6Æ–VçG2‡6VÆb“ ¢&÷EW6W"æö&¦V7G2æÆÂ‚’æFVÆWFR‚ ¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚&6†V6µö–çFVw&F–öç2"Â"ÒÖæòÖf–Â"Â7FF÷WCÖ÷WB ¢÷WGWBÒ÷WBævWGfÇVR‚¢6VÆbæ76W'D–â‚'&VæWvÅ÷&VÖ–æFW'5÷7F'EöCÖæ÷B6WB"Â÷WGWB¢6VÆbæ76W'D–â‚&†fRæòFVÆVw&ÒF&vWB"Â÷WGWB¢6VÆbæ76W'D–â‚'&VæWvÅ÷&VÖ–æFW'5÷7F'EöB‹ŠrŠ­˜m‹¸Í˜Rª˜m¸ÍŠò"Â÷WGWB ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢FVbFW7E÷7F'Eö&÷VæF'•öFöW5öæ÷E÷&W—%ööÆEöÖ—76–æu÷F&vWG2‡6VÆbÂ7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢&÷EW6W"æö&¦V7G2æÆÂ‚’æFVÆWFR‚¢7W7FöÖW%ö6÷VçBÒ7W7FöÖW"æö&¦V7G2æ6÷VçB‚¢7F'EöBÒF–ÖW¦öæRææ÷r‚¢eä6Æ–VçBæö&¦V7G2æf–ÇFW"‡³×6VÆbçgåö6Æ–VçBç²’çWFFR†7&VFVEöC×7F'EöBÒF–ÖVFVÇF†F—3Ó’¢6VÆbç7F÷&Rç&VæWvÅ÷&VÖ–æFW'5÷7F'EöBÒ7F'Eö@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&VæWvÅ÷&VÖ–æFW'5÷7F'EöB"Â'WFFVEöB%Ò ¢7VÖÖ'’Ò'Vå÷&VæWvÅ÷&VÖ–æFW'2†G'•÷'VãÕG'VR ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&–væ÷&VEö&Vf÷&U÷7F'EöB%ÒÂ¢6VÆbæ76W'DWVÂ„&÷EW6W"æö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ„7W7FöÖW"æö&¦V7G2æ6÷VçB‚’Â7W7FöÖW%ö6÷VçB¢7–æ5öÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2ç7–æ5÷gåö6Æ–VçE÷7FG2"¢F6‚‚'7F÷&Ræ&÷G2ä&÷D6Æ–VçBç6VæEöÖW76vR"Â&WGW&å÷fÇVS×²&ö²#¢G'VWÒ¢FVbFW7Eöf÷&6Uö¦ö–åöFöW5öæ÷Eö&Æö6µ÷&VÖ–æFW%öFVÆ—fW'’‡6VÆbÂ6VæEöÖö6²Â7–æ5öÖö6²“ ¢g&öÒç&VæWvÅ÷&VÖ–æFW%÷6W'f–6W2–×÷'B'Vå÷&VæWvÅ÷&VÖ–æFW'0 ¢6VÆbæ&÷Eö6öæf–ræf÷&6U÷FVÆVw&Õö6†ææVÅö¦ö–âÒG'VP¢6VÆbæ&÷Eö6öæf–rçFVÆVw&Õ÷&WV—&VEö6†ææVÅ÷W6W&æÖRÒ&¦FæWB ¢6VÆbæ&÷Eö6öæf–rç6fR‡WFFUöf–VÆG3Õ²&f÷&6U÷FVÆVw&Õö6†ææVÅö¦ö–â"Â'FVÆVw&Õ÷&WV—&VEö6†ææVÅ÷W6W&æÖR"Â'WFFVEöB%Ò¢7–æ5öÖö6²ç&WGW&å÷fÇVRÒ6VÆbç‡V•÷7FG2‡&VÖ–æ–æuöv#Ó’¢7VÖÖ'’Ò'Vå÷&VæWvÅ÷&VÖ–æFW'2‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚  ¦6Æ72&WfVçVTVæv–æU†6TöæUFW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢66†Ræ6ÆV"‚¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ%&WfVçVR7F÷&R"À¢VævÆ—6…öæÖSÒ%&WfVçVR7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢&WfVçVUöVæv–æUöG'•÷'VãÔfÇ6RÀ¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ#t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚#"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢FWf–6UöÆ–Ö—CÓ"À¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢&VÖ&³Ò&Ö–â"À¢&÷Fö6öÃÔ–æ&÷VæBå&÷Fö6öÂådÄU52À¢6W'fW%ö—Ò'gâæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'6V7W&—G“×&VÆ—G’"À¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6R×&WfVçVR"¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò"À¢&÷E÷Fö¶VãÒ##3§Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢¢6VÆbæ&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢ ¢FVbv"‡6VÆbÂfÇVR“ ¢&WGW&â–çB„FV6–ÖÂ‡7G"‡fÇVR’’¢FV6–ÖÂƒ#B¢¢2’ ¢FVbÖ¶Uö6Æ–VçB‡6VÆbÂ¢ÂW‡—&W5öCÔæöæRÂW6VEöv#ÓÂF÷FÅöv#ÓÂ7FGW3Õeä6Æ–VçBå7FGW2ä5D•dR“ ¢6Æ–VçEö–æFW‚Òeä6Æ–VçBæö&¦V7G2æ6÷VçB‚’²¢WV–BÒb#“““““““’Ó“““’ÓC““’Óƒ““’×¶6Æ–VçEö–æFWƒ£&GÒ ¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÖb'&WfVçVU÷¶6Æ–VçEö–æFW‡Ò"À¢WV–C×WV–BÀ¢¢&WGW&âeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÖb'&WfVçVU÷¶6Æ–VçEö–æFW‡Ò"À¢‡V•öVÖ–ÃÖb'&WfVçVU÷¶6Æ–VçEö–æFW‡Ò"À¢WV–C×WV–BÀ¢7FGW3×7FGW2À¢G&ff–5öÆ–Ö—Eö'—FW3×6VÆbæv"‡F÷FÅöv"’À¢W6VE÷G&ff–5ö'—FW3×6VÆbæv"‡W6VEöv"’À¢GW&F–öåöF—3×6VÆbçÆâæGW&F–öåöF—2À¢FWf–6UöÆ–Ö—C×6VÆbçÆâæFWf–6UöÆ–Ö—BÀ¢W‡—&W5öCÖW‡—&W5öB÷"F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó’À¢Æ7EööæÆ–æUöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó’À¢Æ7E÷7–æ6VEöC×F–ÖW¦öæRææ÷r‚’À¢ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç66†VGVÆW"æVÖ—EöWfVçB"¢FVbFW7EöW‡—'•÷G&–vvW"‡6VÆbÂVÖ—EöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WfVçVU÷66à¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôU…•$T@ ¢6Æ–VçBÒ6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó’ÂW6VEöv#Ó ¢7VÖÖ'’Ò'Vå÷&WfVçVU÷66â‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'66ææVB%ÒÂ¢6VÆbæ76W'EG'VR†ç’†6ÆÂæ&w5³ÒÓÒU4U%ôU…•$TBæB6ÆÂæ&w5³ÒÓÒ6Æ–VçBf÷"6ÆÂ–âVÖ—EöÖö6²æ6ÆÅö&w5öÆ—7B’ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç66†VGVÆW"æVÖ—EöWfVçB"¢FVbFW7E÷W6vUö÷fW%óƒ÷G&–vvW"‡6VÆbÂVÖ—EöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WfVçVU÷66à¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'B„”t…õU4tUõU4U  ¢6Æ–VçBÒ6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó’ÂW6VEöv#Ó’ ¢'Vå÷&WfVçVU÷66â‚ ¢6VÆbæ76W'EG'VR†ç’†6ÆÂæ&w5³ÒÓÒ„”t…õU4tUõU4U"æB6ÆÂæ&w5³ÒÓÒ6Æ–VçBf÷"6ÆÂ–âVÖ—EöÖö6²æ6ÆÅö&w5öÆ—7B’ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöæõöGWÆ–6FUöÖW76v–æuö–åó#F‚‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôU…•$T@ ¢6Æ–VçBÒ6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó’¢6öçFW‡BÒ²'W6vU÷W&6VçB#¢FV6–ÖÂ‚#"—Ð¢f—'7BÒ&WfVçVTVæv–æR‚’æ†æFÆR…U4U%ôU…•$TBÂ6Æ–VçBÂ6öçFW‡B¢6V6öæBÒ&WfVçVTVæv–æR‚’æ†æFÆR…U4U%ôU…•$TBÂ6Æ–VçBÂ6öçFW‡B ¢6VÆbæ76W'EG'VR†f—'7E²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡6V6öæE²&7F–öâ%Õ²'&V6öâ%ÒÂ&6ööÆF÷våö7F—fR"¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷6WGW÷&WV—&VE÷7F÷&U÷7W&W76W5÷&WfVçVU÷6VæB‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôU…•$T@ ¢6VÆbç7F÷&Rç6WGW÷7FGW2Ò7F÷&Rå6WGW7FGW2å4UEUõ$UT•$T@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'6WGW÷7FGW2%Ò¢6Æ–VçBÒ6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó’ ¢&W7VÇBÒ&WfVçVTVæv–æR‚’æ†æFÆR…U4U%ôU…•$TBÂ6Æ–VçBÂ²'W6vU÷W&6VçB#¢FV6–ÖÂ‚#"—Ò ¢6VÆbæ76W'EG'VR‡&W7VÇE²&†æFÆVB%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ'6WGW÷&WV—&VB"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢FVbFW7E÷W6W%öW‡—&VEöÆöv–2‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRç'VÆW2–×÷'B'VÆTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôU…•$T@ ¢FV6—6–öâÒ'VÆTVæv–æR‚’æWfÇVFR…U4U%ôU…•$TBÂ6VÆbæ7W7FöÖW"Â·Ò ¢6VÆbæ76W'DWVÂ†FV6—6–öå²&F—66÷VçB%ÒÂ#R¢6VÆbæ76W'DWVÂ†FV6—6–öå²'G—R%ÒÂ'W6W%öW‡—&VB"¢6VÆbæ76W'D–â‚##RRŠ­Ší˜¸Í˜"ÂFV6—6–öå²&ÖW76vR%Ò ¢FVbFW7EöæV%öW‡—'•öF—66÷VçEöÆöv–2‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRç'VÆW2–×÷'B'VÆTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôäT%ôU…•% ¢Væv–æRÒ'VÆTVæv–æR‚ ¢6VÆbæ76W'DWVÂ†Væv–æRæWfÇVFR…U4U%ôäT%ôU…•%’Â6VÆbæ7W7FöÖW"Â²'W6vU÷W&6VçB#¢ƒWÒ•²&F—66÷VçB%ÒÂR¢6VÆbæ76W'DWVÂ†Væv–æRæWfÇVFR…U4U%ôäT%ôU…•%’Â6VÆbæ7W7FöÖW"Â²'W6vU÷W&6VçB#¢cWÒ•²&F—66÷VçB%ÒÂ¢6VÆbæ76W'DWVÂ†Væv–æRæWfÇVFR…U4U%ôäT%ôU…•%’Â6VÆbæ7W7FöÖW"Â²'W6vU÷W&6VçB#¢#Ò•²&F—66÷VçB%ÒÂR ¢FVbFW7Eö†–v…÷W6vU÷Ww&FU÷7VvvW7F–öâ‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRç'VÆW2–×÷'B'VÆTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'B„”t…õU4tUõU4U  ¢FV6—6–öâÒ'VÆTVæv–æR‚’æWfÇVFR„„”t…õU4tUõU4U"Â6VÆbæ7W7FöÖW"Â²'W6vU÷W&6VçB#¢“Ò ¢6VÆbæ76W'DWVÂ†FV6—6–öå²'G—R%ÒÂ&†–v…÷W6vU÷Ww&FR"¢6VÆbæ76W'DWVÂ†FV6—6–öå²'föÇVÖUö×VÇF—Æ–W"%ÒÂ"¢6VÆbæ76W'D–â‚-›í¸Í‹M˜m˜}Š}ŠòŠ}‹Š­˜-Šr"ÂFV6—6–öå²&ÖW76vR%Ò ¢FVbFW7EöVæv–æUöFöW5öæ÷Eö7&6…ööåöÖ—76–æuöFF‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôäT%ôU…•% ¢&W7VÇBÒ&WfVçVTVæv–æR‚’æ†æFÆR…U4U%ôäT%ôU…•%’ÂæöæRÂ·Ò ¢6VÆbæ76W'EG'VR‡&W7VÇE²&†æFÆVB%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ&æõ÷W'6öæÅ÷FVÆVw&Õ÷F&vWB" ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷66†VGVÆW%÷'Vç5÷v—F†÷WEöF%öW'&÷'2‡6VÆbÂ÷6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WfVçVU÷66à ¢6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF††÷W'3Ó"’ÂW6VEöv#Ób¢÷WBÒ7G&–æt”ò‚ ¢7VÖÖ'’Ò'Vå÷&WfVçVU÷66â‚¢6ÆÅö6öÖÖæB‚''Vå÷&WfVçVU÷66â"Â7FF÷WCÖ÷WB ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'66ææVB%ÒÂ¢6VÆbæ76W'D–â‚%&WfVçVR66â7VÖÖ'“¢"Â÷WBævWGfÇVR‚’  ¦6Æ72W6VÆÄVæv–æU†6UGvõFW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢66†Ræ6ÆV"‚¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ%W6VÆÂ7F÷&R"À¢VævÆ—6…öæÖSÒ%W6VÆÂ7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢&WfVçVUöVæv–æUöG'•÷'VãÔfÇ6RÀ¢¢6VÆbç6ÖÆÅ÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%6ÖÆÂ"À¢föÇVÖUöv#ÔFV6–ÖÂ‚#R"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#ÓÀ¢¢6VÆbæÖVF—VÕ÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ$ÖVF—VÒ"À¢föÇVÖUöv#ÔFV6–ÖÂ‚#‚"’À¢GW&F–öåöF—3Ó3À¢&–6SÓ#SÀ¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#Ó"À¢¢6VÆbæÆ&vU÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ$Æ&vR"À¢föÇVÖUöv#ÔFV6–ÖÂ‚##"’À¢GW&F–öåöF—3Ó3À¢&–6SÓ#À¢FWf–6UöÆ–Ö—CÓ2À¢6÷'Eö÷&FW#Ó2À¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢&VÖ&³Ò&Ö–â"À¢&÷Fö6öÃÔ–æ&÷VæBå&÷Fö6öÂådÄU52À¢6W'fW%ö—Ò'gâæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'6V7W&—G“×&VÆ—G’"À¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6R×W6VÆÂ"¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò"À¢&÷E÷Fö¶VãÒ##3§Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢¢6VÆbæ&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢ ¢FVb6öçFW‡B‡6VÆbÂÆãÔæöæRÂ¢¦W‡G&“ ¢FFÒ°¢&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"À¢&6†Eö–B#¢6VÆbæ&÷E÷W6W"æ6†Eö–BÀ¢&&÷Eö6öæf–r#¢6VÆbæ&÷Eö6öæf–rÀ¢'7F÷&R#¢6VÆbç7F÷&RÀ¢'6VÆV7FVE÷Æâ#¢Æâ÷"6VÆbç6ÖÆÅ÷ÆâÀ¢'Æâ#¢Æâ÷"6VÆbç6ÖÆÅ÷ÆâÀ¢'VçF—G’#¢À¢Ð¢FFçWFFR†W‡G&¢&WGW&âFF ¢FVbÖ¶U÷gåö6Æ–VçB‡6VÆbÂ¢ÂW6VEöv#Ó’ÂF÷FÅöv#Ó“ ¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢Æã×6VÆbç6ÖÆÅ÷ÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbç6ÖÆÅ÷Æâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbç6ÖÆÅ÷Æâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ'W6VÆÅö6Æ–VçB"À¢WV–CÒ#ƒƒƒƒƒƒƒ‚Óƒƒƒ‚ÓCƒƒ‚Óƒƒƒ‚Óƒƒƒƒƒƒƒƒƒƒƒ‚"À¢¢&WGW&âeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbç6ÖÆÅ÷ÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ'W6VÆÅö6Æ–VçB"À¢‡V•öVÖ–ÃÒ'W6VÆÅö6Æ–VçB"À¢WV–CÖ÷&FW"çWV–BÀ¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ö–çB„FV6–ÖÂ‡7G"‡F÷FÅöv"’’¢FV6–ÖÂƒ#B¢¢2’’À¢W6VE÷G&ff–5ö'—FW3Ö–çB„FV6–ÖÂ‡7G"‡W6VEöv"’’¢FV6–ÖÂƒ#B¢¢2’’À¢W‡—&W5öC×F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó#’À¢Æ7E÷7–æ6VEöC×F–ÖW¦öæRææ÷r‚’À¢ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷6ÖÆÅ÷Æå÷G&–vvW'5÷W6VÆÂ‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'BU4U%õÄåõ4TÄT5DT@ ¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR…U4U%õÄåõ4TÄT5DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'EG'VR‡&W7VÇE²&†æFÆVB%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²&FV6—6–öâ%Õ²'Ww&FU÷Æâ%ÒÂ6VÆbæÖVF—VÕ÷Æâ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²&6†Eö–B%ÒÂ#C"" ¢FVbFW7Eö†–v…÷W6vU÷G&–vvW'5÷Ww&FUööffW"‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂç'VÆW2–×÷'BW6VÆÅ'VÆTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'BU4U%õÄåõ4TÄT5DT@ ¢FV6—6–öâÒW6VÆÅ'VÆTVæv–æR‚’æWfÇVFR€¢U4U%õÄåõ4TÄT5DTBÀ¢6VÆbæ&÷E÷W6W"À¢6VÆbæ6öçFW‡B‡W6vU÷W&6VçCÓ“’À¢ ¢6VÆbæ76W'DWVÂ†FV6—6–öå²'G—R%ÒÂ'W6VÆÅööffW""¢6VÆbæ76W'DWVÂ†FV6—6–öå²'Ww&FU÷Æâ%ÒÂ6VÆbæÆ&vU÷Æâ¢6VÆbæ76W'D–â‚#"Š‹Š}Š‹ŠÝŠÍ˜R"ÂFV6—6–öå²&ÖW76vR%Ò ¢FVbFW7Eö6†V6¶÷WE÷G&–vvW'5öFEööåööffW"‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂç'VÆW2–×÷'BW6VÆÅ'VÆTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢FV6—6–öâÒW6VÆÅ'VÆTVæv–æR‚’æWfÇVFR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‡Æã×6VÆbæÖVF—VÕ÷Æâ’ ¢6VÆbæ76W'DWVÂ†FV6—6–öå²'G—R%ÒÂ'W6VÆÅööffW""¢6VÆbæ76W'DWVÂ†FV6—6–öå²&FEööâ%ÒÂ&W‡G&öv"" ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöçF•÷7Õó#F…÷'VÆR‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢f—'7BÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‡Æã×6VÆbæÖVF—VÕ÷Æâ’¢6V6öæBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‡Æã×6VÆbæÖVF—VÕ÷Æâ’ ¢6VÆbæ76W'EG'VR†f—'7E²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡6V6öæE²&7F–öâ%Õ²'&V6öâ%ÒÂ&6ööÆF÷våö7F—fR"¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷W6W%÷6¶—öæõ÷&WVEöf÷%óC†‚‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2–×÷'BÖ&µ÷W6VÆÅ÷6¶—V@¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢Ö&µ÷W6VÆÅ÷6¶—VB‡6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‡Æã×6VÆbæÖVF—VÕ÷Æâ’¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‡Æã×6VÆbæÖVF—VÕ÷Æâ’ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ'W6W%÷6¶—VE÷&V6VçFÇ’"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢FVbFW7Eöæõö7&6…ööåöÖ—76–æuö6öçFW‡B‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'BU4U%õÄåõ4TÄT5DT@ ¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR…U4U%õÄåõ4TÄT5DTBÂæöæRÂ·Ò ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²&†æFÆVB%Ò¢6VÆbæ76W'D—4æöæR‡&W7VÇE²&FV6—6–öâ%Ò ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö–çFVw&F–öå÷v—F…÷&WfVçVUöVæv–æU÷v—F†÷WEöGWÆ–6FUöWfVçG2‡6VÆbÂW6VÆÅ÷6VæEöÖö6²Â&VæWvÅ÷6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôU…•$T@¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢gåö6Æ–VçBÒ6VÆbæÖ¶U÷gåö6Æ–VçB‚¢W6VÆÂÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‡Æã×6VÆbæÖVF—VÕ÷Æâ’¢&VæWvÂÒ&WfVçVTVæv–æR‚’æ†æFÆR…U4U%ôU…•$TBÂgåö6Æ–VçBÂ²'W6vU÷W&6VçB#¢Ò ¢6VÆbæ76W'EG'VR‡W6VÆÅ²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡&VæWvÅ²&7F–öâ%Õ²'&V6öâ%ÒÂ'W6VÆÅö7F—fR"¢W6VÆÅ÷6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢&VæWvÅ÷6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚  ¦6Æ72&WFVçF–öäVæv–æU†6UF‡&VUFW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢66†Ræ6ÆV"‚¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ%&WFVçF–öâ7F÷&R"À¢VævÆ—6…öæÖSÒ%&WFVçF–öâ7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢&WfVçVUöVæv–æUöG'•÷'VãÔfÇ6RÀ¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ#t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚#"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢FWf–6UöÆ–Ö—CÓ"À¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢&VÖ&³Ò&Ö–â"À¢&÷Fö6öÃÔ–æ&÷VæBå&÷Fö6öÂådÄU52À¢6W'fW%ö—Ò'gâæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'6V7W&—G“×&VÆ—G’"À¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6R×&WFVçF–öâ"¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò"À¢&÷E÷Fö¶VãÒ##3§Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢¢6VÆbæ&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢ ¢FVb6öçFW‡B‡6VÆbÂ¢¦W‡G&“ ¢FFÒ°¢&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"À¢&6†Eö–B#¢6VÆbæ&÷E÷W6W"æ6†Eö–BÀ¢&&÷Eö6öæf–r#¢6VÆbæ&÷Eö6öæf–rÀ¢&7W7FöÖW"#¢6VÆbæ7W7FöÖW"À¢'7F÷&R#¢6VÆbç7F÷&RÀ¢Ð¢FFçWFFR†W‡G&¢&WGW&âFF ¢FVbÖ¶Uö6Æ–VçB‡6VÆbÂ¢ÂW‡—&W5öCÔæöæRÂ7FGW3Õeä6Æ–VçBå7FGW2ä5D•dR“ ¢W‡—&W5öBÒW‡—&W5öB÷"F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF†F—3Ó¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ'&WFVçF–öåö6Æ–VçB"À¢WV–CÒ#sssssssrÓsssrÓCssrÓƒssrÓsssssssssssr"À¢¢÷&FW"æö&¦V7G2æf–ÇFW"‡³Ö÷&FW"ç²’çWFFR†7&VFVEöCÖW‡—&W5öBÒF–ÖVFVÇF†F—3Ó3’¢&WGW&âeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ'&WFVçF–öåö6Æ–VçB"À¢‡V•öVÖ–ÃÒ'&WFVçF–öåö6Æ–VçB"À¢WV–CÖ÷&FW"çWV–BÀ¢7FGW3×7FGW2À¢G&ff–5öÆ–Ö—Eö'—FW3Ö–çB„FV6–ÖÂ‚#"’¢FV6–ÖÂƒ#B¢¢2’’À¢W6VE÷G&ff–5ö'—FW3Ö–çB„FV6–ÖÂ‚#""’¢FV6–ÖÂƒ#B¢¢2’’À¢W‡—&W5öCÖW‡—&W5öBÀ¢Æ7E÷7–æ6VEöC×F–ÖW¦öæRææ÷r‚’À¢ ¢FVbÖ¶U÷6–ÆVçEö7F—fUö6Æ–VçB‡6VÆbÂ¢ÂW6vUöv#ÔFV6–ÖÂ‚#ãR"’ÂÆ7EööæÆ–æUöCÔæöæR“ ¢6Æ–VçBÒ6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’²F–ÖVFVÇF†F—3Ó#’Â7FGW3Õeä6Æ–VçBå7FGW2ä5D•dR¢6Æ–VçBçW6VE÷G&ff–5ö'—FW2Ò–çB‡W6vUöv"¢FV6–ÖÂƒ#B¢¢2’¢6Æ–VçBæÆ7EööæÆ–æUöBÒÆ7EööæÆ–æUö@¢6Æ–VçBç6fR‡WFFUöf–VÆG3Õ²'W6VE÷G&ff–5ö'—FW2"Â&Æ7EööæÆ–æUöB%Ò¢&WGW&â6Æ–Vç@ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö–æ7F—fUó#F…÷G&–vvW'5÷6ögE÷&VÖ–æFW"‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WFVçF–öå÷66à ¢&÷EW6W"æö&¦V7G2æf–ÇFW"‡³×6VÆbæ&÷E÷W6W"ç²’çWFFR†Æ7E÷6VVåöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó#R’ ¢7VÖÖ'’Ò'Vå÷&WFVçF–öå÷66â‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢6VÆbæ76W'D–â‚-˜]ŠýŠ­¸ÂŠ}‹=Š¢"Â6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²'FW‡B%Ò¢6VÆbæ76W'DWVÂ‡6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²&6†Eö–B%ÒÂ#C"" ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö–æ7F—fUós&…÷G&–vvW'5öF—66÷VçB‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WFVçF–öå÷66à ¢&÷EW6W"æö&¦V7G2æf–ÇFW"‡³×6VÆbæ&÷E÷W6W"ç²’çWFFR†Æ7E÷6VVåöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ós2’ ¢7VÖÖ'’Ò'Vå÷&WFVçF–öå÷66â‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢6VÆbæ76W'D–â‚##RŠ­Ší˜¸Í˜"Â6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²'FW‡B%Ò¢6VÆbæ76W'DWVÂ„&÷DWfVçDÆöræö&¦V7G2ævWB†ÖW76vSÒ'&WFVçF–öåöVæv–æUööffW%÷6VçB"’ç&u÷–ÆöE²&F—66÷VçB%ÒÂ# ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöW‡—&VE÷W6W%÷G&–vvW'5÷v–æ&6²‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WFVçF–öå÷66à ¢6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF†F—3Ó"’ ¢7VÖÖ'’Ò'Vå÷&WFVçF–öå÷66â‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢6VÆbæ76W'D–â‚##RRŠ­Ší˜¸Í˜"Â6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²'FW‡B%Ò ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷&WGW&æVE÷W6W%övWG5ö&öçW2‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæVæv–æR–×÷'B&WFVçF–öäVæv–æP¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâçG&–vvW'2–×÷'BU4U%õ$UEU$äTEôeDU%ô%4Tä4P ¢&W7VÇBÒ&WFVçF–öäVæv–æR‚’æ†æFÆR…U4U%õ$UEU$äTEôeDU%ô%4Tä4RÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²&FV6—6–öâ%Õ²&&öçW5öv"%ÒÂ¢6VÆbæ76W'D–â‚#t"˜}Šý¸Í˜r"Â6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²'FW‡B%Ò ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöçF•÷7ÕóC†…öVæf÷&6VÖVçB‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæVæv–æR–×÷'B&WFVçF–öäVæv–æP¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâçG&–vvW'2–×÷'BU4U%ô”ä5D•dUó#D€ ¢f—'7BÒ&WFVçF–öäVæv–æR‚’æ†æFÆR…U4U%ô”ä5D•dUó#D‚Â6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’¢6V6öæBÒ&WFVçF–öäVæv–æR‚’æ†æFÆR…U4U%ô”ä5D•dUó#D‚Â6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'EG'VR†f—'7E²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡6V6öæE²&7F–öâ%Õ²'&V6öâ%ÒÂ&6ööÆF÷våö7F—fR"¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷&–÷&—G•ö6öæfÆ–7E÷W6VÆÅö&VG5÷&WFVçF–öâ‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæVæv–æR–×÷'B&WFVçF–öäVæv–æP¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâçG&–vvW'2–×÷'BU4U%ô”ä5D•dUó#D€¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2–×÷'BW6VÆÅö7F—fUö¶W ¢66†Rç6WB‡W6VÆÅö7F—fUö¶W’‚#C""’Â&7F—fR"Âc¢c ¢&W7VÇBÒ&WFVçF–öäVæv–æR‚’æ†æFÆR…U4U%ô”ä5D•dUó#D‚Â6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ'W6VÆÅö7F—fR"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöæõöGWÆ–6FUöÖW76vW2‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WFVçF–öå÷66à ¢&÷EW6W"æö&¦V7G2æf–ÇFW"‡³×6VÆbæ&÷E÷W6W"ç²’çWFFR†Æ7E÷6VVåöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ós2’¢6VÆbæÖ¶Uö6Æ–VçB†W‡—&W5öC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF†F—3Ó"’ ¢7VÖÖ'’Ò'Vå÷&WFVçF–öå÷66â‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&WfVçG2%ÒÂ"¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6¶—VB%ÒÂ¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷66†VGVÆW%ö–çFVw&F–öâ‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WFVçF–öå÷66à ¢&÷EW6W"æö&¦V7G2æf–ÇFW"‡³×6VÆbæ&÷E÷W6W"ç²’çWFFR†Æ7E÷6VVåöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó#R’¢÷WBÒ'Vå÷&WFVçF–öå÷66â‚ ¢6VÆbæ76W'DWVÂ†÷WE²'66ææVB%ÒÂ¢6VÆbæ76W'DWVÂ†÷WE²&WfVçG2%ÒÂ¢6VÆbæ76W'DWVÂ†÷WE²'6VçB%ÒÂ¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢FVbFW7E÷6–ÆVçEö7F—fUöFWFV7F–öâ‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'Bö6öçFW‡Eöf÷"Âö—5÷6–ÆVçEö7F—fU÷W6W  ¢æ÷rÒF–ÖW¦öæRææ÷r‚¢6Æ–VçBÒ6VÆbæÖ¶U÷6–ÆVçEö7F—fUö6Æ–VçB†Æ7EööæÆ–æUöCÖæ÷rÒF–ÖVFVÇF††÷W'3ÓC’’ ¢6VÆbæ76W'EG'VR…ö—5÷6–ÆVçEö7F—fU÷W6W"†6Æ–VçBÂö6öçFW‡Eöf÷"†6Æ–VçBÂæ÷r’Âæ÷r’ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ÷u÷W6vUö7F—fU÷7V'67&—F–öå÷6VæG5÷7W÷'Eö6†V6µö–â‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæVæv–æR–×÷'B&WFVçF–öäVæv–æP¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâçG&–vvW'2–×÷'B4”ÄTåEô5D•dUõU4U  ¢&W7VÇBÒ&WFVçF–öäVæv–æR‚’æ†æFÆR€¢4”ÄTåEô5D•dUõU4U"À¢6VÆbæ&÷E÷W6W"À¢6VÆbæ6öçFW‡B†Æ7Eö6öææV7F–öã×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3ÓC’’’À¢ ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²&FV6—6–öâ%Õ²'G—R%ÒÂ'7W÷'Eö6†V6µö–â"¢6VÆbæ76W'D–â‚-›í‹MŠ­¸ÍŠŠ}˜m¸ÂŠý‹Šý‹=Š­‹‹2Š}‹=Š¢"Â6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²'FW‡B%Ò¢6VÆbæ76W'Dæ÷D–â‚-Š­Ší˜¸Í˜"Â6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²'FW‡B%Ò ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöæõöGWÆ–6FU÷6–ÆVçEöÖW76vUö–åós&‚‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæVæv–æR–×÷'B&WFVçF–öäVæv–æP¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâçG&–vvW'2–×÷'B4”ÄTåEô5D•dUõU4U  ¢6öçFW‡BÒ6VÆbæ6öçFW‡B†Æ7Eö6öææV7F–öã×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3ÓC’’¢f—'7BÒ&WFVçF–öäVæv–æR‚’æ†æFÆR…4”ÄTåEô5D•dUõU4U"Â6VÆbæ&÷E÷W6W"Â6öçFW‡B¢6V6öæBÒ&WFVçF–öäVæv–æR‚’æ†æFÆR…4”ÄTåEô5D•dUõU4U"Â6VÆbæ&÷E÷W6W"Â6öçFW‡B ¢6VÆbæ76W'EG'VR†f—'7E²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡6V6öæE²&7F–öâ%Õ²'&V6öâ%ÒÂ'6–ÆVçEö6ööÆF÷våö7F—fR"¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷6–ÆVçEö7F—fU÷7W&W76–öåö–e÷W6VÆÅö7F—fR‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæVæv–æR–×÷'B&WFVçF–öäVæv–æP¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâçG&–vvW'2–×÷'B4”ÄTåEô5D•dUõU4U ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2–×÷'BW6VÆÅö7F—fUö¶W ¢66†Rç6WB‡W6VÆÅö7F—fUö¶W’‚#C""’Â&7F—fR"Âc¢c ¢&W7VÇBÒ&WFVçF–öäVæv–æR‚’æ†æFÆR€¢4”ÄTåEô5D•dUõU4U"À¢6VÆbæ&÷E÷W6W"À¢6VÆbæ6öçFW‡B†Æ7Eö6öææV7F–öã×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3ÓC’’’À¢ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ'W6VÆÅö7F—fR"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷6–ÆVçEö7F—fU÷7W&W76–öåö–e÷&VæWvÅö7F—fR‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2–×÷'B&VæWvÅö7F—fUö¶W¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæVæv–æR–×÷'B&WFVçF–öäVæv–æP¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâçG&–vvW'2–×÷'B4”ÄTåEô5D•dUõU4U  ¢66†Rç6WB‡&VæWvÅö7F—fUö¶W’‚#C""’Â&7F—fR"Âc¢c ¢&W7VÇBÒ&WFVçF–öäVæv–æR‚’æ†æFÆR€¢4”ÄTåEô5D•dUõU4U"À¢6VÆbæ&÷E÷W6W"À¢6VÆbæ6öçFW‡B†Æ7Eö6öææV7F–öã×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3ÓC’’’À¢ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ'&VæWvÅö7F—fR"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷6–ÆVçEö7F—fU÷66†VGVÆW%ö–çFVw&F–öâ‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WfVçVU÷66à ¢6VÆbæÖ¶U÷6–ÆVçEö7F—fUö6Æ–VçB†Æ7EööæÆ–æUöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3ÓC’’ ¢7VÖÖ'’Ò'Vå÷&WfVçVU÷66â‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'66ææVB%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&WfVçG2%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢6VÆbæ76W'D–â‚-Š}‹=Š­˜Š}Šý˜~(ÍŠ}¸ÂŠ½ŠŠ¢˜m‹MŠý˜r"Â6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²'FW‡B%Ò¢6VÆbæ76W'EG'VR„&÷DWfVçDÆöræö&¦V7G2ævWB†ÖW76vSÒ'&WFVçF–öåöVæv–æUööffW%÷6VçB"’ç&u÷–ÆöE²'6–ÆVçEö7F—fU÷W6W"%Ò ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷6–ÆVçEö7F—fUöæõö7&6…ööåöÖ—76–æuöÆ7Eö6öææV7F–öâ‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç66†VGVÆW"–×÷'B'Vå÷&WfVçVU÷66à ¢6VÆbæÖ¶U÷6–ÆVçEö7F—fUö6Æ–VçB†Æ7EööæÆ–æUöCÔæöæR ¢7VÖÖ'’Ò'Vå÷&WfVçVU÷66â‚ ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'66ææVB%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&WfVçG2%ÒÂ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²'6VçB%ÒÂ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚  ¦6Æ72&WfVçVT÷F–Ö—¦F–öå†6Tf÷W%FW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢66†Ræ6ÆV"‚¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ$÷F–Ö—¦F–öâ7F÷&R"À¢VævÆ—6…öæÖSÒ$÷F–Ö—¦F–öâ7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢&WfVçVUöVæv–æUöG'•÷'VãÔfÇ6RÀ¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ#t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚#"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#ÓÀ¢¢6VÆbçWw&FU÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ##t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚##"’À¢GW&F–öåöF—3Ó3À¢&–6SÓ#SÀ¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#Ó"À¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢&VÖ&³Ò&Ö–â"À¢&÷Fö6öÃÔ–æ&÷VæBå&÷Fö6öÂådÄU52À¢6W'fW%ö—Ò'gâæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'6V7W&—G“×&VÆ—G’"À¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6RÖ÷F–Ö—¦F–öâ"¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò"À¢&÷E÷Fö¶VãÒ##3§Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢¢6VÆbæ&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢ ¢FVb6öçFW‡B‡6VÆbÂ¢¦W‡G&“ ¢FFÒ°¢&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"À¢&6†Eö–B#¢6VÆbæ&÷E÷W6W"æ6†Eö–BÀ¢&&÷Eö6öæf–r#¢6VÆbæ&÷Eö6öæf–rÀ¢'7F÷&R#¢6VÆbç7F÷&RÀ¢'6VÆV7FVE÷Æâ#¢6VÆbçÆâÀ¢'Æâ#¢6VÆbçÆâÀ¢'VçF—G’#¢À¢Ð¢FFçWFFR†W‡G&¢&WGW&âFF ¢FVbÖ¶U÷gåö6Æ–VçB‡6VÆb“ ¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ&÷F–Ö—¦F–öåö6Æ–VçB"À¢WV–CÒ#cccccccbÓcccbÓCccbÓƒccbÓcccccccccccb"À¢¢&WGW&âeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&÷F–Ö—¦F–öåö6Æ–VçB"À¢‡V•öVÖ–ÃÒ&÷F–Ö—¦F–öåö6Æ–VçB"À¢WV–CÖ÷&FW"çWV–BÀ¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ö–çB„FV6–ÖÂ‚#"’¢FV6–ÖÂƒ#B¢¢2’’À¢W6VE÷G&ff–5ö'—FW3Ö–çB„FV6–ÖÂ‚#’"’¢FV6–ÖÂƒ#B¢¢2’’À¢W‡—&W5öC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó’À¢Æ7E÷7–æ6VEöC×F–ÖW¦öæRææ÷r‚’À¢ ¢FVb6VVE÷f&–çB‡6VÆbÂöffW%÷G—RÂf&–çBÂ¢Â–×&W76–öç2Â6öçfW'6–öç2“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâçG&6¶W"–×÷'BöffW%G&6¶W  ¢G&6¶W"ÒöffW%G&6¶W"‚¢f÷"–æFW‚–â&ævR†–×&W76–öç2“ ¢W6W%ö–BÒb'¶öffW%÷G—WÒ×·f&–çGÒ×¶–æFW‡Ò ¢G&6¶W"çW6W%÷&V6V—fVEööffW"‡W6W%ö–BÂöffW%÷G—RÂf&–çB¢–b–æFW‚Â6öçfW'6–öç3 ¢G&6¶W"çW6W%÷W&6†6VEögFW%ööffW"‡W6W%ö–BÂöffW%÷G—SÖöffW%÷G—R ¢FVbFW7Eö×VÇF—ÆU÷f&–çG5övVæW&FVB‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâæW‡W&–ÖVçB–×÷'BW‡W&–ÖVçDVæv–æP ¢f&–çG2ÒW‡W&–ÖVçDVæv–æR‚’ævVæW&FU÷f&–çG2€¢²'G—R#¢'W6VÆÅööffW""Â&ÖW76vR#¢&&6RöffW"'ÒÀ¢öffW%÷G—SÒ'W6VÆÂ"À¢ ¢6VÆbæ76W'DWVÂ‡·f&–çE²&W‡W&–ÖVçE÷f&–çB%Òf÷"f&–çB–âf&–çG7ÒÂ²$"Â$""Â$2'Ò¢6VÆbæ76W'EG'VR†ÆÂ‡f&–çE²&÷F–Ö—¦F–öåööffW%÷G—R%ÒÓÒ'W6VÆÂ"f÷"f&–çB–âf&–çG2’ ¢FVbFW7E÷G&6¶W%÷&V6÷&G5ö6öçfW'6–öâ‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâçG&6¶W"–×÷'BöffW%G&6¶W  ¢G&6¶W"ÒöffW%G&6¶W"‚¢G&6¶W"çW6W%÷&V6V—fVEööffW"‚'W6W"Ó"Â'W6VÆÂ"Â$"¢6öçfW'FVBÒG&6¶W"çW6W%÷W&6†6VEögFW%ööffW"‚'W6W"Ó"ÂöffW%÷G—SÒ'W6VÆÂ" ¢6VÆbæ76W'EG'VR†6öçfW'FVBæ6öçfW'FVB¢&V6V—fVBÒ&÷DWfVçDÆöræö&¦V7G2ævWB†ÖW76vSÒ&öffW%öWfVçC§W6W%÷&V6V—fVEööffW""¢6VÆbæ76W'EG'VR‡&V6V—fVBç&u÷–ÆöE²&6öçfW'FVB%Ò ¢FVbFW7E÷66÷&–æuö6Æ7VÆF–öåö67W&7’‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâç66÷&–ær–×÷'B66÷&–ætVæv–æP ¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$"Â–×&W76–öç3ÓÂ6öçfW'6–öç3Ó¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$""Â–×&W76–öç3Ó#Â6öçfW'6–öç3Ór¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$2"Â–×&W76–öç3ÓÂ6öçfW'6–öç3Ó" ¢&FW2Ò66÷&–ætVæv–æR‚’æ6öçfW'6–öå÷&FW2‚'W6VÆÂ" ¢6VÆbæ76W'DÆÖ÷7DWVÂ‡&FW5²$%ÒÂã¢6VÆbæ76W'DÆÖ÷7DWVÂ‡&FW5²$"%ÒÂã3R¢6VÆbæ76W'DÆÖ÷7DWVÂ‡&FW5²$2%ÒÂã# ¢FVbFW7E÷6VÆV7F÷%÷–6·5ö&W7E÷f&–çB‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâç6VÆV7F÷"–×÷'BöffW%6VÆV7F÷  ¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$"Â–×&W76–öç3ÓÂ6öçfW'6–öç3Ó¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$""Â–×&W76–öç3Ó#Â6öçfW'6–öç3Ór¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$2"Â–×&W76–öç3ÓÂ6öçfW'6–öç3Ó" ¢6VÆV7FVBÒöffW%6VÆV7F÷"†Ö–åö–×&W76–öç3ÓÂ&æFöÖ—¦W#×&æFöÒå&æFöÒƒ’’ç6VÆV7B€¢'W6VÆÂ"À¢·²&W‡W&–ÖVçE÷f&–çB#¢$'ÒÂ²&W‡W&–ÖVçE÷f&–çB#¢$"'ÒÂ²&W‡W&–ÖVçE÷f&–çB#¢$2'ÕÒÀ¢W6W%ö–CÒ'6VÆV7F÷"×W6W""À¢ ¢6VÆbæ76W'DWVÂ‡6VÆV7FVE²&W‡W&–ÖVçE÷f&–çB%ÒÂ$""¢6VÆbæ76W'DWVÂ‡6VÆV7FVE²'6VÆV7F–öå÷&V6öâ%ÒÂ&&W7E÷W&f÷&Ö–ær" ¢FVbFW7EöfÆÆ&6µ÷v†VåöæõöFF‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâç6VÆV7F÷"–×÷'BöffW%6VÆV7F÷  ¢6VÆV7FVBÒöffW%6VÆV7F÷"†Ö–åö–×&W76–öç3ÓÂ&æFöÖ—¦W#×&æFöÒå&æFöÒƒ’’ç6VÆV7B€¢'W6VÆÂ"À¢·²&W‡W&–ÖVçE÷f&–çB#¢$'ÒÂ²&W‡W&–ÖVçE÷f&–çB#¢$"'ÒÂ²&W‡W&–ÖVçE÷f&–çB#¢$2'ÕÒÀ¢W6W%ö–CÒ&æWr×W6W""À¢ ¢6VÆbæ76W'D–â‡6VÆV7FVE²&W‡W&–ÖVçE÷f&–çB%ÒÂ²$"Â$""Â$2'Ò¢6VÆbæ76W'DWVÂ‡6VÆV7FVE²'6VÆV7F–öå÷&V6öâ%ÒÂ'6fU÷&æFöÒ" ¢FVbFW7Eöæõ÷7Õ÷6ÖU÷f&–çE÷Fõ÷6ÖU÷W6W"‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâç6VÆV7F÷"–×÷'BöffW%6VÆV7F÷ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâçG&6¶W"–×÷'BöffW%G&6¶W  ¢G&6¶W"ÒöffW%G&6¶W"‚¢G&6¶W"çW6W%÷&V6V—fVEööffW"‚'&WVB×W6W""Â'W6VÆÂ"Â$" ¢6VÆV7FVBÒöffW%6VÆV7F÷"‡G&6¶W#×G&6¶W"ÂÖ–åö–×&W76–öç3ÓÂ&æFöÖ—¦W#×&æFöÒå&æFöÒƒ’’ç6VÆV7B€¢'W6VÆÂ"À¢·²&W‡W&–ÖVçE÷f&–çB#¢$'ÒÂ²&W‡W&–ÖVçE÷f&–çB#¢$"'ÒÂ²&W‡W&–ÖVçE÷f&–çB#¢$2'ÕÒÀ¢W6W%ö–CÒ'&WVB×W6W""À¢ ¢6VÆbæ76W'EG'VR‡G&6¶W"çW6W%ö–åö6ööÆF÷vâ‚'&WVB×W6W""Â'W6VÆÂ"’¢6VÆbæ76W'Dæ÷DWVÂ‡6VÆV7FVE²&W‡W&–ÖVçE÷f&–çB%ÒÂ$" ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö–çFVw&F–öå÷v—F…÷W6VÆÅöVæv–æR‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâçG&6¶W"–×÷'BU4U%õ$T4T•dTEôôddU ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'D–â‡&W7VÇE²&FV6—6–öâ%Õ²&W‡W&–ÖVçE÷f&–çB%ÒÂ²$"Â$""Â$2"Â$’'Ò¢6VÆbæ76W'DWVÂ€¢&÷DWfVçDÆöræö&¦V7G2æf–ÇFW"€¢&u÷–ÆöEõ÷&WfVçVUö÷F–Ö—¦F–öãÕG'VRÀ¢&u÷–ÆöEõööffW%öWfVçCÕU4U%õ$T4T•dTEôôddU"À¢&u÷–ÆöEõööffW%÷G—SÒ'W6VÆÂ"À¢’æ6÷VçB‚’À¢À¢¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚  ¦6Æ72•&WfVçVTFV6—6–öå†6Tf—fUFW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢66†Ræ6ÆV"‚¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ$’&WfVçVR7F÷&R"À¢VævÆ—6…öæÖSÒ$’&WfVçVR7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢&WfVçVUöVæv–æUöG'•÷'VãÔfÇ6RÀ¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ#t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚#"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#ÓÀ¢¢6VÆbçWw&FU÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ##t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚##"’À¢GW&F–öåöF—3Ó3À¢&–6SÓ3À¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#Ó"À¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢&VÖ&³Ò&Ö–â"À¢&÷Fö6öÃÔ–æ&÷VæBå&÷Fö6öÂådÄU52À¢6W'fW%ö—Ò'gâæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'6V7W&—G“×&VÆ—G’"À¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6RÖ’"¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò"À¢&÷E÷Fö¶VãÒ##3§Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢¢6VÆbæ&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢ ¢FVb6öçFW‡B‡6VÆbÂ¢¦W‡G&“ ¢FFÒ°¢&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"À¢&6†Eö–B#¢6VÆbæ&÷E÷W6W"æ6†Eö–BÀ¢&&÷Eö6öæf–r#¢6VÆbæ&÷Eö6öæf–rÀ¢'7F÷&R#¢6VÆbç7F÷&RÀ¢'6VÆV7FVE÷Æâ#¢6VÆbçÆâÀ¢'Æâ#¢6VÆbçÆâÀ¢'VçF—G’#¢À¢'&–6–ær#¢²'F÷FÂ#¢6VÆbçÆâç&–6WÒÀ¢Ð¢FFçWFFR†W‡G&¢&WGW&âFF ¢FVbÖ¶Uö÷&FW"‡6VÆb“ ¢&WGW&â÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ&•ö6Æ–VçB"À¢WV–CÒ#SSSSSSSRÓSSSRÓCSSRÓƒSSRÓSSSSSSSSSSSR"À¢ ¢FVbÖ¶U÷gåö6Æ–VçB‡6VÆb“ ¢÷&FW"Ò6VÆbæÖ¶Uö÷&FW"‚¢&WGW&âeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&•ö6Æ–VçB"À¢‡V•öVÖ–ÃÒ&•ö6Æ–VçB"À¢WV–CÖ÷&FW"çWV–BÀ¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ö–çB„FV6–ÖÂ‚#"’¢FV6–ÖÂƒ#B¢¢2’’À¢W6VE÷G&ff–5ö'—FW3Ö–çB„FV6–ÖÂ‚#’"’¢FV6–ÖÂƒ#B¢¢2’’À¢W‡—&W5öC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó’À¢Æ7E÷7–æ6VEöC×F–ÖW¦öæRææ÷r‚’À¢ ¢FVb6VVE÷f&–çB‡6VÆbÂöffW%÷G—RÂf&–çBÂ¢Â–×&W76–öç2Â6öçfW'6–öç2“ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâçG&6¶W"–×÷'BöffW%G&6¶W  ¢G&6¶W"ÒöffW%G&6¶W"‚¢f÷"–æFW‚–â&ævR†–×&W76–öç2“ ¢W6W%ö–BÒb&’×¶öffW%÷G—WÒ×·f&–çGÒ×¶–æFW‡Ò ¢G&6¶W"çW6W%÷&V6V—fVEööffW"‡W6W%ö–BÂöffW%÷G—RÂf&–çB¢–b–æFW‚Â6öçfW'6–öç3 ¢G&6¶W"çW6W%÷W&6†6VEögFW%ööffW"‡W6W%ö–BÂöffW%÷G—SÖöffW%÷G—R ¢FVbFW7EööffW%övVæW&F–öåög&öÕ÷W6W%ö6öçFW‡B‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ’ævVæW&F÷"–×÷'BöffW$vVæW&F÷ ¢g&öÒç&WfVçVUöVæv–æRæ’ç7G&FVw’–×÷'B&WfVçVU7G&FVw”Væv–æP ¢7G&FVw’Ò&WfVçVU7G&FVw”Væv–æR‚’ç6VÆV7E÷7G&FVw’€¢²&—5ö†–v…÷fÇVR#¢G'VRÂ'W&6†6Uö6÷VçB#¢BÂ&Æ–fWF–ÖU÷fÇVR#¢“Â'W6vU÷W&6VçB#¢ƒWÐ¢¢öffW"ÒöffW$vVæW&F÷"‚’ævVæW&FR€¢²'W6vU÷W&6VçB#¢ƒWÒÀ¢7G&FVw“×7G&FVw’À¢&6UööffW#×²'G—R#¢'W6VÆÅööffW""Â&ÖW76vR#¢&&6R'ÒÀ¢öffW%÷G—SÒ'W6VÆÂ"À¢ ¢6VÆbæ76W'DWVÂ†öffW%²'G—R%ÒÂ&vVæW&FVEööffW""¢6VÆbæ76W'DWVÂ†öffW%²&W‡W&–ÖVçE÷f&–çB%ÒÂ$’"¢6VÆbæ76W'DWVÂ†öffW%²&•÷7G&FVw’%ÒÂ'&VÖ—VÕööffW""¢6VÆbæ76W'D–â‚-›í‹¸Í˜]¸Í˜˜R"ÂöffW%²'F—FÆR%Ò ¢FVbFW7EöfÆÆ&6µ÷Fõö%öVæv–æR‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ’æ÷F–Ö—¦W"–×÷'B•&WfVçVT÷F–Ö—¦W  ¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$"Â–×&W76–öç3ÓÂ6öçfW'6–öç3Ó¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$""Â–×&W76–öç3ÓÂ6öçfW'6–öç3Ó‚ ¢FV6—6–öâÒ•&WfVçVT÷F–Ö—¦W"†6öæf–FVæ6U÷F‡&W6†öÆCÓã“’’æ÷F–Ö—¦R€¢'W6VÆÂ"À¢²'G—R#¢'W6VÆÅööffW""Â&ÖW76vR#¢&&6R'ÒÀ¢W6W#×6VÆbæ&÷E÷W6W"À¢6öçFW‡C×6VÆbæ6öçFW‡B‚’À¢ ¢6VÆbæ76W'DWVÂ†FV6—6–öå²&W‡W&–ÖVçE÷f&–çB%ÒÂ$""¢6VÆbæ76W'DWVÂ†FV6—6–öå²&•öfÆÆ&6µ÷&V6öâ%ÒÂ&Æ÷uö6öæf–FVæ6R" ¢FVbFW7EöfÆÆ&6µ÷Fõ÷'VÆUöVæv–æR‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ’æ÷F–Ö—¦W"–×÷'B•&WfVçVT÷F–Ö—¦W  ¢6Æ72'&ö¶VäW‡W&–ÖVçC ¢FVbvVæW&FU÷f&–çG2‡6VÆbÂ¥ö&w2Â¢¥ö·v&w2“ ¢&—6R'VçF–ÖTW'&÷"‚&"Væf–Æ&ÆR" ¢6Æ72'&ö¶VävVæW&F÷# ¢FVbvVæW&FR‡6VÆbÂ¥ö&w2Â¢¥ö·v&w2“ ¢&—6R'VçF–ÖTW'&÷"‚&’Væf–Æ&ÆR" ¢&6RÒ²'G—R#¢'W6VÆÅööffW""Â&ÖW76vR#¢''VÆRÖW76vR'Ð¢FV6—6–öâÒ•&WfVçVT÷F–Ö—¦W"€¢W‡W&–ÖVçEöVæv–æSÔ'&ö¶VäW‡W&–ÖVçB‚’À¢öffW%övVæW&F÷#Ô'&ö¶VävVæW&F÷"‚’À¢’æ÷F–Ö—¦R‚'W6VÆÂ"Â&6RÂW6W#×6VÆbæ&÷E÷W6W"Â6öçFW‡C×6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DWVÂ†FV6—6–öå²'G—R%ÒÂ'W6VÆÅööffW""¢6VÆbæ76W'DWVÂ†FV6—6–öå²&ÖW76vR%ÒÂ''VÆRÖW76vR"¢6VÆbæ76W'DfÇ6R†FV6—6–öâævWB‚&•övVæW&FVB"’ ¢FVbFW7E÷&VF–7F–öåö67W&7•÷WFFW2‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ’ç&VF–7F÷"–×÷'BW&6†6U&VF–7F÷ ¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâçG&6¶W"–×÷'BöffW%G&6¶W  ¢G&6¶W"ÒöffW%G&6¶W"‚¢G&6¶W"çW6W%÷&V6V—fVEööffW"€¢'S"À¢'W6VÆÂ"À¢$’"À¢ÖWFFF×²&•övVæW&FVB#¢G'VRÂ&•÷7G&FVw’#¢'&VÖ—VÕööffW""Â&•÷&VF–7F–öâ#¢ã‡ÒÀ¢¢G&6¶W"çW6W%÷W&6†6VEögFW%ööffW"‚'S"ÂöffW%÷G—SÒ'W6VÆÂ"¢G&6¶W"çW6W%÷&V6V—fVEööffW"€¢'S""À¢'W6VÆÂ"À¢$’"À¢ÖWFFF×²&•övVæW&FVB#¢G'VRÂ&•÷7G&FVw’#¢'&VÖ—VÕööffW""Â&•÷&VF–7F–öâ#¢ã'ÒÀ¢ ¢&W7VÇBÒW&6†6U&VF–7F÷"‚’çWFFUö67W&7’‚ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²'6×ÆW2%ÒÂ"¢6VÆbæ76W'Dw&VFW$WVÂ‡&W7VÇE²&67W&7’%ÒÂ¢6VÆbæ76W'DÆW74WVÂ‡&W7VÇE²&67W&7’%ÒÂ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eöæõ÷7Õö×VÇF—ÆUö•ööffW'2‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢f—'7BÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’¢6V6öæBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'EG'VR†f—'7E²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ‡6V6öæE²&7F–öâ%Õ²'&V6öâ%ÒÂ&6ööÆF÷våö7F—fR"¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢FVbFW7EöÆ÷uö6öæf–FVæ6U÷6fUöfÆÆ&6²‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ’æ÷F–Ö—¦W"–×÷'B•&WfVçVT÷F–Ö—¦W  ¢FV6—6–öâÒ•&WfVçVT÷F–Ö—¦W"†6öæf–FVæ6U÷F‡&W6†öÆCÓã“’’æ÷F–Ö—¦R€¢'W6VÆÂ"À¢²'G—R#¢'W6VÆÅööffW""Â&ÖW76vR#¢&&6R'ÒÀ¢W6W#×6VÆbæ&÷E÷W6W"À¢6öçFW‡C×6VÆbæ6öçFW‡B‚’À¢ ¢6VÆbæ76W'DfÇ6R†FV6—6–öâævWB‚&•övVæW&FVB"’¢6VÆbæ76W'D–â†FV6—6–öå²&W‡W&–ÖVçE÷f&–çB%ÒÂ²$"Â$""Â$2'Ò¢6VÆbæ76W'DWVÂ†FV6—6–öå²&•öfÆÆ&6µ÷&V6öâ%ÒÂ&Æ÷uö6öæf–FVæ6R" ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6öçfW'6–öå÷G&6¶–æuö•ööffW'2‡6VÆbÂ÷6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%õU$4„4P¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’¢6VÆbæ76W'EG'VR‡&W7VÇE²&FV6—6–öâ%ÒævWB‚&•övVæW&FVB"’ ¢&WfVçVTVæv–æR‚’æ†æFÆR€¢U4U%õU$4„4RÀ¢6VÆbæÖ¶Uö÷&FW"‚’À¢²&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"Â&6†Eö–B#¢#C""Â&&÷Eö6öæf–r#¢6VÆbæ&÷Eö6öæf–wÒÀ¢ ¢&V6V—fVBÒ&÷DWfVçDÆöræö&¦V7G2ævWB€¢&u÷–ÆöEõ÷&WfVçVUö÷F–Ö—¦F–öãÕG'VRÀ¢&u÷–ÆöEõööffW%öWfVçCÒ'W6W%÷&V6V—fVEööffW""À¢&u÷–ÆöEõööffW%÷G—SÒ'W6VÆÂ"À¢¢6VÆbæ76W'EG'VR‡&V6V—fVBç&u÷–ÆöE²&6öçfW'FVB%Ò¢6VÆbæ76W'EG'VR‡&V6V—fVBç&u÷–ÆöE²&ÖWFFF%Õ²&•övVæW&FVB%Ò ¢FVbFW7Eö–çFVw&F–öå÷v—F…ö÷F–Ö—¦F–öåöVæv–æR‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæ’æ÷F–Ö—¦W"–×÷'B•&WfVçVT÷F–Ö—¦W  ¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$"Â–×&W76–öç3ÓÂ6öçfW'6–öç3Ó¢6VÆbç6VVE÷f&–çB‚'W6VÆÂ"Â$""Â–×&W76–öç3ÓÂ6öçfW'6–öç3Ó’ ¢FV6—6–öâÒ•&WfVçVT÷F–Ö—¦W"‚’æ÷F–Ö—¦R€¢'W6VÆÂ"À¢²'G—R#¢'W6VÆÅööffW""Â&ÖW76vR#¢&&6R'ÒÀ¢W6W#×6VÆbæ&÷E÷W6W"À¢6öçFW‡C×6VÆbæ6öçFW‡B‚’À¢ ¢6VÆbæ76W'DWVÂ†FV6—6–öå²&W‡W&–ÖVçE÷f&–çB%ÒÂ$""¢6VÆbæ76W'DWVÂ†FV6—6–öå²&•öfÆÆ&6µ÷&V6öâ%ÒÂ&%öW‡V7FVE÷&WfVçVR" ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö–çFVw&F–öå÷v—F…÷&VæWvÅöVæv–æR‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRæ÷F–Ö—¦F–öâçG&6¶W"–×÷'BU4U%õ$T4T•dTEôôddU ¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôU…•$T@ ¢&W7VÇBÒ&WfVçVTVæv–æR‚’æ†æFÆR…U4U%ôU…•$TBÂ6VÆbæÖ¶U÷gåö6Æ–VçB‚’Â²'W6vU÷W&6VçB#¢FV6–ÖÂ‚#"—Ò ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'D–â‡&W7VÇE²&FV6—6–öâ%Õ²&W‡W&–ÖVçE÷f&–çB%ÒÂ²$"Â$""Â$2"Â$’'Ò¢6VÆbæ76W'DWVÂ€¢&÷DWfVçDÆöræö&¦V7G2æf–ÇFW"€¢&u÷–ÆöEõ÷&WfVçVUö÷F–Ö—¦F–öãÕG'VRÀ¢&u÷–ÆöEõööffW%öWfVçCÕU4U%õ$T4T•dTEôôddU"À¢&u÷–ÆöEõööffW%÷G—SÒ'&VæWvÂ"À¢’æ6÷VçB‚’À¢À¢¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚  ¦6Æ72&WfVçVT6öçG&öÄ6VçFW%†6U6—…FW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢66†Ræ6ÆV"‚¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ$6öçG&öÂ7F÷&R"À¢VævÆ—6…öæÖSÒ$6öçG&öÂ7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢&WfVçVUöVæv–æUöG'•÷'VãÔfÇ6RÀ¢&WfVçVUöÖ…ööffW'5÷W%÷W6W%÷W%öF“ÓRÀ¢&WfVçVUöÖ…ööffW'5÷W%÷W6W%÷W%÷vVV³ÓÀ¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ#t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚#"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#ÓÀ¢¢6VÆbçWw&FU÷ÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ##t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚##"’À¢GW&F–öåöF—3Ó3À¢&–6SÓ3À¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#Ó"À¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢&VÖ&³Ò&Ö–â"À¢&÷Fö6öÃÔ–æ&÷VæBå&÷Fö6öÂådÄU52À¢6W'fW%ö—Ò'gâæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'6V7W&—G“×&VÆ—G’"À¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6RÖ6öçG&öÂ"¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò"À¢&÷E÷Fö¶VãÒ##3§Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢¢6VÆbæ&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢ ¢FVb6öçFW‡B‡6VÆbÂ¢¦W‡G&“ ¢FFÒ°¢&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"À¢&6†Eö–B#¢6VÆbæ&÷E÷W6W"æ6†Eö–BÀ¢&&÷Eö6öæf–r#¢6VÆbæ&÷Eö6öæf–rÀ¢'7F÷&R#¢6VÆbç7F÷&RÀ¢&7W7FöÖW"#¢6VÆbæ7W7FöÖW"À¢'6VÆV7FVE÷Æâ#¢6VÆbçÆâÀ¢'Æâ#¢6VÆbçÆâÀ¢'VçF—G’#¢À¢'&–6–ær#¢²'F÷FÂ#¢6VÆbçÆâç&–6WÒÀ¢Ð¢FFçWFFR†W‡G&¢&WGW&âFF ¢FVbÖ¶U÷gåö6Æ–VçB‡6VÆbÂ¢ÂW‡—&W5öCÔæöæRÂW6VEöv#Ó’ÂF÷FÅöv#Ó“ ¢÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢Ö÷VçC×6VÆbçÆâç&–6RÀ¢÷&–v–æÅöÖ÷VçC×6VÆbçÆâç&–6RÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢—5÷–CÕG'VRÀ¢7FGW3Ô÷&FW"å7FGW2ä4ôÕÄUDTBÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2ådU$”d”TBÀ¢W6W&æÖSÒ&6öçG&öÅö6Æ–VçB"À¢WV–CÒ#CCCCCCCBÓCCCBÓCCCBÓƒCCBÓCCCCCCCCCCCB"À¢¢&WGW&âeä6Æ–VçBæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢÷&FW#Ö÷&FW"À¢Æã×6VÆbçÆâÀ¢–æ&÷VæC×6VÆbæ–æ&÷VæBÀ¢W6W&æÖSÒ&6öçG&öÅö6Æ–VçB"À¢‡V•öVÖ–ÃÒ&6öçG&öÅö6Æ–VçB"À¢WV–CÖ÷&FW"çWV–BÀ¢7FGW3Õeä6Æ–VçBå7FGW2ä5D•dRÀ¢G&ff–5öÆ–Ö—Eö'—FW3Ö–çB„FV6–ÖÂ‡7G"‡F÷FÅöv"’’¢FV6–ÖÂƒ#B¢¢2’’À¢W6VE÷G&ff–5ö'—FW3Ö–çB„FV6–ÖÂ‡7G"‡W6VEöv"’’¢FV6–ÖÂƒ#B¢¢2’’À¢W‡—&W5öCÖW‡—&W5öB÷"F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó’À¢Æ7E÷7–æ6VEöC×F–ÖW¦öæRææ÷r‚’À¢ ¢FVb7&VFUööffW%öÆör‡6VÆbÂ¢¦W‡G&“ ¢FFÒ°¢'7F÷&R#¢6VÆbç7F÷&RÀ¢&7W7FöÖW"#¢6VÆbæ7W7FöÖW"À¢&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"À¢&Væv–æU÷G—R#¢&WfVçVTöffW$ÆöräVæv–æUG—RåU4TÄÂÀ¢&WfVçE÷G—R#¢&6†V6¶÷WE÷7F'FVB"À¢&öffW%÷G—R#¢'W6VÆÂ"À¢'f&–çB#¢$’"À¢&FV6—6–öå÷6÷W&6R#¢&WfVçVTöffW$ÆöräFV6—6–öå6÷W&6Rä’À¢'7FGW2#¢&WfVçVTöffW$Æörå7FGW2å4TåBÀ¢'6VçEöB#¢F–ÖW¦öæRææ÷r‚’À¢&ÖWFFF#¢²'6fR#¢&ö²'ÒÀ¢Ð¢FFçWFFR†W‡G&¢&WGW&â&WfVçVTöffW$Æöræö&¦V7G2æ7&VFR‚¢¦FF ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EövÆö&ÅöF—6&ÆVE÷7W&W76W5ööffW"‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢6VÆbç7F÷&Rç&WfVçVUöVæv–æUöVæ&ÆVBÒfÇ6P¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&WfVçVUöVæv–æUöVæ&ÆVB"Â'WFFVEöB%Ò¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ'&WfVçVUöVæv–æUöF—6&ÆVB"¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’ç7FGW2Â&WfVçVTöffW$Æörå7FGW2å5U$U54TB¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöVæv–æU÷7V6–f–5öF—6&ÆVE÷7W&W76W5ööffW"‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢6VÆbç7F÷&RçW6VÆÅöVæv–æUöVæ&ÆVBÒfÇ6P¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'W6VÆÅöVæv–æUöVæ&ÆVB"Â'WFFVEöB%Ò¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ&Væv–æUöF—6&ÆVB"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöG'•÷'VåöFöW5öæ÷E÷6VæEöæEö7&VFW5öÆör‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢6VÆbç7F÷&Rç&WfVçVUöVæv–æUöG'•÷'VâÒG'VP¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&WfVçVUöVæv–æUöG'•÷'Vâ"Â'WFFVEöB%Ò¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²&G'•÷'Vâ%Ò¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’ç7FGW2Â&WfVçVTöffW$Æörå7FGW2äE%•õ%Tâ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷V–WEö†÷W'5÷7W&W76W5ööffW"‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢æ÷u÷F–ÖRÒF–ÖW¦öæRæÆö6ÇF–ÖR‡F–ÖW¦öæRææ÷r‚’’çF–ÖR‚¢6VÆbç7F÷&Rç&WfVçVUöVæv–æU÷V–WEö†÷W'5öVæ&ÆVBÒG'VP¢6VÆbç7F÷&Rç&WfVçVUöVæv–æU÷V–WEö†÷W'5÷7F'BÒF–ÖRƒÂ¢6VÆbç7F÷&Rç&WfVçVUöVæv–æU÷V–WEö†÷W'5öVæBÒF–ÖRƒ#2ÂS’¢–bæ÷u÷F–ÖRæ†÷W"ÓÒ#2æBæ÷u÷F–ÖRæÖ–çWFRÓÒS“ ¢6VÆbç7F÷&Rç&WfVçVUöVæv–æU÷V–WEö†÷W'5÷7F'BÒF–ÖRƒ#2Â¢6VÆbç7F÷&Rç&WfVçVUöVæv–æU÷V–WEö†÷W'5öVæBÒF–ÖRƒ#2ÂS’¢6VÆbç7F÷&Rç6fR‚¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ'V–WEö†÷W'2"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöF–Ç•ö6÷7W&W76W5ööffW"‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢6VÆbç7F÷&Rç&WfVçVUöÖ…ööffW'5÷W%÷W6W%÷W%öF’Ò¢6VÆbç7F÷&Rç&WfVçVUööffW%ö6ööÆF÷våö†÷W'2Ò¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&WfVçVUöÖ…ööffW'5÷W%÷W6W%÷W%öF’"Â'&WfVçVUööffW%ö6ööÆF÷våö†÷W'2"Â'WFFVEöB%Ò¢ÆörÒ6VÆbæ7&VFUööffW%öÆör†Væv–æU÷G—SÕ&WfVçVTöffW$ÆöräVæv–æUG—Rå$UDTåD”ôâ¢&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡³ÖÆörç²’çWFFR†7&VFVEöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3Ó"’¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ&F–Ç•÷W6W%ö6"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷vVV¶Ç•ö6÷7W&W76W5ööffW"‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢6VÆbç7F÷&Rç&WfVçVUöÖ…ööffW'5÷W%÷W6W%÷W%÷vVV²Ò¢6VÆbç7F÷&Rç&WfVçVUööffW%ö6ööÆF÷våö†÷W'2Ò¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&WfVçVUöÖ…ööffW'5÷W%÷W6W%÷W%÷vVV²"Â'&WfVçVUööffW%ö6ööÆF÷våö†÷W'2"Â'WFFVEöB%Ò¢ÆörÒ6VÆbæ7&VFUööffW%öÆör†Væv–æU÷G—SÕ&WfVçVTöffW$ÆöräVæv–æUG—Rå$UDTåD”ôâ¢&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡³ÖÆörç²’çWFFR†7&VFVEöC×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF†F—3Ó"’¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ'vVV¶Ç•÷W6W%ö6"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6ööÆF÷vå÷7W&W76W5ööffW"‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢6VÆbæ7&VFUööffW%öÆör†Væv–æU÷G—SÕ&WfVçVTöffW$ÆöräVæv–æUG—RåU4TÄÂ¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ&6ööÆF÷våö7F—fR"¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eöæõ÷FVÆVw&Õ÷F&vWEö—5÷6¶—VB‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôU…•$T@ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$æòF&vWB"¢&W7VÇBÒ&WfVçVTVæv–æR‚’æ†æFÆR…U4U%ôU…•$TBÂ7W7FöÖW"Â²'7F÷&R#¢6VÆbç7F÷&RÂ&7W7FöÖW"#¢7W7FöÖW'Ò ¢6VÆbæ76W'DWVÂ‡&W7VÇE²&7F–öâ%Õ²'&V6öâ%ÒÂ&æõ÷W'6öæÅ÷FVÆVw&Õ÷F&vWB"¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’ç7FGW2Â&WfVçVTöffW$Æörå7FGW2å4´•TB¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â6–FUöVffV7CÕ'VçF–ÖTW'&÷"‚&&ööÒ"’¢FVbFW7E÷6VæEöf–Åö7&VFW5öf–ÆVEöÆöu÷v—F†÷WEö7&6‚‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²&f–ÆVB%Ò¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’ç7FGW2Â&WfVçVTöffW$Æörå7FGW2äd”ÄTB¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢FVbFW7EöÖWFFF÷6æ—F—¦U÷&VÖ÷fW5÷6Vç6—F—fU÷fÇVW2‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæwV&G2–×÷'B6æ—F—¦U÷&WfVçVUöÖWFFF ¢6æ—F—¦VBÒ6æ—F—¦U÷&WfVçVUöÖWFFF€¢°¢&6öæf–uöÆ–æ²#¢'fÆW73¢òóÓÓCÓƒÓW†×ÆRæ6öÒ"À¢&•÷Fö¶Vâ#¢&"¢CÀ¢&æ÷FR#¢'W6W"VÖ–ÂÆ–6TW†×ÆRæ6öÒ†öæR³“ƒ“##3CScr"À¢Ð¢ ¢FW‡BÒ§6öâæGV×2‡6æ—F—¦VB¢6VÆbæ76W'Dæ÷D–â‚'fÆW73¢òò"ÂFW‡B¢6VÆbæ76W'Dæ÷D–â‚&Æ–6TW†×ÆRæ6öÒ"ÂFW‡B¢6VÆbæ76W'Dæ÷D–â‚"³“ƒ“##3CScr"ÂFW‡B¢6VÆbæ76W'Dæ÷D–â‚&"¢CÂFW‡B ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷&VæWvÅö7F–öå÷76W5öwV&EöæEöÆöw5÷6VçB‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%ôU…•$T@ ¢&W7VÇBÒ&WfVçVTVæv–æR‚’æ†æFÆR…U4U%ôU…•$TBÂ6VÆbæÖ¶U÷gåö6Æ–VçB‚’Â6VÆbæ6öçFW‡B‡W6vU÷W&6VçCÓ’ ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’æVæv–æU÷G—RÂ&WfVçVTöffW$ÆöräVæv–æUG—Rå$TäUtÂ¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷6–ÆVçEö7F—fU÷&VÖ–ç5÷7W÷'Eö÷&–VçFVB‡6VÆbÂ6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâæVæv–æR–×÷'B&WFVçF–öäVæv–æP¢g&öÒç&WfVçVUöVæv–æRç&WFVçF–öâçG&–vvW'2–×÷'B4”ÄTåEô5D•dUõU4U  ¢&W7VÇBÒ&WFVçF–öäVæv–æR‚’æ†æFÆR€¢4”ÄTåEô5D•dUõU4U"À¢6VÆbæ&÷E÷W6W"À¢6VÆbæ6öçFW‡B†Æ7Eö6öææV7F–öã×F–ÖW¦öæRææ÷r‚’ÒF–ÖVFVÇF††÷W'3ÓC’’’À¢ ¢6VÆbæ76W'EG'VR‡&W7VÇE²&7F–öâ%Õ²'6VçB%Ò¢6VÆbæ76W'D–â‚-›í‹MŠ­¸ÍŠŠ}˜m¸ÂŠý‹Šý‹=Š­‹‹2Š}‹=Š¢"Â6VæEöÖö6²æ6ÆÅö&w2æ·v&w5²'FW‡B%Ò¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’æVæv–æU÷G—RÂ&WfVçVTöffW$ÆöräVæv–æUG—Rå4”ÄTåEô5D•dR ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö•öÆ÷uö6öæf–FVæ6UöfÆÇ5ö&6²‡6VÆbÂ÷6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢6VÆbç7F÷&Rç&WfVçVUöÖ–åö•ö6öæf–FVæ6RÒFV6–ÖÂ‚#ã“R"¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&WfVçVUöÖ–åö•ö6öæf–FVæ6R"Â'WFFVEöB%Ò¢&W7VÇBÒW6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’ ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²&FV6—6–öâ%ÒævWB‚&•övVæW&FVB"’¢6VÆbæ76W'D–â‡&W7VÇE²&FV6—6–öâ%Õ²&W‡W&–ÖVçE÷f&–çB%ÒÂ²$"Â$""Â$2'Ò ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRçW6VÆÂæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷W&6†6UögFW%ööffW%öÖ&·5ö6öçfW'FVB‡6VÆbÂ÷6VæEöÖö6²“ ¢g&öÒç&WfVçVUöVæv–æRæVæv–æR–×÷'B&WfVçVTVæv–æP¢g&öÒç&WfVçVUöVæv–æRçG&–vvW'2–×÷'BU4U%õU$4„4P¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂæVæv–æR–×÷'BW6VÆÄVæv–æP¢g&öÒç&WfVçVUöVæv–æRçW6VÆÂçG&–vvW'2–×÷'B4„T4´õUEõ5D%DT@ ¢W6VÆÄVæv–æR‚’æ†æFÆR„4„T4´õUEõ5D%DTBÂ6VÆbæ&÷E÷W6W"Â6VÆbæ6öçFW‡B‚’¢÷&FW"Ò6VÆbæÖ¶U÷gåö6Æ–VçB‚’æ÷&FW ¢&WfVçVTVæv–æR‚’æ†æFÆR…U4U%õU$4„4RÂ÷&FW"Â6VÆbæ6öçFW‡B†÷&FW#Ö÷&FW"’ ¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’ç7FGW2Â&WfVçVTöffW$Æörå7FGW2ä4ôådU%DTB ¢FVbFW7EöG'•÷'VåööffW%ö—5öæ÷Eö6öçfW'FVB‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæwV&G2–×÷'BÖ&µöÆFW7E÷&WfVçVUööffW%ö6öçfW'FV@ ¢6VÆbæ7&VFUööffW%öÆör‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2äE%•õ%Tâ¢6öçfW'FVBÒÖ&µöÆFW7E÷&WfVçVUööffW%ö6öçfW'FVB‡6VÆbæ7W7FöÖW"Â&÷E÷W6W#×6VÆbæ&÷E÷W6W" ¢6VÆbæ76W'D—4æöæR†6öçfW'FVB¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’ç7FGW2Â&WfVçVTöffW$Æörå7FGW2äE%•õ%Tâ ¢FVbFW7EöGWÆ–6FUö6öçfW'6–öåöFöW5öæ÷Eö7&VFU÷6V6öæEö6öçfW'6–öâ‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæwV&G2–×÷'BÖ&µöÆFW7E÷&WfVçVUööffW%ö6öçfW'FV@ ¢6VÆbæ7&VFUööffW%öÆör‚¢f—'7BÒÖ&µöÆFW7E÷&WfVçVUööffW%ö6öçfW'FVB‡6VÆbæ7W7FöÖW"Â&÷E÷W6W#×6VÆbæ&÷E÷W6W"¢6V6öæBÒÖ&µöÆFW7E÷&WfVçVUööffW%ö6öçfW'FVB‡6VÆbæ7W7FöÖW"Â&÷E÷W6W#×6VÆbæ&÷E÷W6W" ¢6VÆbæ76W'D—4æ÷DæöæR†f—'7B¢6VÆbæ76W'D—4æöæR‡6V6öæB¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2ä4ôådU%DTB’æ6÷VçB‚’Â ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7E÷'Vå÷&WfVçVU÷66åöG'•÷'VåöFöW5öæ÷E÷6VæB‡6VÆbÂ6VæEöÖö6²“ ¢6VÆbæÖ¶U÷gåö6Æ–VçB‚¢÷WBÒ7G&–æt”ò‚ ¢6ÆÅö6öÖÖæB‚''Vå÷&WfVçVU÷66â"Â"ÒÖVæv–æR"Â'&VæWvÂ"Â"ÒÖG'’×'Vâ"Â7FF÷WCÖ÷WB ¢6VÆbæ76W'D–â‚&G'•÷'VãÓ""Â÷WBævWGfÇVR‚’¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢FVbFW7E÷&WfVçVU÷&W÷'Eö6öçfW'6–öå÷&FR‡6VÆb“ ¢6VÆbæ7&VFUööffW%öÆör‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2ä4ôådU%DTB¢6VÆbæ7&VFUööffW%öÆör‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2å4TåBÂf&–çCÒ$""¢÷WBÒ7G&–æt”ò‚ ¢6ÆÅö6öÖÖæB‚'&WfVçVU÷&W÷'B"Â"ÒÖF—2"Â#"Â7FF÷WCÖ÷WB ¢6VÆbæ76W'D–â‚&öffW'5÷6VçCÓ""Â÷WBævWGfÇVR‚’¢6VÆbæ76W'D–â‚&6öçfW'6–öç3Ó"Â÷WBævWGfÇVR‚’¢6VÆbæ76W'D–â‚&6öçfW'6–öå÷&FSÓSãR"Â÷WBævWGfÇVR‚’ ¢FVbFW7E÷&WfVçVUööffW%öÆöuöFÖ–å÷&Vv—7FW&VEöæE÷7F÷&Uöf–VÆG6WEöW†—7G2‡6VÆb“ ¢g&öÒF¦ævòæ6öçG&–"–×÷'BFÖ–â2F¦ævõöFÖ–à¢g&öÒæFÖ–â–×÷'B7F÷&TFÖ–â2&Vv—7FW&VE7F÷&TFÖ–à ¢6VÆbæ76W'D–â…&WfVçVTöffW$ÆörÂF¦ævõöFÖ–âç6—FRå÷&Vv—7G'’¢W6W"ÒvWE÷W6W%öÖöFVÂ‚’æö&¦V7G2æ7&VFU÷7WW'W6W"‡W6W&æÖSÒ&FÖ–â"Â77v÷&CÒ'‚"ÂVÖ–ÃÒ&FÖ–äW†×ÆRæ6öÒ"¢&WVW7BÒ6–×ÆTæÖW76R‡W6W#×W6W"¢7F÷&UöFÖ–âÒ&Vv—7FW&VE7F÷&TFÖ–â…7F÷&RÂF¦ævõöFÖ–âç6—FR¢f–VÆG6WEöæÖW2Ò¶æÖRf÷"æÖRÂö÷F–öç2–â7F÷&UöFÖ–âævWEöf–VÆG6WG2‡&WVW7B•Ð¢6VÆbæ76W'D–â‚%&WfVçVRVæv–æR6öçG&öÇ2"Âf–VÆG6WEöæÖW2 ¢FVbFW7EöF–Ç•÷&W÷'Eö–æ6ÇVFW5÷&WfVçVUöVæv–æU÷6V7F–öâ‡6VÆb“ ¢g&öÒæF–Ç•÷&W÷'E÷6W'f–6W2–×÷'B'V–ÆEöF–Ç•öFÖ–å÷&W÷'EöÖW76vP ¢6VÆbæ7&VFUööffW%öÆör‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2ä4ôådU%DTB¢ÖW76vRÒ'V–ÆEöF–Ç•öFÖ–å÷&W÷'EöÖW76vR‡F–ÖW¦öæRæÆö6ÆFFR‚’Â7F÷&S×6VÆbç7F÷&RÂW'6—7E÷æVÅ÷W6vSÔfÇ6R ¢6VÆbæ76W'D–â‚%&WfVçVRVæv–æR"ÂÖW76vR¢6VÆbæ76W'D–â‚&6öçfW'6–öâ"ÂÖW76vR ¢FVbFW7Eö6†V6µö–çFVw&F–öç5÷&W÷'G5öG'•÷'VåöæEöÆFW7EöÆör‡6VÆb“ ¢6VÆbç7F÷&Rç&WfVçVUöVæv–æUöG'•÷'VâÒG'VP¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&WfVçVUöVæv–æUöG'•÷'Vâ"Â'WFFVEöB%Ò¢6VÆbæ7&VFUööffW%öÆör‚¢÷WBÒ7G&–æt”ò‚ ¢6ÆÅö6öÖÖæB‚&6†V6µö–çFVw&F–öç2"Â"ÒÖæòÖf–Â"Â7FF÷WCÖ÷WB ¢÷WGWBÒ÷WBævWGfÇVR‚¢6VÆbæ76W'D–â‚%&WfVçVRVæv–æRG'•÷'Vâ—2Væ&ÆVB"Â÷WGWB¢6VÆbæ76W'D–â‚$ÆFW7B&WfVçVTöffW$Æör"Â÷WGWB ¢FVbFW7E÷&WfVçVUööffW%öÆöuöÖWFFFöFöW5öæ÷E÷7F÷&U÷6Vç6—F—fU÷fÇVW2‡6VÆb“ ¢g&öÒç&WfVçVUöVæv–æRæwV&G2–×÷'B&V6÷&E÷&WfVçVUööffW%öGFV×@ ¢&V6÷&E÷&WfVçVUööffW%öGFV×B€¢W6W#×6VÆbæ&÷E÷W6W"À¢6öçFW‡C×6VÆbæ6öçFW‡B‚’À¢Væv–æU÷G—SÒ'W6VÆÂ"À¢WfVçE÷G—SÒ&6†V6¶÷WE÷7F'FVB"À¢FV6—6–öã×²'G—R#¢'W6VÆÅööffW""Â&W‡W&–ÖVçE÷f&–çB#¢$’'ÒÀ¢7FGW3Õ&WfVçVTöffW$Æörå7FGW2å4TåBÀ¢ÖWFFF×°¢'7V'67&—F–öåöÆ–æ²#¢&‡GG3¢òöW†×ÆRæ6öÒ÷7V"õ4T5$UBÕDô´Tâ"À¢'WV–B#¢#ÓÓCÓƒÓ"À¢&VÖ–Â#¢&Æ–6TW†×ÆRæ6öÒ"À¢ÒÀ¢ ¢–ÆöBÒ§6öâæGV×2…&WfVçVTöffW$Æöræö&¦V7G2ævWB‚’æÖWFFF¢6VÆbæ76W'Dæ÷D–â‚%4T5$UBÕDô´Tâ"Â–ÆöB¢6VÆbæ76W'Dæ÷D–â‚#ÓÓCÓƒÓ"Â–ÆöB¢6VÆbæ76W'Dæ÷D–â‚&Æ–6TW†×ÆRæ6öÒ"Â–ÆöB  ¦6Æ72&WfVçVT6æ'•†6U6WfVäEFW7G2…FW7D66R“ ¢6æ'•ö6öæf—&ÒÒ%4TäEôôäUõ$UdTåTUô4ä%’  ¢FVb6WEW‡6VÆb“ ¢66†Ræ6ÆV"‚¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ$6æ'’7F÷&R"À¢VævÆ—6…öæÖSÒ$6æ'’7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$Æ–6R"À¢&WfVçVUöVæv–æUöG'•÷'VãÕG'VRÀ¢&WfVçVUöÖ…ööffW'5÷W%÷W6W%÷W%öF“ÓRÀ¢&WfVçVUöÖ…ööffW'5÷W%÷W6W%÷W%÷vVV³ÓÀ¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ#t""À¢föÇVÖUöv#ÔFV6–ÖÂ‚#"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢FWf–6UöÆ–Ö—CÓ"À¢6÷'Eö÷&FW#ÓÀ¢¢6VÆbçæVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢W6W&æÖSÒ&FÖ–â"À¢77v÷&CÒ'6V7&WB"À¢¢6VÆbæ–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2æ7&VFR€¢æVÃ×6VÆbçæVÂÀ¢–æ&÷VæEö–CÓÀ¢&VÖ&³Ò&Ö–â"À¢&÷Fö6öÃÔ–æ&÷VæBå&÷Fö6öÂådÄU52À¢6W'fW%ö—Ò'gâæW†×ÆRæ6öÒ"À¢÷'CÒ#CC2"À¢6öæf–u÷&×3Ò'6V7W&—G“×&VÆ—G’"À¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ$Æ–6R"ÂW6W&æÖSÒ&Æ–6RÖ6æ'’"¢6VÆbæ&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%FVÆVw&Ò"À¢&÷E÷Fö¶VãÒ##3§Fö¶Vâ"À¢FÖ–å÷W6W%ö–CÒ#““’"À¢¢6VÆbæ&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#C""À¢6†Eö–CÒ#C""À¢W6W&æÖSÒ&Æ–6R"À¢F—7Æ•öæÖSÒ$Æ–6R"À¢ ¢FVb•öFV6—6–öâ‡6VÆbÂ¢¦W‡G&“ ¢FV6—6–öâÒ°¢'G—R#¢&vVæW&FVEööffW""À¢&ÖW76vR#¢-¸Íª’›í¸Í‹M˜m˜}Š}ŠòŠŠ}‹-ªý‹MŠ¢Š}˜]˜bŠ‹Š}¸ÂŠ­‹=Š¢Š-˜]Š}Šý˜rŠ}‹=Š¢â"À¢&÷F–Ö—¦F–öåööffW%÷G—R#¢'&WFVçF–öâ"À¢&W‡W&–ÖVçE÷f&–çB#¢$’"À¢&•övVæW&FVB#¢G'VRÀ¢&•ö6öæf–FVæ6R#¢ã‚À¢&•÷&VF–7F–öâ#¢ãBÀ¢'6VÆV7F–öå÷&V6öâ#¢&•öW‡V7FVE÷&WfVçVR"À¢Ð¢FV6—6–öâçWFFR†W‡G&¢&WGW&âFV6—6–öà ¢FVb7&VFUö6æ'•÷6÷W&6UöÆör‡6VÆbÂ¢¦W‡G&“ ¢FFÒ°¢'7F÷&R#¢6VÆbç7F÷&RÀ¢&7W7FöÖW"#¢6VÆbæ7W7FöÖW"À¢&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"À¢&Væv–æU÷G—R#¢&WfVçVTöffW$ÆöräVæv–æUG—Rå$UDTåD”ôâÀ¢&WfVçE÷G—R#¢'W6W%ö–æ7F—fUós&‚"À¢&öffW%÷G—R#¢'&WFVçF–öâ"À¢'f&–çB#¢$’"À¢&FV6—6–öå÷6÷W&6R#¢&WfVçVTöffW$ÆöräFV6—6–öå6÷W&6Rä’À¢'7FGW2#¢&WfVçVTöffW$Æörå7FGW2äE%•õ%TâÀ¢'6¶—÷&V6öâ#¢&G'•÷'Vâ"À¢&ÖWFFF#¢²'6fR#¢&ö²'ÒÀ¢Ð¢FFçWFFR†W‡G&¢&WGW&â&WfVçVTöffW$Æöræö&¦V7G2æ7&VFR‚¢¦FF ¢FVb7&VFUööffW%öÆör‡6VÆbÂ¢¦W‡G&“ ¢FFÒ°¢'7F÷&R#¢6VÆbç7F÷&RÀ¢&7W7FöÖW"#¢6VÆbæ7W7FöÖW"À¢&&÷E÷W6W"#¢6VÆbæ&÷E÷W6W"À¢&Væv–æU÷G—R#¢&WfVçVTöffW$ÆöräVæv–æUG—Rå$UDTåD”ôâÀ¢&WfVçE÷G—R#¢'W6W%ö–æ7F—fUós&‚"À¢&öffW%÷G—R#¢'&WFVçF–öâ"À¢'f&–çB#¢$’"À¢&FV6—6–öå÷6÷W&6R#¢&WfVçVTöffW$ÆöräFV6—6–öå6÷W&6Rä’À¢'7FGW2#¢&WfVçVTöffW$Æörå7FGW2å4TåBÀ¢'6VçEöB#¢F–ÖW¦öæRææ÷r‚’À¢&ÖWFFF#¢²'6fR#¢&ö²'ÒÀ¢Ð¢FFçWFFR†W‡G&¢&WGW&â&WfVçVTöffW$Æöræö&¦V7G2æ7&VFR‚¢¦FF ¢FVb6ÆÅö6æ'’‡6VÆbÂ6÷W&6UöÆörÂ¢¦W‡G&“ ¢÷F–öç2Ò°¢&öffW%öÆöuö–B#¢7G"‡6÷W&6UöÆörç²’À¢&7W7FöÖW%ö–B#¢7G"‡6VÆbæ7W7FöÖW"ç²’À¢&&÷E÷W6W%ö–B#¢7G"‡6VÆbæ&÷E÷W6W"ç²’À¢&6öæf—&Ò#¢6VÆbæ6æ'•ö6öæf—&ÒÀ¢'fÆ–FF–öå÷&W7VÇB#¢²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B"Â'6fUöW'&÷"#¢"'ÒÀ¢Ð¢÷F–öç2çWFFR†W‡G&¢fÆ–FF–öå÷&W7VÇBÒ÷F–öç2ç÷‚'fÆ–FF–öå÷&W7VÇB"¢÷WBÒ7G&–æt”ò‚¢v—F‚F6‚€¢'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’çfÆ–FFU÷FVÆVw&Õ÷F&vWB"À¢&WGW&å÷fÇVS×fÆ–FF–öå÷&W7VÇBÀ¢’2fÆ–FF–öåöÖö6³ ¢6VÆbæÆ7E÷F&vWE÷fÆ–FF–öåöÖö6²ÒfÆ–FF–öåöÖö6°¢6ÆÅö6öÖÖæB€¢'6VæE÷&WfVçVUö6æ'’"À¢"ÒÖöffW"ÖÆörÖ–B"À¢÷F–öç5²&öffW%öÆöuö–B%ÒÀ¢"ÒÖ7W7FöÖW"Ö–B"À¢÷F–öç5²&7W7FöÖW%ö–B%ÒÀ¢"ÒÖ&÷B×W6W"Ö–B"À¢÷F–öç5²&&÷E÷W6W%ö–B%ÒÀ¢"ÒÖ6öæf—&Ò"À¢÷F–öç5²&6öæf—&Ò%ÒÀ¢7FF÷WCÖ÷WBÀ¢¢&WGW&â÷WBævWGfÇVR‚ ¢FVb7&VFUö&F6…ö6æF–FFR‡6VÆbÂ7Vff—‚Â¢¦W‡G&“ ¢7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÖb$&F6‚·7Vff—‡Ò"ÂW6W&æÖSÖb&&F6‚×·7Vff—‡Ò"¢&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#Ö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÖb&&F6‚×·7Vff—‡Ò"À¢6†Eö–CÖb&&F6‚×·7Vff—‡Ò"À¢W6W&æÖSÖb&&F6‡·7Vff—‡Ò"À¢F—7Æ•öæÖSÖb$&F6‚·7Vff—‡Ò"À¢¢&WGW&â6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör†7W7FöÖW#Ö7W7FöÖW"Â&÷E÷W6W#Ö&÷E÷W6W"Â¢¦W‡G& ¢FVb6ÆÅöÆ–Ö—FVEö&F6‚‡6VÆbÂ¢Â&Wf–WsÔfÇ6RÂ6öæf—&ÓÔfÇ6RÂÆ–Ö—CÓ2ÂfW&&÷6SÔfÇ6RÂ&WG'•÷G&ç6–VçEöf–ÆVCÔfÇ6R“ ¢&w2Ò°¢'6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚"À¢"ÒÖVæv–æR"À¢'&WFVçF–öâ"À¢"ÒÖWfVçB"À¢'W6W%ö–æ7F—fUós&‚"À¢"ÒÖÆ–Ö—B"À¢7G"†Æ–Ö—B’À¢"ÒÖF—2"À¢#r"À¢Ð¢–b&Wf–Ws ¢&w2æVæB‚"Ò×&Wf–Wr"¢–b6öæf—&Ó ¢&w2æW‡FVæB…²"ÒÖ6öæf—&Ò"Â%4TäEôÄ”Ô•DTEõ$UdTåTUô$D4‚%Ò¢–bfW&&÷6S ¢&w2æVæB‚"Ò×fW&&÷6R"¢–b&WG'•÷G&ç6–VçEöf–ÆVC ¢&w2æVæB‚"Ò×&WG'’×G&ç6–VçBÖf–ÆVB"¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚¦&w2Â7FF÷WCÖ÷WB¢&WGW&â÷WBævWGfÇVR‚ ¢F6‚‚'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"Â&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&–B#¢C'×Ò’¢FVbFW7E÷F&vWE÷fÆ–FF÷%övWEö6†E÷7V66W72‡6VÆbÂ÷7EöÖö6²“ ¢g&öÒçFVÆVw&Õö&÷BçF&vWE÷fÆ–FF–öâ–×÷'BfÆ–FFU÷FVÆVw&Õ÷F&vW@ ¢&W7VÇBÒfÆ–FFU÷FVÆVw&Õ÷F&vWB‡6VÆbæ&÷E÷W6W" ¢6VÆbæ76W'EG'VR‡&W7VÇE²&ö²%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²'&V6öâ%ÒÂ'fÆ–B"¢6VÆbæ76W'EG'VR‡÷7EöÖö6²æ6ÆÅö&w2æ&w5³ÒæVæG7v—F‚‚"övWD6†B"’¢6VÆbæ76W'DfÇ6R‡÷7EöÖö6²æ6ÆÅö&w2æ&w5³ÒæVæG7v—F‚‚"÷6VæDÖW76vR"’ ¢F6‚€¢'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"À¢&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‡²&ö²#¢fÇ6RÂ&FW67&—F–öâ#¢$&B&WVW7C¢6†Bæ÷Bf÷VæB'Ò’À¢¢FVbFW7E÷F&vWE÷fÆ–FF÷%ö6†Eöæ÷Eöf÷VæB‡6VÆbÂ÷÷7EöÖö6²“ ¢g&öÒçFVÆVw&Õö&÷BçF&vWE÷fÆ–FF–öâ–×÷'BfÆ–FFU÷FVÆVw&Õ÷F&vW@ ¢&W7VÇBÒfÆ–FFU÷FVÆVw&Õ÷F&vWB‡6VÆbæ&÷E÷W6W" ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²&ö²%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²'&V6öâ%ÒÂ&6†Eöæ÷Eöf÷VæB"¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷E÷W6W"æ6†Eö–BÂ&W7VÇE²'6fUöW'&÷"%Ò ¢F6‚€¢'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"À¢&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‡²&ö²#¢fÇ6RÂ&FW67&—F–öâ#¢$f÷&&–FFVã¢&÷Bv2&Æö6¶VB'’F†RW6W"'Ò’À¢¢FVbFW7E÷F&vWE÷fÆ–FF÷%ö&÷Eö&Æö6¶VB‡6VÆbÂ÷÷7EöÖö6²“ ¢g&öÒçFVÆVw&Õö&÷BçF&vWE÷fÆ–FF–öâ–×÷'BfÆ–FFU÷FVÆVw&Õ÷F&vW@ ¢&W7VÇBÒfÆ–FFU÷FVÆVw&Õ÷F&vWB‡6VÆbæ&÷E÷W6W" ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²&ö²%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²'&V6öâ%ÒÂ&&÷Eö&Æö6¶VB" ¢F6‚‚'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"Â6–FUöVffV7C×&WVW7G2æW†6WF–öç2åF–ÖV÷WB‚%&VBF–ÖVB÷WB"’¢FVbFW7E÷F&vWE÷fÆ–FF÷%÷F–ÖV÷WB‡6VÆbÂ÷÷7EöÖö6²“ ¢g&öÒçFVÆVw&Õö&÷BçF&vWE÷fÆ–FF–öâ–×÷'BfÆ–FFU÷FVÆVw&Õ÷F&vW@ ¢&W7VÇBÒfÆ–FFU÷FVÆVw&Õ÷F&vWB‡6VÆbæ&÷E÷W6W" ¢6VÆbæ76W'DfÇ6R‡&W7VÇE²&ö²%Ò¢6VÆbæ76W'DWVÂ‡&W7VÇE²'&V6öâ%ÒÂ'F–ÖV÷WB" ¢FVbFW7E÷fÆ–FFU÷&WfVçVU÷F&vWG5öæWfW%÷6VæG5öæE÷&V6öÖÖVæG5÷fÆ–Eö6æF–FFR‡6VÆb“ ¢–çfÆ–EöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚¢fÆ–Eö7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR†F—7Æ•öæÖSÒ%fÆ–B6æF–FFR"ÂW6W&æÖSÒ'fÆ–BÖ6æF–FFR"¢fÆ–Eö&÷E÷W6W"Ò&÷EW6W"æö&¦V7G2æ7&VFR€¢&÷Eö6öæf–s×6VÆbæ&÷Eö6öæf–rÀ¢7W7FöÖW#×fÆ–Eö7W7FöÖW"À¢&÷f–FW%÷W6W%ö–CÒ#ssr"À¢6†Eö–CÒ#ssr"À¢W6W&æÖSÒ'fÆ–GW6W""À¢F—7Æ•öæÖSÒ%fÆ–BW6W""À¢¢fÆ–EöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör†7W7FöÖW#×fÆ–Eö7W7FöÖW"Â&÷E÷W6W#×fÆ–Eö&÷E÷W6W" ¢FVbf¶U÷÷7B‡W&ÂÂ§6öãÔæöæRÂ¢¦·v&w2“ ¢6VÆbæ76W'DfÇ6R‡W&ÂæVæG7v—F‚‚"÷6VæDÖW76vR"’¢–b§6öâæB7G"†§6öâævWB‚&6†Eö–B"’’ÓÒ#ssr# ¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&–B#¢ssw×Ò¢&WGW&âGVÖ×”&÷E&W7öç6R‡²&ö²#¢fÇ6RÂ&FW67&—F–öâ#¢$&B&WVW7C¢6†Bæ÷Bf÷VæB'Ò ¢÷WBÒ7G&–æt”ò‚¢v—F‚F6‚‚'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"Â6–FUöVffV7CÖf¶U÷÷7B“ ¢6ÆÅö6öÖÖæB€¢'fÆ–FFU÷&WfVçVU÷F&vWG2"À¢"ÒÖF—2"À¢#r"À¢"ÒÖÆ–Ö—B"À¢#"À¢"ÒÖöæÇ’ÖG'’×'VâÖ6æF–FFW2"À¢"Ò×fW&&÷6R"À¢7FF÷WCÖ÷WBÀ¢¢÷WGWBÒ÷WBævWGfÇVR‚ ¢6VÆbæ76W'D–â‚'fÆ–Eö6æ'•ö6æF–FFUöf÷VæC×–W2"Â÷WGWB¢6VÆbæ76W'D–â†b'&WfVçVUööffW%öÆöu÷³×·fÆ–EöÆörç·Ò"Â÷WGWB¢6VÆbæ76W'Dæ÷D–â†b'&V6öÖÖVæFVEö6æF–FFS¥Æç&WfVçVUööffW%öÆöu÷³×¶–çfÆ–EöÆörç·Ò"Â÷WGWB¢6VÆbæ76W'D–â‚&6†Eöæ÷Eöf÷VæCÓ"Â÷WGWB¢6VÆbæ76W'Dæ÷D–â‚'6VæDÖW76vR"Â÷WGWB¢6VÆbæ76W'Dæ÷D–â‚#ssr"Â÷WGWB¢6VÆbæ76W'Dæ÷D–â‚#C""Â÷WGWB¢6VÆbæ76W'Dæ÷D–â‚##3§Fö¶Vâ"Â÷WGWB ¢FVbFW7E÷fÆ–FFU÷&WfVçVU÷F&vWG5÷w&—FW5÷6fUöÖWFFF‡6VÆb“ ¢6VÆbæ&÷E÷W6W"æ6†Eö–BÒ&6†B×6V7&WB×F&vWB ¢6VÆbæ&÷E÷W6W"ç&÷f–FW%÷W6W%ö–BÒ&6†B×6V7&WB×F&vWB ¢6VÆbæ&÷E÷W6W"ç6fR‡WFFUöf–VÆG3Õ²&6†Eö–B"Â'&÷f–FW%÷W6W%ö–B"Â'WFFVEöB%Ò¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚¢÷WBÒ7G&–æt”ò‚¢v—F‚F6‚€¢'7F÷&RçFVÆVw&Õö&÷Bæ6Æ–VçBç&WVW7G2ç÷7B"À¢&WGW&å÷fÇVSÔGVÖ×”&÷E&W7öç6R‡²&ö²#¢G'VRÂ'&W7VÇB#¢²&–B#¢&6†B×6V7&WB×F&vWB'×Ò’À¢“ ¢6ÆÅö6öÖÖæB€¢'fÆ–FFU÷&WfVçVU÷F&vWG2"À¢"ÒÖF—2"À¢#r"À¢"ÒÖÆ–Ö—B"À¢#"À¢"ÒÖöæÇ’ÖG'’×'VâÖ6æF–FFW2"À¢"Ò×w&—FR×fÆ–FF–öâÖÖWFFF"À¢7FF÷WCÖ÷WBÀ¢ ¢6÷W&6UöÆörç&Vg&W6…ög&öÕöF"‚¢F&vWE÷fÆ–FF–öâÒ6÷W&6UöÆöræÖWFFF²'F&vWE÷fÆ–FF–öâ%Ð¢6VÆbæ76W'EG'VR‡F&vWE÷fÆ–FF–öå²&ö²%Ò¢6VÆbæ76W'DWVÂ‡F&vWE÷fÆ–FF–öå²'&V6öâ%ÒÂ'fÆ–B"¢–ÆöBÒ§6öâæGV×2‡6÷W&6UöÆöræÖWFFF¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷E÷W6W"æ6†Eö–BÂ–ÆöB¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷Eö6öæf–ræ&÷E÷Fö¶VâÂ–ÆöB¢6VÆbç7F÷&Rç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'EG'VR‡6VÆbç7F÷&Rç&WfVçVUöVæv–æUöG'•÷'Vâ ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•÷&WV—&W5ö6öæf—&ÖF–öâ‡6VÆbÂ6VæEöÖö6²“ ¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚ ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆörÂ6öæf—&ÓÒ$äõR" ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•÷&V¦V7G5ö–çfÆ–Eö6æF–FFR‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‡f&–çCÒ$"" ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•÷6VæG5öW†7FÇ•ööæUöÖW76vUöæEö¶VW5övÆö&ÅöG'•÷'Vâ‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚ ¢÷WGWBÒ6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VÆbç7F÷&Rç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'EG'VR‡6VÆbç7F÷&Rç&WfVçVUöVæv–æUöG'•÷'Vâ¢6VÆbæ76W'D–â‚%&WfVçVR6æ'’6VçB"Â÷WGWB¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢6VçEöÆöw2Ò&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2å4TåB¢6VÆbæ76W'DWVÂ‡6VçEöÆöw2æ6÷VçB‚’Â¢6VçEöÆörÒ6VçEöÆöw2ævWB‚¢6VÆbæ76W'DWVÂ‡6VçEöÆöræ7W7FöÖW"Â6VÆbæ7W7FöÖW"¢6VÆbæ76W'EG'VR‡6VçEöÆöræÖWFFF²&6æ'’%Ò¢6VÆbæ76W'DWVÂ‡6VçEöÆöræÖWFFF²'6÷W&6UöG'•÷'VåöÆöuö–B%ÒÂ6÷W&6UöÆörç²¢6VÆbæ76W'D–â‚&6æ'•ö6öÖÖæE÷fW'6–öâ"Â6VçEöÆöræÖWFFF¢6÷W&6UöÆörç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡6÷W&6UöÆöræÖWFFF²&6æ'•÷6VçEöÆöuö–B%ÒÂ6VçEöÆörç²¢6VÆbæ76W'D–â‚&6æ'•÷6VçEöB"Â6÷W&6UöÆöræÖWFFF¢6VÆbæ76W'D–â‚&6æ'•ö6öÖÖæE÷fW'6–öâ"Â6÷W&6UöÆöræÖWFFF¢6VÆbæÆ7E÷F&vWE÷fÆ–FF–öåöÖö6²æ76W'Eö6ÆÆVEööæ6R‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•÷6÷W&6UöÆöuö—5ö–FV×÷FVçEögFW%÷6VçB‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢v—F‚6VÆbæ76W'E&—6W4ÖW76vR„6öÖÖæDW'&÷"Â&Ç&VG’GFV×FVB"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2å4TåB’æ6÷VçB‚’Â ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•övÆö&Åö6ööÆF÷våö&Æö6·5÷6V6öæE÷&V6VçEö6æ'’‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚¢6VÆbæ7&VFUööffW%öÆör€¢ÖWFFF×²&6æ'’#¢G'VRÂ'6÷W&6UöG'•÷'VåöÆöuö–B#¢““’Â&6æ'•ö6öÖÖæE÷fW'6–öâ#¢'FW7B'ÒÀ¢ ¢v—F‚6VÆbæ76W'E&—6W4ÖW76vR„6öÖÖæDW'&÷"Â&æ÷F†W"6æ'’v26VçBv—F†–âF†RÆ7B#B†÷W'2"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•öf–ÆVE÷6÷W&6UöÆöuö&Æö6·5÷&WG'’‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚¢f–ÆVEöÆörÒ6VÆbæ7&VFUööffW%öÆör€¢7FGW3Õ&WfVçVTöffW$Æörå7FGW2äd”ÄTBÀ¢6VçEöCÔæöæRÀ¢W'&÷%öÖW76vSÒ'6VæEöf–ÆVB"À¢ÖWFFF×²&6æ'’#¢G'VRÂ'6÷W&6UöG'•÷'VåöÆöuö–B#¢6÷W&6UöÆörç·ÒÀ¢ ¢v—F‚6VÆbæ76W'E&—6W4ÖW76vR„6öÖÖæDW'&÷"Â&Ç&VG’†26æ'’GFV×B"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢6VÆbæ76W'DWVÂ†f–ÆVEöÆörç7FGW2Â&WfVçVTöffW$Æörå7FGW2äd”ÄTB ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•÷&VgW6W5÷v†Vå÷7F÷&UöG'•÷'Våö—5öfÇ6R‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚¢6VÆbç7F÷&Rç&WfVçVUöVæv–æUöG'•÷'VâÒfÇ6P¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&WfVçVUöVæv–æUöG'•÷'Vâ"Â'WFFVEöB%Ò ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•ö¶VW5÷6÷W&6UöG'•÷'VåöÆöu÷Væ6†ævVB‡6VÆbÂ÷6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6÷W&6UöÆörç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡6÷W&6UöÆörç7FGW2Â&WfVçVTöffW$Æörå7FGW2äE%•õ%Tâ¢6VÆbæ76W'DWVÂ‡6÷W&6UöÆörç6¶—÷&V6öâÂ&G'•÷'Vâ" ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•÷&V6VçE÷6VçE÷7W&W76W5÷6VæB‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚¢6VÆbæ7&VFUööffW%öÆör€¢Væv–æU÷G—SÕ&WfVçVTöffW$ÆöräVæv–æUG—Rå$UDTåD”ôâÀ¢7FGW3Õ&WfVçVTöffW$Æörå7FGW2å4TåBÀ¢ ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•öæõ÷FVÆVw&Õ÷F&vWEö—5÷6¶—VE÷6fVÇ’‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚¢6VÆbæ&÷E÷W6W"æ6†Eö–BÒ" ¢6VÆbæ&÷E÷W6W"ç6fR‡WFFUöf–VÆG3Õ²&6†Eö–B"Â'WFFVEöB%Ò ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢6¶—VBÒ&WfVçVTöffW$Æöræö&¦V7G2æW†6ÇVFR‡³×6÷W&6UöÆörç²’ævWB‚¢6VÆbæ76W'DWVÂ‡6¶—VBç7FGW2Â&WfVçVTöffW$Æörå7FGW2å4´•TB¢6VÆbæ76W'DWVÂ‡6¶—VBç6¶—÷&V6öâÂ&æõ÷W'6öæÅ÷FVÆVw&Õ÷F&vWB" ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•ö–çfÆ–E÷fÆ–FFVE÷F&vWEöf–Ç5÷v—F†÷WE÷6VæB‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚ ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö6æ'’€¢6÷W&6UöÆörÀ¢fÆ–FF–öå÷&W7VÇC×²&ö²#¢fÇ6RÂ'&V6öâ#¢&6†Eöæ÷Eöf÷VæB"Â'6fUöW'&÷"#¢&6†Bæ÷Bf÷VæB'ÒÀ¢ ¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢f–ÆVBÒ&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2äd”ÄTB’ævWB‚¢6VÆbæ76W'DWVÂ†f–ÆVBæW'&÷%öÖW76vRÂ'FVÆVw&Õ÷F&vWEö–çfÆ–C¢6†Eöæ÷Eöf÷VæB"¢6VÆbæ76W'DWVÂ†f–ÆVBæÖWFFF²'F&vWE÷fÆ–FF–öâ%Õ²'&V6öâ%ÒÂ&6†Eöæ÷Eöf÷VæB"¢6VÆbæ76W'D–â‚&6æ'•ö6öÖÖæE÷fW'6–öâ"Âf–ÆVBæÖWFFF¢6÷W&6UöÆörç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡6÷W&6UöÆöræÖWFFF²&6æ'•öf–ÆVEöÆöuö–B%ÒÂf–ÆVBç²¢6VÆbæ76W'D–â‚&6æ'•öf–ÆVEöB"Â6÷W&6UöÆöræÖWFFF¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷E÷W6W"æ6†Eö–BÂ§6öâæGV×2†f–ÆVBæÖWFFF’¢6VÆbç7F÷&Rç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'EG'VR‡6VÆbç7F÷&Rç&WfVçVUöVæv–æUöG'•÷'Vâ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â6–FUöVffV7CÕ'VçF–ÖTW'&÷"‚&&ööÒ"’¢FVbFW7Eö6æ'•÷6VæEöf–ÇW&Uö7&VFW5öf–ÆVEöÆör‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚ ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢6VæEöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢f–ÆVBÒ&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2äd”ÄTB’ævWB‚¢6VÆbæ76W'DWVÂ†f–ÆVBæ7W7FöÖW"Â6VÆbæ7W7FöÖW"¢6VÆbæ76W'EG'VR†f–ÆVBæÖWFFF²&6æ'’%Ò¢6÷W&6UöÆörç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡6÷W&6UöÆöræÖWFFF²&6æ'•öf–ÆVEöÆöuö–B%ÒÂf–ÆVBç²¢6VÆbæ76W'D–â‚&6æ'•öf–ÆVEöB"Â6÷W&6UöÆöræÖWFFF ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•öÖWFFFö—5÷6fR‡6VÆbÂ÷6VæEöÖö6²Â÷F–Ö—¦UöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ†ÖW76vSÒ'Fö¶Vâ&6FVfv†–¦¶ÆÖæ÷'7GWgw‡—£#3CScsƒ“VÖ–ÂÆ–6TW†×ÆRæ6öÒ"¢6VÆbæ&÷E÷W6W"æ6†Eö–BÒ&6†B×6V7&WB×F&vWB ¢6VÆbæ&÷E÷W6W"ç&÷f–FW%÷W6W%ö–BÒ&6†B×6V7&WB×F&vWB ¢6VÆbæ&÷E÷W6W"ç6fR‡WFFUöf–VÆG3Õ²&6†Eö–B"Â'&÷f–FW%÷W6W%ö–B"Â'WFFVEöB%Ò¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör†ÖWFFF×²'Fö¶Vâ#¢'6V7&WB'Ò ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢–ÆöBÒ§6öâæGV×2…&WfVçVTöffW$Æöræö&¦V7G2ævWB‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2å4TåB’æÖWFFF¢6VÆbæ76W'D–â‚r&6æ'’#¢G'VRrÂ–ÆöB¢6VÆbæ76W'Dæ÷D–â‚&&6FVfv†–¦¶ÆÖæ÷'7GWgw‡—£#3CScsƒ“"Â–ÆöB¢6VÆbæ76W'Dæ÷D–â‚&Æ–6TW†×ÆRæ6öÒ"Â–ÆöB¢6VÆbæ76W'Dæ÷D–â‚'6V7&WB"Â–ÆöB¢6÷W&6UöÆörç&Vg&W6…ög&öÕöF"‚¢6÷W&6U÷–ÆöBÒ§6öâæGV×2‡6÷W&6UöÆöræÖWFFF¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷E÷W6W"æ6†Eö–BÂ6÷W&6U÷–ÆöB¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷Eö6öæf–ræ&÷E÷Fö¶VâÂ6÷W&6U÷–ÆöB¢6VÆbæ76W'Dæ÷D–â‚'6V7&WB"Â6÷W&6U÷–ÆöB ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç66†VGVÆW"ç'Vå÷&WfVçVU÷66â"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUö6æ'’å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7Eö6æ'•ö6öÖÖæEöFöW5öæ÷E÷'VåövVæW&Å÷66â‡6VÆbÂ÷6VæEöÖö6²Â÷F–Ö—¦UöÖö6²Â66åöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢6÷W&6UöÆörÒ6VÆbæ7&VFUö6æ'•÷6÷W&6UöÆör‚ ¢6VÆbæ6ÆÅö6æ'’‡6÷W&6UöÆör ¢66åöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷&WV—&W5ö6öæf—&Õö&Vf÷&U÷6VæB‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6VÆbæ7&VFUö&F6…ö6æF–FFR‚&6öæf—&Ò" ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‚ ¢fÆ–FF–öåöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷&Wf–WuöæWfW%÷6VæG2‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6÷W&6UöÆörÒ6VÆbæ7&VFUö&F6…ö6æF–FFR‚'&Wf–Wr" ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‡&Wf–WsÕG'VR ¢6VÆbæ76W'D–â‚'7FGW3Õ$Ud”Uuôô²"Â÷WGWB¢6VÆbæ76W'D–â‚'6VÆV7FVEö6÷VçCÓ"Â÷WGWB¢6VÆbæ76W'D–â†b'6VÆV7FVEööffW%öÆöuö–G3Õ··6÷W&6UöÆörç·ÕÒ"Â÷WGWB¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷6VÆV7G5ööæÇ•÷&WFVçF–öåö–æ7F—fUós&‚‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6VÆV7FVBÒ6VÆbæ7&VFUö&F6…ö6æF–FFR‚'&WFVçF–öâ"¢6VÆbæ7&VFUö&F6…ö6æF–FFR‚'W6VÆÂ"ÂVæv–æU÷G—SÕ&WfVçVTöffW$ÆöräVæv–æUG—RåU4TÄÂ¢6VÆbæ7&VFUö&F6…ö6æF–FFR‚&–æ7F—fS#B"ÂWfVçE÷G—SÒ'W6W%ö–æ7F—fUó#F‚" ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‡&Wf–WsÕG'VR ¢6VÆbæ76W'D–â†b'6VÆV7FVEööffW%öÆöuö–G3Õ··6VÆV7FVBç·ÕÒ"Â÷WGWB¢6VÆbæ76W'Dæ÷D–â‚'W6VÆÂ"Â÷WGWB¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷6¶—5ö–çfÆ–E÷F&vWB‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢fÇ6RÂ'&V6öâ#¢&6†Eöæ÷Eöf÷VæB'Ð¢6VÆbæ7&VFUö&F6…ö6æF–FFR‚&–çfÆ–B" ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‡&Wf–WsÕG'VR ¢6VÆbæ76W'D–â‚'6VÆV7FVEö6÷VçCÓ"Â÷WGWB¢6VÆbæ76W'D–â‚'6¶—VEö–çfÆ–E÷F&vWCÓ"Â÷WGWB¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷6¶—5ö7W7FöÖW%÷v—F…÷&V6VçE÷6VçB‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6÷W&6UöÆörÒ6VÆbæ7&VFUö&F6…ö6æF–FFR‚'&V6VçB"¢6VÆbæ7&VFUööffW%öÆör†7W7FöÖW#×6÷W&6UöÆöræ7W7FöÖW"Â&÷E÷W6W#×6÷W&6UöÆöræ&÷E÷W6W" ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‡&Wf–WsÕG'VR ¢6VÆbæ76W'D–â‚'6VÆV7FVEö6÷VçCÓ"Â÷WGWB¢6VÆbæ76W'D–â‚'6¶—VE÷&V6VçE÷6VçCÓ"Â÷WGWB¢fÆ–FF–öåöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷6¶—5öW†—7F–æuöGFV×B‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6÷W&6UöÆörÒ6VÆbæ7&VFUö&F6…ö6æF–FFR‚&GFV×B"¢6VÆbæ7&VFUööffW%öÆör€¢7W7FöÖW#×6÷W&6UöÆöræ7W7FöÖW"À¢&÷E÷W6W#×6÷W&6UöÆöræ&÷E÷W6W"À¢7FGW3Õ&WfVçVTöffW$Æörå7FGW2äd”ÄTBÀ¢6VçEöCÔæöæRÀ¢ÖWFFF×²&Æ–Ö—FVEö&F6‚#¢G'VRÂ'6÷W&6UöG'•÷'VåöÆöuö–B#¢6÷W&6UöÆörç·ÒÀ¢ ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‡&Wf–WsÕG'VR ¢6VÆbæ76W'D–â‚'6VÆV7FVEö6÷VçCÓ"Â÷WGWB¢6VÆbæ76W'D–â‚'6¶—VEöW†—7F–æuöGFV×CÓ"Â÷WGWB¢fÆ–FF–öåöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'F–ÖRç6ÆVW"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷&W7V7G5öÖ…öÆ–Ö—E÷F‡&VR‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²Â6ÆVWöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢f÷"–æFW‚–â&ævRƒR“ ¢6VÆbæ7&VFUö&F6…ö6æF–FFR†b&Æ–Ö—B×¶–æFW‡Ò" ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚†6öæf—&ÓÕG'VRÂÆ–Ö—CÓR ¢6VÆbæ76W'D–â‚'7FGW3ÔÄ”Ô•DTEô$D4…ôô²"Â÷WGWB¢6VÆbæ76W'D–â‚'6VçEö6÷VçCÓ2"Â÷WGWB¢6VÆbæ76W'DWVÂ‡6VæEöÖö6²æ6ÆÅö6÷VçBÂ2¢6VÆbæ76W'DWVÂ‡6ÆVWöÖö6²æ6ÆÅö6÷VçBÂ"¢6VÆbç7F÷&Rç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'EG'VR‡6VÆbç7F÷&Rç&WfVçVUöVæv–æUöG'•÷'Vâ ¢F6‚‚'F–ÖRç6ÆVW"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…öÆÅ÷W6W5÷7F÷&UöF–Ç•ö6‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²Â6ÆVWöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6VÆbç7F÷&Rç&WfVçVUöÖ…÷F÷FÅööffW'5÷W%öF’Ò@¢6VÆbç7F÷&Rç6fR‡WFFUöf–VÆG3Õ²'&WfVçVUöÖ…÷F÷FÅööffW'5÷W%öF’"Â'WFFVEöB%Ò¢f÷"–æFW‚–â&ævRƒb“ ¢6VÆbæ7&VFUö&F6…ö6æF–FFR†b&ÆÂ×¶–æFW‡Ò" ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚†6öæf—&ÓÕG'VRÂÆ–Ö—CÒ&ÆÂ" ¢6VÆbæ76W'D–â‚&Æ–Ö—EöÖöFUöÆÃÓ"Â÷WGWB¢6VÆbæ76W'D–â‚&F–Ç•ö6ö6öæf–wW&VCÓB"Â÷WGWB¢6VÆbæ76W'D–â‚&6÷W6VCÓB"Â÷WGWB¢6VÆbæ76W'D–â‚&æ÷E÷6VÆV7FVEöGVU÷Fõö6Ó""Â÷WGWB¢6VÆbæ76W'D–â‚&W7F–ÖFVE÷&VÅ÷6VæG3ÓB"Â÷WGWB¢6VÆbæ76W'D–â‚'6VçEö6÷VçCÓB"Â÷WGWB¢6VÆbæ76W'DWVÂ‡6VæEöÖö6²æ6ÆÅö6÷VçBÂB¢6VÆbæ76W'DWVÂ‡6ÆVWöÖö6²æ6ÆÅö6÷VçBÂ2¢6VÆbç7F÷&Rç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'EG'VR‡6VÆbç7F÷&Rç&WfVçVUöVæv–æUöG'•÷'Vâ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷&WV—&W5÷&WG'•öfÆuöf÷%÷G&ç6–VçEöf–ÆVEöGFV×B€¢6VÆbÀ¢6VæEöÖö6²À¢÷F–Ö—¦UöÖö6²À¢fÆ–FF–öåöÖö6²À¢“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6÷W&6UöÆörÒ6VÆbæ7&VFUö&F6…ö6æF–FFR‚'G&ç6–VçB×&WG'’"¢f–ÆVEöÆörÒ6VÆbæ7&VFUööffW%öÆör€¢7W7FöÖW#×6÷W&6UöÆöræ7W7FöÖW"À¢&÷E÷W6W#×6÷W&6UöÆöræ&÷E÷W6W"À¢7FGW3Õ&WfVçVTöffW$Æörå7FGW2äd”ÄTBÀ¢6VçEöCÔæöæRÀ¢W'&÷%öÖW76vSÒ%&VBF–ÖVB÷WBv†–ÆR6öææV7F–ærF‡&÷Vv‚&÷‡’"À¢ÖWFFF×²&Æ–Ö—FVEö&F6‚#¢G'VRÂ'6÷W&6UöG'•÷'VåöÆöuö–B#¢6÷W&6UöÆörç·ÒÀ¢¢6÷W&6UöÆöræÖWFFFÒ²&Æ–Ö—FVEö&F6…öf–ÆVEöÆöuö–B#¢f–ÆVEöÆörç·Ð¢6÷W&6UöÆörç6fR‡WFFUöf–VÆG3Õ²&ÖWFFF%Ò ¢v—F†÷WE÷&WG'’Ò6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‡&Wf–WsÕG'VRÂÆ–Ö—CÒ&ÆÂ"¢v—F…÷&WG'’Ò6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‡&Wf–WsÕG'VRÂÆ–Ö—CÒ&ÆÂ"Â&WG'•÷G&ç6–VçEöf–ÆVCÕG'VR ¢6VÆbæ76W'D–â‚'6VÆV7FVEö6÷VçCÓ"Âv—F†÷WE÷&WG'’¢6VÆbæ76W'D–â‚'6¶—VEöW†—7F–æuöGFV×CÓ"Âv—F†÷WE÷&WG'’¢6VÆbæ76W'D–â‚'6VÆV7FVEö6÷VçCÓ"Âv—F…÷&WG'’¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…÷&WG'•öfÆu÷7F–ÆÅ÷6¶—5öæöå÷G&ç6–VçEöf–ÆVEöGFV×B€¢6VÆbÀ¢6VæEöÖö6²À¢÷F–Ö—¦UöÖö6²À¢fÆ–FF–öåöÖö6²À¢“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6÷W&6UöÆörÒ6VÆbæ7&VFUö&F6…ö6æF–FFR‚&–çfÆ–B×&WG'’"¢f–ÆVEöÆörÒ6VÆbæ7&VFUööffW%öÆör€¢7W7FöÖW#×6÷W&6UöÆöræ7W7FöÖW"À¢&÷E÷W6W#×6÷W&6UöÆöræ&÷E÷W6W"À¢7FGW3Õ&WfVçVTöffW$Æörå7FGW2äd”ÄTBÀ¢6VçEöCÔæöæRÀ¢W'&÷%öÖW76vSÒ'FVÆVw&Õ÷F&vWEö–çfÆ–C¢6†Eöæ÷Eöf÷VæB"À¢ÖWFFF×²&Æ–Ö—FVEö&F6‚#¢G'VRÂ'6÷W&6UöG'•÷'VåöÆöuö–B#¢6÷W&6UöÆörç·ÒÀ¢¢6÷W&6UöÆöræÖWFFFÒ²&Æ–Ö—FVEö&F6…öf–ÆVEöÆöuö–B#¢f–ÆVEöÆörç·Ð¢6÷W&6UöÆörç6fR‡WFFUöf–VÆG3Õ²&ÖWFFF%Ò ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚‡&Wf–WsÕG'VRÂÆ–Ö—CÒ&ÆÂ"Â&WG'•÷G&ç6–VçEöf–ÆVCÕG'VR ¢6VÆbæ76W'D–â‚'6VÆV7FVEö6÷VçCÓ"Â÷WGWB¢6VÆbæ76W'D–â‚'6¶—VEöW†—7F–æuöGFV×CÓ"Â÷WGWB¢fÆ–FF–öåöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢6VæEöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢F6‚‚'F–ÖRç6ÆVW"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"¢FVbFW7EöÆ–Ö—FVEö&F6…÷7F÷5ögFW%÷6VæEöf–ÇW&R‡6VÆbÂ6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²Â÷6ÆVWöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6VæEöÖö6²ç6–FUöVffV7BÒµG'VRÂ'VçF–ÖTW'&÷"‚&&ööÒ"’ÂG'VUÐ¢f÷"–æFW‚–â&ævRƒ2“ ¢6VÆbæ7&VFUö&F6…ö6æF–FFR†b&f–Â×¶–æFW‡Ò" ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚†6öæf—&ÓÕG'VR ¢6VÆbæ76W'D–â‚'7FGW3ÔÄ”Ô•DTEô$D4…ôd”ÄTB"Â÷WGWB¢6VÆbæ76W'DWVÂ‡6VæEöÖö6²æ6ÆÅö6÷VçBÂ"¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2å4TåB’æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ…&WfVçVTöffW$Æöræö&¦V7G2æf–ÇFW"‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2äd”ÄTB’æ6÷VçB‚’Â ¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…öÖWFFFö—5÷6fUöæE÷WFFW5÷6÷W&6R‡6VÆbÂ÷6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ†ÖW76vSÒ'Fö¶Vâ&6FVfv†–¦¶ÆÖæ÷'7GWgw‡—£#3CScsƒ“VÖ–ÂÆ–6TW†×ÆRæ6öÒ"¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6÷W&6UöÆörÒ6VÆbæ7&VFUö&F6…ö6æF–FFR‚'6fR"ÂÖWFFF×²'Fö¶Vâ#¢'6V7&WB'Ò¢6÷W&6UöÆöræ&÷E÷W6W"æ6†Eö–BÒ&6†B×6V7&WB×F&vWB ¢6÷W&6UöÆöræ&÷E÷W6W"ç&÷f–FW%÷W6W%ö–BÒ&6†B×6V7&WB×F&vWB ¢6÷W&6UöÆöræ&÷E÷W6W"ç6fR‡WFFUöf–VÆG3Õ²&6†Eö–B"Â'&÷f–FW%÷W6W%ö–B"Â'WFFVEöB%Ò ¢÷WGWBÒ6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚†6öæf—&ÓÕG'VR ¢6VÆbæ76W'D–â‚'7FGW3ÔÄ”Ô•DTEô$D4…ôô²"Â÷WGWB¢6VçEöÆörÒ&WfVçVTöffW$Æöræö&¦V7G2ævWB‡7FGW3Õ&WfVçVTöffW$Æörå7FGW2å4TåB¢6VÆbæ76W'EG'VR‡6VçEöÆöræÖWFFF²&Æ–Ö—FVEö&F6‚%Ò¢6VÆbæ76W'D–â‚&&F6…ö6öÖÖæE÷fW'6–öâ"Â6VçEöÆöræÖWFFF¢6VÆbæ76W'DWVÂ‡6VçEöÆöræÖWFFF²'6÷W&6UöG'•÷'VåöÆöuö–B%ÒÂ6÷W&6UöÆörç²¢6÷W&6UöÆörç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡6÷W&6UöÆöræÖWFFF²&Æ–Ö—FVEö&F6…÷6VçEöÆöuö–B%ÒÂ6VçEöÆörç²¢–ÆöBÒ§6öâæGV×2‡6VçEöÆöræÖWFFF¢6÷W&6U÷–ÆöBÒ§6öâæGV×2‡6÷W&6UöÆöræÖWFFF¢f÷"&r–â°¢&6†B×6V7&WB×F&vWB"À¢6VÆbæ&÷Eö6öæf–ræ&÷E÷Fö¶VâÀ¢&&6FVfv†–¦¶ÆÖæ÷'7GWgw‡—£#3CScsƒ“"À¢&Æ–6TW†×ÆRæ6öÒ"À¢'6V7&WB"À¢Ó ¢6VÆbæ76W'Dæ÷D–â‡&rÂ–ÆöB¢6VÆbæ76W'Dæ÷D–â‡&rÂ6÷W&6U÷–ÆöB ¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç66†VGVÆW"ç'Vå÷&WfVçVU÷66â"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚çfÆ–FFU÷FVÆVw&Õ÷F&vWB"¢F6‚‚'7F÷&RæÖævVÖVçBæ6öÖÖæG2ç6VæE÷&WfVçVUöÆ–Ö—FVEö&F6‚å&WFVçF–öäVæv–æRåö÷F–Ö—¦UööffW""¢F6‚‚'7F÷&Rç&WfVçVUöVæv–æRç&WFVçF–öâæ7F–öç2ç6VæE÷Fõö6öæf–r"Â&WGW&å÷fÇVSÕG'VR¢FVbFW7EöÆ–Ö—FVEö&F6…öFöW5öæ÷E÷'VåövVæW&Å÷66â‡6VÆbÂ÷6VæEöÖö6²Â÷F–Ö—¦UöÖö6²ÂfÆ–FF–öåöÖö6²Â66åöÖö6²“ ¢÷F–Ö—¦UöÖö6²ç&WGW&å÷fÇVRÒ6VÆbæ•öFV6—6–öâ‚¢fÆ–FF–öåöÖö6²ç&WGW&å÷fÇVRÒ²&ö²#¢G'VRÂ'&V6öâ#¢'fÆ–B'Ð¢6VÆbæ7&VFUö&F6…ö6æF–FFR‚'66â" ¢6VÆbæ6ÆÅöÆ–Ö—FVEö&F6‚†6öæf—&ÓÕG'VR ¢66åöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚  ¦6Æ72FF&6U6WGF–æw57v—F6…FW7G2…6–×ÆUFW7D66R“ ¢&WõöF—"ÒF‚…õöf–ÆUõò’ç&W6öÇfR‚’ç&VçBç&Vç@ ¢FVb'Vå÷6WGF–æw5ö–×÷'B‡6VÆbÂVçe÷WFFW3ÔæöæR“ ¢VçbÒ÷2æVçf—&öâæ6÷’‚¢f÷"¶W’–â€¢$DD$4UôTät”äR"À¢%5Ä•DUôDD$4UõD‚"À¢%õ5Du$U5ôD""À¢%õ5Du$U5õU4U""À¢%õ5Du$U5õ55tõ$B"À¢%õ5Du$U5ô„õ5B"À¢%õ5Du$U5õõ%B"À¢%õ5Du$U5ô4ôäåôÔ…ôtR"À¢%õ5Du$U5õ54ÄÔôDR"À¢“ ¢Vçbç÷†¶W’ÂæöæR¢Vçe²%•D„ôåD‚%ÒÒ7G"‡6VÆbç&WõöF—"¢Vçe²%•D„ôäDôåEu$•DT%•DT4ôDR%ÒÒ# ¢VçbçWFFR†Vçe÷WFFW2÷"·Ò¢&WGW&â7V'&ö6W72ç'Vâ€¢°¢7—2æW†V7WF&ÆRÀ¢"Ö2"À¢€¢&–×÷'B§6öã² ¢&g&öÒ6÷&Rç6WGF–æw2–×÷'B&6S² ¢'&–çB†§6öâæGV×2†&6RäDD$4U5²vFVfVÇBuÒÂFVfVÇC×7G"Â6÷'Eö¶W—3ÕG'VR’’ ¢’À¢ÒÀ¢7vC×6VÆbç&WõöF—"À¢VçcÖVçbÀ¢FW‡CÕG'VRÀ¢6GW&Uö÷WGWCÕG'VRÀ¢6†V6³ÔfÇ6RÀ¢ ¢FVbFW7EöFVfVÇEöFF&6UöVæv–æU÷W6W5÷7Æ—FR‡6VÆb“ ¢&W7VÇBÒ6VÆbç'Vå÷6WGF–æw5ö–×÷'B‚ ¢6VÆbæ76W'DWVÂ‡&W7VÇBç&WGW&æ6öFRÂÂ&W7VÇBç7FFW'"¢FF&6RÒ§6öâæÆöG2‡&W7VÇBç7FF÷WB¢6VÆbæ76W'DWVÂ†FF&6U²$Tät”äR%ÒÂ&F¦ævòæF"æ&6¶VæG2ç7Æ—FS2"¢6VÆbæ76W'EG'VR†FF&6U²$äÔR%ÒæVæG7v—F‚‚&F"ç7Æ—FS2"’ ¢FVbFW7E÷7Æ—FUöFF&6UöVæv–æU÷W6W5ö6öæf–wW&VE÷F‚‡6VÆb“ ¢&W7VÇBÒ6VÆbç'Vå÷6WGF–æw5ö–×÷'B€¢°¢$DD$4UôTät”äR#¢'7Æ—FR"À¢%5Ä•DUôDD$4UõD‚#¢"÷F×÷6VF²×FW7Bç7Æ—FS2"À¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7VÇBç&WGW&æ6öFRÂÂ&W7VÇBç7FFW'"¢FF&6RÒ§6öâæÆöG2‡&W7VÇBç7FF÷WB¢6VÆbæ76W'DWVÂ†FF&6U²$Tät”äR%ÒÂ&F¦ævòæF"æ&6¶VæG2ç7Æ—FS2"¢6VÆbæ76W'DWVÂ†FF&6U²$äÔR%ÒÂ"÷F×÷6VF²×FW7Bç7Æ—FS2" ¢FVbFW7E÷÷7Fw&W5öFF&6UöVæv–æU÷W6W5÷÷7Fw&W5ö&6¶VæB‡6VÆb“ ¢&W7VÇBÒ6VÆbç'Vå÷6WGF–æw5ö–×÷'B€¢°¢$DD$4UôTät”äR#¢'÷7Fw&W7Â"À¢%õ5Du$U5ôD"#¢'6VFµ÷FW7B"À¢%õ5Du$U5õU4U"#¢'6VFµ÷W6W""À¢%õ5Du$U5õ55tõ$B#¢''VçF–ÖRÖöæÇ’×77v÷&B"À¢%õ5Du$U5ô„õ5B#¢&F"æ–çFW&æÂ"À¢%õ5Du$U5õõ%B#¢#SSCB"À¢%õ5Du$U5ô4ôäåôÔ…ôtR#¢##"À¢%õ5Du$U5õ54ÄÔôDR#¢'&WV—&R"À¢Ð¢ ¢6VÆbæ76W'DWVÂ‡&W7VÇBç&WGW&æ6öFRÂÂ&W7VÇBç7FFW'"¢FF&6RÒ§6öâæÆöG2‡&W7VÇBç7FF÷WB¢6VÆbæ76W'DWVÂ†FF&6U²$Tät”äR%ÒÂ&F¦ævòæF"æ&6¶VæG2ç÷7Fw&W7Â"¢6VÆbæ76W'DWVÂ†FF&6U²$äÔR%ÒÂ'6VFµ÷FW7B"¢6VÆbæ76W'DWVÂ†FF&6U²%U4U"%ÒÂ'6VFµ÷W6W""¢6VÆbæ76W'DWVÂ†FF&6U²%55tõ$B%ÒÂ''VçF–ÖRÖöæÇ’×77v÷&B"¢6VÆbæ76W'DWVÂ†FF&6U²$„õ5B%ÒÂ&F"æ–çFW&æÂ"¢6VÆbæ76W'DWVÂ†FF&6U²%õ%B%ÒÂ#SSCB"¢6VÆbæ76W'DWVÂ†FF&6U²$4ôäåôÔ…ôtR%ÒÂ#¢6VÆbæ76W'DWVÂ†FF&6U²$õD”ôå2%ÒÂ²'76ÆÖöFR#¢'&WV—&R'Ò ¢FVbFW7Eö–çfÆ–EöFF&6UöVæv–æU÷&—6W5ö–×&÷W&Ç•ö6öæf–wW&VB‡6VÆb“ ¢&W7VÇBÒ6VÆbç'Vå÷6WGF–æw5ö–×÷'B‡²$DD$4UôTät”äR#¢&×—7Â'Ò ¢6VÆbæ76W'Dæ÷DWVÂ‡&W7VÇBç&WGW&æ6öFRÂ¢6VÆbæ76W'D–â‚$DD$4UôTät”äR×W7B&RöæRöb"Â&W7VÇBç7FFW'" ¢FVbFW7E÷÷7Fw&W5÷&WV—&W5÷77v÷&B‡6VÆb“ ¢&W7VÇBÒ6VÆbç'Vå÷6WGF–æw5ö–×÷'B‡²$DD$4UôTät”äR#¢'÷7Fw&W2'Ò ¢6VÆbæ76W'Dæ÷DWVÂ‡&W7VÇBç&WGW&æ6öFRÂ¢6VÆbæ76W'D–â‚%õ5Du$U5õ55tõ$B"Â&W7VÇBç7FFW'"  ¦6Æ727FF–4f–ÆW4Ö–FFÆWv&UFW7G2…6–×ÆUFW7D66R“ ¢FVbFW7E÷v†—FVæö—6U÷6W'fW5ö6öÆÆV7FVE÷7FF–5ö–åö6öçF–æW%÷'VçF–ÖR‡6VÆb“ ¢6VÆbæ76W'D–â‚'v†—FVæö—6RæÖ–FFÆWv&Råv†—FTæö—6TÖ–FFÆWv&R"Â6WGF–æw2äÔ”DDÄUt$R  ¦6Æ72&ö÷G7G&–ç7FÆÄ6öÖÖæEFW7G2…FW7D66R“ ¢FÖ–å÷77v÷&BÒ&FÖ–â×7WW"×6V7&WB ¢&÷E÷Fö¶VâÒ'FW7BÖ&÷B×Fö¶Vã§Æ6V†öÆFW" ¢æVÅ÷77v÷&BÒ'æVÂ×7WW"×6V7&WB  ¢FVbw&—FUö6öæf–r‡6VÆbÂ6öæf–r“ ¢FV×öF—"ÒFV×f–ÆRåFV×÷&'”F—&V7F÷'’‚¢6VÆbæFD6ÆVçW‡FV×öF—"æ6ÆVçW¢F‚ÒF‚‡FV×öF—"ææÖR’ò&–ç7FÆÂæ6öæf–ræ§6öâ ¢F‚çw&—FU÷FW‡B†§6öâæGV×2†6öæf–r’ÂVæ6öF–æsÒ'WFbÓ‚"¢&WGW&âF€ ¢FVb&6Uö6öæf–r‡6VÆbÂ¢ÂFVÆVw&ÓÕG'VRÂ‡V“ÔfÇ6R“ ¢&WGW&â°¢&#¢°¢&–ç7FÆÅöF—"#¢"ö÷B÷gâ×7F÷&R"À¢&FöÖ–â#¢&W†×ÆRæ6öÒ"À¢&Væ&ÆU÷FÇ2#¢fÇ6RÀ¢'F–ÖW¦öæR#¢$6–õFV‡&â"À¢&ÆæwVvR#¢&f"À¢ÒÀ¢&FÖ–â#¢°¢'W6W&æÖR#¢&&ö÷G7G&ÖFÖ–â"À¢&VÖ–Â#¢&FÖ–äW†×ÆRæ6öÒ"À¢'77v÷&B#¢6VÆbæFÖ–å÷77v÷&BÀ¢ÒÀ¢&FF&6R#¢°¢&Væv–æR#¢'7Æ—FR"À¢'7Æ—FU÷F‚#¢"ö÷B÷gâ×7F÷&RöFFöF"ç7Æ—FS2"À¢ÒÀ¢'7F÷&R#¢°¢'6ÇVr#¢&&ö÷G7G&×7F÷&R"À¢&æÖR#¢$&ö÷G7G&7F÷&R"À¢&VævÆ—6…öæÖR#¢$&ö÷G7G&7F÷&R"À¢&FöÖ–â#¢&W†×ÆRæ6öÒ"À¢&6&EöçVÖ&W"#¢#"À¢&6&Eö÷væW"#¢$6öæf–wW&R–ÖVçB÷væW""À¢ÒÀ¢'FVÆVw&Ò#¢°¢&Væ&ÆVB#¢FVÆVw&ÒÀ¢&&÷E÷Fö¶Vâ#¢6VÆbæ&÷E÷Fö¶Vâ–bFVÆVw&ÒVÇ6R""À¢&&÷E÷W6W&æÖR#¢&&ö÷G7G&ö&÷B"À¢&FÖ–åö–G2#¢²##3CScsƒ’%ÒÀ¢ÒÀ¢'‡V’#¢°¢&6öæf–wW&Uöæ÷r#¢‡V’À¢&æÖR#¢%&–Ö'’‚ÕT’æVÂ"À¢'æVÅ÷W&Â#¢&‡GG3¢ò÷æVÂæW†×ÆRæ6öÒ"À¢'W6W&æÖR#¢'æVÂÖFÖ–â"À¢'77v÷&B#¢6VÆbçæVÅ÷77v÷&BÀ¢&–æ&÷VæG2#¢°¢°¢&¶W’#¢'&–Ö'’×fÆW72"À¢&–æ&÷VæEö–B#¢À¢'&VÖ&²#¢%&–Ö'’dÄU52"À¢'&÷Fö6öÂ#¢'fÆW72"À¢'6W'fW%ö—#¢'gâæW†×ÆRæ6öÒ"À¢'÷'B#¢#CC2"À¢&6öæf–u÷&×2#¢'G—S×F7g6V7W&—G“ÖæöæR"À¢&æWGv÷&µ÷G—R#¢'F7"À¢'6V7W&—G’#¢&æöæR"À¢Ð¢ÒÀ¢ÒÀ¢'Æç2#¢°¢°¢&¶W’#¢'7F'FW"Ó3B"À¢&æÖR#¢%7F'FW"3B"À¢'G&ff–5öv"#¢#3"À¢&GW&F–öåöF—2#¢3À¢'&–6R#¢À¢&7W'&Væ7’#¢%DôÔâ"À¢&FWf–6UöÆ–Ö—B#¢"À¢&—5÷V&Æ–2#¢G'VRÀ¢Ð¢ÒÀ¢'Æå÷&÷WFW2#¢°¢°¢'Æâ#¢'7F'FW"Ó3B"À¢&–æ&÷VæB#¢'&–Ö'’×fÆW72"À¢'&–÷&—G’#¢À¢'vV–v‡B#¢À¢Ð¢ÒÀ¢'&WfVçVUöVæv–æR#¢°¢&Væ&ÆVB#¢G'VRÀ¢&G'•÷'Vâ#¢G'VRÀ¢ÒÀ¢Ð ¢FVbÖ–æ–ÖÅö6öæf–r‡6VÆb“ ¢&WGW&â°¢&#¢°¢&–ç7FÆÅöF—"#¢"ö÷B÷6VF²"À¢&FöÖ–â#¢""À¢&Væ&ÆU÷FÇ2#¢fÇ6RÀ¢'F–ÖW¦öæR#¢$6–õFV‡&â"À¢&ÆæwVvR#¢&f"À¢ÒÀ¢&FÖ–â#¢°¢'W6W&æÖR#¢'6VF²ÖFÖ–â"À¢&VÖ–Â#¢""À¢'77v÷&B#¢6VÆbæFÖ–å÷77v÷&BÀ¢ÒÀ¢&FF&6R#¢°¢&Væv–æR#¢'7Æ—FR"À¢'7Æ—FU÷F‚#¢"ö÷B÷6VF²öFFöF"ç7Æ—FS2"À¢ÒÀ¢'7F÷&R#¢°¢&æÖR#¢%6VF²"À¢&VævÆ—6…öæÖR#¢%6VF²"À¢ÒÀ¢'FVÆVw&Ò#¢°¢&Væ&ÆVB#¢fÇ6RÀ¢ÒÀ¢'‡V’#¢°¢&6öæf–wW&Uöæ÷r#¢fÇ6RÀ¢ÒÀ¢'&WfVçVUöVæv–æR#¢°¢&Væ&ÆVB#¢G'VRÀ¢&G'•÷'Vâ#¢G'VRÀ¢ÒÀ¢Ð ¢FVb6ÆÅö&ö÷G7G&‡6VÆbÂ6öæf–rÂ¦&w2“ ¢÷WBÒ7G&–æt”ò‚¢W'"Ò7G&–æt”ò‚¢6ÆÅö6öÖÖæB€¢&&ö÷G7G&ö–ç7FÆÂ"À¢"ÒÖ6öæf–r"À¢7G"‡6VÆbçw&—FUö6öæf–r†6öæf–r’’À¢¦&w2À¢7FF÷WCÖ÷WBÀ¢7FFW'#ÖW'"À¢¢&WGW&â÷WBævWGfÇVR‚’ÂW'"ævWGfÇVR‚ ¢FVbö&¦V7Eö6÷VçG2‡6VÆb“ ¢W6W"ÒvWE÷W6W%öÖöFVÂ‚¢&WGW&â°¢'W6W'2#¢W6W"æö&¦V7G2æ6÷VçB‚’À¢'7F÷&W2#¢7F÷&Ræö&¦V7G2æ6÷VçB‚’À¢&&÷G2#¢&÷D6öæf–wW&F–öâæö&¦V7G2æ6÷VçB‚’À¢'æVÇ2#¢æVÂæö&¦V7G2æ6÷VçB‚’À¢&–æ&÷VæG2#¢–æ&÷VæBæö&¦V7G2æ6÷VçB‚’À¢'Æç2#¢Æâæö&¦V7G2æ6÷VçB‚’À¢'&÷WFW2#¢Æä–æ&÷VæE&÷WFRæö&¦V7G2æ6÷VçB‚’À¢Ð ¢FVbFW7EöG'•÷'VåöFöW5öæ÷Eö7&VFUöF%öö&¦V7G2‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÕG'VRÂ‡V“ÕG'VR¢&Vf÷&RÒ6VÆbæö&¦V7Eö6÷VçG2‚ ¢÷WBÂW'"Ò6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"ÒÖG'’×'Vâ" ¢6VÆbæ76W'DWVÂ‡6VÆbæö&¦V7Eö6÷VçG2‚’Â&Vf÷&R¢6VÆbæ76W'D–â‚'v÷VÆEö7&VFR"Â÷WB¢6VÆbæ76W'DWVÂ†W'"Â"" ¢FVbFW7EöG'•÷'Våö66WG5÷÷7Fw&W5ö–ç7FÆÅö6öæf–u÷v—F†÷WEöF%÷w&—FW2‡6VÆb“ ¢6öæf–rÒ6VÆbæÖ–æ–ÖÅö6öæf–r‚¢6öæf–u²&FF&6R%ÒÒ°¢&Væv–æR#¢'÷7Fw&W2"À¢'÷7Fw&W2#¢°¢&FF&6R#¢'6VF²"À¢'W6W"#¢'6VF²"À¢'77v÷&EöVçb#¢%4TDµôD%õ55tõ$B"À¢&†÷7B#¢##rããã"À¢'÷'B#¢SC3"À¢ÒÀ¢'7Æ—FU÷F‚#¢"ö÷B÷6VF²öFFöF"ç7Æ—FS2"À¢Ð¢&Vf÷&RÒ6VÆbæö&¦V7Eö6÷VçG2‚ ¢÷WBÂW'"Ò6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"ÒÖG'’×'Vâ" ¢6VÆbæ76W'DWVÂ‡6VÆbæö&¦V7Eö6÷VçG2‚’Â&Vf÷&R¢6VÆbæ76W'D–â‚$&ö÷G7G&–ç7FÆÂG'’×'Vâ"Â÷WB¢6VÆbæ76W'DWVÂ†W'"Â""¢6VÆbæ76W'Dæ÷D–â‚%4TDµôD%õ55tõ$B"Â÷WB ¢FVbFW7E÷&VÅ÷'Våö7&VFW5öFÖ–å÷7F÷&UöæE÷&WfVçVUöG'•÷'Vâ‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÔfÇ6RÂ‡V“ÔfÇ6R ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢W6W"ÒvWE÷W6W%öÖöFVÂ‚¢W6W"ÒW6W"æö&¦V7G2ævWB‡W6W&æÖSÒ&&ö÷G7G&ÖFÖ–â"¢6VÆbæ76W'EG'VR‡W6W"æ—5÷7Ffb¢6VÆbæ76W'EG'VR‡W6W"æ—5÷7WW'W6W"¢6VÆbæ76W'EG'VR‡W6W"æ6†V6µ÷77v÷&B‡6VÆbæFÖ–å÷77v÷&B’¢7F÷&RÒ7F÷&Ræö&¦V7G2ævWB‡6ÇVsÒ&&ö÷G7G&×7F÷&R"¢6VÆbæ76W'EG'VR‡7F÷&Rç&WfVçVUöVæv–æUöVæ&ÆVB¢6VÆbæ76W'EG'VR‡7F÷&Rç&WfVçVUöVæv–æUöG'•÷'Vâ¢6VÆbæ76W'DWVÂ‡7F÷&Rç6WGW÷7FGW2Â7F÷&Rå6WGW7FGW2å4UEUõ$UT•$TB ¢FVbFW7EöÖ–æ–ÖÅö6öæf–uö7&VFW5öFÖ–å÷7F÷&UööæÇ•öæE÷&W÷'G5÷6WGWö–æ6ö×ÆWFR‡6VÆb“ ¢6öæf–rÒ6VÆbæÖ–æ–ÖÅö6öæf–r‚ ¢÷WBÂW'"Ò6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢W6W"ÒvWE÷W6W%öÖöFVÂ‚¢W6W"ÒW6W"æö&¦V7G2ævWB‡W6W&æÖSÒ'6VF²ÖFÖ–â"¢6VÆbæ76W'EG'VR‡W6W"æ—5÷7Ffb¢6VÆbæ76W'EG'VR‡W6W"æ—5÷7WW'W6W"¢6VÆbæ76W'EG'VR‡W6W"æ6†V6µ÷77v÷&B‡6VÆbæFÖ–å÷77v÷&B’¢7F÷&RÒ7F÷&Ræö&¦V7G2ævWB†æÖSÒ%6VF²"¢6VÆbæ76W'EG'VR‡7F÷&Rç&WfVçVUöVæv–æUöVæ&ÆVB¢6VÆbæ76W'EG'VR‡7F÷&Rç&WfVçVUöVæv–æUöG'•÷'Vâ¢6VÆbæ76W'DWVÂ‡7F÷&Rç6WGW÷7FGW2Â7F÷&Rå6WGW7FGW2å4UEUõ$UT•$TB¢6VÆbæ76W'DWVÂ„&÷D6öæf–wW&F–öâæö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ…æVÂæö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ„–æ&÷VæBæö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ…Æâæö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ…Æä–æ&÷VæE&÷WFRæö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'D–â‚&–ç7FÆÅ÷7FGW3Ö6ö×ÆWFR"Â÷WB¢6VÆbæ76W'D–â‚&'W6–æW75÷6WGWÖ–æ6ö×ÆWFR"Â÷WB¢6VÆbæ76W'Dæ÷D–â‡6VÆbæFÖ–å÷77v÷&BÂ÷WB²W'" ¢FVbFW7E÷65ö&ö÷G7G&ö7&VFW5ö–æ7F—fU÷FVÆVw&Õ÷Æ6V†öÆFW%÷v—F†÷WE÷Fö¶Vâ‡6VÆb“ ¢6öæf–rÒ6VÆbæÖ–æ–ÖÅö6öæf–r‚¢6öæf–u²'FVÆVw&Ò%Õ²&7&VFUö–æ7F—fU÷Æ6V†öÆFW"%ÒÒG'VP ¢÷WBÂW'"Ò6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢7F÷&RÒ7F÷&Ræö&¦V7G2ævWB†æÖSÒ%6VF²"¢&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2ævWB‡7F÷&S×7F÷&RÂ&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$Ò¢6VÆbæ76W'DWVÂ‡7F÷&Rç6WGW÷7FGW2Â7F÷&Rå6WGW7FGW2å4UEUõ$UT•$TB¢6VÆbæ76W'DfÇ6R†&÷Eö6öæf–ræ—5ö7F—fR¢6VÆbæ76W'DWVÂ†&÷Eö6öæf–ræ&÷E÷Fö¶VâÂ""¢6VÆbæ76W'DWVÂ†&÷Eö6öæf–ræFÖ–å÷W6W%ö–BÂ""¢6VÆbæ76W'Dæ÷D–â‚%DTÄTu$Õô$õEõDô´Tâ"Â÷WB²W'"¢6VÆbæ76W'Dæ÷D–â‡6VÆbæFÖ–å÷77v÷&BÂ÷WB²W'" ¢FVbFW7E÷&W'Våö—5ö–FV×÷FVçB‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÕG'VRÂ‡V“ÕG'VR ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2"¢f—'7Eö6÷VçG2Ò6VÆbæö&¦V7Eö6÷VçG2‚¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢6VÆbæ76W'DWVÂ‡6VÆbæö&¦V7Eö6÷VçG2‚’Âf—'7Eö6÷VçG2 ¢FVbFW7Eö&÷Eö6öæf–wW&F–öåö—5ö7&VFVEöæE÷Fö¶Våö—5÷&VF7FVB‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÕG'VRÂ‡V“ÔfÇ6R ¢÷WBÂW'"Ò6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2ævWB‡&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$Ò¢6VÆbæ76W'DWVÂ†&÷Eö6öæf–ræ&÷E÷Fö¶VâÂ6VÆbæ&÷E÷Fö¶Vâ¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷E÷Fö¶VâÂ÷WB²W'"¢6VÆbæ76W'Dæ÷D–â‡6VÆbæFÖ–å÷77v÷&BÂ÷WB²W'" ¢FVbFW7E÷FVÆVw&ÕöVæ&ÆVE÷v—F†÷WE÷Fö¶Våöf–Ç2‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÕG'VRÂ‡V“ÔfÇ6R¢6öæf–u²'FVÆVw&Ò%Õ²&&÷E÷Fö¶Vâ%ÒÒ"  ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"ÒÖG'’×'Vâ" ¢6VÆbæ76W'DWVÂ„&÷D6öæf–wW&F–öâæö&¦V7G2æ6÷VçB‚’Â ¢FVbFW7Eö–çfÆ–E÷FVÆVw&ÕöFÖ–åö–Eöf–Ç2‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÕG'VRÂ‡V“ÔfÇ6R¢6öæf–u²'FVÆVw&Ò%Õ²&FÖ–åö–G2%ÒÒ²&æ÷BÖçVÖW&–2%Ð ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"ÒÖG'’×'Vâ" ¢FVbFW7E÷‡V•ö6öæf–wW&UöfÇ6U÷v—F†÷WE÷æVÅöFöW5öæ÷Eöf–Â‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÔfÇ6RÂ‡V“ÔfÇ6R¢6öæf–u²'‡V’%ÒÒ²&6öæf–wW&Uöæ÷r#¢fÇ6WÐ ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢6VÆbæ76W'DWVÂ…æVÂæö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ„–æ&÷VæBæö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ…Æâæö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ…Æä–æ&÷VæE&÷WFRæö&¦V7G2æ6÷VçB‚’Â ¢FVbFW7E÷‡V•ö6öæf–wW&U÷G'VU÷v—F†÷WEö7&VFVçF–Ç5öf–Ç2‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÔfÇ6RÂ‡V“ÕG'VR¢6öæf–u²'‡V’%Õ²'W6W&æÖR%ÒÒ" ¢6öæf–u²'‡V’%Õ²'77v÷&B%ÒÒ"  ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"ÒÖG'’×'Vâ" ¢FVbFW7E÷‡V•÷æVÅö–æ&÷VæE÷ÆåöæE÷&÷WFUö&Uö7&VFVB‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÔfÇ6RÂ‡V“ÕG'VR ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢æVÂÒæVÂæö&¦V7G2ævWB†æÖSÒ%&–Ö'’‚ÕT’æVÂ"¢–æ&÷VæBÒ–æ&÷VæBæö&¦V7G2ævWB‡æVÃ×æVÂÂ–æ&÷VæEö–CÓ¢ÆâÒÆâæö&¦V7G2ævWB‡6ÇVsÒ'7F'FW"Ó3B"¢&÷WFRÒÆä–æ&÷VæE&÷WFRæö&¦V7G2ævWB‡Æã×ÆâÂ–æ&÷VæCÖ–æ&÷VæB¢6VÆbæ76W'EG'VR‡æVÂæ—5ö7F—fR¢6VÆbæ76W'EG'VR†–æ&÷VæBæf–Æ&ÆUöf÷%öæWuö÷&FW'2¢6VÆbæ76W'EG'VR†–æ&÷VæBæ†VÇF…öÖöæ—F÷%öVæ&ÆVB¢6VÆbæ76W'EG'VR‡Æâæ—5÷V&Æ–2¢6VÆbæ76W'EG'VR‡&÷WFRæ—5ö7F—fR ¢FVbFW7E÷Væ¶æ÷vå÷&÷WFU÷&VfW&Væ6Uöf–Ç2‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÔfÇ6RÂ‡V“ÕG'VR¢6öæf–u²'Æå÷&÷WFW2%Õ³Õ²'Æâ%ÒÒ&Ö—76–ær×Æâ  ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"ÒÖG'’×'Vâ" ¢FVbFW7E÷&WfVçVUöG'•÷'VåöfÇ6Uöf–Ç2‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÔfÇ6RÂ‡V“ÔfÇ6R¢6öæf–u²'&WfVçVUöVæv–æR%Õ²&G'•÷'Vâ%ÒÒfÇ6P ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"ÒÖG'’×'Vâ" ¢FVbFW7E÷6V7&WG5öFõöæ÷EöV%ö–å÷7FF÷WEö÷%÷7FFW'"‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÕG'VRÂ‡V“ÕG'VR ¢÷WBÂW'"Ò6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢÷WGWBÒ÷WB²W' ¢6VÆbæ76W'Dæ÷D–â‡6VÆbæFÖ–å÷77v÷&BÂ÷WGWB¢6VÆbæ76W'Dæ÷D–â‡6VÆbæ&÷E÷Fö¶VâÂ÷WGWB¢6VÆbæ76W'Dæ÷D–â‡6VÆbçæVÅ÷77v÷&BÂ÷WGWB¢6VÆbæ76W'Dæ÷D–â†6öæf–u²'7F÷&R%Õ²&6&EöçVÖ&W"%ÒÂ÷WGWB ¢FVbFW7Eöæõ÷WFFUöW†—7F–æu÷6¶—5öW†—7F–æu÷&V6÷&G2‡6VÆb“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÔfÇ6RÂ‡V“ÔfÇ6R¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2"¢6öæf–u²'7F÷&R%Õ²&æÖR%ÒÒ$6†ævVBæÖR  ¢÷WBÂöW'"Ò6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2"Â"ÒÖæò×WFFRÖW†—7F–ær" ¢6VÆbæ76W'DWVÂ…7F÷&Ræö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ…7F÷&Ræö&¦V7G2ævWB‡6ÇVsÒ&&ö÷G7G&×7F÷&R"’ææÖRÂ$&ö÷G7G&7F÷&R"¢6VÆbæ76W'D–â‚r&7F–öâ#¢'6¶—"rÂ÷WB ¢F6‚‚'7F÷&Rç&öGV7F—¦F–öâæ&ö÷G7G&ä&ö÷G7G&–ç7FÆÆW"åöÆ—fUö6†V6µ÷FVÆVw&Ò"¢FVbFW7EöÆ—fUö6†V6µ÷'Vç5ööæÇ•÷v†VåöfÆuö—5öW‡Æ–6—B‡6VÆbÂÆ—fUö6†V6µöÖö6²“ ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÕG'VRÂ‡V“ÔfÇ6R ¢÷WBÂöW'"Ò6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2"Â"ÒÖÆ—fRÖ6†V6²" ¢Æ—fUö6†V6µöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢6VÆbæ76W'D–â‚&Æ—fUö6†V6·5÷'Vã×–W2"Â÷WB ¢FVbFW7E÷G&ç67F–öå÷&öÆÇ5ö&6µööåö&ö÷G7G&öW'&÷"‡6VÆb“ ¢g&öÒç&öGV7F—¦F–öâæ&ö÷G7G&–×÷'B&ö÷G7G&–ç7FÆÄW'&÷  ¢6öæf–rÒ6VÆbæ&6Uö6öæf–r‡FVÆVw&ÓÔfÇ6RÂ‡V“ÕG'VR ¢FVbf–ÅööæÇ•÷&VÂ†–ç7FÆÆW"“ ¢–b–ç7FÆÆW"æG'•÷'Vã ¢&WGW&âæöæP¢&—6R&ö÷G7G&–ç7FÆÄW'&÷"‚&f÷&6VB&ö÷G7G&f–ÇW&R" ¢v—F‚F6‚‚'7F÷&Rç&öGV7F—¦F–öâæ&ö÷G7G&ä&ö÷G7G&–ç7FÆÆW"åö&ö÷G7G&÷æVÂ"Âf–ÅööæÇ•÷&VÂ“ ¢v—F‚6VÆbæ76W'E&—6W2„6öÖÖæDW'&÷"“ ¢6VÆbæ6ÆÅö&ö÷G7G&†6öæf–rÂ"Ò×–W2" ¢6VÆbæ76W'DWVÂ…7F÷&Ræö&¦V7G2æ6÷VçB‚’Â¢6VÆbæ76W'DWVÂ†vWE÷W6W%öÖöFVÂ‚’æö&¦V7G2æf–ÇFW"‡W6W&æÖSÒ&&ö÷G7G&ÖFÖ–â"’æ6÷VçB‚’Â  ¦6Æ72FÖ–å7Ffe&öÆU7–æ5FW7G2…FW7D66R“ ¢FVbFW7E÷7–æ5÷7Ffe÷&öÆW5öG'•÷'VåöFöW5öæ÷Eö7&VFUöw&÷W2‡6VÆb“ ¢g&öÒF¦ævòæ6öçG&–"æWF‚æÖöFVÇ2–×÷'Bw&÷W  ¢÷WBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚'7–æ5÷7Ffe÷&öÆW2"Â"ÒÖG'’×'Vâ"Â7FF÷WCÖ÷WB ¢6VÆbæ76W'DWVÂ„w&÷Wæö&¦V7G2æf–ÇFW"†æÖUõ÷7F'G7v—FƒÒ%6VF²"’æ6÷VçB‚’Â¢6VÆbæ76W'D–â‚%7Ffb&öÆR7–æ2G'’×'Vâ"Â÷WBævWGfÇVR‚’ ¢FVbFW7E÷7–æ5÷7Ffe÷&öÆW5öÇ•ö—5ö–FV×÷FVçB‡6VÆb“ ¢g&öÒF¦ævòæ6öçG&–"æWF‚æÖöFVÇ2–×÷'Bw&÷W  ¢f—'7BÒ7G&–æt”ò‚¢6V6öæBÒ7G&–æt”ò‚¢6ÆÅö6öÖÖæB‚'7–æ5÷7Ffe÷&öÆW2"Â"ÒÖÇ’"Â7FF÷WCÖf—'7B¢w&÷Wö6÷VçBÒw&÷Wæö&¦V7G2æf–ÇFW"†æÖUõ÷7F'G7v—FƒÒ%6VF²"’æ6÷VçB‚ ¢6ÆÅö6öÖÖæB‚'7–æ5÷7Ffe÷&öÆW2"Â"ÒÖÇ’"Â7FF÷WC×6V6öæB ¢6VÆbæ76W'DWVÂ†w&÷Wö6÷VçBÂ‚¢6VÆbæ76W'DWVÂ„w&÷Wæö&¦V7G2æf–ÇFW"†æÖUõ÷7F'G7v—FƒÒ%6VF²"’æ6÷VçB‚’Âw&÷Wö6÷VçB¢6VÆbæ76W'D–â‚'W&Ö—76–öç5öFFVCÓ"Â6V6öæBævWGfÇVR‚’  ¦6Æ72FÖ–å7Ffd66W746VçFW%FW7G2…FW7D66R“ ¢FVb6WEW‡6VÆb“ ¢g&öÒF¦ævòæ6öçG&–"æWF‚æÖöFVÇ2–×÷'Bw&÷W ¢g&öÒæFÖ–åö66W72–×÷'B&öÆUöw&÷WöæÖP ¢6ÆÅö6öÖÖæB‚'7–æ5÷7Ffe÷&öÆW2"Â"ÒÖÇ’"Â7FF÷WCÕ7G&–æt”ò‚’¢6VÆbåW6W"ÒvWE÷W6W%öÖöFVÂ‚¢6VÆbç7WW'W6W"Ò6VÆbåW6W"æö&¦V7G2æ7&VFU÷7WW'W6W"‚'&ö÷B"Â'&ö÷DW†×ÆRæ6öÒ"Â'6V7&WB"¢6VÆbæ÷væW"Ò6VÆbæ7&VFU÷&öÆU÷W6W"‚&÷væW""Â'7F÷&Uö÷væW""¢6VÆbç7W÷'BÒ6VÆbæ7&VFU÷&öÆU÷W6W"‚'7W÷'B"Â'7W÷'EövVçB"¢6VÆbæf–ææ6RÒ6VÆbæ7&VFU÷&öÆU÷W6W"‚&f–ææ6R"Â&f–ææ6R"¢6VÆbæ6FÆörÒ6VÆbæ7&VFU÷&öÆU÷W6W"‚&6FÆör"Â&6FÆöuöÖævW""¢6VÆbæÖ&¶WF–ærÒ6VÆbæ7&VFU÷&öÆU÷W6W"‚&Ö&¶WF–ær"Â&Ö&¶WF–æuöÖævW""¢6VÆbææÇ—7BÒ6VÆbæ7&VFU÷&öÆU÷W6W"‚&æÇ—7B"Â&æÇ—7B"¢6VÆbæ÷&FW%ö÷W&F÷"Ò6VÆbæ7&VFU÷&öÆU÷W6W"‚&÷&FW'2"Â&÷&FW%ö÷W&F÷""¢6VÆbç7F÷&RÒ7F÷&Ræö&¦V7G2æ7&VFR€¢æÖSÒ%7F÷&R"À¢6ÇVsÒ'×7F÷&R"À¢6&EöçVÖ&W#Ò#"À¢6&Eö÷væW#Ò$÷væW""À¢¢6VÆbçÆâÒÆâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%Æâ"À¢6ÇVsÒ'×Æâ"À¢föÇVÖUöv#ÔFV6–ÖÂ‚#ã"’À¢GW&F–öåöF—3Ó3À¢&–6SÓÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢¢6VÆbæ7W7FöÖW"Ò7W7FöÖW"æö&¦V7G2æ7&VFR‡W6W&æÖSÒ'7W7FöÖW""Â†öæUöçVÖ&W#Ò#“#"¢6VÆbæ÷&FW"Ò÷&FW"æö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢Æã×6VÆbçÆâÀ¢Ö÷VçCÓÀ¢7W'&Væ7“ÕÆâä7W'&Væ7’åDôÔâÀ¢7FGW3Ô÷&FW"å7FGW2åTäD”äuõdU$”d”4D”ôâÀ¢fW&–f–6F–öå÷7FGW3Ô÷&FW"åfW&–f–6F–öå7FGW2åTäD”ärÀ¢—5÷–CÕG'VRÀ¢–ÖVçEöÖWF†öCÔ÷&FW"å–ÖVçDÖWF†öBäÔåTÅô4$BÀ¢¢6VÆbç7W÷'E÷F–6¶WBÒ7W÷'D6öçfW'6F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢7W7FöÖW#×6VÆbæ7W7FöÖW"À¢7FGW3Õ7W÷'D6öçfW'6F–öâå7FGW2åt•D”äuôDÔ”âÀ¢7V&¦V7CÒ$æVVB†VÇ"À¢¢6VÆbæ6×–vâÒ'&öF67DÖW76vRæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢F—FÆSÒ%6×–vâ"À¢ÖW76vU÷FW‡CÒ-‹=˜MŠ}˜R"À¢7FGW3Ô'&öF67DÖW76vRå7FGW2äE$eBÀ¢¢6VÆbç&öÆUöw&÷WÒw&÷Wæö&¦V7G2ævWB†æÖS×&öÆUöw&÷WöæÖR‚'7F÷&Uö÷væW""’ ¢FVb7&VFU÷&öÆU÷W6W"‡6VÆbÂW6W&æÖRÂ&öÆUö¶W’“ ¢g&öÒF¦ævòæ6öçG&–"æWF‚æÖöFVÇ2–×÷'Bw&÷W ¢g&öÒæFÖ–åö66W72–×÷'B&öÆUöw&÷WöæÖP ¢W6W"Ò6VÆbåW6W"æö&¦V7G2æ7&VFU÷W6W"€¢W6W&æÖS×W6W&æÖRÀ¢VÖ–ÃÖb'·W6W&æÖWÔW†×ÆRæ6öÒ"À¢77v÷&CÒ'6V7&WB"À¢—5÷7FfcÕG'VRÀ¢¢W6W"æw&÷W2æFB„w&÷Wæö&¦V7G2ævWB†æÖS×&öÆUöw&÷WöæÖR‡&öÆUö¶W’’’¢&WGW&âW6W  ¢FVbÆöv–â‡6VÆbÂW6W"“ ¢6Æ–VçBÒ6Æ–VçB‚¢6Æ–VçBæf÷&6UöÆöv–â‡W6W"¢&WGW&â6Æ–Vç@ ¢FVbFW7E÷7Ffeö6VçFW%÷7WW'W6W%öæEö÷væW%ö6å÷f–Wr‡6VÆb“ ¢f÷"W6W"–â‡6VÆbç7WW'W6W"Â6VÆbæ÷væW"“ ¢&W7öç6RÒ6VÆbæÆöv–â‡W6W"’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷7Ffeö66W72"’¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ-ªŠ}‹ª˜mŠ}˜b˜‚Šý‹=Š­‹‹=¸Â" ¢FVbFW7E÷7Ffeö6VçFW%öFVæ–W5öæöå÷7FfeöæE÷7W÷'EövVçB‡6VÆb“ ¢&VwVÆ"Ò6VÆbåW6W"æö&¦V7G2æ7&VFU÷W6W"‚'&VwVÆ""Â77v÷&CÒ'6V7&WB" ¢6VÆbæ76W'Dæ÷DWVÂ‡6VÆbæÆöv–â‡&VwVÆ"’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷7Ffeö66W72"’’ç7FGW5ö6öFRÂ#¢6VÆbæ76W'DWVÂ‡6VÆbæÆöv–â‡6VÆbç7W÷'B’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷7Ffeö66W72"’’ç7FGW5ö6öFRÂC2 ¢FVbFW7Eö÷væW%ö7&VFVE÷7Ffeö—5öæ÷E÷7WW'W6W%öæEö†6…ö—5öæ÷E÷&VæFW&VB‡6VÆb“ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæ÷væW"’ç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷7FfeöæWr"’À¢°¢'W6W&æÖR#¢&æWw7Ffb"À¢&VÖ–Â#¢&æWw7FfdW†×ÆRæ6öÒ"À¢&f—'7EöæÖR#¢$æWr"À¢&Æ7EöæÖR#¢%7Ffb"À¢&—5ö7F—fR#¢&öâ"À¢'&öÆUö¶W’#¢'7W÷'EövVçB"À¢'77v÷&EöÖöFR#¢&vVæW&FVB"À¢&—5÷7WW'W6W"#¢#"À¢ÒÀ¢ ¢7&VFVBÒ6VÆbåW6W"æö&¦V7G2ævWB‡W6W&æÖSÒ&æWw7Ffb"¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'EG'VR†7&VFVBæ—5÷7Ffb¢6VÆbæ76W'DfÇ6R†7&VFVBæ—5÷7WW'W6W"¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ7&VFVBç77v÷&B¢6VÆbæ76W'Dæ÷D–â‚'&¶Fc""Â&W7öç6Ræ6öçFVçBæFV6öFR‚’ ¢FVbFW7EöÆ7E÷7WW'W6W%ö6ææ÷Eö&UöFV7F—fFVB‡6VÆb“ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W"’ç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷7FfeöVF—B"Â&w3Õ·6VÆbç7WW'W6W"çµÒ’À¢°¢'W6W&æÖR#¢6VÆbç7WW'W6W"çW6W&æÖRÀ¢&VÖ–Â#¢6VÆbç7WW'W6W"æVÖ–ÂÀ¢&f—'7EöæÖR#¢""À¢&Æ7EöæÖR#¢""À¢'&öÆUö¶W’#¢'7F÷&Uö÷væW""À¢ÒÀ¢ ¢6VÆbç7WW'W6W"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'EG'VR‡6VÆbç7WW'W6W"æ—5ö7F—fR¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ-Š-Ší‹¸Í˜b7WW'W6W"" ¢FVbFW7Eö÷væW%ö6ææ÷EöFVÖ÷FUö÷%öÆö6µ÷6VÆb‡6VÆb“ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæ÷væW"’ç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷7FfeöVF—B"Â&w3Õ·6VÆbæ÷væW"çµÒ’À¢°¢'W6W&æÖR#¢6VÆbæ÷væW"çW6W&æÖRÀ¢&VÖ–Â#¢6VÆbæ÷væW"æVÖ–ÂÀ¢&f—'7EöæÖR#¢""À¢&Æ7EöæÖR#¢""À¢&—5ö7F—fR#¢""À¢'&öÆUö¶W’#¢&f–ææ6R"À¢ÒÀ¢ ¢6VÆbæ÷væW"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'EG'VR‡6VÆbæ÷væW"æ—5ö7F—fR¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ&Æö6²Ö÷WB" ¢FVbFW7E÷&öÆUö6†ævUö—5öVF—FVB‡6VÆb“ ¢g&öÒF¦ævòæ6öçG&–"æFÖ–âæÖöFVÇ2–×÷'BÆötVçG' ¢F&vWBÒ6VÆbæ7&VFU÷&öÆU÷W6W"‚&VF—B×F&vWB"Â'7W÷'EövVçB" ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæ÷væW"’ç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷7FfeöVF—B"Â&w3Õ·F&vWBçµÒ’À¢°¢'W6W&æÖR#¢F&vWBçW6W&æÖRÀ¢&VÖ–Â#¢F&vWBæVÖ–ÂÀ¢&f—'7EöæÖR#¢""À¢&Æ7EöæÖR#¢""À¢&—5ö7F—fR#¢&öâ"À¢'&öÆUö¶W’#¢&f–ææ6R"À¢ÒÀ¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢6VÆbæ76W'EG'VR„ÆötVçG'’æö&¦V7G2æf–ÇFW"†ö&¦V7Eö–C×7G"‡F&vWBç²’Â6†ævUöÖW76vUõö6öçF–ç3Ò'&öÆRæ6†ævVB"’æW†—7G2‚’ ¢FVbFW7E÷7W÷'EövVçEö6å÷f–Wu÷7W÷'Eö'WEöæ÷E÷&WfVçVR‡6VÆb“ ¢6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbç7W÷'B ¢6VÆbæ76W'DWVÂ†6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷7W÷'E÷v÷&¶&Væ6‚"’’ç7FGW5ö6öFRÂ#¢6VÆbæ76W'DWVÂ†6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷&WfVçVUö6öçG&öÂ"’’ç7FGW5ö6öFRÂC2 ¢FVbFW7EöFÖ–åö†öÖUö6&G5ö&U÷W&Ö—76–öåöv&Uöf÷%÷7W÷'Eöf–ææ6UöæEöæÇ—7B‡6VÆb“ ¢7W÷'E÷&W7öç6RÒ6VÆbæÆöv–â‡6VÆbç7W÷'B’ævWB‡&WfW'6R‚&FÖ–ã¦–æFW‚"’¢f–ææ6U÷&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæf–ææ6R’ævWB‡&WfW'6R‚&FÖ–ã¦–æFW‚"’¢æÇ—7E÷&W7öç6RÒ6VÆbæÆöv–â‡6VÆbææÇ—7B’ævWB‡&WfW'6R‚&FÖ–ã¦–æFW‚"’ ¢6VÆbæ76W'DWVÂ‡7W÷'E÷&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2‡7W÷'E÷&W7öç6RÂ-˜]¸Í‹"ªŠ}‹›í‹MŠ­¸ÍŠŠ}˜m¸Â"¢6VÆbæ76W'D6öçF–ç2‡7W÷'E÷&W7öç6RÂ-˜]¸Í‹"ªŠ}‹‹=‹˜¸Í‹>(Í˜}Šr"¢6VÆbæ76W'Dæ÷D6öçF–ç2‡7W÷'E÷&W7öç6RÂ-ª˜mŠ­‹˜BŠý‹Š-˜]Šò˜}˜‹M˜]˜mŠò"¢6VÆbæ76W'Dæ÷D6öçF–ç2‡7W÷'E÷&W7öç6RÂ-ªŠ}‹ª˜mŠ}˜b˜‚Šý‹=Š­‹‹=¸Î(Í˜}Šr" ¢6VÆbæ76W'DWVÂ†f–ææ6U÷&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2†f–ææ6U÷&W7öç6RÂ-˜]¸Í‹"ªŠ}‹‹=˜Š}‹‹N(Í˜}Šr"¢6VÆbæ76W'D6öçF–ç2†f–ææ6U÷&W7öç6RÂ-ªý‹-Š}‹‹N(Í˜}Šr˜‚Š­ŠÝ˜M¸Í˜Bª‹=ŠŽ(Í˜ªŠ}‹"¢6VÆbæ76W'Dæ÷D6öçF–ç2†f–ææ6U÷&W7öç6RÂ-ªŠ}‹ª˜mŠ}˜b˜‚Šý‹=Š­‹‹=¸Î(Í˜}Šr"¢6VÆbæ76W'Dæ÷D6öçF–ç2†f–ææ6U÷&W7öç6RÂ-ª˜]›í¸Í˜n(Í˜}Šr˜‚›í¸ÍŠ}˜^(Í‹‹=Š}˜m¸Â" ¢6VÆbæ76W'DWVÂ†æÇ—7E÷&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2†æÇ—7E÷&W7öç6RÂ-ªý‹-Š}‹‹N(Í˜}Šr˜‚Š­ŠÝ˜M¸Í˜Bª‹=ŠŽ(Í˜ªŠ}‹"¢6VÆbæ76W'Dæ÷D6öçF–ç2†æÇ—7E÷&W7öç6RÂ-‹Š}˜~(ÍŠ}˜mŠýŠ}‹-¸Â˜‹˜‹MªýŠ}˜r"¢6VÆbæ76W'Dæ÷D6öçF–ç2†æÇ—7E÷&W7öç6RÂ'GrÖ'WGFöâÖFævW"" ¢FVbFW7E÷6–FV&%öæf–vF–öåö—5öw&÷WVEöæE÷W&Ö—76–öåöv&R‡6VÆb“ ¢6FÆöu÷&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæ6FÆör’ævWB‡&WfW'6R‚&FÖ–ã¦–æFW‚"’ ¢6VÆbæ76W'DWVÂ†6FÆöu÷&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2†6FÆöu÷&W7öç6RÂ%6ÆW2"¢6VÆbæ76W'D6öçF–ç2†6FÆöu÷&W7öç6RÂ%&öGV7G2òÆç2"¢6VÆbæ76W'D6öçF–ç2†6FÆöu÷&W7öç6RÂ%6ÆW2&÷WFW2"¢6VÆbæ76W'D6öçF–ç2†6FÆöu÷&W7öç6RÂ%&W÷'G2"¢6VÆbæ76W'Dæ÷D6öçF–ç2†6FÆöu÷&W7öç6RÂ%7Ffb66W72"¢6VÆbæ76W'Dæ÷D6öçF–ç2†6FÆöu÷&W7öç6RÂ$&6·Wò&W7F÷&R"¢6VÆbæ76W'Dæ÷D6öçF–ç2†6FÆöu÷&W7öç6RÂ%&WfVçVRVæv–æR" ¢FVbFW7Eöf–ææ6Uö6å÷f–Wuö÷&FW'5ö'WEöæ÷Eö6FÆöuö×WFF–öâ‡6VÆb“ ¢6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbæf–ææ6R ¢6VÆbæ76W'DWVÂ†6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö÷&FW%÷v÷&¶&Væ6‚"’’ç7FGW5ö6öFRÂ#¢6VÆbæ76W'DWVÂ€¢6Æ–VçBç÷7B‡&WfW'6R‚&FÖ–å÷7F÷&Uö6FÆör"’Â²&7F–öâ#¢&GWÆ–6FR"Â'Æåö–B#¢6VÆbçÆâç²Â&6öæf—&Õö7F–öâ#¢#'Ò’ç7FGW5ö6öFRÀ¢C2À¢ ¢FVbFW7Eö6FÆöuöÖævW%ö6ææ÷E÷VWVUö6×–vâ‡6VÆb“ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæ6FÆör’ç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö6×–våö6öæf—&Ò"Â&w3Õ·6VÆbæ6×–vâçµÒ’À¢²&6öæf—&ÖF–öâ#¢b%4TäEô4Õ”tå÷·6VÆbæ6×–vâç·Ò'ÒÀ¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂC2 ¢FVbFW7EöÖ&¶WF–æuöÖævW%ö6ææ÷EöVæ&ÆU÷&WfVçVU÷&VÅ÷6VæB‡6VÆb“ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæÖ&¶WF–ær’ç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷&WfVçVUö6öçG&öÂ"’À¢²'7F÷&R#¢6VÆbç7F÷&Rç²Â&7F–öâ#¢&Væ&ÆU÷&VÅ÷6VæB"Â&6öæf—&ÖF–öâ#¢$Tä$ÄUõ$TÅõ$UdTåTUõ4TäB'ÒÀ¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂC2 ¢FVbFW7EöæÇ—7Eö6å÷f–Wu÷&W÷'G5÷v—F†÷WEö77eöW‡÷'B‡6VÆb“ ¢6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbææÇ—7B ¢&W7öç6RÒ6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷&W÷'G5ö6VçFW""’¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ$55b˜‹˜‹B"¢6VÆbæ76W'DWVÂ†6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷&W÷'G5öW‡÷'B"’Â²'&W÷'B#¢'6ÆW2'Ò’ç7FGW5ö6öFRÂC2 ¢FVbFW7Eö÷&FW%ö÷W&F÷%ö6åö&÷fUöæE÷7W÷'EövVçEö6ææ÷E÷÷7Eö÷&FW%ö7F–öâ‡6VÆb“ ¢÷W&F÷%ö6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbæ÷&FW%ö÷W&F÷"¢7W÷'Eö6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbç7W÷'B ¢&Wf–Wu÷&W7öç6RÒ÷W&F÷%ö6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö÷&FW%÷&Wf–Wr"Â&w3Õ·6VÆbæ÷&FW"çµÒ’¢6VÆbæ76W'DWVÂ‡&Wf–Wu÷&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2‡&Wf–Wu÷&W7öç6RÂ-Š­Š}¸Í¸ÍŠò›í‹ŠýŠ}ŠíŠ¢òŠ­ª˜]¸Í˜B‹=˜Š}‹‹B"¢6VÆbæ76W'DWVÂ€¢7W÷'Eö6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö÷&FW%÷&Wf–Wr"Â&w3Õ·6VÆbæ÷&FW"çµÒ’À¢²&7F–öâ#¢&&÷fR"Â&6öæf—&ÕöW‡FW&æÂ#¢#'ÒÀ¢’ç7FGW5ö6öFRÀ¢C2À¢ ¢FVbFW7Eö7F–öåö'WGFöåöæ÷E÷&VæFW&VE÷v—F†÷WEö6&–Æ—G’‡6VÆb“ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbç7W÷'B’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷7W÷'E÷&Wf–Wr"Â&w3Õ·6VÆbç7W÷'E÷F–6¶WBçµÒ’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ-Š}‹‹=Š}˜B›íŠ}‹=Šâ"¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæ÷&FW%ö÷W&F÷"’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷7W÷'E÷&Wf–Wr"Â&w3Õ·6VÆbç7W÷'E÷F–6¶WBçµÒ’¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ-Š}‹‹=Š}˜B›íŠ}‹=Šâ" ¢FVbFW7E÷&uö–×÷'EöW‡÷'E÷7W&f6W5ö&U÷7WW'W6W%ööæÇ’‡6VÆb“ ¢g&öÒF¦ævòæ6öçG&–"–×÷'BFÖ–â2F¦ævõöFÖ–à¢g&öÒæFÖ–â–×÷'B÷&FW$FÖ–à ¢ÖöFVÅöFÖ–âÒ÷&FW$FÖ–â„÷&FW"ÂF¦ævõöFÖ–âç6—FR ¢6VÆbæ76W'DfÇ6R†ÖöFVÅöFÖ–âæ†5öW‡÷'E÷W&Ö—76–öâ…6–×ÆTæÖW76R‡W6W#×6VÆbæ÷væW"’’¢6VÆbæ76W'DfÇ6R†ÖöFVÅöFÖ–âæ†5ö–×÷'E÷W&Ö—76–öâ…6–×ÆTæÖW76R‡W6W#×6VÆbæ÷væW"’’¢6VÆbæ76W'EG'VR†ÖöFVÅöFÖ–âæ†5öW‡÷'E÷W&Ö—76–öâ…6–×ÆTæÖW76R‡W6W#×6VÆbç7WW'W6W"’’ ¢FVbFW7E÷f–WuööæÇ•÷7F÷&UöFÖ–åöFöW5öæ÷E÷&VæFW%ögVÆÅö6&B‡6VÆb“ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbææÇ—7B’ævWB‡&WfW'6R‚&FÖ–ã§7F÷&U÷7F÷&Uö6†ævR"Â&w3Õ·6VÆbç7F÷&RçµÒ’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ6VÆbç7F÷&Ræ6&EöçVÖ&W"¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ6VÆbç7F÷&Ræ6&EöçVÖ&W%²ÓC¥Ò ¢FVbFW7E÷f–WuööæÇ•÷æVÅöFÖ–åöÖ6·5÷77v÷&E÷&÷‡•öæE÷W&Åö7&VFVçF–Ç2‡6VÆb“ ¢æVÂÒæVÂæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢æÖSÒ%æVÂ"À¢W&ÃÒ&‡GG3¢ò÷æVÂ×W6W$W†×ÆRæ6öÒ"À¢W6W&æÖSÒ'æVÂÖFÖ–â"À¢77v÷&CÒ'æVÂ×77v÷&B×6V7&WB"À¢&÷‡•÷W&ÃÒ&‡GG¢ò÷&÷‡’×W6W$W†×ÆRææWC£ƒƒ"À¢ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbæ6FÆör’ævWB‡&WfW'6R‚&FÖ–ã§7F÷&U÷æVÅö6†ævR"Â&w3Õ·æVÂçµÒ’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂæVÂç77v÷&B¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂæVÂç&÷‡•÷W&Â¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ'æVÂ×W6W""¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ'&÷‡’×W6W"" ¢FVbFW7EöF—&V7E÷f–WuööæÇ•ö&÷Eö6öæf–wW&F–öå÷W&Ö—76–öåöFöW5öæ÷E÷&VæFW%÷Fö¶Vâ‡6VÆb“ ¢g&öÒF¦ævòæ6öçG&–"æWF‚æÖöFVÇ2–×÷'BW&Ö—76–öà ¢&÷Eö6öæf–rÒ&÷D6öæf–wW&F–öâæö&¦V7G2æ7&VFR€¢7F÷&S×6VÆbç7F÷&RÀ¢&÷f–FW#Ô&÷D6öæf–wW&F–öâå&÷f–FW"åDTÄTu$ÒÀ¢æÖSÒ%&÷B"À¢&÷E÷Fö¶VãÒ##3CSc§&r×Fö¶Vâ×6V7&WB"À¢FÖ–å÷W6W%ö–CÒ#““ƒƒsscb"À¢—5ö7F—fSÕG'VRÀ¢¢W6W"Ò6VÆbåW6W"æö&¦V7G2æ7&VFU÷W6W"‚&&÷B×f–WvW""Â77v÷&CÒ'6V7&WB"Â—5÷7FfcÕG'VR¢W6W"çW6W%÷W&Ö—76–öç2æFB…W&Ö—76–öâæö&¦V7G2ævWB†6öFVæÖSÒ'f–Wuö&÷F6öæf–wW&F–öâ"’ ¢&W7öç6RÒ6VÆbæÆöv–â‡W6W"’ævWB‡&WfW'6R‚&FÖ–ã§7F÷&Uö&÷F6öæf–wW&F–öåö6†ævR"Â&w3Õ¶&÷Eö6öæf–rçµÒ’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ&÷Eö6öæf–ræ&÷E÷Fö¶Vâ¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ&÷Eö6öæf–ræFÖ–å÷W6W%ö–B  ¦6Æ72FÖ–ä&6·W&W7F÷&T6VçFW%FW7G2…FW7D66R“ ¢&u÷6V7&WBÒ'&r×Fö¶Vâ×6V7&WBÓ#3CScsƒ“  ¢FVb6WEW‡6VÆb“ ¢g&öÒF¦ævòæ6öçG&–"æWF‚æÖöFVÇ2–×÷'Bw&÷W ¢g&öÒæFÖ–åö66W72–×÷'B&öÆUöw&÷WöæÖP ¢6VÆbçFV×öF—"ÒFV×f–ÆRåFV×÷&'”F—&V7F÷'’‚¢6VÆbæFD6ÆVçW‡6VÆbçFV×öF—"æ6ÆVçW¢6VÆbæ&6U÷F‚ÒF‚‡6VÆbçFV×öF—"ææÖR¢6VÆbæ&6·W÷&ö÷BÒ6VÆbæ&6U÷F‚ò'&—fFRÖ&6·W2 ¢6VÆbçWÆöE÷&ö÷BÒ6VÆbæ&6U÷F‚ò'&W7F÷&R×WÆöG2 ¢6VÆbç6WGF–æw5ö÷fW'&–FRÒ÷fW'&–FU÷6WGF–æw2€¢4TDµõ$•dDUô$4µUõ$ôõC×6VÆbæ&6·W÷&ö÷BÀ¢4TDµõ$U5Dõ$UõUÄôEõ$ôõC×6VÆbçWÆöE÷&ö÷BÀ¢4TDµô$4µUôÔ…õUÄôEõ4•¤SÓ‚¢#B¢#BÀ¢4TDµô$4µUôÔ…ôU…E$5DTEõ4•¤SÓb¢#B¢#BÀ¢4TDµôDÔ”åõ$U5Dõ$UôTä$ÄTCÔfÇ6RÀ¢¢6VÆbç6WGF–æw5ö÷fW'&–FRæVæ&ÆR‚¢6VÆbæFD6ÆVçW‡6VÆbç6WGF–æw5ö÷fW'&–FRæF—6&ÆR ¢6ÆÅö6öÖÖæB‚'7–æ5÷7Ffe÷&öÆW2"Â"ÒÖÇ’"Â7FF÷WCÕ7G&–æt”ò‚’¢6VÆbåW6W"ÒvWE÷W6W%öÖöFVÂ‚¢6VÆbç7WW'W6W"Ò6VÆbåW6W"æö&¦V7G2æ7&VFU÷7WW'W6W"‚&&6·W×&ö÷B"Â'&ö÷DW†×ÆRæ6öÒ"Â'6V7&WB"¢6VÆbæ÷væW"Ò6VÆbæ7&VFU÷&öÆU÷W6W"‚&&6·WÖ÷væW""Â'7F÷&Uö÷væW""¢6VÆbçFV6†æ–6ÂÒ6VÆbæ7&VFU÷&öÆU÷W6W"‚&&6·W×FV6‚"Â'FV6†æ–6Åö÷W&F÷""¢6VÆbç7W÷'BÒ6VÆbæ7&VFU÷&öÆU÷W6W"‚&&6·W×7W÷'B"Â'7W÷'EövVçB"¢6VÆbæ÷væW%öw&÷WÒw&÷Wæö&¦V7G2ævWB†æÖS×&öÆUöw&÷WöæÖR‚'7F÷&Uö÷væW""’ ¢FVb7&VFU÷&öÆU÷W6W"‡6VÆbÂW6W&æÖRÂ&öÆUö¶W’“ ¢g&öÒF¦ævòæ6öçG&–"æWF‚æÖöFVÇ2–×÷'Bw&÷W ¢g&öÒæFÖ–åö66W72–×÷'B&öÆUöw&÷WöæÖP ¢W6W"Ò6VÆbåW6W"æö&¦V7G2æ7&VFU÷W6W"€¢W6W&æÖS×W6W&æÖRÀ¢VÖ–ÃÖb'·W6W&æÖWÔW†×ÆRæ6öÒ"À¢77v÷&CÒ'6V7&WB"À¢—5÷7FfcÕG'VRÀ¢¢W6W"æw&÷W2æFB„w&÷Wæö&¦V7G2ævWB†æÖS×&öÆUöw&÷WöæÖR‡&öÆUö¶W’’’¢&WGW&âW6W  ¢FVbÆöv–â‡6VÆbÂW6W"“ ¢6Æ–VçBÒ6Æ–VçB‚¢6Æ–VçBæf÷&6UöÆöv–â‡W6W"¢&WGW&â6Æ–Vç@ ¢FVbw&—FU÷7Æ—FUöFF&6R‡6VÆbÂF‚“ ¢F‚ç&VçBæÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢v—F‚7Æ—FS2æ6öææV7B‡F‚’26öææV7F–öã ¢6öææV7F–öâæW†V7WFR‚$5$TDRD$ÄR6Öö¶R†–B–çFVvW"&–Ö'’¶W’ÂæÖRFW‡B’"¢6öææV7F–öâæW†V7WFR‚$”å4U%B”åDò6Öö¶R†æÖR’dÅTU2‚vö²r’"¢6öææV7F–öâæ6öÖÖ—B‚ ¢FVbÖ¶Uö&6·Wö&6†—fR‡6VÆbÂ¢ÂVæv–æSÒ'7Æ—FR"ÂæÖSÒ'6VF²Ö&6·W×FW7BçF"æw¢"ÂÖæ–fW7Eö÷fW'&–FW3ÔæöæRÂ6†V6·7VÕöÖ—6ÖF6ƒÔfÇ6R“ ¢g&öÒæ&6·W÷&W7F÷&U÷6W'f–6W2–×÷'B7&VFUö&6†—fRÂw&—FUö6†V6·7V×0 ¢7FvRÒ6VÆbæ&6U÷F‚òb'7FvR×·&æFöÒç&æF–çBƒÂ“““’—Ò ¢–ÆöBÒ7FvRò'–ÆöB ¢F%öF—"Ò–ÆöBò&FF&6R ¢F%öF—"æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢–bVæv–æRÓÒ'÷7Fw&W2# ¢†F%öF—"ò&F"ç÷7Fw&W2æGV×"’çw&—FUö'—FW2†"%tDÕ6VF²FW7BGV×"¢VÇ6S ¢6VÆbçw&—FU÷7Æ—FUöFF&6R†F%öF—"ò&F"ç7Æ—FS2"¢Öæ–fW7BÒ°¢'6VFµö&6·W÷fW'6–öâ#¢#"À¢&7&VFVEöB#¢###bÓbÓ#uC#££³£"À¢&÷fW'6–öâ#¢'FW7B"À¢&v—Eö6öÖÖ—B#¢&&3#2"À¢&F¦ævõ÷6WGF–æw5öÖöGVÆR#¢&6÷&Rç6WGF–æw2çFW7B"À¢&FF&6UöVæv–æR#¢Væv–æRÀ¢&FF&6U÷fVæF÷"#¢'÷7Fw&W7Â"–bVæv–æRÓÒ'÷7Fw&W2"VÇ6R'7Æ—FR"À¢'÷7Fw&W5öGV×öf÷&ÖB#¢&7W7FöÒ"–bVæv–æRÓÒ'÷7Fw&W2"VÇ6R""À¢&FF&6UöæÖU÷&VF7FVB#¢%¶6öæf–wW&VEÒ"À¢&FF&6U÷66†VÖöÖ–w&F–öç2#¢²'7F÷&R#¢#SB'ÒÀ¢&&6·W÷G—R#¢&F%ööæÇ’"À¢&–æ6ÇVFW5öÖVF–#¢fÇ6RÀ¢&–æ6ÇVFW5öVçb#¢fÇ6RÀ¢&–æ6ÇVFW5÷7—7FVÒ#¢fÇ6RÀ¢&ÖVF–öf–ÆUö6÷VçB#¢À¢&F%÷6—¦Uö'—FW2#¢À¢&&6†—fU÷6—¦Uö'—FW2#¢À¢&7&VFVEö'’#¢&&6·WÖ÷væW""À¢&†÷7FæÖR#¢'FW7BÖ†÷7B"À¢&–ç7FÆÅöF—"#¢"ö÷B÷6VF²"À¢'&WfVçVUöVæv–æUöG'•÷'Vâ#¢G'VRÀ¢&ÆÆ÷uövÆö&Åö–æ&÷VæEöfÆÆ&6²#¢fÇ6RÀ¢'v&æ–æw2#¢µÒÀ¢&6†V6·7V×2#¢·ÒÀ¢'&VF7F–öå÷öÆ–7’#¢'FW7BÖWFFFöæÇ’"À¢Ð¢Öæ–fW7BçWFFR†Öæ–fW7Eö÷fW'&–FW2÷"·Ò¢‡–ÆöBò&Öæ–fW7Bæ§6öâ"’çw&—FU÷FW‡B†§6öâæGV×2†Öæ–fW7B’ÂVæ6öF–æsÒ'WFbÓ‚"¢w&—FUö6†V6·7V×2‡–ÆöB¢–b6†V6·7VÕöÖ—6ÖF6ƒ ¢F&vWBÒF%öF—"ò‚&F"ç÷7Fw&W2æGV×"–bVæv–æRÓÒ'÷7Fw&W2"VÇ6R&F"ç7Æ—FS2"¢F&vWBçw&—FUö'—FW2‡F&vWBç&VEö'—FW2‚’²"'F×W&VB"¢&6†—fRÒ6VÆbæ&6U÷F‚òæÖP¢7&VFUö&6†—fR‡–ÆöBÂ&6†—fR¢&WGW&â&6†—fP ¢FVbÖ¶U÷Vç6fUö&6†—fR‡6VÆbÂæÖRÂ¢ÂÖVÖ&W%öæÖSÔæöæRÂ7–ÖÆ–æ³ÔfÇ6R“ ¢&6†—fRÒ6VÆbæ&6U÷F‚òæÖP¢v—F‚F&f–ÆRæ÷Vâ†&6†—fRÂ's¦w¢"’2F# ¢–b7–ÖÆ–æ³ ¢–æfòÒF&f–ÆRåF$–æfò‚&Öæ–fW7BÖÆ–æ²"¢–æfòçG—RÒF&f–ÆRå5”ÕE•P¢–æfòæÆ–æ¶æÖRÒ&Öæ–fW7Bæ§6öâ ¢F"æFFf–ÆR†–æfò¢VÇ6S ¢FFÒ"'Vç6fR ¢–æfòÒF&f–ÆRåF$–æfò†ÖVÖ&W%öæÖR÷""ââöWf–ÂçG‡B"¢–æfòç6—¦RÒÆVâ†FF¢F"æFFf–ÆR†–æfòÂ'—FW4”ò†FF’¢&WGW&â&6†—fP ¢FVbWÆöEö&6†—fR‡6VÆbÂ&6†—fR“ ¢g&öÒæ&6·W÷&W7F÷&U÷6W'f–6W2–×÷'B7&VFU÷&W7F÷&Uö¦ö  ¢WÆöFVBÒ6–×ÆUWÆöFVDf–ÆR†&6†—fRææÖRÂ&6†—fRç&VEö'—FW2‚’Â6öçFVçE÷G—SÒ&Æ–6F–öâöw¦—"¢&WGW&â7&VFU÷&W7F÷&Uö¦ö"‡6VÆbç7WW'W6W"ÂWÆöFVB ¢FVbw&—FUö–ç7FÆÅöVçb‡6VÆbÂVæv–æSÒ'7Æ—FR"“ ¢–ç7FÆÅöF—"Ò6VÆbæ&6U÷F‚òb&–ç7FÆÂ×¶Væv–æWÒ ¢–ç7FÆÅöF—"æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢†–ç7FÆÅöF—"ò"æVçb"’çw&—FU÷FW‡B†b$DD$4UôTät”äS×¶Væv–æWÕÆâ"ÂVæ6öF–æsÒ'WFbÓ‚"¢&WGW&â–ç7FÆÅöF—  ¢FVb'Vå÷&W7F÷&U÷67&—B‡6VÆbÂ¦&w2“ ¢VçbÒ÷2æVçf—&öâæ6÷’‚¢Vçe²%4TDµô$4µUôÔ…õUÄôEõ4•¤R%ÒÒ7G"ƒ‚¢#B¢#B¢Vçe²%4TDµô$4µUôÔ…ôU…E$5DTEõ4•¤R%ÒÒ7G"ƒb¢#B¢#B¢&WGW&â7V'&ö6W72ç'Vâ€¢·7G"…F‚‚'67&—G2"’ò'&W7F÷&Rç6‚"’Â¦Ö‡7G"Â&w2•ÒÀ¢7vCÕF‚…õöf–ÆUõò’ç&W6öÇfR‚’ç&VçG5³ÒÀ¢FW‡CÕG'VRÀ¢6GW&Uö÷WGWCÕG'VRÀ¢6†V6³ÔfÇ6RÀ¢VçcÖVçbÀ¢ ¢FVb'Våö&6·W÷67&—B‡6VÆbÂ¦&w2“ ¢&WGW&â7V'&ö6W72ç'Vâ€¢·7G"…F‚‚'67&—G2"’ò&&6·Wç6‚"’Â¦Ö‡7G"Â&w2•ÒÀ¢7vCÕF‚…õöf–ÆUõò’ç&W6öÇfR‚’ç&VçG5³ÒÀ¢FW‡CÕG'VRÀ¢6GW&Uö÷WGWCÕG'VRÀ¢6†V6³ÔfÇ6RÀ¢ ¢FVbFW7Eö&6·Wö6VçFW%÷7WW'W6W%öæEö÷væW%ö6å÷f–Wr‡6VÆb“ ¢f÷"W6W"–â‡6VÆbç7WW'W6W"Â6VÆbæ÷væW"“ ¢&W7öç6RÒ6VÆbæÆöv–â‡W6W"’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·Wö6VçFW""’¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ-›í‹MŠ­¸ÍŠŠ}˜n(Íªý¸Í‹¸Â˜‚Š}˜mŠ­˜-Š}˜B‹=‹˜‹" ¢FVbFW7Eö&6·Wö6VçFW%öFVæ–W5öæöå÷7FfeöæE÷7W÷'EövVçB‡6VÆb“ ¢&VwVÆ"Ò6VÆbåW6W"æö&¦V7G2æ7&VFU÷W6W"‚&&6·W×&VwVÆ""Â77v÷&CÒ'6V7&WB" ¢6VÆbæ76W'Dæ÷DWVÂ‡6VÆbæÆöv–â‡&VwVÆ"’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·Wö6VçFW""’’ç7FGW5ö6öFRÂ#¢6VÆbæ76W'DWVÂ‡6VÆbæÆöv–â‡6VÆbç7W÷'B’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·Wö6VçFW""’’ç7FGW5ö6öFRÂC2 ¢FVbFW7EövWEö7&VFUö&6·Wö†5öæõ÷6–FUöVffV7EöæEöæõ÷7V'&ö6W72‡6VÆb“ ¢v—F‚F6‚‚'7F÷&Ræ&6·W÷&W7F÷&U÷6W'f–6W2ç7V'&ö6W72ç'Vâ"’2'VåöÖö6³ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W"’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·Wö7&VFR"’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂCR¢6VÆbæ76W'DWVÂ…6VF´&6·W¦ö"æö&¦V7G2æ6÷VçB‚’Â¢'VåöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢FVbFW7EöF%ööæÇ•ö&6·W÷÷7Eö7&VFW5ö¦ö%÷v—F†÷WEöÆ—fUö–çFVw&F–öç2‡6VÆb“ ¢FVbf¶U÷'Vâ†¦ö"“ ¢&6†—fRÒ6VÆbæ&6·W÷&ö÷Bò'6VF²Ö&6·WÖf¶RçF"æw¢ ¢&6†—fRç&VçBæÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢&6†—fRçw&—FUö'—FW2†"&f¶R"¢¦ö"ç7FGW2Ò6VF´&6·W¦ö"å7FGW2ä4ôÕÄUDT@¢¦ö"æf–ÆU÷F‚Ò7G"†&6†—fR¢¦ö"æf–ÆUöæÖRÒ&6†—fRææÖP¢¦ö"æf–ÆU÷6—¦RÒ&6†—fRç7FB‚’ç7E÷6—¦P¢¦ö"ç6†#SbÒ&"¢c@¢¦ö"ç6fR‚¢&WGW&â¦ö  ¢v—F‚€¢F6‚‚'7F÷&RæFÖ–åö&6·W÷&W7F÷&Rç'Våö&6·Wö¦ö""Â6–FUöVffV7CÖf¶U÷'Vâ’2'VåöÖö6²À¢F6‚‚'7F÷&Ræ&÷G2ç&WVW7G2ç÷7B"’2FVÆVw&ÕöÖö6²À¢F6‚‚'7F÷&Rç‡V•ö’ç&WVW7G2å6W76–öâ"’2‡V•öÖö6²À¢“ ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W"’ç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö&6·Wö7&VFR"’À¢²&&6·W÷G—R#¢6VF´&6·W¦ö"ä&6·WG—RäD%ôôäÅ—ÒÀ¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢¦ö"Ò6VF´&6·W¦ö"æö&¦V7G2ævWB‚¢6VÆbæ76W'DWVÂ†¦ö"æ&6·W÷G—RÂ6VF´&6·W¦ö"ä&6·WG—RäD%ôôäÅ’¢6VÆbæ76W'DWVÂ†¦ö"ç7FGW2Â6VF´&6·W¦ö"å7FGW2ä4ôÕÄUDTB¢6VÆbæ76W'DfÇ6R†¦ö"æ–æ6ÇVFW5öÖVF–¢'VåöÖö6²æ76W'Eö6ÆÆVEööæ6R‚¢FVÆVw&ÕöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚¢‡V•öÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢FVbFW7EöVçeö–æ6ÇVF–æuö&6·W÷&WV—&W5÷7G&öævW%÷W&Ö—76–öâ‡6VÆb“ ¢6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbçFV6†æ–6Â¢6Æ–VçBç&—6U÷&WVW7EöW†6WF–öâÒfÇ6P ¢v—F‚F6‚‚'7F÷&RæFÖ–åö&6·W÷&W7F÷&Rç'Våö&6·Wö¦ö""’2'VåöÖö6³ ¢&W7öç6RÒ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö&6·Wö7&VFR"’À¢²&&6·W÷G—R#¢6VF´&6·W¦ö"ä&6·WG—RäeTÄÅõE$å4dU'ÒÀ¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂC2¢6VÆbæ76W'DWVÂ…6VF´&6·W¦ö"æö&¦V7G2æ6÷VçB‚’Â¢'VåöÖö6²æ76W'Eöæ÷Eö6ÆÆVB‚ ¢FVbFW7Eö&6·Wö&6†—fUö6öçF–ç5÷&WV—&VEöf–ÆW5öæEö6†V6·7V×2‡6VÆb“ ¢&6†—fRÒ6VÆbæÖ¶Uö&6·Wö&6†—fR‚ ¢v—F‚F&f–ÆRæ÷Vâ†&6†—fRÂ'#¦w¢"’2F# ¢æÖW2Ò6WB‡F"ævWFæÖW2‚’ ¢6VÆbæ76W'D–â‚&Öæ–fW7Bæ§6öâ"ÂæÖW2¢6VÆbæ76W'D–â‚&6†V6·7V×2ç6†#Sb"ÂæÖW2¢6VÆbæ76W'D–â‚&FF&6RöF"ç7Æ—FS2"ÂæÖW2 ¢FVbFW7Eö&6·W÷67&—EöF%ööæÇ•÷6¶vUö†5÷&ö÷EöÖæ–fW7EöæEöW†6ÇVFW5öVçeö'•öFVfVÇB‡6VÆb“ ¢–ç7FÆÅöF—"Ò6VÆbæ&6U÷F‚ò'67&—BÖ–ç7FÆÂ ¢F%÷F‚Ò–ç7FÆÅöF—"ò&FF"ò&F"ç7Æ—FS2 ¢÷WGWEöF—"Ò6VÆbæ&6U÷F‚ò'67&—BÖ&6·W2 ¢–ç7FÆÅöF—"æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢6VÆbçw&—FU÷7Æ—FUöFF&6R†F%÷F‚¢†–ç7FÆÅöF—"ò"æVçb"’çw&—FU÷FW‡B€¢%Æâ"æ¦ö–â€¢°¢$DD$4UôTät”äS×7Æ—FR"À¢b%5Ä•DUôDD$4UõDƒ×¶F%÷F‡Ò"À¢b%4T5$UEô´U“×·6VÆbç&u÷6V7&WGÒ"À¢$$õEõDô´TãÖGVÖ×’×Fö¶Vâ"À¢Ð¢¢²%Æâ"À¢Væ6öF–æsÒ'WFbÓ‚"À¢ ¢&W7VÇBÒ6VÆbç'Våö&6·W÷67&—B‚"ÒÖ–ç7FÆÂÖF—""Â–ç7FÆÅöF—"Â"ÒÖ÷WGWBÖF—""Â÷WGWEöF—"Â"Ò×–W2" ¢6VÆbæ76W'DWVÂ‡&W7VÇBç&WGW&æ6öFRÂÂ&W7VÇBç7FFW'"¢&6†—fW2ÒÆ—7B†÷WGWEöF—"ævÆö"‚'6VF²Ö&6·WÒ¢çF"æw¢"’¢6VÆbæ76W'DWVÂ†ÆVâ†&6†—fW2’Â¢v—F‚F&f–ÆRæ÷Vâ†&6†—fW5³ÒÂ'#¦w¢"’2F# ¢&uöæÖW2ÒF"ævWFæÖW2‚¢æÖW2Ò¶æÖRç&VÖ÷fW&Vf—‚‚"âò"’f÷"æÖR–â&uöæÖW2–bæÖRæ÷B–â²"â"Â"âò'×Ð¢Öæ–fW7EöÖVÖ&W"ÒæW‡B†æÖRf÷"æÖR–â&uöæÖW2–bæÖRç&VÖ÷fW&Vf—‚‚"âò"’ÓÒ&Öæ–fW7Bæ§6öâ"¢Öæ–fW7BÒ§6öâæÆöG2‡F"æW‡G&7Ff–ÆR†Öæ–fW7EöÖVÖ&W"’ç&VB‚’æFV6öFR‚'WFbÓ‚"’ ¢6VÆbæ76W'D–â‚&Öæ–fW7Bæ§6öâ"ÂæÖW2¢6VÆbæ76W'D–â‚&6†V6·7V×2ç6†#Sb"ÂæÖW2¢6VÆbæ76W'D–â‚&FF&6RöF"ç7Æ—FS2"ÂæÖW2¢6VÆbæ76W'Dæ÷D–â‚&Vçb÷&öGV7F–öâæVçb"ÂæÖW2¢6VÆbæ76W'DfÇ6R†ç’†æÖRç7F'G7v—F‚‚'6VF²Ö&6·WÒ"’f÷"æÖR–âæÖW2’¢Öæ–fW7E÷FW‡BÒ§6öâæGV×2†Öæ–fW7BÂVç7W&Uö66–“ÔfÇ6R¢6VÆbæ76W'Dæ÷D–â‡6VÆbç&u÷6V7&WBÂÖæ–fW7E÷FW‡B¢6VÆbæ76W'DWVÂ†Öæ–fW7E²&&6·W÷G—R%ÒÂ&F%ööæÇ’"¢6VÆbæ76W'DfÇ6R†Öæ–fW7E²&–æ6ÇVFW5öVçb%Ò ¢FVbFW7E÷fÆ–FFU÷&VF7G5÷6V7&WEöÖæ–fW7E÷fÇVW5öæEö66WG5÷÷7Fw&W5÷6†R‡6VÆb“ ¢g&öÒæ&6·W÷&W7F÷&U÷6W'f–6W2–×÷'BfÆ–FFUö&6·Wö&6†—fP ¢&6†—fRÒ6VÆbæÖ¶Uö&6·Wö&6†—fR€¢Væv–æSÒ'÷7Fw&W2"À¢Öæ–fW7Eö÷fW'&–FW3×°¢&&÷E÷Fö¶Vâ#¢6VÆbç&u÷6V7&WBÀ¢'æVÅ÷77v÷&B#¢'æVÂ×77v÷&B×6V7&WB"À¢&7W7FöÖW%öVÖ–Â#¢&Æ–6Rç&—fFTW†×ÆRæ6öÒ"À¢&6öæf–uöÆ–æ²#¢'fÆW73¢òóÓÓCÓƒÓW†×ÆRæ6öÒ"À¢ÒÀ¢ ¢v—F‚F6‚‚'7F÷&Ræ&6·W÷&W7F÷&U÷6W'f–6W2ç6‡WF–Âçv†–6‚"Â&WGW&å÷fÇVSÒ"÷W7"ö&–â÷u÷&W7F÷&R"“ ¢7VÖÖ'’ÒfÆ–FFUö&6·Wö&6†—fR†&6†—fR ¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&FF&6UöVæv–æR%ÒÂ'÷7Fw&W2"¢6VÆbæ76W'DWVÂ‡7VÖÖ'•²&FF&6U÷fVæF÷"%ÒÂ'÷7Fw&W7Â"¢7VÖÖ'•÷FW‡BÒ§6öâæGV×2‡7VÖÖ'’ÂVç7W&Uö66–“ÔfÇ6R¢6VÆbæ76W'Dæ÷D–â‡6VÆbç&u÷6V7&WBÂ7VÖÖ'•÷FW‡B¢6VÆbæ76W'Dæ÷D–â‚'æVÂ×77v÷&B×6V7&WB"Â7VÖÖ'•÷FW‡B¢6VÆbæ76W'Dæ÷D–â‚&Æ–6Rç&—fFTW†×ÆRæ6öÒ"Â7VÖÖ'•÷FW‡B¢6VÆbæ76W'Dæ÷D–â‚'fÆW73¢òò"Â7VÖÖ'•÷FW‡B ¢FVbFW7EöF÷væÆöE÷&WV—&W5ö6&–Æ—G•öVçe÷&WV—&W5÷7WW'W6W%öæE÷G&fW'6Åö—5ö&Æö6¶VB‡6VÆb“ ¢&6†—fRÒ6VÆbæ&6·W÷&ö÷Bò'6VF²Ö&6·WÖF÷væÆöBçF"æw¢ ¢&6†—fRç&VçBæÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢&6†—fRçw&—FUö'—FW2†"&F÷væÆöB"¢æ÷&ÖÂÒ6VF´&6·W¦ö"æö&¦V7G2æ7&VFR€¢7&VFVEö'“×6VÆbç7WW'W6W"À¢7FGW3Õ6VF´&6·W¦ö"å7FGW2ä4ôÕÄUDTBÀ¢f–ÆU÷Fƒ×7G"†&6†—fR’À¢f–ÆUöæÖSÖ&6†—fRææÖRÀ¢f–ÆU÷6—¦SÖ&6†—fRç7FB‚’ç7E÷6—¦RÀ¢6†#ScÒ&""¢cBÀ¢¢Vçeö¦ö"Ò6VF´&6·W¦ö"æö&¦V7G2æ7&VFR€¢7&VFVEö'“×6VÆbç7WW'W6W"À¢7FGW3Õ6VF´&6·W¦ö"å7FGW2ä4ôÕÄUDTBÀ¢f–ÆU÷Fƒ×7G"†&6†—fR’À¢f–ÆUöæÖSÖ&6†—fRææÖRÀ¢f–ÆU÷6—¦SÖ&6†—fRç7FB‚’ç7E÷6—¦RÀ¢6†#ScÒ&2"¢cBÀ¢–æ6ÇVFW5öVçcÕG'VRÀ¢¢÷WG6–FRÒ6VÆbæ&6U÷F‚ò&÷WG6–FRçF"æw¢ ¢÷WG6–FRçw&—FUö'—FW2†"&÷WG6–FR"¢G&fW'6ÂÒ6VF´&6·W¦ö"æö&¦V7G2æ7&VFR€¢7&VFVEö'“×6VÆbç7WW'W6W"À¢7FGW3Õ6VF´&6·W¦ö"å7FGW2ä4ôÕÄUDTBÀ¢f–ÆU÷Fƒ×7G"†÷WG6–FR’À¢f–ÆUöæÖSÖ÷WG6–FRææÖRÀ¢f–ÆU÷6—¦SÖ÷WG6–FRç7FB‚’ç7E÷6—¦RÀ¢6†#ScÒ&B"¢cBÀ¢ ¢6VÆbæ76W'DWVÂ‡6VÆbæÆöv–â‡6VÆbç7W÷'B’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·WöF÷væÆöB"Â&w3Õ¶æ÷&ÖÂçµÒ’’ç7FGW5ö6öFRÂC2¢6VÆbæ76W'DWVÂ‡6VÆbæÆöv–â‡6VÆbçFV6†æ–6Â’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·WöF÷væÆöB"Â&w3Õ¶Vçeö¦ö"çµÒ’’ç7FGW5ö6öFRÂC2¢6VÆbæ76W'DWVÂ‡6VÆbæÆöv–â‡6VÆbç7WW'W6W"’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·WöF÷væÆöB"Â&w3Õ¶Vçeö¦ö"çµÒ’’ç7FGW5ö6öFRÂ#¢6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W"¢6Æ–VçBç&—6U÷&WVW7EöW†6WF–öâÒfÇ6P¢6VÆbæ76W'DWVÂ†6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·WöF÷væÆöB"Â&w3Õ·G&fW'6ÂçµÒ’’ç7FGW5ö6öFRÂC ¢FVbFW7EöFVÆWFUö&6·W÷&WV—&W5÷÷7EöæEöW†7Eö6öæf—&ÖF–öâ‡6VÆb“ ¢&6†—fRÒ6VÆbæ&6·W÷&ö÷Bò'6VF²Ö&6·WÖFVÆWFRçF"æw¢ ¢&6†—fRç&VçBæÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢&6†—fRçw&—FUö'—FW2†"&FVÆWFR"¢¦ö"Ò6VF´&6·W¦ö"æö&¦V7G2æ7&VFR€¢7&VFVEö'“×6VÆbç7WW'W6W"À¢7FGW3Õ6VF´&6·W¦ö"å7FGW2ä4ôÕÄUDTBÀ¢f–ÆU÷Fƒ×7G"†&6†—fR’À¢f–ÆUöæÖSÖ&6†—fRææÖRÀ¢f–ÆU÷6—¦SÖ&6†—fRç7FB‚’ç7E÷6—¦RÀ¢6†#ScÒ&R"¢cBÀ¢¢6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W" ¢6VÆbæ76W'DWVÂ†6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·WöFVÆWFR"Â&w3Õ¶¦ö"çµÒ’’ç7FGW5ö6öFRÂCR¢6VÆbæ76W'DWVÂ€¢6Æ–VçBç÷7B‡&WfW'6R‚&FÖ–å÷7F÷&Uö&6·WöFVÆWFR"Â&w3Õ¶¦ö"çµÒ’Â²&6öæf—&ÖF–öâ#¢'w&öær'Ò’ç7FGW5ö6öFRÀ¢3"À¢¢¦ö"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†¦ö"ç7FGW2Â6VF´&6·W¦ö"å7FGW2ä4ôÕÄUDTB¢6VÆbæ76W'EG'VR†&6†—fRæW†—7G2‚’¢&W7öç6RÒ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&Uö&6·WöFVÆWFR"Â&w3Õ¶¦ö"çµÒ’À¢²&6öæf—&ÖF–öâ#¢b$DTÄUDUõ4TDµô$4µU÷¶¦ö"ç·Ò'ÒÀ¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢¦ö"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†¦ö"ç7FGW2Â6VF´&6·W¦ö"å7FGW2äDTÄUDTB¢6VÆbæ76W'DfÇ6R†&6†—fRæW†—7G2‚’ ¢FVbFW7E÷&W7F÷&U÷WÆöE÷&WV—&W5÷÷7EöæE÷&—fFU÷F%öw¥öæÖR‡6VÆb“ ¢6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W" ¢6VÆbæ76W'DWVÂ†6Æ–VçBævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷&W7F÷&U÷WÆöB"’’ç7FGW5ö6öFRÂCR¢&W7öç6RÒ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷&W7F÷&U÷WÆöB"’À¢²&&6·Wöf–ÆR#¢6–×ÆUWÆöFVDf–ÆR‚&&6·Wç¦—"Â"&æ÷B66WFVB"—ÒÀ¢ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢6VÆbæ76W'DWVÂ…6VFµ&W7F÷&T¦ö"æö&¦V7G2æ6÷VçB‚’Â ¢FVbFW7Eö–çfÆ–EöÖ—76–æuöÖæ–fW7Eö6†V6·7VÕ÷G&fW'6Å÷7–ÖÆ–æµöæE÷fW'6–öå÷&V¦V7FVB‡6VÆb“ ¢g&öÒæ&6·W÷&W7F÷&U÷6W'f–6W2–×÷'B&6·WfÆ–FF–öäW'&÷"ÂfÆ–FFUö&6·Wö&6†—fP ¢–çfÆ–BÒ6VÆbæ&6U÷F‚ò'6VF²Ö–çfÆ–BçF"æw¢ ¢–çfÆ–Bçw&—FUö'—FW2†"&æ÷BF&&ÆÂ"¢Ö—76–æuöÖæ–fW7BÒ6VÆbæ&6U÷F‚ò'6VF²ÖÖ—76–ærÖÖæ–fW7BçF"æw¢ ¢v—F‚F&f–ÆRæ÷Vâ†Ö—76–æuöÖæ–fW7BÂ's¦w¢"’2F# ¢FFÒ"&†VÆÆò ¢–æfòÒF&f–ÆRåF$–æfò‚&FF&6RöF"ç7Æ—FS2"¢–æfòç6—¦RÒÆVâ†FF¢F"æFFf–ÆR†–æfòÂ'—FW4”ò†FF’¢6†V6·7VÕöÖ—6ÖF6‚Ò6VÆbæÖ¶Uö&6·Wö&6†—fR†æÖSÒ'6VF²Ö6†V6·7VÒçF"æw¢"Â6†V6·7VÕöÖ—6ÖF6ƒÕG'VR¢G&fW'6ÂÒ6VÆbæÖ¶U÷Vç6fUö&6†—fR‚'6VF²×G&fW'6ÂçF"æw¢"ÂÖVÖ&W%öæÖSÒ"ââöWf–ÂçG‡B"¢7–ÖÆ–æ²Ò6VÆbæÖ¶U÷Vç6fUö&6†—fR‚'6VF²×7–ÖÆ–æ²çF"æw¢"Â7–ÖÆ–æ³ÕG'VR¢Vç7W÷'FVBÒ6VÆbæÖ¶Uö&6·Wö&6†—fR€¢æÖSÒ'6VF²×Vç7W÷'FVBçF"æw¢"À¢Öæ–fW7Eö÷fW'&–FW3×²'6VFµö&6·W÷fW'6–öâ#¢#““’'ÒÀ¢ ¢f÷"&6†—fR–â†–çfÆ–BÂÖ—76–æuöÖæ–fW7BÂ6†V6·7VÕöÖ—6ÖF6‚ÂG&fW'6ÂÂ7–ÖÆ–æ²ÂVç7W÷'FVB“ ¢v—F‚6VÆbç7V%FW7B†&6†—fSÖ&6†—fRææÖR“ ¢v—F‚6VÆbæ76W'E&—6W2„&6·WfÆ–FF–öäW'&÷"“ ¢fÆ–FFUö&6·Wö&6†—fR†&6†—fR ¢FVbFW7E÷&W7F÷&U÷fÆ–FFUö7&VFW5÷6fU÷Æâ‡6VÆb“ ¢&6†—fRÒ6VÆbæÖ¶Uö&6·Wö&6†—fR€¢Öæ–fW7Eö÷fW'&–FW3×°¢&&÷E÷Fö¶Vâ#¢6VÆbç&u÷6V7&WBÀ¢&7W7FöÖW%öVÖ–Â#¢&Æ–6Rç&—fFTW†×ÆRæ6öÒ"À¢Ð¢¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W"’ç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷&W7F÷&U÷WÆöB"’À¢²&&6·Wöf–ÆR#¢6–×ÆUWÆöFVDf–ÆR†&6†—fRææÖRÂ&6†—fRç&VEö'—FW2‚’Â6öçFVçE÷G—SÒ&Æ–6F–öâöw¦—"—ÒÀ¢¢¦ö"Ò6VFµ&W7F÷&T¦ö"æö&¦V7G2ævWB‚ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W"’ç÷7B‡&WfW'6R‚&FÖ–å÷7F÷&U÷&W7F÷&U÷fÆ–FFR"Â&w3Õ¶¦ö"çµÒ’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢¦ö"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ†¦ö"ç7FGW2Â6VFµ&W7F÷&T¦ö"å7FGW2ådÄ”DDTB¢6VÆbæ76W'DWVÂ†¦ö"ç&W7F÷&U÷Æå²'6÷W&6UöFF&6UöVæv–æR%ÒÂ'7Æ—FR"¢Æå÷FW‡BÒ§6öâæGV×2†¦ö"ç&W7F÷&U÷ÆâÂVç7W&Uö66–“ÔfÇ6R¢7VÖÖ'•÷FW‡BÒ§6öâæGV×2†¦ö"çfÆ–FF–öå÷7VÖÖ'’ÂVç7W&Uö66–“ÔfÇ6R¢6VÆbæ76W'Dæ÷D–â‡6VÆbç&u÷6V7&WBÂÆå÷FW‡B²7VÖÖ'•÷FW‡B¢6VÆbæ76W'Dæ÷D–â‚&Æ–6Rç&—fFTW†×ÆRæ6öÒ"ÂÆå÷FW‡B²7VÖÖ'•÷FW‡B ¢FVbFW7E÷&W7F÷&Uö6öÖÖæE÷&WV—&W5÷fÆ–FFVEö¦ö%ö6öæf—&ÖF–öåöæEö†5öæõ÷6V7&WG2‡6VÆb“ ¢g&öÒæ&6·W÷&W7F÷&U÷6W'f–6W2–×÷'B&W7F÷&Uö6öæf—&ÖF–öå÷‡&6RÂfÆ–FFU÷&W7F÷&Uö¦ö  ¢&6†—fRÒ6VÆbæÖ¶Uö&6·Wö&6†—fR†Öæ–fW7Eö÷fW'&–FW3×²&&÷E÷Fö¶Vâ#¢6VÆbç&u÷6V7&WGÒ¢¦ö"Ò6VÆbçWÆöEö&6†—fR†&6†—fR¢6Æ–VçBÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W" ¢&W7öç6RÒ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷&W7F÷&Uö6öÖÖæB"Â&w3Õ¶¦ö"çµÒ’À¢²&6öæf—&ÖF–öâ#¢&W7F÷&Uö6öæf—&ÖF–öå÷‡&6R†¦ö"ç²—ÒÀ¢¢¦ö"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢6VÆbæ76W'DWVÂ†¦ö"ç7FGW2Â6VFµ&W7F÷&T¦ö"å7FGW2åUÄôDTB ¢fÆ–FFU÷&W7F÷&Uö¦ö"†¦ö"Â7F÷#×6VÆbç7WW'W6W"¢&W7öç6RÒ6Æ–VçBç÷7B‡&WfW'6R‚&FÖ–å÷7F÷&U÷&W7F÷&Uö6öÖÖæB"Â&w3Õ¶¦ö"çµÒ’Â²&6öæf—&ÖF–öâ#¢'w&öær'Ò¢¦ö"ç&Vg&W6…ög&öÕöF"‚¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢6VÆbæ76W'DWVÂ†¦ö"ç7FGW2Â6VFµ&W7F÷&T¦ö"å7FGW2ådÄ”DDTB ¢&W7öç6RÒ6Æ–VçBç÷7B€¢&WfW'6R‚&FÖ–å÷7F÷&U÷&W7F÷&Uö6öÖÖæB"Â&w3Õ¶¦ö"çµÒ’À¢²&6öæf—&ÖF–öâ#¢&W7F÷&Uö6öæf—&ÖF–öå÷‡&6R†¦ö"ç²’Â&–æ6ÇVFUöÖVF–#¢&öâ'ÒÀ¢¢¦ö"ç&Vg&W6…ög&öÕöF"‚¢6öÖÖæBÒ¦ö"ç&W7F÷&U÷ÆâævWB‚'&W7F÷&Uö6öÖÖæB"Â"" ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ3"¢6VÆbæ76W'DWVÂ†¦ö"ç7FGW2Â6VFµ&W7F÷&T¦ö"å7FGW2å$U5Dõ$Uô4ôÔÔäEôtTäU$DTB¢6VÆbæ76W'D–â‚'67&—G2÷&W7F÷&Rç6‚"Â6öÖÖæB¢6VÆbæ76W'D–â†b"Ò×&W7F÷&RÖ¦ö"Ö–B¶¦ö"ç·Ò"Â6öÖÖæB¢6VÆbæ76W'Dæ÷D–â‡6VÆbç&u÷6V7&WBÂ§6öâæGV×2†¦ö"ç&W7F÷&U÷ÆâÂVç7W&Uö66–“ÔfÇ6R’¢6VÆbæ76W'DWVÂ†¦ö"ç&W7F÷&U÷Æå²'vV%÷&W7F÷&UöÇ’%ÒÂ&F—6&ÆVB" ¢FVbFW7E÷&W7F÷&UöFWF–ÅöFöW5öæ÷EööffW%ööæUö6Æ–6µöÇ•ö'•öFVfVÇB‡6VÆb“ ¢&6†—fRÒ6VÆbæÖ¶Uö&6·Wö&6†—fR‚¢¦ö"Ò6VÆbçWÆöEö&6†—fR†&6†—fR ¢&W7öç6RÒ6VÆbæÆöv–â‡6VÆbç7WW'W6W"’ævWB‡&WfW'6R‚&FÖ–å÷7F÷&U÷&W7F÷&UöFWF–Â"Â&w3Õ¶¦ö"çµÒ’ ¢6VÆbæ76W'DWVÂ‡&W7öç6Rç7FGW5ö6öFRÂ#¢6VÆbæ76W'D6öçF–ç2‡&W7öç6RÂ$öæRÖ6Æ–6²&W7F÷&RŠý‹‹­¸Í‹˜‹Š}˜BŠ}‹=Š¢"¢6VÆbæ76W'Dæ÷D6öçF–ç2‡&W7öç6RÂ%4TDµôDÔ”åõ$U5Dõ$UôTä$ÄTCÕG'VR" ¢FVbFW7E÷&W7F÷&U÷67&—EöG'•÷'Vå÷fÆ–FFW5÷v—F†÷WEö×WFF–öâ‡6VÆb“ ¢–ç7FÆÅöF—"Ò6VÆbçw&—FUö–ç7FÆÅöVçb‚'7Æ—FR"¢&6†—fRÒ6VÆbæÖ¶Uö&6·Wö&6†—fR‚¢&Vf÷&UöVçbÒ†–ç7FÆÅöF—"ò"æVçb"’ç&VE÷FW‡B†Væ6öF–æsÒ'WFbÓ‚" ¢&W7VÇBÒ6VÆbç'Vå÷&W7F÷&U÷67&—B‚"ÒÖ–ç7FÆÂÖF—""Â–ç7FÆÅöF—"Â"ÒÖ&6·WÖf–ÆR"Â&6†—fRÂ"ÒÖG'’×'Vâ" ¢6VÆbæ76W'DWVÂ‡&W7VÇBç&WGW&æ6öFRÂÂ&W7VÇBç7FFW'"¢6VÆbæ76W'D–â‚'fÆ–FF–öãÖö²"Â&W7VÇBç7FF÷WB¢6VÆbæ76W'D–â‚$E%’Õ%Tâ"Â&W7VÇBç7FF÷WB¢6VÆbæ76W'DWVÂ‚†–ç7FÆÅöF—"ò"æVçb"’ç&VE÷FW‡B†Væ6öF–æsÒ'WFbÓ‚"’Â&Vf÷&UöVçb ¢FVbFW7E÷&W7F÷&U÷67&—E÷&VgW6W5öÖ—76–æuö6öæf—&ÖF–öâ‡6VÆb“ ¢–ç7FÆÅöF—"Ò6VÆbçw&—FUö–ç7FÆÅöVçb‚'7Æ—FR"¢&6†—fRÒ6VÆbæÖ¶Uö&6·Wö&6†—fR‚ ¢&W7VÇBÒ6VÆbç'Vå÷&W7F÷&U÷67&—B‚"ÒÖ–ç7FÆÂÖF—""Â–ç7FÆÅöF—"Â"ÒÖ&6·WÖf–ÆR"Â&6†—fR ¢6VÆbæ76W'Dæ÷DWVÂ‡&W7VÇBç&WGW&æ6öFRÂ¢6VÆbæ76W'D–â‚"ÒÖ6öæf—&Ò—2&WV—&VB"Â&W7VÇBç7FFW'" ¢FVbFW7E÷&W7F÷&U÷67&—E÷&VgW6W5÷F…÷G&fW'6Åö&6†—fR‡6VÆb“ ¢–ç7FÆÅöF—"Ò6VÆbçw&—FUö–ç7FÆÅöVçb‚'7Æ—FR"¢&6†—fRÒ6VÆbæÖ¶U÷Vç6fUö&6†—fR‚'67&—B×G&fW'6ÂçF"æw¢"ÂÖVÖ&W%öæÖSÒ"ââöWf–ÂçG‡B" ¢&W7VÇBÒ6VÆbç'Vå÷&W7F÷&U÷67&—B‚"ÒÖ–ç7FÆÂÖF—""Â–ç7FÆÅöF—"Â"ÒÖ&6·WÖf–ÆR"Â&6†—fRÂ"ÒÖG'’×'Vâ" ¢6VÆbæ76W'Dæ÷DWVÂ‡&W7VÇBç&WGW&æ6öFRÂ¢6VÆbæ76W'D–â‚'F‚G&fW'6Â"Â&W7VÇBç7FF÷WB²&W7VÇBç7FFW'" ¢FVbFW7E÷&W7F÷&U÷67&—EöFWFV7G5öVæv–æUöÖ—6ÖF6‚‡6VÆb“ ¢–ç7FÆÅöF—"Ò6VÆbçw&—FUö–ç7FÆÅöVçb‚'÷7Fw&W2"¢&6†—fRÒ6VÆbæÖ¶Uö&6·Wö&6†—fR‚ ¢&W7VÇBÒ6VÆbç'Vå÷&W7F÷&U÷67&—B‚"ÒÖ–ç7FÆÂÖF—""Â–ç7FÆÅöF—"Â"ÒÖ&6·WÖf–ÆR"Â&6†—fRÂ"ÒÖG'’×'Vâ" ¢6VÆbæ76W'Dæ÷DWVÂ‡&W7VÇBç&WGW&æ6öFRÂ¢6VÆbæ76W'D–â‚&Væv–æRÖ—6ÖF6‚"Â&W7VÇBç7FF÷WB²&W7VÇBç7FFW'"