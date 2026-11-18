ИСПРАВЛЕНИЕ ТОЧКИ ВХОДА VPT

В корень исходного репозитория нужно добавить файл vpt_bot.py.
Файл запускает vpt_bot_gemini_togglee.py из той же папки.

ВАЖНО:
- Не удаляйте и не переименовывайте vpt_bot_gemini_togglee.py.
- Существующие файлы state.json, music_cache.json, bells.pdf и latest.xlsx
  оставьте без изменений.
- Файл должен находиться в корне репозитория, рядом с
  vpt_bot_gemini_togglee.py, а не внутри vpt_bot_github/.

Содержимое vpt_bot.py:

from pathlib import Path
import runpy

script = Path(__file__).with_name("vpt_bot_gemini_togglee.py")
runpy.run_path(str(script), run_name="__main__")
