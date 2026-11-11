"""Maintenance CLI: ``python -m shoplegacy.maintenance``.

``purge [--dry-run]`` deletes shops past their retention deadline and prints
their ids one per line; ``--dry-run`` runs the same deletion and rolls it back.
``generate-key`` prints a new key for ``SHOPLEGACY_PII_KEYS``.
``set-plan <org_id> <free|pilot|org>`` changes an org's plan (operators do this
after a contract; there is no API for it) and writes an ``org.plan`` audit row.
Row security lets only an org owner update ``orgs``, and the app role has no
owner identity here, so run it with ``SHOPLEGACY_DATABASE_URL`` set to a DSN
that bypasses row security (the database owner), the same variable ``purge`` reads.
``issue-invoice <org_id> <pilot|org> <YYYY-MM> [--amount N] [--memo ...]`` issues an
invoice for that month (the plan's list price unless ``--amount``) and prints its id.
``mark-paid <invoice_id> --method contract|card [--memo ...]`` records a payment made
outside the card checkout; the org moves to the invoice's plan.
``void-invoice <invoice_id>`` cancels an unpaid invoice.
``refund-invoice <invoice_id> --reason TEXT`` refunds a paid invoice in full: a card
payment is cancelled with the payment provider first.
``mark-tax-invoice <invoice_id> --ref TEXT [--issued YYYY-MM-DD]`` records the tax
invoice number issued for a paid (or refunded) invoice.
``charge-due [--today YYYY-MM-DD]`` charges the billing keys due that day (the daily
job does the same) and prints ``<org_id> <status> <invoice_id> <code>`` per key.
``stop-autopay <org_id>`` stops an org's monthly card charge.
Failures of these four print a code to stderr and exit 1; 2 when the provider
refuses a refund (nothing recorded), 3 when the card payment was cancelled but
the refund could not be recorded (record it by hand).
"""

import argparse
import os
import re
import sys
from datetime import UTC, date, datetime

import psycopg
import structlog
from psycopg.types.json import Jsonb

from shoplegacy import autopay
from shoplegacy.billing import PLANS
from shoplegacy.config import load_settings
from shoplegacy.crypto import build_cipher, generate_key
from shoplegacy.observability import configure_logging
from shoplegacy.payments import PLAN_PRICES_KRW, PaymentError, build_payment_provider
from shoplegacy.store import (
    BillingKeyNotFoundError,
    InvoiceNotPayableError,
    InvoiceNotRefundableError,
    InvoiceRow,
    Store,
)


class _Failed(Exception):
    """A command failure: ``code`` goes to stderr and the CLI exits with ``exit_code``."""

    def __init__(self, code: str, exit_code: int = 1) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


def _database_url() -> str:
    dsn = os.environ.get("SHOPLEGACY_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("SHOPLEGACY_DATABASE_URL is required")
    return dsn


def _purge(dry_run: bool) -> list[str]:
    dsn = _database_url()
    now = datetime.now(UTC)
    if dry_run:
        with psycopg.connect(dsn) as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM public.purge_expired_shops(%s)", (now,)
                ).fetchall()
            finally:
                conn.rollback()
        return [str(r[0]) for r in rows]
    store = Store(dsn, min_size=1, max_size=1)
    try:
        return store.purge_expired(now)
    finally:
        store.close()


def _set_plan(org_id: str, plan: str) -> str:
    """Set the org's plan and audit it; returns the previous plan."""
    with psycopg.connect(_database_url()) as conn, conn.transaction():
        row = conn.execute(
            "SELECT plan FROM public.orgs WHERE org_id::text = %s FOR UPDATE", (org_id,)
        ).fetchone()
        if row is None:
            raise SystemExit(
                f"org {org_id} not found (or this DSN cannot see it: use the database owner)"
            )
        before = str(row[0])
        conn.execute("UPDATE public.orgs SET plan = %s WHERE org_id::text = %s", (plan, org_id))
        conn.execute(
            "INSERT INTO public.audit_log (org_id, action, table_name, row_id, new_json) "
            "VALUES (%s, 'org.plan', 'orgs', %s, %s)",
            (org_id, org_id, Jsonb({"from": before, "to": plan})),
        )
    return before


