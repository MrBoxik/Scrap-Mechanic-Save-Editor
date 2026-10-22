# Scrap Mechanic Save Editor 4.22.1

Version 4.22.1 protects exploration history during rapid session changes.

- Serializes exploration file reads and writes independently of the live-state
  lock. Rejoining a world now waits for its pending background flush instead
  of reading an older file and replacing newly explored history.
- Preserves unsaved exploration when a write fails, and retries world binding
  instead of replacing that history with another world's state.
- Rejects activation work if the game session changes while save detection is
  still running, before an obsolete snapshot can be bound.
- Rechecks the session after map image preparation so a late result cannot
  replace the next world's map. Hides both map windows before flushing history
  on world exit or game shutdown.
- Adds deterministic concurrency, failed-write, and stale-session regression
  tests, including a reproduced failing test against 4.22.0's implementation.

# Scrap Mechanic Save Editor 4.22.0

Version 4.22.0 reworks map navigation, companion startup, and editing safety.

- Uses one centered, fixed-size waypoint system in the editor, full map, and
  minimap. Overlapping locations cluster, player markers stay visible, and
  destinations can be searched and inspected with their tile artwork.
- Opens the companion with the world's terrain map immediately, reuses cached
  HD maps, and picks up editor-built HD maps on the next world entry. Expensive
  HD generation no longer runs a second hidden 3D renderer beside the game.
- Fixes native minimap motion: between-paint terrain transforms now reach the
  actual transparent desktop surface instead of being lost in CSS. Preserves
  the existing clipped circle/rounded-square window and persistent discovery.
- Rebinds player tracking when the same world is re-entered, rejects late
  samples from old sessions, retries failed map-image loads, and clears stale
  selection details when worlds change.
- Provides a settings preview using the real renderer, including a working M
  full-map preview, and clearer full-map settings and loading states.
- Corrects inventory dragging to atomically swap complete item records without
  losing tool metadata. Blocks writes while the game runs or the source save
  changed on disk, and confirms closing with unsaved edits.
- Adds HD-build cancellation, readable generation stages, deterministic map
  navigation tests, and browser regression checks of actual native frame data.
- Slices interactive 3D visibility scans across frames and caches LOD policy
  calculations. A matched loaded-world navigation check reduced mean CPU
  render time from about 17 ms to 4.9 ms without changing the visible objects
  or LOD choices. Capture visibility remains isolated from interactive scans.

# Scrap Mechanic Save Editor 4.21.7

Version 4.21.7 restores readable map generation and fluid minimap updates.

- Restores the generation HUD as separate, wrapped lines for the current map
  section, active stage, and remaining-work counts instead of collapsing the
  whole status into one truncated line.
- Serializes the full-state bootstrap request and leaves an in-flight request
  alive until it really completes. Expensive map publication can no longer
  create overlapping timed-out requests that freeze the renderer immediately
  after a new map finishes.
- Keeps a matching cached image decode in flight instead of restarting it on a
  timer, preventing valid warm maps from remaining blank on save reload.
- Delivers shared-buffer frames directly to the native minimap as they arrive
  and uses phase-preserving frame pacing. A busy shared slot now drops one frame
  cleanly rather than falling into the slow encoded-image fallback.
- Verified in Scrap Mechanic on an uncached Survival save: generation progress
  remained live and readable, telemetry sustained about 57 updates per second,
  and the native minimap sustained about 54 displayed frames per second. The
  complete 124-test suite passes.

# Scrap Mechanic Save Editor 4.21.6

Version 4.21.6 repairs Map Companion startup, save switching, and shutdown.

- Boots the hidden minimap renderer through WebView2's native message channel
  and retries from every live telemetry packet, so entering a world never
  waits for a mouse click or a foreground animation frame.
- Publishes a live minimap frame while a new world's map is being generated,
  including bounded build status and progress, instead of leaving the HUD and
  M map blank until the full render finishes.
- Replaces the map configuration atomically when the active save changes and
  repairs pending configurations written by older builds. A new world can no
  longer inherit another save's image, dimensions, bounds, or detail tiles.
- Owns and synchronously closes both the visible native minimap and its hidden
  WebView renderer. Game exit now hides every map surface, terminates the
  companion, and persists a stopped runtime state instead of leaving a map on
  the Windows desktop.
- Keeps the M hint hidden until a real map is ready and positions pending-map
  status completely inside circular and rounded minimap regions.
- Verified with cached and uncached Survival saves launched through Steam: the
  minimap appeared without post-load input, sustained 38 displayed frames and
  54 live pushes per second in the cached run, switched to a clean pending map
  for the new save, and left zero game, companion, or map-window processes on
  exit. The complete 124-test suite also passes.

# Scrap Mechanic Save Editor 4.21.5

Version 4.21.5 repairs persisted Map Companion startup across repeated sessions.

- Installs the cached-map virtual host on both WebView2 map instances before
  either real page navigates, eliminating the failed first request that left a
  valid minimap or full map blank after restarting the game.
- Reuses a matching master image and minimap derivative without running map
  metadata inspection, PNG decoding, derivative generation, or config
  republication. A warm cache is now ready and idle as soon as its save is
  identified.
- Keeps the native minimap hidden until the cached image has actually decoded,
  so users never see an empty hatched placeholder reported as a ready map.
- Retries full-map activation promptly when M is pressed during the brief save
  snapshot bind instead of waiting for the 15-second maintenance refresh.
- Adds regressions for startup ordering, derivative validation, seed matching,
  and the no-regeneration warm activation path. Verified in two fresh companion
  processes and a complete Steam-launched leave/rejoin cycle on the same save.

# Scrap Mechanic Save Editor 4.21.4

Version 4.21.4 fixes the opaque minimap window and restores fluid rendering.

- Replaces the visible WebView2 minimap with a native WinForms picture surface
  backed by a WebView2 shared buffer. Win32 ellipse and rounded-rectangle
  regions now remove the host corners from desktop composition instead of
  leaving a black square behind circle and rounded-square modes.
- Converts browser RGBA pixels to native BGRA in an embedded WebAssembly pass,
  preserving the intended yellow frame, map colors, fog, icons, compass, and
  player marker without per-frame image encoding or Python byte copying.
