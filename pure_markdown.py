import math
import os
import re
import struct
import sublime
import sublime_plugin
import traceback
import webbrowser

try:
    from urllib.parse import quote as _quote
except ImportError:
    from urllib import quote as _quote

try:
    import html as _html

    def _escape(s):
        return _html.escape(s, quote=False)
except ImportError:
    import cgi as _cgi

    def _escape(s):
        return _cgi.escape(s, quote=False)


SETTINGS_FILE = 'PureMarkdownPreview.sublime-settings'
PHANTOM_KEY = 'pure_markdown_images'
PREVIEW_PHANTOM_KEY = 'pure_markdown_preview'
REGION_RED = 'pure_markdown_red'
REGION_GREEN = 'pure_markdown_green'

IMG_RE = re.compile(r'<img\s+[^>]*?src="([^"]+)"[^>]*?>', re.IGNORECASE)
TAG_RE = re.compile(r'(<img\s+[^>]*?>|<\s*span[^>]*>|<\s*/\s*span\s*>)', re.IGNORECASE)
SPAN_RE = re.compile(
    r'<span\s+[^>]*?color\s*:\s*(red|green)[^>]*?>(.*?)</\s*span\s*>',
    re.IGNORECASE | re.DOTALL)
LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*\S)\s*$')
HR_RE = re.compile(r'^\s*(---|\*\*\*|___)\s*$')
TAG_SRC_RE = re.compile(r'src="[^"]*"', re.IGNORECASE)

_pending = set()
# Keep PhantomSet refs alive: a collected set removes its phantoms
_phantom_sets = {}
_preview_sets = {}
# image path -> ((mtime, size), (width, height) or None)
_img_sizes = {}
# view id -> viewport width the tail padding was computed for
_preview_width = {}


def _settings():
    return sublime.load_settings(SETTINGS_FILE)


def is_markdown_view(view):
    if view is None or not view.is_valid() or view.is_scratch():
        return False
    name = view.file_name() or ''
    return name.lower().endswith(('.md', '.markdown', '.mdown'))


def file_to_url(path):
    p = os.path.abspath(path).replace('\\', '/')
    url_path = _quote(p, safe='/:')
    if not url_path.startswith('/'):
        url_path = '/' + url_path
    return 'file://' + url_path


def resolve_src(src, base_dir):
    s = (src or '').strip()
    low = s.lower()
    if low.startswith(('http://', 'https://', 'data:', 'file://', 'res://', 'subl:')):
        return s
    if not base_dir:
        return s
    # strip query-ish leading ./ and normalize
    abs_path = os.path.normpath(os.path.join(base_dir, s))
    if os.path.isfile(abs_path):
        return file_to_url(abs_path)
    return s


def _resolve_img_tag(tag, base_dir):
    m = re.search(r'src="([^"]+)"', tag, re.IGNORECASE)
    if not m:
        return tag
    url = resolve_src(m.group(1), base_dir)
    return TAG_SRC_RE.sub('src="' + url + '"', tag, count=1)


def _inline_format(text):
    links = []

    def _link_sub(m):
        links.append('<a href="' + m.group(2) + '">' + _escape(m.group(1)) + '</a>')
        return '@@MDLINK' + str(len(links) - 1) + '@@MDLINK'

    s = LINK_RE.sub(_link_sub, text)
    s = _escape(s)
    s = BOLD_RE.sub(r'<strong>\1</strong>', s)
    for i, html in enumerate(links):
        s = s.replace('@@MDLINK' + str(i) + '@@MDLINK', html)
    return s


def format_mixed(line, base_dir):
    parts = TAG_RE.split(line)
    out = []
    for p in parts:
        if not p:
            continue
        low = p.lower()
        if low.startswith('<img'):
            out.append(_resolve_img_tag(p, base_dir))
        elif low.startswith('<span') or low.startswith('</span'):
            out.append(p)
        else:
            out.append(_inline_format(p))
    return ''.join(out)


