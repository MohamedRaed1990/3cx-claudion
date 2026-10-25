# -*- coding: utf-8 -*-
import re
import time
import requests
import frappe
from frappe.utils import now_datetime, getdate, time_diff_in_seconds

CACHE_KEY = "cx3_xapi_token"
QUEUE_FIELD = "QueueStatus"

CUSTOM_STATUS_LABELS = {
    "Out of office": "Do Not Disturb",
    "Custom 1": "Break",
    "Custom 2": "Off Duty",
    "Offline Work": "Off Duty",
}

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8,ar;q=0.7",
})


def _get_base_url():
    base_url = frappe.conf.get("cx3_3cx_web_base_url") or frappe.conf.get("cx3_xapi_base_url")
    if not base_url:
        raise Exception("cx3_3cx_web_base_url or cx3_xapi_base_url not set in site_config.json")
    return base_url.rstrip("/")


def _web_login():
    base_url = _get_base_url()
    username = frappe.conf.get("cx3_3cx_web_user")
    password = frappe.conf.get("cx3_3cx_web_pass")

    if not username or not password:
        raise Exception("cx3_3cx_web_user and cx3_3cx_web_pass must be set in site_config.json")

    _session.cookies.clear()

    headers = {
        "Content-Type": "application/json",
        "Origin": base_url,
        "Referer": f"{base_url}/",
        "ngsw-bypass": "bypass",
        "cache-control": "no-store",
        "pragma": "no-cache",
    }

    resp = _session.post(
        f"{base_url}/webclient/api/Login/GetAccessToken",
        json={"ReCaptchaResponse": None, "SecurityCode": "", "Username": str(username), "Password": str(password)},
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    return _session


def get_access_token(force_login=False):
    cached = frappe.cache().get_value(CACHE_KEY)
    if cached and not force_login:
        return cached

    base_url = _get_base_url()
    _web_login()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "application/json",
        "Origin": base_url,
        "Referer": f"{base_url}/",
        "ngsw-bypass": "bypass",
        "cache-control": "no-store",
        "pragma": "no-cache",
    }

    resp = _session.post(
        f"{base_url}/connect/token",
        data={"client_id": "Webclient", "grant_type": "refresh_token"},
        headers=headers,
        timeout=20,
    )
    if resp.status_code != 200:
        _session.cookies.clear()
        _web_login()
        resp = _session.post(
            f"{base_url}/connect/token",
            data={"client_id": "Webclient", "grant_type": "refresh_token"},
            headers=headers,
            timeout=20,
        )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]

    expires_in = int(data.get("expires_in", 3600)) - 90
    frappe.cache().set_value(CACHE_KEY, token, expires_in_sec=max(expires_in, 60))
    return token


def xapi_get(path, params=None):
    base_url = _get_base_url()
    for attempt in (1, 2):
        try:
            headers = {"Authorization": f"Bearer {get_access_token(force_login=(attempt == 2))}"}
            resp = _session.get(f"{base_url}{path}", headers=headers, params=params, timeout=20)
            if resp.status_code == 401 and attempt == 1:
                frappe.cache().delete_value(CACHE_KEY)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401 and attempt == 1:
                frappe.cache().delete_value(CACHE_KEY)
                continue
            raise


def xapi_patch(path, payload):
    base_url = _get_base_url()
    for attempt in (1, 2):
        try:
            headers = {"Authorization": f"Bearer {get_access_token(force_login=(attempt == 2))}"}
            resp = _session.patch(f"{base_url}{path}", headers=headers, json=payload, timeout=20)
            if resp.status_code == 401 and attempt == 1:
                frappe.cache().delete_value(CACHE_KEY)
                continue
            resp.raise_for_status()
            return
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401 and attempt == 1:
                frappe.cache().delete_value(CACHE_KEY)
                continue
            raise


def normalize_queue_status(value):
    """True = داخل الطابور، False = خارج، None = غير معروف."""
    if value is None:
        return None
    v = str(value).strip().lower().replace(" ", "")
    if v in ("loggedin", "login", "true", "1", "userloggedin"):
        return True
    if v in ("loggedout", "logout", "false", "0", "userloggedout"):
        return False
    return None


def get_fresh_queue_status(extension, current_qs, retries=3, delay=2):
    """3CX قد يحدّث QueueStatus بعد CurrentProfileName بثوانٍ.
    نعيد قراءة قيمة الطابور لهذا الـ extension وحده حتى تتغيّر أو نستسلم."""
    for _ in range(retries):
        try:
            data = xapi_get("/xapi/v1/Users", params={
                "$filter": f"Number eq '{extension}'",
                "$select": "QueueStatus",
            }).get("value", [])
        except Exception:
            return current_qs
        if data:
            fresh = normalize_queue_status(data[0].get("QueueStatus"))
            if fresh is not None and fresh != current_qs:
                return fresh
        time.sleep(delay)
    return current_qs