- Removes frame-path pywebview request/reply calls that could strand WebView2
  response threads and starve the native paint timer. Live in-game validation
  measured about 36 displayed frames and 44 player-state pushes per second
  during camera movement, replacing the broken 1-2 updates per second path.
- Keeps the renderer resident behind Scrap Mechanic so Chromium continues to
  draw, while only the click-through, natively clipped HUD surface is promoted
  above the game.
- Adds regression coverage for shared-buffer publication, native color layout,
  RPC-free frame delivery, and real ellipse/rounded-rectangle window regions.

# Scrap Mechanic Save Editor 4.21.3

Version 4.21.3 restores instant, fluid Map Companion rendering.

- Warms the minimap renderer as soon as the Scrap Mechanic process starts and
  binds the active Survival save from Python immediately instead of waiting on
  a throttled 15-second browser interval.
- Reuses a finished cached map on the first playable frame when the current
  game log identifies the same save, while refusing to flash another world's
  cached map during a save change.
- Replaces per-frame PNG/base64 encoding and synchronous bridge decoding with
  the directly visible, natively clipped WebView renderer. Rounded and circular
  HUDs no longer need an opaque backing window or a copied bitmap surface.
- Sends compact live player transforms through WebView2's one-way native
  message channel. Real-game instrumentation measured about 58 delivered
  updates per second from about 58 process-memory samples per second, with no
  steady-state browser bridge polling.
- Coalesces superseded native messages and keeps map, fog, waypoints, player
  direction, settings, and active-context changes on the same current state.

# Scrap Mechanic Save Editor 4.21.2

Version 4.21.2 is the Map Companion state and persistence repair.

- Restricts the minimap, full map, and live memory sampler to game sessions
  whose current Scrap Mechanic log explicitly reports `Game mode: Survival`.
  Creative, Custom Game, and Challenge sessions remain overlay-free.
- Reloads persistent fog history and waypoint context when active-save loading
  finishes after the minimap WebView starts, fixing black fog after a companion
  restart and restoring on-screen and edge-clamped waypoint markers. Static
  waypoint rows now survive every high-frequency player-state repaint.
- Flushes dirty exploration on every game-stop and WebView shutdown path and
  rejects a first unload-camera transform that does not match the active
  Survival save's player position.
- Streams the WebView map/fog/waypoint canvas into a native WinForms picture
  surface, so Win32 regions remove the opaque browser backing rectangle around
  rounded and circular minimaps without affecting black fog.
- Geometrically centers the shared full-map and minimap waypoint SVGs.

# Scrap Mechanic Save Editor 4.21.1

Version 4.21.1 is the HD map stability and companion detail patch.

- Replaces the editor and companion's monolithic 8K canvas/base64 handoff with
  bounded streamed capture tiles. The backend validates and atomically
  assembles those tiles, preventing the WebView out-of-memory crash seen at
  the end of Build HD map.
- Reuses one exact terrain water atlas across capture sections and releases it
  after finalization, reducing repeated work and peak memory.
- Automatically captures and caches the HD 2D map when returning from a newly
  built 3D world, so the two map tabs no longer drift out of sync.
- Optically centers the shared waypoint artwork in both DOM and canvas maps.
- Fixes the Map Companion preview's Show M map hint control so the hint
  visibly appears and disappears before settings are saved.
- Adds the installed-game terrain tile preview to a selected waypoint's
  in-game full-map details, matching the editor's 2D map behavior.
- Rejects incomplete, stale-revision, malformed, and mis-sized streamed tile
  manifests before an HD map can be published.

# Scrap Mechanic Save Editor 4.21.0

Version 4.21.0 is the map interaction and companion customization release.

- Adds a live Map Companion preview in the editor. Size, zoom, orientation,
  frame, compass, key hint, fog radius, and reveal shape update as controls are
  changed, before the settings are saved.
- Adds directional and circular exploration reveal modes and keeps discovery,
  fog drawing, and waypoint visibility consistent across the backend, minimap,
  and full in-game map.
- Adds waypoint hover labels and a persistent selected-waypoint detail panel to
  the in-game full map, with tooltips clamped inside the screen edges.
- Adds live minimap controls to the full map's right side. Appearance,
  orientation, visible area, fog, and reveal settings apply and persist without
  restarting Scrap Mechanic or the Map Companion.
- Uses the same vector waypoint icons and colors in the editor's 2D World Map,
  full in-game map, and minimap.
- Removes the separate 3D Top view control. Fit now returns the 3D world to the
  useful straight-down view, matching Fit in 2D.
- Removes the browser focus rectangle from the flyable 3D canvas while keeping
  WASD and keyboard flight controls available.

# Scrap Mechanic Save Editor 4.20.0

Version 4.20.0 is the save and map performance, cache management, and stability
release.

- Replaces the terrain decoder's per-bit Python loop with a bounded bulk bit
  reader. Recent real-world saves load in roughly 0.52–0.59 seconds instead of
  about 1.5 seconds on the same machine, close to a threefold improvement.
- Opens uncached worlds with the exact terrain atlas in well under a second
  instead of blocking on a full 3D capture. A new Build HD map control performs
  and caches the expensive installed-asset capture only when requested.
- Doubles the bounded 3D scene-pack batch from four to eight. A representative
  warm world drops from 112 Python bridge calls to about 57 while retaining the
  existing 6 MB normal-response ceiling.
- Guards map metadata, images, features, and HD generation with world
  generations. Late work from a previous save can no longer replace the map or
  markers for the newly selected save.
- Invalidates dependent world, service, unlock, and map state after overwrite
  or backup restore so no stale panel survives a database reload.
- Adds a fast cache report and a Settings action that clears only regenerable
  map/3D data. Automatic maintenance removes abandoned temporary files and
  bounds the rendering cache at 4 GB without touching saves or backups.
- Logs slow desktop bridge requests to the local editor log for practical
  performance diagnostics.

# Scrap Mechanic Save Editor 4.19.0

Version 4.19.0 is the desktop experience and startup release.

- Removes the Harvestables and Challenges pages, their editor actions, and
  Challenge progress backup/export controls.
- Reorganizes the sidebar into Library, World Editor, and Tools groups and
  gives the complete interface a denser, consistent consumer desktop finish.
- Shows the application window before the save catalogue and 3D map engine
  initialize, so launch work is visible instead of looking like a failed click.
