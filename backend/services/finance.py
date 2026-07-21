"""Finance module service helpers: lookups (404s), FK validation, serialization.

All GST/TDS math and balance updates stay in services/tax.py; this module only
fetches, validates and serializes.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    ContactPerson,
    CreditNote,
    CreditNoteLine,
    Customer,
    CustomerBranch,
    Invoice,
    InvoiceLine,
    InvoicePayment,
    Opportunity,
    POActivityLog,
    POProjectAllocation,
    POStatus,
    Project,
    PurchaseOrder,
    TdsPayment,
    TdsRecord,
    Timesheet,
)
from services import tax


def _ev(value):
    """Enum -> spec string; anything else passes through."""
    return value.value if hasattr(value, "value") else value


def _num(value):
    return float(value) if value is not None else None


def _iso(value):
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def get_po_or_404(db: Session, po_id: int) -> PurchaseOrder:
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


def get_invoice_or_404(db: Session, invoice_id: int) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def get_project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_customer(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=400, detail="Invalid customer_id")
    return customer


def validate_branch(db: Session, customer_id: int, branch_id: int | None) -> None:
    if branch_id is None:
        return
    branch = db.get(CustomerBranch, branch_id)
    if branch is None or branch.customer_id != customer_id:
        raise HTTPException(status_code=400, detail="Branch does not belong to this customer")


def validate_contact(db: Session, customer_id: int, contact_id: int | None) -> None:
    if contact_id is None:
        return
    contact = db.get(ContactPerson, contact_id)
    if contact is None or contact.customer_id != customer_id:
        raise HTTPException(status_code=400, detail="Contact person does not belong to this customer")


def ensure_unique_po_number(db: Session, po_number: str, exclude_id: int | None = None) -> None:
    stmt = select(PurchaseOrder.id).where(PurchaseOrder.po_number == po_number)
    if exclude_id is not None:
        stmt = stmt.where(PurchaseOrder.id != exclude_id)
    if db.execute(stmt).first():
        raise HTTPException(status_code=400, detail=f"PO number '{po_number}' already exists")


def ensure_unique_invoice_number(db: Session, invoice_number: str) -> None:
    if db.execute(select(Invoice.id).where(Invoice.invoice_number == invoice_number)).first():
        raise HTTPException(status_code=400, detail=f"Invoice number '{invoice_number}' already exists")


def apply_gst_split(po: PurchaseOrder, inter_state: bool) -> None:
    """Fill sgst/cgst/igst rate columns from the PO's tax_slab via services.tax."""
    if po.tax_slab is None:
        po.sgst = po.cgst = po.igst = None
        return
    parts = tax.split_gst(po.tax_slab, inter_state=inter_state)
    po.sgst, po.cgst, po.igst = parts["sgst"], parts["cgst"], parts["igst"]


def active_po_allocation_for_project(db: Session, project_id: int) -> POProjectAllocation | None:
    """First (oldest) allocation of an ACTIVE purchase order to this project.

    Used when generating an invoice from a timesheet: if present, the invoice
    is raised against that PO (tax from its slab, balance consumed); if not,
    the invoice is raised without a PO (tax 0), mirroring create_invoice.
    """
    return db.execute(
        select(POProjectAllocation)
        .join(PurchaseOrder, PurchaseOrder.id == POProjectAllocation.po_id)
        .where(
            POProjectAllocation.project_id == project_id,
            PurchaseOrder.status == POStatus.ACTIVE,
        )
        .order_by(POProjectAllocation.id)
    ).scalars().first()


