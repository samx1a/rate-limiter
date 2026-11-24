-- Fixed window: check and update in one atomic step.
--
-- KEYS[1]  hash with fields "w" (window number) and "n" (count in that window)
-- ARGV[1]  limit, ARGV[2] window length (ms), ARGV[3] cost,
-- ARGV[4]  now (ms) or "" for the Redis server clock
--
-- One key per limiter key (not one per window), so a script only ever touches the
-- key it declares, which keeps it valid on Redis Cluster.
-- Returns { allowed, remaining, retry_after_ms, reset_after_ms } (times as strings).

local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local now
if ARGV[4] == "" then
  local t = redis.call("TIME")
  now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
else
  now = tonumber(ARGV[4])
end

local index = math.floor(now / window)

local state = redis.call("HMGET", KEYS[1], "w", "n")
local stored = tonumber(state[1])
local used = 0
if stored ~= nil and stored >= index then
  -- Same window, or the clock stepped back (failover to a replica, an NTP step).
  -- Keep counting in the newest window we have seen; treating the older index as a
  -- fresh window would hand out a whole extra limit.
  index = stored
  used = tonumber(state[2])
end
local ends_in = (index + 1) * window - now

if used + cost > limit then
  local retry = ends_in
  if cost > limit then retry = -1 end
  return { 0, limit - used, string.format("%.17g", retry), string.format("%.17g", ends_in) }
end

used = used + cost
redis.call("HSET", KEYS[1], "w", string.format("%.17g", index), "n", used)
redis.call("PEXPIRE", KEYS[1], math.ceil(ends_in) + 1000)
return { 1, limit - used, "0", string.format("%.17g", ends_in) }
