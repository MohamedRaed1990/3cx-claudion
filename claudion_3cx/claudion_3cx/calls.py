import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime, get_url

SUPERVISOR_ROLE = "3CX Supervisor"

INCOMING_CALLTYPES = {"inbound", "missed"}
OUTGOING_CALLTYPES = {"outbound", "notanswered"}
NO_ANSWER_CALLTYPES = {"missed", "notanswered"}


# ===================== Helpers =====================

def get_agent_user(extension):
    """يجلب مستخدم Frappe بناءً على رقم الإكستنشن في 3CX (بدون خريطة يدوية)."""
    if not extension:
        return None
    ext = str(extension).strip()
    if not ext:
        return None
    return frappe.db.get_value(
        "User",
        {"custom_3cx_extension": ext, "enabled": 1},
        ["name", "full_name", "custom_3cx_queue"],
        as_dict=True,
    )


def is_supervisor(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return SUPERVISOR_ROLE in frappe.get_roles(user)


def setup_custom_fields():
    """Run once: bench execute claudion_3cx.claudion_3cx.calls.setup_custom_fields"""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    if not frappe.db.exists("Role", SUPERVISOR_ROLE):
        frappe.get_doc({
            "doctype": "Role",
            "role_name": SUPERVISOR_ROLE,
            "desk_access": 1,
        }).insert(ignore_permissions=True)

    create_custom_fields({
        "User": [
            {"fieldname": "custom_3cx_extension", "label": "3CX Extension",
             "fieldtype": "Data", "insert_after": "mobile_no"},
            {"fieldname": "custom_3cx_queue", "label": "3CX Queue",
             "fieldtype": "Data", "insert_after": "custom_3cx_extension"},
        ],
        "Call Log": [
            {"fieldname": "custom_3cx_queue", "label": "3CX Queue",
             "fieldtype": "Data", "insert_after": "medium"},
        ],
    }, update=True)

    frappe.db.commit()
    return "Custom fields and role created successfully."


# ===================== 3CX Webhooks =====================

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

    agent_user = get_agent_user(agent)

    if agent_user and phone:
        frappe.publish_realtime("exotel_call", {
            "AgentEmail": agent_user.name,
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
                    rec_file=None, rec_path=None, queue=None, **kwargs):
    """WebHook triggered by 3CX on Call Completion."""
    frappe.log_error(
        title="CX3 LOG DEBUG - FULL",
        message=f"phone={phone}, agent={agent}, calltype={calltype}, duration={duration}, "
                f"queue={queue}, recording_url={recording_url}, rec_file={rec_file}, "
                f"rec_path={rec_path}, kwargs={kwargs}"
    )

    # 0. Call Type — نحتاجه مبكراً
    ctype = str(calltype).lower().strip() if calltype else ""

    # 1. Unique Call ID
    cid = call_id or kwargs.get("callid") or kwargs.get("CallId") or kwargs.get("call_id")
    final_call_id = str(cid).strip() if cid and str(cid).strip() else frappe.generate_hash(length=10)

    # 2. Queue من 3CX إن كانت ممرَّرة (غالباً فارغة في نسختك)
    queue_name = str(queue).strip() if queue and str(queue).strip() else None

    # 3. Agent Lookup (Queue Member) — ديناميكي من قاعدة البيانات
    current_agent = str(agent).strip() if agent else ""
    agent_user = get_agent_user(current_agent)
    is_unassigned_incoming = False

    if not agent_user:
        if ctype in INCOMING_CALLTYPES:
            # وارد بلا موظف مطابق (طابور لم يرد أحد) → يُسجَّل دائماً
            # 3CX ترسل رقم الطابور في [Agent] عندما لا يرد أحد
            is_unassigned_incoming = True
            agent_email = None
            if not queue_name and current_agent:
                queue_name = current_agent
        else:
            return {
                "status": "ignored",
                "message": f"Call ignored. Extension {current_agent} is not assigned to any enabled Frappe user.",
            }
    else:
        agent_email = agent_user.name
        if not queue_name:
            queue_name = agent_user.custom_3cx_queue or None

    # 4. Short Deduplication Lock
    dedup_key = f"cx3_reportcall_lock:{phone}:{duration}"
    if frappe.cache().get_value(dedup_key):
        return {"status": "ignored", "message": "Duplicate ReportCall webhook ignored"}
    frappe.cache().set_value(dedup_key, "1", expires_in_sec=10)

    # 5. Parse Duration
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

    # 6. UI Realtime Popup (فقط عند وجود موظف)
    if agent_email:
        frappe.publish_realtime("exotel_call", {
            "AgentEmail": agent_email,
            "Direction": "incoming",
            "CallType": "completed",
            "DialCallStatus": "completed",
            "Legs[0][OnCallDuration]": total_seconds,
            "DialCallDuration": total_seconds,
        })

    # 7. Recording Link Setup
    rec_file = str(rec_file or "").strip()
    rec_path = str(rec_path or "").strip()

    recording_link = None
    if recording_url and str(recording_url).strip() and not str(recording_url).startswith("["):
        recording_link = str(recording_url).strip()
    start_time = add_to_date(now_datetime(), seconds=-total_seconds)
    end_time = now_datetime()

    # اسم المعروض
    if agent_user:
        if agent_name and current_agent:
            agent_display = f"{agent_name} ({current_agent})"
        elif agent_user.full_name:
            agent_display = f"{agent_user.full_name} ({current_agent})"
        else:
            agent_display = current_agent or "Main Telecom"
    else:
        if queue_name and queue_name != current_agent:
            agent_display = queue_name
        elif current_agent:
            agent_display = f"Queue {current_agent}"
        else:
            agent_display = "Unknown Queue"

    # 8. Lookups (Customer, Contact, Employee)
    customer_name_val = None
    contact_name_val = None
    employee_name_val = None

    if phone:
        customer_name_val = frappe.db.get_value("Customer", {"mobile_no": ["like", f"%{phone}%"]}, "name")
        contact_name_val = frappe.db.get_value("Contact", {"mobile_no": ["like", f"%{phone}%"]}, "name") or \
                           frappe.db.get_value("Contact", {"phone": ["like", f"%{phone}%"]}, "name")

    if agent_email:
        employee_name_val = frappe.db.get_value("Employee", {"user_id": agent_email}, "name")

    # 9. Direction & Status Calculation
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

    if is_unassigned_incoming:
        # طابور بلا إجابة: duration>0 هو وقت الرنين وليس وقت الرد
        call_status = "No Answer"
    else:
        call_status = "No Answer" if ctype in NO_ANSWER_CALLTYPES else ("Completed" if total_seconds > 0 else "No Answer")

    summary_text = f"{actual_type} call {'from ' + from_val if actual_type == 'Incoming' else 'to ' + to_val}. Duration: {total_seconds}s."
    if queue_name:
        summary_text += f" Queue: {queue_name}."

    # Cache cleanup & Pending Notes
    pending_subject = frappe.cache().get_value(f"cx3_subject:{phone}") or ""
    pending_note = frappe.cache().get_value(f"cx3_note:{phone}")
    if pending_note:
        summary_text += f"\n\nEmployee note during call:\n{pending_note}"
        frappe.cache().delete_value(f"cx3_note:{phone}")

    if pending_subject:
        frappe.cache().delete_value(f"cx3_subject:{phone}")

    frappe.cache().delete_value(f"cx3_active:{phone}")

    # 10. Populate Call Log Document
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
        "custom_3cx_queue": queue_name,
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


# ===================== Permissions =====================

def has_call_log_permission(doc=None, ptype=None, user=None, **kwargs):
    """يرى المستخدم العادي مكالماته + المكالمات الفائتة على طابوره، والمشرف يرى الكل."""
    user = user or frappe.session.user
    if is_supervisor(user):
        return True

    # مستوى الـ Doctype (فتح الصفحة): مسموح، والصفوف تُفلتر في list view
    if not doc:
        return True

    # مكالمة مستخدم نفسه
    if getattr(doc, "employee_user_id", None) == user:
        return True

    # مكالمة فائتة على الطابور: تظهر لأعضاء نفس الطابور
    if not getattr(doc, "employee_user_id", None):
        user_queue = frappe.db.get_value("User", user, "custom_3cx_queue")
        if user_queue and getattr(doc, "custom_3cx_queue", None) == user_queue:
            return True

    # عبر ربط الـ Employee المرتبط باليوزر
    for link in doc.get("links") or []:
        if link.link_doctype == "Employee":
            emp_user = frappe.db.get_value("Employee", link.link_name, "user_id")
            if emp_user == user:
                return True

    return False


def call_log_query_conditions(user=None):
    """فلترة قائمة Call Log:
    - المشرف: الكل
    - المستخدم العادي: مكالماته + المكالمات الفائتة (بلا موظف) على طابوره
    """
    user = user or frappe.session.user
    if is_supervisor(user):
        return ""

    user_queue = frappe.db.get_value("User", user, "custom_3cx_queue")
    cond = f"`tabCall Log`.employee_user_id = {frappe.db.escape(user)}"
    if user_queue:
        cond += (f" OR (`tabCall Log`.employee_user_id IS NULL"
                 f" AND `tabCall Log`.custom_3cx_queue = {frappe.db.escape(user_queue)})")
    return f"({cond})"
