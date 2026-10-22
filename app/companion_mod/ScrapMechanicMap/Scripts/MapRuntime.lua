MapRuntime = class()

local LOGBOOK_UUID = sm.uuid.new( "a6113860-f4d4-42df-b8f9-129efdbaf777" )
local WORLD_MIN_X, WORLD_MAX_X = -64, 63
local WORLD_MIN_Y, WORLD_MAX_Y = -48, 47
local WORLD_WIDTH = WORLD_MAX_X - WORLD_MIN_X + 1
local WORLD_HEIGHT = WORLD_MAX_Y - WORLD_MIN_Y + 1
local MINI_RADIUS = 4
local MINI_CELL = 20
local MINI_MAP_SIZE = ( MINI_RADIUS * 2 + 1 ) * MINI_CELL

-- JSON GUI textures are resolved through the game's GUI texture registry when
-- given a bare name.  Workshop-owned files must use CONTENT_DATA explicitly.
-- Runtime-loaded JSON GUI images are resolved by DirectoryManager, where a
-- Custom Game is mounted under its stable content id (not $CONTENT_DATA).
-- The id is the package's localId and remains the same after Workshop upload.
local GUI_TEXTURE_ROOT = "$CONTENT_9bf45fa0-6dc8-4f21-9eb7-cdb06f4312c0/Gui/MapCompanion/"

local function guiTexture( fileName )
    return GUI_TEXTURE_ROOT .. fileName
end

local CELL_TEXTURE = {
    land = guiTexture( "cell_land.png" ),
    road = guiTexture( "cell_road.png" ),
    water = guiTexture( "cell_water.png" ),
    dry = guiTexture( "cell_dry.png" ),
    start = guiTexture( "cell_start.png" ),
    poi = guiTexture( "cell_poi.png" ),
    station = guiTexture( "cell_station.png" ),
    packing = guiTexture( "cell_packing.png" ),
    warehouse = guiTexture( "cell_warehouse.png" ),
    unknown = guiTexture( "cell_unknown.png" )
}

local POI_TEXTURE = {
    start = guiTexture( "poi_start.png" ),
    poi = guiTexture( "poi_generic.png" ),
    station = guiTexture( "poi_station.png" ),
    packing = guiTexture( "poi_packing.png" ),
    warehouse = guiTexture( "poi_warehouse.png" )
}

local function cellKey( x, y )
    return tostring( x ) .. ":" .. tostring( y )
end

local function image( name, texture, x, y, width, height, visible )
    return {
        Type = "ImageBox", Skin = "ImageBox", Name = name,
        ImageTexture = texture, x = x, y = y, width = width, height = height,
        NeedMouse = false, NeedKey = false, Visible = visible ~= false, Childs = {}
    }
end

local function label( name, caption, x, y, width, height, align, font )
    return {
        Type = "TextBox", Skin = "TextBox", Name = name, Caption = caption,
        FontName = font or "SM_HeaderTiny", TextAlign = align or "Center",
        TextColour = "1 1 1 1", x = x, y = y, width = width, height = height,
        NeedMouse = false, NeedKey = false, Visible = true, Childs = {}
    }
end

local function rootGui( name, anchor, x, y, width, height, children, onClose )
    local root = {
        Type = "Widget", Skin = "PanelEmpty", Name = name,
        Anchor = anchor, LayoutRule = "Absolute", x = x, y = y,
        width = width, height = height, NeedMouse = false, NeedKey = false,
        Visible = true, Childs = children
    }
    if onClose then root.onClose = onClose end
    return root
end

local function directionIndex( direction )
    local angle = math.atan2( direction.x, direction.y )
    return math.floor( ( angle / ( math.pi * 2 ) ) * 8 + 0.5 ) % 8
end

function MapRuntime.onCreate( self, owner )
    self.owner = owner
    self.cells = {}
    self.hudGui = sm.jsonGui.createGui( { isHud = true, isInteractive = false, needsCursor = false, layer = "Middle" } )
    self.fullGui = sm.jsonGui.createGui( { isInteractive = false, needsCursor = false, hidesHotbar = true, handleKeySetup = "Logbook", name = "Map Companion" } )
    self.fullOpen = false
    self.zoom = 1
    self.lastCellKey = nil
    self.lastDirection = nil
    self.hudDirty = true
    self.directionWidgets = {}
end

function MapRuntime.destroy( self )
    if self.hudGui and sm.exists( self.hudGui ) then self.hudGui:close() end
    if self.fullGui and sm.exists( self.fullGui ) then self.fullGui:close() end
    self.hudGui = nil
    self.fullGui = nil
    self.owner = nil
