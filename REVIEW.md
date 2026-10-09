# REVIEW.md

| 1 | `compute_late_minutes` (lines 33-37) | Grace period logic bug: converts to minutes first and checks `minutes > 10`. At 10m 1s late (601s), `int(601/60)` is 10, so `10 > 10` is False, returning 0 instead of 10. | Punch-in at 09:40:01 for a 09:30 shift returns 0 late minutes instead of 10. | Replaced with `diff = (punch_in - start).total_seconds()` and checked `diff > 600`, returning `int(diff // 60)`. |

| 2 | `compute_work_hours` (lines 40-42) | Uses Python's built-in `round()`, which applies banker's rounding (round-half-to-even) instead of the required half-up rounding (Rule R4). | Input producing 2.125 hours rounds down to 2.12 instead of 2.13. | Replaced with Python's `Decimal` and `ROUND_HALF_UP` to round to 2 decimal places. |

| 3 | `POST /employees` (lines 78-86) | Missing validation for shift timings; allowed identical `shift_start` and `shift_end`. | Creating an employee with `shift_start="09:30"` and `shift_end="09:30"` returned 201 Created. | Added validation `if body.shift_start == body.shift_end:` raising HTTP 422. |

| 4 | `GET /employees` (lines 88-96) | Off-by-one pagination bug (`skip = page * page_size` skipped the first 20 records on page 1), and `total` ignored the department query filter. | Page 1 skipped records 0–19. Querying by department returned filtered items but total showed the count of the whole database. | Changed skip to `(page - 1) * page_size` and changed count to `count_documents(q)`. |

| 5 | `POST /attendance/punch-in` (lines 99-122) | Missing 404 check on employee lookup; also allowed any arbitrary status string instead of valid punch-in statuses. | Punching in with a non-existent `emp_code` caused an unhandled `TypeError` (HTTP 500) when accessing `emp["shift_start"]`. Sending invalid status was accepted. | Added `if not emp: raise HTTPException(404, "employee not found")` and validated status to only allow `PRESENT`, `WFH`, `ON_DUTY`. |
