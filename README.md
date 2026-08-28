  
```powershell
# Every night at 03:00 — Windows Task Scheduler
python etl/run_etl.py >> etl_cron.log 2>&1
```

The default (no flags) is incremental: pulls the last 2 days. Re-running for overlapping dates is safe.
