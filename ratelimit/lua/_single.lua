-- One key. KEYS[1] is the limiter key.
-- ARGV: limit, param (window ms or refill rate per second), cost,
--       now (ms, or "" for the Redis server clock), id (sliding window only)
-- Returns { allowed (0|1), remaining, retry_after_ms, reset_after_ms }.

local d = decide(KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]),
  now_ms(ARGV[4]), ARGV[5])
if d.allowed then
  d.commit()
end
return { d.allowed and 1 or 0, d.remaining, fmt(d.retry), fmt(d.reset) }