def assert_po_allows_new_drawdown(po: PurchaseOrder | None, *, on_date: date | None = None,
                                  action: str = "this action",
                                  check_balance: bool = True) -> None:
    """Block NEW timesheet submit / invoice generate when the funding PO is
    expired (end_date < on_date) or (optionally) exhausted. Existing drafts
    remain editable.
    """
    if po is None:
        return
    from services.project_employee_billing import PoLine, po_is_blocked
    check_date = on_date or date.today()
    balance = po.balance_value if check_balance else Decimal("1")  # skip balance gate when False
    if not check_balance:
        # Expiry-only gate (timesheet submit): ignore balance/exhaustion.
        if po.end_date is not None and check_date > po.end_date:
            raise HTTPException(
                status_code=400,
                detail=f"PO {po.po_number} expired on {po.end_date.isoformat()}; "
                       f"cannot {action}",
            )
        return
    line = PoLine.of(po.id, balance or 0, po.end_date)
    if po_is_blocked(line, check_date):
        if po.end_date is not None and check_date > po.end_date:
            raise HTTPException(
                status_code=400,
                detail=f"PO {po.po_number} expired on {po.end_date.isoformat()}; "
                       f"cannot {action}",
            )
        raise HTTPException(
            status_code=400,
            detail=f"PO {po.po_number} has insufficient balance; cannot {action}",
        )


def require_live_po_for_project(db: Session, project_id: int, *, on_date: date | None = None,
                                action: str = "this action",
                                check_balance: bool = True) -> POProjectAllocation | None:
    """Return the project's ACTIVE PO allocation after expiry/balance gate, or None."""
    alloc = active_po_allocation_for_project(db, project_id)
    if alloc is None:
        return None
    po = db.get(PurchaseOrder, alloc.po_id)
    assert_po_allows_new_drawdown(po, on_date=on_date, action=action, check_balance=check_balance)
    return alloc


def primary_branch(db: Session, customer_id: int) -> CustomerBranch | None:
    branches = db.execute(
        select(CustomerBranch)
        .where(CustomerBranch.customer_id == customer_id)
        .order_by(CustomerBranch.is_primary.desc(), CustomerBranch.id)
    ).scalars().all()
    return branches[0] if branches else None


def primary_contact(db: Session, customer_id: int, branch_id: int | None = None) -> ContactPerson | None:
    contacts = db.execute(
        select(ContactPerson)
        .where(ContactPerson.customer_id == customer_id, ContactPerson.is_active.is_(True))
        .order_by(ContactPerson.id)
    ).scalars().all()
    if branch_id is not None:
        for c in contacts:
            if c.branch_id == branch_id:
                return c
    return contacts[0] if contacts else None


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def serialize_allocation(alloc: POProjectAllocation, project_name: str | None = None) -> dict:
    return {
        "id": alloc.id,
        "po_id": alloc.po_id,
        "project_id": alloc.project_id,
        "project_name": project_name if project_name is not None else (
            alloc.project.name if alloc.project else None
        ),
        "allocated_amount": _num(alloc.allocated_amount),
        "consumed_amount": _num(alloc.consumed_amount),
        "hsn_sac": alloc.hsn_sac,
    }


def _branch_out(branch: CustomerBranch | None) -> dict | None:
    """Branch block for the PO detail: name + GSTIN/PAN + structured address."""
    if branch is None:
        return None
    return {
        "branch_name": branch.branch_name,
        "gstin": branch.gstin,
        "pan": branch.pan,
        "address": {
            "line1": branch.billing_address,
            "line2": branch.address_line_2,
            "city": branch.city,
            "state": branch.state,
            "postal_code": branch.pincode,
            "country": branch.country or "India",
        },
    }


def _contact_out(contact: ContactPerson | None) -> dict | None:
    if contact is None:
        return None
    return {"name": contact.name, "phone": contact.phone, "email": contact.email}


def po_commercial_block(po: PurchaseOrder) -> dict:
    """Commercial summary of a PO (Tab 10 "Commercial" section).

    GST semantic: the PO's tax_slab/sgst/cgst/igst columns are PERCENTAGES.
    The PO's total_value is treated as the taxable base — exactly how invoices
    raised against the PO treat their sub_total (tax = pct x sub_total / 100,
    grand_total = sub_total + tax; see create_invoice / generate-invoice).
    So here: sub_total = total_value, each *_amount = pct x total_value / 100
    (via tax.gst_component_amounts), tax_amount = their sum, and
    grand_total = total_value + tax_amount ("grand-with-tax").

    Note: PO consumption itself keeps the existing convention — invoice
    grand totals (tax-inclusive) draw down total_value via
    tax.apply_po_consumption. This block is display/summary only.
    """
    amounts = tax.gst_component_amounts(po.total_value, po.sgst, po.cgst, po.igst)
    return {
        "po_type": _ev(po.po_type),
        "po_value": _num(po.total_value),
        "tax_slab": _num(po.tax_slab),
        "sgst_amount": _num(amounts["sgst_amount"]),
        "cgst_amount": _num(amounts["cgst_amount"]),
        "igst_amount": _num(amounts["igst_amount"]),
        "tax_amount": _num(amounts["tax_amount"]),
        "sub_total": _num(po.total_value),
        "grand_total": _num(tax._d(po.total_value) + amounts["tax_amount"]),
    }


