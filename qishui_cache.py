#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
汽水音乐 VIP 无损下载器（从客户端本地缓存读取，无需抓包）

原理
----
汽水音乐 PC 端把每个播放过的档位缓存在:
  %APPDATA%\\SodaMusic\\LunaCacheV2\\entries.db   （MessagePack 格式）
里面存着每个缓存条目的完整 video_model：
  video_list[] -> main_url（播放地址）+ encrypt_info.spade_a（AES 密钥）
                 + video_meta.quality / codec_type / bitrate
以及曲目信息（歌名/歌手）。所以**不需要 Fiddler**。

用法
----
  python qishui_cache.py --list                     列出所有档位
  python qishui_cache.py --list --flac-only         只列无损档
  python qishui_cache.py --songs                    按歌曲汇总（每首最佳档）
  python qishui_cache.py --best-per-song -o 目录     每首歌取最佳档，批量下载+解密+转MP3
  python qishui_cache.py --pick flac -o 目录         下载最高码率无损档
  python qishui_cache.py --pick-flac 0 -o 目录       下载指定编号的无损档
  python qishui_cache.py --all-flac -o 目录          批量下载所有无损档
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request

APPDATA = os.environ.get('APPDATA', '')
DB_PATH = os.path.join(APPDATA, 'SodaMusic', 'LunaCacheV2', 'entries.db')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'SodaMusic/3.7.0 Chrome/136.0.7103.59 Electron/36.4.0-rs.29.release.main.0 '
      'TTElectron/36.4.0-rs.29.release.main.0 Safari/537.36')
DECRYPTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qishui_decrypt.py')
QUALITY = {'medium', 'higher', 'highest', 'lossless', 'hi_res', 'spatial'}


def load_decryptor():
    spec = importlib.util.spec_from_file_location('qdl', DECRYPTOR)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------- 歌名提取 ----------------
def _read_strings(buf, lo, hi):
    """扫描 [lo,hi) 内所有 msgpack 字符串"""
    out = []
    pos = lo
    while pos < hi:
        b = buf[pos]
        ln, hdr = None, 0
        if 0xa0 <= b <= 0xbf:
            ln, hdr = b & 0x1f, 1
        elif b == 0xd9 and pos + 1 < hi:
            ln, hdr = buf[pos+1], 2
        elif b == 0xda and pos + 2 < hi:
            ln, hdr = struct.unpack_from('>H', buf, pos+1)[0], 3
        if ln is not None and 0 < ln < 400 and pos + hdr + ln <= len(buf):
            s = buf[pos+hdr:pos+hdr+ln]
            try:
                t = s.decode('utf-8')
                if all(ord(c) >= 32 for c in t):
                    out.append((pos, t))
            except Exception:
                pass
            pos += hdr + ln
            continue
        pos += 1
    return out


def _title_like(t):
    if not (1 < len(t) <= 70):
        return False
    if t in QUALITY or t in ('track', 'album', 'lyric', 'video', 'audio'):
        return False
    if '://' in t or t.startswith(('tos-cn', 'tplv-', 'benefit_')):
        return False
    if re.fullmatch(r'[0-9a-f]{2,8}', t):
        return False
    if re.fullmatch(r'[0-9a-f]{32}', t):
        return False
    if re.fullmatch(r'\d{6,}', t):
        return False
    if re.fullmatch(r'[vV]\d[a-z0-9]{20,}', t):      # media_id
        return False
    if re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', t):
        return True
    return bool(re.search(r'[A-Za-z]{2,}', t))


def extract_title(buf, json_start):
    """歌名紧挨在音质标签块之前"""
    strs = _read_strings(buf, max(0, json_start - 8000), json_start)
    strs.sort(key=lambda x: -x[0])
    i = 0
    while i < len(strs) and strs[i][1] not in QUALITY:
        i += 1
    if i >= len(strs):
        return '', ''
    while i < len(strs) and strs[i][1] in QUALITY:
        i += 1
    while i < len(strs) and not _title_like(strs[i][1]):
        i += 1
    title = strs[i][1] if i < len(strs) else ''
    artist = ''
    j = i + 1
    while j < len(strs) and not _title_like(strs[j][1]):
        j += 1
    if j < len(strs) and len(strs[j][1]) <= 30:
        artist = strs[j][1]
    return title, artist


