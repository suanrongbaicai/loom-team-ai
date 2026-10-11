"""Bounded local media reader for the already selected, immutable message."""
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from .control import digest, within
from .wechat_runner import checked_path, verify_adapter
from .wechat_identity import recheck_live

MAX_BYTES = 32*1024*1024


def checked_times(values):
    if (not isinstance(values,list) or not 1<=len(values)<=6
            or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=3600 for v in values)
            or len(set(values))!=len(values)):
        raise ValueError('每次指定1至6个不同的视频时间点，范围0至3600秒。')
    return sorted(values)


def video_frames(executable,source,output,times,guard):
    times=checked_times(times);frames=[];missing=[]
    prefix=[str(executable),'-nostdin','-hide_banner','-protocol_whitelist','file,pipe','-f','mov']
    guard()
    probe=subprocess.run(prefix+['-i',str(source)],capture_output=True,timeout=10)
    details=probe.stderr.decode('utf-8',errors='replace')[:64000]
    duration=re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)',details)
    seconds=sum(float(v)*scale for v,scale in zip(duration.groups(),(3600,60,1))) if duration else None
    for i,at in enumerate(times):
        guard();frame=output/('frame-'+str(i)+'.jpg')
        if seconds is not None and at>=seconds:
            missing.append(at);continue
        with (output/('decode-'+str(i)+'.log')).open('wb') as log:
            subprocess.run(prefix+['-v','error','-threads','2','-filter_threads','1','-ss',str(at),
                '-i',str(source),'-an','-frames:v','1','-vf','scale=1600:1600:force_original_aspect_ratio=decrease',str(frame)],
                check=True,timeout=20,stdout=log,stderr=log)
        guard()
        if frame.exists():
            item=dict(image_result(bounded_bytes(frame),'video_selected_frame'),at_seconds=at)
            if sum(len(f['data']) for f in frames)+len(item['data'])>6*1024*1024:
                raise ValueError('Video samples exceed response budget; request fewer timestamps')
            frames.append(item)
        else:missing.append(at)
    return {'state':'read' if frames else 'unavailable','kind':'video','frames':frames,
            'requested_seconds':times,'missing_seconds':missing,'duration_seconds':seconds,
            'audio_stream_present':'Audio:' in details,'audio_understood':False,'all_frames_read':False,
            'coverage':'仅指定时间附近的抽样画面；未听取声音，未连续看完整视频。',
            'reason':None if frames else '这些时间点没有取得画面，请核对视频时长。'}


def xml_content(message):
    content = message.get('content', '')
    start = content.find('<')
    if start < 0 or '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
        return None
    try:
        return ET.fromstring(content[start:])
    except ET.ParseError:
        return None