def convert_markdown(md_text, base_dir, img_height=150, width=0):
    html_lines = []
    for raw in md_text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if HR_RE.match(line):
            # minihtml has no <hr>: bordered div instead (border-top is supported)
            html_lines.append('<div class="rule"></div>')
            continue
        mh = HEADING_RE.match(line.strip())
        if mh:
            level = len(mh.group(1))
            inner = format_mixed(mh.group(2), base_dir)
            html_lines.append('<h{0}>{1}</h{0}>'.format(level, inner))
            continue
        if IMG_RE.search(line):
            # image-only (or image-led) row: keep tags, no <p> wrapper
            for row in _split_image_line(line, base_dir, img_height, width):
                html_lines.append('<div class="imgs">' + format_mixed(row, base_dir) + '</div>')
            continue
        html_lines.append('<p>' + format_mixed(line, base_dir) + '</p>')
    return '\n'.join(html_lines)


# minihtml fragment shown in a Sublime scratch view (VS Code preview equivalent).
# Only a CSS subset works here: plain colors, no flexbox. Images flow inline
# and wrap, which visually matches the VS Code grid for photo rows.
MINIHTML_TEMPLATE = """<style>
div.mdprev {{ font-size: 14px; color: {fg}; }}
h1 {{ font-size: 1.6em; }}
h2 {{ font-size: 1.4em; }}
h3 {{ color: #f0b429; font-size: 1.2em; }}
h3 a {{ color: #f0b429; }}
a {{ color: #4da3ff; }}
div.rule {{ border-top: 1px solid #555555; margin-top: 16px; margin-bottom: 16px; }}
/* minihtml measures an image row by the height attribute and paints its
   images at the Windows DPI scale. Rows are split at the painted width (see
   _split_image_line) so the measurement and the paint wrap alike; this
   padding only absorbs the rounding the paint adds on top of that, and
   covers an image too wide for the window (see tail_padding). */
div.mdprev {{ padding-bottom: {pad}px; }}
</style>
<div class="mdprev">{body}</div>
"""

TAIL_PADDING_MIN = 200
TAIL_PADDING_MARGIN = 24
# spacing minihtml puts between two images of the same row, and the width
# assumed for an image whose file cannot be measured
IMAGE_GAP = 4.0
IMAGE_WIDTH_GUESS = 1.25
# share of the window width an image row may use when painted; the rest is
# slack for the DPI scaling and the gap rounding
IMAGE_FIT_RATIO = 0.95

_dpi_scale = None


def _paint_scale():
    # minihtml computes the phantom height in 96-DPI units but paints at the
    # system DPI scale, so the painted content is taller than the measured
    # box by (scale - 1) x its height. The scale comes from the Windows DPI
    # setting; everywhere else the measured and painted box agree.
    global _dpi_scale
    if _dpi_scale is not None:
        return _dpi_scale
    scale = 1.0
    try:
        if sublime.platform() == 'windows':
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r'Control Panel\Desktop\WindowMetrics')
            try:
                dpi = int(winreg.QueryValueEx(key, 'AppliedDPI')[0])
            finally:
                key.Close()
            if 72 <= dpi <= 480:
                scale = dpi / 96.0
    except Exception:
        scale = 1.0
    _dpi_scale = scale
    return scale


def _jpeg_size(fp):
    fp.seek(2)
    while True:
        b = fp.read(1)
        while b and b != b'\xff':
            b = fp.read(1)
        while b == b'\xff':
            b = fp.read(1)
        if not b:
            return None
        marker = b[0]
        if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            continue
        seg = fp.read(2)
        if len(seg) < 2:
            return None
        seg = struct.unpack('>H', seg)[0]
        if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
            data = fp.read(5)
            if len(data) < 5:
                return None
            h, w = struct.unpack('>HH', data[1:5])
            return (w, h)
        fp.seek(seg - 2, 1)