def get_busy_extensions():
    """Returns the set of extension numbers currently on a live call."""
    try:
        calls = xapi_get("/xapi/v1/ActiveCalls").get("value", [])
    except Exception as e:
        frappe.log_error(f"Error fetching active calls: {e}", "CX3 Sync")
        return set()

    busy = set()
    for call in calls:
        for side in ("Caller", "Callee"):
            party = (call.get(side) or "").strip()
            match = re.match(r"^(\d+)\b", party)
            if match:
                busy.add(match.group(1))
    return busy


def get_queue_members():
    """مجموعة أرقام الـ extensions الأعضاء في أي queue."""
    members = set()
    try:
        for q in xapi_get("/xapi/v1/Queues").get("value", []):
            data = xapi_get(f"/xapi/v1/Queues({q['Id']})", params={"$expand": "Agents"})
            for a in data.get("Agents", []):
                if a.get("Number"):
                    members.add(str(a["Number"]))
    except Exception as e:
        frappe.log_error(f"Error fetching queue members: {e}", "CX3 Sync")
    return members


def sync_extension_status():
    users = xapi_get(
        "/xapi/v1/Users",
        params={
            "$select": "Number,FirstName,LastName,EmailAddress,CurrentProfileName,IsRegistered," + QUEUE_FIELD
        },
    ).get("value", [])

    busy_extensions = get_busy_extensions()
    queue_members = get_queue_members()
    synced_at = now_datetime()

    for user in users:
        extension = str(user.get("Number") or "").strip()
        if not extension:
            continue

        first_name = (user.get("FirstName") or "").strip()
        last_name = (user.get("LastName") or "").strip()
        display_name = f"{first_name} {last_name}".strip()
        email = (user.get("EmailAddress") or "").strip() or None

        raw_profile = user.get("CurrentProfileName") or ""
        is_registered = bool(user.get("IsRegistered"))

        if extension in busy_extensions:
            profile_name = "On a Call"
        else:
            profile_name = CUSTOM_STATUS_LABELS.get(raw_profile, raw_profile)

        employee = None
        if email:
            employee = frappe.db.get_value("Employee", {"user_id": email}, "name")

        qs = normalize_queue_status(user.get(QUEUE_FIELD))

        frappe.db.savepoint("cx3_ext")
        try:
            existing = frappe.db.exists("CX3 Extension Status", extension)
            if existing:
                doc = frappe.get_doc("CX3 Extension Status", existing)
                old_profile = doc.current_profile
            else:
                doc = frappe.new_doc("CX3 Extension Status")
                doc.extension = extension
                old_profile = None

            if (
                old_profile
                and old_profile != profile_name
                and extension in queue_members
                and qs is not None
            ):
                qs = get_fresh_queue_status(extension, qs)

            target_queue_status = {True: "Logged In", False: "Logged Out"}.get(qs) if extension in queue_members else None

            doc.display_name = display_name
            doc.agent_email = email
            doc.employee = employee
            doc.current_profile = profile_name
            doc.is_registered = is_registered
            doc.last_synced = synced_at
            doc.queue_status = target_queue_status

            doc.save(ignore_permissions=True)

        except frappe.QueryDeadlockError:
            frappe.db.rollback(save_point="cx3_ext")
            frappe.db.set_value(
                "CX3 Extension Status",
                extension,
                {
                    "current_profile": profile_name,
                    "queue_status": target_queue_status,
                    "last_synced": synced_at,
                },
                update_modified=True,
            )

        except Exception:
            frappe.db.rollback(save_point="cx3_ext")
            frappe.log_error(
                f"Sync failed for extension {extension}\n{frappe.get_traceback()}",
                "CX3 Sync",
            )

    frappe.db.commit()


def on_extension_status_update(doc, method=None):
    old_doc = doc.get_doc_before_save()
    old_profile = old_doc.current_profile if old_doc else None
    old_queue_status = old_doc.queue_status if old_doc else None

    profile_changed = (not old_doc) or old_profile != doc.current_profile
    queue_changed = (not old_doc) or old_queue_status != doc.queue_status

    if not profile_changed and not queue_changed:
        return

    now = doc.flags.get("status_time") or now_datetime()
    _auto_queue_sync(doc, profile_changed, old_profile)

    active_logs = frappe.get_all(
        "CX3 Status Log",
        filters={"extension": doc.extension, "is_active": 1},
        fields=["name"],
    )

    if profile_changed or not active_logs:
        _close_logs("CX3 Status Log", doc.extension, now)

        if doc.current_profile:
            new_log = frappe.new_doc("CX3 Status Log")
            new_log.extension = doc.extension
            new_log.employee = doc.employee
            new_log.employee_name = doc.employee_name or doc.display_name
            new_log.status = doc.current_profile
            new_log.queue_status = doc.queue_status
            new_log.from_time = now
            new_log.log_date = getdate(now)
            new_log.is_active = 1
            new_log.save(ignore_permissions=True)

    if queue_changed:
        _roll_queue_log(doc, now)


