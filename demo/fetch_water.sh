#!/bin/sh
# Fetch the latest water test results as CSV on stdout.
echo "site,sampled_on,nitrate_mg_l"
echo "Wabash-03,2026-10-02,4.1"
echo "Wabash-07,2026-10-02,3.8"
echo "error: GET /water/results?page=2 -> HTTP 404 Not Found" >&2
exit 1
