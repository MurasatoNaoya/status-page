# Gunicorn configuration for status-page
#
# Single worker avoids duplicate APScheduler instances.
# Threads handle concurrent requests (plenty for a status page).

bind = "0.0.0.0:5050"
workers = 1
threads = 2
timeout = 30
accesslog = "-"
errorlog = "-"
# Single-worker setup: keep recycling infrequent to avoid avoidable brief
# unavailability during worker restarts.
max_requests = 10000
max_requests_jitter = 500