def _img_size(path):
    # intrinsic pixel size of a local image. minihtml sizes an <img> that
    # only carries a height by the aspect ratio, so the wrap simulation
    # below needs the real one
    try:
        st = os.stat(path)
    except Exception:
        return None
    key = (st.st_mtime, st.st_size)
    hit = _img_sizes.get(path)
    if hit and hit[0] == key:
        return hit[1]
    size = None
    try:
        with open(path, 'rb') as f:
            head = f.read(32)
            if head[:8] == b'\x89PNG\r\n\x1a\n':
                size = struct.unpack('>II', head[16:24])
            elif head[:6] in (b'GIF87a', b'GIF89a'):
                size = struct.unpack('<HH', head[6:10])
            elif head[:2] == b'BM':
                w, h = struct.unpack('<ii', head[18:26])
                size = (w, abs(h))
            elif head[:4] == b'RIFF' and head[8:12] == b'WEBP' \
                    and head[12:16] == b'VP8X':
                size = (1 + int.from_bytes(head[24:27], 'little'),
                        1 + int.from_bytes(head[27:30], 'little'))
            elif head[:2] == b'\xff\xd8':
                size = _jpeg_size(f)
    except Exception:
        size = None
    if not size or size[0] <= 0 or size[1] <= 0:
        size = None
    _img_sizes[path] = (key, size)
    return size


def _local_img_path(src, base_dir):
    s = (src or '').strip()
    low = s.lower()
    if not s or low.startswith(('http://', 'https://', 'data:', 'res://', 'subl:')):
        return None
    if low.startswith('file://'):
        s = s[7:]
        if re.match(r'^/[A-Za-z]:', s):
            s = s[1:]
    if not os.path.isabs(s):
        if not base_dir:
            return None
        s = os.path.join(base_dir, s)
    return os.path.normpath(s)


def _image_widths(line, base_dir, img_height):
    # width minihtml gives each image of one markdown line, in layout pixels
    widths = []
    for tag in re.findall(r'<img\s+[^>]*?>', line, re.IGNORECASE):
        mh = re.search(r'height="(\d+)"', tag, re.IGNORECASE)
        height = int(mh.group(1)) if mh else img_height
        ms = re.search(r'src="([^"]+)"', tag, re.IGNORECASE)
        size = None
        path = _local_img_path(ms.group(1), base_dir) if ms else None
        if path:
            size = _img_size(path)
        if size:
            widths.append(height * size[0] / float(size[1]))
        else:
            # unknown size: assume a wide image so an extra wrapped line is
            # padded rather than clipped
            widths.append(height * IMAGE_WIDTH_GUESS)
    return widths


def _image_painted_width(tag, base_dir, img_height):
    # width minihtml paints an image at. A tag without a width attribute is
    # laid out at the intrinsic pixel width of the file (the height
    # attribute only sets the height), and painted at the Windows DPI scale
    mw = re.search(r'width="(\d+)"', tag, re.IGNORECASE)
    size = None
    ms = re.search(r'src="([^"]+)"', tag, re.IGNORECASE)
    path = _local_img_path(ms.group(1), base_dir) if ms else None
    if path:
        size = _img_size(path)
    mh = re.search(r'height="(\d+)"', tag, re.IGNORECASE)
    height = int(mh.group(1)) if mh else img_height
    if mw:
        width = int(mw.group(1))
    elif size:
        width = float(size[0])
    else:
        # unknown size: assume a wide image, so the row is split earlier
        width = height * IMAGE_WIDTH_GUESS
    return width * max(1.0, _paint_scale())


def _split_image_line(line, base_dir, img_height, width):
    # minihtml measures a row by the height attribute but paints the images
    # at the DPI scale, so a row that fits the measured width wraps again
    # when painted: that extra painted line is outside the measured box and
    # pushes the end of the document out of reach. Splitting the row at the
    # painted width makes the measurement and the paint wrap alike.
    tags = re.findall(r'<img\s+[^>]*?>', line, re.IGNORECASE)
    if len(tags) < 2 or width <= 0:
        return [line]
    if re.sub(r'<img\s+[^>]*?>', '', line, flags=re.IGNORECASE).strip():
        return [line]
    avail = width * IMAGE_FIT_RATIO
    rows, cur, used = [], [], 0.0
    for tag in tags:
        w = _image_painted_width(tag, base_dir, img_height)
        if cur and used + w + IMAGE_GAP > avail:
            rows.append(cur)
            cur, used = [tag], w
        else:
            used += w + IMAGE_GAP if cur else w
            cur.append(tag)
    if cur:
        rows.append(cur)
    return [' '.join(row) for row in rows]


