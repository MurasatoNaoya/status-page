# Status Page - Project Rules

## NEVER create synthetic or fake data

This is non-negotiable. Do not insert fabricated records into the database under any circumstances.

- No synthetic "up" check_results
- No assumed statuses for periods when the app wasn't running
- No backfill records with NULL response_time_ms pretending a service was checked
- If there is no real data for a time period, it must show as "no data" (grey) on the UI
- The only valid sources of data are: real health checks executed by the scheduler, and real incidents imported from external status feeds
- "No data" is always preferable to inaccurate data