# ---------------- db 解析 ----------------
def parse_db(path):
    """返回 [(song_index, title, artist, tiers[])]"""
    raw = open(path, 'rb').read()
    songs = []
    for i in range(len(raw) - 5):
        b = raw[i]
        if b == 0xda:
            n = struct.unpack_from('>H', raw, i+1)[0]; hs = 3
        elif b == 0xdb:
            n = struct.unpack_from('>I', raw, i+1)[0]; hs = 5
        else:
            continue
        if not (500 < n < 200000) or i + hs + n > len(raw):
            continue
        seg = raw[i+hs:i+hs+n]
        if b'"video_list"' not in seg:
            continue
        try:
            j = json.loads(seg.decode('utf-8'))
        except Exception:
            continue
        title, artist = extract_title(raw, i)
        tiers = []
        for item in (j.get('video_list') or []):
            enc = item.get('encrypt_info') or {}
            meta = item.get('video_meta') or {}
            if not item.get('main_url') or not enc.get('spade_a'):
                continue
            tiers.append({
                'url': item['main_url'], 'spade_a': enc['spade_a'],
                'codec': meta.get('codec_type', ''), 'quality': meta.get('quality', ''),
                'bitrate': meta.get('bitrate', 0),
                'duration': meta.get('duration', 0) or j.get('video_duration', 0),
                'video_id': j.get('video_id', ''),
            })
        if tiers:
            songs.append({'title': title, 'artist': artist, 'tiers': tiers,
                          'offset': i, 'video_id': j.get('video_id', '')})
    # 按 video_id 去重（同一首歌可能被缓存多次）
    seen, uniq = {}, []
    for s in songs:
        k = s['video_id'] or s['offset']
        if k in seen:
            # 合并档位
            seen[k]['tiers'].extend(s['tiers'])
            if not seen[k]['title'] and s['title']:
                seen[k]['title'], seen[k]['artist'] = s['title'], s['artist']
            continue
        seen[k] = s
        uniq.append(s)
    # 每个 song 内部档位去重
    for s in uniq:
        d, u = set(), []
        for t in s['tiers']:
            if t['url'][:70] in d:
                continue
            d.add(t['url'][:70]); u.append(t)
        s['tiers'] = u
    return uniq


def best_tier(tiers):
    flac = [t for t in tiers if t['codec'] == 'flac']
    if flac:
        return max(flac, key=lambda x: x['bitrate'])
    return max(tiers, key=lambda x: x['bitrate'])


def probe_size(url):
    """用 Range 请求拿文件总大小（不下载内容）"""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': UA, 'Referer': 'https://music.douyin.com/',
            'Accept': '*/*', 'Range': 'bytes=0-0'})
        with urllib.request.urlopen(req, timeout=25) as r:
            cr = r.headers.get('Content-Range', '')
            m = re.search(r'/(\d+)\s*$', cr)
            if m:
                return int(m.group(1))
            cl = r.headers.get('Content-Length')
            if cl:
                return int(cl)
    except Exception:
        pass
    return 0


def pick_under_size(tiers, max_bytes):
    """按「无损优先、码率降序」挑第一个不超过 max_bytes 的档位；max_bytes<=0 表示不限"""
    order = sorted(tiers, key=lambda t: (0 if t['codec'] == 'flac' else 1, -t['bitrate']))
    if max_bytes <= 0:
        return order[0] if order else None
    for t in order:
        sz = probe_size(t['url'])
        t['size'] = sz
        if sz and sz <= max_bytes:
            return t
    # 全超限 → 取最小的那个
    sized = [(t.get('size', 0) or 1 << 62, t) for t in order]
    return min(sized, key=lambda x: x[0])[1] if sized else None


def url_stamp(url):
    m = re.search(r'&l=(\d{14})', url)
    if not m:
        return ''
    s = m.group(1)
    return f"{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"


def safe_name(s, fallback):
    s = (s or '').strip()
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', s)
    s = re.sub(r'\s+', ' ', s).strip(' .')
    return s[:80] or fallback


def download(url, out_path):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Referer': 'https://music.douyin.com/', 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=900) as r, open(out_path, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        while True:
            c = r.read(262144)
            if not c:
                break
            f.write(c); got += len(c)
            if total:
                print(f"\r    {got*100//total}% ({got//1024}/{total//1024} KB)", end='', flush=True)
    print()
    return os.path.getsize(out_path)


