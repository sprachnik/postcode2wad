"""The in-game overlay: minimap, compass, coordinates — and tile transitions.

Three jobs, all in one StaticEventHandler because a static handler is the only
thing in GZDoom that survives a level change, and the tile transition needs to
carry the player's position across one.

The minimap is *baked* at generation time (`preview.render_minimap`) because we
already know the plan and redrawing 14,000 linedefs per frame in ZScript would
be absurd. The position readout has to be live, so each tile's georeference is
baked into the script instead and evaluated per frame.

The coordinate conversion is a **local linear approximation**, not a real datum
transform. Doing OSGB36 → WGS84 properly means the OSTN15 shift grid, which is
1.5 MB and a bilinear interpolation — not something to reimplement in ZScript.
Over a single 400m tile the true transform is very nearly affine, so fitting a
plane costs four coefficients and lands within about 3cm. The fit is per tile,
not per region: one plane stretched across a county would drift badly.
"""

from __future__ import annotations

from . import UNITS_PER_METRE
from .tiles import Tile, osgb_to_lonlat

#: Lump and graphic names. Minimaps are numbered to match their map: MAP03's
#: minimap is DMMIN03, so the script derives the name instead of storing it.
MINIMAP = "DMMIN01"
#: Must match the class name in the template, and be registered in MAPINFO.
HANDLER = "PostcodeHUD"
ZSCRIPT_LUMP = "ZSCRIPT"

#: GZDoom feature level the script is written against.
ZSCRIPT_VERSION = "4.10"

#: How close to a tile boundary triggers the change, and how far inside the
#: neighbour you arrive. Both in map units; 96 is 3m, comfortably inside the
#: perimeter wall but far enough that you have to walk at it deliberately.
EDGE_UNITS = 96
REENTRY_UNITS = 192


def georeference(tile: Tile) -> dict[str, float]:
    """Fit lat/lon as a linear function of tile-local map units.

    Finite differences across the whole tile rather than a tiny epsilon: the
    span is what we care about being accurate over, and differencing over 400m
    of double-precision output is far better conditioned than over 1mm.
    """
    origin_e, origin_n = tile.origin
    span = float(tile.size_m)

    lon0, lat0 = osgb_to_lonlat(origin_e, origin_n)
    lon_e, lat_e = osgb_to_lonlat(origin_e + span, origin_n)
    lon_n, lat_n = osgb_to_lonlat(origin_e, origin_n + span)

    return {
        "lat0": lat0,
        "lon0": lon0,
        "lat_per_x": (lat_e - lat0) / span / UNITS_PER_METRE,
        "lat_per_y": (lat_n - lat0) / span / UNITS_PER_METRE,
        "lon_per_x": (lon_e - lon0) / span / UNITS_PER_METRE,
        "lon_per_y": (lon_n - lon0) / span / UNITS_PER_METRE,
    }


def _array(values, fmt: str = "{!r}") -> str:
    return ", ".join(fmt.format(v) for v in values)


