dofile "$SURVIVAL_DATA/Scripts/game/SurvivalPlayer.lua"
dofile "$CONTENT_DATA/Scripts/MapRuntime.lua"

Player = class( SurvivalPlayer )

function Player.server_onCreate( self )
    SurvivalPlayer.server_onCreate( self )
    self:sv_mapCompanionInit()
end

function Player.server_onRefresh( self )
    SurvivalPlayer.server_onRefresh( self )
    self:sv_mapCompanionInit()
end

function Player.sv_mapCompanionInit( self )
    self.sv.mapCompanionClock = 0
    self.sv.mapCompanionLastCell = nil
    self.sv.saved.mapCompanion = self.sv.saved.mapCompanion or { schema = 1, cells = {} }
    self.sv.saved.mapCompanion.cells = self.sv.saved.mapCompanion.cells or {}
    self.storage:save( self.sv.saved )
end

local function mapCellKey( x, y )
    return tostring( x ) .. ":" .. tostring( y )
end

local function hasTag( tags, wanted )
    for _, tag in ipairs( tags or {} ) do
        if tostring( tag ) == wanted then return true end
    end
    return false
end

local function tagContains( tags, wanted )
    for _, tag in ipairs( tags or {} ) do
        if string.find( string.upper( tostring( tag ) ), wanted, 1, true ) then return true end
    end
    return false
end

local function classifyCell( x, y, world )
    -- getTags operates on the current world and accepts only cell coordinates.
    -- getNodesByTag accepts an explicit world, which is important on the host.
    local tagsOk, tags = pcall( sm.cell.getTags, x, y )
    if not tagsOk then tags = {} end
    if tagContains( tags, "WAREHOUSE" ) then return "warehouse", true end
    if tagContains( tags, "PACKING" ) then return "packing", true end
    if tagContains( tags, "HIDEOUT" ) or tagContains( tags, "MECHANIC" ) or tagContains( tags, "STATION" ) then
        return "station", true
    end
    if hasTag( tags, "STARTAREA" ) then
        -- STARTAREA covers several neighbouring cells.  Only the cell that
        -- actually owns the player spawn is a point of interest; otherwise
        -- every revealed start-area cell gets a large, overlapping icon.
        local spawnOk, spawnNodes = pcall( sm.cell.getNodesByTag, x, y, "PLAYER_SPAWN", world )
        return "start", spawnOk and spawnNodes and #spawnNodes > 0
    end
    if hasTag( tags, "POI" ) then return "poi", true end

    local ok, waterNodes = pcall( sm.cell.getNodesByTag, x, y, "WATER", world )
    if ok and waterNodes and #waterNodes > 0 then return "water", false end
    if tagContains( tags, "ROAD" ) then return "road", false end
    if tagContains( tags, "DESERT" ) or tagContains( tags, "BURN" ) then return "dry", false end
    return "land", false
end