def process(t, qdl, outdir, base, to_mp3=True):
    os.makedirs(outdir, exist_ok=True)
    enc = os.path.join(outdir, '_enc.tmp')
    try:
        size = download(t['url'], enc)
    except urllib.error.HTTPError as e:
        print(f"    ✗ HTTP {e.code}（URL 已过期 —— 在客户端重新播放该歌曲刷新）")
        return None
    except Exception as e:
        print(f"    ✗ {type(e).__name__}: {e}")
        return None
    print(f"    {size:,} 字节 = {size/1048576:.2f} MB")
    with contextlib.redirect_stdout(io.StringIO()):
        key = qdl.resolve_key(t['spade_a'])
    if len(key) not in (16, 24, 32):
        print("    ✗ 密钥解析失败")
        try: os.remove(enc)
        except Exception: pass
        return None
    with contextlib.redirect_stdout(io.StringIO()):
        out = qdl.decrypt_file(enc, outdir, key)
    try:
        os.remove(enc)
    except Exception:
        pass
    if not out:
        print("    ✗ 解密失败")
        return None
    ext = os.path.splitext(out)[1]
    final = os.path.join(outdir, base + ext)
    try:
        if os.path.exists(final):
            os.remove(final)
        shutil.move(out, final)
    except Exception:
        final = out
    print(f"    ✓ {os.path.basename(final)}  ({os.path.getsize(final):,} 字节)")

    mp3path = None
    if to_mp3:
        ff = shutil.which('ffmpeg')
        if ff:
            mp3path = os.path.join(outdir, base + '.mp3')
            subprocess.run([ff, '-y', '-hide_banner', '-loglevel', 'error',
                            '-i', final, '-vn', '-c:a', 'libmp3lame', '-q:a', '0',
                            '-metadata', f'title={base}', mp3path], capture_output=True)
            if os.path.exists(mp3path):
                print(f"    ✓ {os.path.basename(mp3path)}  ({os.path.getsize(mp3path):,} 字节)")
            else:
                mp3path = None
    return mp3path or final


