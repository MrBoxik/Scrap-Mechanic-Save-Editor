local renderables = { "$SURVIVAL_DATA/Character/Char_Tools/Char_logbook/char_logbook.rend" }
local renderablesTp = { "$SURVIVAL_DATA/Character/Char_Male/Animations/char_male_tp_logbook.rend" }
local renderablesFp = { "$SURVIVAL_DATA/Character/Char_Male/Animations/char_male_fp_logbook.rend" }

sm.tool.preloadRenderables( renderables )
sm.tool.preloadRenderables( renderablesTp )
sm.tool.preloadRenderables( renderablesFp )

MapTool = class()
MapTool.equipWhileSeated = true

function MapTool.client_onCreate( self )
    self.cl = { equipped = false }
end

function MapTool.client_onEquip( self )
    self.cl.equipped = true
    local tp = {}
    for _, value in ipairs( renderablesTp ) do tp[#tp + 1] = value end
    for _, value in ipairs( renderables ) do tp[#tp + 1] = value end
    self.tool:setTpRenderables( tp )
    if self.tool:isLocal() then
        local fp = {}
        for _, value in ipairs( renderablesFp ) do fp[#fp + 1] = value end
        for _, value in ipairs( renderables ) do fp[#fp + 1] = value end
        self.tool:setFpRenderables( fp )
    end
end

function MapTool.client_onUnequip( self )
    self.cl.equipped = false
end

function MapTool.client_onEquippedUpdate( self, primaryState, secondaryState )
    if not self.tool:isLocal() then return false, false end

    sm.gui.setInteractionText( "", sm.gui.getKeyBinding( "Create", true ), "Open / close exploration map" )
    if primaryState == sm.tool.interactState.start then
        sm.event.sendToPlayer( sm.localPlayer.getPlayer(), "cl_e_mapCompanionPrimary" )
        return true, false
    end
    if secondaryState == sm.tool.interactState.start then
        sm.event.sendToPlayer( sm.localPlayer.getPlayer(), "cl_e_mapCompanionSecondary" )
        return false, true
    end
    return false, false
end