- Reuses an isolated WebView2 profile under Local AppData instead of creating
  and deleting a private browser profile during every launch.
- Adds a Windows single-instance lock. Reopening the editor restores and
  focuses the running window rather than creating duplicate copies.
- Changes the public Windows package to a portable extracted folder. This
  avoids PyInstaller's one-file extraction delay while retaining one launcher
  executable and a self-contained `_internal` runtime directory.

# Scrap Mechanic Save Editor 4.18.0

Version 4.18.0 is the public-release portability, packaging, and diagnostics
pass.

- Steam and Scrap Mechanic are discovered from Windows registry entries,
  `libraryfolders.vdf`, and app manifests. The game, Steam client, and official
  Mod Tool may be installed on different drives.
- Every local `User_*` profile is detected; no developer Steam ID, drive letter,
  username, or download path is compiled into the application.
- The one-file executable now contains the Map Companion and relaunches itself
  in companion mode. Public users no longer need the source folder or a sidecar
  batch file.
- Settings includes a readable installation check for Steam, Scrap Mechanic,
  profiles, the optional official Mod Tool, and the bundled companion.
- The home screen has a first-class Map Companion launcher that starts the game
  normally through Steam without developer mode or game-file hooks.
- A verified Mod Tool/Workshop feasibility report documents why executable UI
  is limited to Custom Games and why the external overlay remains the supported
  option for existing vanilla Survival saves.
- Release documentation now includes privacy, security, third-party, support,
  and checksum guidance. Automated checks reject personal absolute paths and
  version drift before publication.
- Layout overflow and long-path presentation were tightened for smaller desktop
  windows without removing any editor or map capability.

# Scrap Mechanic Save Editor 4.17.11

Version 4.17.11 removes the remaining movement-only minimap stalls and restores
high-refresh presentation without overloading the WebView render thread.

- Exploration samples and cells now append in constant time. Walking no longer
  copies the complete fog history for every four-metre or six-degree update.
- New fog samples no longer invalidate every waypoint discovery result. The
  cache changes only when the backend reports an actually discovered location.
- The large map, fog, and waypoint canvas is repainted at a bounded 60 Hz while
  GPU-composited translation and rotation present its interpolated transform on
  every display frame. The independent vector player arrow remains fully live.
- The native game-memory sampler keeps a true 60 Hz phase on Windows, and proven
  memory records use a two-candidate audit instead of repeatedly rescanning the
  complete address set.
- A 24-second stop/start movement stress test delivered 4,393 presentation
  frames with no frame over 33 ms and no render exception.

# Scrap Mechanic Save Editor 4.17.10

Version 4.17.10 repairs the minimap's native frame, restores fluid live
tracking, and makes full-map symbols independent of map zoom.

- The minimap WebView no longer forces a manually layered native host. WebView2
  supplies per-pixel alpha itself, while a DPI-aware rounded window region
  clips only the pixels outside the Scrap Mechanic frame. This preserves the
  complete gold border and removes the black square from all four corners.
- The player map and arrow now render on every display frame. Fresh game-memory
  positions and headings are accepted at their real sampling rate, lightly
  predicted between samples, and corrected with shortest-arc interpolation.
  Walking and turning no longer freeze or jump behind the game.
- Full-map player and waypoint symbols now live in a screen-space vector layer.
  Map pan and zoom update their screen coordinates without scaling a rasterized
  parent, so symbols remain crisp circles and arrows instead of collapsing to
  2-by-2 pixel blocks at high zoom.
- Regression coverage now checks the native alpha style, safe rounded-region
  radius, high-rate transform acceptance, per-frame minimap rendering, and
  zoom-independent marker coordinates.

# Scrap Mechanic Save Editor 4.17.9

Version 4.17.9 replaces the three ineffective cosmetic workarounds with
independent rendering and native-window fixes.

- The minimap player and facing cone now have their own transparent canvas.
  They update every display frame without waiting for the expensive 8K map,
  fog, or waypoint repaint. Existing 24/30 Hz settings migrate to 60 Hz.
- Full-map fog cleanup now prunes one-pixel bridges before removing small
  enclosed components. This catches the grey/black smears that were still
  connected to the unexplored region by an antialiased filament.
- The rounded minimap crop is applied to the real top-level WinForms window,
  including its invisible DPI/client offset, and the installed region is
  verified after WebView2 finishes attaching. The host rectangle outside the
  Scrap Mechanic frame is no longer composited.

# Scrap Mechanic Save Editor 4.17.8

Version 4.17.8 restores fluid player tracking, closes tiny fog artifacts,
removes the remaining native minimap rectangle, and improves marker clarity.

- The minimap now caches its complete fog mask and incrementally adds only
  new exploration samples. Player position and shortest-arc heading remain a
  light 60 FPS canvas operation instead of redrawing the whole fog history on
  every frame.
- The full-map player marker also interpolates at display refresh rate while
  live transforms are sampled at 20 Hz, eliminating the visible 100 ms steps.
- Adjacent four-metre discovery samples receive a matching four-metre
  mathematical closing margin. Tiny antialiased black/grey islands are
  removed without turning the directional footprint back into a circle.
- Full-map fog updates are incremental, so walking no longer rebuilds every
  old footprint and stalls the player marker.
- The native minimap clip is validated after WebView2 finishes its asynchronous
  show transition and follows the exact rendered frame bounds reported by the
  page at the current DPI. Only the rounded or circular frame remains visible.
- Minimap and full-map POI markers are larger. The minimap player stays at its
  existing size, while the full-map player receives a small readability bump.

# Scrap Mechanic Save Editor 4.17.7

Version 4.17.7 unifies the player marker, makes fog discovery directional,
and removes the opaque native square behind the minimap frame.

- The full map and minimap now render the player from one canonical vector
  asset—the same SVG/canvas pipeline used by POI waypoints. The full-map
  arrow remains crisp at every zoom and exactly matches the HUD arrow.
- Exploration is recorded from the exact centre of the live player arrow at
  four-metre intervals with six-degree facing samples. It no longer changes
  reach at a 64-metre world-tile boundary.
- Fog reveal is a smooth forward-biased visibility footprint: full distance
  ahead, progressively less at the sides, and only a small safety radius
  behind. Current and persistent fog use the same geometry in both maps.
