# DECISIONS.md

1. **Indexes.**
   Created a unique index on `employees.emp_code` to enforce uniqueness across employee creations. Created a compound unique index on `attendance_logs (emp_code, date)` to ensure an employee can only punch in once per day and prevent race conditions. Created a compound index on `(date, emp_code)` to enable fast sorting and pagination for attendance listings without in-memory sorting. Considered an index on `status` alone, but rejected it because low-cardinality fields provide poor selectivity compared to compound date-based indexes.

2. **Punch-in race.**
   If two identical punch-in requests arrive simultaneously for the same employee and date, the application relies on the database unique index on `(emp_code, date)`. The first request inserts successfully and returns HTTP 201. The second request hits a duplicate key collision at the database engine level; the app rejects it and returns HTTP 409 Conflict.

3. **Ties.**
   The leaderboard uses standard competition ranking (1, 2, 2, 4) where tied scores share the same rank and subsequent ranks are skipped. Filtering by `rank <= limit` occurs after ranking. If two employees tie at rank 10, both are included in the response, even if the total count exceeds `limit`, with ties ordered by `emp_code` ascending.

4. **Headcount.**
   Department headcount queries the `employees` collection directly, filtering by `joined_on <= end_of_month`. It counts all matching employees in the department regardless of whether any attendance documents exist for them in `attendance_logs`.

5. **One thing you would change if this had to serve 100x the data.**
   For 100x data, pre-aggregate daily and monthly statistics using scheduled background jobs or a materialized summary collection, rather than aggregating raw attendance logs on the fly during user API requests.
