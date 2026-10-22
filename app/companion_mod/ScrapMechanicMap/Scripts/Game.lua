dofile "$SURVIVAL_DATA/Scripts/game/SurvivalGame.lua"

Game = class( SurvivalGame )

local MAP_TOOL_UUID = sm.uuid.new( "2a8c60ae-0c42-49a9-a5b0-95d484786af7" )

function Game.server_onCreate( self )
    SurvivalGame.server_onCreate( self )
end

function Game.client_onCreate( self )
    SurvivalGame.client_onCreate( self )
end

-- Survival's first-player cinematic assumes the stock game startup path.  A
-- Custom Game can finish creating the overworld and character without the
-- cinematic's end callback ever restoring the player camera/controls.  Keep
-- the normal Survival loading-screen and tutorial setup, but deliberately do
-- not arm that cinematic for this Custom Game.
function Game.cl_n_onJoined( self, params )
    self.cl.playIntroCinematic = false
end

-- Guard the loading-screen callback as well.  On a freshly generated world
-- the join RPC and loading-screen callback can arrive in either order, so the
-- flag must be cleared immediately before SurvivalGame checks it.
function Game.client_onLoadingScreenLifted( self )
    self.cl.playIntroCinematic = false
    SurvivalGame.client_onLoadingScreenLifted( self )
end

local function giveMapTool( player )
    local inventory = player:getInventory()
    -- In Survival's default limited-inventory mode getHotbar() is forbidden;
    -- getInventory() is the active ten-slot hotbar.  Unlimited/dev games use
    -- the separate hotbar container instead.
    local target = inventory
    if not sm.game.getLimitedInventory() then
        target = player:getHotbar()
    end

    if sm.container.totalQuantity( inventory, MAP_TOOL_UUID ) > 0 or
       ( target ~= inventory and sm.container.totalQuantity( target, MAP_TOOL_UUID ) > 0 ) then
        return
    end

    sm.container.beginTransaction()
    local slot = target:getItem( 2 )
    if slot and slot.uuid:isNil() then
        sm.container.setItem( target, 2, MAP_TOOL_UUID, 1 )
    else
        sm.container.collect( inventory, MAP_TOOL_UUID, 1, false )
    end
    sm.container.endTransaction()
end

function Game.server_onPlayerJoined( self, player, newPlayer )
    SurvivalGame.server_onPlayerJoined( self, player, newPlayer )
    giveMapTool( player )
end