- Valid zero direction components are preserved, correcting the hidden
  east/west heading skew in player markers and discovery.
- The native minimap WebView is clipped to its rounded or circular visible
  frame, so a GPU-composited black host rectangle cannot appear behind it.
- Old circle- and cell-based exploration files migrate automatically; their
  existing discovered area is retained while all new samples use the precise
  directional format.

# Scrap Mechanic Save Editor 4.17.6

Version 4.17.6 fixes the delayed minimap blink sequence after closing the
full map.

- Closing the full map now transfers input to Scrap Mechanic synchronously,
  then performs one final reveal of the already-rendered minimap window.
- Focus restoration no longer schedules delayed retries that can reorder
  native windows after the close transition has finished.
- The game window is never temporarily promoted above the always-on-top
  minimap. Keyboard and mouse focus are restored without changing z-order
  bands.
- Live close traces remained continuously visible at 0.18, 0.9, 1.55, and
  2.8 seconds after the transition, covering the former blink interval.

# Scrap Mechanic Save Editor 4.17.5

Version 4.17.5 repairs the full-map hotkey process, restores real POI artwork,
and removes the remaining high-resolution map interaction stall.

- `M` is now handled inside the companion's Python monitor. The detached
  PowerShell key watcher is no longer started, so it cannot die separately
  while leaving a working minimap with a permanently unavailable full map.
- Full-map and minimap visibility now use native Win32 calls only. Background
  threads no longer invoke pywebview/WinForms `show`, `hide`, or JavaScript
  APIs—the source of the logged `InvalidAsynchronousStateException` process
  termination.
- A finished cached PNG is authoritative after restart. If the minimap can
  display that cache, `M` can immediately open it even before any new renderer
  progress packet or client-bounds sample arrives.
- POIs no longer depend on WebView2's optional SVG-string `Path2D` support.
  Explicit vector primitives and SVG fallbacks render category artwork on the
  first frame, including player beacons, instead of dark empty discs.
- High-resolution detail tiles remain decoded while panning and zooming; the
  viewer no longer hides and recomposites the entire tile layer on each input
  frame. A larger decoded LRU makes nearby reverse pans smooth.
- Closing the full map performs one atomic native z-order reveal of the warm,
  resident minimap. It does not hide, recreate, redraw, or resize the HUD.

# Scrap Mechanic Save Editor 4.17.4

Version 4.17.4 removes the minimap's native-window flicker, makes full-map
interaction substantially lighter, and unifies every POI symbol.

- The minimap remains one resident WebView surface while a world is loaded.
  Game/full-map focus changes now alter only native z-order; they no longer
  hide, show, recreate, or repeatedly promote the minimap compositor.
- Native bounds checks run only when the game client rectangle or minimap
  settings actually change, instead of issuing SetWindowPos twice per second.
- Full-map drag and wheel input transforms a 4K overview while moving and
  restores the original lossless detail tiles shortly after input settles.
  Detail tiles use a bounded decoded LRU rather than being destroyed and
  decoded again after every pan.
- Full map and minimap now consume the same category colours and exact SVG
  path geometry. Minimap POIs are no longer clipped to the icon circle and
  beacon artwork receives a safe inset.

# Scrap Mechanic Save Editor 4.17.3

Version 4.17.3 stabilizes exploration, native-window handoff, POIs, and full-map interaction.

- Fog is now truly opaque black and records a smooth 4-metre trail instead
  of centring every reveal on a 64-metre cell. The live player hole remains
  exact while moving.
- Implausible process-memory jumps during world exit are rejected, and the
  legacy isolated centre-map reveal is removed during exploration migration.
- Existing travelled routes now recover nearby discovered locations, so
  crashed ships, stations, and other visited POIs receive their vector marker
  without exposing locations still hidden by fog.
- The warm minimap is no longer redrawn or promoted when the full map closes
  or when a legend checkbox changes, eliminating both flicker and minimap
  overlap on the full-map window.
- Panning and zooming are animation-frame coordinated; high-resolution tile
  loading waits until interaction pauses instead of decoding and unloading
  tiles during every pointer event.

# Scrap Mechanic Save Editor 4.17.2

Version 4.17.2 restores full-map zoom and adds real multiresolution rendering.

- Full-map zoom remains available through the previous 32x range. The viewer
  now swaps in high-resolution 3D-rendered tiles for the visible area instead
  of enlarging only the 8192-pixel overview.
- The complete world is captured once as a lightweight overview and twelve
  independently cached 2x-detail sections. Off-screen detail tiles are not
  decoded, keeping GPU memory and map interaction bounded.
- Exploration fog now unions travelled circles on one alpha mask, eliminating
  the overlapping-ring mosaic made by even/odd subtraction.
- Generated POIs use crisp category symbols at every zoom instead of embedding
  blurry isometric world-tile thumbnails inside waypoint circles.
- The minimap stays warm beneath the opaque full map so closing it no longer
  recreates the HUD WebView and flashes it for several seconds.

# Scrap Mechanic Save Editor 4.17.1

Version 4.17.1 fixes new-world map publication after the high-resolution map
upgrade.

- The hidden real-3D renderer and the completion validator now use the same
  8192-pixel `companion-hq` cache identity. Rendering can no longer reach 100%
  and then lose the finished image because it was stored in the retired
  4096-pixel cache.
- A finalization error now keeps the progress minimap visible and preserves
  the immutable capture job for an automatic retry. It no longer silently
  removes the minimap and permanently disables the full-map M action.
- Added a regression test covering the exact completed-then-disappears failure
  and the capture cache contract.

# Scrap Mechanic Save Editor 4.17.0

Version 4.17.0 adds persistent exploration fog, discoverable game-art map
markers, exact live positioning, and a first-class Map Companion launcher.

- The full map now uses a separate lossless 8192-pixel master capture. The
  always-visible minimap uses a high-quality 4096-pixel derivative, preserving
  sharp full-map zoom without spending extra GPU memory or frames during play.
- Player coordinates are mapped in exact world units on both surfaces. The old
  half-cell offset that could move the full-map arrow by about 32 metres is
  gone.
- SnowRunner-style exploration is stored outside game saves. A circular area
  is revealed around the live player, the travelled trail persists per save
  and seed, and undiscovered generated locations stay hidden.
