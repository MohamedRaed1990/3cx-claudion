import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime, get_url

EXTENSION_AGENT_MAP = {
    "125": "m.raed@cx3.me",
    "127": "ahmed_mansour23@hotmail.com",
}

INCOMING_CALLTYPES = {"inbound", "missed"}
OUTGOING_CALLTYPES = {"outbound", "notanswered"}
NO_ANSWER_CALLTYPES = {"missed", "notanswered"}


@frappe.whitelist(allow_guest=True)
def cx3_ring(phone=None, agent=None, **kwargs):
    """WebHook triggered on 3CX Ringing (LookupByPhone)."""
    frappe.log_error(
        title="CX3 RING DEBUG",
        message=f"phone={phone}, agent={agent}, kwargs={kwargs}"
    )

    if phone:
        frappe.cache().delete_value(f"cx3_note:{phone}")
        frappe.cache().delete_value(f"cx3_subject:{phone}")
        frappe.cache().set_value(f"cx3_active:{phone}", "1", expires_in_sec=3600)

    agent_email = EXTENSION_AGENT_MAP.get(str(agent).strip()) if agent else None

    if agent_email and phone:
        frappe.publish_realtime("exotel_call", {
            "AgentEmail": agent_email,
            "CallFrom": phone,
            "From": phone,
            "EventType": "Dial",
            "Direction": "incoming",
            "Status": "busy",
        })

    contact = frappe.db.get_value(
        "Contact",
        {"phone": ["like", f"%{phone}%"]},
        ["name", "first_name", "last_name"],
        as_dict=True
    ) if phone else None

    return {
        "id": contact.name if contact else "",
        "first_name": contact.first_name if contact else "",
        "last_name": contact.last_name if contact else "",
        "url": get_url(f"/helpdesk/tickets?phone={phone}"),
    }


@frappe.whitelist(allow_guest=True)
def log_call_to_log(phone=None, duration=None, calltype=None, customer_name=None,
                    agent=None, agent_name=None, call_id=None, recording_url=None,
                    rec_file=None, rec_path=None, **kwargs):
    """WebHook triggered by 3CX on Call Completion."""
    frappe.log_error(
        title="CX3 LOG DEBUG - FULL",
        message=f"phone={phone}, agent={agent}, calltype={calltype}, duration={duration}, recording_url={recording_url}, rec_file={rec_file}, rec_path={rec_path}, kwargs={kwargs}"
    )
    # 1. Unique Call ID
    cid = call_id or kwargs.get("callid") or kwargs.get("CallId") or kwargs.get("call_id")
    final_call_id = str(cid).strip() if cid and str(cid).strip() else frappe.generate_hash(length=10)

    # 2. Tracked Extension Check
    current_agent = str(agent).strip() if agent else ""
    if current_agent not in EXTENSION_AGENT_MAP:
        return {
            "status": "ignored",
            "message": f"Call ignored. Extension {current_agent} is not tracked."
        }

    # 3. Short Deduplication Lock
    dedup_key = f"cx3_reportcall_lock:{phone}:{duration}"
    if frappe.cache().get_value(dedup_key):
        return {"status": "ignored", "message": "Duplicate ReportCall webhook ignored"}
    frappe.cache().set_value(dedup_key, "1", expires_in_sec=10)

    # 4. Parse Duration
    total_seconds = 0
    try:
        if duration and ":" in str(duration):
            time_parts = str(duration).split(":")
            if len(time_parts) == 3:
                total_seconds = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
            elif len(time_parts) == 2:
                total_seconds = int(time_parts[0]) * 60 + int(time_parts[1])
        else:
            total_seconds = frappe.utils.cint(duration)
    except Exception:
        total_seconds = 0

    # 5. Agent Email & UI Realtime Popup Broadcast
    agent_email = EXTENSION_AGENT_MAP.get(current_agent)
    if agent_email:
        frappe.publish_realtime("exotel_call", {
            "AgentEmail": agent_email,
            "Direction": "incoming",
            "CallType": "completed",
            "DialCallStatus": "completed",
            "Legs[0][OnCallDuration]": total_seconds,
            "DialCallDuration": total_seconds,
        })

    # 6. Recording Link Setup 

    rec_file = str(rec_file or "").strip()
    rec_path = str(rec_path or "").strip()

    recording_link = None
    if recording_url and str(recording_url).strip() and not str(recording_url).startswith("["):
        recording_link = str(recording_url).strip()
    start_time = add_to_date(now_datetime(), seconds=-total_seconds)
    end_time = now_datetime()
    agent_display = f"{agent_name} ({agent})" if agent and agent_name else (agent if agent else "Main Telecom")

    # 7. Lookups (Customer, Contact, Employee)
    customer_name_val = None
    contact_name_val = None
    employee_name_val = None

    if phone:
        customer_name_val = frappe.db.get_value("Customer", {"mobile_no": ["like", f"%{phone}%"]}, "name")
        contact_name_val = frappe.db.get_value("Contact", {"mobile_no": ["like", f"%{phone}%"]}, "name") or \
                           frappe.db.get_value("Contact", {"phone": ["like", f"%{phone}%"]}, "name")

    if agent_email:
        employee_name_val = frappe.db.get_value("Employee", {"user_id": agent_email}, "name")

    # 8. Direction & Status Calculation
    ctype = str(calltype).lower().strip() if calltype else ""
    if ctype in INCOMING_CALLTYPES:
        actual_type = "Incoming"
        from_val = phone if phone else "Unknown"
        to_val = agent_display
    elif ctype in OUTGOING_CALLTYPES:
        actual_type = "Outgoing"
        from_val = agent_display
        to_val = phone if phone else "Unknown"
    else:
        actual_type = "Incoming" if "in" in ctype else "Outgoing"
        from_val = phone if actual_type == "Incoming" else agent_display
        to_val = agent_display if actual_type == "Incoming" else phone

    call_status = "No Answer" if ctype in NO_ANSWER_CALLTYPES else ("Completed" if total_seconds > 0 else "No Answer")
    summary_text = f"{actual_type} call {'from ' + from_val if actual_type == 'Incoming' else 'to ' + to_val}. Duration: {total_seconds}s."
    # Cache cleanup & Pending Notes
    pending_subject = frappe.cache().get_value(f"cx3_subject:{phone}") or ""
    pending_note = frappe.cache().get_value(f"cx3_note:{phone}")
    if pending_note:
        summary_text += f"\n\nEmployee note during call:\n{pending_note}"
        frappe.cache().delete_value(f"cx3_note:{phone}")

    if pending_subject:
        frappe.cache().delete_value(f"cx3_subject:{phone}")

    frappe.cache().delete_value(f"cx3_active:{phone}")

    # 9. Populate Call Log Document
    doc_payload = {
        "doctype": "Call Log",
        "id": final_call_id,
        "from": from_val,
        "to": to_val,
        "type": actual_type,
        "medium": "3CX",
        "status": call_status,
        "duration": total_seconds,
        "start_time": start_time,
        "end_time": end_time,
        "summary": summary_text,
        "recording_url": None,
        "custom_recording_url": recording_link,
        "employee_user_id": agent_email,
        "call_received_by": employee_name_val,
        "customer": customer_name_val,
    }

    if pending_subject:
        doc_payload["custom_subject"] = pending_subject

    call_log = frappe.get_doc(doc_payload)

    if customer_name_val:
        call_log.append("links", {"link_doctype": "Customer", "link_name": customer_name_val})
    if contact_name_val:
        call_log.append("links", {"link_doctype": "Contact", "link_name": contact_name_val})
    if employee_name_val:
        call_log.append("links", {"link_doctype": "Employee", "link_name": employee_name_val})

    call_log.insert(ignore_permissions=True)
    frappe.db.commit()

    if recording_link:
        redirect_url = get_url(
            f"/api/method/claudion_3cx.claudion_3cx.calls.get_recording?call_log={call_log.name}"
        )
        frappe.db.set_value("Call Log", call_log.name, "recording_url", redirect_url)
        frappe.db.commit()

    return {"status": "success", "id": call_log.name}