def tail_padding(md_text, base_dir, width):
    # rows are split at the painted width, so the paint no longer adds lines
    # the measurement does not have. Only a single image wider than the
    # window still overflows; pad that, and keep a small margin as the tail.
    img_h = _img_height()
    scale = max(1.0, _paint_scale())
    avail = width * IMAGE_FIT_RATIO if width > 0 else 0.0
    over = 0
    for raw in md_text.splitlines():
        if not raw.strip() or not IMG_RE.search(raw):
            continue
        tags = re.findall(r'<img\s+[^>]*?>', raw, re.IGNORECASE)
        for tag in tags:
            w = _image_painted_width(tag, base_dir, img_h)
            if avail <= 0 or w > avail:
                over += img_h
    return int(max(TAIL_PADDING_MIN,
                   math.ceil(over * (scale - 1.0)) + TAIL_PADDING_MARGIN))


def build_minihtml(md_text, base_dir, fg='#d4d4d4', width=0):
    body = convert_markdown(md_text, base_dir, _img_height(), width)
    return MINIHTML_TEMPLATE.format(body=body, fg=fg,
                                    pad=tail_padding(md_text, base_dir, width))


def _on_preview_navigate(href):
    if href and href.lower().startswith(('http://', 'https://')):
        try:
            webbrowser.open(href)
        except Exception:
            pass


# source view id -> saved view state while in-place preview is on
_preview_state = {}


def _is_in_preview(view):
    try:
        return bool(view.settings().get('markdown_preview_mode', False))
    except Exception:
        return False


GENERATED_SCHEME = 'PureMarkdownPreviewHiddenCaret.generated.sublime-color-scheme'
GENERATED_SCHEME_PATH = 'Packages/User/' + GENERATED_SCHEME
_last_scheme_colors = (None, None)


def _write_preview_scheme(bg, fg):
    # buffer text fully invisible; the source below the phantom is folded,
    # so fold markers are painted in the background color too; phantom HTML
    # carries its own explicit colors
    global _last_scheme_colors
    if (bg, fg) == _last_scheme_colors:
        return
    try:
        path = os.path.join(sublime.packages_path(), 'User', GENERATED_SCHEME)
        g = []
        g.append('\t\t"background": "' + bg + '",')
        g.append('\t\t"foreground": "' + bg + '",')
        for key in ('invisibles', 'line_highlight', 'selection',
                    'selection_foreground', 'selection_border',
                    'find_highlight', 'find_highlight_foreground',
                    'highlight', 'highlight_foreground',
                    'guide', 'active_guide', 'stack_guide',
                    'brackets_foreground', 'tags_foreground',
                    'fold_marker', 'misspelling', 'minimap_border',
                    'accent',
                    'inert_gutter_foreground'):
            g.append('\t\t"' + key + '": "' + bg + '",')
        content = ('{\n\t"name": "Markdown Preview (invisible buffer, generated)",\n'
                   '\t"globals": {\n' + '\n'.join(g) + '\n'
                   '\t},\n\t"rules": [\n\t]\n}\n')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        _last_scheme_colors = (bg, fg)
    except Exception:
        pass


def _preview_colors(src_view):
    # while the generated scheme is active, style() reports fg==bg, which
    # would paint the default phantom text in the background color; bg is
    # still correct there (it is what the generated scheme carries)
    bg, fg = '#343d46', '#d4d4d4'
    try:
        st = src_view.style()
        bg = st.get('background', bg) or bg
        if src_view.settings().get('color_scheme') == GENERATED_SCHEME_PATH:
            return bg, fg
        fg = st.get('foreground', fg) or fg
    except Exception:
        pass
    return bg, fg


def _render_preview_html(view):
    base = _base_dir(view)
    md_text = view.substr(sublime.Region(0, view.size()))
    # saved on enter_preview: view settings survive an ST restart, unlike
    # module state, and the view is already on the generated scheme then
    fg = view.settings().get('pure_md_preview_fg') or _preview_colors(view)[1]
    try:
        width = int(view.viewport_extent()[0])
    except Exception:
        width = 0
    _preview_width[view.id()] = width
    return build_minihtml(md_text, base, fg, width)


