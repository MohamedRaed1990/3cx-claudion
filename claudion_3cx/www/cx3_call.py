import frappe
from frappe.utils import now_datetime
from urllib.parse import urlencode

def get_context(context):
    context.no_cache = 1

    phone = frappe.form_dict.get("phone") or frappe.form_dict.get("CallerNumber") or ""
    phone_clean = str(phone).strip()

    if not phone_clean:
        context.redirect_url = "/app"
        return

    phone_param = f"%{phone_clean}%"
    current_time = now_datetime().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # 1. حساب عدد المكالمات المباشرة
        previous_calls_count = frappe.db.count("HD Ticket", filters={"custom_phone_number": phone_clean})
        current_call_count = previous_calls_count + 1
        
        # النص المنسق بدون Total Calls
        call_info_str = f"Incoming 3CX Call | Time: {current_time}"

        # 2. تجهيز القاموس
        params = {
            "ticket_type": "Phone Call",
            "custom_phone_number": phone_clean,
            "custom_agency_phone": phone_clean,
            "subject": f"Incoming Call: {phone_clean}",
            "custom_call_count": current_call_count,
            "custom_call_info": call_info_str
        }

        # 3. البحث في جدول الوكالات الرئيسي (Sabre Agency)
        agency_row = frappe.db.sql("""
            SELECT name, agency_name, iata, pcc, email, primary_contact_name
            FROM `tabSabre Agency`
            WHERE phone LIKE %s
            LIMIT 1
        """, (phone_param,), as_dict=True)

        if agency_row:
            row = agency_row[0]
            params.update({
                "custom_agency": row.name or "",
                "agency_name": row.agency_name or "",
                "custom_agency_iata": row.iata or "",
                "pcc": row.pcc or "",
                "custom_agency_pcc": row.pcc or "",
                "custom_agency_email": row.email or "",
                "custom_contact_name": row.primary_contact_name or ""
            })
        else:
            # 4. البحث في جدول جهات الاتصال الفرعي بـ phone أو mobile
            contact_match = frappe.db.sql("""
                SELECT
                    c.parent,
                    c.contact_name,
                    c.pcc AS contact_pcc,
                    c.email AS contact_email,
                    a.agency_name,
                    a.iata,
                    a.pcc AS agency_pcc,
                    a.email AS agency_email,
                    a.primary_contact_name
                FROM `tabSabre Agency Contact` c
                JOIN `tabSabre Agency` a ON a.name = c.parent
                WHERE (c.phone LIKE %s OR c.mobile LIKE %s)
                LIMIT 1
            """, (phone_param, phone_param), as_dict=True)

            if contact_match:
                c = contact_match[0]
                final_contact_name = c.contact_name or c.primary_contact_name or ""
                final_pcc = c.contact_pcc or c.agency_pcc or ""
                final_email = c.contact_email or c.agency_email or ""

                params.update({
                    "custom_agency": c.parent or "",
                    "agency_name": c.agency_name or "",
                    "custom_agency_iata": c.iata or "",
                    "custom_contact_name": final_contact_name,
                    "pcc": final_pcc,
                    "custom_agency_pcc": final_pcc,
                    "custom_agency_email": final_email
                })
            else:
                params.update({
                    "agency_name": f"Unknown - {phone_clean}",
                    "pcc": "Unknown"
                })
        query_string = urlencode(params)
        context.redirect_url = f"/app/hd-ticket/new-hd-ticket-1?{query_string}"

    except Exception as e:
        frappe.log_error(f"3CX Call Redirect Error: {str(e)}")
        context.redirect_url = "/app"