def fields(data):
    def integer(pos):
        value = 0
        for shift in range(0, 70, 7):
            byte = data[pos]; pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, pos
        raise ValueError('Invalid media locator')
    pos, result = 0, {}
    while pos < len(data):
        key, pos = integer(pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = integer(pos)
        elif wire == 2:
            size, pos = integer(pos)
            if size > len(data)-pos:
                raise ValueError('Truncated media locator')
            value, pos = data[pos:pos+size], pos+size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if size > len(data)-pos:
                raise ValueError('Truncated media locator')
            value, pos = data[pos:pos+size], pos+size
        else:
            raise ValueError('Unsupported media locator')
        if number in result:
            raise ValueError('Ambiguous media locator')
        result[number] = value
    return result


def media_id(packed, kind):
    outer, inner = (3, 4) if kind == 3 else (4, 8)
    value = fields(fields(packed)[outer])[inner]
    if not isinstance(value, bytes) or not re.fullmatch(rb'[0-9a-fA-F]{32}', value):
        raise ValueError('Exact media identifier missing')
    return value.decode().lower()


def exact_files(root, names, guard):
    """Only metadata enumeration; read a file only after exact-name selection."""
    if not root.exists():
        return []
    within(root.parent, root.name)
    found, count = [], 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        guard()
        for name in [*dirs, *files]:
            count += 1
            if count > 20000:
                raise ValueError('Attachment directory budget exceeded')
            within(root, str((Path(folder)/name).relative_to(root)))
        for name in files:
            if name in names:
                found.append(Path(folder)/name)
    return found


def bounded_bytes(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES+1)
    if len(data) > MAX_BYTES:
        raise ValueError('Attachment exceeds 32 MiB budget')
    return data


def decrypt_image(data, cfg, wxid):
    magic = data[:6]
    xor = cfg & 255
    if magic == b'\x07\x08\x56\x32\x08\x07':
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        aes_size, xor_size = struct.unpack_from('<LL', data, 6)
        block = (aes_size//16+1)*16
        if len(data) < 15+block+xor_size:
            raise ValueError('Truncated encrypted image')
        key = hashlib.md5((str(cfg)+wxid).encode()).hexdigest()[:16].encode()
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = dec.update(data[15:15+block])+dec.finalize()
        pad = plain[-1]
        if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad])*pad:
            raise ValueError('Image decryption did not validate')
        tail_start = len(data)-xor_size
        return plain[:-pad]+data[15+block:tail_start]+bytes(b^xor for b in data[tail_start:])
    if magic == b'\x07\x08\x05\x56\x02\x05':
        return bytes(b^xor for b in data[22:])
    for key in (0x88, 0x30, 0xff, 0xe9):
        plain = bytes(b^key for b in data)
        if plain.startswith((b'\xff\xd8\xff', b'\x89PNG')):
            return plain
    raise ValueError('Unrecognized image format')


def image_result(data, coverage):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 20_000_000
    with Image.open(io.BytesIO(data)) as pic:
        if pic.width*pic.height > 20_000_000:
            raise ValueError('Image pixel budget exceeded')
        animated = getattr(pic, 'n_frames', 1) > 1
        pic.seek(0); pic.load()
        dimensions = [pic.width, pic.height]
        pic.thumbnail((1600, 1600))
        output = io.BytesIO()
        pic.convert('RGB').save(output, format='JPEG', quality=90)
    blob = output.getvalue()
    if len(blob) > 4*1024*1024:
        raise ValueError('Decoded image too large')
    return {'state': 'read', 'kind': 'image', 'coverage': coverage,
            'original_dimensions': dimensions, 'first_frame_only': animated or coverage.startswith('video_first_frame_only'),
            'sha256': digest(blob), 'mimeType': 'image/jpeg', 'data': base64.b64encode(blob).decode()}