def _apply_preview_settings(view):
    # buffer text is invisible via the generated scheme (fold markers
    # painted in the background color as well), phantom carries its own
    # colors. line_height floors at ~4px no matter how small font_size
    # gets, so the hidden buffer below the phantom would still add ~760px
    # of scrollable dead space; folding it collapses the layout instead.
    # scroll_past_end off removes the overshoot past the layout end.
    st = view.settings()
    st.set('markdown_preview_mode', True)
    st.set('color_scheme', GENERATED_SCHEME_PATH)
    st.set('font_size', 2)
    st.set('scroll_past_end', False)
    st.set('gutter', False)
    view.set_read_only(True)
    try:
        view.sel().clear()
        view.sel().add(sublime.Region(0))
        if view.size() > 0:
            for r in view.folded_regions():
                view.unfold(r)
            view.fold(sublime.Region(view.text_point(1, 0), view.size()))
        view.show(0)
    except Exception:
        pass


def _update_preview_phantom(view, html):
    pset = sublime.PhantomSet(view, PREVIEW_PHANTOM_KEY)
    pset.update([sublime.Phantom(
        sublime.Region(0), html, sublime.LAYOUT_BLOCK,
        _on_preview_navigate)])
    _preview_sets[view.id()] = pset


def enter_preview(view):
    if _is_in_preview(view):
        return True
    try:
        html = _render_preview_html(view)
    except Exception as e:
        traceback.print_exc()
        sublime.error_message('Markdown preview: cannot render ({0})'.format(e))
        return False
    st = view.settings()
    _preview_state[view.id()] = {
        'sel': [(r.a, r.b) for r in view.sel()],
        'color_scheme': st.get('color_scheme'),
        'font_size': st.get('font_size'),
        'gutter': st.get('gutter'),
        'scroll_past_end': st.get('scroll_past_end'),
    }
    clear_view(view)
    try:
        bg, fg = _preview_colors(view)
        _write_preview_scheme(bg, fg)
        st.set('pure_md_preview_fg', fg)
        _apply_preview_settings(view)
        _update_preview_phantom(view, html)
    except Exception:
        traceback.print_exc()
        exit_preview(view)
        return False
    sublime.status_message('Markdown preview on')
    return True


def exit_preview(view):
    st = view.settings()
    try:
        st.set('markdown_preview_mode', False)
    except Exception:
        pass
    try:
        sublime.PhantomSet(view, PREVIEW_PHANTOM_KEY).update([])
    except Exception:
        pass
    _preview_sets.pop(view.id(), None)
    _preview_width.pop(view.id(), None)
    try:
        for r in view.folded_regions():
            view.unfold(r)
    except Exception:
        pass
    state = _preview_state.pop(view.id(), {})
    try:
        for key in ('color_scheme', 'font_size', 'gutter', 'scroll_past_end'):
            if state.get(key) is not None:
                st.set(key, state[key])
            else:
                st.erase(key)
        st.erase('pure_md_preview_fg')
        if view.is_read_only():
            view.set_read_only(False)
    except Exception:
        pass
    try:
        view.sel().clear()
        for a, b in state.get('sel', [(0, 0)]):
            view.sel().add(sublime.Region(a, b))
    except Exception:
        pass
    if is_markdown_view(view):
        update_view_phantoms(view)
    sublime.status_message('Markdown preview off')


def _refit_preview_width(view):
    # a window resize re-wraps the painted image rows without any ST event
    # firing, which invalidates the padding; re-render once, holding the
    # scroll position so the view does not jump to the top
    vid = view.id()
    try:
        width = int(view.viewport_extent()[0])
    except Exception:
        return
    if width <= 0:
        return
    old = _preview_width.get(vid)
    if old is None or abs(width - old) < 20:
        return
    try:
        pos = view.viewport_position()
        _update_preview_phantom(view, _render_preview_html(view))
    except Exception:
        traceback.print_exc()
        return
    sublime.set_timeout(lambda: _hold_viewport(view, pos), 100)


