@echo off
chcp 65001 >nul
title 汽水音乐缓存解析与选择下载
cd /d "%~dp0"
python interactive_dl.py