end

function MapRuntime.applySnapshot( self, cells )
    self.cells = {}
    self:applyDelta( cells )
end

function MapRuntime.applyDelta( self, cells )
    for _, cell in ipairs( cells ) do self.cells[cellKey( cell.x, cell.y )] = cell end
    self.hudDirty = true
    if self.fullOpen then self:renderFullMap() end
end

function MapRuntime.getLocalCharacter( self )
    local player = sm.localPlayer.getPlayer()
    if not player then return nil end
    local character = player:getCharacter()
    if character and sm.exists( character ) then return character end
    return nil
end

function MapRuntime.renderHud( self, character )
    if not self.hudGui or not sm.exists( self.hudGui ) then return end
    local position = character.worldPosition
    local centerX = math.floor( position.x / 64 )
    local centerY = math.floor( position.y / 64 )
    local children = {}

    children[#children + 1] = image( "Background", guiTexture( "panel.png" ), 0, 0, 232, 248 )
    children[#children + 1] = label( "Title", "EXPLORATION MAP", 18, 8, 196, 22, "Center", "SM_HeaderSmall" )
    local mapX, mapY = 26, 36
    children[#children + 1] = image( "MapBackground", CELL_TEXTURE.unknown, mapX - 3, mapY - 3, MINI_MAP_SIZE + 6, MINI_MAP_SIZE + 6 )

    for dy = MINI_RADIUS, -MINI_RADIUS, -1 do
        for dx = -MINI_RADIUS, MINI_RADIUS do
            local x, y = centerX + dx, centerY + dy
            local cell = self.cells[cellKey( x, y )]
            local texture = CELL_TEXTURE[cell and cell.kind or "unknown"] or CELL_TEXTURE.land
            local px = mapX + ( dx + MINI_RADIUS ) * MINI_CELL
            local py = mapY + ( MINI_RADIUS - dy ) * MINI_CELL
            children[#children + 1] = image( "MiniCell_" .. tostring( x ) .. "_" .. tostring( y ), texture, px, py, MINI_CELL, MINI_CELL )
            if cell and cell.poi and POI_TEXTURE[cell.kind] then
                children[#children + 1] = image( "MiniPoi_" .. tostring( x ) .. "_" .. tostring( y ), POI_TEXTURE[cell.kind], px - 2, py - 2, 16, 16 )
            end
        end
    end

    local arrowX = mapX + MINI_RADIUS * MINI_CELL - 6
    local arrowY = mapY + MINI_RADIUS * MINI_CELL - 6
    for i = 0, 7 do
        children[#children + 1] = image( "PlayerDirection" .. tostring( i ), guiTexture( "direction_" .. tostring( i ) .. ".png" ), arrowX, arrowY, 24, 24, i == 0 )
    end
    children[#children + 1] = label( "North", "N", 100, 32, 32, 16 )
    children[#children + 1] = label( "Help", "PRIMARY: MAP  |  SECONDARY: RANGE", 12, 222, 208, 18, "Center" )

    self.hudGui:render( rootGui( "MapCompanionHud", "Top Left", 18, 18, 232, 248, children ) )
    self.directionWidgets = {}
    for i = 0, 7 do self.directionWidgets[i] = self.hudGui:getWidget( "PlayerDirection" .. tostring( i ) ) end
    self.lastDirection = nil
    self.hudDirty = false
end

function MapRuntime.setDirection( self, index )
    if index == self.lastDirection then return end
    self.lastDirection = index
    for i = 0, 7 do
        local widget = self.directionWidgets[i]
        if widget then widget:setVisible( i == index ) end
    end
end

function MapRuntime.renderFullMap( self )
    local character = self:getLocalCharacter()
    if not character or not self.fullGui or not sm.exists( self.fullGui ) then return end
    local position = character.worldPosition
    local playerCellX = math.floor( position.x / 64 )
    local playerCellY = math.floor( position.y / 64 )
    local cellSize = 6 * self.zoom
    local mapWidth, mapHeight = WORLD_WIDTH * cellSize, WORLD_HEIGHT * cellSize
    local viewportX, viewportY, viewportWidth, viewportHeight = 190, 72, 900, 576
    local centerWorldX = ( playerCellX - WORLD_MIN_X + 0.5 ) * cellSize
    local centerWorldY = ( WORLD_MAX_Y - playerCellY + 0.5 ) * cellSize
    local offsetX = viewportX + viewportWidth * 0.5 - centerWorldX
    local offsetY = viewportY + viewportHeight * 0.5 - centerWorldY
    if self.zoom == 1 then
        offsetX = viewportX + ( viewportWidth - mapWidth ) * 0.5
        offsetY = viewportY + ( viewportHeight - mapHeight ) * 0.5
    end

    local children = {}
    children[#children + 1] = image( "FullBackground", guiTexture( "full_panel.png" ), 0, 0, 1280, 720 )
    children[#children + 1] = label( "FullTitle", "SURVIVAL EXPLORATION MAP", 28, 18, 500, 38, "Left", "SM_HeaderLarge_Wide" )
    children[#children + 1] = label( "FullHelp", "PRIMARY: CLOSE   SECONDARY: ZOOM   ESC: CLOSE", 738, 24, 500, 24, "Right", "SM_HeaderSmall" )
    children[#children + 1] = image( "WorldBackground", CELL_TEXTURE.unknown, viewportX, viewportY, viewportWidth, viewportHeight )

    for _, cell in pairs( self.cells ) do
        local px = offsetX + ( cell.x - WORLD_MIN_X ) * cellSize
        local py = offsetY + ( WORLD_MAX_Y - cell.y ) * cellSize
        if px + cellSize >= viewportX and px <= viewportX + viewportWidth and py + cellSize >= viewportY and py <= viewportY + viewportHeight then
            local texture = CELL_TEXTURE[cell.kind] or CELL_TEXTURE.land
            children[#children + 1] = image( "FullCell_" .. tostring( cell.x ) .. "_" .. tostring( cell.y ), texture, px, py, cellSize + 1, cellSize + 1 )
            if cell.poi and POI_TEXTURE[cell.kind] then
                local size = math.max( 18, cellSize * 2 )
                children[#children + 1] = image( "FullPoi_" .. tostring( cell.x ) .. "_" .. tostring( cell.y ), POI_TEXTURE[cell.kind], px + cellSize * 0.5 - size * 0.5, py + cellSize * 0.5 - size * 0.5, size, size )
            end
        end
    end

    local playerX = offsetX + ( position.x / 64 - WORLD_MIN_X ) * cellSize
    local playerY = offsetY + ( WORLD_MAX_Y - position.y / 64 ) * cellSize
    local direction = directionIndex( character.direction )
    children[#children + 1] = image( "FullPlayer", guiTexture( "direction_" .. tostring( direction ) .. ".png" ), playerX - 14, playerY - 14, 28, 28 )
    children[#children + 1] = label( "Zoom", "ZOOM " .. tostring( self.zoom ) .. "x", 1100, 660, 140, 26, "Right", "SM_HeaderSmall" )
    children[#children + 1] = label( "Legend", "GREEN  EXPLORED     BLUE  WATER     PURPLE  POI     ORANGE  WAREHOUSE", 190, 660, 780, 26, "Left" )

    self.fullGui:render( rootGui( "MapCompanionFull", "Top Left", 0, 0, 1280, 720, children, "cl_mapCompanionClosed" ) )
end

function MapRuntime.toggleFullMap( self )
    if self.fullOpen then self:closeFullMap() else self:openFullMap() end
end

function MapRuntime.openFullMap( self )
    if self.fullOpen then return end
    self.fullOpen = true
    if self.hudGui and self.hudGui:isActive() then self.hudGui:close() end
    self:renderFullMap()
end

function MapRuntime.closeFullMap( self )
    if not self.fullOpen then return end
    self.fullOpen = false
    if self.fullGui and self.fullGui:isActive() then self.fullGui:close() end
    self.hudDirty = true
end

function MapRuntime.onFullMapClosed( self )
    self.fullOpen = false
    self.hudDirty = true
end

function MapRuntime.isFullMapOpen( self )
    return self.fullOpen
end

function MapRuntime.cycleZoom( self )
    if not self.fullOpen then return end
    if self.zoom == 1 then self.zoom = 2 elseif self.zoom == 2 then self.zoom = 4 else self.zoom = 1 end
    self:renderFullMap()
end

function MapRuntime.update( self, dt )
    local character = self:getLocalCharacter()
    if not character then
        if self.hudGui and self.hudGui:isActive() then self.hudGui:close() end
        return
    end
    if self.fullOpen then return end
    if sm.localPlayer.getActiveItem() == LOGBOOK_UUID then
        if self.hudGui and self.hudGui:isActive() then self.hudGui:close() end
        return
    end

    local position = character.worldPosition
    local key = cellKey( math.floor( position.x / 64 ), math.floor( position.y / 64 ) )
    if key ~= self.lastCellKey then
        self.lastCellKey = key
        self.hudDirty = true
    end
    if self.hudDirty or not self.hudGui:isActive() then self:renderHud( character ) end
    self:setDirection( directionIndex( character.direction ) )
end