- Quests, stations, warehouses, packing stations, labs and dungeons,
  landmarks, and player beacons use installed game artwork. A full-map legend
  provides independent Full map and Minimap checkboxes for every category.
- Discovered waypoints remain useful on the minimap: distant locations clamp
  to its edge and grow smoothly as the player approaches. Player beacons are
  known immediately and keep their saved icon and colour.
- Closing the full map now hands visibility directly to the already-warm
  minimap before restoring Scrap Mechanic focus, eliminating the two-second
  WebView show/hide flicker.
- The editor's Load save home page now has a dedicated launch action, and a
  separate Map companion tab contains minimap, frame, orientation, fog,
  discovery-radius, and live-rate settings. Tracking can be raised to 60 Hz.

# Scrap Mechanic Save Editor 4.16.8

Version 4.16.8 removes the remaining shoreline helper blocks and keeps live
input continuous through autosaves and full-map transitions.

- Dark teal helper fringes are now treated as water only when they directly
  touch confirmed atlas water. This removes the last small green rectangles
  near shores without flooding grass, islands, rocks, or inland terrain.
- Autosave snapshot jumps remain rejected without triggering a process-memory
  rescan, and the companion no longer reparses a copied SQLite save every 30
  seconds while a world is active. The proven live address stays active,
  preventing the recurring multi-second minimap pause.
- Closing the full map now restores Scrap Mechanic through the native foreground
  path after WebView2 has actually hidden, with short transition retries so the
  game immediately accepts keyboard and mouse input again.
- Companion map cache revision 11 upgrades revision-10 captures in place; the
  detailed 3D world does not need to be rendered again.

# Scrap Mechanic Save Editor 4.16.7

Version 4.16.7 fixes live minimap rollback, map-input desynchronization, menu
visibility, and the remaining ocean helper fragments.

- Live position is now exclusively sourced from the active per-frame game
  record while a world is loaded. Slower save snapshots cannot overwrite it,
  and a one-shot autosave copy must show consecutive motion before it can ever
  replace the selected memory record.
- M and Escape now send explicit open/close requests. The key monitor keeps
  fast press parity but resynchronizes from the overlay's acknowledged physical
  state. A native-window visibility invariant also catches delayed WebView2
  transitions, preventing stuck, reopened, or mixed full-map/minimap states.
- Scrap Mechanic's latest frontend state controls the HUD lifecycle. Returning
  to Load/Menu force-hides both companion surfaces and clears stale live state
  even though the game process remains running.
- The exact-water pass now removes small enclosed atlas helper fragments from
  open ocean while retaining coastlines and genuine islands. Cache revision 10
  migrates the previous detailed capture without rebuilding its 3D world.

# Scrap Mechanic Save Editor 4.16.5

Version 4.16.5 fixes the minimap-ready startup race and makes generated map
water follow the exact game shoreline.

- A completed map is now pushed directly into the already-visible minimap and
  retried while the HUD is active. The minimap therefore appears without an
  initial M key press or a redundant "map ready" instruction.
- Generated map water now uses the game's exact rotated cell atlas as a
  pixel-level coastline stencil over the detailed real-3D capture. Missing
  lakes are restored, straight/square water spill is clipped, and dry quarries,
  islands, rocks, roads, buildings, and vegetation remain intact.
- Companion map cache revision 8 rebuilds revision-7 PNGs once so an old
  incorrect water layer cannot continue to appear after updating.

# Scrap Mechanic Save Editor 4.16.4

Version 4.16.4 fixes live minimap latency and incorrect generated water.

- The live transform scanner now distinguishes Scrap Mechanic's active
  per-frame player record from frozen renderer/autosave copies with the same
  layout. Position and direction are sampled at 60 Hz and pushed directly to
  the minimap at the configured rate, independently of 3D map capture work.
- The 2D capture now follows Survival's authored-water rule exactly. Lake
  flags adjust only installed water assets; they no longer create synthetic
  square water planes through beaches, quarries, or dry excavation tiles.
- Companion map cache revision 7 automatically rebuilds older captures once,
  then reuses the corrected 4096x3072 PNG on later launches.

# Scrap Mechanic Save Editor 4.16.3

Version 4.16.3 fixes saved player creations whose block-built sections were
rendered black in the complete 3D world. Scrap Mechanic block diffuse textures
store paint coverage in alpha while their RGB is often intentionally black;
the viewer now uses the same alpha paint-mask path as mesh-based parts instead
of multiplying the saved paint colour by that black RGB channel.

# Scrap Mechanic Save Editor 4.16.2

Version 4.16.2 fixes live 3D textures repeatedly disappearing and reloading
when the camera stops or crosses the texture-memory threshold. Textures used by
the current camera view are now resident and cannot be evicted. Recently hidden
textures receive a short grace period for smooth camera reversals, while only
off-camera textures are reclaimed when memory pressure remains high. The live
working-set budget was also raised to cover the normal full-detail view without
cycling its materials.

# Scrap Mechanic Save Editor 4.16.1

Version 4.16.1 fixes premature 2D map captures. The high-resolution map now
waits for full-detail LOD geometry, material lookups, and every visible texture
in each capture tile before rendering it. Capture textures are pinned against
eviction until their tile is safely copied into the PNG, and an incomplete tile
now cancels the capture instead of caching a damaged map. Existing incomplete
map caches are automatically ignored and rebuilt with the new renderer revision.

# Scrap Mechanic Save Editor 4.16.0

Version 4.16.0 replaces the old flattened-preview map with a reusable,
high-resolution orthographic capture of the complete real 3D world.

## Generated 2D world map

- The first visit to **World map** loads the complete installed-game 3D world
  and renders it straight down into a sharp PNG at up to 8192 pixels per side.
- Captures the world in safe 4096-pixel GPU tiles and stitches them into one
  seamless image, avoiding WebView drawing-buffer limits and black edge bands.
- Stores each generated map under
  `%LOCALAPPDATA%\ScrapMechanicSaveEditor\map-cache\generated-3d`, never inside
  Scrap Mechanic's save directories.
- Keys the cache by save path, exact modification timestamp, renderer revision,
  resolution, and game installation. Editing or replacing a save therefore
  produces a fresh map while unchanged saves open from cache.
