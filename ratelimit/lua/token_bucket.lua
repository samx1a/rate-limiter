-- Token bucket with lazy refill.
--
-- Key: hash with fields "tokens" and "ts" (last refill, ms). param: refill rate in
-- tokens per second. A missing bucket is a full bucket, so once a bucket would be
-- full again the key can expire; that keeps idle keys from piling up in Redis.

local function decide(key, capacity, rate, cost, now)
  local rate_per_ms = rate / 1000
  local state = redis.call("HMGET", key, "tokens", "ts")
  local tokens = tonumber(state[1])
  local ts = tonumber(state[2])
  if tokens == nil or ts == nil then
    tokens = capacity
    ts = now
  end

  -- Only move forward in time, so a clock step backwards is harmless.
  if now > ts then
    tokens = math.min(capacity, tokens + (now - ts) * rate_per_ms)
    ts = now
  end

  local d = {}
  d.allowed = tokens + EPSILON >= cost
  if d.allowed then
    tokens = math.max(0, tokens - cost)
    d.retry = 0
  elseif cost > capacity then
    d.retry = -1
  else
    d.retry = (cost - tokens) / rate_per_ms
  end
  d.remaining = math.floor(tokens + EPSILON)
  d.reset = (capacity - tokens) / rate_per_ms
  d.commit = function()
    redis.call("HSET", key, "tokens", fmt(tokens), "ts", fmt(ts))
    redis.call("PEXPIRE", key, math.ceil(d.reset) + 1000)
  end
  return d
end