def build_zscript(tiles: list[Tile], map_names: list[str]) -> str:
    """The ZSCRIPT lump for a one-tile map or a whole region.

    A single tile is just the degenerate case of a region with one entry, so
    there is only one code path to get wrong.
    """
    if len(tiles) != len(map_names):
        raise ValueError("need one map name per tile")

    geos = [georeference(t) for t in tiles]
    mid = geos[len(geos) // 2]

    # The script derives map names from the index, so the ordering here is
    # load-bearing: index i must be map_names[i]. Which of the two schemes is in
    # play is decided by the pack size, and both are parsed the same way in
    # ZScript: a fixed prefix then a fixed run of digits.
    from .region import map_name as _map_name

    count = len(tiles)
    for index, name in enumerate(map_names):
        expected = _map_name(index, count)
        if name != expected:
            raise ValueError(f"map {index} is {name}, expected {expected}")

    prefix = "".join(c for c in map_names[0] if not c.isdigit())
    digits = len(map_names[0]) - len(prefix)

    return _TEMPLATE.format(
        version=ZSCRIPT_VERSION,
        count=len(tiles),
        tile_units=float(tiles[0].size_units),
        edge=float(EDGE_UNITS),
        prefix=prefix,
        prefix_len=len(prefix),
        digits=digits,
        name_len=len(map_names[0]),
        reentry=float(REENTRY_UNITS),
        ix=_array(t.ix for t in tiles),
        iy=_array(t.iy for t in tiles),
        lat0=_array(g["lat0"] for g in geos),
        lon0=_array(g["lon0"] for g in geos),
        lat_px=repr(mid["lat_per_x"]),
        lat_py=repr(mid["lat_per_y"]),
        lon_px=repr(mid["lon_per_x"]),
        lon_py=repr(mid["lon_per_y"]),
    )


#: Notes on the awkward bits, since none are obvious from the code:
#:
#: * A StaticEventHandler is the only thing that survives a level change, which
#:   is why the transition hand-off lives on one.
#: * `RenderOverlay` runs in UI scope. `RenderEvent` hands us ViewPos and
#:   ViewAngle precisely so we do not have to reach into play scope for them.
#: * Doom angles run anticlockwise from east. Screen "up" is the way the player
#:   is facing, so a world bearing B sits at rotation (B - facing) anticlockwise
#:   from up, which is screen offset (-sin, -cos). North is B = 90.
#: * The minimap is north-up and never rotates. A rotating minimap is harder to
#:   read and would need the texture re-sampled; the compass carries the heading.
_TEMPLATE = '''\
version "{version}"

// Generated by postcode2wad. The arrays below are the region's tile table:
// grid indices, and each tile's georeference as a plane fitted across it.
// Index i corresponds to MAP(i+1), zero-padded to two digits.
class PostcodeHUD : StaticEventHandler
{{
    const TILE_COUNT  = {count};
    const TILE_UNITS  = {tile_units};
    const EDGE        = {edge};
    const REENTRY     = {reentry};

    // ChangeLevel flags, spelled out because the CHANGELEVEL_* constants are not
    // exposed to ZScript. Getting these wrong is quiet: passing 1 (KEEPFACING)
    // where 16 (NOINTERMISSION) was meant still changes level, but parks the
    // player on the "FINISHED" tally screen waiting for a keypress -- which
    // looks exactly like the transition having failed.
    const KEEP_FACING      = 1;
    const NO_INTERMISSION  = 16;
    const TRANSITION_FLAGS = KEEP_FACING | NO_INTERMISSION;

    static const int TILE_IX[]      = {{ {ix} }};
    static const int TILE_IY[]      = {{ {iy} }};
    static const double TILE_LAT0[] = {{ {lat0} }};
    static const double TILE_LON0[] = {{ {lon0} }};
    // Gradients are scalars, not per-tile arrays. They vary by 0.35% across a
    // 3km block, which is about 6cm over one tile -- far below what a five
    // decimal place readout resolves -- so the centre tile's values stand in for
    // all of them. Only the per-tile *origins* need to be tabulated.
    const LAT_PER_X = {lat_px};
    const LAT_PER_Y = {lat_py};
    const LON_PER_X = {lon_px};
    const LON_PER_Y = {lon_py};
    // Deliberately no String arrays here. The minimap name is derivable from the
    // tile index, and the place name is already in MAPINFO as the level title;
    // carrying either as a `static const String[]` would be one more ZScript
    // feature to be wrong about for no gain.

    const MARGIN      = 8;
    const PANEL_FRAC  = 0.26;   // minimap edge, as a fraction of screen height
    const MIN_PANEL   = 120;

    // Hand-off across a level change. Set when we decide to move, applied once
    // the destination has loaded.
    int pending;
    double pendingX, pendingY, pendingAngle;
    int cooldown;

    override void OnRegister()
    {{
        pending = -1;
    }}

    // ---- tile table -----------------------------------------------------

    // clearscope: called from WorldTick (play) *and* RenderOverlay (ui). Without
    // it the ui side fails with "Can't call play function ... from ui context",
    // and because `here` then never gets declared, every later use of it reports
    // as an unrelated unknown identifier.
    clearscope int TileIndexFor(String mapname)
    {{
        // "{prefix}0007" -> 6. Nothing else in the pack is named this way.
        if (mapname.Length() != {name_len}) return -1;
        int n = mapname.Mid({prefix_len}, {digits}).ToInt();
        if (n < 1 || n > TILE_COUNT) return -1;
        return n - 1;
    }}

    // Play-side only.
    String MapNameFor(int index)
    {{
        return String.Format("{prefix}%0{digits}d", index + 1);
    }}

    // ui-side only.
    ui String MinimapFor(int index)
    {{
        return String.Format("DMMIN%0{digits}d", index + 1);
    }}

    // Not static: a static function cannot see the class's own static const
    // arrays, which is what made TILE_IX read as an unknown identifier here.
    int FindTile(int ix, int iy)
    {{
        for (int i = 0; i < TILE_COUNT; i++)
        {{
            if (TILE_IX[i] == ix && TILE_IY[i] == iy) return i;
        }}
        return -1;   // outside the generated block
    }}

    // ---- edge transitions -----------------------------------------------

    override void WorldTick()
    {{
        if (cooldown > 0) {{ cooldown--; return; }}
        if (TILE_COUNT < 2) return;

        PlayerInfo p = players[consoleplayer];
        if (p == null || p.mo == null || p.mo.health <= 0) return;

        int here = TileIndexFor(level.MapName);
        if (here < 0) return;

        double x = p.mo.pos.x, y = p.mo.pos.y;
        int dx = 0, dy = 0;
        if (x < EDGE) dx = -1; else if (x > TILE_UNITS - EDGE) dx = 1;
        if (y < EDGE) dy = -1; else if (y > TILE_UNITS - EDGE) dy = 1;
        if (dx == 0 && dy == 0) return;

        int target = FindTile(TILE_IX[here] + dx, TILE_IY[here] + dy);
        if (target < 0) return;   // region boundary: the horizon wall stops you

        // Arrive just inside the far edge, keeping whichever coordinate is
        // parallel to the seam. Not a true continuation of the crossing -- that
        // needs the engine fork -- but it keeps you on the same street.
        pendingX = x; pendingY = y;
        if (dx > 0) pendingX = REENTRY; else if (dx < 0) pendingX = TILE_UNITS - REENTRY;
        if (dy > 0) pendingY = REENTRY; else if (dy < 0) pendingY = TILE_UNITS - REENTRY;
        pendingAngle = p.mo.angle;
        pending = target;

        cooldown = 35;
        level.ChangeLevel(MapNameFor(target), 0, TRANSITION_FLAGS, -1);
    }}

    override void WorldLoaded(WorldEvent e)
    {{
        if (pending < 0) return;

        PlayerInfo p = players[consoleplayer];
        if (p != null && p.mo != null)
        {{
            p.mo.SetOrigin((pendingX, pendingY, p.mo.pos.z), false);
            // Terrain heights are absolute across tiles so z is nearly right
            // already, but snap to the floor rather than trusting that.
            p.mo.FindFloorCeiling();
            p.mo.SetZ(p.mo.floorz);
            p.mo.angle = pendingAngle;
        }}
        pending = -1;
        cooldown = 35;
    }}

    // ---- overlay --------------------------------------------------------

    // A ring, as a coarse polygon. The overlay API has no circle primitive.
    ui void DrawRing(double cx, double cy, double radius, Color col, int steps)
    {{
        double prevx = cx + radius, prevy = cy;
        for (int i = 1; i <= steps; i++)
        {{
            double a = 360.0 * i / steps;
            double nx = cx + cos(a) * radius;
            double ny = cy - sin(a) * radius;
            Screen.DrawLine(int(prevx), int(prevy), int(nx), int(ny), col);
            prevx = nx; prevy = ny;
        }}
    }}

    // A filled-ish arrowhead, built from lines because ZScript has no polygon
    // fill in the overlay API.
    ui void DrawArrow(double cx, double cy, double facing, double size, Color col)
    {{
        // World (cos, sin) is east/north; the map is north-up, so screen y flips.
        double fx =  cos(facing), fy = -sin(facing);
        double sx = -fy,          sy =  fx;      // perpendicular

        double tipx = cx + fx * size,        tipy = cy + fy * size;
        double lx   = cx - fx * size * 0.6 + sx * size * 0.55;
        double ly   = cy - fy * size * 0.6 + sy * size * 0.55;
        double rx   = cx - fx * size * 0.6 - sx * size * 0.55;
        double ry   = cy - fy * size * 0.6 - sy * size * 0.55;

        Screen.DrawLine(int(tipx), int(tipy), int(lx), int(ly), col);
        Screen.DrawLine(int(tipx), int(tipy), int(rx), int(ry), col);
        Screen.DrawLine(int(lx), int(ly), int(cx), int(cy), col);
        Screen.DrawLine(int(rx), int(ry), int(cx), int(cy), col);
    }}

    // N/S and E/W rather than a minus sign: nobody reads "-0.31" as "west" at a
    // glance. Kept as a helper with a single two-argument String.Format, because
    // a four-argument one mixing %f and %s is what the ZScript parser rejected.
    ui String Hemi(double value, String positive, String negative)
    {{
        String suffix = positive;
        double magnitude = value;
        if (magnitude < 0)
        {{
            suffix = negative;
            magnitude = -magnitude;
        }}
        return String.Format("%.5f%s", magnitude, suffix);
    }}

    ui void DrawCompass(double cx, double cy, double radius, double facing)
    {{
        Color needle = Color(255, 240, 92, 92);
        DrawRing(cx, cy, radius, Color(230, 214, 218, 226), 24);

        // North, rotated into the player's frame. Up on screen is where they
        // are looking, so north sits at (90 - facing) anticlockwise from up.
        double r = 90.0 - facing;
        double nx = -sin(r), ny = -cos(r);
        Screen.DrawLine(int(cx), int(cy), int(cx + nx * radius * 0.86),
                        int(cy + ny * radius * 0.86), needle);
        Screen.DrawLine(int(cx - nx * radius * 0.5), int(cy - ny * radius * 0.5),
                        int(cx), int(cy), Color(160, 190, 195, 200));

        Screen.DrawText(smallfont, Font.CR_GOLD,
            int(cx + nx * radius * 1.32) - smallfont.StringWidth("N") / 2,
            int(cy + ny * radius * 1.32) - smallfont.GetHeight() / 2, "N");
    }}

    override void RenderOverlay(RenderEvent e)
    {{
        if (automapactive) return;

        int here = TileIndexFor(level.MapName);
        if (here < 0) return;

        // Not called `map`: that is ZScript's dictionary type and shadowing it
        // reports as "Unknown identifier 'Map'" several lines later.
        TextureID minimap = TexMan.CheckForTexture(MinimapFor(here), TexMan.Type_Any);
        if (!minimap.IsValid()) return;

        int sh = Screen.GetHeight();
        int panel = int(sh * PANEL_FRAC);
        if (panel < MIN_PANEL) panel = MIN_PANEL;

        int px = MARGIN;
        int py = MARGIN;
        double cx = px + panel * 0.5;
        double cy = py + panel * 0.5;
        double radius = panel * 0.5;

        // The disc is masked into the texture's alpha, so this draws round.
        Screen.DrawTexture(minimap, false, px, py,
            DTA_DestWidth, panel, DTA_DestHeight, panel,
            DTA_Alpha, 0.86);
        DrawRing(cx, cy, radius - 1, Color(220, 16, 18, 22), 48);
        DrawRing(cx, cy, radius - 2, Color(150, 210, 214, 220), 48);

        // Where we are, as a fraction of the tile. The minimap is north-up and
        // covers the tile exactly, so this is a straight scale.
        double fx = e.ViewPos.X / TILE_UNITS;
        double fy = e.ViewPos.Y / TILE_UNITS;
        if (fx >= 0.0 && fx <= 1.0 && fy >= 0.0 && fy <= 1.0)
        {{
            DrawArrow(px + fx * panel, py + (1.0 - fy) * panel,
                      e.ViewAngle, panel * 0.05, Color(255, 255, 80, 96));
        }}

        double lat = TILE_LAT0[here] + LAT_PER_X * e.ViewPos.X + LAT_PER_Y * e.ViewPos.Y;
        double lon = TILE_LON0[here] + LON_PER_X * e.ViewPos.X + LON_PER_Y * e.ViewPos.Y;

        String coords = Hemi(lat, "N", "S") .. "  " .. Hemi(lon, "E", "W");
        // MAPINFO already carries the place name as the level title.
        String label = level.LevelName;

        int fh = smallfont.GetHeight();
        double crad = panel * 0.13;

        // Everything below the disc gets a dark plate behind it. Without one the
        // compass and the readout are thin light lines over a bright daylight
        // scene, and simply cannot be read.
        int blockTop = py + panel + 3;
        int labelH = 0;
        if (label.Length() > 0) {{ labelH = fh + 2; }}
        int blockH = int(crad * 2 + 10 + fh + 4 + labelH);
        Screen.Dim(0x0C1014, 0.55, px, blockTop, panel, blockH);

        double ccy = blockTop + 5 + crad;
        DrawCompass(cx, ccy, crad, e.ViewAngle);

        int ty = int(ccy + crad + 6);
        Screen.DrawText(smallfont, Font.CR_WHITE,
            int(cx) - smallfont.StringWidth(coords) / 2, ty, coords);

        if (label.Length() > 0)
        {{
            Screen.DrawText(smallfont, Font.CR_SAPPHIRE,
                int(cx) - smallfont.StringWidth(label) / 2,
                ty + fh + 2, label);
        }}
    }}
}}
'''