- Validates the PNG signature and exact expected dimensions before an atomic
  write, preventing interrupted or malformed captures from becoming the map.
- Keeps search, category filters, hover/click details, player tracking, beacons,
  pan, zoom, and Fit as live HTML overlays over the cached image.
- Excludes players, enemies, and creations from the baked background so moving
  save entities cannot become stale pixels; their live map data remains
  separate.

## Capture reliability

- Waits for the sharp geometry LODs and visible textures before capture.
- Correctly accepts installed meshes whose requested LOD falls back to the
  game's available LOD, eliminating false missing-model results during capture.
- Releases the full 3D renderer after PNG generation so ordinary 2D map use
  does not retain the large WebGL world in memory.

# Scrap Mechanic Save Editor 4.15.0

Version 4.15.0 replaces terrain-seam workarounds with the same cell ownership
and generated elevation rules used by Scrap Mechanic's Survival terrain code.

## Terrain and decal reconstruction

- Decodes every compiled terrain cell as its own closed 33-by-33 patch, just as
  `sm.terrainTile.getHeightAt` does. Shared border positions remain visually
  joined, while each copy keeps the owning cell's cliff base. This removes the
  grass/rock walls caused by assigning an exact-border vertex to its neighbor.
- Removes all synthetic seam skirts and aprons. An audit of all 445 installed
  scene types found every expected compiled terrain cell present, so no fake
  filler geometry is required.
- Ports Survival's cubic Bezier elevation control points and road-flatness
  flags into the vertex shader instead of bilinearly interpolating corner
  heights. Roads, terrain, objects, and decals now share the game's generated
  height curve.
- Tessellates supported ground decals into projected 4-by-4 meshes. Each decal
  vertex follows generated terrain height, so road paint and tire marks bend
  over hills instead of crossing them as one flat plane.

## Full-detail performance

- Keeps full-detail rendering and all world instances, but groups scene and
  save-entity instances in one pass instead of rescanning every scene once per
  asset definition.
- Back-face culls only the authored terrain surface. Arbitrary game meshes and
  foliage remain double-sided, preserving their visual coverage.
- Bumps the compiled world cache revision so the corrected cell meshes are
  rebuilt once and reused on subsequent launches.

# Scrap Mechanic Save Editor 4.14.1

Version 4.14.1 removes the terrain-wall workaround and reproduces the game's
per-cell lake rule for authored water assets.

## 3D world corrections

- Reverses horizontal mouse look so left/right dragging follows the requested
  direction while keeping the already-correct vertical movement.
- Replaces 32-metre vertical terrain skirts with narrow horizontal boundary
  aprons that copy the real terrain material. Small tile cracks stay covered
  without producing grass or rock paper walls.
- Reads `TYPE_LAKE` directly from each save cell's terrain flags and only skips
  elevation for the official water mesh in those cells, matching
  `terrain_overworld.lua`. Water authored for bridges, ponds, or other dry-cell
  scenes now receives normal world elevation instead of appearing as an inland
  ocean square.
- Invalidates the prior world-scene cache so corrected seam meshes are rebuilt.

# Scrap Mechanic Save Editor 4.14.0

Version 4.14.0 reconstructs the complete-world terrain borders and camera path
from the installed game's terrain rules instead of masking the previous
artifacts with distance culling.

## World renderer reconstruction

- Removes the broken **Balanced** mode. The 3D world now always loads and keeps
  every world, player, unit, and creation instance in one full-detail scene.
- Stops rebuilding and re-uploading hundreds of thousands of instance matrices
  whenever the camera moves. WASD, mouse look, Q/E, pan, and wheel movement now
  update only the camera view matrix.
- Corrects left/right and up/down mouse look, A/D strafing, and Q/E turning so
  every control follows the direction pressed or dragged.
- Adds cell-aware terrain boundary geometry. Full tile placements draw only
  their perimeter, while partial placements close only the exposed masked edge,
  eliminating raw void cracks without adding a fake world-sized floor.
- Renders authored hard terrain steps and boundary faces with the installed
  rock material instead of stretching bright grass into paper-thin walls.
- Invalidates the old compiled world cache so every terrain tile is rebuilt with
  the corrected cell ownership and seam data.

## Research and validation

- Verified the save placement origins and quarter-turn rotations against
  `Survival/Scripts/util.lua` and `terrain_util2.lua`.
- Audited all 12,288 generated world cells and all 445 used compiled scene
  types: there are no missing or duplicate cells and no undecoded terrain
  blocks. The remaining discontinuities are authored cliff steps plus nine
  genuine cell-border height differences, now handled by the boundary mesh.
- Revalidated the real installed-game terrain, full save world, saved entities,
  and complete automated editor suite before packaging.

# Scrap Mechanic Save Editor 4.13.3

Version 4.13.3 repairs the regressions that made the complete-world viewer
unusable at close range and restores saved creations to the 3D scene.

## Complete-world fixes

- Fixes **Full detail** so it truly keeps every loaded world instance visible;
  it no longer retains the hidden 900–1400-unit culling radius that exposed
  terrain seams, void strips, and uncovered cliff-level walls.
- Restores camera-relative free flight: drag looks from the camera, WASD flies
  in the viewed direction, Q/E turns in place, Space/Ctrl moves vertically,
  and right-drag pans.
- Bases balanced-detail visibility on the camera position rather than a remote
  orbit target, preventing nearby cover meshes from disappearing while the
  camera is far from its focus point.
- Keeps the distance-aware near plane from the stable renderer so the restored
  free camera does not reintroduce whole-world depth flicker.

## Saved creations

- Corrects the ChildShape block-size byte order. A saved 14 x 65 x 1 block was
  previously decoded as 3584 x 16640 x 256, producing giant paper-thin walls.
- Invalidates the old entity cache and renders the save's installed creation
  blocks and parts alongside the player and units.
- Reports player, unit, and creation counts when the complete 3D world finishes
  instead of silently suppressing creations.

## Validation

- Added a real-save regression that rejects implausibly large decoded creation
  scales, plus source checks for free flight, complete Full-detail visibility,
  and creation rendering.
- Revalidated the JavaScript parser and the full automated editor suite before
  packaging.

---

# Scrap Mechanic Save Editor 4.13.2