def text_file(data, extension):
    if extension in ('.txt', '.csv', '.md', '.json', '.xml', '.log'):
        for encoding in ('utf-8-sig', 'gb18030'):
            try: return data.decode(encoding)[:200000]
            except UnicodeError: pass
        raise ValueError('Text encoding unavailable')
    if extension not in ('.docx', '.xlsx', '.pptx'):
        return None
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(p.file_size for p in archive.infolist()) > MAX_BYTES:
            raise ValueError('Expanded document exceeds budget')
        names = (['word/document.xml'] if extension == '.docx' else
                 sorted([n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml',n)],key=lambda n:int(re.search(r'slide(\d+)\.xml',n)[1])) if extension=='.pptx' else
                 [n for n in archive.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')])
        shared = []
        if extension == '.xlsx' and 'xl/sharedStrings.xml' in archive.namelist():
            raw = archive.read('xl/sharedStrings.xml')
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ValueError('Document entities refused')
            root = ET.fromstring(raw)
            shared = [''.join(node.itertext()) for node in root]
        parts = []
        for name in names:
            raw = archive.read(name)
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ValueError('Document entities refused')
            root = ET.fromstring(raw)
            if extension in ('.docx','.pptx'):
                if extension=='.pptx':parts.append(name)
                parts.extend(''.join(p.itertext()) for p in root.iter() if p.tag.endswith('}p'))
            else:
                parts.append(name)
                for row in root.iter():
                    if not row.tag.endswith('}row'): continue
                    values = []
                    for cell in row:
                        value = ''.join(cell.itertext())
                        if cell.get('t') == 's':
                            value = shared[int(value)]
                        values.append(cell.get('r', '')+': '+value)
                    parts.append('\t'.join(values))
        return '\n'.join(parts)[:200000]


def run(job, guard):
    private, output = checked_path(job['private_root']), checked_path(job['run'])
    cache = checked_path(job['cache'])
    if not all(p.is_relative_to(private) for p in (output, cache)):
        raise ValueError('Protected attachment storage required')
    adapter = verify_adapter(job['adapter_dir']);sys.path.insert(0, str(adapter))
    import capture_owner as pinned
    pinned.R1, pinned.PRIVATE = checked_path(job['upstream_root']), private
    sys.addaudithook(pinned.guard_writes)
    tempfile.tempdir = str(output/'temp')
    account, message = job['account'], job['attachment']
    name = account['account']
    if any(c in name for c in '/\\:') or name in ('.', '..'):
        raise ValueError('Invalid provisioned account path')
    account_root = within(checked_path(account['db_root']), name)
    guard();recheck_live({'account_root': str(account_root)})
    module = pinned.load_pinned_db()
    shard = message['source_shard'].replace('\\', '/')
    if not re.fullmatch(r'message/message_\d+\.db', shard):
        raise ValueError('Unsupported original shard')
    table = 'Msg_'+hashlib.md5(message['chat'].encode()).hexdigest()
    if table != message['source_table']:
        raise ValueError('Original chat/table mismatch')
    dbpath = within(cache, shard.replace('/', '__'))
    conn = sqlite3.connect(dbpath.as_uri()+'?mode=ro', uri=True);conn.row_factory=sqlite3.Row
    try:
        rows = conn.execute('SELECT * FROM "'+table+'" WHERE local_id=? AND server_id=? AND create_time=?',
                            (message['local_id'], message['server_id'], message['create_time'])).fetchall()
    finally: conn.close()
    if len(rows) != 1:
        raise ValueError('Original message is not uniquely matched')
    row = dict(rows[0])
    if message.get('raw_content_hex') and row['message_content'].hex() != message['raw_content_hex']:
        raise ValueError('Cached original content changed')
    kind = row['local_type'] & 0xffffffff
    result = {'state': 'unavailable', 'kind': 'attachment', 'reason': '此类附件还不能读取。'}
    original_raw=None; original_name='attachment';original_coverage='original'
    if kind in (3, 43):
        ident = media_id(row['packed_info_data'], kind)
        base = account_root/'msg'/('attach/'+hashlib.md5(message['chat'].encode()).hexdigest() if kind == 3 else 'video')
        names = [ident+'_h.dat', ident+'.dat', ident+'_t.dat'] if kind == 3 else [ident+'.mp4']
        matches = exact_files(base, names, guard)
        selected = []
        for name in names:
            selected = [p for p in matches if p.name == name]
            if selected: break
        if len(selected) > 1:
            raise ValueError('Multiple local files claim this attachment')
        if not selected:
            result['reason'] = '这条消息的附件未下载到本机，尚未读到内容。'
        else:
            guard();raw = bounded_bytes(selected[0])
            if kind == 3:
                import psutil
                cfgs = set()
                for proc in psutil.process_iter(['pid', 'name']):
                    if (proc.info.get('name') or '').lower() != 'weixin.exe': continue
                    guard();got = module.extract_master_key_from_cfg(proc.pid)
                    if got and got[2] == account['wxid']: cfgs.add(got[1])
                if len(cfgs) != 1:
                    raise ValueError('Current image key account unavailable')
                raw = decrypt_image(raw, cfgs.pop(), account['wxid'])
                coverage = 'thumbnail' if selected[0].name.endswith('_t.dat') else 'local_image'
            else:
                coverage = 'video_first_frame_only; audio_and_remaining_video_not_read'
            original_raw=raw
            original_name=selected[0].name
            original_coverage='thumbnail_only' if coverage=='thumbnail' else 'local_original'
            if kind == 43 or raw.startswith(b'wxgf'):
                converter = job.get('media_converter', {})
                executable = checked_path(converter['executable'])
                if executable.drive.casefold() == 'c:':
                    raise ValueError('Media converter requires non-C deployment')
                with executable.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != converter['sha256']:
                        raise ValueError('Media converter deployment changed')
                source = output/'temp'/('input.mp4' if kind == 43 else 'input.hevc')
                if kind == 3:
                    start = raw.find(b'\x00\x00\x00\x01')
                    if start < 0: raise ValueError('Image frame unavailable')
                    raw = raw[start:]
                source.write_bytes(raw);frame = output/'temp/frame.jpg'
                guard()
                if kind==43 and job.get('frame_times') is not None:
                    result=video_frames(executable,source,output/'temp',job['frame_times'],guard)
                else:
                    with (output/'temp/decode.log').open('wb') as log:
                        subprocess.run([str(executable), '-nostdin', '-v', 'error', '-threads', '2',
                                    '-filter_threads', '1', '-protocol_whitelist', 'file,pipe',
                                    '-f', 'mov' if kind == 43 else 'hevc',
                                    '-i', str(source), '-frames:v', '1', '-vf',
                                    'scale=1600:1600:force_original_aspect_ratio=decrease', str(frame)],
                                       check=True, timeout=30, stdout=log, stderr=log)
                    raw = bounded_bytes(frame)
            if not (kind==43 and job.get('frame_times') is not None):result = image_result(raw, coverage)
    elif kind == 49:
        xml = xml_content(message)
        app = xml.find('.//appmsg') if xml is not None else None
        if app is not None and app.findtext('type') != '6':
            result = {'state': 'read', 'kind': 'card', 'coverage': 'card_fields_only; linked_content_not_opened',
                      'text': {key: app.findtext(key) for key in ('type', 'title', 'des', 'url')},
                      'reply_text': app.findtext('.//refermsg/content')}
        elif app is not None:
            title = app.findtext('title', '')
            md5 = app.findtext('.//appattach/filemd5') or app.findtext('.//filemd5')
            if (not title or title != Path(title).name or any(c in title for c in '/\\:')
                    or not md5 or not re.fullmatch('[a-fA-F0-9]{32}', md5)):
                result['reason'] = '文件缺少可核对的内容标识，不能仅凭同名文件认定是这份附件。'
            else:
                matches = exact_files(account_root/'msg/file', [title], guard)
                verified = []
                for path in matches:
                    guard();blob = bounded_bytes(path)
                    if hashlib.md5(blob).hexdigest() == md5.lower(): verified.append(blob)
                if not verified:
                    result['reason'] = '未找到与原消息内容标识一致的本机文件。'
                elif any(blob != verified[0] for blob in verified):
                    raise ValueError('Ambiguous file contents')
                else:
                    original_raw=verified[0];original_name=title
                    text = text_file(verified[0], Path(title).suffix.lower())
                    if text is not None:
                        result = {'state': 'read', 'kind': 'document', 'text': text, 'sha256': digest(verified[0]),
                                  'coverage': 'text_only; formatting_images_macros_not_read; max_200000_characters'}
                    else: result['reason'] = '文件已定位，但此格式的正文尚不能读取。'
    guard();recheck_live({'account_root': str(account_root)})
    export = output/'export';export.mkdir()
    if original_raw is not None:
        (export/'original.bin').write_bytes(original_raw)
        result['retained_original']={'sha256':digest(original_raw),'bytes':len(original_raw),
                                     'name':original_name,'coverage':original_coverage}
    raw = json.dumps(result, ensure_ascii=False).encode()
    (export/'messages.json').write_bytes(raw)
    (export/'receipt.json').write_text(json.dumps({'sha256': digest(raw), 'attachment': True}), encoding='utf-8')