def get_user_id_by_extension(extension):
    data = xapi_get(
        "/xapi/v1/Users",
        params={"$filter": f"Number eq '{extension}'", "$select": "Id,Number"},
    ).get("value", [])
    return data[0]["Id"] if data else None


def _auto_queue_sync(doc, profile_changed, old_profile):
    if not profile_changed or doc.queue_status is None:
        return

    target = None
    if doc.current_profile == "Off Duty" and doc.queue_status == "Logged In":
        target = "LoggedOut"
    elif doc.current_profile == "Available" and doc.queue_status == "Logged Out":
        target = "LoggedIn"

    if not target:
        return

    try:
        user_id = get_user_id_by_extension(doc.extension)
        if user_id:
            xapi_patch(f"/xapi/v1/Users({user_id})", {"QueueStatus": target})
            frappe.enqueue(
                "claudion_3cx.claudion_3cx.status_sync.verify_queue_status",
                extension=doc.extension,
                expected=(target == "LoggedIn"),
                enqueue_after_commit=True,
            )
    except Exception:
        frappe.log_error(
            f"Auto queue sync failed for {doc.extension}\n{frappe.get_traceback()}",
            "CX3 Auto Queue Login",
        )


def verify_queue_status(extension, expected, retries=15, delay=2):
    for _ in range(retries):
        try:
            data = xapi_get("/xapi/v1/Users", params={
                "$filter": f"Number eq '{extension}'",
                "$select": "QueueStatus",
            }).get("value", [])
        except Exception:
            return
        if data:
            qs = normalize_queue_status(data[0].get("QueueStatus"))
            if qs is expected:
                target = {True: "Logged In", False: "Logged Out"}.get(qs)
                current = frappe.db.get_value("CX3 Extension Status", extension, "queue_status")
                if current != target:
                    doc = frappe.get_doc("CX3 Extension Status", extension)
                    doc.queue_status = target
                    doc.save(ignore_permissions=True)
            return
        time.sleep(delay)


def _close_logs(doctype, extension, now):
    for name in frappe.get_all(doctype, filters={"extension": extension, "is_active": 1}, pluck="name"):
        log = frappe.get_doc(doctype, name)
        log.to_time = now
        log.is_active = 0
        if log.from_time:
            log.duration = time_diff_in_seconds(now, log.from_time)
        log.save(ignore_permissions=True)


def _roll_queue_log(doc, now):
    _close_logs("CX3 Queue Log", doc.extension, now)

    if not doc.queue_status:
        return

    new_log = frappe.new_doc("CX3 Queue Log")
    new_log.extension = doc.extension
    new_log.employee = doc.employee
    new_log.employee_name = doc.employee_name or doc.display_name
    new_log.queue_status = doc.queue_status
    new_log.from_time = now
    new_log.log_date = getdate(now)
    new_log.is_active = 1
    new_log.save(ignore_permissions=True)


OFF_DUTY_RAW = {"Custom 2", "Offline Work"}
TARGET_RAW = "Custom 2"


def _apply_off_duty_locally(number, cutoff, queue_logged_out):
    if not frappe.db.exists("CX3 Extension Status", number):
        return
    doc = frappe.get_doc("CX3 Extension Status", number)
    doc.current_profile = "Off Duty"
    if queue_logged_out and doc.queue_status is not None:
        doc.queue_status = "Logged Out"
    doc.flags.status_time = cutoff
    doc.save(ignore_permissions=True)


def set_all_off_duty(dry_run=False):
    cutoff = now_datetime().replace(hour=23, minute=59, second=59, microsecond=0)
    users = xapi_get(
        "/xapi/v1/Users",
        params={"$select": "Id,Number,CurrentProfileName,QueueStatus,Enabled,AIAgent"},
    ).get("value", [])

    queue_members = get_queue_members()

    changed, already, failed = [], [], []
    for u in users:
        if not u.get("Enabled") or u.get("AIAgent"):
            continue

        number = str(u.get("Number") or "").strip()

        payload = {}
        if u.get("CurrentProfileName") not in OFF_DUTY_RAW:
            payload["CurrentProfileName"] = TARGET_RAW

        if number in queue_members and normalize_queue_status(u.get("QueueStatus")) is True:
            payload["QueueStatus"] = "LoggedOut"

        if not payload:
            already.append(number)
            continue

        if dry_run:
            changed.append({number: payload})
            continue

        try:
            xapi_patch(f"/xapi/v1/Users({u['Id']})", payload)
            _apply_off_duty_locally(number, cutoff, "QueueStatus" in payload)
            changed.append({number: payload})
        except Exception:
            failed.append(number)
            frappe.log_error(
                f"Off Duty failed for {number}\n{frappe.get_traceback()}",
                "CX3 Night Status",
            )

    return {"changed": changed, "already": already, "failed": failed}
