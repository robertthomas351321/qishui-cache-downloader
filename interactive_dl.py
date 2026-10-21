#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
汽水音乐缓存交互式解析下载器
功能：
1. 自动读取并解析客户端本地缓存 (entries.db)
2. 列表展示所有已识别的歌曲名与歌手
3. 支持用户交互选择歌曲编号（单选或多选、全选）
4. 支持选择音质（无损 FLAC、高品质 AAC/M4A 等指定档位）
5. 保持原始音质格式解密输出，不转 MP3、不强改后缀
"""

import os
import sys
import shutil
import importlib.util

# 确保控制台 UTF-8 输出
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stdin.reconfigure(encoding='utf-8')
    except Exception:
        pass

DIR_PATH = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(DIR_PATH, 'downloads')

# 动态加载同目录下的 qishui_cache 与 qishui_decrypt
def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

qcache = load_module('qcache', os.path.join(DIR_PATH, 'qishui_cache.py'))
qdecrypt = load_module('qdecrypt', os.path.join(DIR_PATH, 'qishui_decrypt.py'))


def main():
    print("=" * 60)
    print("         汽水音乐缓存解析与交互下载工具")
    print("=" * 60)

    # 1. 读取并复制 entries.db
    if not os.path.exists(qcache.DB_PATH):
        print(f"✗ 未找到缓存数据库: {qcache.DB_PATH}")
        print("  请确认已在电脑端运行过汽水音乐并播放过歌曲。")
        input("\n按回车键退出...")
        return 1

    tmp_db = os.path.join(os.environ.get('TEMP', '.'), 'qishui_interactive_entries.db')
    try:
        shutil.copy2(qcache.DB_PATH, tmp_db)
        dbp = tmp_db
    except Exception:
        dbp = qcache.DB_PATH

    print("\n[1/3] 正在扫描并解析客户端缓存...")
    songs = qcache.parse_db(dbp)
    if not songs:
        print("✗ 缓存中未解析到有效歌曲。请先在汽水音乐播放要下载的歌曲。")
        input("\n按回车键退出...")
        return 1

    # 2. 列出所有歌曲
    print(f"\n成功解析到 {len(songs)} 首歌曲：\n")
    print(f"{'序号':>4}  {'歌名':<32} {'歌手':<16} {'包含档位'}")
    print("-" * 68)
    for i, s in enumerate(songs):
        title = (s['title'] or '(未知歌名)')[:30]
        artist = (s['artist'] or '')[:14]
        codecs = set(t['codec'] for t in s['tiers'])
        codecs_str = "/".join(sorted(codecs))
        has_flac = " [含无损FLAC]" if "flac" in codecs else ""
        print(f"{i:>4}  {title:<32} {artist:<16} {codecs_str}{has_flac}")

    # 3. 选择歌曲
    print("-" * 68)
    print("选择歌曲方式：")
    print("  - 输入单个序号 (例如: 3)")
    print("  - 输入多个序号以空格或逗号分隔 (例如: 1 3 5 或 1,3,5)")
    print("  - 输入 all 下载全部歌曲")
    print("  - 直接回车退出")

    choice = input("\n请输入要下载的歌曲序号: ").strip()
    if not choice:
        print("已取消操作。")
        return 0

    selected_indices = []
    if choice.lower() == 'all':
        selected_indices = list(range(len(songs)))
    else:
        parts = choice.replace(',', ' ').split()
        for p in parts:
            if p.isdigit():
                idx = int(p)
                if 0 <= idx < len(songs):
                    if idx not in selected_indices:
                        selected_indices.append(idx)
                else:
                    print(f"⚠️ 忽略超出范围的序号: {idx}")

    if not selected_indices:
        print("✗ 未选择有效歌曲。")
        return 1

    # 4. 选择音质模式
    print("\n音质选项：")
    print("  1. 最佳音质（有无损则优先下载 FLAC，否则下载最高码率 AAC/M4A）[推荐]")
    print("  2. 仅无损 FLAC（若该歌曲无无损档则自动跳过）")
    print("  3. 逐首手动挑选具体音质档位")
    q_mode = input("请选择音质模式 [默认 1]: ").strip() or "1"

    os.makedirs(DEFAULT_OUT, exist_ok=True)
    print(f"\n[2/3] 开始下载解密（输出目录: {DEFAULT_OUT}，保持原始格式不改后缀）...\n")

    ok_count = 0
    for idx in selected_indices:
        s = songs[idx]
        title = s['title'] or f"track_{idx:02d}"
        artist = s['artist'] or ""
        base_name = qcache.safe_name(f"{title} - {artist}" if artist else title, f"track_{idx:02d}")

        target_tier = None
        tiers = s['tiers']

        if q_mode == '1':
            target_tier = qcache.best_tier(tiers)
        elif q_mode == '2':
            flacs = [t for t in tiers if t['codec'] == 'flac']
            if flacs:
                target_tier = max(flacs, key=lambda x: x.get('bitrate', 0))
            else:
                print(f"⏩ 跳过: 《{title}》 无 FLAC 无损档")
                continue
        elif q_mode == '3':
            print(f"\n--- 《{title}》 可选音质档位 ---")
            for t_idx, t in enumerate(tiers):
                br_kbps = t.get('bitrate', 0) // 1000
                q_name = t.get('quality', '')
                codec = t.get('codec', '')
                print(f"  [{t_idx}] {codec.upper():<6} {q_name:<10} ~{br_kbps} kbps")
            t_choice = input(f"请选择 《{title}》 的档位编号 [默认 0]: ").strip() or "0"
            if t_choice.isdigit() and int(t_choice) < len(tiers):
                target_tier = tiers[int(t_choice)]
            else:
                target_tier = tiers[0]

        if not target_tier:
            continue

        codec = target_tier.get('codec', 'audio')
        br_str = f"~{target_tier.get('bitrate', 0) // 1000}kbps"
        print(f"▶ 正在下载: 《{base_name}》 [{codec.upper()} {br_str}]")

        # process(..., to_mp3=False): 保持解密后的原生格式（FLAC/M4A），不转 MP3
        saved_file = qcache.process(target_tier, qdecrypt, DEFAULT_OUT, base_name, to_mp3=False)
        if saved_file:
            ok_count += 1

    print("\n" + "=" * 60)
    print(f"[3/3] 全部处理完成！成功: {ok_count} / {len(selected_indices)}")
    print(f"文件保存路径: {DEFAULT_OUT}")
    print("=" * 60)
    input("\n按回车键退出...")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n操作已取消。")
        sys.exit(0)