def serialize_po(po: PurchaseOrder, detail: bool = False, db: Session | None = None) -> dict:
    data = {
        "id": po.id,
        "po_number": po.po_number,
        "customer_id": po.customer_id,
        "billing_branch_id": po.billing_branch_id,
        "delivery_branch_id": po.delivery_branch_id,
        "received_date": _iso(po.received_date),
        "start_date": _iso(po.start_date),
        "end_date": _iso(po.end_date),
        "attachments": po.attachments or [],
        "contact_person_id": po.contact_person_id,
        "po_type": _ev(po.po_type),
        "payment_terms": po.payment_terms,
        "terms_conditions": po.terms_conditions,
        "tax_slab": _num(po.tax_slab),
        "sgst": _num(po.sgst),
        "cgst": _num(po.cgst),
        "igst": _num(po.igst),
        "total_value": _num(po.total_value),
        "consumed_value": _num(po.consumed_value),
        "balance_value": _num(po.balance_value),
        "status": _ev(po.status),
    }
    if detail:
        data["allocations"] = [serialize_allocation(a) for a in po.allocations]
        data["invoice_count"] = len(po.invoices)
        data["commercial"] = po_commercial_block(po)
        if db is not None:
            billing = db.get(CustomerBranch, po.billing_branch_id) if po.billing_branch_id else None
            delivery = db.get(CustomerBranch, po.delivery_branch_id) if po.delivery_branch_id else None
            contact = db.get(ContactPerson, po.contact_person_id) if po.contact_person_id else None
            data["billing_branch"] = _branch_out(billing)
            data["delivery_branch"] = _branch_out(delivery)
            data["contact"] = _contact_out(contact)
    return data


def serialize_payment(payment: InvoicePayment) -> dict:
    return {
        "id": payment.id,
        "invoice_id": payment.invoice_id,
        "payment_date": _iso(payment.payment_date),
        "amount": _num(payment.amount),
        "payment_mode": payment.payment_mode,
        "reference_number": payment.reference_number,
        "attachment_url": payment.attachment_url,
        "notes": payment.notes,
    }


def serialize_tds_payment(payment: TdsPayment) -> dict:
    return {
        "id": payment.id,
        "tds_record_id": payment.tds_record_id,
        "payment_date": _iso(payment.payment_date),
        "amount": _num(payment.amount),
        "transaction_id": payment.transaction_id,
        "attachment_url": payment.attachment_url,
        "notes": payment.notes,
        "created_at": _iso(payment.created_at),
    }


def serialize_tds(record: TdsRecord, invoice_number: str | None = None,
                  customer_name: str | None = None) -> dict:
    data = {
        "id": record.id,
        "invoice_id": record.invoice_id,
        "tds_amount": _num(record.tds_amount),
        "tds_paid": _num(record.tds_paid),
        "tds_balance": _num(record.tds_balance),
        "tds_status": _ev(record.tds_status),
    }
    if invoice_number is not None:
        data["invoice_number"] = invoice_number
    if customer_name is not None:
        data["customer_name"] = customer_name
    return data


def serialize_invoice_line(line: InvoiceLine) -> dict:
    return {
        "id": line.id,
        "invoice_id": line.invoice_id,
        "s_no": line.s_no,
        "description": line.description,
        "qty": _num(line.qty),
        "rate": _num(line.rate),
        "amount": _num(line.amount),
    }