Version 4.13.2 restores the last visually stable complete-world renderer after
the 4.13 camera and surface changes caused repeating terrain bands, giant
ground planes, black spikes, and unusable depth overlap. It keeps the bounded
compressed scene transfer and finite texture-finalization work from 4.13.1.

## Renderer recovery

- Removes the generated world-seal plane that could become a giant green or
  black sheet when sampled through the height field.
- Restores the target-orbit camera and its distance-scaled near plane, fixing
  the severe whole-world depth-precision artifacts visible in fitted views.
- Restores focus-based spatial culling, so nearby visibility is based on the
  area being inspected instead of the offset camera position.
- Stops forcing decals onto the generated height surface and restores the
  proven decal depth bias, eliminating paper-thin spikes and unstable strips.
- Stops treating every water/chemical instance as permanently visible at all
  distances; ordinary balanced-detail culling is used again.
- Keeps direct 2D/3D switching, close-range 3D players and units, compressed
  scene packs, bounded texture finalization, and the complete-world cache.
- Temporarily suppresses experimental rigid-body creation shapes because their
  unresolved transform/scale cases can cover the world with giant geometry;
  they remain available to the save editor and can return after that decoder is
  independently corrected.

## Validation

- Added source regressions that reject the global seal, continuous-surface
  override, free-camera visibility path, and forced terrain-decal deformation.
- Revalidated JavaScript syntax, the full automated editor suite, and the
  packaged desktop renderer on the 11,105-placement Survival test world.

---

# Scrap Mechanic Save Editor 4.13.1

Version 4.13.1 fixes the complete-world regression where loading could remain
at a scene count such as 400/445. Large installed scenes are now transferred in
bounded compressed chunks, progress advances after every decoded scene, model
batches are smaller, and late texture requests are stopped after a fixed
finalization window instead of keeping the map in a permanent loading state.

# Scrap Mechanic Save Editor 4.13.0

Version 4.13.0 finishes the free-flight world controls, preserves continuous
water and terrain coverage at every distance, and brings live save entities
into the complete 3D world as geometry instead of 2D pins.

## Camera and complete-world reliability

- Replaces target-orbit movement with a real free camera. Mouse-look and Q/E
  rotate the camera in place; WASD follows its view direction, Space/Ctrl moves
  vertically, right-drag pans, and Fit/Top view establish predictable poses.
- Exempts water and chemical surfaces from distant prop sampling, so the ocean
  no longer loses most of its tiles in a fitted or far-away view.
- Adds a generated-height crack underlay below the authored terrain to conceal
  the last sub-pixel black seams without overlapping hills or cliffs.
- Waits for image decode and GPU texture upload before reporting **Complete 3D
  world**, so the completion label now reflects the visible result.

## Real save entities

- Decodes the save's Unit records, resolves their reversed binary UUIDs through
  the installed character sets, and draws the actual static character meshes.
- Reconstructs player-built rigid bodies from ChildShape UUIDs, body
  quaternions, all 24 packed child orientations, exact colors, and block bounds.
- Draws the active player as an installed mechanic model and proximity-culls
  all save entities so they appear when approached without filling distant
  overview frames.
- Removes enemy and creation waypoint layers from 2D. The player location
  remains available there as the useful **You** marker.

## Decals, caching, and performance

- Rejects malformed or non-ground projector planes, bends supported ground
  decals with the generated terrain height, and reduces depth bias to limit
  floating, clipping, and paper-thin spikes.
- Reuses geometry already loaded by the generated world when creations use the
  same parts, and stores a save-mtime-keyed compiled entity assembly outside
  the game save folders for fast repeat openings.
- Keeps spatial bins and balanced distant sampling for ordinary props while
  guaranteeing full coverage for continuous surfaces and nearby entities.

## Validation

- Decoded the active test save into 1 player, 767 units, 972 rigid bodies, and
  3,954 child shapes with zero unresolved entity asset UUIDs.
- Added regressions for entity decoding, packed orientation mapping, free-camera
  state, continuous water, accurate completion, terrain-conforming decals, and
  removal of 2D enemy/creation controls.

---

# Scrap Mechanic Save Editor 4.12.0

Version 4.12.0 makes the generated world a direct, smoother 2D/3D experience
and applies the same elevation and cliff rules that Scrap Mechanic uses when
assembling adjacent terrain cells.

## Seamless generated terrain

- Decodes the save's corner elevation and cliff-level arrays and applies their
  bilinear height field on the GPU to terrain, objects, water, and decals.
- Uses Scrap Mechanic's separate water-height rule, removing the remaining
  beach/ocean overlap without lifting lakes onto nearby raised ground.
- Keeps the exact compiled decal quaternion and generated height at every
  placement, improving the last road-marking and shoreline alignment cases.

## Direct world navigation

- Replaces the Native/Real 3D/single-tile path with a direct **2D map** /
  **3D world** switch; 3D always opens the complete generated world.
- Keeps the complete 3D scene resident when returning to 2D, so switching back
  is immediate instead of rebuilding the world.
- Adds **Q/E** in-place camera rotation, a **Top view** for a sharp
  geometry-based overhead map, and **Balanced / Full detail** rendering.
- Adds optional Enemies and Creations position layers to the searchable 2D
  map without forcing the noisy creation layer on by default.

## Loading and rendering performance

- Batches scene and mesh bridge requests instead of making thousands of
  individual Python calls.
- Persists resolved scenes, packed mesh geometry, converted textures, and
  texture-profile metadata under the editor's local cache. Repeat visits reuse
  the installed assets instead of decoding them again.
- Uses spatial instance bins, camera-distance culling, dynamic instance
  buffers, cached shader uniforms, and balanced display resolution to keep
  camera movement substantially smoother in the 567,000-object test world.
- Loads common repeated geometry first and keeps the interface responsive
  while the remaining detail streams in.

## Validation

- Live-loaded all 11,105 placements and 567,056 object instances, checked the
  complete-world fit and geometry top view, and verified that 2D/3D switching
  does not discard the resident renderer.
- Added regressions for saved elevation/cliff decoding, generated-height
  shaders, water height behavior, batch endpoints, cached texture profiles,
  camera rotation, and direct complete-world controls.

---

# Scrap Mechanic Save Editor 4.11.1

Version 4.11.1 turns the whole-world experiment into a persistent complete
3D world and fixes the remaining scene-overlap and ground-decal transform
problems around roads, beaches, and Excavation Island.