def _hold_viewport(view, pos):
    try:
        if view.is_valid() and _is_in_preview(view):
            end = max(0.0, view.layout_extent()[1] - view.viewport_extent()[1])
            view.set_viewport_position((pos[0], min(pos[1], end)), False)
    except Exception:
        traceback.print_exc()


def refresh_inplace_preview(view):
    if not _is_in_preview(view):
        return False
    try:
        _update_preview_phantom(view, _render_preview_html(view))
    except Exception:
        return False
    return True


def _resume_preview_if_needed(view):
    # after an ST restart the session restores preview-mode view settings
    # (generated scheme, flags, folds) but phantoms live only in memory:
    # rebuild the phantom, otherwise the view comes back empty
    try:
        if view is None or not view.is_valid():
            return False
        st = view.settings()
        in_preview = bool(st.get('markdown_preview_mode', False)) or (
            st.get('color_scheme') == GENERATED_SCHEME_PATH)
        if not in_preview or _preview_sets.get(view.id()) is not None:
            return False
        html = _render_preview_html(view)
        _apply_preview_settings(view)
        _update_preview_phantom(view, html)
        sublime.status_message('Markdown preview on')
        return True
    except Exception:
        traceback.print_exc()
        return False


def _base_dir(view):
    fname = view.file_name() or ''
    if fname:
        return os.path.dirname(fname)
    folders = sublime.active_window().folders() if sublime.active_window() else []
    return folders[0] if folders else ''


def _img_height():
    try:
        return int(_settings().get('image_height', 150))
    except Exception:
        return 150


def update_view_phantoms(view, show_remote=True):
    if not is_markdown_view(view):
        return
    if _is_in_preview(view):
        return
    if not _settings().get('enabled', True):
        clear_view(view)
        return
    base = _base_dir(view)
    height = _img_height()
    try:
        max_per_line = int(_settings().get('max_images_per_line', 12))
    except Exception:
        max_per_line = 12

    phantoms = []
    red_regions = []
    green_regions = []

    # colorize <span style="color:red|green"> markers
    content = view.substr(sublime.Region(0, view.size()))
    for m in SPAN_RE.finditer(content):
        color = m.group(1).lower()
        a, b = m.span(2)
        if a < b:
            r = sublime.Region(a, b)
            if color == 'red':
                red_regions.append(r)
            else:
                green_regions.append(r)

    # one BLOCK phantom per image (stacked under the line),
    # same pattern as the Markdown Images package
    lines = view.lines(sublime.Region(0, view.size()))
    for line in lines:
        text = view.substr(line)
        imgs = IMG_RE.findall(text)
        if not imgs:
            continue
        for src in imgs[:max_per_line]:
            if not show_remote and src.lower().startswith(('http://', 'https://')):
                continue
            url = resolve_src(src, base)
            if url == src and not src.lower().startswith(
                    ('http://', 'https://', 'data:', 'file://', 'res://')):
                # local file missing: skip thumbnail, keep text as-is
                continue
            html = ('<img src="' + url + '" height="' + str(height) + '">')
            try:
                phantoms.append(sublime.Phantom(
                    sublime.Region(line.b), html, sublime.LAYOUT_BLOCK))
            except Exception:
                pass

    try:
        pset = sublime.PhantomSet(view, PHANTOM_KEY)
        pset.update(phantoms)
        if phantoms:
            _phantom_sets[view.id()] = pset
        else:
            _phantom_sets.pop(view.id(), None)
    except Exception:
        pass
    flags = getattr(sublime, 'DRAW_NO_OUTLINE', 0)
    try:
        if red_regions:
            view.add_regions(REGION_RED, red_regions, 'markup.deleted', '', flags)
        else:
            view.erase_regions(REGION_RED)
        if green_regions:
            view.add_regions(REGION_GREEN, green_regions, 'markup.inserted', '', flags)
        else:
            view.erase_regions(REGION_GREEN)
    except Exception:
        pass