def serialize_invoice(invoice: Invoice, detail: bool = False, db: Session | None = None) -> dict:
    data = {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "po_id": invoice.po_id,
        "project_id": invoice.project_id,
        "timesheet_id": invoice.timesheet_id,
        "invoice_date": _iso(invoice.invoice_date),
        "due_date": _iso(invoice.due_date),
        "sub_total": _num(invoice.sub_total),
        "tax_amount": _num(invoice.tax_amount),
        "grand_total": _num(invoice.grand_total),
        "payment_status": _ev(invoice.payment_status),
        "paid_amount": _num(invoice.paid_amount),
        "balance_amount": _num(invoice.balance_amount),
        "invoice_pdf_url": invoice.invoice_pdf_url,
    }
    if detail:
        data["po_number"] = invoice.po.po_number if invoice.po else None
        data["project_name"] = invoice.project.name if invoice.project else None
        data["lines"] = [serialize_invoice_line(l) for l in invoice.lines]
        data["payments"] = [serialize_payment(p) for p in invoice.payments]
        data["tds_payments"] = ([serialize_tds_payment(p) for p in invoice.tds_record.payments]
                                if invoice.tds_record else [])
        data["tds_record"] = serialize_tds(invoice.tds_record) if invoice.tds_record else None
        # Flattened TDS view (null-safe) + bank receivables (= open balance).
        tds = invoice.tds_record
        data["tds_amount"] = _num(tds.tds_amount) if tds else None
        data["tds_paid"] = _num(tds.tds_paid) if tds else None
        data["tds_balance"] = _num(tds.tds_balance) if tds else None
        data["tds_status"] = _ev(tds.tds_status) if tds else None
        data["bank_receivables"] = _num(invoice.balance_amount)
        # project_type = the opp_type of the project's source opportunity.
        project_type = None
        if db is not None and invoice.project and invoice.project.opportunity_id:
            opp = db.get(Opportunity, invoice.project.opportunity_id)
            project_type = _ev(opp.opp_type) if opp else None
        data["project_type"] = project_type
    return data


# ---------------------------------------------------------------------------
# Credit notes
# ---------------------------------------------------------------------------

