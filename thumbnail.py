"""
thumbnail.py - HTML+CSS+Playwright thumbnail renderer.

Alur:
  1. Background image (extracted frame) -> base64 inline
  2. Twemoji SVG -> download on-the-fly dari GitHub -> inline <svg>
  3. Google Font via <link href=...> (no local file)
  4. Style (font, color, shadow, position) -> dari LLM JSON
  5. Playwright Chromium -> screenshot -> PIL Image -> save JPG

Dependencies: playwright (Chromium binary auto-downloaded by `playwright install`)
"""
import base64
import pathlib
import re as _re
import requests
import os
from PIL import Image, ImageDraw, ImageFont

EMOJI_PATTERN = _re.compile(
    r'([\U0001F300-\U0001FAFF\U00002600-\U000027BF\u200D\uFE0F])'
)
TWEMOJI_BASE = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/svg"
EMOJI_CACHE_DIR = pathlib.Path(os.environ.get("TWEMOJI_CACHE", "")) or pathlib.Path(tempfile_dir() if (tempfile_dir := os.environ.get("TMPDIR", "/tmp")) else "/tmp") / "twemoji_cache"
EMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _unicode_to_codepoint(emoji_char):
    """Convert unicode emoji ke Twemoji codepoint string.
    Example: '🔥' -> '1f525', '❤️' -> '2764', '👨‍💻' (with ZWJ) -> '1f468-200d-1f4bb'
    """
    cps = []
    for ch in emoji_char:
        cp = f"{ord(ch):x}"
        # Strip variation selector (FE0F) — Twemoji biasanya skip itu
        if cp != "fe0f":
            cps.append(cp)
    return "-".join(cps)


def _fetch_twemoji_svg(emoji_char):
    """Download Twemoji SVG (cached lokal) dan return path ke file SVG.
    Returns None kalau gagal download.
    """
    cp = _unicode_to_codepoint(emoji_char)
    fp = EMOJI_CACHE_DIR / f"{cp}.svg"
    if fp.exists() and fp.stat().st_size > 100:
        return fp
    url = f"{TWEMOJI_BASE}/{cp}.svg"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok and b"<svg" in r.content[:200]:
            fp.write_bytes(r.content)
            return fp
    except Exception:
        pass
    return None


def _replace_emoji_in_text(text, emoji_size_px=120):
    """Replace emoji unicode di text jadi inline <img> Twemoji SVG.
    Return list of (kind, content) tuples:
      ('text', 'Kamu gak bakal ')
      ('emoji', '<svg>...</svg>')
      ('text', ' percaya!')
    """
    parts = []
    cursor = 0
    for m in EMOJI_PATTERN.finditer(text):
        # text before emoji
        if m.start() > cursor:
            parts.append(("text", text[cursor:m.start()]))
        emoji_char = m.group(0)
        svg_path = _fetch_twemoji_svg(emoji_char)
        if svg_path:
            svg_text = svg_path.read_text(encoding="utf-8")
            # Inject explicit width/height into <svg> tag
            svg_text = _re.sub(
                r'<svg ',
                f'<svg width="{emoji_size_px}" height="{emoji_size_px}" style="display:inline-block;vertical-align:middle" ',
                svg_text, count=1)
            # Strip xml declaration if any
            svg_text = _re.sub(r'<\?xml[^>]+\?>\s*', '', svg_text)
            parts.append(("emoji", svg_text))
        else:
            # Fallback: keep unicode (akan render sebagai text biasa)
            parts.append(("text", emoji_char))
        cursor = m.end()
    if cursor < len(text):
        parts.append(("text", text[cursor:]))
    return parts


def _image_to_base64(image_path):
    """Read image file, return base64 string (without data: prefix)."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def build_thumbnail_html(
    bg_image_path,
    hook_text,
    width=1080,
    height=1920,
    font_family="Bebas Neue",
    font_size=140,
    text_color="#FFE600",
    text_shadow="0 0 0 #000, 4px 4px 0 #000, -4px -4px 0 #000, 4px -4px 0 #000, -4px 4px 0 #000, 0 4px 0 #000, 0 -4px 0 #000, 4px 0 0 #000, -4px 0 0 #000",
    gradient="linear-gradient(to bottom, rgba(0,0,0,0.0) 0%, rgba(0,0,0,0.3) 40%, rgba(0,0,0,0.6) 100%)",
    text_align="center",
    vertical_position="center",  # "top" | "center" | "bottom"
    emoji_size=140,
    line_height=1.1,
    max_width_pct=85,
):
    """Build HTML string untuk thumbnail. Position: top-center, text-shadow,
    Google Font, inline Twemoji SVG. Return HTML string.
    """
    # Vertical position
    if vertical_position == "top":
        container_align = "flex-start"
        padding_top = "12%"
    elif vertical_position == "bottom":
        container_align = "flex-end"
        padding_top = "0"
    else:  # center
        container_align = "center"
        padding_top = "0"

    # Build hook_text HTML (mix text + emoji)
    parts = _replace_emoji_in_text(hook_text, emoji_size_px=emoji_size)
    hook_html_parts = []
    for kind, content in parts:
        if kind == "text":
            # Escape HTML special chars
            escaped = (content.replace("&", "&amp;")
                              .replace("<", "&lt;")
                              .replace(">", "&gt;")
                              .replace("\n", "<br>"))
            hook_html_parts.append(f'<span class="txt">{escaped}</span>')
        else:
            # Inline SVG
            hook_html_parts.append(f'<span class="emoji">{content}</span>')
    hook_html = "".join(hook_html_parts)

    # Google Font URL — encode font name for spaces/specials
    font_name_url = font_family.replace(" ", "+")
    font_url = f"https://fonts.googleapis.com/css2?family={font_name_url}:wght@400;700;900&display=swap"

    # Background image base64
    bg_b64 = _image_to_base64(bg_image_path)
    bg_data_url = f"data:image/jpeg;base64,{bg_b64}"

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{font_url}" rel="stylesheet">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{ width: {width}px; height: {height}px; overflow: hidden; background: #000; }}
  .thumbnail {{
    position: relative; width: 100%; height: 100%;
    font-family: '{font_family}', sans-serif;
    display: flex; align-items: {container_align}; justify-content: center;
    padding-top: {padding_top};
  }}
  .bg {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%;
         object-fit: cover; z-index: 0; }}
  .gradient {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%;
               background: {gradient}; z-index: 1; }}
  .hook {{
    position: relative; z-index: 2;
    font-size: {font_size}px; font-weight: 900; text-align: {text_align};
    color: {text_color};
    text-shadow: {text_shadow};
    line-height: {line_height};
    max-width: {max_width_pct}%;
    padding: 0 5%;
  }}
  .hook .emoji {{ display: inline-block; vertical-align: middle; }}
  .hook .txt {{ display: inline; }}
</style>
</head>
<body>
<div class="thumbnail">
  <img class="bg" src="{bg_data_url}">
  <div class="gradient"></div>
  <h1 class="hook">{hook_html}</h1>
</div>
</body>
</html>"""
    return html


