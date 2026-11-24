-- Fixed window.
--
-- Key: hash with fields "w" (window number) and "n" (count in that window). One key
-- per limiter key, not one per window, so a script only touches keys it declares,
-- which keeps it valid on Redis Cluster. param: window length in ms.

local function decide(key, limit, window, cost, now)
  local index = math.floor(now / window)
  local state = redis.call("HMGET", key, "w", "n")
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

  local d = { reset = ends_in }
  d.allowed = used + cost <= limit
  if d.allowed then
    d.retry = 0
    d.remaining = limit - used - cost
  else
    d.retry = ends_in
    if cost > limit then d.retry = -1 end
    d.remaining = limit - used
  end
  d.commit = function()
    redis.call("HSET", key, "w", fmt(index), "n", used + cost)
    redis.call("PEXPIRE", key, math.ceil(ends_in) + 1000)
  end
  return d
end
