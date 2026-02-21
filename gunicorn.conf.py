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
