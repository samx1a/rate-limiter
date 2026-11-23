-- Sliding window log: check and update in one atomic step.
--
-- KEYS[1]  sorted set: one member per admitted unit of cost, scored by time (ms)
-- ARGV[1]  limit, ARGV[2] window length (ms), ARGV[3] cost,
-- ARGV[4]  now (ms) or "" for the Redis server clock,
-- ARGV[5]  a unique id for this request (members must be unique, or two requests in
--          the same millisecond would collapse into one)
--
-- Returns { allowed, remaining, retry_after_ms, reset_after_ms } (times as strings).

local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local id = ARGV[5]

local now
if ARGV[4] == "" then
  local t = redis.call("TIME")
  now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
else
  now = tonumber(ARGV[4])
end

-- Drop entries that slid out: an entry at time t counts while t + window > now.
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now - window)
local count = redis.call("ZCARD", KEYS[1])

if count + cost <= limit then
  for i = 1, cost do
    redis.call("ZADD", KEYS[1], now, id .. ":" .. i)
  end
  -- Everything in the set has expired one window after the newest entry (now).
  redis.call("PEXPIRE", KEYS[1], math.ceil(window))
  return { 1, limit - count - cost, "0", string.format("%.17g", window) }
end

local retry = -1
if cost <= limit then
  -- Wait for the oldest (count + cost - limit) units to expire.
  local need = count + cost - limit
  local entry = redis.call("ZRANGE", KEYS[1], need - 1, need - 1, "WITHSCORES")
  retry = tonumber(entry[2]) + window - now
end
local reset = 0
if count > 0 then
  local newest = redis.call("ZRANGE", KEYS[1], -1, -1, "WITHSCORES")
  reset = tonumber(newest[2]) + window - now
end
return { 0, limit - count, string.format("%.17g", retry), string.format("%.17g", reset) }
