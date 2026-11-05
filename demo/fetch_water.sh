#!/bin/sh
# Demo prop for Fledge: a "data pull" that half works. It prints a partial table, then fails the way a
# flaky government site does. No network access; everything here is canned text.
echo "site,sampled_on,nitrate_mg_l"
echo "Wabash-03,2026-10-02,4.1"
echo "Wabash-07,2026-10-02,3.8"
echo "error: GET dnr.example.invalid/water/results?page=2 -> HTTP 404 Not Found" >&2
exit 1