def main():
    ap = argparse.ArgumentParser(description='汽水音乐 VIP 下载器（读本地缓存，无需抓包）')
    ap.add_argument('--db', default=DB_PATH)
    ap.add_argument('--list', action='store_true', help='列出所有档位')
    ap.add_argument('--flac-only', action='store_true', help='只显示无损档')
    ap.add_argument('--songs', action='store_true', help='按歌曲汇总')
    ap.add_argument('--best-per-song', action='store_true', help='每首歌取最佳档批量下载')
    ap.add_argument('--pick', default='flac', help='flac / best / 数字索引')
    ap.add_argument('--pick-flac', type=int, default=None)
    ap.add_argument('--all-flac', action='store_true')
    ap.add_argument('-o', '--outdir', default='.')
    ap.add_argument('--name', default='')
    ap.add_argument('--no-mp3', action='store_true')
    ap.add_argument('--keep-original', action='store_true', help='保留解密后的原文件')
    ap.add_argument('--force', action='store_true', help='强制重新下载已存在的文件')
    ap.add_argument('--max-src-mb', type=float, default=0,
                    help='源文件大小上限(MB)，超过则自动退到小一档（0=不限）')
    args = ap.parse_args()
    MAXBYTES = int(args.max_src_mb * 1024 * 1024) if args.max_src_mb > 0 else 0

    if not os.path.exists(args.db):
        print(f"✗ 找不到 {args.db}")
        return 1
    tmpdb = os.path.join(os.environ.get('TEMP', '.'), 'qishui_entries.db')
    try:
        shutil.copy2(args.db, tmpdb); dbp = tmpdb
    except Exception:
        dbp = args.db

    print(f"[1/3] 解析缓存 ...")
    songs = parse_db(dbp)
    alltiers = [t for s in songs for t in s['tiers']]
    flac = [t for t in alltiers if t['codec'] == 'flac']
    print(f"      歌曲 {len(songs)} 首，档位 {len(alltiers)} 个（无损 {len(flac)} 个）")

    if args.songs or (args.list and not args.flac_only):
        print(f"\n{'#':>4}  {'歌名':<36} {'歌手':<12} {'最佳档':<22}")
        print("-" * 82)
        for i, s in enumerate(songs):
            b = best_tier(s['tiers'])
            info = f"{b['codec']} {b['bitrate']}"
            t = s['title'][:34] if s['title'] else '(未知)'
            print(f"{i:>4}  {t:<36} {s['artist'][:11]:<12} {info:<22}")
        print(f"\n共 {len(songs)} 首")
        if args.songs:
            return 0

    if args.list and args.flac_only:
        print(f"\n{'#':>4}  {'编码':<6} {'码率':>9}  {'音质':<10} {'抓取时间':<12}")
        for i, t in enumerate(flac):
            print(f"{i:>4}  {t['codec']:<6} {t['bitrate']:>9}  {t['quality']:<10} {url_stamp(t['url']):<12}")
        return 0

    if not alltiers:
        print("✗ 没有档位")
        return 1
    qdl = load_decryptor()

    # 每首歌最佳档
    if args.best_per_song:
        print(f"\n[2/3] 逐首下载（共 {len(songs)} 首）"
              + (f"，源文件上限 {args.max_src_mb} MB" if MAXBYTES else "") + " ...")
        ok = 0
        for i, s in enumerate(songs):
            b = pick_under_size(s['tiers'], MAXBYTES)
            if not b:
                print(f"\n[{i+1}/{len(songs)}] {s['title'] or '(未知)'} — 无可用档位")
                continue
            base = args.name or safe_name(
                f"{s['title']} - {s['artist']}" if s['title'] else '', f"track_{i:02d}")
            if base.startswith('track_'):
                base = f"track_{i:02d}"

            mp3_final = os.path.join(args.outdir, base + '.mp3')
            orig_final = os.path.join(args.outdir, base + ('.flac' if b['codec'] == 'flac' else '.m4a'))
            target_exists = os.path.exists(mp3_final) if not args.no_mp3 else os.path.exists(orig_final)
            if target_exists and not getattr(args, 'force', False):
                sz_exist = os.path.getsize(mp3_final if not args.no_mp3 else orig_final)
                print(f"\n[{i+1}/{len(songs)}] {s['title'] or '(未知)'}  [已存在，跳过 ({sz_exist/1048576:.1f}MB)]")
                ok += 1
                continue

            sz = b.get('size', 0)
            szs = f"{sz/1048576:.1f}MB" if sz else "?"
            print(f"\n[{i+1}/{len(songs)}] {s['title'] or '(未知)'}  "
                  f"[{b['codec']} {b['bitrate']} {szs}]")
            r = process(b, qdl, args.outdir, base, not args.no_mp3)
            if r:
                ok += 1
                if not args.keep_original and r.endswith('.mp3'):
                    for ext in ('.flac', '.m4a'):
                        p = os.path.join(args.outdir, base + ext)
                        if os.path.exists(p):
                            try: os.remove(p)
                            except Exception: pass
        print(f"\n[3/3] 完成 {ok}/{len(songs)}")
        return 0 if ok else 1

    if args.all_flac:
        print(f"\n[2/3] 批量下载 {len(flac)} 个无损档 ...")
        ok = 0
        for i, t in enumerate(flac):
            base = args.name or f"FLAC_{i:03d}_{t['bitrate']}"
            print(f"\n[{i+1}/{len(flac)}] {t['bitrate']} bps")
            if process(t, qdl, args.outdir, base, not args.no_mp3):
                ok += 1
        print(f"\n[3/3] 完成 {ok}/{len(flac)}")
        return 0

    pick = args.pick
    if args.pick_flac is not None:
        try:
            target = flac[args.pick_flac]
        except Exception:
            print(f"✗ 编号越界（共 {len(flac)} 个无损档）")
            return 1
    elif pick == 'flac':
        target = max(flac, key=lambda x: x['bitrate']) if flac else max(alltiers, key=lambda x: x['bitrate'])
    elif pick == 'best':
        target = max(alltiers, key=lambda x: x['bitrate'])
    else:
        try:
            target = alltiers[int(pick)]
        except Exception:
            target = max(alltiers, key=lambda x: x['bitrate'])

    print(f"\n[2/3] 选中 codec={target['codec']} bitrate={target['bitrate']} quality={target['quality']}")
    base = args.name or f"VIP_{target['codec']}_{target['bitrate']}"
    r = process(target, qdl, args.outdir, base, not args.no_mp3)
    print(f"\n[3/3] {'✓ 完成' if r else '✗ 失败'}")
    return 0 if r else 1


if __name__ == '__main__':
    sys.exit(main())
