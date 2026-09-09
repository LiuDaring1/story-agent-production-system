"""Conservative FFmpeg dependency snapshots. Unknown inputs never authorize reuse."""
import glob
import re
import shlex
from pathlib import Path
from story_production_v2 import binding


def snapshot(args):
    files, groups, reasons = [], [], []
    def add(path):
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f'Encode dependency missing: {p}')
        item = binding(p)
        files.append(item)
        return item
    fmt = None
    pattern_type = None
    for index, arg in enumerate(args[:-1]):
        value = args[index + 1]
        if arg == '-f':
            fmt = value
        elif arg == '-pattern_type':
            pattern_type = value
        elif arg == '-i':
            if fmt == 'lavfi':
                # Only these self-contained deterministic generators are known.
                if not re.fullmatch(r'(testsrc2?|color|sine|anullsrc)(=[a-zA-Z0-9_.:=/#-]+)?', value):
                    reasons.append('unresolved lavfi graph')
            elif value == '-' or re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', value):
                reasons.append('stream/protocol input')
            elif (pattern_type != 'none' and re.search(r'%0?\d*d', value)) or pattern_type == 'glob':
                if pattern_type == 'glob':
                    reasons.append('glob ordering is demuxer-dependent')
                    members = sorted(glob.glob(value))
                else:
                    p = Path(value)
                    expression = re.escape(p.name)
                    expression = re.sub(r'%0?(\d*)d', lambda m: r'(\d{' + m[1] + ',})' if m[1] else r'(\d+)', expression)
                    matcher = re.compile('^' + expression + '$')
                    members = sorted((str(f) for f in p.parent.iterdir() if matcher.fullmatch(f.name)),
                                     key=lambda name: tuple(int(n) for n in matcher.fullmatch(Path(name).name).groups()))
                if not members:
                    raise FileNotFoundError(f'Encode image sequence is empty: {value}')
                groups.append({'pattern': value, 'members': [add(p) for p in members]})
            else:
                item = add(value)
                p = Path(item['path'])
                with p.open('rb') as stream:
                    prefix = stream.read(4096).lstrip(b'\xef\xbb\xbf \r\n\t')
                detected_concat = prefix.startswith(b'ffconcat version ')
                if fmt not in {None, 'concat', 'image2', 'png_pipe', 'jpeg_pipe', 'mov', 'mp4', 'matroska', 'webm', 'wav', 'mp3', 'flac', 'ogg', 'aac', 'avi'}:
                    reasons.append('unresolved explicit demuxer')
                if prefix.startswith((b'#EXTM3U', b'<?xml', b'<MPD', b'v=0')):
                    reasons.append('content identifies indirect manifest')
                if fmt == 'concat' or detected_concat or p.suffix.lower() in {'.ffconcat', '.m3u8', '.m3u', '.mpd', '.sdp'}:
                    reasons.append('indirect manifest input: no reuse')
                    if fmt == 'concat' or detected_concat or p.suffix.lower() == '.ffconcat':
                        for line in p.read_text().splitlines():
                            if '"' in line:
                                reasons.append('concat quoting delegated to FFmpeg')
                                continue
                            try:
                                parts = shlex.split(line, comments=False)
                            except ValueError:
                                reasons.append('unparsed concat directive')
                                continue
                            if parts and parts[0] == 'file' and len(parts) == 2:
                                if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', parts[1]):
                                    reasons.append('concat protocol member')
                                else:
                                    add(p.parent / parts[1])
                elif p.suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.wav','.mp3','.flac','.m4a','.aac','.ogg','.mp4','.mov','.mkv','.webm','.avi','.m4v'}:
                    reasons.append('unrecognized input format')
            fmt, pattern_type = None, None
        elif arg.startswith(('-filter_complex_script', '-filter_script', '-/filter')):
            add(value)
            reasons.append('external filter script may reference other files')
        elif arg.startswith(('-filter', '-vf', '-af', '-lavfi')):
            reasons.append('filter graph may reference external files')
        elif arg in {'-pass','-passlogfile','-attach','-enable_drefs','-hls_key_info_file'}:
            reasons.append('additional external encoder dependency')
        elif arg.startswith('-') and arg not in {
            '-y','-n','-v','-loglevel','-hide_banner','-nostdin','-stats','-nostats',
            '-c:v','-c:a','-c','-codec:v','-codec:a','-preset','-crf','-pix_fmt','-b:a','-b:v',
            '-movflags','-r','-framerate','-t','-ss','-to','-s','-an','-vn','-sn','-dn',
            '-map','-map_metadata','-map_chapters','-shortest','-loop','-stream_loop',
            '-start_number','-start_number_range','-safe','-re','-threads','-filter_threads',
            '-filter_complex_threads','-ar','-ac','-q:v','-vsync','-fps_mode',
        } and not re.fullmatch(r'-\d+(\.\d+)?', arg):
            reasons.append('unclassified option: ' + arg)
    return {'version': 1, 'files': files, 'sequences': groups,
            'reusable': not reasons, 'no_reuse_reasons': sorted(set(reasons))}
