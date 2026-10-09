"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import calendar
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    diff = (punch_in - start).total_seconds()
    return int(diff // 60) if diff > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = Decimal(str((punch_out - punch_in).total_seconds() / 3600))
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")  
    if body.status not in ("PRESENT", "WFH", "ON_DUTY"):
        raise HTTPException(422, "invalid status for punch-in")
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)]
    )
    if not record:
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    diff_secs = (ts - record["punch_in"]).total_seconds()
    if diff_secs <= 0 or diff_secs > 86400:
        raise HTTPException(422, "invalid punch-out time")

    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_end"], record["date"])
    half_day = work_hours < 4.50

    db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }}
    )
    record["punch_out"] = ts
    record["work_hours"] = work_hours
    record["overtime_minutes"] = overtime
    record["half_day"] = half_day
    record.pop("_id", None)
    return record


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "record not found")

    new_status = body.status if body.status is not None else record.get("status")

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot provide punch times for ABSENT or LEAVE")
        new_in = None
        new_out = None
    else:
        new_in = datetime.fromtimestamp(body.punch_in / 1000) if body.punch_in is not None else record.get("punch_in")
        new_out = datetime.fromtimestamp(body.punch_out / 1000) if body.punch_out is not None else record.get("punch_out")
        if not new_in:
            raise HTTPException(422, "presence status requires punch_in")

    if new_in and new_out:
        diff_secs = (new_out - new_in).total_seconds()
        if diff_secs <= 0 or diff_secs > 86400:
            raise HTTPException(422, "invalid punch times")

    new_late = compute_late_minutes(new_in, emp["shift_start"]) if new_in else 0
    new_hours = compute_work_hours(new_in, new_out) if (new_in and new_out) else None
    new_ot = compute_overtime(new_out, emp["shift_end"], date) if (new_in and new_out) else 0
    new_half = (new_hours < 4.50) if new_hours is not None else False

    changes = {}
    fields = [
        ("status", record.get("status"), new_status),
        ("punch_in", record.get("punch_in"), new_in),
        ("punch_out", record.get("punch_out"), new_out),
        ("late_minutes", record.get("late_minutes"), new_late),
        ("work_hours", record.get("work_hours"), new_hours),
        ("overtime_minutes", record.get("overtime_minutes"), new_ot),
        ("half_day", record.get("half_day"), new_half),
    ]

    for key, old_v, new_v in fields:
        if old_v != new_v:
            changes[key] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(422, "no changes detected")

    history_entry = {
        "at": datetime.now(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "late_minutes": new_late,
                "work_hours": new_hours,
                "overtime_minutes": new_ot,
                "half_day": new_half,
            },
            "$push": {"history": history_entry},
        },
    )

    return db.attendance_logs.find_one({"_id": record["_id"]}, {"_id": 0})

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = max(datetime(year, m, 1).date(), datetime.fromisoformat(emp["joined_on"]).date())
    end_date = datetime(year, m, last_day).date()

    working_days = 0
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)

    logs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$regex": f"^{month}"}}))

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late = 0
    total_ot = 0

    for log in logs:
        is_weekday = datetime.fromisoformat(log["date"]).weekday() < 5
        st = log.get("status")
        if is_weekday and st in ("PRESENT", "WFH", "ON_DUTY"):
            present_days += 0.5 if log.get("half_day") else 1.0
        if st == "LEAVE":
            leave_days += 1
        late_m = log.get("late_minutes", 0)
        if late_m > 0:
            late_count += 1
            total_late += late_m
        total_ot += log.get("overtime_minutes", 0)

    att_pct = None
    if working_days > 0:
        pct = Decimal(str(present_days / working_days * 100))
        att_pct = float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    end_date_str = f"{month}-{last_day:02d}"

    emp_match = {"joined_on": {"$lte": end_date_str}}
    if department:
        emp_match["department"] = department

    departments = sorted(db.employees.distinct("department", emp_match))
    items = []

    for dept in departments:
        dept_emps = list(db.employees.find({"department": dept, "joined_on": {"$lte": end_date_str}}))
        headcount = len(dept_emps)
        if headcount == 0:
            continue

        emp_codes = [e["emp_code"] for e in dept_emps]
        logs = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$regex": f"^{month}"}}))

        present_days = 0.0
        work_hours_list = []
        late_count = 0
        total_late = 0
        leave_count = 0
        on_duty_count = 0

        for log in logs:
            is_weekday = datetime.fromisoformat(log["date"]).weekday() < 5
            st = log.get("status")
            if is_weekday and st in ("PRESENT", "WFH", "ON_DUTY"):
                present_days += 0.5 if log.get("half_day") else 1.0
            if st in ("PRESENT", "WFH", "ON_DUTY") and log.get("work_hours") is not None:
                work_hours_list.append(log["work_hours"])
            if log.get("late_minutes", 0) > 0:
                late_count += 1
                total_late += log["late_minutes"]
            if st == "LEAVE":
                leave_count += 1
            if st == "ON_DUTY":
                on_duty_count += 1

        avg_work = None
        if work_hours_list:
            avg_val = Decimal(str(sum(work_hours_list) / len(work_hours_list)))
            avg_work = float(avg_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        items.append({
            "department": dept,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    emp_query = {}
    if department:
        emp_query["department"] = department
    employees = {e["emp_code"]: e for e in db.employees.find(emp_query)}

    logs = list(db.attendance_logs.find({"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}))
    stats = {}
    for log in logs:
        code = log["emp_code"]
        if code not in employees:
            continue
        if code not in stats:
            stats[code] = {"total_late": 0, "late_count": 0}
        stats[code]["total_late"] += log.get("late_minutes", 0)
        stats[code]["late_count"] += 1

    sorted_emps = sorted(
        stats.items(),
        key=lambda item: (-item[1]["total_late"], item[0])
    )

    ranked_items = []
    current_rank = 1
    for i, (code, data) in enumerate(sorted_emps):
        if i > 0 and data["total_late"] < sorted_emps[i - 1][1]["total_late"]:
            current_rank = i + 1
        emp = employees[code]
        ranked_items.append({
            "rank": current_rank,
            "emp_code": code,
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": data["total_late"],
            "late_count": data["late_count"],
        })

    items = [item for item in ranked_items if item["rank"] <= limit]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "department not found")

    d_from = datetime.fromisoformat(from_date).date()
    d_to = datetime.fromisoformat(to_date).date()

    if d_to < d_from or (d_to - d_from).days > 92:
        raise HTTPException(422, "invalid date range")

    all_dept_emps = list(db.employees.find({"department": department}))
    dept_emp_codes = {e["emp_code"]: e["joined_on"] for e in all_dept_emps}

    logs = list(db.attendance_logs.find({
        "emp_code": {"$in": list(dept_emp_codes.keys())},
        "date": {"$gte": from_date, "$lte": to_date}
    }))
    logs_by_date = {}
    for log in logs:
        d = log["date"]
        logs_by_date.setdefault(d, []).append(log)

    items = []
    rates_history = []
    cur = d_from
    while cur <= d_to:
        d_str = cur.isoformat()
        is_working = cur.weekday() < 5
        headcount = sum(1 for e in all_dept_emps if e["joined_on"] <= d_str)

        day_logs = logs_by_date.get(d_str, [])
        present_count = 0.0
        late_count = 0
        for log in day_logs:
            if log.get("status") in ("PRESENT", "WFH", "ON_DUTY"):
                present_count += 0.5 if log.get("half_day") else 1.0
            if log.get("late_minutes", 0) > 0:
                late_count += 1

        att_rate = None
        if is_working and headcount > 0:
            rate_val = Decimal(str(present_count / headcount))
            att_rate = float(rate_val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        rates_history.append(att_rate)
        recent_window = [r for r in rates_history[-7:] if r is not None]
        m_avg = None
        if recent_window:
            avg_val = Decimal(str(sum(recent_window) / len(recent_window)))
            m_avg = float(avg_val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        items.append({
            "date": d_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        plan = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "missing parameters")
        plan = db.attendance_logs.find({"emp_code": emp_code, "date": {"$regex": f"^{month}"}}).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "missing month")
        plan = db.employees.find({"joined_on": {"$lte": f"{month}-31"}}).explain()
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "missing month")
        plan = db.attendance_logs.find({"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "missing parameters")
        plan = db.attendance_logs.find({"date": {"$gte": from_date, "$lte": to_date}}).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    else:
        raise HTTPException(422, "unknown endpoint")