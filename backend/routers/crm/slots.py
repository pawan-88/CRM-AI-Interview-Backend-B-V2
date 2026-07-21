"""Interview slot-booking flow (automated candidate pipeline).

TA side (auth):
  GET/POST /api/requirements/{id}/slots      list upcoming / bulk-create slots
  DELETE   /api/slots/{slot_id}              remove a slot (400 if confirmed bookings)
  GET      /api/requirements/{id}/bookings   list slot bookings for a requirement
  POST     /api/resumes/{id}/send-slot-invite manual (re)send of the booking link

Public (no auth, follows apply.py's token-page pattern):
  GET  /book/{token}                          HTML page: pick + confirm a slot
  POST /api/book/{token}/confirm {slot_id}    confirm; schedules the AI interview

Confirming a slot runs the same pipeline as TA's schedule-ai-interview: the
candidate + profile are found-or-created, a real AI L1 session is scheduled and
the invite link/access key is returned (and emailed/WhatsApped best-effort).
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from models import (
    AiInterviewStatus, InterviewSlot, Requirement, RequirementActivityLog, Resume, SlotBooking,
)
from routers.crm.apply import _base_url, _page
from schemas.common import envelope
from services.ai_interview_bridge import schedule_l1_interview
from services.candidate_comms import interview_link_message, notify_candidate
from services.crm_common import log_activity
from services.notify import notify_role
from services.slot_booking import (
    booking_url_for, find_or_create_candidate_from_resume, get_or_create_profile,
    send_slot_invite, upcoming_open_slots,
)

router = APIRouter(tags=["CRM: Interview Slots"])


def _now():
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _when_text(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return _as_utc(dt).strftime("%A, %d %B %Y at %H:%M UTC")


def _requirement_or_404(db: Session, requirement_id: int) -> Requirement:
    req = db.get(Requirement, requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return req


def _serialize_slot(slot: InterviewSlot) -> dict:
    return {
        "id": slot.id,
        "requirement_id": slot.requirement_id,
        "slot_at": slot.slot_at.isoformat() if slot.slot_at else None,
        "slot_at_text": _when_text(slot.slot_at),
        "capacity": slot.capacity,
        "booked_count": slot.booked_count,
        "available": max(0, (slot.capacity or 0) - (slot.booked_count or 0)),
        "created_by": slot.created_by,
        "created_at": slot.created_at.isoformat() if slot.created_at else None,
    }


def _serialize_booking(booking: SlotBooking, resume: Resume | None,
                       slot: InterviewSlot | None, base_url: str) -> dict:
    return {
        "id": booking.id,
        "token": booking.token,
        "booking_url": booking_url_for(base_url, booking),
        "resume_id": booking.resume_id,
        "candidate_name": resume.candidate_name if resume else None,
        "email": resume.email if resume else None,
        "phone": resume.phone if resume else None,
        "requirement_id": booking.requirement_id,
        "candidate_id": booking.candidate_id,
        "slot_id": booking.slot_id,
        "slot_at": slot.slot_at.isoformat() if slot and slot.slot_at else None,
        "slot_at_text": _when_text(slot.slot_at) if slot else "",
        "status": booking.status,
        "invite_url": booking.invite_url,
        "created_at": booking.created_at.isoformat() if booking.created_at else None,
        "confirmed_at": booking.confirmed_at.isoformat() if booking.confirmed_at else None,
    }


# ------------------------------------------------------------------ TA: slots

class SlotIn(BaseModel):
    slot_at: datetime
    capacity: int = 1


@router.get("/api/requirements/{requirement_id}/slots")
def list_slots(
    requirement_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    _requirement_or_404(db, requirement_id)
    slots = db.execute(
        select(InterviewSlot)
        .where(InterviewSlot.requirement_id == requirement_id, InterviewSlot.slot_at > func.now())
        .order_by(InterviewSlot.slot_at.asc())
    ).scalars().all()
    return envelope([_serialize_slot(s) for s in slots])


@router.post("/api/requirements/{requirement_id}/slots")
def create_slots(
    requirement_id: int,
    slots_in: list[SlotIn],
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    req = _requirement_or_404(db, requirement_id)
    if not slots_in:
        raise HTTPException(status_code=400, detail="Provide at least one slot ({slot_at, capacity?})")
    now = _now()
    created: list[InterviewSlot] = []
    for item in slots_in:
        slot_at = _as_utc(item.slot_at)
        if slot_at <= now:
            raise HTTPException(status_code=400,
                                detail=f"Slot datetime must be in the future: {item.slot_at.isoformat()}")
        if item.capacity < 1:
            raise HTTPException(status_code=400, detail="Slot capacity must be at least 1")
        slot = InterviewSlot(requirement_id=req.id, slot_at=slot_at,
                             capacity=item.capacity, created_by=user.id)
        db.add(slot)
        created.append(slot)
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "SLOTS_CREATED", f"{len(created)} interview slot(s) published")
    db.commit()
    for slot in created:
        db.refresh(slot)
    return envelope([_serialize_slot(s) for s in created],
                    message=f"{len(created)} slot(s) created")


@router.delete("/api/slots/{slot_id}")
def delete_slot(
    slot_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    slot = db.get(InterviewSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found")
    confirmed = db.execute(
        select(func.count()).select_from(SlotBooking)
        .where(SlotBooking.slot_id == slot.id, SlotBooking.status == "Confirmed")
    ).scalar() or 0
    if confirmed:
        raise HTTPException(status_code=400,
                            detail=f"Cannot delete: {confirmed} confirmed booking(s) on this slot")
    db.delete(slot)
    db.commit()
    return envelope({"deleted": True, "slot_id": slot_id}, message="Slot deleted")


@router.get("/api/requirements/{requirement_id}/bookings")
def list_bookings(
    requirement_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    _requirement_or_404(db, requirement_id)
    base = _base_url(request)
    rows = db.execute(
        select(SlotBooking, Resume)
        .join(Resume, Resume.id == SlotBooking.resume_id)
        .where(SlotBooking.requirement_id == requirement_id)
        .order_by(SlotBooking.created_at.desc(), SlotBooking.id.desc())
    ).all()
    out = []
    for booking, resume in rows:
        slot = db.get(InterviewSlot, booking.slot_id) if booking.slot_id else None
        out.append(_serialize_booking(booking, resume, slot, base))
    return envelope(out)


@router.post("/api/resumes/{resume_id}/send-slot-invite")
def send_slot_invite_manual(
    resume_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not (resume.email or "").strip() and not (resume.phone or "").strip():
        raise HTTPException(status_code=400,
                            detail="Resume has no email or phone — add contact info before sending an invite")
    req = _requirement_or_404(db, resume.requirement_id)
    base = _base_url(request)
    booking, results = send_slot_invite(db, resume, req, base)
    channels = [ch for ch, res in results.items() if res.get("sent")]
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "SLOT_INVITE_SENT",
                 f"SLOT_INVITE_SENT ({'/'.join(channels) if channels else 'no channel delivered'}) "
                 f"to {resume.candidate_name} — booking #{booking.id}")
    db.commit()
    db.refresh(booking)
    slot = db.get(InterviewSlot, booking.slot_id) if booking.slot_id else None
    return envelope(
        {"booking": _serialize_booking(booking, resume, slot, base), "notified": results},
        message="Slot invite sent" if channels else "Booking link created (no channel delivered)",
    )


# --------------------------------------------------------------- public: page

def _booking_by_token(db: Session, token: str) -> SlotBooking | None:
    if not token or len(token) > 64:
        return None
    return db.execute(select(SlotBooking).where(SlotBooking.token == token)).scalars().first()


@router.get("/book/{token}", response_class=HTMLResponse)
def booking_page(token: str, db: Session = Depends(get_crm_db)):
    booking = _booking_by_token(db, token)
    if booking is None:
        return _page("Interview booking", '<div class="card"><h1>Link not found</h1>'
                     '<p class="sub">This booking link is invalid or has expired.</p></div>', status=404)
    resume = db.get(Resume, booking.resume_id)
    req = db.get(Requirement, booking.requirement_id)
    title = req.title if req else "Interview"
    name = resume.candidate_name if resume else "Candidate"

    if booking.status == "Confirmed":
        slot = db.get(InterviewSlot, booking.slot_id) if booking.slot_id else None
        when = _when_text(slot.slot_at) if slot else ""
        invite = escape(booking.invite_url or "")
        body = f"""
  <div class="card">
    <h1>{escape(title)}</h1>
    <div class="sub">Hello {escape(name)}, your interview slot is confirmed.</div>
    {f'<div class="msg ok"><strong>When:</strong> {escape(when)}</div>' if when else ''}
    <div class="msg ok">Your AI interview link:<br/>
      <a href="{invite}" style="word-break:break-all">{invite or 'Check your email for the link'}</a></div>
    <div class="note">Your secure access key was sent to your email/WhatsApp along with this link.
    You will need your registered email and the access key to enter the interview.
    If you can't find it, please contact the Karnex recruitment team.</div>
  </div>"""
        return _page(title, body)

    if booking.status in ("Expired", "Cancelled"):
        body = f"""
  <div class="card">
    <h1>{escape(title)}</h1>
    <div class="msg err">This booking link is no longer active ({escape(booking.status.lower())}).
    Our recruitment team will contact you.</div>
  </div>"""
        return _page(title, body)

    slots = upcoming_open_slots(db, booking.requirement_id)
    if not slots:
        body = f"""
  <div class="card">
    <h1>{escape(title)}</h1>
    <div class="sub">Hello {escape(name)},</div>
    <div class="msg err">No interview slots are currently available for this role.
    A recruiter will contact you shortly to arrange a time.</div>
  </div>"""
        return _page(title, body)

    options = "".join(
        f'<label style="display:flex;align-items:center;gap:10px;font-weight:600;padding:12px;'
        f'border:1px solid var(--line);border-radius:12px;margin:10px 0;cursor:pointer;">'
        f'<input type="radio" name="slot_id" value="{slot.id}" style="width:auto" required/>'
        f'{escape(_when_text(slot.slot_at))}'
        f'<span style="margin-left:auto;color:var(--muted);font-size:12px;font-weight:500">'
        f'{max(0, (slot.capacity or 0) - (slot.booked_count or 0))} seat(s) left</span></label>'
        for slot in slots
    )
    body = f"""
  <div class="card">
    <h1>{escape(title)}</h1>
    <div class="sub">Hello {escape(name)}, pick the interview slot that works best for you.
    All times are in UTC.</div>
    <form id="f">
      {options}
      <button id="btn" type="submit">Confirm my slot</button>
      <div id="out"></div>
      <div class="note">After confirming, your AI interview link and secure access key appear here
      and are also sent to your email/WhatsApp.</div>
    </form>
  </div>
  <script>
    var f=document.getElementById('f'),btn=document.getElementById('btn'),out=document.getElementById('out');
    f.addEventListener('submit',async function(e){{
      e.preventDefault(); out.innerHTML=''; btn.disabled=true; btn.textContent='Confirming…';
      try {{
        var sel=f.querySelector('input[name=slot_id]:checked');
        if(!sel){{throw new Error('Please choose a slot');}}
        var res=await fetch('/api/book/{token}/confirm',{{method:'POST',
          headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{slot_id:parseInt(sel.value,10)}})}});
        var data=await res.json();
        if(!res.ok){{throw new Error((data&&data.detail)||'Confirmation failed');}}
        var d=(data&&data.data)||{{}};
        f.style.display='none';
        out.innerHTML='<div class="msg ok"><strong>Slot confirmed!</strong><br/>'
          +'Your AI interview link: <a href="'+d.invite_url+'" style="word-break:break-all">'+d.invite_url+'</a>'
          +(d.access_key?('<br/><strong>Secure access key:</strong> <code>'+d.access_key+'</code>'
          +'<br/><small>Keep this key safe — you need it (with your registered email) to enter the interview.</small>'):'')
          +'</div>';
      }} catch(err) {{
        out.innerHTML='<div class="msg err">'+(err.message||'Something went wrong')+'</div>';
        btn.disabled=false; btn.textContent='Confirm my slot';
      }}
    }});
  </script>"""
    return _page(title, body)


# ------------------------------------------------------------ public: confirm

class ConfirmIn(BaseModel):
    slot_id: int


@router.post("/api/book/{token}/confirm")
def confirm_booking(
    token: str,
    payload: ConfirmIn,
    request: Request,
    db: Session = Depends(get_crm_db),
):
    booking = _booking_by_token(db, token)
    if booking is None:
        raise HTTPException(status_code=404, detail="Invalid or expired booking link")
    if booking.status == "Confirmed":
        raise HTTPException(status_code=400, detail="This booking is already confirmed")
    if booking.status != "Pending":
        raise HTTPException(status_code=400,
                            detail=f"This booking is no longer active ({booking.status})")

    # Lock the slot row so two candidates can't take the last seat concurrently.
    slot = db.execute(
        select(InterviewSlot).where(InterviewSlot.id == payload.slot_id).with_for_update()
    ).scalars().first()
    if slot is None or slot.requirement_id != booking.requirement_id:
        raise HTTPException(status_code=404, detail="Slot not found for this role")
    if _as_utc(slot.slot_at) <= _now():
        raise HTTPException(status_code=400, detail="This slot is in the past — pick another one")
    if (slot.booked_count or 0) >= (slot.capacity or 0):
        raise HTTPException(status_code=409, detail="This slot just filled up — pick another one")

    resume = db.get(Resume, booking.resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Application not found for this booking")
    req = _requirement_or_404(db, booking.requirement_id)

    candidate = find_or_create_candidate_from_resume(db, resume)
    profile = get_or_create_profile(db, candidate, req)

    bridge = schedule_l1_interview(db, candidate, req, profile, resume=resume, scheduled_by=None)
    if not bridge.get("scheduled"):
        raise HTTPException(status_code=502,
                            detail=f"AI interview scheduling failed: {bridge.get('error')}")

    now = _now()
    booking.status = "Confirmed"
    booking.confirmed_at = now
    booking.candidate_id = candidate.id
    booking.slot_id = slot.id
    booking.invite_token = bridge.get("session_ref")
    booking.invite_url = bridge.get("invite_url") or None
    slot.booked_count = (slot.booked_count or 0) + 1
    resume.candidate_id = candidate.id
    resume.ai_interview_status = AiInterviewStatus.SCHEDULED
    resume.ai_interview_scheduled_at = now

    when = _when_text(slot.slot_at)
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, req.created_by,
                 "SLOT_CONFIRMED",
                 f"SLOT_CONFIRMED — {resume.candidate_name} booked {when}; AI L1 scheduled")
    notify_role(db, "TA",
                f"Slot confirmed: {resume.candidate_name}",
                f"{resume.candidate_name} confirmed {when} for '{req.title}' — AI L1 scheduled.",
                f"/admin?view=crm&p=requirements/{req.id}")

    msg = interview_link_message(resume.candidate_name, req.title, when,
                                 bridge.get("invite_url", ""), bridge.get("access_key", ""))
    notified = notify_candidate(resume.email, resume.phone, msg["subject"], msg["text"], msg["html"])

    db.commit()
    return envelope(
        {
            "booking_id": booking.id,
            "status": booking.status,
            "slot_id": slot.id,
            "slot_at": slot.slot_at.isoformat() if slot.slot_at else None,
            "slot_at_text": when,
            "invite_url": bridge.get("invite_url", ""),
            "access_key": bridge.get("access_key", ""),
            "notified": notified,
        },
        message="Slot confirmed — AI interview scheduled",
    )