function Player.sv_mapCompanionDiscover( self, centerX, centerY, world )
    local delta = {}
    -- Match the visible minimap range.  The terrain generator can provide
    -- cell tags without forcing those cells to stay loaded, so the first
    -- frame is a useful local map instead of a tiny 3x3 island in an empty
    -- grid.  Subsequent cell crossings reveal only the new leading edge.
    local discoveryRadius = 4
    for dy = -discoveryRadius, discoveryRadius do
        for dx = -discoveryRadius, discoveryRadius do
            local x, y = centerX + dx, centerY + dy
            if x >= -64 and x <= 63 and y >= -48 and y <= 47 then
                local key = mapCellKey( x, y )
                if self.sv.saved.mapCompanion.cells[key] == nil then
                    local ok, kind, poi = pcall( classifyCell, x, y, world )
                    if ok then
                        local cell = { x = x, y = y, kind = kind, poi = poi == true }
                        self.sv.saved.mapCompanion.cells[key] = cell
                        delta[#delta + 1] = cell
                    end
                end
            end
        end
    end

    if #delta > 0 then
        self.storage:save( self.sv.saved )
        self.network:sendToClient( self.player, "cl_n_mapCompanionDelta", { cells = delta } )
    end
end

function Player.server_onFixedUpdate( self, dt )
    SurvivalPlayer.server_onFixedUpdate( self, dt )

    self.sv.mapCompanionClock = ( self.sv.mapCompanionClock or 0 ) + dt
    if self.sv.mapCompanionClock < 0.20 then return end
    self.sv.mapCompanionClock = 0

    local character = self.player:getCharacter()
    if not character or not sm.exists( character ) then return end
    local world = character:getWorld()
    if not world then return end
    local publicData = world.publicData
    if publicData and publicData.type and publicData.type ~= "Overworld" then return end

    local position = character.worldPosition
    local cellX = math.floor( position.x / 64 )
    local cellY = math.floor( position.y / 64 )
    local key = mapCellKey( cellX, cellY )
    if key ~= self.sv.mapCompanionLastCell then
        self.sv.mapCompanionLastCell = key
        self:sv_mapCompanionDiscover( cellX, cellY, world )
    end
end

function Player.sv_n_mapCompanionRequest( self )
    local cells = {}
    for _, cell in pairs( self.sv.saved.mapCompanion.cells ) do cells[#cells + 1] = cell end
    self.network:sendToClient( self.player, "cl_n_mapCompanionSnapshot", { cells = cells } )
end

function Player.client_onCreate( self )
    SurvivalPlayer.client_onCreate( self )
    if self.player == sm.localPlayer.getPlayer() then
        self.cl.mapCompanion = MapRuntime()
        self.cl.mapCompanion:onCreate( self )
        self.network:sendToServer( "sv_n_mapCompanionRequest" )
    end
end

function Player.client_onRefresh( self )
    if self.cl and self.cl.mapCompanion then
        self.cl.mapCompanion:destroy()
        self.cl.mapCompanion = nil
    end
    SurvivalPlayer.client_onRefresh( self )
    if self.player == sm.localPlayer.getPlayer() then
        self.cl.mapCompanion = MapRuntime()
        self.cl.mapCompanion:onCreate( self )
        self.network:sendToServer( "sv_n_mapCompanionRequest" )
    end
end

function Player.client_onUpdate( self, dt )
    BasePlayer.client_onUpdate( self, dt )
    if self.player == sm.localPlayer.getPlayer() and self.cl.mapCompanion then
        self.cl.mapCompanion:update( dt )
    end
end

function Player.cl_n_mapCompanionSnapshot( self, data )
    if self.cl and self.cl.mapCompanion then self.cl.mapCompanion:applySnapshot( data.cells or {} ) end
end

function Player.cl_n_mapCompanionDelta( self, data )
    if self.cl and self.cl.mapCompanion then self.cl.mapCompanion:applyDelta( data.cells or {} ) end
end

function Player.cl_e_mapCompanionPrimary( self )
    if self.cl and self.cl.mapCompanion then self.cl.mapCompanion:toggleFullMap() end
end

function Player.cl_e_mapCompanionSecondary( self )
    if self.cl and self.cl.mapCompanion then self.cl.mapCompanion:cycleZoom() end
end

function Player.cl_mapCompanionClosed( self )
    if self.cl and self.cl.mapCompanion then self.cl.mapCompanion:onFullMapClosed() end
end

function Player.client_onCancel( self )
    if self.cl and self.cl.mapCompanion and self.cl.mapCompanion:isFullMapOpen() then
        self.cl.mapCompanion:closeFullMap()
        return
    end
    SurvivalPlayer.client_onCancel( self )
end

function Player.client_onDestroy( self )
    if self.cl and self.cl.mapCompanion then
        self.cl.mapCompanion:destroy()
        self.cl.mapCompanion = nil
    end
    SurvivalPlayer.client_onDestroy( self )
end