def get_credit_note_or_404(db: Session, credit_note_id: int) -> CreditNote:
    note = db.get(CreditNote, credit_note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Credit note not found")
    return note


def serialize_credit_note_line(line: CreditNoteLine) -> dict:
    return {
        "id": line.id,
        "credit_note_id": line.credit_note_id,
        "s_no": line.s_no,
        "item": line.item,
        "hsn_sac": line.hsn_sac,
        "description": line.description,
        "qty": _num(line.qty),
        "unit_price": _num(line.unit_price),
        "gst_percent": _num(line.gst_percent),
        "line_total": _num(line.line_total),
    }


def serialize_credit_note(note: CreditNote, detail: bool = False,
                          db: Session | None = None) -> dict:
    data = {
        "id": note.id,
        "credit_note_number": note.credit_note_number,
        "invoice_id": note.invoice_id,
        "credit_type": note.credit_type,
        "credit_date": _iso(note.credit_date),
        "reason": note.reason,
        "sub_total": _num(note.sub_total),
        "tax_amount": _num(note.tax_amount),
        "total_amount": _num(note.total_amount),
        "status": note.status,
        "approved_by": note.approved_by,
        "approved_at": _iso(note.approved_at),
        "settled_against": note.settled_against,
        "created_by": note.created_by,
        "created_at": _iso(note.created_at),
    }
    if detail:
        # lines relationship is ordered by s_no on the model.
        data["lines"] = [serialize_credit_note_line(l) for l in note.lines]
        invoice = note.invoice
        data["invoice_number"] = invoice.invoice_number if invoice else None
        if db is not None and invoice is not None:
            # Billing block reused from the invoice's PO billing branch (same
            # _branch_out shape as the PO detail). Falls back to the project's
            # customer name only when the invoice has no PO/billing branch.
            billing = None
            customer_name = None
            po = invoice.po
            if po is not None and po.billing_branch_id:
                billing = db.get(CustomerBranch, po.billing_branch_id)
            customer_id = po.customer_id if po else (
                invoice.project.customer_id if invoice.project else None)
            if customer_id:
                customer = db.get(Customer, customer_id)
                customer_name = ((customer.legal_entity_name or customer.name)
                                 if customer else None)
            data["billing_branch"] = _branch_out(billing)
            data["customer_name"] = customer_name
    return data


# ---------------------------------------------------------------------------
# Tab 10: PO activity log + invoices-under-PO
# ---------------------------------------------------------------------------

def log_po_activity(db: Session, po_id: int, user_id: int, action_type: str,
                    comment: str | None = None, section: str | None = None,
                    record_id: str | int | None = None) -> None:
    """Append one po_activity_log row. Caller commits."""
    db.add(POActivityLog(
        po_id=po_id,
        user_id=user_id,
        action_type=action_type,
        comment=comment,
        section=section,
        record_id=str(record_id) if record_id is not None else None,
    ))


def log_invoice_created_on_po(db: Session, po: PurchaseOrder | None, invoice: Invoice,
                              user_id: int) -> None:
    """Shared hook: record 'invoice raised against this PO' in the PO's activity log.

    Used by both POST /api/invoices and the timesheet generate-invoice path so
    the audit trail is identical regardless of how the invoice was created.
    """
    if po is None:
        return
    log_po_activity(
        db, po.id, user_id, "INVOICE_CREATED",
        comment=f"Invoice {invoice.invoice_number} created against PO {po.po_number} "
                f"for {float(invoice.grand_total or 0):.2f}",
        section="invoices",
        record_id=invoice.id,
    )


def fetch_po_activity_log(db: Session, po_id: int) -> list[dict]:
    """Chronological PO activity log with user names joined from registration_data."""
    rows = db.execute(
        sa.text(
            "SELECT l.id, l.user_id, r.username, r.full_name, l.action_type, l.comment, "
            "       l.section, l.record_id, l.timestamp "
            "FROM po_activity_log l "
            "LEFT JOIN registration_data r ON r.id = l.user_id "
            "WHERE l.po_id = :pid "
            "ORDER BY l.timestamp ASC, l.id ASC"
        ),
        {"pid": po_id},
    ).mappings().all()
    return [
        {
            "id": row["id"],
            "comment": row["comment"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
            "action_type": row["action_type"],
            "user": row["full_name"] or row["username"],
            "user_id": row["user_id"],
            "record_id": row["record_id"],
            "section": row["section"],
        }
        for row in rows
    ]


def po_invoice_rows(db: Session, po: PurchaseOrder) -> list[dict]:
    """Invoices raised against a PO — the Tab 10 'Invoices' grid rows."""
    from services.timesheets import period_label  # local import; no cycle at module load

    customer = db.get(Customer, po.customer_id)
    billing_branch = db.get(CustomerBranch, po.billing_branch_id) if po.billing_branch_id else None
    invoices = db.execute(
        select(Invoice).where(Invoice.po_id == po.id).order_by(Invoice.id)
    ).scalars().all()

    rows: list[dict] = []
    for inv in invoices:
        project = db.get(Project, inv.project_id)
        opp = (db.get(Opportunity, project.opportunity_id)
               if project and project.opportunity_id else None)
        ts = db.get(Timesheet, inv.timesheet_id) if inv.timesheet_id else None
        tds = inv.tds_record
        rows.append({
            "id": inv.id,
            "customer_name": customer.name if customer else None,
            "customer_branch": billing_branch.branch_name if billing_branch else None,
            "timesheet_id": inv.timesheet_id,
            "timesheet_period": period_label(ts.year, ts.month) if ts else None,
            "project_title": project.name if project else None,
            "project_type": _ev(opp.opp_type) if opp else None,
            "invoice_number": inv.invoice_number,
            "invoice_date": _iso(inv.invoice_date),
            "due_date": _iso(inv.due_date),
            "po_date": _iso(po.received_date),
            "sub_total": _num(inv.sub_total),
            "tax_amount": _num(inv.tax_amount),
            "grand_total": _num(inv.grand_total),
            "tds": tds is not None,
            "tds_amount": _num(tds.tds_amount) if tds else None,
            "bank_receivables": _num(inv.balance_amount),
            "payment_status": _ev(inv.payment_status),
            "tds_status": _ev(tds.tds_status) if tds else None,
            "paid_amount": _num(inv.paid_amount),
            "balance_amount": _num(inv.balance_amount),
            "tds_paid": _num(tds.tds_paid) if tds else None,
            "tds_balance": _num(tds.tds_balance) if tds else None,
        })
    return rows