def render_html_to_image(html, width=1080, height=1920, out_path=None):
    """Render HTML via Playwright Chromium. Return PIL Image.
    Playwright sync, headless. Slow (~2-5s) but precise CSS.

    Falls back ke pure-PIL renderer kalau Chromium binary belum terinstall
    (mis. CI runner tanpa playwright install-deps). Slower ~50ms tapi gak crash.
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": width, "height": height},
                                           device_scale_factor=1)
            page = context.new_page()
            page.set_content(html, wait_until="networkidle", timeout=15000)
            # Wait for Google Fonts to load
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(500)
            # Screenshot
            png_bytes = page.screenshot(type="png", full_page=False, omit_background=False)
            browser.close()
        import io as _io
        img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
    except Exception as e:
        # Playwright unavailable (no chromium binary, network blocked, etc).
        # Fallback: parse the HTML minimally and render via PIL only.
        import logging
        logging.warning(f"[thumbnail] playwright fail ({e.__class__.__name__}: {str(e)[:100]}), using PIL fallback")
        img = _render_pil_fallback(html, width, height)
    if out_path:
        img.save(out_path, "JPEG", quality=92)
    return img


def _render_pil_fallback(html, width=1080, height=1920):
    """PIL-only fallback: extract background from <img class="bg"> data URL,
    overlay text from .hook (plain text only, no emoji rendering).

    Quality rendah vs Chromium, tapi pipeline tetap jalan. Cuma dipakai
    kalau playwright unavailable (cold-start CI, network block, dll).
    """
    from io import BytesIO
    # Extract background image
    bg_match = _re.search(r'<img class="bg" src="data:image/[^;]+;base64,([^"]+)"', html)
    if bg_match:
        bg_data = base64.b64decode(bg_match.group(1))
        bg = Image.open(BytesIO(bg_data)).convert("RGB")
        img = bg.resize((width, height), Image.LANCZOS)
    else:
        img = Image.new("RGB", (width, height), "#000000")
    # Add dark gradient overlay
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for y in range(int(height * 0.4), height):
        alpha = int(160 * (y - height * 0.4) / (height * 0.6))
        for x in range(width):
            overlay.putpixel((x, y), (0, 0, 0, alpha))
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)
    # Extract hook text (strip HTML)
    hook_match = _re.search(r'<h1 class="hook">(.*?)</h1>', html, _re.DOTALL)
    if hook_match:
        # Strip tags, keep text + emoji
        text = _re.sub(r'<[^>]+>', '', hook_match.group(1))
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        # Try to find a default font
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 120)
        except Exception:
            from PIL import ImageFont
            font = ImageFont.load_default()
        # Wrap text
        draw = ImageDraw.Draw(img)
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > int(width * 0.85):
                lines.append(cur)
                cur = w
            else:
                cur = test
        if cur:
            lines.append(cur)
        # Draw lines centered vertically
        y_start = (height - len(lines) * 130) // 2
        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
            x = (width - tw) // 2
            y = y_start + i * 130
            # Stroke (black outline)
            for dx, dy in [(-4, -4), (-4, 4), (4, -4), (4, 4), (0, -4), (0, 4), (-4, 0), (4, 0)]:
                draw.text((x + dx, y + dy), line, font=font, fill="black")
            # Text fill (yellow from CSS default)
            draw.text((x, y), line, font=font, fill="#FFE600")
    return img.convert("RGB")


def gen_thumbnail_html(bg_image_path, hook_text, out_path, **style_kwargs):
    """One-shot: build HTML + render + save JPG. Default style."""
    html = build_thumbnail_html(bg_image_path, hook_text, **style_kwargs)
    return render_html_to_image(html, out_path=out_path, width=1080, height=1920)