def _month(value: str) -> tuple[date, date]:
    """``YYYY-MM`` -> (first day of that month, first day of the next)."""
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}")
    year, month = int(value[:4]), int(value[5:])
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}")
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _issue_invoice(
    org_id: str, plan: str, period: tuple[date, date], amount: int, memo: str | None
) -> str:
    store = Store(_database_url(), min_size=1, max_size=1)
    try:
        row = store.issue_invoice(org_id, plan, period[0], period[1], amount, memo)
    except psycopg.Error as exc:
        raise SystemExit(f"cannot issue invoice: {exc.diag.message_primary or exc}") from None
    finally:
        store.close()
    return row.invoice_id


def _mark_paid(invoice_id: str, method: str, memo: str | None) -> None:
    dsn = _database_url()
    store = Store(dsn, min_size=1, max_size=1)
    try:
        store.mark_invoice_paid(invoice_id, method, "manual", None, datetime.now(UTC))
    except LookupError:
        raise SystemExit(f"invoice {invoice_id} not found") from None
    except InvoiceNotPayableError:
        raise SystemExit(f"invoice {invoice_id} is void and cannot be paid") from None
    finally:
        store.close()
    if memo is not None:
        with psycopg.connect(dsn) as conn, conn.transaction():
            conn.execute(
                "UPDATE public.invoices SET memo = %s WHERE invoice_id = %s", (memo, invoice_id)
            )


def _find_invoice(store: Store, invoice_id: str) -> InvoiceRow | None:
    # Invoices are readable only by org members; the order id lookup needs no user.
    return store.invoice_by_order(f"sl-{invoice_id}")


def _void_invoice(invoice_id: str) -> None:
    store = Store(_database_url(), min_size=1, max_size=1)
    try:
        store.void_invoice(invoice_id)
    except LookupError:
        raise SystemExit(f"invoice {invoice_id} not found") from None
    except InvoiceNotPayableError as exc:
        if str(exc) == "invoice_refunded":
            raise _Failed("invoice_not_voidable") from None
        raise SystemExit(f"invoice {invoice_id} is paid and cannot be voided") from None
    finally:
        store.close()


def _refund_invoice(invoice_id: str, reason: str) -> None:
    store = Store(_database_url(), min_size=1, max_size=1)
    try:
        invoice = _find_invoice(store, invoice_id)
        if invoice is None:
            raise _Failed("invoice_not_found")
        if invoice.status != "paid":
            raise _Failed("invoice_not_refundable")
        if invoice.method == "card" and invoice.provider_payment_key:
            provider = build_payment_provider(load_settings())
            try:
                result = provider.cancel(invoice.provider_payment_key, reason)
            except PaymentError as exc:
                raise _Failed(f"refund_failed:{exc.code}", exit_code=2) from None
            if result.status != "refunded":
                raise _Failed("refund_failed:not_canceled", exit_code=2)
            try:
                store.refund_invoice(invoice_id, reason, datetime.now(UTC))
            except (InvoiceNotRefundableError, LookupError, psycopg.Error):
                raise _Failed(
                    f"refund_unrecorded:{invoice_id} payment already canceled at the provider; "
                    "record the refund manually",
                    exit_code=3,
                ) from None
            return
        try:
            store.refund_invoice(invoice_id, reason, datetime.now(UTC))
        except InvoiceNotRefundableError:
            raise _Failed("invoice_not_refundable") from None
    finally:
        store.close()


def _mark_tax_invoice(invoice_id: str, ref: str, issued_on: date | None) -> None:
    issued_at = (
        datetime.now(UTC)
        if issued_on is None
        else datetime(issued_on.year, issued_on.month, issued_on.day, tzinfo=UTC)
    )
    store = Store(_database_url(), min_size=1, max_size=1)
    try:
        store.mark_tax_invoice(invoice_id, ref, issued_at)
    except LookupError:
        raise _Failed("invoice_not_found") from None
    except InvoiceNotPayableError:
        raise _Failed("invoice_not_payable") from None
    finally:
        store.close()


def _charge_due(today: date) -> list[autopay.ChargeOutcome]:
    settings = load_settings()
    store = Store(_database_url(), cipher=build_cipher(settings), min_size=1, max_size=1)
    try:
        return autopay.charge_due(store, build_payment_provider(settings), today)
    finally:
        store.close()


def _stop_autopay(org_id: str) -> None:
    store = Store(_database_url(), min_size=1, max_size=1)
    try:
        store.stop_billing_key(org_id)
    except BillingKeyNotFoundError:
        raise _Failed("billing_key_not_found") from None
    finally:
        store.close()


