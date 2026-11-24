-- Sliding window log.
--
-- Key: sorted set, one member per admitted unit of cost, scored by time (ms).
-- param: window length in ms. id: unique per request, because members must be
-- unique or two requests in the same millisecond would collapse into one.

local function decide(key, limit, window, cost, now, id)
  -- Drop entries that slid out: an entry at time t counts while t + window > now.
  -- Pruning expired entries changes no decision, so it's fine even if we end up
  -- not committing.
  redis.call("ZREMRANGEBYSCORE", key, "-inf", now - window)
  local count = redis.call("ZCARD", key)

  local d = {}
  d.allowed = count + cost <= limit
  if d.allowed then
    d.retry = 0
    d.remaining = limit - count - cost
    -- Everything in the set has expired one window after the newest entry (now).
    d.reset = window
  else
    d.remaining = limit - count
    d.retry = -1
    if cost <= limit then
      -- Wait for the oldest (count + cost - limit) units to expire.
      local need = count + cost - limit
      local entry = redis.call("ZRANGE", key, need - 1, need - 1, "WITHSCORES")
      d.retry = tonumber(entry[2]) + window - now
    end
    d.reset = 0
    if count > 0 then
      local newest = redis.call("ZRANGE", key, -1, -1, "WITHSCORES")
      d.reset = tonumber(newest[2]) + window - now
    end
  end
  d.commit = function()
    for i = 1, cost do
      redis.call("ZADD", key, now, id .. ":" .. i)
    end
    redis.call("PEXPIRE", key, math.ceil(window))
  end
  return d
end
