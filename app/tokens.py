"""Design tokens — single source of truth for the CAPTCHA Lab.

Tone: technical / dense (security lab console).
Palette: cool-tinted neutrals + one signal accent (amber) + semantic set.
Type: grotesk (body) + mono (data/labels). Max 2 families.
"""

# --- primitive: color ------------------------------------------------------
NEUTRAL_0 = "#fbfbfa"   # page surface (warm-cool near-white, not pure #fff)
NEUTRAL_50 = "#f3f3f1"  # panel
NEUTRAL_100 = "#e8e8e4"  # hairline / track
NEUTRAL_200 = "#d6d6d1"  # border
NEUTRAL_500 = "#8b8b85"  # muted text
NEUTRAL_700 = "#4a4a46"  # secondary text
NEUTRAL_900 = "#1b1b19"  # primary text
NEUTRAL_950 = "#121211"  # ink panel / dark surface

AMBER_500 = "#d97706"   # signal accent
AMBER_600 = "#b45309"   # accent hover
AMBER_100 = "#fdf1dc"   # accent wash

GREEN_600 = "#15803d"   # semantic success
GREEN_100 = "#e6f4ea"
RED_600 = "#b91c1c"     # semantic danger
RED_100 = "#fbeaea"
BLUE_600 = "#1d4ed8"    # semantic info
BLUE_100 = "#e8eefc"

# --- primitive: type --------------------------------------------------------
FONT_SANS = "'Liberation Sans','DejaVu Sans',system-ui,-apple-system,sans-serif"
FONT_MONO = "'Liberation Mono','DejaVu Sans Mono',ui-monospace,monospace"

# modular scale 12 / 14 / 16 / 20 / 24 / 32 / 48
TEXT_SM = "12px"
TEXT_BASE = "14px"
TEXT_MD = "16px"
TEXT_LG = "20px"
TEXT_XL = "24px"
TEXT_2XL = "32px"

# --- primitive: space (4/8 base) --------------------------------------------
SPACE_1 = "4px"
SPACE_2 = "8px"
SPACE_3 = "12px"
SPACE_4 = "16px"
SPACE_6 = "24px"
SPACE_8 = "32px"
SPACE_12 = "48px"

# --- primitive: radius (one family) -----------------------------------------
RADIUS_1 = "4px"
RADIUS_2 = "8px"

# --- primitive: motion ------------------------------------------------------
DUR_FAST = "120ms"
DUR_BASE = "220ms"
EASE_OUT = "cubic-bezier(.2,.8,.2,1)"

# --- primitive: shadow (max 3 levels) ---------------------------------------
SHADOW_RAISED = "0 1px 2px rgb(27 27 25 / .06), 0 2px 8px rgb(27 27 25 / .04)"
SHADOW_OVERLAY = "0 8px 24px rgb(27 27 25 / .12)"
SHADOW_INSET = "inset 0 1px 0 rgb(255 255 255 / .04)"

# --- canvas / renderer defaults ---------------------------------------------
CANVAS_W = 320
CANVAS_H = 160
PIECE_SIZE = 46          # slider puzzle piece edge (px)
PIECE_MARGIN_X = 8       # keep piece fully inside the canvas
TRACK_TOLERANCE = 6      # px tolerance around the true gap x
TRACK_TOLERANCE_Y = 8    # px tolerance around the true gap y
PIECE_JITTER = 7         # visual noise offset applied to the rendered gap
MIN_DRAG_DISTANCE = 12   # below this a drag is a click, not a solve attempt


def hexrgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