def _day(value: str) -> date:
    """``YYYY-MM-DD`` -> date."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


def main(argv: list[str] | None = None) -> int:
    # Command output goes to stdout; logs must not mix into it.
    saved = structlog.get_config()
    configure_logging(os.environ.get("SHOPLEGACY_LOG_FORMAT", "console"), stream=sys.stderr)
    try:
        return _main(argv)
    except _Failed as exc:
        print(exc.code, file=sys.stderr)
        return exc.exit_code
    finally:
        structlog.configure(**saved)


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m shoplegacy.maintenance")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    purge = commands.add_parser("purge", help="delete shops past their retention deadline")
    purge.add_argument("--dry-run", action="store_true", help="list without deleting")
    commands.add_parser("generate-key", help="print a new SHOPLEGACY_PII_KEYS key")
    set_plan = commands.add_parser("set-plan", help="change an org's plan (after a contract)")
    set_plan.add_argument("org_id")
    set_plan.add_argument("plan", choices=sorted(PLANS))
    issue = commands.add_parser("issue-invoice", help="issue a monthly invoice to an org")
    issue.add_argument("org_id")
    issue.add_argument("plan", choices=sorted(PLAN_PRICES_KRW))
    issue.add_argument("month", type=_month, help="YYYY-MM")
    issue.add_argument("--amount", type=int, help="KRW (default: the plan's list price)")
    issue.add_argument("--memo")
    paid = commands.add_parser("mark-paid", help="record a payment made outside checkout")
    paid.add_argument("invoice_id")
    paid.add_argument("--method", required=True, choices=["card", "contract"])
    paid.add_argument("--memo")
    void = commands.add_parser("void-invoice", help="cancel an unpaid invoice")
    void.add_argument("invoice_id")
    refund = commands.add_parser("refund-invoice", help="refund a paid invoice in full")
    refund.add_argument("invoice_id")
    refund.add_argument("--reason", required=True)
    tax = commands.add_parser("mark-tax-invoice", help="record a tax invoice number")
    tax.add_argument("invoice_id")
    tax.add_argument("--ref", required=True)
    tax.add_argument("--issued", type=_day, help="YYYY-MM-DD (default: now)")
    charge = commands.add_parser("charge-due", help="charge the billing keys due that day")
    charge.add_argument("--today", type=_day, help="YYYY-MM-DD (default: today)")
    stop = commands.add_parser("stop-autopay", help="stop an org's monthly card charge")
    stop.add_argument("org_id")
    args = parser.parse_args(argv)
    if args.command == "refund-invoice":
        _refund_invoice(args.invoice_id, args.reason)
        print(f"{args.invoice_id} refunded")
        return 0
    if args.command == "mark-tax-invoice":
        _mark_tax_invoice(args.invoice_id, args.ref, args.issued)
        print(f"{args.invoice_id} tax_invoice {args.ref}")
        return 0
    if args.command == "charge-due":
        for outcome in _charge_due(args.today or date.today()):
            print(
                f"{outcome.org_id} {outcome.status} "
                f"{outcome.invoice_id or '-'} {outcome.code or '-'}"
            )
        return 0
    if args.command == "stop-autopay":
        org_id = args.org_id.strip().lower()
        _stop_autopay(org_id)
        print(f"{org_id} autopay stopped")
        return 0
    if args.command == "issue-invoice":
        amount = PLAN_PRICES_KRW[args.plan] if args.amount is None else args.amount
        if amount < 0:
            parser.error("--amount must not be negative")
        org_id = args.org_id.strip().lower()
        print(_issue_invoice(org_id, args.plan, args.month, amount, args.memo))
        return 0
    if args.command == "mark-paid":
        _mark_paid(args.invoice_id, args.method, args.memo)
        print(f"{args.invoice_id} paid")
        return 0
    if args.command == "void-invoice":
        _void_invoice(args.invoice_id)
        print(f"{args.invoice_id} void")
        return 0
    if args.command == "generate-key":
        print(generate_key())
        return 0
    if args.command == "set-plan":
        org_id = args.org_id.strip().lower()
        before = _set_plan(org_id, args.plan)
        print(f"{org_id} {before} -> {args.plan}")
        return 0
    for shop_id in _purge(dry_run=args.dry_run):
        print(shop_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
