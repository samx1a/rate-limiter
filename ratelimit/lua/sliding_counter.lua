-- Sliding window counter: check and update in one atomic step.
--
-- KEYS[1]  hash with fields "w" (current window number), "c" (its count) and
--          "p" (the previous window's count)
-- ARGV[1]  limit, ARGV[2] window length (ms), ARGV[3] cost,
-- ARGV[4]  now (ms) or "" for the Redis server clock
--
-- estimate = p * (share of the previous window still in view) + c
-- Same math as ratelimit/algorithms/sliding_counter.py; the contract tests check that
-- the two agree.
-- Returns { allowed, remaining, retry_after_ms, reset_after_ms } (times as strings).

local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local EPSILON = 1e-9

local now
if ARGV[4] == "" then
  local t = redis.call("TIME")
  now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
else
  now = tonumber(ARGV[4])
end

local index = math.floor(now / window)
local state = redis.call("HMGET", KEYS[1], "w", "c", "p")
local stored = tonumber(state[1])
local current, previous = 0, 0
if stored ~= nil then
  if stored >= index then
    -- Same window, or the clock stepped back: keep the newest window we have seen.
    index = stored
    current, previous = tonumber(state[2]), tonumber(state[3])
  elseif stored == index - 1 then
    previous = tonumber(state[2])
  end
end

local start = index * window
local weight = math.max(0, math.min(1, 1 - (now - start) / window))
local estimate = previous * weight + current

local function reset_after(c, p)
  if c > 0 then return start + 2 * window - now end
  if p > 0 then return start + window - now end
  return 0
end

local function fmt(x) return string.format("%.17g", x) end

if estimate + cost > limit + EPSILON then
  local retry = -1
  if cost <= limit then
    local room = limit - current - cost
    if room >= 0 then
      retry = math.max(0, start + (1 - room / previous) * window - now)
    else
      retry = start + window + math.max(0, 1 - (limit - cost) / current) * window - now
    end
  end
  local remaining = math.max(0, math.floor(limit - estimate + EPSILON))
  return { 0, remaining, fmt(retry), fmt(reset_after(current, previous)) }
end

current = current + cost
redis.call("HSET", KEYS[1], "w", fmt(index), "c", current, "p", previous)
-- The counts matter until the end of the next window; after that the key is the same
-- as a missing one.
redis.call("PEXPIRE", KEYS[1], math.ceil(start + 2 * window - now) + 1000)
local remaining = math.max(0, math.floor(limit - estimate - cost + EPSILON))
return { 1, remaining, "0", fmt(reset_after(current, previous)) }
