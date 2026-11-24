-- Several keys in one round trip, all or nothing.
-- KEYS: the limiter keys, in the order to check them.
-- ARGV[1]: now (ms, or "" for the Redis server clock). Then 4 values per key:
--   limit, param, cost, id
-- Decides for each key in order and stops at the first one that blocks. Commits
-- (spends from every key) only if all of them allowed, so a request that gets a 429
-- from one limit never uses up another.
-- Returns 4 values per key checked: allowed, remaining, retry_after_ms, reset_after_ms.

local now = now_ms(ARGV[1])
local decisions = {}
local all_allowed = true
for i = 1, #KEYS do
  local b = 1 + (i - 1) * 4
  local d = decide(KEYS[i], tonumber(ARGV[b + 1]), tonumber(ARGV[b + 2]),
    tonumber(ARGV[b + 3]), now, ARGV[b + 4])
  decisions[i] = d
  if not d.allowed then
    all_allowed = false
    break
  end
end

if all_allowed then
  for _, d in ipairs(decisions) do
    d.commit()
  end
end

local reply = {}
for _, d in ipairs(decisions) do
  table.insert(reply, d.allowed and 1 or 0)
  table.insert(reply, d.remaining)
  table.insert(reply, fmt(d.retry))
  table.insert(reply, fmt(d.reset))
end
return reply