def clear_view(view):
    try:
        sublime.PhantomSet(view, PHANTOM_KEY).update([])
    except Exception:
        pass
    _phantom_sets.pop(view.id(), None)
    try:
        view.erase_regions(REGION_RED)
        view.erase_regions(REGION_GREEN)
    except Exception:
        pass


def _schedule(view_id):
    if view_id in _pending:
        return
    _pending.add(view_id)

    def _run():
        _pending.discard(view_id)
        v = None
        for w in sublime.windows():
            for cand in w.views():
                if cand.id() == view_id:
                    v = cand
                    break
        if v is not None and v.is_valid():
            update_view_phantoms(v)

    sublime.set_timeout(_run, 400)


class PureMarkdownShow(sublime_plugin.EventListener):
    def on_load(self, view):
        if _resume_preview_if_needed(view):
            return
        if is_markdown_view(view):
            _schedule(view.id())

    def on_activated(self, view):
        if _resume_preview_if_needed(view):
            return
        if is_markdown_view(view):
            _schedule(view.id())

    def on_modified_async(self, view):
        if _is_in_preview(view):
            return
        if is_markdown_view(view):
            _schedule(view.id())

    def on_post_save(self, view):
        if _is_in_preview(view):
            refresh_inplace_preview(view)
        elif is_markdown_view(view):
            _schedule(view.id())

    def on_close(self, view):
        _pending.discard(view.id())
        _phantom_sets.pop(view.id(), None)
        _preview_sets.pop(view.id(), None)
        _preview_state.pop(view.id(), None)


class PureMarkdownImagesShowCommand(sublime_plugin.TextCommand):
    def run(self, edit, show_local=True, show_remote=True):
        update_view_phantoms(self.view, show_remote=show_remote)


class PureMarkdownImagesHideCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        clear_view(self.view)


class PureMarkdownImagesToggleCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        s = _settings()
        s.set('enabled', not s.get('enabled', True))
        sublime.save_settings(SETTINGS_FILE)
        if s.get('enabled', True):
            update_view_phantoms(self.view)
        else:
            clear_view(self.view)


class PureMarkdownWheelCommand(sublime_plugin.TextCommand):
    # plain wheel replacement: pixel scroll in preview, line scroll elsewhere
    def run(self, edit, direction=1, fast=False):
        v = self.view
        try:
            direction = float(direction)
        except Exception:
            direction = 1.0
        in_preview = _is_in_preview(v)
        if in_preview:
            _refit_preview_width(v)
            step = 200.0 if fast else 350.0
        else:
            try:
                lh = float(v.line_height())
            except Exception:
                lh = 15.0
            if lh <= 0:
                lh = 15.0
            step = (10.0 if fast else 3.0) * lh
        try:
            x, y = v.viewport_position()
            target = y + direction * step
            if in_preview:
                # clamp to the bottom of the preview phantom: below it lies
                # the hidden source buffer, reachable via native scrollbar
                # scrolling as dead empty space at the end
                try:
                    y_max = max(0.0, v.text_to_layout(v.text_point(1, 0))[1]
                                - v.viewport_extent()[1])
                    target = min(max(0.0, target), y_max)
                except Exception:
                    pass
            v.set_viewport_position((x, target), False)
        except Exception:
            pass


class PureMarkdownPreviewCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if _is_in_preview(self.view):
            exit_preview(self.view)
            return
        if not is_markdown_view(self.view):
            sublime.error_message('Markdown preview: open a .md file first')
            return
        enter_preview(self.view)


def plugin_loaded():
    # pre-generate the preview scheme at startup so the file exists
    # (and is indexed) long before any preview references it
    try:
        v = None
        if sublime.active_window():
            v = sublime.active_window().active_view()
        if v is not None and v.is_valid():
            bg, fg = _preview_colors(v)
        else:
            bg, fg = '#0b111f', '#a9b7c6'
        _write_preview_scheme(bg, fg)
    except Exception:
        pass
    for w in sublime.windows():
        for v in w.views():
            if _resume_preview_if_needed(v):
                continue
            if is_markdown_view(v):
                _schedule(v.id())