@frappe.whitelist(allow_guest=True)
def get_recording(call_log=None):
    """Redirects to the full 3CX recording link stored in custom_recording_url."""
    if not call_log:
        frappe.local.response.http_status_code = 400
        return {"status": "error", "message": "call_log is required"}

    url = frappe.db.get_value("Call Log", call_log, "custom_recording_url")
    if url:
        frappe.local.response.type = "redirect"
        frappe.local.response.location = url
        return

    frappe.local.response.http_status_code = 404
    return {"status": "error", "message": "Recording not found"}

@frappe.whitelist(allow_guest=True)
def cx3_save_note(phone=None, note=None, subject=None):
    if not phone:
        return {"status": "error", "message": "phone is required"}

    recent_call = frappe.get_all(
        "Call Log",
        filters={"from": phone},
        fields=["name"],
        order_by="creation desc",
        limit=1,
    )

    ticket_name = None
    if recent_call:
        links = frappe.get_all(
            "Dynamic Link",
            filters={
                "parenttype": "Call Log",
                "parent": recent_call[0].name,
                "link_doctype": "HD Ticket",
            },
            fields=["link_name"],
            limit=1,
        )
        if links:
            ticket_name = links[0].link_name

    if ticket_name and frappe.db.exists("HD Ticket", ticket_name):
        if note:
            existing_description = frappe.db.get_value("HD Ticket", ticket_name, "description") or ""
            updated_description = existing_description + f"\n\nEmployee note (added after call ended):\n{note}"
            frappe.db.set_value("HD Ticket", ticket_name, "description", updated_description)
        if subject:
            frappe.db.set_value("HD Ticket", ticket_name, "subject", subject)
        frappe.db.commit()
        return {"status": "success", "ticket": ticket_name}

    if note:
        frappe.cache().set_value(f"cx3_note:{phone}", note, expires_in_sec=1800)
    if subject:
        frappe.cache().set_value(f"cx3_subject:{phone}", subject, expires_in_sec=1800)
    return {"status": "success"}


@frappe.whitelist()
def click_to_call(phone_number, customer=None):
    frappe.logger().info(f"Click To Call -> {customer} -> {phone_number}")
    return "success"


@frappe.whitelist(allow_guest=True)
def create_contact_with_phone():
    """Creates a Contact document in Frappe using query/form parameters."""
    try:
        first_name = frappe.form_dict.get('firstname') or frappe.form_dict.get('first_name')
        phone = frappe.form_dict.get('phone') or frappe.form_dict.get('mobile_phone')

        if not first_name or not phone:
            return {
                'status': 'error',
                'message': _('First name and phone number are required.')
            }

        contact = frappe.get_doc({
            'doctype': 'Contact',
            'first_name': first_name,
            'mobile_no': phone
        })
        contact.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            'status': 'success',
            'message': 'Contact created successfully',
            'contact_id': contact.name
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'create_contact_with_phone API Error')
        return {
            'status': 'error',
            'message': f"An error occurred: {str(e)}"
        }
