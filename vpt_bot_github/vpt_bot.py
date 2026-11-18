#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Совместимая точка входа для GitHub Actions и ручного запуска."""

from pathlib import Path
import runpy

base = Path(__file__).resolve().parent
legacy_script = base / "vpt_bot_gemini_togglee.py"
current_script = base / "bot.py"

# Старый файл имеет приоритет, если он действительно присутствует в репозитории.
# В предоставленном архиве его нет, поэтому используется включённый bot.py.
script = legacy_script if legacy_script.is_file() else current_script

if not script.is_file():
    raise SystemExit(
        "Не найден файл бота: ни vpt_bot_gemini_togglee.py, ни bot.py"
    )

runpy.run_path(str(script), run_name="__main__")
