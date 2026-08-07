"""Seller / company + bank details for Tax Invoice rendering.

Sourced from environment variables with Karnex defaults — not hardcoded in
React. Override any key via INVOICE_* env vars (see get_seller_details /
get_bank_details). Optional logo/seal URLs may point at /api/crm-files/… or
any absolute URL.
"""
from __future__ import annotations

import os


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def get_seller_details() -> dict:
    """Company (seller) block embedded in GET /api/invoices/{id} detail."""
    return {
        "name": _env(
            "INVOICE_SELLER_NAME",
            "KARNEX SOFTWARE SOLUTIONS PRIVATE LIMITED",
        ),
        "tagline": _env("INVOICE_SELLER_TAGLINE", "Excellence In Motion"),
        "address_line1": _env(
            "INVOICE_SELLER_ADDRESS_LINE1",
            "103, Pride Purple Accord, Opp- RMZ Icon, Near Nanakbawdi Flyover, Baner",
        ),
        "address_line2": _env(
            "INVOICE_SELLER_ADDRESS_LINE2",
            "Pune, MH, India - 411045",
        ),
        "city": _env("INVOICE_SELLER_CITY", "Pune"),
        "state": _env("INVOICE_SELLER_STATE", "Maharashtra"),
        "state_code": _env("INVOICE_SELLER_STATE_CODE", "27"),
        "pincode": _env("INVOICE_SELLER_PINCODE", "411045"),
        "country": _env("INVOICE_SELLER_COUNTRY", "India"),
        "phone": _env("INVOICE_SELLER_PHONE", "+91 1234 5678"),
        "email": _env("INVOICE_SELLER_EMAIL", "karnex.singh@karnex.in"),
        "contact_email": _env("INVOICE_SELLER_CONTACT_EMAIL", "info@karnex.in"),
        "website": _env("INVOICE_SELLER_WEBSITE", "www.karnex.in"),
        "gstin": _env("INVOICE_SELLER_GSTIN", "27AAJCK2474BA1ZL"),
        "pan": _env("INVOICE_SELLER_PAN", "AAJCK2474BA"),
        "cin": _env("INVOICE_SELLER_CIN", "U72900RJ2018PTC638288"),
        # Default: admin-dashboard public asset (transparent wordmark above legal name).
        "logo_url": _env(
            "INVOICE_SELLER_LOGO_URL",
            "/admin/assets/karnex-logo-invoice.png",
        ),
        "seal_url": _env(
            "INVOICE_SELLER_SEAL_URL",
            "/admin/assets/karnex-seal-sign.png",
        ),
        "declaration": _env(
            "INVOICE_SELLER_DECLARATION",
            "We declare that this invoice shows the actual price of the "
            "goods described and that all particulars are true and correct.",
        ),
    }


def get_bank_details() -> dict:
    """Receivable bank account shown on the Tax Invoice."""
    return {
        "bank_name": _env("INVOICE_BANK_NAME", "HDFC Bank"),
        "account_name": _env(
            "INVOICE_BANK_ACCOUNT_NAME",
            "KARNEX SOFTWARE SOLUTIONS PRIVATE LIMITED",
        ),
        "account_number": _env("INVOICE_BANK_ACCOUNT_NUMBER", "50200075368143"),
        "ifsc": _env("INVOICE_BANK_IFSC", "HDFC0001784"),
        "branch": _env("INVOICE_BANK_BRANCH", "Baner, Pune"),
        "account_type": _env("INVOICE_BANK_ACCOUNT_TYPE", "Current"),
    }
