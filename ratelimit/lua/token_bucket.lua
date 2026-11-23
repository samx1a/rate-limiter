-- Token bucket: check and update in one atomic step.
--
-- Redis runs a script to completion before serving any other command, so no other
-- client can read or write this bucket between our read and our write. That is what
-- makes the limit hold across many app servers.
--
-- KEYS[1]  hash with fields "tokens" and "ts" (last refill, milliseconds)
-- ARGV[1]  capacity (integer)
-- ARGV[2]  refill rate, tokens per second
-- ARGV[3]  cost (integer)
-- ARGV[4]  current time in milliseconds, or "" to use the Redis server clock
--
-- Returns { allowed (0|1), remaining (integer), retry_after_ms, reset_after_ms }.
-- The two times are strings: Redis would truncate Lua numbers to integers.
-- retry_after_ms is "-1" when cost > capacity (it can never succeed).

local capacity = tonumber(ARGV[1])
local rate_per_ms = tonumber(ARGV[2]) / 1000
local cost = tonumber(ARGV[3])
local EPSILON = 1e-9

local now
if ARGV[4] == "" then
  -- One clock for every app server: Redis's. Avoids drift between machines.
  local t = redis.call("TIME")
  now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
else
  now = tonumber(ARGV[4])
end

local state = redis.call("HMGET", KEYS[1], "tokens", "ts")
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = capacity  -- a missing bucket is a full bucket
  ts = now
end

-- Lazy refill. Only move forward in time, so a clock step backwards is harmless.
if now > ts then
  tokens = math.min(capacity, tokens + (now - ts) * rate_per_ms)
  ts = now
end

local allowed = 0
if tokens + EPSILON >= cost then
  tokens = math.max(0, tokens - cost)
  allowed = 1
end

local reset_after = (capacity - tokens) / rate_per_ms
redis.call("HSET", KEYS[1], "tokens", string.format("%.17g", tokens), "ts", string.format("%.17g", ts))
-- Once the bucket would be full again, the key is equivalent to a missing one, so it
-- can expire. This keeps idle keys from piling up in Redis.
redis.call("PEXPIRE", KEYS[1], math.ceil(reset_after) + 1000)

local retry_after = 0
if allowed == 0 then
  if cost > capacity then
    retry_after = -1
  else
    retry_after = (cost - tokens) / rate_per_ms
  end
end

return {
  allowed,
  math.floor(tokens + EPSILON),
  string.format("%.17g", retry_after),
  string.format("%.17g", reset_after),
}
