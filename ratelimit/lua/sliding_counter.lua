-- Sliding window counter.
--
-- Key: hash with fields "w" (current window number), "c" (its count) and "p" (the
-- previous window's count). param: window length in ms.
-- estimate = p * (share of the previous window still in view) + c
-- Same math as ratelimit/algorithms/sliding_counter.py; the contract tests check that
-- the two agree.

local function decide(key, limit, window, cost, now)
  local index = math.floor(now / window)
  local state = redis.call("HMGET", key, "w", "c", "p")
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

  local d = {}
  d.allowed = estimate + cost <= limit + EPSILON
  if d.allowed then
    d.retry = 0
    d.remaining = math.max(0, math.floor(limit - estimate - cost + EPSILON))
    d.reset = reset_after(current + cost, previous)
  else
    d.remaining = math.max(0, math.floor(limit - estimate + EPSILON))
    d.reset = reset_after(current, previous)
    d.retry = -1
    if cost <= limit then
      local room = limit - current - cost
      if room >= 0 then
        d.retry = math.max(0, start + (1 - room / previous) * window - now)
      else
        d.retry = start + window + math.max(0, 1 - (limit - cost) / current) * window - now
      end
    end
  end
  d.commit = function()
    redis.call("HSET", key, "w", fmt(index), "c", current + cost, "p", previous)
    -- The counts matter until the end of the next window; after that the key is the
    -- same as a missing one.
    redis.call("PEXPIRE", key, math.ceil(start + 2 * window - now) + 1000)
  end
  return d
end
