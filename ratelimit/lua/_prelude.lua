-- Shared by every script. Python glues files together before loading them:
--   _prelude.lua + <algorithm>.lua + _single.lua   (one key)
--   _prelude.lua + <algorithm>.lua + _multi.lua    (several keys, all or nothing)
--
-- Each algorithm file defines decide(key, limit, param, cost, now, id), which reads
-- state and returns a table:
--   allowed    boolean
--   remaining  integer, what's left if this request goes through (or now, if blocked)
--   retry      ms until a blocked request could succeed, -1 if it never can
--   reset      ms until the full limit is back
--   commit     function that writes the new state; only called if every key allows
-- decide() never writes. That split is what lets _multi.lua check several keys and
-- only spend from any of them if all of them have room.

local EPSILON = 1e-9

local function now_ms(arg)
  if arg == nil or arg == "" then
    -- One clock for every app server: Redis's. Avoids drift between machines.
    local t = redis.call("TIME")
    return tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
  end
  return tonumber(arg)
end

-- Redis truncates Lua numbers to integers in replies, so times go back as strings.
local function fmt(x)
  return string.format("%.17g", x)
end