## Persistent complete world

- Replaces the moving nearby-cell patch with all 11,105 generated placements
  from the active test save, loaded progressively into one resident scene.
- Camera movement no longer clears or rebuilds the world when crossing a
  terrain-cell boundary.
- Groups repeated scenes and meshes for GPU instancing; the validated world
  contains 567,056 rendered object instances and remains responsive.
- Renames the control from **Whole world** to **Complete world** and makes
  **Fit** frame the entire generated world.

## Correct scene ownership and transforms

- Detects the true occupied-cell mask for partial multi-cell POIs instead of
  filling their entire rectangular footprint.
- Prevents Excavation Island's mountain, kinematic bridge, and beach scenes
  from drawing land or water over one another.
- Uses compiled `.tile` quaternions for ground decals and the game's Euler
  order for editable prefab transforms, correcting mirrored and rotated road
  markings.
- Restricts negative polygon offset to decals; water keeps normal depth
  behavior, removing beach and ocean z-fighting.

## Validation

- Reconstructed all 12,288 cells in the test world exactly once, with no gaps
  or overlapping scene ownership.
- Live-loaded and fitted the complete 3D world in the desktop application, then
  verified that camera input leaves the 11,105-placement scene resident.
- Added regressions for partial placement masks, complete-world persistence,
  compiled decal rotations, and water/decal depth separation.

---

# Scrap Mechanic Save Editor 4.11.0

Version 4.11.0 replaces the remaining texture guesses with Scrap Mechanic's installed material rules and adds an experimental streamed whole-world 3D view.

## Game-authored materials and assembly

- Reads `asset_materials.json` and `part_materials.json` shader defines instead of guessing a UV channel from a material name.
- Uses diffuse alpha as the Paint Tool mask and reads ASG red/green/blue/alpha channels for transparency, specular response, glow, and reflectivity.
- Keeps `DifAsgNorNorD_UV1` diffuse textures on UV0 unless the installed profile explicitly defines diffuse UV1, removing the repeating atlas mosaics and oily smears.
- Includes locked build-quest blueprints used by generated locations, restoring the steel bridge and other quest structures that were previously omitted.
- Filters game-only light-cone and mesh-decal helper geometry, uses inverse-transpose normal transforms, and removes fabricated tile-wide water planes.

## Whole-world flight

- Adds a **Whole world** Real 3D toggle that streams nearby generated terrain cells around the camera with bounded memory use.
- Adds camera-relative WASD flight, Space/Ctrl vertical movement, Shift boost, and corrected vertical mouse-look direction.
- Reuses scene, geometry, and texture caches while moving and cancels stale streaming work when the camera enters a new area.

## Validation

- Live-checked the mechanic station, the locked steel bridge, and a 140-cell whole-world stream containing more than 6,000 rendered objects.
- Added regressions for shader-defined UV selection, ASG maps, paint masks, locked blueprint inclusion, explicit-only water, and the new flight controls.

---

# Scrap Mechanic Save Editor 4.10.4

Version 4.10.4 fixes the root causes behind the remaining Real 3D corruption instead of adding more per-tile visual workarounds.

## Correct installed materials

- Reads both legacy `textureList` entries and the named `textures` maps used by packing stations, bridges, crystals, silos, and newer Survival assets.
- Matches textures to their exact Assimp material and no longer borrows an unrelated submesh atlas when a material deliberately has no diffuse map.
- Interprets UvMaterial sheets as detail masks combined with the authored object color, removing rainbow/mosaic surfaces from large bridges and dungeon structures.
- Preserves the installed texture orientation and restores the centimetre scale of Collada packing-station machinery.

## Stable terrain and surfaces

- Stops treating packed terrain vertex-mask bytes as RGB paint, removing the repeated red circles and neon ground tint.
- Creates water only from explicit installed water or chemical geometry, so dry dungeons and landmarks stay dry.
- Defers embedded `MeshDecal` layers that the game composites in a later render pass, removing the severe ruin-roof z-fighting in the lightweight preview.
- Filters editor projector helpers while retaining genuine horizontal road and ground markings.

## Verified scenes

- Live-checked the Kinematic Bridge, Autumn Forest Ruin, Packing Station, Hay Maze, and a dry Minidungeon in the desktop application.
- Added regression coverage for named textures, UvMaterial detection, Collada scale, explicit-only water, terrain-mask handling, and MeshDecal deferral.

---

# Scrap Mechanic Save Editor 4.10.3

Version 4.10.3 focuses on Real 3D material accuracy and removes engine-only helper effects that a lightweight WebGL preview cannot reproduce faithfully.

## Cleaner water and terrain

- Detects installed water and chemical meshes and renders their real geometry with dedicated procedural surface materials.
- Avoids adding a second full-tile water plane when a shaped water mesh is already present, removing ghost terrain and water z-fighting.
- Draws water after opaque geometry with stable depth separation.
- Detects alpha-only diffuse maps even when Assimp renames the material and expands their usable contrast, restoring rock and tiled-surface detail.
- Treats uniform black diffuse maps as material placeholders instead of painting objects black or white.

## Safer prefabs and effects

- Honors `ts:show` runtime state tags, so event-only destroyed pieces, activated lights, and effect helpers do not overlap the default tile.
- Keeps default-visible `ts:hide` content and still prefers intact prefab variants.
- Removes exact duplicate nested-prefab instances that caused flashing ruin roofs and stacked surfaces.
- Defers invisible area-light and light-cone helper meshes while keeping physical lamps and fixtures.
- Verifies that packing-station base, middle, front, screens, and crate-loader definitions remain present.

## Projector and decal correctness

- Renders only genuine authored-horizontal road, tire, parking, crack, dirt, patch, moss, and burn-mark decals.
- Defers curved-object, wall, landing-symbol, and effect projectors instead of turning them into flat walls through geometry.
- Removes red projector-helper dots and giant floating symbols.
- Deduplicates identical decals and increases deterministic layer separation to reduce road and ruin z-fighting.

## Current limits

- This remains a lightweight WebGL reconstruction rather than Scrap Mechanic's complete renderer. Animated lights, particles, kinematics, shader-specific reflections, and arbitrary deferred projector volumes are deliberately deferred when a flat approximation would be misleading.
